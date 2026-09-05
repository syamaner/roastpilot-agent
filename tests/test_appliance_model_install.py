"""Tests for secure model placement/verification (AC5, T1-T8 + guard mutations).

Hardware-free and network-free throughout: every HTTPS fetch goes through
``httpx.MockTransport`` (the repo's established fake-transport pattern, see
``tests/test_bean_sourcing.py``), never a real socket.
"""

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Iterator
from pathlib import Path

import httpx
import pytest

from roastpilot_agent.appliance import model_install as model_install_module
from roastpilot_agent.appliance.model_install import ModelInstallError, install_model
from roastpilot_agent.appliance.model_manifest import ManifestFile

_REVISION = "a" * 40
_REPO_ID = "acme/test-model"


class _LazyByteStream(httpx.SyncByteStream):
    """A genuinely lazy, not-yet-consumed byte stream for test responses.

    ``httpx.Response(status, content=...)`` eagerly reads (and, per a
    ``Content-Encoding`` header, eagerly *decodes*) the body the moment the
    response is constructed — before this module's own code ever runs. That
    masks two things this module must control itself: rejecting an
    undesired ``Content-Encoding`` before any decoding is attempted, and
    calling ``response.iter_raw()`` (which requires an unconsumed stream,
    unlike ``iter_bytes()``'s fully-materialized-content fallback). Building
    the response with ``stream=`` instead reproduces what a real,
    not-yet-read network response looks like.
    """

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks


