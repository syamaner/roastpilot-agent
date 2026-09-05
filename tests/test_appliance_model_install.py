"""Tests for secure model placement/verification (AC5, T1-T8 + guard mutations).

Hardware-free and network-free throughout: every HTTPS fetch goes through
``httpx.MockTransport`` (the repo's established fake-transport pattern, see
``tests/test_bean_sourcing.py``), never a real socket.
"""

import hashlib
import json
import stat
from pathlib import Path

import httpx
import pytest

from roastpilot_agent.appliance import model_install as model_install_module
from roastpilot_agent.appliance.model_install import ModelInstallError, install_model
from roastpilot_agent.appliance.model_manifest import ManifestFile

_REVISION = "a" * 40
_REPO_ID = "acme/test-model"


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
        return httpx.Response(200, content=contents[relative])

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
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=wrong))
    )
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

    assert not (outside / "file1.bin").exists()


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


def test_cross_host_https_redirect_is_followed(tmp_path: Path) -> None:
    """Integrity is the committed digest, not the transport host — a
    same-scheme cross-host redirect must still be followed."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"hello")
        return httpx.Response(302, headers={"location": "https://cdn.example/a/file1.bin"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    summary = install_model(
        tmp_path / "dest",
        manifest_files=manifest,
        repo_id=_REPO_ID,
        revision=_REVISION,
        http_client=client,
    )
    assert summary.files[0].source == "fetched"


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


# --- T7: response longer than the byte cap ----------------------------------


def test_response_exceeding_byte_cap_aborts_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_install_module, "_MAX_MODEL_FILE_BYTES", 8)
    manifest = _make_manifest({"a/file1.bin": b"x" * 100})
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"x" * 100))
    )
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


def test_chmod_failure_after_digest_verification_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure in the chmod/replace step (after the digest already matched)
    must still remove the temp file — cleanup is not only a digest-mismatch
    behavior."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"

    def failing_chmod(_path: object, _mode: int) -> None:
        raise OSError("simulated chmod failure")

    monkeypatch.setattr("os.chmod", failing_chmod)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"hello"))
    )

    with pytest.raises(OSError, match="simulated chmod failure"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


def test_replace_failure_after_digest_verification_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure in ``os.replace`` (after ``chmod`` already succeeded) must
    still remove the temp file rather than leaking a verified-but-unplaced
    ``.part`` file."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"

    def failing_replace(_src: object, _dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("os.replace", failing_replace)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"hello"))
    )

    with pytest.raises(OSError, match="simulated replace failure"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


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
