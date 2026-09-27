"""Descriptor-bound private evidence store, seal, and retained-copy verification.

This module writes, seals, and verifies private cold-characterisation evidence
trees.  It computes integrity facts only: it never interprets an outcome, never
deletes, moves, shortens, or repairs anything, and never claims a filesystem
transaction.  The sealing boundary is "every node seen at enumeration pass 1 is
unchanged at pass 2"; same-size, same-timestamp rewrites between the two
identity reads remain a named residual.
"""

import collections.abc
import enum
import hashlib
import json
import math
import os
import stat
import typing

import pydantic

from roastpilot_agent.cold_characterisation.evidence_schema import (
    ColdCapabilityBranch,
    ColdEnvelopeKind,
    ColdEvidenceError,
    ColdEvidenceRecord,
    ColdFinalisationRecord,
    ColdFinalisationStatus,
    ColdPhaseKind,
    ColdRunHeader,
    ColdSealedEnvelope,
    validate_record,
)
from roastpilot_agent.cold_characterisation.mcp import (
    SessionFinalisationResult,
    finalisation_command_streaming_observation,
)

MAX_MANIFEST_ENTRIES = 16_384
MAX_EVIDENCE_FILE_BYTES = 1_073_741_824
MAX_MANIFEST_BYTES = 8_388_608
MAX_PATH_SEGMENTS = 4
MAX_SEGMENT_CHARACTERS = 128
MAX_IDENTITY_EXTRA_KEYS = 64
MAX_MODEL_MANIFEST_ENTRIES = 1_024
MANIFEST_JSON_NAME = "manifest.json"
MANIFEST_SIDECAR_NAME = "manifest.sha256"
_RESERVED_TOP_LEVEL_NAMES = frozenset({MANIFEST_JSON_NAME, MANIFEST_SIDECAR_NAME})
_READ_CHUNK_BYTES = 1_048_576
_HEX_DIGITS = frozenset("0123456789abcdef")
_SEGMENT_FIRST = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
_SEGMENT_REST = _SEGMENT_FIRST | frozenset("_.-")
_RUN_ID_ADAPTER: pydantic.TypeAdapter[str] = pydantic.TypeAdapter(
    typing.Annotated[str, *ColdRunHeader.model_fields["run_id"].metadata]
)
_ADMISSION_TOKEN = object()


class ColdEvidenceStoreFailure(enum.Enum):
    """Closed failures for private evidence storage, sealing, and reading."""

    PLATFORM_UNSUPPORTED = "platform_unsupported"
    ROOT_NOT_ABSOLUTE = "root_not_absolute"
    ROOT_PROTECTED = "root_protected"
    ROOTS_OVERLAP = "roots_overlap"
    ROOT_UNUSABLE = "root_unusable"
    RUN_DIR_EXISTS = "run_dir_exists"
    OWNERSHIP_OR_MODE_MISMATCH = "ownership_or_mode_mismatch"
    RUN_ID_MISMATCHED = "run_id_mismatched"
    HEADER_MISSING = "header_missing"
    HEADER_DUPLICATED = "header_duplicated"
    HEADER_BINDING_MISMATCHED = "header_binding_mismatched"
    IDENTITY_DIGEST_MISMATCHED = "identity_digest_mismatched"
    FINALISATION_INDEX_MISMATCHED = "finalisation_index_mismatched"
    WRITER_SEALED = "writer_sealed"
    WRITER_POISONED = "writer_poisoned"
    WRITE_FAILED = "write_failed"
    SEAL_TREE_INVALID = "seal_tree_invalid"
    SEAL_TREE_CHANGED = "seal_tree_changed"
    SEAL_LIMIT_EXCEEDED = "seal_limit_exceeded"
    FILE_CHANGED = "file_changed"
    FILE_READ_INCOMPLETE = "file_read_incomplete"
    MANIFEST_MALFORMED = "manifest_malformed"
    MANIFEST_DIGEST_MISMATCHED = "manifest_digest_mismatched"
    MANIFEST_COPIES_DIFFER = "manifest_copies_differ"
    SIDECAR_INCONSISTENT = "sidecar_inconsistent"
    INVENTORY_MISMATCHED = "inventory_mismatched"
    ENTRY_PATH_INVALID = "entry_path_invalid"
    ENTRY_DUPLICATED = "entry_duplicated"
    FILE_NOT_REGULAR = "file_not_regular"
    FILE_DIGEST_MISMATCHED = "file_digest_mismatched"
    LINE_MALFORMED = "line_malformed"
    LINE_TOO_LARGE = "line_too_large"
    JSON_DUPLICATE_KEY = "json_duplicate_key"
    JSON_NOT_FINITE = "json_not_finite"
    LINE_NOT_CANONICAL = "line_not_canonical"
    SCHEMA_VERSION_UNKNOWN = "schema_version_unknown"
    IDENTITY_NOT_V1 = "identity_not_v1"


class ColdEvidenceStoreError(RuntimeError):
    """Closed store error carrying only its failure member and a fixed message."""

    failure: ColdEvidenceStoreFailure

    def __init__(self, failure: ColdEvidenceStoreFailure) -> None:
        """Create a content-free store failure.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold evidence store operation failed.")
        self.failure = failure


_T = typing.TypeVar("_T")
_OS_ERRORS: tuple[type[BaseException], ...] = (OSError, ValueError, OverflowError, TypeError)


def _guard(
    call: collections.abc.Callable[[], _T],
    failure: ColdEvidenceStoreFailure,
    errors: tuple[type[BaseException], ...] = _OS_ERRORS,
) -> _T:
    """Run one call and map expected errors to a fresh chain-free closed error."""
    try:
        value = call()
    except errors:
        pass
    else:
        return value
    raise ColdEvidenceStoreError(failure)


def _release(descriptor: int) -> bool:
    """Close one descriptor exactly once, reporting (never raising) a close failure.

    A failed close is never retried: the kernel may already have released the
    number, which could then belong to an unrelated later open.  A ``False`` result
    therefore proves nothing about the descriptor's final kernel state.
    """
    try:
        os.close(descriptor)
    except OSError:
        return False
    return True


def _release_all(descriptors: collections.abc.Iterable[int]) -> bool:
    """Attempt every descriptor exactly once; ``False`` if any release failed."""
    released = True
    for descriptor in descriptors:
        released = _release(descriptor) and released
    return released


def _open_at(name: str, flags: int, dir_fd: int, failure: ColdEvidenceStoreFailure) -> int:
    """Open one name relative to a directory descriptor, mapping failures closed."""
    return _guard(lambda: os.open(name, flags, dir_fd=dir_fd), failure)


def _fstat(descriptor: int, failure: ColdEvidenceStoreFailure) -> "_NodeStat":
    """Return one descriptor's identity, mapping failures closed."""
    return _guard(lambda: _node_stat(os.fstat(descriptor)), failure)


def _lstat_at(name: str, dir_fd: int, failure: ColdEvidenceStoreFailure) -> "_NodeStat":
    """Return one directory entry's no-follow identity, mapping failures closed."""
    return _guard(lambda: _node_stat(os.stat(name, dir_fd=dir_fd, follow_symlinks=False)), failure)


def _scan_directory(
    descriptor: int, failure: ColdEvidenceStoreFailure
) -> "os._ScandirIterator[str]":  # pyright: ignore[reportPrivateUsage]
    """Open one incremental directory scan by descriptor, mapping failures closed."""
    return _guard(lambda: os.scandir(descriptor), failure)


def _next_name(
    iterator: collections.abc.Iterator["os.DirEntry[str]"], failure: ColdEvidenceStoreFailure
) -> str | None:
    """Return the next scanned entry name, or ``None`` at the end, mapping failures closed."""
    entry = _guard(lambda: next(iterator, None), failure)
    return None if entry is None else entry.name


def _realpath(path: str, failure: ColdEvidenceStoreFailure) -> str:
    """Resolve one path, mapping failures closed."""
    return _guard(lambda: os.path.realpath(path), failure)


def canonical_json(value: object) -> str:
    """Return the canonical JSON text used for every retained evidence artefact.

    Args:
        value: An already admitted JSON-shaped value.

    Returns:
        Sorted-key, compact, non-ASCII-preserving JSON that refuses non-finite numbers.
    """
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object, refusing any repeated key."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.JSON_DUPLICATE_KEY)
        result[key] = value
    return result


