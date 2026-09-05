"""Secure placement/verification of the bundled/pinned first-crack model (AC5).

This module places the exact model files pinned in
:mod:`roastpilot_agent.appliance.model_manifest` onto local disk so a roast
never waits on a live Hugging Face pull (`roastpilot-agent/plan.md:30`), and
so the appliance can be prepared fully air-gapped via ``--from-dir``.

**External-input surface.** When neither a cached copy nor ``--from-dir``
satisfies a manifest file, this module fetches it anonymously over HTTPS from
``huggingface.co``. This is why the story requires ``security-reviewer``: the
fetched bytes originate outside this process. The mitigations below are the
whole of the trust model:

- The fetch URL is built **only** from the closed manifest fields (a
  40-hex-char revision, a validated relative path, and the fixed
  ``owner/name`` repo id) — never from any operator-supplied string, so there
  is no URL-injection surface.
- Every byte is verified against the manifest's committed SHA-256 digest
  before it is ever visible at its final path (stream-to-temp, verify,
  ``os.replace``). **Cross-host redirects are deliberately permitted**: a
  redirect may legitimately land on a CDN host different from
  ``huggingface.co``. That is safe here specifically because integrity is
  established by the committed digest, not by transport host trust — a
  redirect to any host still has to serve exactly the pinned bytes or the
  install fails closed. Only the *scheme* (``https`` only) and the *number*
  of hops (bounded) are policed on the redirect chain itself.
  Downloaded bytes are never executed, unpacked, or made executable.
- The request is anonymous: no ``Authorization`` header is ever sent and no
  token is ever read from the environment.
- A per-file byte cap, connect/read timeouts, and a bounded redirect chain
  (the "total attempt bound") each fail closed and remove any partial file.

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

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Iterator, Sequence
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

#: Anonymous, fixed User-Agent. No credential, token, or environment value is
#: ever placed in a request header.
_ANONYMOUS_HEADERS: Final[dict[str, str]] = {
    "user-agent": "roastpilot-agent-appliance-model-install/1",
}

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


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _is_already_valid(target: Path, expected_sha256: str) -> bool:
    """A regular file already exists at ``target`` with the expected digest."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    return _sha256_of_file(target) == expected_sha256


def _iter_file_bytes(path: Path) -> Iterator[bytes]:
    with path.open("rb") as fh:
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


def _iter_https_bytes(client: httpx.Client, url: str) -> Iterator[bytes]:
    """Stream a file's bytes over HTTPS, following a bounded https-only redirect chain.

    Args:
        client: The HTTP client (a real ``httpx.Client()`` in production, a
            ``httpx.MockTransport``-backed one in tests).
        url: The initial HTTPS URL built by :func:`_build_url`.

    Yields:
        Body chunks of the final ``200`` response.

    Raises:
        ModelInstallError: A non-https URL appears anywhere in the chain, a
            redirect is malformed, the chain exceeds :data:`_MAX_REDIRECTS`,
            the final response is not ``200``, or a transport/timeout error
            occurs. The message never contains the request URL (so it can
            never leak a query string or embedded credential).
    """
    current = httpx.URL(url)
    timeout = httpx.Timeout(
        connect=_CONNECT_TIMEOUT_SECONDS,
        read=_READ_TIMEOUT_SECONDS,
        write=_READ_TIMEOUT_SECONDS,
        pool=_CONNECT_TIMEOUT_SECONDS,
    )
    for _attempt in range(_MAX_REDIRECTS + 1):
        if current.scheme != "https":
            raise ModelInstallError("refusing a non-https URL while fetching the model")
        try:
            with client.stream(
                "GET",
                current,
                headers=_ANONYMOUS_HEADERS,
                follow_redirects=False,
                timeout=timeout,
            ) as response:
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
                yield from response.iter_bytes()
                return
        except httpx.HTTPError as exc:
            raise ModelInstallError("network error while fetching the model") from exc
    raise ModelInstallError("exceeded the maximum number of redirects while fetching the model")