def _ok_response(content: bytes, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """Build a lazy-streamed ``200`` response (see :class:`_LazyByteStream`)."""
    return httpx.Response(200, headers=headers or {}, stream=_LazyByteStream([content]))


def _make_manifest(contents: dict[str, bytes]) -> tuple[ManifestFile, ...]:
    """Build a small synthetic manifest whose digests match ``contents``."""
    return tuple(
        ManifestFile(relative_path=path, sha256=hashlib.sha256(data).hexdigest())
        for path, data in contents.items()
    )


def _serving_transport(
    contents: dict[str, bytes], *, call_count: list[int] | None = None
) -> httpx.MockTransport:
    """A fake origin serving exactly ``contents`` under the manifest's URL shape."""
    prefix = f"https://huggingface.co/{_REPO_ID}/resolve/{_REVISION}/"

    def handler(request: httpx.Request) -> httpx.Response:
        if call_count is not None:
            call_count[0] += 1
        url = str(request.url)
        assert url.startswith(prefix), url
        relative = url[len(prefix) :]
        if relative not in contents:
            return httpx.Response(404)
        return _ok_response(contents[relative])

    return httpx.MockTransport(handler)


# --- T1: clean destination + fake origin -> both files placed, verified -----


def test_fetch_places_and_verifies_every_file(tmp_path: Path) -> None:
    contents = {"a/file1.bin": b"hello-world-bytes", "b/file2.json": b'{"ok":true}'}
    manifest = _make_manifest(contents)
    client = httpx.Client(transport=_serving_transport(contents))
    dest = tmp_path / "dest"

    summary = install_model(
        dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
    )

    assert summary.network_used is True
    assert {f.source for f in summary.files} == {"fetched"}
    for relative, data in contents.items():
        placed = dest / relative
        assert placed.read_bytes() == data
        assert stat.S_IMODE(placed.stat().st_mode) == 0o644


# --- T2: served bytes differ by one byte -> non-zero, final path absent -----


def test_digest_mismatch_removes_partial_and_writes_nothing_final(tmp_path: Path) -> None:
    correct = b"hello-world-bytes"
    wrong = correct[:-1] + bytes([correct[-1] ^ 0x01])
    manifest = _make_manifest({"a/file1.bin": correct})
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(wrong)))
    dest = tmp_path / "dest"

    with pytest.raises(ModelInstallError, match="digest mismatch"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


# --- T3: second run with valid files present makes zero network calls ------


def test_second_run_with_valid_files_makes_no_network_call(tmp_path: Path) -> None:
    contents = {"a/file1.bin": b"hello", "b/file2.json": b"{}"}
    manifest = _make_manifest(contents)
    calls = [0]
    client = httpx.Client(transport=_serving_transport(contents, call_count=calls))
    dest = tmp_path / "dest"

    install_model(
        dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
    )
    assert calls[0] == 2

    summary = install_model(
        dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
    )
    assert calls[0] == 2
    assert summary.network_used is False
    assert {f.source for f in summary.files} == {"cached"}


def test_verify_only_rejects_an_initially_oversized_cached_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached verification never hashes a file already over the per-file cap."""
    monkeypatch.setattr(model_install_module, "_MAX_MODEL_FILE_BYTES", 8)
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "file1.bin").write_bytes(b"x" * 9)
    manifest = _make_manifest({"a/file1.bin": b"expected"})

    with pytest.raises(ModelInstallError, match="per-file cap"):
        install_model(
            dest,
            verify_only=True,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
        )


def test_cached_verification_stops_when_file_grows_over_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A growing cached file is rejected before an unbounded hash can occur."""
    monkeypatch.setattr(model_install_module, "_MAX_MODEL_FILE_BYTES", 8)
    monkeypatch.setattr(model_install_module, "_CHUNK_SIZE", 2)
    target = tmp_path / "cached.bin"
    target.write_bytes(b"good")
    original_fdopen = os.fdopen
    grew = False

    class GrowingReader:
        """Append beyond the cap after the first bounded read."""

        def __init__(self, file_handle: object) -> None:
            self._file_handle = file_handle

        def __enter__(self) -> "GrowingReader":
            return self

        def __exit__(self, *args: object) -> None:
            self._file_handle.close()  # type: ignore[union-attr]

        def read(self, size: int = -1) -> bytes:
            nonlocal grew
            result = self._file_handle.read(size)  # type: ignore[union-attr]
            if not grew:
                grew = True
                with target.open("ab") as growing_file:
                    growing_file.write(b"overflow")
            return result  # type: ignore[no-any-return]

    def growing_fdopen(fd: int, mode: str, *, closefd: bool = True) -> GrowingReader:
        return GrowingReader(original_fdopen(fd, mode, closefd=closefd))

    monkeypatch.setattr(os, "fdopen", growing_fdopen)
    expected = hashlib.sha256(b"good").hexdigest()

    with pytest.raises(ModelInstallError, match="per-file cap"):
        model_install_module._is_already_valid(  # pyright: ignore[reportPrivateUsage]
            target, expected
        )


# --- T4: --from-dir missing a file -> error, no fallback to network --------


def test_from_dir_missing_file_errors_without_a_network_call(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello", "b/file2.json": b"{}"})
    from_dir = tmp_path / "src"
    (from_dir / "a").mkdir(parents=True)
    (from_dir / "a" / "file1.bin").write_bytes(b"hello")
    # b/file2.json is intentionally absent from from_dir.
    calls = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "dest"

    with pytest.raises(ModelInstallError, match="from-dir is missing"):
        install_model(
            dest,
            from_dir=from_dir,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
            http_client=client,
        )
    assert calls[0] == 0


def test_from_dir_digest_mismatch_does_not_fall_back_to_network(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"correct-bytes"})
    from_dir = tmp_path / "src"
    (from_dir / "a").mkdir(parents=True)
    (from_dir / "a" / "file1.bin").write_bytes(b"WRONG-BYTES-HERE")
    calls = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(200, content=b"correct-bytes")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "dest"

    with pytest.raises(ModelInstallError, match="digest mismatch"):
        install_model(
            dest,
            from_dir=from_dir,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
            http_client=client,
        )
    assert calls[0] == 0
    assert not (dest / "a" / "file1.bin").exists()


def test_from_dir_copy_places_with_correct_mode(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    from_dir = tmp_path / "src"
    (from_dir / "a").mkdir(parents=True)
    (from_dir / "a" / "file1.bin").write_bytes(b"payload")
    dest = tmp_path / "dest"

    summary = install_model(
        dest, from_dir=from_dir, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
    )

    assert summary.network_used is False
    assert summary.files[0].source == "local"
    placed = dest / "a" / "file1.bin"
    assert placed.read_bytes() == b"payload"
    assert stat.S_IMODE(placed.stat().st_mode) == 0o644


def test_from_dir_symlinked_subdirectory_escape_is_rejected(tmp_path: Path) -> None:
    """A directory symlink inside ``--from-dir`` that points outside it is
    caught the same way the destination-side escape is (directive 4): the
    source root gets exactly the same containment strictness as the
    destination root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file1.bin").write_bytes(b"do-not-read")
    from_dir = tmp_path / "src"
    from_dir.mkdir()
    (from_dir / "evil").symlink_to(outside)
    manifest = _make_manifest({"evil/file1.bin": b"do-not-read"})
    dest = tmp_path / "dest"

    with pytest.raises(ModelInstallError, match="escapes the --from-dir root"):
        install_model(
            dest, from_dir=from_dir, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
        )

    assert not (dest / "evil").exists()


def test_from_dir_symlinked_source_file_is_rejected_as_non_regular(tmp_path: Path) -> None:
    """A manifest file that resolves to a symlink inside ``--from-dir`` is
    refused — never read through — even when the symlink target holds the
    exact expected bytes (directive 4)."""
    real = tmp_path / "real.bin"
    real.write_bytes(b"payload")
    from_dir = tmp_path / "src"
    (from_dir / "a").mkdir(parents=True)
    (from_dir / "a" / "file1.bin").symlink_to(real)
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    dest = tmp_path / "dest"

    with pytest.raises(ModelInstallError, match="not a regular file"):
        install_model(
            dest, from_dir=from_dir, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
        )

    assert not (dest / "a" / "file1.bin").exists()


def test_from_dir_directory_source_is_rejected_as_non_regular(tmp_path: Path) -> None:
    """A manifest file that resolves to a directory inside ``--from-dir`` is
    refused (directive 4)."""
    from_dir = tmp_path / "src"
    (from_dir / "a" / "file1.bin").mkdir(parents=True)
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    dest = tmp_path / "dest"

    with pytest.raises(ModelInstallError, match="not a regular file"):
        install_model(
            dest, from_dir=from_dir, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
        )


def test_cached_destination_wins_even_with_an_invalid_from_dir(tmp_path: Path) -> None:
    """A valid cached destination is used with zero I/O against ``--from-dir``
    — an invalid/missing ``--from-dir`` never surfaces as an error when every
    file is already correctly placed (directive 6a)."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "file1.bin").write_bytes(b"hello")
    missing_from_dir = tmp_path / "does-not-exist-at-all"

    summary = install_model(
        dest,
        from_dir=missing_from_dir,
        manifest_files=manifest,
        repo_id=_REPO_ID,
        revision=_REVISION,
    )

    assert summary.files[0].source == "cached"
    assert summary.network_used is False


# --- T5: closed-grammar rejections, parametrised -----------------------------


@pytest.mark.parametrize("revision", ["", "a" * 39, "a" * 41, "A" * 40, "g" * 40])
def test_invalid_revision_rejected(tmp_path: Path, revision: str) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    with pytest.raises(ModelInstallError, match="revision"):
        install_model(
            tmp_path / "dest", manifest_files=manifest, repo_id=_REPO_ID, revision=revision
        )


@pytest.mark.parametrize("digest", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64])
def test_invalid_digest_rejected(tmp_path: Path, digest: str) -> None:
    manifest = (ManifestFile(relative_path="a/file1.bin", sha256=digest),)
    with pytest.raises(ModelInstallError, match="digest"):
        install_model(
            tmp_path / "dest", manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
        )


@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        "../etc/passwd",
        "/etc/passwd",
        "a/../b",
        "a\\b",
        "a/b\x00c",
        "a//b",
        "a/./b",
        "a/b?c",
    ],
)
def test_invalid_relative_path_rejected(tmp_path: Path, bad_path: str) -> None:
    manifest = (ManifestFile(relative_path=bad_path, sha256="a" * 64),)
    with pytest.raises(ModelInstallError):
        install_model(
            tmp_path / "dest", manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
        )
    dest = tmp_path / "dest"
    if dest.exists():
        assert list(dest.rglob("*")) == []


