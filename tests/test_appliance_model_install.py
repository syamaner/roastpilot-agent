"""Tests for secure model placement/verification (AC5, T1-T8 + guard mutations).

Hardware-free and network-free throughout: every HTTPS fetch goes through
``httpx.MockTransport`` (the repo's established fake-transport pattern, see
``tests/test_bean_sourcing.py``), never a real socket.
"""

import fcntl
import hashlib
import json
import os
import stat
import time
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


@pytest.mark.parametrize("verify_only", [False, True])
def test_cached_file_requires_exact_service_readable_mode(
    tmp_path: Path, verify_only: bool
) -> None:
    """A digest match does not override the MCP service's required 0644 mode."""
    manifest = _make_manifest({"a/file1.bin": b"verified"})
    target = tmp_path / "dest" / "a" / "file1.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"verified")
    target.chmod(0o600)
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"verified")))

    with pytest.raises(ModelInstallError, match="service-readable mode 0644"):
        install_model(
            tmp_path / "dest",
            verify_only=verify_only,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
            http_client=client,
        )

    assert target.read_bytes() == b"verified"


def test_reclaim_abandoned_part_removes_only_exact_safe_candidate(tmp_path: Path) -> None:
    """Descriptor-checked stale parts are reclaimed without touching lookalikes."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    stale = tmp_path / ".model.bin.0123456789abcdef0123456789abcdef.part"
    stale.write_bytes(b"stale")
    stale.chmod(0o600)
    lookalike = tmp_path / ".model.bin.not-a-uuid.part"
    lookalike.write_bytes(b"keep")
    try:
        model_install_module._reclaim_abandoned_parts(  # pyright: ignore[reportPrivateUsage]
            parent_fd, "model.bin"
        )
    finally:
        os.close(parent_fd)

    assert not stale.exists()
    assert lookalike.read_bytes() == b"keep"


def test_reclaim_rejects_unsafe_matching_part_without_deleting_it(tmp_path: Path) -> None:
    """A matching name is not sufficient authority to delete an unsafe entry."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    stale = tmp_path / ".model.bin.0123456789abcdef0123456789abcdef.part"
    stale.write_bytes(b"unsafe")
    stale.chmod(0o644)
    try:
        with pytest.raises(ModelInstallError, match="unsafe abandoned"):
            model_install_module._reclaim_abandoned_parts(  # pyright: ignore[reportPrivateUsage]
                parent_fd, "model.bin"
            )
    finally:
        os.close(parent_fd)

    assert stale.read_bytes() == b"unsafe"


def test_reclaim_rejects_matching_hardlinked_part_without_deleting_it(tmp_path: Path) -> None:
    """A hardlinked matching part cannot be an abandoned private temp file."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    stale = tmp_path / ".model.bin.0123456789abcdef0123456789abcdef.part"
    stale.write_bytes(b"unsafe")
    stale.chmod(0o600)
    alias = tmp_path / "alias"
    os.link(stale, alias)
    try:
        with pytest.raises(ModelInstallError, match="unsafe abandoned"):
            model_install_module._reclaim_abandoned_parts(  # pyright: ignore[reportPrivateUsage]
                parent_fd, "model.bin"
            )
    finally:
        os.close(parent_fd)

    assert stale.exists()
    assert alias.exists()


def test_reclaim_rejects_matching_fifo_without_opening_or_deleting_it(tmp_path: Path) -> None:
    """A FIFO matching the namespace is opened non-blocking then rejected."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    stale = tmp_path / ".model.bin.0123456789abcdef0123456789abcdef.part"
    os.mkfifo(stale, 0o600)
    try:
        with pytest.raises(ModelInstallError, match="unsafe abandoned"):
            model_install_module._reclaim_abandoned_parts(  # pyright: ignore[reportPrivateUsage]
                parent_fd, "model.bin"
            )
    finally:
        os.close(parent_fd)

    assert stat.S_ISFIFO(os.lstat(stale).st_mode)


def test_install_reclaims_safe_partial_before_fetching(tmp_path: Path) -> None:
    """A crash-left safe partial cannot accumulate across a later installation."""
    manifest = _make_manifest({"a/file1.bin": b"verified"})
    parent = tmp_path / "dest" / "a"
    parent.mkdir(parents=True)
    stale = parent / ".file1.bin.0123456789abcdef0123456789abcdef.part"
    stale.write_bytes(b"stale")
    stale.chmod(0o600)
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"verified")))

    summary = install_model(
        tmp_path / "dest",
        manifest_files=manifest,
        repo_id=_REPO_ID,
        revision=_REVISION,
        http_client=client,
    )

    assert summary.files[0].source == "fetched"
    assert not stale.exists()


def test_destination_parent_lock_refuses_contention_then_allows_after_release(
    tmp_path: Path,
) -> None:
    """The advisory lock rejects a concurrent installer and releases on close."""
    first_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    second_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        model_install_module._lock_destination_parent(first_fd)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(ModelInstallError, match="another model installation"):
            model_install_module._lock_destination_parent(second_fd)  # pyright: ignore[reportPrivateUsage]
        os.close(first_fd)
        first_fd = -1
        model_install_module._lock_destination_parent(second_fd)  # pyright: ignore[reportPrivateUsage]
    finally:
        if first_fd != -1:
            os.close(first_fd)
        os.close(second_fd)