def _reject_constant(_token: str) -> object:
    """Refuse the NaN and infinity literals Python's JSON parser admits."""
    raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.JSON_NOT_FINITE)


def _finite_float(token: str) -> float:
    """Parse one JSON number literal, refusing overflow to infinity."""
    value = float(token)
    if not math.isfinite(value):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.JSON_NOT_FINITE)
    return value


def load_strict_json(data: bytes, *, malformed: ColdEvidenceStoreFailure) -> object:
    """Decode strict UTF-8 JSON, refusing duplicate keys and non-finite numbers.

    This is the single strict loader for lines, manifests, and identity JSON.

    Args:
        data: Exact bytes to decode.
        malformed: Closed failure for any other decoding or syntax error.

    Returns:
        The decoded JSON value.

    Raises:
        ColdEvidenceStoreError: If decoding fails or a strictness rule is broken.
    """
    text = _guard(lambda: data.decode("utf-8", "strict"), malformed)
    return _guard(
        lambda: json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        ),
        malformed,
        (ValueError, RecursionError),
    )


def _is_sha256_hex(value: object) -> bool:
    """Whether a value is exactly 64 lowercase hexadecimal characters."""
    return type(value) is str and len(value) == 64 and all(c in _HEX_DIGITS for c in value)


def _segment_is_valid(segment: str) -> bool:
    """Whether one path segment satisfies the closed segment grammar."""
    return (
        0 < len(segment) <= MAX_SEGMENT_CHARACTERS
        and segment[0] in _SEGMENT_FIRST
        and all(c in _SEGMENT_REST for c in segment[1:])
    )


def _entry_path_is_valid(relative_path: str) -> bool:
    """Whether one manifest entry path is a bounded, non-reserved relative path."""
    segments = relative_path.split("/")
    return (
        len(segments) <= MAX_PATH_SEGMENTS
        and all(_segment_is_valid(segment) for segment in segments)
        and relative_path not in _RESERVED_TOP_LEVEL_NAMES
    )


def run_id_is_valid(run_id: object) -> bool:
    """Whether a value matches the delivered evidence-record run-id grammar.

    Args:
        run_id: Candidate run identifier.

    Returns:
        ``True`` only for a string matching the record schema's run-id pattern.
    """
    if type(run_id) is not str:
        return False
    try:
        _RUN_ID_ADAPTER.validate_python(run_id, strict=True)
    except pydantic.ValidationError:
        return False
    return True


# --------------------------------------------------------------------------- roots


def _platform_is_supported() -> bool:
    """Whether descriptor-relative, no-follow filesystem operations are available."""
    return (
        os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and os.scandir in os.supports_fd
        and hasattr(os, "O_NONBLOCK")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "fchmod")
    )


def _directory_flags() -> int:
    """Return no-follow read-only directory open flags."""
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_read_flags() -> int:
    """Return no-follow, non-blocking read-only open flags.

    ``O_NONBLOCK`` keeps a regular file swapped for a FIFO from blocking the open;
    the following identity check then refuses the non-regular node.
    """
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _file_create_flags(*, append: bool) -> int:
    """Return exclusive no-follow create flags."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    return flags | os.O_APPEND if append else flags


def _source_checkout_root(module_file: str) -> str | None:
    """Return the source-checkout root containing a module, or ``None``; never raises.

    Args:
        module_file: Path of a module at ``src/roastpilot_agent/cold_characterisation/``.

    Returns:
        The real checkout root when it holds ``pyproject.toml`` and ``src/roastpilot_agent/``.
    """
    try:
        candidate = os.path.realpath(module_file)
        for _ in range(4):
            candidate = os.path.dirname(candidate)
        if os.path.isfile(os.path.join(candidate, "pyproject.toml")) and os.path.isdir(
            os.path.join(candidate, "src", "roastpilot_agent")
        ):
            return candidate
    except (OSError, ValueError, TypeError):
        return None
    return None


def _is_within(path: str, parent: str) -> bool:
    """Whether a real path equals or lies under a real parent, component-wise."""
    return path == parent or os.path.commonpath([path, parent]) == parent


def _protected_realpaths(declared: tuple[str, ...]) -> tuple[str, ...]:
    """Return the add-only protected set: package, checkout (if any), declared roots."""
    package_directory = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    protected = [package_directory]
    checkout = _source_checkout_root(__file__)
    if checkout is not None:
        protected.append(checkout)
    for root in declared:
        if type(root) is not str or not os.path.isabs(root):
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOT_NOT_ABSOLUTE)
        protected.append(_realpath(root, ColdEvidenceStoreFailure.ROOT_UNUSABLE))
    return tuple(protected)


def _directory_identity(descriptor: int) -> tuple[int, int]:
    """Return one open directory's ``(st_dev, st_ino)``, mapping failures closed."""
    node = _fstat(descriptor, ColdEvidenceStoreFailure.ROOT_UNUSABLE)
    return node.dev, node.ino


def _protected_identities(protected: tuple[str, ...]) -> frozenset[tuple[int, int]]:
    """Return ``(st_dev, st_ino)`` of every protected root that exists.

    A protected root that does not exist contributes nothing here; its resolved-path
    containment check still applies.  Any other stat failure fails closed.
    """
    identities: set[tuple[int, int]] = set()
    for path in protected:
        result: os.stat_result | None = None
        missing = False
        try:
            result = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            missing = True
        except (OSError, ValueError):
            pass
        if missing:
            continue
        if result is None:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOT_UNUSABLE)
        identities.add((result.st_dev, result.st_ino))
    return frozenset(identities)


def _open_absolute_directory(path: str, lineage: list[tuple[int, int]] | None = None) -> int:
    """Open an absolute directory by per-component no-follow descriptor traversal.

    When ``lineage`` is given, it receives ``(st_dev, st_ino)`` of ``/`` and of every
    opened component, so containment can be judged by filesystem identity.
    """
    components = [part for part in path.split("/") if part not in ("", ".")]
    if ".." in components:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOT_UNUSABLE)
    flags = _directory_flags()
    descriptor = _guard(lambda: os.open("/", flags), ColdEvidenceStoreFailure.ROOT_UNUSABLE)
    owned = True
    try:
        if lineage is not None:
            lineage.append(_directory_identity(descriptor))
        for component in components:
            parent = descriptor
            owned = False
            try:
                descriptor = _open_at(
                    component, flags, parent, ColdEvidenceStoreFailure.ROOT_UNUSABLE
                )
            finally:
                _release(parent)
            owned = True
            if lineage is not None:
                lineage.append(_directory_identity(descriptor))
    except BaseException:
        if owned:
            _release(descriptor)
        raise
    return descriptor


class ColdAdmittedRoot:
    """An evidence root admitted by :func:`admit_evidence_root`; holds no descriptor."""

    __slots__ = ("lineage", "path", "realpath")

    path: str
    realpath: str
    lineage: tuple[tuple[int, int], ...]

    def __init__(
        self,
        path: str,
        realpath: str,
        *,
        token: object,
        lineage: tuple[tuple[int, int], ...] = (),
    ) -> None:
        """Create an admitted root; only the admission function may do so.

        Args:
            path: The admitted absolute root string, exactly as supplied.
            realpath: Its resolved real path.
            token: Private admission token.
            lineage: ``(st_dev, st_ino)`` of ``/`` and each component, root last.

        Raises:
            ColdEvidenceStoreError: If constructed outside admission.
        """
        if token is not _ADMISSION_TOKEN:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOT_UNUSABLE)
        self.path = path
        self.realpath = realpath
        self.lineage = lineage


def admit_evidence_root(root: str, *, protected_roots: tuple[str, ...] = ()) -> ColdAdmittedRoot:
    """Admit one absolute, symlink-free evidence root outside every protected root.

    Args:
        root: Absolute evidence-root path; checked before any resolution.
        protected_roots: Additional absolute roots that may never contain evidence.

    Returns:
        The admitted root.

    Raises:
        ColdEvidenceStoreError: If the platform, path, protection, or traversal fails.

    Protection is judged twice: by resolved-path containment, which also covers a
    declared protected root that does not exist, and by filesystem identity along the
    candidate's no-follow traversal, so an alias spelling (for example a case variant)
    that names an existing protected root or one of its descendants is refused.
    Same-uid substitution between these checks and later use remains a named residual.
    """
    if not _platform_is_supported():
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.PLATFORM_UNSUPPORTED)
    if type(root) is not str or not os.path.isabs(root):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOT_NOT_ABSOLUTE)
    protected = _protected_realpaths(protected_roots)
    real = _realpath(root, ColdEvidenceStoreFailure.ROOT_UNUSABLE)
    if any(_is_within(real, parent) for parent in protected):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOT_PROTECTED)
    identities = _protected_identities(protected)
    lineage: list[tuple[int, int]] = []
    descriptor = _open_absolute_directory(root, lineage)
    _release(descriptor)
    if identities.intersection(lineage):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOT_PROTECTED)
    return ColdAdmittedRoot(root, real, token=_ADMISSION_TOKEN, lineage=tuple(lineage))


