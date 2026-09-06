"""Secure placement/verification of the bundled/pinned first-crack model (AC5).

This module places the exact model files pinned in
:mod:`roastpilot_agent.appliance.model_manifest` onto local disk so a roast
never waits on a live Hugging Face pull (`roastpilot-agent/plan.md:30`), and
so the appliance can be prepared fully air-gapped via ``--from-dir``.

**External-input surface.** When neither a cached copy nor ``--from-dir``
satisfies a manifest file, this module fetches it anonymously over HTTPS from
``huggingface.co``. This is why the story requires ``security-reviewer``: the
fetched bytes originate outside this process. Digest verification alone does
not make an arbitrary redirect destination safe — a byte-for-byte match only
proves *what* was served, never *whether the connection should have been made
at all* (e.g. exfiltrating the signed query string on a redirect to an
attacker-controlled host, or a DNS/route-level MITM against an unpinned
internal address). Host trust and byte-integrity are therefore two
independent, both-mandatory controls. The mitigations below are the whole of
the trust model:

- The fetch URL is built **only** from the closed manifest fields (a
  40-hex-char revision, a validated relative path, and the fixed
  ``owner/name`` repo id) — never from any operator-supplied string, so there
  is no URL-injection surface.
- **Every hop — the initial request and every redirect — must land on a
  fixed, small Hugging-Face-operated hostname policy**: exactly
  ``huggingface.co``, exactly ``hf.co``, or a subdomain of ``hf.co`` (the
  CDN hosts HF's model-file redirects legitimately use). An IP-literal host,
  a non-default port, embedded userinfo, a URL fragment, or any other
  hostname is rejected *before* that hop is ever made — regardless of what
  bytes the far end would have served. A signed query string on an allowed
  CDN redirect is preserved (never stripped) so the redirect still resolves,
  but it is never included in any error message this module raises.
- Every byte is **also** verified against the manifest's committed SHA-256
  digest before it is ever visible at its final path (stream-to-temp, verify,
  ``os.replace``) — the second, independent control. Downloaded bytes are
  never executed, unpacked, or made executable.
- The request is anonymous: no ``Authorization`` header is ever sent and no
  token is ever read from the environment; ``Accept-Encoding: identity`` is
  sent and any response carrying a ``Content-Encoding`` header is rejected
  outright (a transparently-decompressed body could otherwise expand past
  every byte-cap check that inspects the wire bytes).
- A per-file byte cap (checked against a declared ``Content-Length`` before
  any body bytes are read, and re-checked against the actual streamed byte
  count as a backstop), connect/read timeouts, and a bounded redirect chain
  (the "total attempt bound") each fail closed and remove any partial file.
- The internally-owned production ``httpx.Client`` is constructed with
  ``trust_env=False`` so no ambient ``HTTP_PROXY``/``HTTPS_PROXY``/
  ``NO_PROXY`` or CA-bundle environment variable can redirect or intercept
  this traffic. A caller-supplied test/mock client is unaffected.

**MCP layout coupling.** The manifest's relative paths
(``onnx/int8/model_quantized.onnx`` / ``onnx/int8/preprocessor_config.json``)
are exactly the repository-relative paths ``coffee-roaster-mcp``'s
``first_crack.local_model_dir`` resolution joins onto that directory
unchanged. This repository's dev dependency group deliberately stays pinned
to ``coffee-roaster-mcp==0.1.13`` (never installed alongside the ``[pi]``
extra's ``0.2.0`` pin — `pyproject.toml:136-139`), so this was verified two
ways rather than by importing an installed ``0.2.0``: (1) the installed
``0.1.13`` package's ``artifacts._resolve_local_artifact`` /
``INT8_ONNX_MODEL_FILENAME`` / ``INT8_FEATURE_EXTRACTOR_FILENAME`` join
``local_model_dir`` onto exactly these relative paths, and (2) a byte-for-byte
comparison of the downloaded ``coffee_roaster_mcp-0.2.0`` wheel's
``artifacts.py`` against the installed ``0.1.13`` copy showed the module is
unchanged between the two releases. Placing files under those same relative
paths beneath the configured ``local_model_dir`` therefore already satisfies
the MCP server's expected root layout with no MCP-side change.
"""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from roastpilot_agent.appliance.model_manifest import (
    MANIFEST_FILES,
    REPO_ID,
    REVISION,
    ManifestFile,
)

#: Anonymous, fixed User-Agent, and ``identity`` encoding only. No credential,
#: token, or environment value is ever placed in a request header, and
#: ``Accept-Encoding: identity`` (plus the response-side check in
#: :func:`_iter_https_bytes`) closes the transparent-decompression risk: a
#: byte-cap check that only ever sees the wire bytes cannot be bypassed by a
#: compressed body that expands after the cap has already been satisfied.
_ANONYMOUS_HEADERS: Final[dict[str, str]] = {
    "user-agent": "roastpilot-agent-appliance-model-install/1",
    "accept-encoding": "identity",
}

#: Hosts permitted for the initial request and every redirect hop. Exactly
#: ``huggingface.co`` (the pinned origin) plus ``hf.co`` and its subdomains
#: (the CDN hosts Hugging Face's own model-file redirects legitimately land
#: on) — a small, explicit, Hugging-Face-operated policy, not "any host that
#: serves the right bytes" (see the module docstring's trust model).
_ALLOWED_REDIRECT_HOSTS_EXACT: Final[frozenset[str]] = frozenset({"huggingface.co", "hf.co"})
_ALLOWED_REDIRECT_HOST_SUFFIX: Final[str] = ".hf.co"

#: At most this many redirect hops are followed for one file fetch (matches
#: the existing bean-sourcing fetch's bound, ``bean_sourcing.py``'s
#: ``_MAX_REDIRECTS``). This is also the "total attempt bound": one initial
#: request plus at most this many redirected requests, then abort.
_MAX_REDIRECTS: Final[int] = 5

#: Per-file byte cap. The pinned int8 model is ~89.9 MB
#: (`roastpilot-agent/plan.md:30`); this leaves comfortable headroom while
#: still bounding a misbehaving or hostile response.
_MAX_MODEL_FILE_BYTES: Final[int] = 150 * 1024 * 1024

_CONNECT_TIMEOUT_SECONDS: Final[float] = 10.0
_READ_TIMEOUT_SECONDS: Final[float] = 60.0
#: Whole-file monotonic bound, deliberately conservative for Pi downloads.
_FETCH_DEADLINE_SECONDS: Final[float] = 15 * 60.0