def test_relative_path_over_byte_cap_rejected(tmp_path: Path) -> None:
    long_path = "a/" + ("b" * 130)
    manifest = (ManifestFile(relative_path=long_path, sha256="a" * 64),)
    with pytest.raises(ModelInstallError, match="exceeds"):
        install_model(
            tmp_path / "dest", manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
        )


def test_symlink_at_destination_is_rejected_and_not_overwritten(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    evil_target = tmp_path / "evil.txt"
    evil_target.write_bytes(b"do-not-touch")
    (dest / "a" / "file1.bin").symlink_to(evil_target)

    with pytest.raises(ModelInstallError, match="non-regular-file"):
        install_model(dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION)

    assert evil_target.read_bytes() == b"do-not-touch"


def test_directory_at_destination_is_rejected(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    (dest / "a" / "file1.bin").mkdir(parents=True)

    with pytest.raises(ModelInstallError, match="non-regular-file"):
        install_model(dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX-only")
def test_fifo_at_destination_is_rejected(tmp_path: Path) -> None:
    """A portable non-regular entry (a FIFO, where the platform supports one)
    at the destination is refused exactly like a symlink or directory
    (directive 6c)."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    os.mkfifo(dest / "a" / "file1.bin")

    with pytest.raises(ModelInstallError, match="non-regular-file"):
        install_model(dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION)


def test_symlinked_subdirectory_escape_is_rejected(tmp_path: Path) -> None:
    """A directory symlink inside dest that points outside it is still caught
    (defense in depth beyond the '..'/leading-'/' grammar check, since the
    manifest's own relative-path segments never contain '..')."""
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "evil").symlink_to(outside)
    manifest = _make_manifest({"evil/file1.bin": b"hello"})

    with pytest.raises(ModelInstallError, match="escapes"):
        install_model(dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION)


def test_swapped_destination_parent_cannot_redirect_placement_outside_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent swapped for a symlink after descriptor open cannot redirect writes."""
    dest = tmp_path / "dest"
    parent = dest / "a"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    parked_parent = tmp_path / "parked-parent"
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    original_stream_to_temp = model_install_module._stream_to_temp  # pyright: ignore[reportPrivateUsage]

    def swap_parent_then_stream(
        parent_fd: int, target_name: str, chunks: Iterable[bytes], *, byte_cap: int
    ) -> tuple[str, int, str]:
        parent.rename(parked_parent)
        parent.symlink_to(outside, target_is_directory=True)
        return original_stream_to_temp(parent_fd, target_name, chunks, byte_cap=byte_cap)

    monkeypatch.setattr(model_install_module, "_stream_to_temp", swap_parent_then_stream)
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"payload")))

    summary = install_model(
        dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
    )

    assert summary.files[0].source == "fetched"
    assert not (outside / "file1.bin").exists()
    assert (parked_parent / "file1.bin").read_bytes() == b"payload"


@pytest.mark.parametrize("bad_repo_id", ["", "no-slash", "a/b/c", "a/", "/b", "a b/c"])
def test_invalid_repo_id_rejected(tmp_path: Path, bad_repo_id: str) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    with pytest.raises(ModelInstallError, match="repo_id"):
        install_model(
            tmp_path / "dest", manifest_files=manifest, repo_id=bad_repo_id, revision=_REVISION
        )


def test_empty_manifest_rejected(tmp_path: Path) -> None:
    with pytest.raises(ModelInstallError, match="must not be empty"):
        install_model(tmp_path / "dest", manifest_files=(), repo_id=_REPO_ID, revision=_REVISION)


# --- T6: redirect abuse ------------------------------------------------------


def test_redirect_to_http_aborts(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(302, headers={"location": "http://evil.example/a/file1.bin"})
        )
    )
    with pytest.raises(ModelInstallError, match="https"):
        install_model(
            tmp_path / "dest",
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
            http_client=client,
        )


def test_redirect_chain_longer_than_bound_aborts(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(302, headers={"location": str(request.url)})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ModelInstallError, match="redirects"):
        install_model(
            tmp_path / "dest",
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
            http_client=client,
        )
    assert calls[0] == 6  # one initial request + _MAX_REDIRECTS (5) hops


def test_malformed_redirect_location_aborts(tmp_path: Path) -> None:
    """A malformed ``Location`` header aborts fail-closed and writes nothing.

    httpx itself eagerly parses/validates the redirect URL inside
    ``client.stream()``/``send()`` (even with ``follow_redirects=False``, it
    always builds the redirect request to decide whether to *follow* it), so
    this surfaces through the generic network-error path rather than this
    module's own (defense-in-depth) join try/except — see
    ``_iter_https_bytes``'s comment. Either way the outcome must be a
    fail-closed :class:`ModelInstallError` with no file written.
    """
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(302, headers={"location": "http://[::1"})
        )
    )
    with pytest.raises(ModelInstallError):
        install_model(
            dest,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
            http_client=client,
        )
    assert not (dest / "a" / "file1.bin").exists()


@pytest.mark.parametrize("cdn_host", ["hf.co", "cdn-lfs.hf.co", "cdn-lfs-us-1.hf.co"])
def test_allowed_cross_host_cdn_redirect_is_followed(tmp_path: Path, cdn_host: str) -> None:
    """A same-scheme redirect to a host within the small HF-operated policy
    (``huggingface.co``, ``hf.co``, or any ``*.hf.co`` subdomain) must still
    be followed — host trust and byte-integrity are independent controls,
    and a legitimate CDN redirect satisfies both."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == cdn_host:
            return _ok_response(b"hello")
        return httpx.Response(302, headers={"location": f"https://{cdn_host}/a/file1.bin"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    summary = install_model(
        tmp_path / "dest",
        manifest_files=manifest,
        repo_id=_REPO_ID,
        revision=_REVISION,
        http_client=client,
    )
    assert summary.files[0].source == "fetched"


@pytest.mark.parametrize(
    ("location", "expected_reason"),
    [
        ("https://internal.example/a/file1.bin", "outside the allowed policy"),
        ("https://evil.hf.co.attacker.example/a/file1.bin", "outside the allowed policy"),
        ("https://notreallyhf.co/a/file1.bin", "outside the allowed policy"),
        ("https://203.0.113.7/a/file1.bin", "IP-literal host"),
        ("https://[2001:db8::1]/a/file1.bin", "IP-literal host"),
        ("https://user:pass@hf.co/a/file1.bin", "embedded userinfo"),
        ("https://hf.co/a/file1.bin#fragment", "a fragment"),
        ("https://hf.co:8443/a/file1.bin", "non-default port"),
    ],
    ids=[
        "arbitrary-internal-host",
        "hf.co-lookalike-suffix",
        "hf.co-lookalike-prefix",
        "ipv4-literal",
        "ipv6-literal",
        "userinfo",
        "fragment",
        "non-default-port",
    ],
)
def test_disallowed_redirect_destination_aborts_with_no_file_written(
    tmp_path: Path, location: str, expected_reason: str
) -> None:
    """Every disallowed redirect-hop shape is rejected before the hop is made:
    an arbitrary/internal host, a hostname that merely *looks* like it is
    within the ``hf.co`` policy (suffix/prefix lookalikes), an IP literal
    (v4 or v6), embedded userinfo, a fragment, and a non-default port — none
    ever leaves a final or partial file behind.

    Each case also asserts the specific :class:`ModelInstallError` reason for
    its rejected class, and that exactly one HTTP request is ever made: the
    initial allowed Hugging Face request that returns the redirect. The
    disallowed redirect target itself is never connected to —
    ``_validate_fetch_url`` rejects the joined redirect URL, on the next loop
    iteration, before ``client.stream`` is called again — so the request
    counter proves validation happens before any connection attempt, not
    merely that the outcome is an error.
    """
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    calls = [0]

    def redirect_handler(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(302, headers={"location": location})

    client = httpx.Client(transport=httpx.MockTransport(redirect_handler))

    with pytest.raises(ModelInstallError, match=expected_reason):
        install_model(
            dest,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
            http_client=client,
        )

    assert calls[0] == 1
    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


def test_unexpected_http_status_aborts(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    with pytest.raises(ModelInstallError, match="unexpected HTTP status"):
        install_model(
            tmp_path / "dest",
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
            http_client=client,
        )


# --- Content-Encoding / declared Content-Length rejection (transparent-decompression) --


def test_response_with_content_encoding_is_rejected(tmp_path: Path) -> None:
    """A response declaring any ``Content-Encoding`` is refused outright,
    never transparently decoded, even though ``Accept-Encoding: identity``
    was sent — a byte-cap check that only inspects the wire bytes must never
    be bypassable by a compressed body that expands after decoding."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: _ok_response(b"hello", headers={"content-encoding": "gzip"})
        )
    )

    with pytest.raises(ModelInstallError, match="Content-Encoding"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


def test_response_with_oversized_declared_content_length_is_rejected_before_reading_body(
    tmp_path: Path,
) -> None:
    """A declared ``Content-Length`` over the byte cap is rejected before any
    body bytes are read — a cheap pre-check ahead of the streaming backstop."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    oversized = model_install_module._MAX_MODEL_FILE_BYTES + 1  # pyright: ignore[reportPrivateUsage]
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200, headers={"content-length": str(oversized)}, content=b"hello"
            )
        )
    )

    with pytest.raises(ModelInstallError, match="Content-Length"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


def test_response_with_valid_in_cap_content_length_is_fetched_normally(tmp_path: Path) -> None:
    """A declared ``Content-Length`` that is a valid integer within the byte
    cap passes the pre-check and the file is fetched and placed normally —
    the pre-check must never reject a legitimate response."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: _ok_response(b"hello", headers={"content-length": "5"})
        )
    )

    summary = install_model(
        dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
    )

    assert summary.files[0].source == "fetched"
    assert (dest / "a" / "file1.bin").read_bytes() == b"hello"


def test_response_with_non_numeric_content_length_falls_back_to_the_streaming_cap(
    tmp_path: Path,
) -> None:
    """A ``Content-Length`` header that is not a valid integer is ignored by
    the declared-length pre-check (never crashes it) and the file still
    places correctly once the real streamed bytes are hashed and verified —
    the streaming cap in :func:`_stream_to_temp` remains the authoritative
    backstop for this case."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: _ok_response(b"hello", headers={"content-length": "not-a-number"})
        )
    )

    summary = install_model(
        dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
    )

    assert summary.files[0].source == "fetched"
    assert (dest / "a" / "file1.bin").read_bytes() == b"hello"


# --- T7: response longer than the byte cap ----------------------------------


def test_response_exceeding_byte_cap_aborts_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_install_module, "_MAX_MODEL_FILE_BYTES", 8)
    manifest = _make_manifest({"a/file1.bin": b"x" * 100})
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"x" * 100)))
    dest = tmp_path / "dest"

    with pytest.raises(ModelInstallError, match="cap"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


# --- T8: timeout/connection error --------------------------------------------


def test_network_error_aborts_with_no_url_or_env_value_in_message(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    manifest = _make_manifest({"a/file1.bin": b"hello"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "dest"

    with pytest.raises(ModelInstallError) as exc_info:
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    message = str(exc_info.value)
    assert "?" not in message
    assert "huggingface.co" not in message
    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


def test_fchmod_failure_after_digest_verification_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure in the fchmod/replace step (after the digest already matched)
    must still remove the temp file — cleanup is not only a digest-mismatch
    behavior."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"

    def failing_fchmod(_fd: int, _mode: int) -> None:
        raise OSError("simulated fchmod failure")

    monkeypatch.setattr("os.fchmod", failing_fchmod)
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"hello")))

    with pytest.raises(OSError, match="simulated fchmod failure"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


def test_placement_uses_fchmod_without_pathname_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Placement changes the verified temporary file mode through its descriptor."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    fchmod_calls: list[tuple[int, int]] = []
    original_fchmod = os.fchmod

    def spying_fchmod(fd: int, mode: int) -> None:
        fchmod_calls.append((fd, mode))
        original_fchmod(fd, mode)

    def unexpected_chmod(*_args: object, **_kwargs: object) -> None:
        pytest.fail("placement must not use pathname-based chmod")

    monkeypatch.setattr(os, "fchmod", spying_fchmod)
    monkeypatch.setattr(os, "chmod", unexpected_chmod)
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"hello")))

    install_model(
        dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
    )

    assert len(fchmod_calls) == 1
    assert fchmod_calls[0][1] == 0o644
    assert stat.S_IMODE((dest / "a" / "file1.bin").stat().st_mode) == 0o644