# ----------------------------------------------------------------- identity v1

_V1Check = collections.abc.Callable[[object], bool]


def _is_str(value: object) -> bool:
    return type(value) is str


def _is_float(value: object) -> bool:
    return type(value) is float


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_bool(value: object) -> bool:
    return type(value) is bool


def _nullable(check: _V1Check) -> _V1Check:
    return lambda value: value is None or check(value)


def _one_of(*values: str) -> _V1Check:
    admitted = frozenset(values)
    return lambda value: type(value) is str and value in admitted


def _is_str_array(value: object) -> bool:
    return type(value) is list and all(
        type(item) is str for item in typing.cast(list[object], value)
    )


def _is_hex(length: int) -> _V1Check:
    return lambda value: (
        type(value) is str and len(value) == length and all(c in _HEX_DIGITS for c in value)
    )


_V1_TOP_LEVEL_STRINGS = (
    "run_id",
    "started_at_utc",
    "agent_version",
    "coffee_roaster_mcp_version",
    "python_version",
    "platform",
    "machine",
    "operating_system",
    "kernel",
    "boot_id",
    "pi_model",
    "pi_revision",
    "model_repo_id",
    "model_revision",
    "audio_device_identity",
    "serial_port_path",
    "pi_evidence_root",
    "laptop_evidence_root",
    "advisor_provider",
    "advisor_model",
    "advisor_prompt_version",
    "credential_env_var_name",
    "stimulus_block",
    "operator_host_notes",
    "operator_psu_notes",
    "operator_cooling_notes",
)
V1_TOP_LEVEL_SCALARS: dict[str, _V1Check] = {
    **{name: _is_str for name in _V1_TOP_LEVEL_STRINGS},
    "controller_tick_seconds": _is_float,
    "credential_present": _is_bool,
}
V1_RUNTIME_CONFIG: dict[str, _V1Check] = {
    "config_source": _nullable(_is_str),
    "roaster_driver": _is_str,
    "roaster_port": _nullable(_is_str),
    "roaster_baudrate": _is_int,
    "temperature_unit": _is_str,
    "command_interval_seconds": _is_float,
    "first_crack_mode": _is_str,
    "model_repo_id": _is_str,
    "model_precision": _is_str,
    "allow_manual_override": _is_bool,
    "log_dir": _is_str,
    "sample_interval_seconds": _is_float,
    "auto_t0_detection_enabled": _is_bool,
    "auto_t0_drop_threshold_c": _is_float,
}
V1_SERVER_INFO: dict[str, _V1Check] = {
    "product_name": _is_str,
    "package_name": _is_str,
    "version": _is_str,
    "transport": _is_str,
    "current_phase": _is_str,
    "roaster_driver": _is_str,
    "first_crack_mode": _is_str,
    "bootstrap_safe": _is_bool,
    "available_bootstrap_tools": _is_str_array,
    "started_at_utc": _is_str,
}
V1_DEVICE_CONFIG: dict[str, _V1Check] = {
    "serial_port": _nullable(_is_str),
    "roaster_driver": _nullable(_is_str),
    "audio_input_device": _nullable(_is_str),
    "recording_enabled": _nullable(_is_bool),
    "recording_autocapture": _nullable(_is_bool),
    "recording_devices": _nullable(_is_str_array),
    "fc_mode": _nullable(_one_of("disabled", "audio", "manual")),
    "fc_confidence_threshold": _nullable(_is_float),
    "auto_t0_detection_enabled": _nullable(_is_bool),
    "auto_t0_drop_threshold_c": _nullable(_is_float),
    "mcp_yaml_source_path": _nullable(_is_str),
    "ambient_mode": _nullable(_one_of("disabled", "yoctopuce")),
    "ambient_device": _nullable(_is_str),
    "ambient_poll_interval_seconds": _nullable(_is_float),
}
V1_BUILD_PROVENANCE: dict[str, _V1Check] = {
    "source_revision": _is_hex(40),
    "source_tree_dirty": _is_bool,
    "artefact_kind": _one_of("wheel", "sdist", "editable_source"),
    "artefact_sha256": _nullable(_is_hex(64)),
}
V1_EFFECTIVE_MCP_PROFILE: dict[str, _V1Check] = {
    "source_sha256": _is_hex(64),
    "source_byte_length": _is_int,
    "first_crack_onnx_threads": _is_int,
    "first_crack_min_positive_windows": _is_int,
    "first_crack_confirmation_window_seconds": _is_float,
    "first_crack_revision": _is_str,
    "audio_sample_rate": _is_int,
    "audio_window_seconds": _is_float,
    "audio_overlap": _is_float,
    "audio_hop_seconds": _nullable(_is_float),
    "session_ror_window_seconds": _is_int,
    "session_ror_min_sample_seconds": _is_int,
}
V1_MODEL_MANIFEST_ENTRY: dict[str, _V1Check] = {"relative_path": _is_str, "sha256": _is_str}
V1_TOLERANT_OBJECTS: dict[str, dict[str, _V1Check]] = {
    "runtime_config": V1_RUNTIME_CONFIG,
    "server_info": V1_SERVER_INFO,
}
V1_CLOSED_OBJECTS: dict[str, dict[str, _V1Check]] = {
    "device_config": V1_DEVICE_CONFIG,
    "build_provenance": V1_BUILD_PROVENANCE,
    "effective_mcp_profile": V1_EFFECTIVE_MCP_PROFILE,
}
V1_TOP_LEVEL_KEYS = frozenset(
    {*V1_TOP_LEVEL_SCALARS, *V1_TOLERANT_OBJECTS, *V1_CLOSED_OBJECTS, "model_manifest"}
)


class ColdRetainedIdentityV1(pydantic.BaseModel):
    """What a v1 identity envelope stored, read without current-package constants."""

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    run_id: str
    pi_evidence_root: str
    known: dict[str, pydantic.JsonValue]
    runtime_config_extras: dict[str, pydantic.JsonValue]
    server_info_extras: dict[str, pydantic.JsonValue]


def _check_closed_object(value: object, spec: dict[str, _V1Check]) -> dict[str, object]:
    """Require an exact-key object whose values pass exact-type checks."""
    if type(value) is not dict:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    mapping = typing.cast(dict[str, object], value)
    if set(mapping) != set(spec) or not all(spec[key](mapping[key]) for key in spec):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    return mapping


def _split_tolerant_object(
    value: object, spec: dict[str, _V1Check]
) -> tuple[dict[str, object], dict[str, object]]:
    """Check known keys exactly and retain bounded unknown keys losslessly."""
    if type(value) is not dict:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    mapping = typing.cast(dict[str, object], value)
    if not all(key in mapping and spec[key](mapping[key]) for key in spec):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    extras = {key: item for key, item in mapping.items() if key not in spec}
    if len(extras) > MAX_IDENTITY_EXTRA_KEYS:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    return {key: mapping[key] for key in spec}, extras