_CHUNK_SIZE: Final[int] = 1024 * 1024

_REVISION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_RELATIVE_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._/-]+$")
_REPO_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_RELATIVE_PATH_BYTES: Final[int] = 128


class ModelInstallError(RuntimeError):
    """Placement/verification could not complete safely.

    Never carries a URL query string, credential, or environment value —
    every message here is built only from manifest fields (repo id,
    revision, relative path) and local filesystem paths.
    """


def _cleanup_preserving_primary(cleanup: Callable[[], None], *, action: str) -> None:
    """Run cleanup without replacing an already-propagating exception.

    A cleanup failure after successful work remains observable and therefore
    propagates. During unwinding, the primary error stays authoritative and
    receives a concise Python 3.11 exception note about the failed cleanup.

    Args:
        cleanup: The close, unlink, or client-close operation to run.
        action: A safe, local description of the cleanup operation.

    Raises:
        OSError: The cleanup failure when no primary exception is active.
    """
    primary = sys.exception()
    try:
        cleanup()
    except BaseException as cleanup_error:
        if primary is None:
            raise
        primary.add_note(f"cleanup failed while {action}: {type(cleanup_error).__name__}")


def _validate_revision(revision: str) -> None:
    """Reject anything but a full 40-character lowercase-hex commit SHA."""
    if not _REVISION_PATTERN.fullmatch(revision):
        raise ModelInstallError("manifest revision must be exactly 40 lowercase hex characters")


def _validate_digest(digest: str, *, label: str) -> None:
    """Reject anything but a full 64-character lowercase-hex SHA-256 digest."""
    if not _DIGEST_PATTERN.fullmatch(digest):
        raise ModelInstallError(
            f"manifest digest for {label!r} must be exactly 64 lowercase hex characters"
        )


def _validate_repo_id(repo_id: str) -> None:
    """Reject anything but a plain ``owner/name`` Hugging Face repository id."""
    if not _REPO_ID_PATTERN.fullmatch(repo_id):
        raise ModelInstallError(
            "manifest repo_id must be an 'owner/name' Hugging Face repository id"
        )


def _validate_relative_path(path: str) -> None:
    """Reject any path that could escape the destination root.

    POSIX only, a closed character grammar, no leading ``/``, no empty/``.``/
    ``..`` segment, no backslash, no NUL, and a bounded byte length.
    """
    if not path:
        raise ModelInstallError("manifest relative path must not be empty")
    if "\\" in path or "\x00" in path:
        raise ModelInstallError(
            f"manifest relative path {path!r} must not contain a backslash or NUL byte"
        )
    if len(path.encode("utf-8")) > _MAX_RELATIVE_PATH_BYTES:
        raise ModelInstallError(
            f"manifest relative path {path!r} exceeds {_MAX_RELATIVE_PATH_BYTES} bytes"
        )
    if not _RELATIVE_PATH_PATTERN.fullmatch(path):
        raise ModelInstallError(f"manifest relative path {path!r} contains a disallowed character")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ModelInstallError(
            f"manifest relative path {path!r} has an empty, '.', or '..' segment"
        )