def test_replace_failure_after_digest_verification_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure in ``os.replace`` (after ``chmod`` already succeeded) must
    still remove the temp file rather than leaking a verified-but-unplaced
    ``.part`` file."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"

    def failing_replace(_src: object, _dst: object, **_kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("os.replace", failing_replace)
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"hello")))

    with pytest.raises(OSError, match="simulated replace failure"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


def test_replace_is_called_with_the_temp_file_beside_the_exact_final_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.replace`` is called with the streamed temp file sitting beside
    (same parent directory as) the exact final target path — never some
    other location — so the rename really is the same-filesystem atomic
    placement the module docstring promises (directive 6e)."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    target = dest / "a" / "file1.bin"
    calls: list[tuple[str, str, int, int]] = []
    original_replace = os.replace

    def spying_replace(src: str, dst: str, **kwargs: object) -> None:
        src_dir_fd = kwargs["src_dir_fd"]
        dst_dir_fd = kwargs["dst_dir_fd"]
        assert isinstance(src_dir_fd, int)
        assert isinstance(dst_dir_fd, int)
        calls.append((src, dst, src_dir_fd, dst_dir_fd))
        original_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr("os.replace", spying_replace)
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"hello")))

    summary = install_model(
        dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
    )

    assert summary.files[0].source == "fetched"
    assert len(calls) == 1
    src, dst, src_dir_fd, dst_dir_fd = calls[0]
    assert src.startswith(".file1.bin.") and src.endswith(".part")
    assert dst == "file1.bin"
    assert src_dir_fd == dst_dir_fd
    assert target.read_bytes() == b"hello"