def read_identity_v1(envelope: ColdSealedEnvelope) -> ColdRetainedIdentityV1:
    """Read a retained identity envelope through the frozen v1 type tree.

    It never parses through ``ColdRunIdentity``, tolerant mirrors, or current
    package constants, and never raises because a value differs from them.

    Args:
        envelope: A digest-verified identity envelope.

    Returns:
        The retained v1 identity with tolerant-origin extras kept losslessly.

    Raises:
        ColdEvidenceStoreError: If the envelope is not a lossless v1 identity.
    """
    if envelope.kind is not ColdEnvelopeKind.IDENTITY:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    if envelope.schema_version not in {1}:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.SCHEMA_VERSION_UNKNOWN)
    document = load_strict_json(
        envelope.canonical_json.encode("utf-8"),
        malformed=ColdEvidenceStoreFailure.IDENTITY_NOT_V1,
    )
    if type(document) is not dict:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    top = typing.cast(dict[str, object], document)
    if frozenset(top) != V1_TOP_LEVEL_KEYS:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    if not all(check(top[name]) for name, check in V1_TOP_LEVEL_SCALARS.items()):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    known: dict[str, object] = {name: top[name] for name in V1_TOP_LEVEL_SCALARS}
    extras: dict[str, dict[str, object]] = {}
    for name, spec in V1_TOLERANT_OBJECTS.items():
        known[name], extras[name] = _split_tolerant_object(top[name], spec)
    for name, spec in V1_CLOSED_OBJECTS.items():
        known[name] = _check_closed_object(top[name], spec)
    raw_manifest = top["model_manifest"]
    if type(raw_manifest) is not list:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    manifest = typing.cast(list[object], raw_manifest)
    if not 1 <= len(manifest) <= MAX_MODEL_MANIFEST_ENTRIES:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    known["model_manifest"] = [
        _check_closed_object(entry, V1_MODEL_MANIFEST_ENTRY) for entry in manifest
    ]
    reassembled = dict(known)
    for name, extra in extras.items():
        reassembled[name] = {**typing.cast(dict[str, object], known[name]), **extra}
    if canonical_json(reassembled) != envelope.canonical_json:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_NOT_V1)
    return _guard(
        lambda: ColdRetainedIdentityV1(
            run_id=typing.cast(str, top["run_id"]),
            pi_evidence_root=typing.cast(str, top["pi_evidence_root"]),
            known=typing.cast(dict[str, pydantic.JsonValue], known),
            runtime_config_extras=typing.cast(
                dict[str, pydantic.JsonValue], extras["runtime_config"]
            ),
            server_info_extras=typing.cast(dict[str, pydantic.JsonValue], extras["server_info"]),
        ),
        ColdEvidenceStoreFailure.IDENTITY_NOT_V1,
    )


# ------------------------------------------------------------ finalisation index


class ColdFinalisationIndex(pydantic.BaseModel):
    """The five finalisation scalars derived solely from a finalisation envelope."""

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid", strict=True)

    session_id: str
    status: ColdFinalisationStatus
    clean: bool
    observed_command_streaming_required: bool | None
    applied_branch: ColdCapabilityBranch | None


def derive_finalisation_index(result: SessionFinalisationResult) -> ColdFinalisationIndex:
    """Derive the finalisation index; the only producer of those five scalars.

    ``clean`` copies ``result.clean`` exactly; this computes no verdict.

    Args:
        result: A finalisation result re-parsed from its retained envelope.

    Returns:
        The derived index; capability fields are ``None`` without trusted evidence.
    """
    observed = finalisation_command_streaming_observation(result)
    branch: ColdCapabilityBranch | None
    if observed is None:
        branch = None
    elif observed:
        branch = ColdCapabilityBranch.STREAMING
    else:
        branch = ColdCapabilityBranch.NON_STREAMING
    return ColdFinalisationIndex(
        session_id=result.session_id,
        status=ColdFinalisationStatus(result.status),
        clean=result.clean,
        observed_command_streaming_required=observed,
        applied_branch=branch,
    )


def parse_finalisation_envelope(envelope: ColdSealedEnvelope) -> SessionFinalisationResult:
    """Strictly re-parse the MCP finalisation result retained in an envelope.

    Args:
        envelope: A finalisation envelope.

    Returns:
        The strictly parsed finalisation result.

    Raises:
        ColdEvidenceStoreError: If the envelope does not hold a valid result.
    """
    return _guard(
        lambda: SessionFinalisationResult.model_validate_json(envelope.canonical_json),
        ColdEvidenceStoreFailure.FINALISATION_INDEX_MISMATCHED,
        (ValueError, RecursionError),
    )


# ------------------------------------------------------------------- binding


class ColdBindingState:
    """Per-run binding state shared by the writer and the reader."""

    __slots__ = ("_headers", "_identities", "run_id")

    run_id: str
    _headers: dict[ColdPhaseKind, ColdRunHeader]
    _identities: dict[ColdPhaseKind, ColdRetainedIdentityV1]

    def __init__(self, run_id: str) -> None:
        """Create empty binding state for one run.

        Args:
            run_id: The run identifier every record must carry.
        """
        self.run_id = run_id
        self._headers = {}
        self._identities = {}

    @property
    def headers(self) -> tuple[tuple[ColdRunHeader, ColdRetainedIdentityV1], ...]:
        """Bound headers and their v1 identities, in phase order."""
        return tuple(
            (self._headers[phase], self._identities[phase])
            for phase in ColdPhaseKind
            if phase in self._headers
        )


def check_record_binding(
    state: ColdBindingState, record: ColdEvidenceRecord, *, writer_root: str | None
) -> None:
    """Bind one validated record to its run and phase header; the single binding site.

    Args:
        state: Mutable binding state; updated only when a header binds.
        record: A record snapshot returned by ``validate_record``.
        writer_root: The admitted root string when writing; ``None`` when reading.

    Raises:
        ColdEvidenceStoreError: If run, header, digest, or finalisation index binding fails.
    """
    if record.run_id != state.run_id:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.RUN_ID_MISMATCHED)
    if type(record) is ColdRunHeader:
        if record.phase in state._headers:  # pyright: ignore[reportPrivateUsage]
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.HEADER_DUPLICATED)
        if record.identity.sha256 != record.identity_sha256:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_DIGEST_MISMATCHED)
        identity = read_identity_v1(record.identity)
        if identity.run_id != record.run_id:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.HEADER_BINDING_MISMATCHED)
        if writer_root is not None and identity.pi_evidence_root != writer_root:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.HEADER_BINDING_MISMATCHED)
        state._headers[record.phase] = record  # pyright: ignore[reportPrivateUsage]
        state._identities[record.phase] = identity  # pyright: ignore[reportPrivateUsage]
        return
    header = state._headers.get(record.phase)  # pyright: ignore[reportPrivateUsage]
    if header is None:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.HEADER_MISSING)
    if record.identity_sha256 != header.identity_sha256:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.IDENTITY_DIGEST_MISMATCHED)
    if type(record) is ColdFinalisationRecord:
        index = derive_finalisation_index(parse_finalisation_envelope(record.envelope))
        if (
            record.session_id != index.session_id
            or record.status is not index.status
            or record.clean is not index.clean
            or record.observed_command_streaming_required
            is not index.observed_command_streaming_required
            or record.applied_branch is not index.applied_branch
        ):
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.FINALISATION_INDEX_MISMATCHED)


# ------------------------------------------------------------ tree primitives


class _NodeStat(typing.NamedTuple):
    """Identity fields of one node, always built from one ``stat_result`` shape."""

    is_directory: bool
    dev: int
    ino: int
    mode: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _node_stat(result: os.stat_result) -> _NodeStat:
    """Project an ``lstat`` or ``fstat`` result into the one compared shape."""
    return _NodeStat(
        is_directory=stat.S_ISDIR(result.st_mode),
        dev=result.st_dev,
        ino=result.st_ino,
        mode=result.st_mode,
        nlink=result.st_nlink,
        size=result.st_size,
        mtime_ns=result.st_mtime_ns,
        ctime_ns=result.st_ctime_ns,
    )


class _TreeRules(typing.NamedTuple):
    """Enumeration rules and failure mapping for sealing or verifying."""

    seal: bool
    invalid: ColdEvidenceStoreFailure
    not_regular: ColdEvidenceStoreFailure
    limit: ColdEvidenceStoreFailure


_SEAL_RULES = _TreeRules(
    seal=True,
    invalid=ColdEvidenceStoreFailure.SEAL_TREE_INVALID,
    not_regular=ColdEvidenceStoreFailure.SEAL_TREE_INVALID,
    limit=ColdEvidenceStoreFailure.SEAL_LIMIT_EXCEEDED,
)
_VERIFY_RULES = _TreeRules(
    seal=False,
    invalid=ColdEvidenceStoreFailure.INVENTORY_MISMATCHED,
    not_regular=ColdEvidenceStoreFailure.FILE_NOT_REGULAR,
    limit=ColdEvidenceStoreFailure.INVENTORY_MISMATCHED,
)
_ROOT_NODE = ""


def _between_enumeration_passes() -> None:
    """Test seam between the two enumeration passes; intentionally inert."""


def _after_first_identity_read(_relative_path: str) -> None:
    """Test seam between a file's two identity reads; intentionally inert."""


def _read_chunk(descriptor: int, size: int) -> bytes:
    """Read one chunk from a descriptor."""
    return os.read(descriptor, size)