def _stream_to_temp(target: Path, chunks: Iterable[bytes], *, byte_cap: int) -> tuple[Path, str]:
    """Stream ``chunks`` to a temp file beside ``target``, hashing as it writes.

    The temp file lives in ``target``'s own parent directory so the eventual
    ``os.replace`` is an atomic same-filesystem rename. Any exception —
    including exceeding ``byte_cap`` — removes the temp file before
    propagating.

    Returns:
        The temp file path and its streamed SHA-256 hex digest.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
    tmp_path = Path(tmp_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            for chunk in chunks:
                total += len(chunk)
                if total > byte_cap:
                    raise ModelInstallError(
                        f"download for {target.name!r} exceeded the {byte_cap}-byte per-file cap"
                    )
                digest.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path, digest.hexdigest()


def _place_verified(
    target: Path,
    chunks: Iterable[bytes],
    *,
    expected_sha256: str,
    byte_cap: int,
    context: str,
) -> None:
    """Stream ``chunks`` to a temp file, verify the digest, then atomically place.

    Raises:
        ModelInstallError: The streamed digest does not match
            ``expected_sha256`` (the temp file is removed; the final path is
            never written before verification) or streaming itself failed.
    """
    tmp_path, digest_hex = _stream_to_temp(target, chunks, byte_cap=byte_cap)
    if digest_hex != expected_sha256:
        tmp_path.unlink(missing_ok=True)
        raise ModelInstallError(f"digest mismatch for {context!r}")
    try:
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, target)
    except BaseException:
        # A chmod/replace failure (e.g. a permission error, or the target's
        # parent disappearing between verification and rename) must not leave
        # a verified-but-unplaced temp file behind — clean up on every failure
        # path, not only a digest mismatch. `os.replace` is a single atomic
        # rename on POSIX: if it raises, the file never moved, so the temp
        # path is still the one to remove.
        tmp_path.unlink(missing_ok=True)
        raise


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
    """
    _validate_revision(revision)
    _validate_repo_id(repo_id)
    if not manifest_files:
        raise ModelInstallError("manifest_files must not be empty")

    if verify_only:
        if not dest.is_dir():
            raise ModelInstallError(f"destination {dest} does not exist; nothing to verify")
        dest_root_real = Path(os.path.realpath(dest))
    else:
        dest.mkdir(parents=True, exist_ok=True)
        dest_root_real = Path(os.path.realpath(dest))

    results: list[ManifestFileResult] = []
    network_used = False
    owned_client = http_client is None
    client = http_client if http_client is not None else httpx.Client()
    try:
        for manifest_file in manifest_files:
            _validate_digest(manifest_file.sha256, label=manifest_file.relative_path)
            target = _resolve_and_validate_target(dest_root_real, manifest_file.relative_path)

            if _is_already_valid(target, manifest_file.sha256):
                results.append(
                    ManifestFileResult(manifest_file.relative_path, manifest_file.sha256, "cached")
                )
                continue

            if verify_only:
                raise ModelInstallError(
                    f"verification failed for {manifest_file.relative_path!r}: "
                    f"missing or digest mismatch at {target}"
                )

            if from_dir is not None:
                source = from_dir / manifest_file.relative_path
                if not source.is_file():
                    raise ModelInstallError(
                        f"--from-dir is missing required file {manifest_file.relative_path!r}"
                    )
                _place_verified(
                    target,
                    _iter_file_bytes(source),
                    expected_sha256=manifest_file.sha256,
                    byte_cap=_MAX_MODEL_FILE_BYTES,
                    context=manifest_file.relative_path,
                )
                results.append(
                    ManifestFileResult(manifest_file.relative_path, manifest_file.sha256, "local")
                )
                continue

            network_used = True
            url = _build_url(repo_id, revision, manifest_file.relative_path)
            _place_verified(
                target,
                _iter_https_bytes(client, url),
                expected_sha256=manifest_file.sha256,
                byte_cap=_MAX_MODEL_FILE_BYTES,
                context=manifest_file.relative_path,
            )
            results.append(
                ManifestFileResult(manifest_file.relative_path, manifest_file.sha256, "fetched")
            )
    finally:
        if owned_client:
            client.close()

    return ModelInstallSummary(dest=dest_root_real, files=tuple(results), network_used=network_used)