def test_reclaim_candidate_identity_swap_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-name replacement between descriptor validation and unlink fails closed."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    stale = tmp_path / ".model.bin.0123456789abcdef0123456789abcdef.part"
    stale.write_bytes(b"stale")
    stale.chmod(0o600)
    original_lstat = os.lstat
    calls = [0]

    def swap_on_second_lstat(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        calls[0] += 1
        if calls[0] == 2:
            stale.unlink()
            stale.write_bytes(b"replacement")
            stale.chmod(0o600)
        return original_lstat(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "lstat", swap_on_second_lstat)
    try:
        with pytest.raises(ModelInstallError, match="changed during reclamation"):
            model_install_module._reclaim_abandoned_parts(  # pyright: ignore[reportPrivateUsage]
                parent_fd, "model.bin"
            )
    finally:
        os.close(parent_fd)

    assert stale.read_bytes() == b"replacement"


def test_cached_open_and_post_hash_identity_failures_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached verification translates open and post-hash disappearance races."""
    target = tmp_path / "cached.bin"
    target.write_bytes(b"payload")
    original_open = os.open

    def fail_open(*_args: object, **_kwargs: object) -> int:
        raise OSError("simulated cached open failure")

    monkeypatch.setattr(os, "open", fail_open)
    with pytest.raises(ModelInstallError, match="cannot safely verify cached"):
        model_install_module._open_cached_file(target)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(os, "open", original_open)

    original_stat = os.stat

    def disappear_after_hash(*_args: object, **_kwargs: object) -> os.stat_result:
        raise FileNotFoundError

    monkeypatch.setattr(os, "stat", disappear_after_hash)
    with pytest.raises(ModelInstallError, match="changed while being verified"):
        model_install_module._open_verified_cached_file(target)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(os, "stat", original_stat)


def test_cached_hash_rechecks_final_size_and_successfully_closes(tmp_path: Path) -> None:
    """Direct bounded hashing detects a changed final stat and closes on success."""
    target = tmp_path / "cached.bin"
    target.write_bytes(b"payload")
    digest, _device, _inode = model_install_module._sha256_of_file(  # pyright: ignore[reportPrivateUsage]
        target
    )
    assert digest == hashlib.sha256(b"payload").hexdigest()


def test_destination_parent_and_root_reject_non_directory_descriptors(tmp_path: Path) -> None:
    """Trust validators reject regular-file descriptors before any path operation."""
    candidate = tmp_path / "not-a-directory"
    candidate.write_bytes(b"x")
    fd = os.open(candidate, os.O_RDONLY)
    try:
        with pytest.raises(ModelInstallError, match="parent is not a directory"):
            model_install_module._validate_destination_parent_trust(  # pyright: ignore[reportPrivateUsage]
                fd
            )
        with pytest.raises(ModelInstallError, match="root is not a directory"):
            model_install_module._validate_destination_root_trust(  # pyright: ignore[reportPrivateUsage]
                fd, allow_sticky=False
            )
    finally:
        os.close(fd)


def test_destination_lock_and_reclaim_filesystem_errors_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock/list/open/unlink failures fail closed without deleting unknown data."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_flock = fcntl.flock

    def fail_flock(_fd: int, _flags: int) -> None:
        raise OSError("busy")

    monkeypatch.setattr(fcntl, "flock", fail_flock)
    try:
        with pytest.raises(ModelInstallError, match="another model installation"):
            model_install_module._lock_destination_parent(parent_fd)  # pyright: ignore[reportPrivateUsage]
    finally:
        monkeypatch.setattr(fcntl, "flock", original_flock)

    def fail_listdir(_fd: int) -> list[str]:
        raise OSError("no list")

    monkeypatch.setattr(os, "listdir", fail_listdir)
    try:
        with pytest.raises(ModelInstallError, match="cannot safely inspect"):
            model_install_module._reclaim_abandoned_parts(  # pyright: ignore[reportPrivateUsage]
                parent_fd, "model.bin"
            )
    finally:
        os.close(parent_fd)


def test_cached_hash_rejects_final_size_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The descriptor's final size must still agree after bounded hashing."""
    target = tmp_path / "cached.bin"
    target.write_bytes(b"payload")
    fd = os.open(target, os.O_RDONLY)
    original_fstat = os.fstat
    calls = [0]

    def changed_final_size(candidate_fd: int) -> os.stat_result:
        result = original_fstat(candidate_fd)
        calls[0] += 1
        if candidate_fd == fd and calls[0] == 2:
            synthetic = list(result)
            synthetic[stat.ST_SIZE] += 1
            return os.stat_result(synthetic)
        return result

    monkeypatch.setattr(os, "fstat", changed_final_size)
    try:
        with pytest.raises(ModelInstallError, match="changed while being verified"):
            model_install_module._sha256_of_open_file(  # pyright: ignore[reportPrivateUsage]
                fd, target
            )
    finally:
        os.close(fd)


def test_reclaim_open_and_unlink_failures_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reclamation does not turn a racing open or durable unlink into a raw success."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    name = ".model.bin.0123456789abcdef0123456789abcdef.part"
    stale = tmp_path / name
    stale.write_bytes(b"stale")
    stale.chmod(0o600)
    original_open = os.open

    def fail_candidate_open(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == name:
            raise OSError("race")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", fail_candidate_open)
    try:
        with pytest.raises(ModelInstallError, match="changed during reclamation"):
            model_install_module._reclaim_abandoned_parts(  # pyright: ignore[reportPrivateUsage]
                parent_fd, "model.bin"
            )
    finally:
        monkeypatch.setattr(os, "open", original_open)

    def fail_unlink(_path: object, **_kwargs: object) -> None:
        raise OSError("unlink")

    monkeypatch.setattr(os, "unlink", fail_unlink)
    try:
        with pytest.raises(ModelInstallError, match="cannot safely reclaim"):
            model_install_module._reclaim_abandoned_parts(  # pyright: ignore[reportPrivateUsage]
                parent_fd, "model.bin"
            )
    finally:
        os.close(parent_fd)


def test_source_open_missing_and_root_errors_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source descriptor traversal normalises missing and local-root failures."""
    with pytest.raises(ModelInstallError, match="from-dir is missing"):
        model_install_module._open_verified_source_file(  # pyright: ignore[reportPrivateUsage]
            tmp_path / "missing", "a/file.bin"
        )

    source_root = tmp_path / "source"
    source_root.mkdir()
    original_open = os.open

    def fail_root_open(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == source_root:
            raise OSError("permission")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", fail_root_open)
    with pytest.raises(ModelInstallError, match="cannot safely open the --from-dir root"):
        model_install_module._open_verified_source_file(  # pyright: ignore[reportPrivateUsage]
            source_root, "a/file.bin"
        )


def test_source_final_missing_is_typed_after_parent_open(tmp_path: Path) -> None:
    """A final source disappearance remains the explicit no-fallback error."""
    source_root = tmp_path / "source"
    (source_root / "a").mkdir(parents=True)
    with pytest.raises(ModelInstallError, match="from-dir is missing"):
        model_install_module._open_verified_source_file(  # pyright: ignore[reportPrivateUsage]
            source_root, "a/file.bin"
        )


def test_source_parent_close_failure_releases_child_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Traversal closes an opened child if the preceding source parent close fails."""
    source_root = tmp_path / "source"
    (source_root / "a").mkdir(parents=True)
    original_open = os.open
    original_close = os.close
    root_fds: list[int] = []
    child_fds: list[int] = []
    close_calls: list[int] = []

    def record_open(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == source_root:
            root_fds.append(fd)
        elif path == "a":
            child_fds.append(fd)
        return fd

    def fail_root_close(fd: int) -> None:
        close_calls.append(fd)
        if root_fds and fd == root_fds[0]:
            raise OSError("source parent close")
        original_close(fd)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", fail_root_close)
    try:
        with pytest.raises(OSError, match="source parent close"):
            model_install_module._open_verified_source_file(  # pyright: ignore[reportPrivateUsage]
                source_root, "a/file.bin"
            )
    finally:
        for fd in root_fds:
            original_close(fd)

    assert child_fds and close_calls.count(child_fds[0]) == 1


def test_destination_walkers_normalise_mkdir_and_disappearance_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both descriptor walkers map mkdir and post-mkdir disappearance races safely."""
    root = tmp_path / "root"
    original_mkdir = os.mkdir

    def fail_root_mkdir(path: Path | str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if path == root.name:
            raise OSError("mkdir root")
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", fail_root_mkdir)
    with pytest.raises(ModelInstallError, match="cannot safely create the model destination root"):
        model_install_module._open_destination_root(  # pyright: ignore[reportPrivateUsage]
            root, create=True
        )
    monkeypatch.setattr(os, "mkdir", original_mkdir)

    root.mkdir()
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)

    def fail_parent_mkdir(
        path: Path | str, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> None:
        if path == "a":
            raise OSError("mkdir parent")
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", fail_parent_mkdir)
    try:
        with pytest.raises(ModelInstallError, match="cannot safely create destination directory"):
            model_install_module._open_destination_parent(  # pyright: ignore[reportPrivateUsage]
                root_fd, "a/model.bin", create=True
            )
    finally:
        os.close(root_fd)


def test_destination_parent_disappearance_after_mkdir_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A just-created component is reopened; disappearance cannot be blessed."""
    root = tmp_path / "root"
    root.mkdir()
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    original_open = os.open

    def remove_before_reopen(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == "a" and (root / "a").exists():
            (root / "a").rmdir()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", remove_before_reopen)
    try:
        with pytest.raises(ModelInstallError, match="destination directory disappeared"):
            model_install_module._open_destination_parent(  # pyright: ignore[reportPrivateUsage]
                root_fd, "a/model.bin", create=True
            )
    finally:
        os.close(root_fd)


def test_destination_parent_creation_stat_and_open_errors_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent walker never exposes raw stat/open races after mkdir."""
    root = tmp_path / "root"
    root.mkdir()
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    original_stat = os.stat

    def fail_created_stat(path: Path | str, *args: object, **kwargs: object) -> os.stat_result:
        if path == "a":
            raise OSError("created stat")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fail_created_stat)
    try:
        with pytest.raises(ModelInstallError, match="changed while being created"):
            model_install_module._open_destination_parent(  # pyright: ignore[reportPrivateUsage]
                root_fd, "a/model.bin", create=True
            )
    finally:
        monkeypatch.setattr(os, "stat", original_stat)
        if (root / "a").exists():
            (root / "a").rmdir()

    original_open = os.open

    def fail_component_open(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == "a":
            raise OSError("component open")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", fail_component_open)
    try:
        with pytest.raises(ModelInstallError, match="cannot safely open destination directory"):
            model_install_module._open_destination_parent(  # pyright: ignore[reportPrivateUsage]
                root_fd, "a/model.bin", create=False
            )
    finally:
        os.close(root_fd)


def test_placement_accepts_existing_regular_target(tmp_path: Path) -> None:
    """The final overwrite check permits only a regular existing target."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    target = tmp_path / "model.bin"
    target.write_bytes(b"old")
    try:
        model_install_module._place_verified(  # pyright: ignore[reportPrivateUsage]
            parent_fd,
            "model.bin",
            [b"new"],
            expected_sha256=hashlib.sha256(b"new").hexdigest(),
            byte_cap=8,
            context="model.bin",
        )
    finally:
        os.close(parent_fd)
    assert target.read_bytes() == b"new"


def test_live_destination_revalidation_normalises_missing_root_and_target(tmp_path: Path) -> None:
    """Live-path confirmation converts root and final-entry disappearance to typed errors."""
    dest = tmp_path / "dest"
    parent = dest / "a"
    target = parent / "file.bin"
    parent.mkdir(parents=True)
    target.write_bytes(b"payload")
    root_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    verified = target.stat()
    try:
        target.unlink()
        with pytest.raises(ModelInstallError, match="destination changed before placement"):
            model_install_module._revalidate_live_destination(  # pyright: ignore[reportPrivateUsage]
                dest,
                "a/file.bin",
                dest_root_fd=root_fd,
                parent_fd=parent_fd,
                device=verified.st_dev,
                inode=verified.st_ino,
            )
        target.write_bytes(b"payload")
        dest.rename(tmp_path / "parked")
        with pytest.raises(ModelInstallError, match="destination changed before placement"):
            model_install_module._revalidate_live_destination(  # pyright: ignore[reportPrivateUsage]
                dest,
                "a/file.bin",
                dest_root_fd=root_fd,
                parent_fd=parent_fd,
                device=verified.st_dev,
                inode=verified.st_ino,
            )
    finally:
        os.close(parent_fd)
        os.close(root_fd)


def test_install_reraises_non_verify_destination_root_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-verify root-opening absence preserves the local filesystem error."""

    def missing_root(_dest: Path, *, create: bool) -> tuple[Path, int]:
        assert create is True
        raise FileNotFoundError("simulated root race")

    monkeypatch.setattr(model_install_module, "_open_destination_root", missing_root)
    manifest = _make_manifest({"a/file.bin": b"payload"})
    with pytest.raises(FileNotFoundError, match="simulated root race"):
        install_model(
            tmp_path / "dest", manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
        )


def test_from_dir_uses_existing_parent_and_verify_missing_parent_closes_nothing(
    tmp_path: Path,
) -> None:
    """Existing-parent local placement and missing-parent verification take both cleanup arcs."""
    manifest = _make_manifest({"a/file.bin": b"payload"})
    from_dir = tmp_path / "source"
    (from_dir / "a").mkdir(parents=True)
    (from_dir / "a" / "file.bin").write_bytes(b"payload")
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)

    summary = install_model(
        dest, from_dir=from_dir, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
    )
    assert summary.files[0].source == "local"

    missing_parent_dest = tmp_path / "missing-parent-dest"
    missing_parent_dest.mkdir()
    with pytest.raises(ModelInstallError, match="verification failed"):
        install_model(
            missing_parent_dest,
            verify_only=True,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
        )


def test_cached_entry_replacement_after_hash_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached success requires the verified inode to remain named at the target."""
    manifest = _make_manifest({"a/file1.bin": b"verified"})
    dest = tmp_path / "dest"
    target = dest / "a" / "file1.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"verified")
    original_hash = model_install_module._sha256_of_open_file  # pyright: ignore[reportPrivateUsage]

    def replace_after_hash(fd: int, path: Path | str) -> tuple[str, int, int]:
        result = original_hash(fd, path)
        target.unlink()
        target.write_bytes(b"replacement")
        return result

    monkeypatch.setattr(model_install_module, "_sha256_of_open_file", replace_after_hash)

    with pytest.raises(ModelInstallError, match="changed while being verified"):
        install_model(
            dest,
            verify_only=True,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
        )

    assert target.read_bytes() == b"replacement"


def test_cached_parent_renamed_after_hash_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached bytes in a detached parent cannot satisfy the live destination path."""
    manifest = _make_manifest({"a/file1.bin": b"verified"})
    dest = tmp_path / "dest"
    parent = dest / "a"
    target = parent / "file1.bin"
    parent.mkdir(parents=True)
    target.write_bytes(b"verified")
    parked_parent = tmp_path / "parked-parent"
    original_hash = model_install_module._sha256_of_open_file  # pyright: ignore[reportPrivateUsage]

    def rename_parent_after_hash(fd: int, path: Path | str) -> tuple[str, int, int]:
        result = original_hash(fd, path)
        parent.rename(parked_parent)
        parent.mkdir(mode=0o700)
        os.link(parked_parent / "file1.bin", target)
        return result

    monkeypatch.setattr(model_install_module, "_sha256_of_open_file", rename_parent_after_hash)

    with pytest.raises(
        ModelInstallError, match="destination changed before placement could be confirmed"
    ):
        install_model(
            dest,
            verify_only=True,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
        )

    assert os.path.samefile(parked_parent / "file1.bin", target)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO support")
def test_cached_fifo_swap_after_lstat_is_rejected_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FIFO substituted before cached open is rejected without being consumed."""
    manifest = _make_manifest({"a/file1.bin": b"verified"})
    dest = tmp_path / "dest"
    target = dest / "a" / "file1.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"verified")
    original_open = model_install_module._open_cached_file  # pyright: ignore[reportPrivateUsage]

    def fail_if_read_attempted(*args: object, **kwargs: object) -> object:
        pytest.fail("a substituted FIFO must be rejected before any read")

    monkeypatch.setattr(os, "fdopen", fail_if_read_attempted)

    def replace_with_fifo(path: Path | str, *, dir_fd: int | None = None) -> int:
        target.unlink()
        os.mkfifo(target)
        return original_open(path, dir_fd=dir_fd)

    monkeypatch.setattr(model_install_module, "_open_cached_file", replace_with_fifo)

    with pytest.raises(ModelInstallError, match="not a regular file"):
        install_model(
            dest,
            verify_only=True,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
        )

    assert stat.S_ISFIFO(target.stat().st_mode)


def test_group_writable_destination_parent_is_rejected_before_placement(tmp_path: Path) -> None:
    """Named temporary placement requires an owner-only parent directory."""
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    dest = tmp_path / "dest"
    parent = dest / "a"
    parent.mkdir(parents=True)
    parent.chmod(0o777)
    calls = [0]
    client = httpx.Client(
        transport=_serving_transport({"a/file1.bin": b"payload"}, call_count=calls)
    )

    with pytest.raises(ModelInstallError, match="owner-only placement boundary"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert calls == [0]
    assert not (parent / "file1.bin").exists()


def test_group_writable_destination_parent_is_rejected_before_cached_acceptance(
    tmp_path: Path,
) -> None:
    """Cached verification requires the same owner-only directory boundary."""
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    dest = tmp_path / "dest"
    parent = dest / "a"
    parent.mkdir(parents=True)
    target = parent / "file1.bin"
    target.write_bytes(b"payload")
    parent.chmod(0o777)

    with pytest.raises(ModelInstallError, match="owner-only placement boundary"):
        install_model(
            dest,
            verify_only=True,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
        )

    assert target.read_bytes() == b"payload"


def test_group_writable_cached_file_is_rejected_from_its_open_descriptor(tmp_path: Path) -> None:
    """Cached acceptance rejects a regular file writable by another principal."""
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    dest = tmp_path / "dest"
    target = dest / "a" / "file1.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"payload")
    same_inode_alias = tmp_path / "same-inode-alias"
    os.link(target, same_inode_alias)
    target.chmod(0o664)

    with pytest.raises(ModelInstallError, match="owner-only regular file"):
        install_model(
            dest,
            verify_only=True,
            manifest_files=manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
        )

    assert os.path.samefile(target, same_inode_alias)


def test_cached_file_with_untrusted_effective_owner_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cached ownership policy is bound to fstat, not the target pathname."""
    target = tmp_path / "cached.bin"
    target.write_bytes(b"payload")
    untrusted_uid = target.stat().st_uid + 1
    monkeypatch.setattr(os, "geteuid", lambda: untrusted_uid)

    with pytest.raises(ModelInstallError, match="owner-only regular file"):
        model_install_module._sha256_of_file(target)  # pyright: ignore[reportPrivateUsage]


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


def test_bounded_hash_failure_is_not_masked_by_close_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded-hash failure remains authoritative when descriptor cleanup fails."""
    monkeypatch.setattr(model_install_module, "_MAX_MODEL_FILE_BYTES", 8)
    target = tmp_path / "oversized.bin"
    target.write_bytes(b"x" * 9)
    close_calls: list[int] = []
    original_close = os.close

    def failing_close(fd: int) -> None:
        close_calls.append(fd)
        raise OSError("simulated close failure")

    monkeypatch.setattr(os, "close", failing_close)

    with pytest.raises(ModelInstallError, match="per-file cap"):
        model_install_module._sha256_of_file(target)  # pyright: ignore[reportPrivateUsage]

    assert len(close_calls) == 1
    original_close(close_calls[0])


def test_stream_cleanup_unlink_failure_preserves_byte_cap_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Temporary-file cleanup notes but cannot replace the streaming failure."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_unlink = os.unlink

    def failing_unlink(_path: object, **_kwargs: object) -> None:
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(os, "unlink", failing_unlink)
    try:
        with pytest.raises(ModelInstallError, match="per-file cap") as exc_info:
            model_install_module._stream_to_temp(  # pyright: ignore[reportPrivateUsage]
                parent_fd, "model.bin", [b"too-large"], byte_cap=1
            )
    finally:
        os.close(parent_fd)

    assert any(
        "removing temporary model file: OSError" in note for note in exc_info.value.__notes__
    )
    for temporary_file in tmp_path.glob("*.part"):
        original_unlink(temporary_file)


def test_placement_cleanup_unlink_failure_preserves_digest_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Digest failure remains primary when its temporary-file unlink fails."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_unlink = os.unlink

    def failing_unlink(_path: object, **_kwargs: object) -> None:
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(os, "unlink", failing_unlink)
    try:
        with pytest.raises(ModelInstallError, match="digest mismatch") as exc_info:
            model_install_module._place_verified(  # pyright: ignore[reportPrivateUsage]
                parent_fd,
                "model.bin",
                [b"wrong"],
                expected_sha256=hashlib.sha256(b"expected").hexdigest(),
                byte_cap=8,
                context="model.bin",
            )
    finally:
        os.close(parent_fd)

    assert any(
        "removing temporary model file: OSError" in note for note in exc_info.value.__notes__
    )
    for temporary_file in tmp_path.glob("*.part"):
        original_unlink(temporary_file)


def test_placement_fsyncs_containing_directory_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful atomic placement makes its new directory entry durable."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_fsync = os.fsync
    fsynced_directory_fds: list[int] = []

    def record_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            fsynced_directory_fds.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", record_fsync)
    try:
        model_install_module._place_verified(  # pyright: ignore[reportPrivateUsage]
            parent_fd,
            "model.bin",
            [b"payload"],
            expected_sha256=hashlib.sha256(b"payload").hexdigest(),
            byte_cap=8,
            context="model.bin",
        )
    finally:
        os.close(parent_fd)

    assert fsynced_directory_fds == [parent_fd]
    assert (tmp_path / "model.bin").read_bytes() == b"payload"


def test_mode_fsync_failure_prevents_placement_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-fchmod file sync is required before the rename can occur."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_fsync = os.fsync
    file_syncs = [0]

    def fail_second_file_sync(fd: int) -> None:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            file_syncs[0] += 1
            if file_syncs[0] == 2:
                raise OSError("simulated mode fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_second_file_sync)
    try:
        with pytest.raises(OSError, match="simulated mode fsync failure"):
            model_install_module._place_verified(  # pyright: ignore[reportPrivateUsage]
                parent_fd,
                "model.bin",
                [b"payload"],
                expected_sha256=hashlib.sha256(b"payload").hexdigest(),
                byte_cap=8,
                context="model.bin",
            )
    finally:
        os.close(parent_fd)

    assert not (tmp_path / "model.bin").exists()
    assert list(tmp_path.glob("*.part")) == []


def test_directory_fsync_failure_after_replace_propagates_without_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-rename directory-sync failure cannot be reported as success."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_fsync = os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    try:
        with pytest.raises(OSError, match="simulated directory fsync failure") as exc_info:
            model_install_module._place_verified(  # pyright: ignore[reportPrivateUsage]
                parent_fd,
                "model.bin",
                [b"payload"],
                expected_sha256=hashlib.sha256(b"payload").hexdigest(),
                byte_cap=8,
                context="model.bin",
            )
    finally:
        os.close(parent_fd)

    assert list(tmp_path.glob("*.part")) == []
    assert not any(
        "removing temporary model file" in note for note in getattr(exc_info.value, "__notes__", [])
    )


def test_post_validation_non_regular_target_is_rejected_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target swapped after early validation cannot be overwritten."""
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_stream = model_install_module._stream_to_temp  # pyright: ignore[reportPrivateUsage]

    def stream_then_swap(
        fd: int, target_name: str, chunks: Iterable[bytes], *, byte_cap: int
    ) -> tuple[str, int, str]:
        result = original_stream(fd, target_name, chunks, byte_cap=byte_cap)
        (tmp_path / target_name).symlink_to(tmp_path / "outside")
        return result

    monkeypatch.setattr(model_install_module, "_stream_to_temp", stream_then_swap)
    try:
        with pytest.raises(ModelInstallError, match="non-regular target"):
            model_install_module._place_verified(  # pyright: ignore[reportPrivateUsage]
                parent_fd,
                "model.bin",
                [b"payload"],
                expected_sha256=hashlib.sha256(b"payload").hexdigest(),
                byte_cap=8,
                context="model.bin",
            )
    finally:
        os.close(parent_fd)


def test_created_destination_directory_is_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory creation is synced before descriptor traversal continues."""
    dest = tmp_path / "dest"
    dest.mkdir()
    dest_root_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    original_fsync = os.fsync
    fsynced_directory_fds: list[int] = []

    def record_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            fsynced_directory_fds.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", record_fsync)
    try:
        parent_fd, _target_name = model_install_module._open_destination_parent(  # pyright: ignore[reportPrivateUsage]
            dest_root_fd, "a/model.bin", create=True
        )
        try:
            assert (dest / "a").is_dir()
        finally:
            os.close(parent_fd)
    finally:
        os.close(dest_root_fd)

    assert fsynced_directory_fds


def test_destination_parent_rejects_same_type_replacement_after_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A created intermediate directory must retain its reopened identity."""
    dest = tmp_path / "dest"
    dest.mkdir()
    parked = tmp_path / "parked-a"
    root_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    original_open = os.open
    opens_of_a = 0

    def replace_before_reopen(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        nonlocal opens_of_a
        if path == "a":
            opens_of_a += 1
            if opens_of_a == 1:
                (dest / "a").rename(parked)
                (dest / "a").mkdir(mode=0o700)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_before_reopen)
    try:
        with pytest.raises(ModelInstallError, match="changed while being created"):
            model_install_module._open_destination_parent(  # pyright: ignore[reportPrivateUsage]
                root_fd, "a/model.bin", create=True
            )
    finally:
        os.close(root_fd)


def test_destination_parent_rejects_writable_raced_intermediate(tmp_path: Path) -> None:
    """A reopened intermediate directory must satisfy the owner-only boundary."""
    dest = tmp_path / "dest"
    intermediate = dest / "a"
    intermediate.mkdir(parents=True)
    intermediate.chmod(0o777)
    root_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ModelInstallError, match="owner-only placement boundary"):
            model_install_module._open_destination_parent(  # pyright: ignore[reportPrivateUsage]
                root_fd, "a/model.bin", create=True
            )
    finally:
        os.close(root_fd)


def test_destination_parent_file_exists_race_reopens_and_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FileExists race is reopened and validated before traversal continues."""
    dest = tmp_path / "dest"
    dest.mkdir()
    root_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    original_mkdir = os.mkdir

    def create_then_report_exists(
        path: Path | str, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> None:
        original_mkdir(path, mode, dir_fd=dir_fd)
        if path == "a":
            raise FileExistsError

    monkeypatch.setattr(os, "mkdir", create_then_report_exists)
    try:
        parent_fd, target_name = model_install_module._open_destination_parent(  # pyright: ignore[reportPrivateUsage]
            root_fd, "a/model.bin", create=True
        )
        try:
            assert target_name == "model.bin"
        finally:
            os.close(parent_fd)
    finally:
        os.close(root_fd)


def test_destination_parent_old_close_failure_closes_child_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An indeterminate parent close does not retry it and releases the child."""
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    root_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    original_open = os.open
    original_close = os.close
    child_fds: list[int] = []
    parent_fds: list[int] = []
    close_calls: list[int] = []
    close_start: list[int] = []

    def capture_open(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "a":
            close_start.append(len(close_calls))
            child_fds.append(fd)
            assert dir_fd is not None
            parent_fds.append(dir_fd)
        return fd

    def fail_parent_close(fd: int) -> None:
        close_calls.append(fd)
        if parent_fds and fd == parent_fds[0]:
            raise OSError("simulated destination parent close failure")
        original_close(fd)

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(os, "close", fail_parent_close)
    try:
        with pytest.raises(OSError, match="simulated destination parent close failure"):
            model_install_module._open_destination_parent(  # pyright: ignore[reportPrivateUsage]
                root_fd, "a/model.bin", create=False
            )
    finally:
        original_close(root_fd)

    final_close_calls = close_calls[close_start[0] :]
    assert final_close_calls.count(child_fds[0]) == 1
    assert final_close_calls.count(parent_fds[0]) == 1


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


def test_source_descriptor_is_closed_if_final_parent_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final source-parent close failure cannot leak the opened source file."""
    from_dir = tmp_path / "src"
    source = from_dir / "a" / "file1.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"payload")
    original_open = os.open
    original_close = os.close
    opened_child_fds: list[int] = []
    opened_source_fds: list[int] = []
    source_close_calls: list[int] = []

    def tracking_open(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "a":
            opened_child_fds.append(fd)
        elif path == "file1.bin":
            opened_source_fds.append(fd)
        return fd

    def fail_final_parent_close(fd: int) -> None:
        if opened_source_fds and fd == opened_source_fds[-1]:
            source_close_calls.append(fd)
        if opened_child_fds and fd == opened_child_fds[-1]:
            raise OSError("simulated final parent close failure")
        original_close(fd)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", fail_final_parent_close)
    try:
        with pytest.raises(OSError, match="simulated final parent close failure"):
            model_install_module._open_verified_source_file(  # pyright: ignore[reportPrivateUsage]
                from_dir, "a/file1.bin"
            )
    finally:
        for fd in opened_child_fds:
            original_close(fd)

    assert opened_source_fds
    assert source_close_calls == [opened_source_fds[0]]


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


def test_from_dir_parent_swap_after_validation_cannot_read_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source parent replaced after realpath validation is not followed."""
    from_dir = tmp_path / "src"
    source_parent = from_dir / "a"
    source_parent.mkdir(parents=True)
    (source_parent / "file1.bin").write_bytes(b"payload")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file1.bin").write_bytes(b"outside-bytes")
    parked_parent = tmp_path / "parked-source-parent"
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    dest = tmp_path / "dest"
    original_resolve = model_install_module._resolve_and_validate_source  # pyright: ignore[reportPrivateUsage]

    def swap_after_validation(root: Path, relative_path: str) -> Path:
        result = original_resolve(root, relative_path)
        source_parent.rename(parked_parent)
        source_parent.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(model_install_module, "_resolve_and_validate_source", swap_after_validation)

    with pytest.raises(ModelInstallError, match="cannot safely open --from-dir directory"):
        install_model(
            dest, from_dir=from_dir, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
        )

    assert (outside / "file1.bin").read_bytes() == b"outside-bytes"
    assert not (dest / "a" / "file1.bin").exists()


def test_from_dir_final_symlink_swap_after_validation_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final source symlink substituted after validation is refused by O_NOFOLLOW."""
    from_dir = tmp_path / "src"
    source = from_dir / "a" / "file1.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"payload")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside-bytes")
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    dest = tmp_path / "dest"
    original_resolve = model_install_module._resolve_and_validate_source  # pyright: ignore[reportPrivateUsage]

    def swap_after_validation(root: Path, relative_path: str) -> Path:
        result = original_resolve(root, relative_path)
        source.unlink()
        source.symlink_to(outside)
        return result

    monkeypatch.setattr(model_install_module, "_resolve_and_validate_source", swap_after_validation)

    with pytest.raises(ModelInstallError, match="not a regular file"):
        install_model(
            dest, from_dir=from_dir, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
        )

    assert outside.read_bytes() == b"outside-bytes"
    assert not (dest / "a" / "file1.bin").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX-only")
def test_from_dir_final_fifo_swap_after_validation_is_not_read_or_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A substituted FIFO is opened non-blocking then rejected before any read."""
    from_dir = tmp_path / "src"
    source = from_dir / "a" / "file1.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"payload")
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    dest = tmp_path / "dest"
    original_resolve = model_install_module._resolve_and_validate_source  # pyright: ignore[reportPrivateUsage]

    def replace_with_fifo(root: Path, relative_path: str) -> Path:
        result = original_resolve(root, relative_path)
        source.unlink()
        os.mkfifo(source)
        return result

    monkeypatch.setattr(model_install_module, "_resolve_and_validate_source", replace_with_fifo)

    with pytest.raises(ModelInstallError, match="not a regular file"):
        install_model(
            dest, from_dir=from_dir, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION
        )

    assert source.is_fifo()
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


def test_cached_continue_propagates_parent_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-file descriptor cleanup still fails loudly after a cached continue."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "file1.bin").write_bytes(b"hello")
    captured_parent_fds: list[int] = []
    original_open_parent = model_install_module._open_destination_parent  # pyright: ignore[reportPrivateUsage]
    original_close = os.close

    def capture_parent(dest_root_fd: int, relative_path: str, *, create: bool) -> tuple[int, str]:
        result = original_open_parent(dest_root_fd, relative_path, create=create)
        captured_parent_fds.append(result[0])
        return result

    def failing_parent_close(fd: int) -> None:
        if captured_parent_fds and fd == captured_parent_fds[0]:
            raise OSError("simulated parent close failure")
        original_close(fd)

    monkeypatch.setattr(
        model_install_module,
        "_open_destination_parent",
        capture_parent,
    )
    monkeypatch.setattr(os, "close", failing_parent_close)
    try:
        with pytest.raises(OSError, match="simulated parent close failure"):
            install_model(dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION)
    finally:
        if captured_parent_fds:
            original_close(captured_parent_fds[0])


def test_primary_error_survives_parent_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-file parent cleanup annotates but cannot replace a digest failure."""
    manifest = _make_manifest({"a/file1.bin": b"expected"})
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    captured_parent_fds: list[int] = []
    original_open_parent = model_install_module._open_destination_parent  # pyright: ignore[reportPrivateUsage]
    original_close = os.close

    def capture_parent(dest_root_fd: int, relative_path: str, *, create: bool) -> tuple[int, str]:
        result = original_open_parent(dest_root_fd, relative_path, create=create)
        captured_parent_fds.append(result[0])
        return result

    def failing_parent_close(fd: int) -> None:
        if captured_parent_fds and fd == captured_parent_fds[0]:
            raise OSError("simulated parent close failure")
        original_close(fd)

    monkeypatch.setattr(model_install_module, "_open_destination_parent", capture_parent)
    monkeypatch.setattr(os, "close", failing_parent_close)
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"wrong")))
    try:
        with pytest.raises(ModelInstallError, match="digest mismatch") as exc_info:
            install_model(
                dest,
                manifest_files=manifest,
                repo_id=_REPO_ID,
                revision=_REVISION,
                http_client=client,
            )
    finally:
        if captured_parent_fds:
            original_close(captured_parent_fds[0])

    assert any(
        "closing destination directory descriptor: OSError" in note
        for note in exc_info.value.__notes__
    )


def test_owned_client_close_failure_propagates_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owned-client cleanup remains observable when installation otherwise succeeds."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "file1.bin").write_bytes(b"hello")

    def failing_client_close(_client: httpx.Client) -> None:
        raise OSError("simulated client close failure")

    monkeypatch.setattr(httpx.Client, "close", failing_client_close)

    with pytest.raises(OSError, match="simulated client close failure"):
        install_model(dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION)


def test_owned_client_constructor_failure_closes_destination_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root setup is cleaned up when internally-owned client construction fails."""
    dest = tmp_path / "dest"
    dest.mkdir()
    root_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    original_close = os.close
    close_calls: list[int] = []

    def fixed_root(_dest: Path, *, create: bool) -> tuple[Path, int]:
        assert create
        return dest, root_fd

    def failing_client(**_kwargs: object) -> httpx.Client:
        raise OSError("simulated client construction failure")

    def capture_close(fd: int) -> None:
        close_calls.append(fd)
        original_close(fd)

    monkeypatch.setattr(model_install_module, "_open_destination_root", fixed_root)
    monkeypatch.setattr(httpx, "Client", failing_client)
    monkeypatch.setattr(os, "close", capture_close)

    with pytest.raises(OSError, match="simulated client construction failure"):
        install_model(dest, manifest_files=_make_manifest({"a/file1.bin": b"payload"}))

    assert close_calls.count(root_fd) == 1


def test_primary_error_survives_owned_client_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owned-client cleanup annotates but cannot replace a manifest failure."""
    invalid_manifest = (ManifestFile(relative_path="a/file1.bin", sha256="invalid"),)

    def failing_client_close(_client: httpx.Client) -> None:
        raise OSError("simulated client close failure")

    monkeypatch.setattr(httpx.Client, "close", failing_client_close)

    with pytest.raises(ModelInstallError, match="manifest digest") as exc_info:
        install_model(
            tmp_path / "dest",
            manifest_files=invalid_manifest,
            repo_id=_REPO_ID,
            revision=_REVISION,
        )

    assert any(
        "closing model download client: OSError" in note for note in exc_info.value.__notes__
    )


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


def test_destination_root_creation_rejects_a_swapped_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink substituted after root mkdir is never opened or accepted."""
    dest = tmp_path / "dest"
    parked = tmp_path / "parked-dest"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_mkdir = os.mkdir

    def create_then_swap(path: Path | str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        original_mkdir(path, mode, dir_fd=dir_fd)
        if path == dest.name:
            dest.rename(parked)
            dest.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(os, "mkdir", create_then_swap)

    with pytest.raises(ModelInstallError, match="cannot safely open the model destination root"):
        model_install_module._open_destination_root(  # pyright: ignore[reportPrivateUsage]
            dest, create=True
        )

    assert parked.is_dir()
    assert not (outside / "a").exists()


def test_destination_root_creation_rejects_a_replaced_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-type untrusted root replacement cannot cross the trust boundary."""
    dest = tmp_path / "dest"
    parked = tmp_path / "parked-dest"
    original_mkdir = os.mkdir

    def create_then_replace(
        path: Path | str, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> None:
        original_mkdir(path, mode, dir_fd=dir_fd)
        if path == dest.name:
            dest.rename(parked)
            dest.mkdir(mode=0o777)
            dest.chmod(0o777)

    monkeypatch.setattr(os, "mkdir", create_then_replace)

    with pytest.raises(ModelInstallError, match="owner-only directory boundary"):
        model_install_module._open_destination_root(  # pyright: ignore[reportPrivateUsage]
            dest, create=True
        )

    assert parked.is_dir()
    assert dest.is_dir()


def test_preexisting_destination_root_symlink_is_rejected(tmp_path: Path) -> None:
    """An existing destination-root symlink cannot be blessed by resolution."""
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    dest.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ModelInstallError, match="cannot safely open the model destination root"):
        model_install_module._open_destination_root(  # pyright: ignore[reportPrivateUsage]
            dest, create=False
        )


def test_destination_root_symlink_ancestor_is_rejected(tmp_path: Path) -> None:
    """No ancestor symlink is followed while opening a destination root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    ancestor = tmp_path / "ancestor"
    ancestor.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ModelInstallError, match="cannot safely open the model destination root"):
        model_install_module._open_destination_root(  # pyright: ignore[reportPrivateUsage]
            ancestor / "dest", create=True
        )


def test_destination_root_create_race_reopens_an_existing_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FileExistsError race reopens and validates the entry rather than trusting it."""
    dest = tmp_path / "dest"
    original_mkdir = os.mkdir

    def create_then_report_exists(
        path: Path | str, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> None:
        original_mkdir(path, mode, dir_fd=dir_fd)
        if path == dest.name:
            raise FileExistsError

    monkeypatch.setattr(os, "mkdir", create_then_report_exists)
    _path, root_fd = model_install_module._open_destination_root(  # pyright: ignore[reportPrivateUsage]
        dest, create=True
    )
    os.close(root_fd)
    assert dest.is_dir()


def test_destination_root_create_race_disappearance_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mkdir-to-stat disappearance is a fail-closed installer error."""
    dest = tmp_path / "dest"
    original_stat = os.stat

    def remove_before_stat(path: Path | str, *args: object, **kwargs: object) -> os.stat_result:
        if path == dest.name and dest.exists():
            dest.rmdir()
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", remove_before_stat)

    with pytest.raises(ModelInstallError, match="changed while being created"):
        model_install_module._open_destination_root(  # pyright: ignore[reportPrivateUsage]
            dest, create=True
        )


@pytest.mark.parametrize(
    ("owner", "expect_error"),
    [(0, False), (os.geteuid() + 1, True)],
)
def test_sticky_destination_ancestor_requires_root_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: int, expect_error: bool
) -> None:
    """Only a root-owned sticky shared ancestor is accepted."""
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_fstat = os.fstat
    actual = original_fstat(fd)
    synthetic = list(actual)
    synthetic[stat.ST_MODE] = stat.S_IFDIR | 0o1777
    synthetic[stat.ST_UID] = owner

    def sticky_stat(candidate_fd: int) -> os.stat_result:
        if candidate_fd == fd:
            return os.stat_result(synthetic)
        return original_fstat(candidate_fd)

    monkeypatch.setattr(os, "fstat", sticky_stat)
    try:
        if expect_error:
            with pytest.raises(ModelInstallError, match="owner-only directory boundary"):
                model_install_module._validate_destination_root_trust(  # pyright: ignore[reportPrivateUsage]
                    fd, allow_sticky=True
                )
        else:
            model_install_module._validate_destination_root_trust(  # pyright: ignore[reportPrivateUsage]
                fd, allow_sticky=True
            )
    finally:
        original_fstat(fd)
        os.close(fd)


@pytest.mark.parametrize("failure", ["identity", "trust"])
def test_destination_root_rejection_closes_old_parent_and_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Root-walker rejection releases both descriptors exactly once."""
    dest = tmp_path / "dest"
    original_open = os.open
    original_close = os.close
    original_fstat = os.fstat
    opened_child_fds: list[int] = []
    opened_parent_fds: list[int] = []
    close_calls: list[int] = []
    close_start: list[int] = []

    def capture_open(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == dest.name:
            close_start.append(len(close_calls))
            opened_child_fds.append(fd)
            if dir_fd is not None:
                opened_parent_fds.append(dir_fd)
        return fd

    def capture_close(fd: int) -> None:
        close_calls.append(fd)
        original_close(fd)

    def fail_identity(fd: int) -> os.stat_result:
        current = original_fstat(fd)
        if failure == "identity" and opened_child_fds and fd == opened_child_fds[0]:
            synthetic = list(current)
            synthetic[stat.ST_INO] += 1
            return os.stat_result(synthetic)
        return current

    def fail_trust(_fd: int, *, allow_sticky: bool) -> None:
        if failure == "trust" and opened_child_fds and _fd == opened_child_fds[0]:
            raise ModelInstallError("simulated trust rejection")

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(os, "close", capture_close)
    if failure == "identity":
        monkeypatch.setattr(os, "fstat", fail_identity)
    else:
        monkeypatch.setattr(model_install_module, "_validate_destination_root_trust", fail_trust)

    with pytest.raises(ModelInstallError):
        model_install_module._open_destination_root(  # pyright: ignore[reportPrivateUsage]
            dest, create=True
        )

    assert opened_child_fds and opened_parent_fds
    final_close_calls = close_calls[close_start[0] :]
    assert final_close_calls.count(opened_child_fds[0]) == 1
    assert final_close_calls.count(opened_parent_fds[0]) == 1


def test_destination_root_old_parent_close_failure_does_not_retry_or_leak_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An indeterminate old-parent close is attempted once and closes the child."""
    dest = tmp_path / "dest"
    original_open = os.open
    original_close = os.close
    child_fds: list[int] = []
    parent_fds: list[int] = []
    close_calls: list[int] = []
    close_start: list[int] = []

    def capture_open(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == dest.name:
            close_start.append(len(close_calls))
            child_fds.append(fd)
            assert dir_fd is not None
            parent_fds.append(dir_fd)
        return fd

    def fail_old_parent_close(fd: int) -> None:
        close_calls.append(fd)
        if parent_fds and fd == parent_fds[0]:
            raise OSError("simulated old parent close failure")
        original_close(fd)

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(os, "close", fail_old_parent_close)

    with pytest.raises(OSError, match="simulated old parent close failure"):
        model_install_module._open_destination_root(  # pyright: ignore[reportPrivateUsage]
            dest, create=True
        )

    assert child_fds and parent_fds
    final_close_calls = close_calls[close_start[0] :]
    assert final_close_calls.count(child_fds[0]) == 1
    assert final_close_calls.count(parent_fds[0]) == 1


def test_swapped_destination_parent_cannot_redirect_placement_outside_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent swapped for a symlink after descriptor open cannot redirect writes."""
    dest = tmp_path / "dest"
    parent = dest / "a"
    parent.mkdir(parents=True)
    parked_parent = tmp_path / "parked-parent"
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    original_stream_to_temp = model_install_module._stream_to_temp  # pyright: ignore[reportPrivateUsage]
    original_place_verified = model_install_module._place_verified  # pyright: ignore[reportPrivateUsage]

    def swap_parent_then_stream(
        parent_fd: int, target_name: str, chunks: Iterable[bytes], *, byte_cap: int
    ) -> tuple[str, int, str]:
        parent.rename(parked_parent)
        parent.mkdir(mode=0o700)
        return original_stream_to_temp(parent_fd, target_name, chunks, byte_cap=byte_cap)

    def link_placed_file_into_replacement(
        parent_fd: int,
        target_name: str,
        chunks: Iterable[bytes],
        *,
        expected_sha256: str,
        byte_cap: int,
        context: str,
    ) -> tuple[int, int]:
        result = original_place_verified(
            parent_fd,
            target_name,
            chunks,
            expected_sha256=expected_sha256,
            byte_cap=byte_cap,
            context=context,
        )
        os.link(parked_parent / target_name, parent / target_name)
        return result

    monkeypatch.setattr(model_install_module, "_stream_to_temp", swap_parent_then_stream)
    monkeypatch.setattr(model_install_module, "_place_verified", link_placed_file_into_replacement)
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"payload")))

    with pytest.raises(
        ModelInstallError, match="destination changed before placement could be confirmed"
    ):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert os.path.samefile(parked_parent / "file1.bin", parent / "file1.bin")


def test_live_destination_revalidation_rejects_replaced_target(tmp_path: Path) -> None:
    """A live target must retain the verified device and inode before success."""
    dest = tmp_path / "dest"
    parent = dest / "a"
    target = parent / "file1.bin"
    parent.mkdir(parents=True)
    target.write_bytes(b"payload")
    target_fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    dest_root_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        verified = os.fstat(target_fd)
        target.unlink()
        target.write_bytes(b"payload")
        with pytest.raises(
            ModelInstallError, match="destination changed before placement could be confirmed"
        ):
            model_install_module._revalidate_live_destination(  # pyright: ignore[reportPrivateUsage]
                dest,
                "a/file1.bin",
                dest_root_fd=dest_root_fd,
                parent_fd=parent_fd,
                device=verified.st_dev,
                inode=verified.st_ino,
            )
    finally:
        try:
            os.close(parent_fd)
        finally:
            try:
                os.close(target_fd)
            finally:
                os.close(dest_root_fd)


def test_live_destination_revalidation_rejects_replaced_root(tmp_path: Path) -> None:
    """A detached destination root cannot be reported as the requested path."""
    dest = tmp_path / "dest"
    parent = dest / "a"
    target = parent / "file1.bin"
    parent.mkdir(parents=True)
    target.write_bytes(b"payload")
    dest_root_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    verified = target.stat()
    parked = tmp_path / "parked-dest"
    dest.rename(parked)
    dest.mkdir(mode=0o700)
    try:
        with pytest.raises(
            ModelInstallError, match="destination changed before placement could be confirmed"
        ):
            model_install_module._revalidate_live_destination(  # pyright: ignore[reportPrivateUsage]
                dest,
                "a/file1.bin",
                dest_root_fd=dest_root_fd,
                parent_fd=parent_fd,
                device=verified.st_dev,
                inode=verified.st_ino,
            )
    finally:
        try:
            os.close(parent_fd)
        finally:
            os.close(dest_root_fd)


def test_replaced_temporary_entry_before_rename_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Placement never renames a name that no longer names the verified inode."""
    manifest = _make_manifest({"a/file1.bin": b"payload"})
    dest = tmp_path / "dest"
    original_stream_to_temp = model_install_module._stream_to_temp  # pyright: ignore[reportPrivateUsage]

    def replace_temp_entry(
        parent_fd: int, target_name: str, chunks: Iterable[bytes], *, byte_cap: int
    ) -> tuple[str, int, str]:
        tmp_name, tmp_fd, digest_hex = original_stream_to_temp(
            parent_fd, target_name, chunks, byte_cap=byte_cap
        )
        os.unlink(tmp_name, dir_fd=parent_fd)
        replacement_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(replacement_fd, b"replacement")
        finally:
            os.close(replacement_fd)
        return tmp_name, tmp_fd, digest_hex

    monkeypatch.setattr(model_install_module, "_stream_to_temp", replace_temp_entry)
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"payload")))

    with pytest.raises(ModelInstallError, match="temporary file changed before placement"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


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


def test_whole_fetch_deadline_aborts_body_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixed monotonic deadline bounds a trickle body beyond HTTPX's read timeout."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    clock_values = iter((0.0, 0.0, model_install_module._FETCH_DEADLINE_SECONDS))  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(time, "monotonic", lambda: next(clock_values))
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"hello")))
    dest = tmp_path / "dest"

    with pytest.raises(ModelInstallError, match="overall model fetch deadline"):
        install_model(
            dest, manifest_files=manifest, repo_id=_REPO_ID, revision=_REVISION, http_client=client
        )

    assert not (dest / "a" / "file1.bin").exists()
    assert list(dest.rglob("*.part")) == []


def test_whole_fetch_deadline_applies_between_redirect_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect time consumes the same total budget and prevents the next hop."""
    calls = [0]
    clock_values = iter((0.0, 0.0, model_install_module._FETCH_DEADLINE_SECONDS))  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(time, "monotonic", lambda: next(clock_values))

    def redirect(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(302, headers={"location": "https://hf.co/model"})

    client = httpx.Client(transport=httpx.MockTransport(redirect))
    with pytest.raises(ModelInstallError, match="overall model fetch deadline"):
        list(
            model_install_module._iter_https_bytes(  # pyright: ignore[reportPrivateUsage]
                client, "https://huggingface.co/acme/model", byte_cap=10
            )
        )

    assert calls == [1]


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


def test_temp_close_failure_is_not_retried_and_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deferred-writeback close error cannot close a reused descriptor."""
    manifest = _make_manifest({"a/file1.bin": b"hello"})
    dest = tmp_path / "dest"
    temp_fds: list[int] = []
    close_calls: list[int] = []
    original_fchmod = os.fchmod
    original_close = os.close

    def tracking_fchmod(fd: int, mode: int) -> None:
        temp_fds.append(fd)
        original_fchmod(fd, mode)

    def failing_temp_close(fd: int) -> None:
        if temp_fds and fd == temp_fds[0]:
            close_calls.append(fd)
            raise OSError("simulated deferred writeback failure")
        original_close(fd)

    monkeypatch.setattr(os, "fchmod", tracking_fchmod)
    monkeypatch.setattr(os, "close", failing_temp_close)
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: _ok_response(b"hello")))

    try:
        with pytest.raises(OSError, match="simulated deferred writeback failure"):
            install_model(
                dest,
                manifest_files=manifest,
                repo_id=_REPO_ID,
                revision=_REVISION,
                http_client=client,
            )
    finally:
        if temp_fds:
            original_close(temp_fds[0])

    assert len(temp_fds) == 1
    assert close_calls == temp_fds
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


@pytest.mark.parametrize("expected_bytes", [b"hello", b"mismatch"])
def test_is_already_valid_closes_cached_descriptor_after_digest_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected_bytes: bytes
) -> None:
    """Direct cached verification closes its retained descriptor on both outcomes."""
    target = tmp_path / "cached.bin"
    target.write_bytes(b"hello")
    original_close = os.close
    cached_fds: list[int] = []
    closed_cached_fds: list[int] = []
    original_open = model_install_module._open_cached_file  # pyright: ignore[reportPrivateUsage]

    def capture_cached_open(path: Path | str, *, dir_fd: int | None = None) -> int:
        fd = original_open(path, dir_fd=dir_fd)
        cached_fds.append(fd)
        return fd

    def capture_close(fd: int) -> None:
        if fd in cached_fds:
            closed_cached_fds.append(fd)
        original_close(fd)

    monkeypatch.setattr(model_install_module, "_open_cached_file", capture_cached_open)
    monkeypatch.setattr(os, "close", capture_close)

    is_valid = model_install_module._is_already_valid(  # pyright: ignore[reportPrivateUsage]
        target, hashlib.sha256(expected_bytes).hexdigest()
    )

    assert is_valid is (expected_bytes == b"hello")
    assert cached_fds
    assert closed_cached_fds == [cached_fds[0]]


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