def _write_chunk(descriptor: int, data: memoryview) -> int:
    """Write one chunk to a descriptor."""
    return os.write(descriptor, data)


def _open_node_directory(
    run_fd: int, segments: collections.abc.Sequence[str], nodes: dict[str, _NodeStat]
) -> int:
    """Open a recorded directory by no-follow descriptor walk, checking each identity."""
    flags = _directory_flags()
    changed = ColdEvidenceStoreFailure.SEAL_TREE_CHANGED
    descriptor = _open_at(".", flags, run_fd, changed)
    owned = True
    walked: list[str] = []
    try:
        for segment in segments:
            parent = descriptor
            owned = False
            try:
                descriptor = _open_at(segment, flags, parent, changed)
            finally:
                _release(parent)
            owned = True
            walked.append(segment)
        if _fstat(descriptor, changed) != nodes.get("/".join(walked)):
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.SEAL_TREE_CHANGED)
    except BaseException:
        if owned:
            _release(descriptor)
        raise
    return descriptor


def _enumerate(run_fd: int, rules: _TreeRules) -> dict[str, _NodeStat]:
    """Enumerate a run tree by descriptor, returning every node's identity."""
    nodes: dict[str, _NodeStat] = {_ROOT_NODE: _fstat(run_fd, rules.invalid)}
    pending: list[tuple[str, ...]] = [()]
    while pending:
        segments = pending.pop()
        directory = _open_node_directory(run_fd, segments, nodes)
        try:
            with _scan_directory(directory, rules.invalid) as scan:
                _scan_entries(scan, directory, segments, nodes, pending, rules)
        finally:
            _release(directory)
    return nodes


def _scan_entries(
    scan: collections.abc.Iterator["os.DirEntry[str]"],
    directory: int,
    segments: tuple[str, ...],
    nodes: dict[str, _NodeStat],
    pending: list[tuple[str, ...]],
    rules: _TreeRules,
) -> None:
    """Record one directory's entries incrementally, stopping at the first bound breach."""
    while True:
        if len(nodes) > 2 * MAX_MANIFEST_ENTRIES + 2:
            raise ColdEvidenceStoreError(rules.limit)
        name = _next_name(scan, rules.invalid)
        if name is None:
            return
        child = (*segments, name)
        relative = "/".join(child)
        if not _segment_is_valid(name):
            raise ColdEvidenceStoreError(rules.invalid)
        if len(child) > MAX_PATH_SEGMENTS:
            raise ColdEvidenceStoreError(rules.limit)
        node = _lstat_at(name, directory, ColdEvidenceStoreFailure.SEAL_TREE_CHANGED)
        if node.is_directory:
            if rules.seal and stat.S_IMODE(node.mode) != 0o700:
                raise ColdEvidenceStoreError(rules.invalid)
            pending.append(child)
        elif stat.S_ISREG(node.mode):
            if rules.seal and (
                stat.S_IMODE(node.mode) != 0o600
                or node.nlink != 1
                or relative in _RESERVED_TOP_LEVEL_NAMES
            ):
                raise ColdEvidenceStoreError(rules.invalid)
        else:
            raise ColdEvidenceStoreError(rules.not_regular)
        nodes[relative] = node


def _files_and_orphans(nodes: dict[str, _NodeStat]) -> tuple[list[str], list[str]]:
    """Return regular-file paths and directories that are ancestors of no file."""
    files = [path for path, node in nodes.items() if path and not node.is_directory]
    ancestors = {
        "/".join(path.split("/")[:depth])
        for path in files
        for depth in range(1, path.count("/") + 1)
    }
    orphans = [
        path for path, node in nodes.items() if path and node.is_directory and path not in ancestors
    ]
    return files, orphans


def _read_node(
    run_fd: int,
    relative_path: str,
    nodes: dict[str, _NodeStat],
    *,
    retain: bool,
    sink: collections.abc.Callable[[bytes], None] | None = None,
) -> tuple[str, bytes | None]:
    """Hash one recorded file under before/after identity checks and complete reads.

    ``sink`` receives each chunk as it is read and may refuse early by raising.
    """
    segments = relative_path.split("/")
    expected = nodes[relative_path]
    parent = _open_node_directory(run_fd, segments[:-1], nodes)
    try:
        descriptor = _open_at(
            segments[-1], _file_read_flags(), parent, ColdEvidenceStoreFailure.FILE_CHANGED
        )
    finally:
        _release(parent)
    try:
        before = _fstat(descriptor, ColdEvidenceStoreFailure.FILE_CHANGED)
        if before != expected:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.FILE_CHANGED)
        _after_first_identity_read(relative_path)
        digest = hashlib.sha256()
        retained = bytearray() if retain else None
        total = 0
        while total <= expected.size:
            want = min(_READ_CHUNK_BYTES, expected.size - total + 1)
            chunk = _guard(
                lambda: _read_chunk(descriptor, want),  # noqa: B023 - invoked immediately.
                ColdEvidenceStoreFailure.FILE_READ_INCOMPLETE,
            )
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            if retained is not None:
                retained.extend(chunk)
            if sink is not None:
                sink(chunk)
        if total != expected.size:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.FILE_READ_INCOMPLETE)
        after = _fstat(descriptor, ColdEvidenceStoreFailure.FILE_CHANGED)
        if after != before:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.FILE_CHANGED)
    finally:
        _release(descriptor)
    return digest.hexdigest(), bytes(retained) if retained is not None else None


def _require_run_id(run_id: object) -> str:
    """Refuse any run id outside the record grammar before any filesystem access."""
    if not run_id_is_valid(run_id):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.RUN_ID_MISMATCHED)
    return typing.cast(str, run_id)


def _open_run_directory(
    root: ColdAdmittedRoot, run_id: str, failure: ColdEvidenceStoreFailure
) -> int:
    """Open one run directory beneath an admitted root by no-follow descriptor walk."""
    _require_run_id(run_id)
    root_fd = _open_absolute_directory(root.path)
    try:
        return _open_at(run_id, _directory_flags(), root_fd, failure)
    finally:
        _release(root_fd)


# ------------------------------------------------------------------ manifest


class ColdManifestBinding(pydantic.BaseModel):
    """One phase's identity digest bound into a manifest."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    phase: ColdPhaseKind
    identity_sha256: str


class ColdManifestEntry(pydantic.BaseModel):
    """One retained file's relative path, size, and SHA-256."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    relative_path: str
    size_bytes: int
    sha256: str


class ColdEvidenceManifest(pydantic.BaseModel):
    """Closed canonical manifest of one sealed evidence run."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    schema_version: typing.Literal[1]
    run_id: str
    identity_bindings: tuple[ColdManifestBinding, ...]
    entries: tuple[ColdManifestEntry, ...]

    @pydantic.model_validator(mode="after")
    def _require_closed_manifest(self) -> typing.Self:
        """Require bounded, unique, ordered bindings and entries."""
        paths = [entry.relative_path for entry in self.entries]
        if not all(_entry_path_is_valid(path) for path in paths):
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ENTRY_PATH_INVALID)
        if len(set(paths)) != len(paths):
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ENTRY_DUPLICATED)
        encoded = [path.encode("utf-8") for path in paths]
        phase_order = list(ColdPhaseKind)
        binding_positions = [phase_order.index(binding.phase) for binding in self.identity_bindings]
        if (
            not run_id_is_valid(self.run_id)
            or not 1 <= len(self.identity_bindings) <= len(phase_order)
            or binding_positions != sorted(set(binding_positions))
            or not all(_is_sha256_hex(b.identity_sha256) for b in self.identity_bindings)
            or not 1 <= len(self.entries) <= MAX_MANIFEST_ENTRIES
            or encoded != sorted(encoded)
            or not all(0 <= e.size_bytes <= MAX_EVIDENCE_FILE_BYTES for e in self.entries)
            or not all(_is_sha256_hex(e.sha256) for e in self.entries)
        ):
            raise ValueError("manifest is not closed and canonical")
        return self


def render_manifest_sidecar(manifest: ColdEvidenceManifest) -> bytes:
    """Render the ``sha256sum -c`` compatible sidecar for one manifest.

    Args:
        manifest: The manifest to render.

    Returns:
        One ``<sha256>  <relative_path>`` LF-terminated line per entry, in order.
    """
    return "".join(f"{entry.sha256}  {entry.relative_path}\n" for entry in manifest.entries).encode(
        "utf-8"
    )


def _manifest_bytes(manifest: ColdEvidenceManifest) -> bytes:
    """Return a manifest's canonical bytes, without a trailing newline."""
    return canonical_json(manifest.model_dump(mode="json")).encode("utf-8")