# --- verify-only -------------------------------------------------------------


def test_verify_only_succeeds_when_everything_already_matches(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "file1.bin").write_bytes(b"hello")

    summary = install_model(
        dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, verify_only=True
    )
    assert summary.network_used is False
    assert summary.files[0].source == "cached"


def test_verify_only_fails_when_destination_absent(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    with pytest.raises(ModelInstallError, match="does not exist"):
        install_model(
            tmp_path / "dest",
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
            verify_only=True,
        )


def test_verify_only_fails_on_mismatch_without_writing(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "file1.bin").write_bytes(b"WRONG")

    with pytest.raises(ModelInstallError, match="verification failed"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, verify_only=True
        )
    assert (dest / "a" / "file1.bin").read_bytes() == b"WRONG"


# --- summary / json shape -----------------------------------------------------


def test_summary_json_dict_carries_no_url_and_reports_source(tmp_path: Path) -> None:
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "file1.bin").write_bytes(b"hello")

    summary = install_model(dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION)
    payload = summary.to_json_dict()
    text = json.dumps(payload)

    assert "http" not in text
    assert payload["network_used"] is False
    files = payload["files"]
    assert isinstance(files, list)
    assert files[0]["source"] == "cached"
    assert files[0]["relative_path"] == "a/file1.bin"