def _resolve_and_validate_target(dest_root_real: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` under ``dest_root_real``, fail closed on escape.

    Args:
        dest_root_real: The destination root, already ``os.path.realpath``-resolved.
        relative_path: A manifest-declared relative path (validated here).

    Returns:
        The final placement path (not required to exist yet).

    Raises:
        ModelInstallError: The path fails grammar validation, its resolved
            location escapes ``dest_root_real``, or an existing entry at that
            location is not a regular file (a symlink, directory, or device
            is never overwritten).
    """
    _validate_relative_path(relative_path)
    candidate = dest_root_real.joinpath(*relative_path.split("/"))
    resolved_parent = Path(os.path.realpath(candidate.parent))
    final_target = resolved_parent / candidate.name
    try:
        final_target.relative_to(dest_root_real)
    except ValueError as exc:
        raise ModelInstallError(
            f"resolved destination for {relative_path!r} escapes the destination root"
        ) from exc
    try:
        st = os.lstat(final_target)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(st.st_mode):
            raise ModelInstallError(
                f"refusing to overwrite a non-regular-file destination entry for {relative_path!r}"
            )
    return final_target


def _resolve_and_validate_source(from_dir_real: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` under ``from_dir_real``, fail closed on escape.

    Mirrors :func:`_resolve_and_validate_target`'s containment logic for the
    ``--from-dir`` source root — the destination boundary and the source
    boundary are validated exactly as strictly. The caller separately checks
    that the resolved path, once it exists, is a regular file (never a
    symlink, directory, or device); this function only resolves and checks
    containment, since a missing source is a distinct, expected error
    (:func:`install_model`'s "missing required file" message).

    Args:
        from_dir_real: The ``--from-dir`` root, already
            ``os.path.realpath``-resolved.
        relative_path: A manifest-declared relative path (already
            grammar-validated by the destination-side resolution earlier in
            the same per-file iteration).

    Returns:
        The resolved source path (not required to exist yet).

    Raises:
        ModelInstallError: The resolved location escapes ``from_dir_real``.
    """
    _validate_relative_path(relative_path)
    candidate = from_dir_real.joinpath(*relative_path.split("/"))
    resolved_parent = Path(os.path.realpath(candidate.parent))
    final_source = resolved_parent / candidate.name
    try:
        final_source.relative_to(from_dir_real)
    except ValueError as exc:
        raise ModelInstallError(
            f"--from-dir source for {relative_path!r} escapes the --from-dir root"
        ) from exc
    return final_source


def _open_cached_file(path: Path | str, *, dir_fd: int | None = None) -> int:
    """Open one cached candidate without following or blocking on unsafe entries."""
    # A target can be swapped from the preceding lstat into a FIFO before
    # this descriptor-relative open.  Non-blocking open ensures fstat below
    # rejects that non-regular entry without waiting for a writer.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        return os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise ModelInstallError(f"cannot safely verify cached destination {path}") from exc


def _sha256_of_open_file(fd: int, path: Path | str) -> tuple[str, int, int]:
    """Hash an already-open regular cached file without exceeding the cap."""
    digest = hashlib.sha256()
    initial = os.fstat(fd)
    if not stat.S_ISREG(initial.st_mode):
        raise ModelInstallError(f"cached destination {path} is not a regular file")
    if initial.st_uid != os.geteuid() or initial.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ModelInstallError(f"cached destination {path} is not an owner-only regular file")
    if stat.S_IMODE(initial.st_mode) != 0o644:
        raise ModelInstallError(f"cached destination {path} is not service-readable mode 0644")
    if initial.st_size > _MAX_MODEL_FILE_BYTES:
        raise ModelInstallError(
            f"cached destination {path} exceeds the {_MAX_MODEL_FILE_BYTES}-byte per-file cap"
        )
    total = 0
    with os.fdopen(fd, "rb", closefd=False) as fh:
        while True:
            chunk = fh.read(min(_CHUNK_SIZE, _MAX_MODEL_FILE_BYTES - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_MODEL_FILE_BYTES:
                raise ModelInstallError(
                    f"cached destination {path} exceeded the "
                    f"{_MAX_MODEL_FILE_BYTES}-byte per-file cap"
                )
            digest.update(chunk)
    final = os.fstat(fd)
    if final.st_size != total or final.st_size > _MAX_MODEL_FILE_BYTES:
        raise ModelInstallError(f"cached destination {path} changed while being verified")
    return digest.hexdigest(), final.st_dev, final.st_ino


def _sha256_of_file(  # pyright: ignore[reportUnusedFunction]
    path: Path | str, *, dir_fd: int | None = None
) -> tuple[str, int, int]:
    """Hash one regular file without reading more than the model-file cap.

    The descriptor is opened without following a final symlink.  Both its
    initial size and its final size must be within the cap and agree with the
    bytes read, so a file that grows while it is verified cannot turn cached
    verification into an unbounded read.

    Raises:
        ModelInstallError: The entry is unsafe or violates bounded cached
            verification.
        OSError: A local filesystem operation fails outside the fail-closed
            validation cases.
    """
    fd = _open_cached_file(path, dir_fd=dir_fd)
    try:
        result = _sha256_of_open_file(fd, path)
    except BaseException:
        _cleanup_preserving_primary(
            lambda: os.close(fd), action="closing cached model file descriptor"
        )
        raise
    else:
        os.close(fd)
    return result


def _open_verified_cached_file(
    target: Path | str, *, dir_fd: int | None = None
) -> tuple[str, int, int, int] | None:
    """Hash and identity-check a cached target while retaining its descriptor.

    The returned descriptor remains open so the caller can bind a subsequent
    live-path check to the exact bytes that were hashed. The caller owns and
    must close it on the successful return path.
    """
    try:
        st = os.lstat(target, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    fd = _open_cached_file(target, dir_fd=dir_fd)
    try:
        digest_hex, device, inode = _sha256_of_open_file(fd, target)
        try:
            current = os.stat(target, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ModelInstallError(
                f"cached destination {target} changed while being verified"
            ) from exc
        if not stat.S_ISREG(current.st_mode) or current.st_dev != device or current.st_ino != inode:
            raise ModelInstallError(f"cached destination {target} changed while being verified")
        return digest_hex, fd, device, inode
    except BaseException:
        _cleanup_preserving_primary(
            lambda: os.close(fd), action="closing cached model file descriptor"
        )
        raise


def _is_already_valid(  # pyright: ignore[reportUnusedFunction]
    target: Path | str, expected_sha256: str, *, dir_fd: int | None = None
) -> bool:
    """A regular file already exists at ``target`` with the expected digest."""
    verified = _open_verified_cached_file(target, dir_fd=dir_fd)
    if verified is None:
        return False
    digest_hex, fd, _device, _inode = verified
    is_valid = digest_hex == expected_sha256
    os.close(fd)
    return is_valid


def _validate_destination_parent_trust(parent_fd: int) -> None:
    """Require an owner-only directory boundary for temp-name placement.

    POSIX exposes descriptor-relative ``rename`` but not a portable
    rename-by-inode primitive. The directory containing a named temporary file
    is therefore a trust boundary: its effective owner must be this process,
    and group/other users cannot replace the verified ``.part`` name between
    identity validation and atomic replacement.
    """
    directory_stat = os.fstat(parent_fd)
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise ModelInstallError("model destination parent is not a directory")
    if directory_stat.st_uid != os.geteuid() or directory_stat.st_mode & (
        stat.S_IWGRP | stat.S_IWOTH
    ):
        raise ModelInstallError("model destination parent is not an owner-only placement boundary")


def _lock_destination_parent(parent_fd: int) -> None:
    """Acquire the per-directory installer lock without waiting on another install."""
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise ModelInstallError(
            "another model installation is active for this destination"
        ) from exc


def _reclaim_abandoned_parts(parent_fd: int, target_name: str) -> None:
    """Remove only safe abandoned temporary files for ``target_name``.

    The caller holds the non-blocking advisory lock on ``parent_fd`` for the
    whole target operation.  Each candidate is still verified through an open
    no-follow descriptor immediately before unlinking: a malformed or unsafe
    lookalike is a fail-closed installation error, never a deletion guess.
    """
    pattern = re.compile(rf"^\.{re.escape(target_name)}\.[0-9a-f]{{32}}\.part$")
    try:
        names = os.listdir(parent_fd)
    except OSError as exc:
        raise ModelInstallError("cannot safely inspect abandoned model temporary files") from exc
    for name in names:
        if not pattern.fullmatch(name):
            continue
        try:
            entry = os.lstat(name, dir_fd=parent_fd)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        except OSError as exc:
            raise ModelInstallError(
                "abandoned model temporary file changed during reclamation"
            ) from exc
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) not in (0o600, 0o644)
                or opened.st_nlink != 1
                or opened.st_size > _MAX_MODEL_FILE_BYTES
                or entry.st_dev != opened.st_dev
                or entry.st_ino != opened.st_ino
            ):
                raise ModelInstallError("refusing unsafe abandoned model temporary file")
            current = os.lstat(name, dir_fd=parent_fd)
            if current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
                raise ModelInstallError("abandoned model temporary file changed during reclamation")
        except BaseException:
            _cleanup_preserving_primary(
                lambda fd=fd: os.close(fd),
                action="closing abandoned temporary model file descriptor",
            )
            raise
        else:
            os.close(fd)
        try:
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as exc:
            raise ModelInstallError("cannot safely reclaim abandoned model temporary file") from exc


def _open_source_root(from_dir_real: Path, relative_path: str) -> int:
    """Open an absolute ``--from-dir`` root through no-follow descriptors."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_path = Path(os.path.abspath(from_dir_real))
    parent_fd = os.open("/", directory_flags)
    try:
        for component in root_path.parts[1:]:
            try:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                raise ModelInstallError(
                    f"--from-dir is missing required file {relative_path!r}"
                ) from None
            except OSError as exc:
                raise ModelInstallError("cannot safely open the --from-dir root") from exc
            try:
                os.close(parent_fd)
            except BaseException:
                _cleanup_preserving_primary(
                    lambda fd=child_fd: os.close(fd),
                    action="closing child --from-dir directory descriptor",
                )
                parent_fd = None
                raise
            parent_fd = child_fd
    except BaseException:
        if parent_fd is not None:
            _cleanup_preserving_primary(
                lambda: os.close(parent_fd), action="closing --from-dir directory descriptor"
            )
        raise
    assert parent_fd is not None
    return parent_fd


def _open_verified_source_file(from_dir_real: Path, relative_path: str) -> int:
    """Open one regular ``--from-dir`` source through no-follow descriptors.

    Every source path component is opened relative to its verified parent, and
    the final file uses ``O_NONBLOCK`` before its type is checked. A swapped
    symlink therefore cannot redirect the read, while a swapped FIFO or device
    cannot block or be consumed before it is rejected.

    Raises:
        ModelInstallError: The source is missing, unsafe, or not a regular file.
        OSError: A local filesystem operation fails outside the fail-closed
            validation cases.
    """
    _validate_relative_path(relative_path)
    components = relative_path.split("/")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    source_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    parent_fd = _open_source_root(from_dir_real, relative_path)
    source_fd: int | None = None
    try:
        for component in components[:-1]:
            try:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                raise ModelInstallError(
                    f"--from-dir is missing required file {relative_path!r}"
                ) from None
            except OSError as exc:
                raise ModelInstallError(
                    f"cannot safely open --from-dir directory for {relative_path!r}"
                ) from exc
            try:
                os.close(parent_fd)
            except BaseException:
                _cleanup_preserving_primary(
                    lambda fd=child_fd: os.close(fd),
                    action="closing child --from-dir directory descriptor",
                )
                parent_fd = None
                raise
            parent_fd = child_fd
        try:
            source_fd = os.open(components[-1], source_flags, dir_fd=parent_fd)
        except FileNotFoundError:
            raise ModelInstallError(
                f"--from-dir is missing required file {relative_path!r}"
            ) from None
        except OSError as exc:
            raise ModelInstallError(
                f"--from-dir source for {relative_path!r} is not a regular file "
                "(a symlink, directory, or device is never read)"
            ) from exc
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ModelInstallError(
                f"--from-dir source for {relative_path!r} is not a regular file "
                "(a symlink, directory, or device is never read)"
            )
    except BaseException:
        if source_fd is not None:
            _cleanup_preserving_primary(
                lambda fd=source_fd: os.close(fd), action="closing --from-dir source descriptor"
            )
        raise
    else:
        fd_to_close = parent_fd
        parent_fd = None
        try:
            os.close(fd_to_close)
        except BaseException:
            _cleanup_preserving_primary(
                lambda fd=source_fd: os.close(fd), action="closing --from-dir source descriptor"
            )
            source_fd = None
            raise
        verified_source_fd = source_fd
        source_fd = None
        return verified_source_fd
    finally:
        if parent_fd is not None:
            _cleanup_preserving_primary(
                lambda fd=parent_fd: os.close(fd), action="closing --from-dir directory descriptor"
            )


def _iter_open_file_bytes(fd: int) -> Iterator[bytes]:
    """Yield bounded-copy source bytes without relinquishing ``fd`` ownership."""
    with os.fdopen(fd, "rb", closefd=False) as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                return
            yield chunk


def _build_url(repo_id: str, revision: str, relative_path: str) -> str:
    """Build the fetch URL from validated manifest fields only.

    No operator-controlled string is ever interpolated here — only the fixed
    repo id, the pinned revision, and a grammar-validated relative path.
    """
    return f"https://huggingface.co/{repo_id}/resolve/{revision}/{relative_path}"


def _validate_fetch_url(url: httpx.URL) -> None:
    """Reject any hop (initial request or redirect) outside the trust model.

    This is the redirect-destination policy referenced by the module
    docstring: HTTPS-only, a fixed small Hugging-Face-operated hostname
    allow-list (``huggingface.co``, ``hf.co``, or a subdomain of ``hf.co``),
    no embedded userinfo, no fragment, no IP-literal host, and no non-default
    port. Every check runs *before* the hop is made — a host that would have
    served the exact pinned bytes is still rejected if it fails this policy,
    because host trust and byte-integrity are independent controls (see the
    module docstring).

    Args:
        url: The URL about to be requested (the initial built URL, or a
            redirect target already joined against the prior URL).

    Raises:
        ModelInstallError: Any check fails. The message never includes the
            URL itself (so it can never leak a signed query string or
            embedded credential) — only the fixed reason and, where safe,
            the offending hostname.
    """
    if url.scheme != "https":
        raise ModelInstallError("refusing a non-https URL while fetching the model")
    if url.userinfo:
        raise ModelInstallError("refusing a URL with embedded userinfo while fetching the model")
    if url.fragment:
        raise ModelInstallError("refusing a URL with a fragment while fetching the model")
    if url.port is not None:
        raise ModelInstallError("refusing a URL with a non-default port while fetching the model")
    host = url.host
    if not host:  # pragma: no cover - defensive: an absolute https URL always
        # has a host by the time it reaches here. The initial URL is always
        # `_build_url`'s fixed `huggingface.co` origin, and `httpx.URL.join()`
        # (used for every redirect hop) follows RFC 3986 §5.3: an empty
        # authority component in the reference is resolved as "keep the
        # base's authority", so a redirect Location cannot actually produce
        # an empty host here (confirmed empirically against httpx 0.28).
        raise ModelInstallError("refusing a URL with no hostname while fetching the model")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ModelInstallError("refusing an IP-literal host while fetching the model")
    if host in _ALLOWED_REDIRECT_HOSTS_EXACT or host.endswith(_ALLOWED_REDIRECT_HOST_SUFFIX):
        return
    raise ModelInstallError(f"refusing redirect host {host!r}: outside the allowed policy")


def _reject_content_encoding(response: httpx.Response) -> None:
    """Fail closed if the response declares any ``Content-Encoding``.

    ``Accept-Encoding: identity`` already asks the origin not to compress the
    body; this is the response-side half of that control. Any
    ``Content-Encoding`` value — including one an origin sends despite the
    request header — is rejected outright rather than transparently decoded,
    because a byte-cap check that only ever inspects the wire bytes could
    otherwise be bypassed by a compressed body that expands past the cap only
    after decoding.
    """
    if "content-encoding" in response.headers:
        raise ModelInstallError(
            "refusing a response with a Content-Encoding header while fetching the model"
        )


def _reject_oversized_declared_length(response: httpx.Response, *, byte_cap: int) -> None:
    """Fail closed before reading any body bytes if ``Content-Length`` exceeds the cap.

    This is a cheap pre-check ahead of the streaming cap enforced in
    :func:`_stream_to_temp`, which remains the authoritative backstop for a
    response that declares no length (or an inaccurate one) at all.
    """
    declared = response.headers.get("content-length")
    if declared is None:
        return
    try:
        declared_bytes = int(declared)
    except ValueError:
        return
    if declared_bytes > byte_cap:
        raise ModelInstallError(
            f"declared Content-Length {declared_bytes} exceeds the {byte_cap}-byte per-file cap"
        )


def _iter_https_bytes(client: httpx.Client, url: str, *, byte_cap: int) -> Iterator[bytes]:
    """Stream a file's bytes over HTTPS, following a bounded https-only redirect chain.

    Args:
        client: The HTTP client (a real ``httpx.Client()`` in production, a
            ``httpx.MockTransport``-backed one in tests).
        url: The initial HTTPS URL built by :func:`_build_url`.
        byte_cap: The per-file byte cap, checked against a declared
            ``Content-Length`` before any body bytes are read.

    Yields:
        Raw (never transparently decompressed) body chunks of the final
        ``200`` response.

    Raises:
        ModelInstallError: A hop fails :func:`_validate_fetch_url`, a
            redirect is malformed, the chain exceeds :data:`_MAX_REDIRECTS`,
            the final response is not ``200``, the response carries a
            ``Content-Encoding`` header or an over-cap declared
            ``Content-Length``, a transport/timeout error, or the 15-minute
            whole-file monotonic deadline expires. The message never contains
            the request URL (so it can never leak a query string or embedded
            credential).
    """
    current = httpx.URL(url)
    deadline = time.monotonic() + _FETCH_DEADLINE_SECONDS
    for _attempt in range(_MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModelInstallError("overall model fetch deadline exceeded")
        _validate_fetch_url(current)
        hop_timeout = httpx.Timeout(
            connect=min(_CONNECT_TIMEOUT_SECONDS, remaining),
            read=min(_READ_TIMEOUT_SECONDS, remaining),
            write=min(_READ_TIMEOUT_SECONDS, remaining),
            pool=min(_CONNECT_TIMEOUT_SECONDS, remaining),
        )
        try:
            with client.stream(
                "GET",
                current,
                headers=_ANONYMOUS_HEADERS,
                follow_redirects=False,
                timeout=hop_timeout,
            ) as response:
                _reject_content_encoding(response)
                if response.has_redirect_location:
                    location = response.headers["location"]
                    # Empirically, httpx itself eagerly parses/validates the
                    # Location header into a URL inside client.stream()/send()
                    # even with follow_redirects=False (it always builds the
                    # redirect request to decide whether to *follow* it) — a
                    # malformed header raises httpx.RemoteProtocolError (an
                    # httpx.HTTPError) before we ever reach this line, caught
                    # by the except below. This join is therefore
                    # defense-in-depth against a differently-behaved transport
                    # or future httpx version that skips that eager check.
                    try:
                        current = current.join(location)
                    except Exception as exc:  # pragma: no cover - httpx pre-validates, see above
                        raise ModelInstallError(
                            "malformed redirect Location header while fetching the model"
                        ) from exc
                    continue
                if response.status_code != 200:
                    raise ModelInstallError(
                        f"unexpected HTTP status {response.status_code} while fetching the model"
                    )
                _reject_oversized_declared_length(response, byte_cap=byte_cap)
                # `iter_raw()`, never `iter_bytes()`: with `Accept-Encoding:
                # identity` and `_reject_content_encoding` above, there is
                # nothing to transparently decode — using the raw stream
                # keeps that true even against a future httpx version whose
                # auto-decoding behavior changes.
                for chunk in response.iter_raw():
                    if time.monotonic() >= deadline:
                        raise ModelInstallError("overall model fetch deadline exceeded")
                    yield chunk
                return
        except httpx.HTTPError as exc:
            raise ModelInstallError("network error while fetching the model") from exc
    raise ModelInstallError("exceeded the maximum number of redirects while fetching the model")


def _validate_destination_root_trust(parent_fd: int, *, allow_sticky: bool) -> None:
    """Require a trusted directory, allowing only sticky shared ancestors."""
    directory_stat = os.fstat(parent_fd)
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise ModelInstallError("model destination root is not a directory")
    writable_by_others = directory_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if directory_stat.st_uid == os.geteuid() and not writable_by_others:
        return
    if allow_sticky and directory_stat.st_uid == 0 and directory_stat.st_mode & stat.S_ISVTX:
        return
    if directory_stat.st_uid == 0 and not writable_by_others:
        return
    raise ModelInstallError("model destination root is not an owner-only directory boundary")


def _open_destination_root(dest: Path, *, create: bool) -> tuple[Path, int]:
    """Open ``dest`` through no-follow descriptors, creating trusted components."""
    dest_path = Path(os.path.abspath(dest))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open("/", flags)
    try:
        components = dest_path.parts[1:]
        for index, component in enumerate(components):
            created_stat: os.stat_result | None = None
            child_fd: int | None = None
            try:
                child_fd = os.open(component, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o755, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ModelInstallError(
                        "cannot safely create the model destination root"
                    ) from exc
                else:
                    try:
                        created_stat = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                    except OSError as exc:
                        raise ModelInstallError(
                            "model destination root changed while being created"
                        ) from exc
                    os.fsync(parent_fd)
                try:
                    child_fd = os.open(component, flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise ModelInstallError(
                        "cannot safely open the model destination root"
                    ) from exc
            except OSError as exc:
                raise ModelInstallError("cannot safely open the model destination root") from exc
            try:
                assert child_fd is not None
                child_stat = os.fstat(child_fd)
                if created_stat is not None and (
                    child_stat.st_dev != created_stat.st_dev
                    or child_stat.st_ino != created_stat.st_ino
                ):
                    raise ModelInstallError("model destination root changed while being created")
                _validate_destination_root_trust(child_fd, allow_sticky=index < len(components) - 1)
                old_parent_fd = parent_fd
                try:
                    os.close(old_parent_fd)
                except BaseException:
                    _cleanup_preserving_primary(
                        lambda fd=child_fd: os.close(fd),
                        action="closing child destination root descriptor",
                    )
                    parent_fd = None
                    child_fd = None
                    raise
                parent_fd = child_fd
                child_fd = None
            except BaseException:
                if child_fd is not None:
                    _cleanup_preserving_primary(
                        lambda fd=child_fd: os.close(fd),
                        action="closing child destination root descriptor",
                    )
                raise
        _validate_destination_root_trust(parent_fd, allow_sticky=False)
        return dest_path, parent_fd
    except BaseException:
        if parent_fd is not None:
            _cleanup_preserving_primary(
                lambda: os.close(parent_fd), action="closing destination root descriptor"
            )
        raise


def _open_destination_parent(
    dest_root_fd: int, relative_path: str, *, create: bool
) -> tuple[int, str]:
    """Open a manifest file's parent directory beneath ``dest_root_fd``.

    Every path component is opened relative to an already-verified directory
    descriptor with ``O_NOFOLLOW``.  Creation, temporary-file placement, and
    replacement can therefore remain anchored even if an attacker swaps a
    pathname component for a symlink after the initial path validation.

    Raises:
        ModelInstallError: The destination root or a component cannot be
            safely traversed.
        OSError: A local filesystem operation fails outside the fail-closed
            validation cases.
    """
    _validate_relative_path(relative_path)
    components = relative_path.split("/")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.dup(dest_root_fd)
    try:
        for component in components[:-1]:
            created_stat: os.stat_result | None = None
            child_fd: int | None = None
            if create:
                try:
                    os.mkdir(component, mode=0o755, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ModelInstallError(
                        f"cannot safely create destination directory for {relative_path!r}"
                    ) from exc
                else:
                    try:
                        created_stat = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                    except OSError as exc:
                        raise ModelInstallError(
                            "destination directory changed while being created "
                            f"for {relative_path!r}"
                        ) from exc
                    os.fsync(parent_fd)
            try:
                child_fd = os.open(component, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise
                raise ModelInstallError(
                    f"destination directory disappeared for {relative_path!r}"
                ) from None
            except OSError as exc:
                raise ModelInstallError(
                    f"cannot safely open destination directory for {relative_path!r}"
                ) from exc
            try:
                assert child_fd is not None
                child_stat = os.fstat(child_fd)
                if created_stat is not None and (
                    child_stat.st_dev != created_stat.st_dev
                    or child_stat.st_ino != created_stat.st_ino
                ):
                    raise ModelInstallError(
                        f"destination directory changed while being created for {relative_path!r}"
                    )
                _validate_destination_parent_trust(child_fd)
                old_parent_fd = parent_fd
                try:
                    os.close(old_parent_fd)
                except BaseException:
                    _cleanup_preserving_primary(
                        lambda fd=child_fd: os.close(fd),
                        action="closing child destination directory descriptor",
                    )
                    parent_fd = None
                    child_fd = None
                    raise
                parent_fd = child_fd
                child_fd = None
            except BaseException:
                if child_fd is not None:
                    _cleanup_preserving_primary(
                        lambda fd=child_fd: os.close(fd),
                        action="closing child destination directory descriptor",
                    )
                raise
    except BaseException:
        if parent_fd is not None:
            _cleanup_preserving_primary(
                lambda: os.close(parent_fd), action="closing destination directory descriptor"
            )
        raise
    return parent_fd, components[-1]


def _stream_to_temp(
    parent_fd: int, target_name: str, chunks: Iterable[bytes], *, byte_cap: int
) -> tuple[str, int, str]:
    """Stream ``chunks`` to a descriptor-anchored temp file, hashing as it writes.

    The temp file lives in the already-open destination parent so the eventual
    ``os.replace`` is an atomic same-filesystem rename. Any exception —
    including exceeding ``byte_cap`` — removes the temp file before propagating.

    Returns:
        The temp filename (relative to ``parent_fd``), its still-open
        descriptor, and its streamed SHA-256 hex digest.

    Raises:
        ModelInstallError: Streamed bytes exceed the per-file cap.
        OSError: A local filesystem operation fails.
    """
    _validate_destination_parent_trust(parent_fd)
    tmp_name = f".{target_name}.{uuid.uuid4().hex}.part"
    fd = os.open(
        tmp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb", closefd=False) as fh:
            for chunk in chunks:
                total += len(chunk)
                if total > byte_cap:
                    raise ModelInstallError(
                        f"download for {target_name!r} exceeded the {byte_cap}-byte per-file cap"
                    )
                digest.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        _cleanup_preserving_primary(
            lambda: os.close(fd), action="closing temporary model file descriptor"
        )
        _cleanup_preserving_primary(
            lambda: os.unlink(tmp_name, dir_fd=parent_fd), action="removing temporary model file"
        )
        raise
    return tmp_name, fd, digest.hexdigest()


def _place_verified(
    parent_fd: int,
    target_name: str,
    chunks: Iterable[bytes],
    *,
    expected_sha256: str,
    byte_cap: int,
    context: str,
) -> tuple[int, int]:
    """Stream ``chunks`` to a temp file, verify the digest, then atomically place.

    Raises:
        ModelInstallError: The streamed digest does not match
            ``expected_sha256`` (the temp file is removed; the final path is
            never written before verification) or streaming itself failed.
        OSError: A local filesystem operation fails.

    Returns:
        Device and inode of the placed file, for live-path revalidation by the
        caller before it reports success.
    """
    tmp_name, tmp_fd, digest_hex = _stream_to_temp(
        parent_fd, target_name, chunks, byte_cap=byte_cap
    )
    temp_exists = True
    try:
        if digest_hex != expected_sha256:
            raise ModelInstallError(f"digest mismatch for {context!r}")
        os.fchmod(tmp_fd, 0o644)
        os.fsync(tmp_fd)
        verified_temp = os.fstat(tmp_fd)
        named_temp = os.stat(tmp_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(named_temp.st_mode)
            or named_temp.st_dev != verified_temp.st_dev
            or named_temp.st_ino != verified_temp.st_ino
        ):
            raise ModelInstallError(
                f"verified temporary file changed before placement for {context!r}"
            )
        _validate_destination_parent_trust(parent_fd)
        try:
            existing_target = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(existing_target.st_mode):
                raise ModelInstallError(
                    f"refusing to overwrite a non-regular target for {context!r}"
                )
        fd_to_close = tmp_fd
        tmp_fd = -1
        os.close(fd_to_close)
        os.replace(tmp_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temp_exists = False
        os.fsync(parent_fd)
        return verified_temp.st_dev, verified_temp.st_ino
    except BaseException:
        # A chmod/replace failure (e.g. a permission error, or the target's
        # parent disappearing between verification and rename) must not leave
        # a verified-but-unplaced temp file behind — clean up on every failure
        # path, not only a digest mismatch. `os.replace` is a single atomic
        # rename on POSIX: if it raises, the file never moved, so the temp
        # path is still the one to remove.
        if tmp_fd != -1:
            _cleanup_preserving_primary(
                lambda: os.close(tmp_fd), action="closing temporary model file descriptor"
            )
        if temp_exists:
            _cleanup_preserving_primary(
                lambda: os.unlink(tmp_name, dir_fd=parent_fd),
                action="removing temporary model file",
            )
        raise


def _revalidate_live_destination(
    dest_root_path: Path,
    relative_path: str,
    *,
    dest_root_fd: int,
    parent_fd: int,
    device: int,
    inode: int,
) -> None:
    """Prove the live destination path still names the verified placement.

    A directory descriptor remains usable after its pathname is renamed away.
    Reopening the path chain below the destination root and comparing both the
    parent and final entry prevents a detached-directory placement from being
    reported as success for the original destination path.
    """
    live_root_fd: int | None = None
    live_parent_fd: int | None = None
    try:
        try:
            _live_root_path, live_root_fd = _open_destination_root(dest_root_path, create=False)
        except FileNotFoundError as exc:
            raise ModelInstallError(
                f"destination changed before placement could be confirmed for {relative_path!r}"
            ) from exc
        expected_root = os.fstat(dest_root_fd)
        live_root = os.fstat(live_root_fd)
        if expected_root.st_dev != live_root.st_dev or expected_root.st_ino != live_root.st_ino:
            raise ModelInstallError(
                f"destination changed before placement could be confirmed for {relative_path!r}"
            )
        live_parent_fd, target_name = _open_destination_parent(
            live_root_fd, relative_path, create=False
        )
        assert live_parent_fd is not None
        expected_parent = os.fstat(parent_fd)
        live_parent = os.fstat(live_parent_fd)
        _validate_destination_parent_trust(live_parent_fd)
        try:
            live_target = os.stat(target_name, dir_fd=live_parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ModelInstallError(
                f"destination changed before placement could be confirmed for {relative_path!r}"
            ) from exc
        if (
            expected_parent.st_dev != live_parent.st_dev
            or expected_parent.st_ino != live_parent.st_ino
            or not stat.S_ISREG(live_target.st_mode)
            or live_target.st_dev != device
            or live_target.st_ino != inode
        ):
            raise ModelInstallError(
                f"destination changed before placement could be confirmed for {relative_path!r}"
            )
    except BaseException:
        if live_parent_fd is not None:
            _cleanup_preserving_primary(
                lambda: os.close(live_parent_fd),
                action="closing live destination directory descriptor",
            )
        raise
    else:
        os.close(live_parent_fd)
    finally:
        if live_root_fd is not None:
            _cleanup_preserving_primary(
                lambda: os.close(live_root_fd), action="closing live destination root descriptor"
            )


@dataclass(frozen=True, slots=True)
class ManifestFileResult:
    """Per-file outcome of an :func:`install_model` call.

    Attributes:
        relative_path: The manifest-declared relative path.
        sha256: The verified SHA-256 digest (always equal to the manifest's).
        source: ``"cached"`` (already valid at the destination, no I/O
            performed beyond hashing), ``"local"`` (copied from
            ``--from-dir``), or ``"fetched"`` (downloaded over HTTPS).
    """

    relative_path: str
    sha256: str
    source: str


@dataclass(frozen=True, slots=True)
class ModelInstallSummary:
    """Aggregate result of an :func:`install_model` call.

    Attributes:
        dest: The resolved (real-path) destination root.
        files: Per-file results, in manifest order.
        network_used: Whether any file required an HTTPS fetch.
    """

    dest: Path
    files: tuple[ManifestFileResult, ...]
    network_used: bool

    def to_json_dict(self) -> dict[str, object]:
        """A machine-readable summary safe to print: no URL, token, or env value."""
        return {
            "dest": str(self.dest),
            "network_used": self.network_used,
            "files": [
                {"relative_path": f.relative_path, "sha256": f.sha256, "source": f.source}
                for f in self.files
            ],
        }


def install_model(
    dest: Path,
    *,
    from_dir: Path | None = None,
    verify_only: bool = False,
    manifest_files: Sequence[ManifestFile] = MANIFEST_FILES,
    repo_id: str = REPO_ID,
    revision: str = REVISION,
    http_client: httpx.Client | None = None,
) -> ModelInstallSummary:
    """Place and verify every manifest file under ``dest``.

    Resolution order per file, fail-closed at each step:

    1. Already valid at ``dest`` (matching SHA-256) → no I/O beyond hashing,
       no network call — this is the idempotence path and the guarantee that
       a roast never waits on a live Hugging Face pull.
    2. ``from_dir`` supplied → copy and verify from it; a missing file there
       is an error, **never** a silent fallback to network.
    3. Otherwise → fetch anonymously over HTTPS (see the module docstring for
       the fetch's trust model).

    Args:
        dest: Destination root directory (created if it does not exist,
            unless ``verify_only``).
        from_dir: Optional local source directory for air-gapped
            preparation, mirroring the manifest's relative layout.
        verify_only: When set, never places or fetches anything — only checks
            that every manifest file already exists at ``dest`` with a
            matching digest, raising if any is missing or mismatched.
        manifest_files: The manifest file set (defaults to the pinned
            first-crack model manifest; overridable for tests).
        repo_id: The Hugging Face repository id (defaults to the pinned one).
        revision: The pinned revision (defaults to the pinned one).
        http_client: An optional pre-built ``httpx.Client`` (a
            ``httpx.MockTransport``-backed one in tests); a real client is
            created and closed internally when omitted.

    Returns:
        A summary of what happened to each file.

    Raises:
        ModelInstallError: Any manifest, path, verification, or fetch step
            fails closed (see the per-step docstrings above).
        OSError: A local filesystem or environment operation fails outside
            the fail-closed validation cases.
    """
    _validate_revision(revision)
    _validate_repo_id(repo_id)
    if not manifest_files:
        raise ModelInstallError("manifest_files must not be empty")

    try:
        dest_root_path, dest_root_fd = _open_destination_root(dest, create=not verify_only)
    except FileNotFoundError:
        if verify_only:
            raise ModelInstallError(
                f"destination {dest} does not exist; nothing to verify"
            ) from None
        raise

    results: list[ManifestFileResult] = []
    network_used = False
    owned_client = http_client is None
    client = http_client
    try:
        # `trust_env=False`: this internally-owned client must never inherit
        # ambient HTTP_PROXY/HTTPS_PROXY/NO_PROXY or CA-bundle environment
        # variables (a caller-supplied test/mock client is unaffected).
        if client is None:
            client = httpx.Client(trust_env=False)
        assert client is not None
        for manifest_file in manifest_files:
            _validate_digest(manifest_file.sha256, label=manifest_file.relative_path)
            target = _resolve_and_validate_target(dest_root_path, manifest_file.relative_path)
            target_name = manifest_file.relative_path.rsplit("/", maxsplit=1)[-1]
            try:
                parent_fd, target_name = _open_destination_parent(
                    dest_root_fd, manifest_file.relative_path, create=False
                )
            except FileNotFoundError:
                parent_fd = None
            try:
                if parent_fd is not None:
                    _validate_destination_parent_trust(parent_fd)
                    _lock_destination_parent(parent_fd)
                cached = (
                    _open_verified_cached_file(target_name, dir_fd=parent_fd)
                    if parent_fd is not None
                    else None
                )
                if cached is not None:
                    cached_digest, cached_fd, cached_device, cached_inode = cached
                    try:
                        if cached_digest == manifest_file.sha256:
                            assert parent_fd is not None
                            _revalidate_live_destination(
                                dest_root_path,
                                manifest_file.relative_path,
                                dest_root_fd=dest_root_fd,
                                parent_fd=parent_fd,
                                device=cached_device,
                                inode=cached_inode,
                            )
                            results.append(
                                ManifestFileResult(
                                    manifest_file.relative_path, manifest_file.sha256, "cached"
                                )
                            )
                            continue
                    finally:
                        _cleanup_preserving_primary(
                            lambda fd=cached_fd: os.close(fd),
                            action="closing cached model file descriptor",
                        )

                if verify_only:
                    raise ModelInstallError(
                        f"verification failed for {manifest_file.relative_path!r}: "
                        f"missing or digest mismatch at {target}"
                    )

                if from_dir is not None:
                    from_dir_real = Path(os.path.realpath(from_dir))
                    _resolve_and_validate_source(from_dir_real, manifest_file.relative_path)
                    source_fd = _open_verified_source_file(
                        from_dir_real, manifest_file.relative_path
                    )
                    try:
                        if parent_fd is None:
                            parent_fd, target_name = _open_destination_parent(
                                dest_root_fd, manifest_file.relative_path, create=True
                            )
                            _validate_destination_parent_trust(parent_fd)
                            _lock_destination_parent(parent_fd)
                        _reclaim_abandoned_parts(parent_fd, target_name)
                        placed_device, placed_inode = _place_verified(
                            parent_fd,
                            target_name,
                            _iter_open_file_bytes(source_fd),
                            expected_sha256=manifest_file.sha256,
                            byte_cap=_MAX_MODEL_FILE_BYTES,
                            context=manifest_file.relative_path,
                        )
                        _revalidate_live_destination(
                            dest_root_path,
                            manifest_file.relative_path,
                            dest_root_fd=dest_root_fd,
                            parent_fd=parent_fd,
                            device=placed_device,
                            inode=placed_inode,
                        )
                    finally:
                        _cleanup_preserving_primary(
                            lambda fd=source_fd: os.close(fd),
                            action="closing --from-dir source descriptor",
                        )
                    results.append(
                        ManifestFileResult(
                            manifest_file.relative_path, manifest_file.sha256, "local"
                        )
                    )
                    continue

                network_used = True
                url = _build_url(repo_id, revision, manifest_file.relative_path)
                if parent_fd is None:
                    parent_fd, target_name = _open_destination_parent(
                        dest_root_fd, manifest_file.relative_path, create=True
                    )
                    _validate_destination_parent_trust(parent_fd)
                    _lock_destination_parent(parent_fd)
                _reclaim_abandoned_parts(parent_fd, target_name)
                placed_device, placed_inode = _place_verified(
                    parent_fd,
                    target_name,
                    _iter_https_bytes(client, url, byte_cap=_MAX_MODEL_FILE_BYTES),
                    expected_sha256=manifest_file.sha256,
                    byte_cap=_MAX_MODEL_FILE_BYTES,
                    context=manifest_file.relative_path,
                )
                _revalidate_live_destination(
                    dest_root_path,
                    manifest_file.relative_path,
                    dest_root_fd=dest_root_fd,
                    parent_fd=parent_fd,
                    device=placed_device,
                    inode=placed_inode,
                )
                results.append(
                    ManifestFileResult(manifest_file.relative_path, manifest_file.sha256, "fetched")
                )
            finally:
                if parent_fd is not None:
                    _cleanup_preserving_primary(
                        lambda fd=parent_fd: os.close(fd),
                        action="closing destination directory descriptor",
                    )
    finally:
        try:
            if owned_client and client is not None:
                _cleanup_preserving_primary(client.close, action="closing model download client")
        finally:
            _cleanup_preserving_primary(
                lambda: os.close(dest_root_fd), action="closing destination root descriptor"
            )

    return ModelInstallSummary(dest=dest_root_path, files=tuple(results), network_used=network_used)