class ColdSealedRun(pydantic.BaseModel):
    """Integrity facts returned by a successful seal; not an outcome."""

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    run_id: str
    manifest_sha256: str
    entry_count: int
    identity_bindings: tuple[ColdManifestBinding, ...]


class ColdVerifiedManifest(pydantic.BaseModel):
    """Integrity facts of two verified retained copies; not an outcome."""

    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")

    run_id: str
    manifest_sha256: str
    entry_count: int
    identity_bindings: tuple[ColdManifestBinding, ...]


# -------------------------------------------------------------------- writer


def _require_owned(descriptor: int, *, directory: bool, mode: int) -> None:
    """Require a descriptor's type, exact permission bits, and effective-uid owner."""
    result = os.fstat(descriptor)
    kind_ok = stat.S_ISDIR(result.st_mode) if directory else stat.S_ISREG(result.st_mode)
    if not kind_ok or stat.S_IMODE(result.st_mode) != mode or result.st_uid != os.geteuid():
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.OWNERSHIP_OR_MODE_MISMATCH)


def _require_owned_after_chmod(descriptor: int) -> None:
    """Set a run directory's mode by descriptor, then verify type, mode, and owner."""
    os.fchmod(descriptor, 0o700)
    _require_owned(descriptor, directory=True, mode=0o700)


def _make_directory(parent: int, name: str) -> int:
    """Create, open, chmod, and verify one private directory beneath a descriptor."""
    os.mkdir(name, 0o700, dir_fd=parent)
    descriptor = os.open(name, _directory_flags(), dir_fd=parent)
    try:
        os.fchmod(descriptor, 0o700)
        _require_owned(descriptor, directory=True, mode=0o700)
        os.fsync(parent)
    except BaseException:
        _release(descriptor)
        raise
    return descriptor


def _create_file(parent: int, name: str, *, append: bool) -> int:
    """Exclusively create, chmod, and verify one private regular file."""
    descriptor = os.open(name, _file_create_flags(append=append), 0o600, dir_fd=parent)
    try:
        os.fchmod(descriptor, 0o600)
        _require_owned(descriptor, directory=False, mode=0o600)
        os.fsync(parent)
    except BaseException:
        _release(descriptor)
        raise
    return descriptor


def _write_all(descriptor: int, data: bytes) -> None:
    """Write all bytes, looping over partial writes, then fsync."""
    view = memoryview(data)
    while view:
        written = _write_chunk(descriptor, view)
        if written <= 0:
            raise OSError("incomplete write")
        view = view[written:]
    os.fsync(descriptor)


class ColdEvidenceWriter:
    """Descriptor-holding append-only writer for one private evidence run."""

    def __init__(self, *, root_path: str, run_id: str, run_fd: int, token: object) -> None:
        """Create a writer; only :func:`open_run` may do so.

        Args:
            root_path: The admitted root string, bound against header identities.
            run_id: The run identifier.
            run_fd: The owned run-directory descriptor.
            token: Private admission token.

        Raises:
            ColdEvidenceStoreError: If constructed outside :func:`open_run`.
        """
        if token is not _ADMISSION_TOKEN:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOT_UNUSABLE)
        self._root_path = root_path
        self._run_id = run_id
        self._run_fd: int | None = run_fd
        self._records_fd: int | None = None
        self._phase_fds: dict[ColdPhaseKind, int] = {}
        self._stream_fds: dict[tuple[ColdPhaseKind, str], int] = {}
        self._state = ColdBindingState(run_id)
        self._poisoned = False
        self._sealed = False

    def _require_writable(self) -> int:
        """Return the run descriptor, refusing a sealed or poisoned writer."""
        if self._sealed:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.WRITER_SEALED)
        if self._poisoned or self._run_fd is None:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.WRITER_POISONED)
        return self._run_fd

    def _detach_streams(self) -> list[int]:
        """Remove every stream and layout descriptor from ownership, returning them."""
        detached = [*self._stream_fds.values(), *self._phase_fds.values()]
        if self._records_fd is not None:
            detached.append(self._records_fd)
        self._stream_fds = {}
        self._phase_fds = {}
        self._records_fd = None
        return detached

    def _detach_all(self) -> list[int]:
        """Remove every owned descriptor, the run directory's last, returning them."""
        detached = self._detach_streams()
        if self._run_fd is not None:
            detached.append(self._run_fd)
            self._run_fd = None
        return detached

    def _abandon(self) -> None:
        """Poison, then release every owned descriptor once; never raises OSError."""
        self._poisoned = True
        _release_all(self._detach_all())

    def close(self) -> None:
        """Release every descriptor once; an unsealed writer becomes unusable.

        Ownership is detached before any release, so a repeated call is a no-op and
        never retries a descriptor number.

        Raises:
            ColdEvidenceStoreError: ``WRITE_FAILED`` if any release reported failure.
        """
        if not self._sealed:
            self._poisoned = True
        if not _release_all(self._detach_all()):
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.WRITE_FAILED)

    def _stream_fd(self, run_fd: int, phase: ColdPhaseKind, stream: str) -> int:
        """Return one stream descriptor, creating its layout lazily."""
        key = (phase, stream)
        existing = self._stream_fds.get(key)
        if existing is not None:
            return existing
        if self._records_fd is None:
            self._records_fd = _make_directory(run_fd, "records")
        phase_fd = self._phase_fds.get(phase)
        if phase_fd is None:
            phase_fd = _make_directory(self._records_fd, phase.value)
            self._phase_fds[phase] = phase_fd
        descriptor = _create_file(phase_fd, f"{stream}.jsonl", append=True)
        self._stream_fds[key] = descriptor
        return descriptor

    def append(self, record: ColdEvidenceRecord) -> None:
        """Validate, bind, and durably append one canonical record line.

        Args:
            record: One in-process evidence record.

        Raises:
            ColdEvidenceError: If schema validation fails (propagated unchanged).
            ColdEvidenceStoreError: If binding fails, or a write fails (then poisoned).
        """
        run_fd = self._require_writable()
        snapshot = validate_record(record)
        try:
            check_record_binding(self._state, snapshot, writer_root=self._root_path)
        except (ColdEvidenceStoreError, ColdEvidenceError):
            raise
        except BaseException:
            self._abandon()
            raise
        failed = False
        try:
            line = (canonical_json(snapshot.model_dump(mode="json")) + "\n").encode("utf-8")
            _write_all(self._stream_fd(run_fd, snapshot.phase, snapshot.stream), line)
        except (OSError, ValueError, ColdEvidenceStoreError):
            failed = True
        except BaseException:
            self._abandon()
            raise
        if failed:
            self._abandon()
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.WRITE_FAILED)

    def seal(self) -> ColdSealedRun:
        """Seal the run: two-pass enumeration, hashing, and the manifest pair.

        Returns:
            The manifest digest (over bytes re-read from disk) and integrity facts.

        Raises:
            ColdEvidenceStoreError: If the writer is unusable or sealing fails (then poisoned).
        """
        run_fd = self._require_writable()
        sealed: ColdSealedRun | None = None
        try:
            if _release_all(self._detach_streams()):
                sealed = self._seal(run_fd)
        except BaseException:
            self._abandon()
            raise
        if sealed is None:
            self._abandon()
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.WRITE_FAILED)
        self._sealed = True
        _release_all(self._detach_all())
        return sealed

    def _seal(self, run_fd: int) -> ColdSealedRun:
        """Perform the seal against an owned run descriptor."""
        headers = self._state.headers
        if not headers:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.SEAL_TREE_INVALID)
        first = _enumerate(run_fd, _SEAL_RULES)
        files, orphans = _files_and_orphans(first)
        if orphans:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.SEAL_TREE_INVALID)
        if len(files) > MAX_MANIFEST_ENTRIES or any(
            first[path].size > MAX_EVIDENCE_FILE_BYTES for path in files
        ):
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.SEAL_LIMIT_EXCEEDED)
        for header, _identity in headers:
            if f"records/{header.phase.value}/header.jsonl" not in first:
                raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.SEAL_TREE_INVALID)
        files.sort(key=lambda path: path.encode("utf-8"))
        entries = tuple(
            ColdManifestEntry(
                relative_path=path,
                size_bytes=first[path].size,
                sha256=_read_node(run_fd, path, first, retain=False)[0],
            )
            for path in files
        )
        _between_enumeration_passes()
        if _enumerate(run_fd, _SEAL_RULES) != first:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.SEAL_TREE_CHANGED)
        bindings = tuple(
            ColdManifestBinding(phase=header.phase, identity_sha256=header.identity_sha256)
            for header, _identity in headers
        )
        manifest = _guard(
            lambda: ColdEvidenceManifest(
                schema_version=1, run_id=self._run_id, identity_bindings=bindings, entries=entries
            ),
            ColdEvidenceStoreFailure.SEAL_TREE_INVALID,
        )
        manifest_bytes = _manifest_bytes(manifest)
        sidecar_bytes = render_manifest_sidecar(manifest)
        if max(len(manifest_bytes), len(sidecar_bytes)) > MAX_MANIFEST_BYTES:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.SEAL_LIMIT_EXCEEDED)
        failed = False
        try:
            for name, data in (
                (MANIFEST_JSON_NAME, manifest_bytes),
                (MANIFEST_SIDECAR_NAME, sidecar_bytes),
            ):
                descriptor = _create_file(run_fd, name, append=False)
                try:
                    _write_all(descriptor, data)
                except BaseException:
                    _release(descriptor)
                    raise
                if not _release(descriptor):
                    raise OSError("artefact close failed")
            os.fsync(run_fd)
        except (OSError, ValueError, ColdEvidenceStoreError):
            failed = True
        if failed:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.WRITE_FAILED)
        reread = _read_small_file(run_fd, MANIFEST_JSON_NAME)
        if reread != manifest_bytes:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.WRITE_FAILED)
        return ColdSealedRun(
            run_id=self._run_id,
            manifest_sha256=hashlib.sha256(reread).hexdigest(),
            entry_count=len(entries),
            identity_bindings=bindings,
        )