def test_install_model_owns_and_closes_its_own_client_when_unused(tmp_path: Path) -> None:
    """When every file is already cached, an internally-created client is
    never used for a network call and is still cleaned up without error."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "file1.bin").write_bytes(b"hello")

    summary = install_model(dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION)
    assert summary.network_used is False


def test_internal_http_client_disables_environment_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The internally-owned production ``httpx.Client`` — the one
    ``install_model`` constructs itself when no ``http_client`` is supplied —
    is built with ``trust_env=False`` so it never inherits an ambient
    HTTP_PROXY/HTTPS_PROXY/NO_PROXY or CA-bundle environment value
    (directive 3). Captured via a constructor spy so no real network request
    is ever performed: the spy substitutes a ``MockTransport`` before
    delegating to the real ``httpx.Client.__init__``."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    captured_kwargs: dict[str, object] = {}
    real_client_cls = httpx.Client

    def spying_client(*args: object, **kwargs: object) -> httpx.Client:
        captured_kwargs.update(kwargs)
        kwargs = dict(kwargs)
        kwargs["transport"] = httpx.MockTransport(lambda _r: _ok_response(b"hello"))
        return real_client_cls(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", spying_client)

    summary = install_model(dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION)

    assert captured_kwargs.get("trust_env") is False
    assert summary.files[0].source == "fetched"


def test_outbound_request_has_no_credential_and_expected_anonymous_headers(tmp_path: Path) -> None:
    """The one real network call this module ever makes carries no
    ``Authorization``/credential header, and does carry the fixed anonymous
    headers: ``Accept-Encoding: identity`` and the expected User-Agent
    (directive 5)."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    captured_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(request.headers)
        return _ok_response(b"hello")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    install_model(
        tmp_path / "dest",
        manifest_files=manifest,
        repo_id=_REPO_ID,
        revision=_REVISION,
        http_client=client,
    )

    assert len(captured_headers) == 1
    headers = captured_headers[0]
    assert "authorization" not in headers
    assert "cookie" not in headers
    assert all("token" not in name.lower() for name in headers)
    assert headers["accept-encoding"] == "identity"
    assert headers["user-agent"] == "roastpilot-agent-appliance-model-install/1"