def _read_small_file(run_fd: int, name: str) -> bytes:
    """Re-read one top-level manifest file by descriptor with complete-read checks."""
    nodes = {
        _ROOT_NODE: _fstat(run_fd, ColdEvidenceStoreFailure.WRITE_FAILED),
        name: _lstat_at(name, run_fd, ColdEvidenceStoreFailure.WRITE_FAILED),
    }
    data = _read_node(run_fd, name, nodes, retain=True)[1]
    return data if data is not None else b""


def open_run(root: ColdAdmittedRoot, run_id: str) -> ColdEvidenceWriter:
    """Create one new private run directory beneath an admitted root.

    Args:
        root: The admitted evidence root.
        run_id: A run identifier matching the record schema grammar.

    Returns:
        A writer holding the run-directory descriptor; nothing else is created yet.

    Raises:
        ColdEvidenceStoreError: If the run id, root, directory, owner, or mode fails.
    """
    if type(root) is not ColdAdmittedRoot:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOT_UNUSABLE)
    if not run_id_is_valid(run_id):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.RUN_ID_MISMATCHED)
    root_fd = _open_absolute_directory(root.path)
    try:
        failure: ColdEvidenceStoreFailure | None = None
        try:
            os.mkdir(run_id, 0o700, dir_fd=root_fd)
        except FileExistsError:
            failure = ColdEvidenceStoreFailure.RUN_DIR_EXISTS
        except OSError:
            failure = ColdEvidenceStoreFailure.ROOT_UNUSABLE
        if failure is not None:
            raise ColdEvidenceStoreError(failure)
        # Sync the new entry into the admitted root before anything else relies on it.
        # A failure keeps the created directory for diagnosis; nothing is removed.
        _guard(lambda: os.fsync(root_fd), ColdEvidenceStoreFailure.ROOT_UNUSABLE)
        run_fd = _open_at(
            run_id, _directory_flags(), root_fd, ColdEvidenceStoreFailure.ROOT_UNUSABLE
        )
    finally:
        _release(root_fd)
    try:
        _guard(
            lambda: _require_owned_after_chmod(run_fd),
            ColdEvidenceStoreFailure.OWNERSHIP_OR_MODE_MISMATCH,
        )
    except BaseException:
        _release(run_fd)
        raise
    return ColdEvidenceWriter(
        root_path=root.path, run_id=run_id, run_fd=run_fd, token=_ADMISSION_TOKEN
    )


# ---------------------------------------------------------------- verification


class ColdVerifiedTree:
    """One verified retained tree: its manifest and verified node identities."""

    __slots__ = ("_nodes", "manifest", "manifest_bytes", "manifest_sha256", "root", "sidecar_bytes")

    root: ColdAdmittedRoot
    manifest: ColdEvidenceManifest
    manifest_bytes: bytes
    sidecar_bytes: bytes
    manifest_sha256: str
    _nodes: dict[str, _NodeStat]

    def __init__(
        self,
        *,
        root: ColdAdmittedRoot,
        manifest: ColdEvidenceManifest,
        manifest_bytes: bytes,
        sidecar_bytes: bytes,
        nodes: dict[str, _NodeStat],
        token: object,
    ) -> None:
        """Create a verified tree; only :func:`verify_retained_tree` may do so.

        Args:
            root: The admitted root holding the run directory.
            manifest: The verified manifest.
            manifest_bytes: Exact ``manifest.json`` bytes.
            sidecar_bytes: Exact ``manifest.sha256`` bytes.
            nodes: Verified node identities.
            token: Private admission token.

        Raises:
            ColdEvidenceStoreError: If constructed outside verification.
        """
        if token is not _ADMISSION_TOKEN:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOT_UNUSABLE)
        self.root = root
        self.manifest = manifest
        self.manifest_bytes = manifest_bytes
        self.sidecar_bytes = sidecar_bytes
        self.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        self._nodes = nodes


def _verify_admitted_tree(
    root: ColdAdmittedRoot, *, run_id: str, expected_manifest_sha256: str
) -> ColdVerifiedTree:
    """Verify one admitted tree against one expected manifest digest; read-only."""
    if not _is_sha256_hex(expected_manifest_sha256):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.MANIFEST_DIGEST_MISMATCHED)
    run_fd = _open_run_directory(root, run_id, ColdEvidenceStoreFailure.INVENTORY_MISMATCHED)
    try:
        first = _enumerate(run_fd, _VERIFY_RULES)
        for name in (MANIFEST_JSON_NAME, MANIFEST_SIDECAR_NAME):
            node = first.get(name)
            if node is None or node.is_directory or node.size > MAX_MANIFEST_BYTES:
                raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.MANIFEST_MALFORMED)
        manifest_digest, manifest_bytes = _read_node(run_fd, MANIFEST_JSON_NAME, first, retain=True)
        if manifest_digest != expected_manifest_sha256 or manifest_bytes is None:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.MANIFEST_DIGEST_MISMATCHED)
        load_strict_json(manifest_bytes, malformed=ColdEvidenceStoreFailure.MANIFEST_MALFORMED)
        manifest = _guard(
            lambda: ColdEvidenceManifest.model_validate_json(manifest_bytes, strict=True),
            ColdEvidenceStoreFailure.MANIFEST_MALFORMED,
            (ValueError, RecursionError),
        )
        if _manifest_bytes(manifest) != manifest_bytes:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.MANIFEST_MALFORMED)
        if manifest.run_id != run_id:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.RUN_ID_MISMATCHED)
        sidecar_bytes = _read_node(run_fd, MANIFEST_SIDECAR_NAME, first, retain=True)[1] or b""
        if sidecar_bytes != render_manifest_sidecar(manifest):
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.SIDECAR_INCONSISTENT)
        files, orphans = _files_and_orphans(first)
        inventory = {path for path in files if path not in _RESERVED_TOP_LEVEL_NAMES}
        entries = {entry.relative_path: entry for entry in manifest.entries}
        if orphans or inventory != set(entries):
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.INVENTORY_MISMATCHED)
        for path, entry in entries.items():
            if first[path].size != entry.size_bytes:
                raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.FILE_DIGEST_MISMATCHED)
            if _read_node(run_fd, path, first, retain=False)[0] != entry.sha256:
                raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.FILE_DIGEST_MISMATCHED)
        _between_enumeration_passes()
        if _enumerate(run_fd, _VERIFY_RULES) != first:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.SEAL_TREE_CHANGED)
    finally:
        _release(run_fd)
    return ColdVerifiedTree(
        root=root,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        sidecar_bytes=sidecar_bytes,
        nodes=first,
        token=_ADMISSION_TOKEN,
    )