def test_configured_byte_cap_is_at_least_90_000_000_bytes() -> None:
    """The per-file byte cap must comfortably exceed the pinned int8 model's
    real size (~89.9 MB, `roastpilot-agent/plan.md:30`) — directive 6d."""
    assert (
        model_install_module._MAX_MODEL_FILE_BYTES  # pyright: ignore[reportPrivateUsage]
        >= 90_000_000
    )


# --- #138 invariant: cleanly separate from the roast advisor / control path --


def test_model_install_module_does_not_directly_import_controller_safety_or_mcp_client() -> None:
    """Static check on this module's own import statements (directive 6f,
    mirroring the established #573 pattern in ``test_bean_sourcing.py``)."""
    import ast

    tree = ast.parse(Path(str(model_install_module.__file__)).read_text())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
    forbidden = {
        "roastpilot_agent.controller",
        "roastpilot_agent.safety",
        "roastpilot_agent.mcp_client",
    }
    assert imported_modules.isdisjoint(forbidden)


def test_model_install_never_transitively_imports_controller_safety_or_mcp_client() -> None:
    """Authoritative transitive-import check, run in a FRESH subprocess so an
    already-imported module elsewhere in this test session cannot produce a
    false pass (mirrors ``test_bean_sourcing.py``'s established #573
    pattern)."""
    import subprocess
    import sys

    script = (
        "import sys\n"
        "import roastpilot_agent.appliance.model_install\n"
        "loaded = {m for m in sys.modules if m.startswith('roastpilot_agent.')}\n"
        "forbidden = {\n"
        "    'roastpilot_agent.controller',\n"
        "    'roastpilot_agent.safety',\n"
        "    'roastpilot_agent.mcp_client',\n"
        "}\n"
        "hit = sorted(loaded & forbidden)\n"
        "print(','.join(hit))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        "appliance.model_install transitively imported forbidden modules: "
        f"{result.stdout.strip()}\nstderr: {result.stderr}"
    )


# --- direct unit coverage for small private helpers --------------------------


def test_is_already_valid_treats_a_symlink_as_not_valid(tmp_path: Path) -> None:
    real = tmp_path / "real.bin"
    real.write_bytes(b"hello")
    link = tmp_path / "link.bin"
    link.symlink_to(real)
    digest = hashlib.sha256(b"hello").hexdigest()

    is_valid = model_install_module._is_already_valid(  # pyright: ignore[reportPrivateUsage]
        link, digest
    )
    assert is_valid is False


def test_is_already_valid_treats_missing_as_not_valid(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"hello").hexdigest()
    is_valid = model_install_module._is_already_valid(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "missing.bin", digest
    )
    assert is_valid is False


def test_build_url_uses_only_manifest_fields() -> None:
    url = model_install_module._build_url(  # pyright: ignore[reportPrivateUsage]
        "acme/x", _REVISION, "onnx/int8/model.onnx"
    )
    assert url == f"https://huggingface.co/acme/x/resolve/{_REVISION}/onnx/int8/model.onnx"