def verify_retained_tree(
    root: str,
    *,
    run_id: str,
    expected_manifest_sha256: str,
    protected_roots: tuple[str, ...] = (),
) -> ColdVerifiedTree:
    """Admit and verify one retained tree against its expected manifest digest; read-only.

    Trust boundary: ``expected_manifest_sha256`` must be the externally recorded
    ``ColdSealedRun.manifest_sha256`` returned by a successful seal.  It must never be
    derived from the candidate tree, its ``manifest.json`` or its sidecar, which would
    verify a tree against itself.  A seal that fails after creating manifest artefacts
    can leave internally consistent bytes but returns no digest, so such a tree has no
    trusted receipt.  None of this is a filesystem transaction.

    Args:
        root: Absolute evidence root holding the run directory.
        run_id: The run identifier.
        expected_manifest_sha256: The recorded ``manifest.json`` digest.
        protected_roots: Additional absolute roots evidence may never occupy.

    Returns:
        The verified tree.

    Raises:
        ColdEvidenceStoreError: If admission or any integrity check fails.
    """
    _require_run_id(run_id)
    admitted = admit_evidence_root(root, protected_roots=protected_roots)
    return _verify_admitted_tree(
        admitted, run_id=run_id, expected_manifest_sha256=expected_manifest_sha256
    )


def _reread_entry(
    tree: ColdVerifiedTree,
    relative_path: str,
    *,
    retain: bool,
    sink: collections.abc.Callable[[bytes], None] | None,
) -> bytes | None:
    """Re-read one verified entry under identity checks and require its manifest digest."""
    entry = next(
        (item for item in tree.manifest.entries if item.relative_path == relative_path), None
    )
    if entry is None:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ENTRY_PATH_INVALID)
    nodes = tree._nodes  # pyright: ignore[reportPrivateUsage]
    run_fd = _open_run_directory(
        tree.root, tree.manifest.run_id, ColdEvidenceStoreFailure.FILE_CHANGED
    )
    try:
        if _fstat(run_fd, ColdEvidenceStoreFailure.FILE_CHANGED) != nodes[_ROOT_NODE]:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.FILE_CHANGED)
        digest, data = _read_node(run_fd, relative_path, nodes, retain=retain, sink=sink)
    finally:
        _release(run_fd)
    if digest != entry.sha256:
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.FILE_DIGEST_MISMATCHED)
    return data


def read_verified_file(tree: ColdVerifiedTree, relative_path: str) -> bytes:
    """Re-read one verified entry under identity checks and require its manifest digest.

    Args:
        tree: A verified tree.
        relative_path: One manifest entry path.

    Returns:
        Exactly the verified bytes.

    Raises:
        ColdEvidenceStoreError: If the file changed since verification or its digest differs.
    """
    return _reread_entry(tree, relative_path, retain=True, sink=None) or b""


class _LineFramer:
    """Frame LF-terminated lines incrementally, refusing an overlong line before buffering it."""

    __slots__ = ("_limit", "_pending", "lines")

    def __init__(self, max_line_bytes: int) -> None:
        self._limit = max_line_bytes
        self._pending = bytearray()
        self.lines: list[bytes] = []

    def feed(self, chunk: bytes) -> None:
        """Consume one chunk; the partial-line buffer never exceeds the line bound."""
        view = memoryview(chunk)
        start = 0
        while True:
            index = chunk.find(b"\n", start)
            end = len(chunk) if index < 0 else index
            if len(self._pending) + (end - start) + 1 > self._limit:
                raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.LINE_TOO_LARGE)
            self._pending += view[start:end]
            if index < 0:
                return
            self.lines.append(bytes(self._pending))
            self._pending.clear()
            start = index + 1

    def finish(self) -> tuple[bytes, ...]:
        """Return the complete lines, refusing a torn final line or an empty file."""
        if self._pending or not self.lines:
            raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.LINE_MALFORMED)
        return tuple(self.lines)


def read_verified_lines(
    tree: ColdVerifiedTree, relative_path: str, *, max_line_bytes: int
) -> tuple[bytes, ...]:
    """Re-read one verified record file, framing lines incrementally under the line bound.

    An overlong line is refused as soon as its partial buffer would exceed the bound.
    Lines are returned only after the identity and manifest-digest checks pass, so no
    caller parses changed or unverified bytes.  Complete lines of a legitimate file are
    held until then (bounded by the file cap), because verification precedes parsing.

    Args:
        tree: A verified tree.
        relative_path: One manifest entry path.
        max_line_bytes: Maximum bytes per line including its LF.

    Returns:
        The file's lines without their LF terminators.

    Raises:
        ColdEvidenceStoreError: If framing, identity, or digest checks fail.
    """
    framer = _LineFramer(max_line_bytes)
    _reread_entry(tree, relative_path, retain=False, sink=framer.feed)
    return framer.finish()


def _regular_file_identities(tree: ColdVerifiedTree) -> frozenset[tuple[int, int]]:
    """Return ``(st_dev, st_ino)`` of every verified regular file, manifests included."""
    nodes = tree._nodes  # pyright: ignore[reportPrivateUsage]
    return frozenset((node.dev, node.ino) for node in nodes.values() if not node.is_directory)


def _run_directory_identity(root: ColdAdmittedRoot, run_id: str) -> tuple[int, int]:
    """Return one run directory's ``(st_dev, st_ino)`` to refuse aliased copies.

    This is a same-host software check only; it proves nothing about physical storage.
    """
    run_fd = _open_run_directory(root, run_id, ColdEvidenceStoreFailure.INVENTORY_MISMATCHED)
    try:
        node = _fstat(run_fd, ColdEvidenceStoreFailure.INVENTORY_MISMATCHED)
    finally:
        _release(run_fd)
    return node.dev, node.ino


def verify_retained_copies(
    first: str,
    second: str,
    *,
    run_id: str,
    expected_manifest_sha256: str,
    protected_roots: tuple[str, ...] = (),
) -> ColdVerifiedManifest:
    """Verify both retained copies against one manifest digest; never writes or repairs.

    Trust boundary: ``expected_manifest_sha256`` must be the externally recorded
    ``ColdSealedRun.manifest_sha256`` returned by a successful seal.  It must never be
    derived from the candidate tree, its ``manifest.json`` or its sidecar, which would
    verify a tree against itself.  A seal that fails after creating manifest artefacts
    can leave internally consistent bytes but returns no digest, so such a tree has no
    trusted receipt.  None of this is a filesystem transaction.

    Args:
        first: Absolute root of the first retained copy.
        second: Absolute root of the second retained copy.
        run_id: The run identifier.
        expected_manifest_sha256: The recorded ``manifest.json`` digest.
        protected_roots: Additional absolute roots evidence may never occupy.

    Returns:
        Integrity facts common to both copies.

    Raises:
        ColdEvidenceStoreError: If either copy fails or the copies differ.
    """
    _require_run_id(run_id)
    first_root = admit_evidence_root(first, protected_roots=protected_roots)
    second_root = admit_evidence_root(second, protected_roots=protected_roots)
    if (
        _is_within(first_root.realpath, second_root.realpath)
        or _is_within(second_root.realpath, first_root.realpath)
        or first_root.lineage[-1] in second_root.lineage
        or second_root.lineage[-1] in first_root.lineage
    ):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOTS_OVERLAP)
    if _run_directory_identity(first_root, run_id) == _run_directory_identity(second_root, run_id):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOTS_OVERLAP)
    trees = [
        _verify_admitted_tree(
            root, run_id=run_id, expected_manifest_sha256=expected_manifest_sha256
        )
        for root in (first_root, second_root)
    ]
    if (
        trees[0].manifest_bytes != trees[1].manifest_bytes
        or trees[0].sidecar_bytes != trees[1].sidecar_bytes
    ):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.MANIFEST_COPIES_DIFFER)
    if _regular_file_identities(trees[0]) & _regular_file_identities(trees[1]):
        raise ColdEvidenceStoreError(ColdEvidenceStoreFailure.ROOTS_OVERLAP)
    manifest = trees[0].manifest
    return ColdVerifiedManifest(
        run_id=manifest.run_id,
        manifest_sha256=trees[0].manifest_sha256,
        entry_count=len(manifest.entries),
        identity_bindings=manifest.identity_bindings,
    )
