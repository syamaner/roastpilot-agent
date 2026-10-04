"""Closed, versioned D209 run evidence: temperature aborts and MCP candidate provenance.

This module is pure.  It defines the closed temperature screen vocabulary, the
per-stream ``schema_version`` 5 temperature-abort record written to
``records/<phase>/temperature_abort.jsonl``, the per-stream ``schema_version`` 6 MCP
candidate record written to ``records/<phase>/mcp_candidate.jsonl``, their
in-process content re-admission boundaries, and their retained-document decoders.
It performs no I/O, reads no clock, calls no provider, and decides nothing about a
run.  It adds no member to any existing grammar: an abort here always carries the
existing ``ColdAbortDomain.ENGINE`` domain, so there are still exactly seven abort
domains, and earlier readers refuse both new files.

Honest limits of the candidate record: it is an operator assertion of a reviewed
candidate artefact.  For the current reviewed candidate the reported version string
equals the published 0.2.2 string, so only the artefact digest, byte length and
reviewed source revision distinguish it.  The grammar also admits later versions.
No digest is hard-coded as an allow-list or a deny-list.  A mislabelled published
wheel lacks the temperature projection and so still fails closed at its first
tick.  Nothing here attests authenticity or installed bytes: the only admitted
``installed_bytes_attested`` value is ``False``.  Re-admission proves content, never
provenance.
"""

import enum
import json
import math
import re
import typing

import pydantic

from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_RECORD_BYTES,
    MAX_TEXT_FIELD_BYTES,
    ColdAbortDomain,
    ColdEvidenceError,
    ColdEvidenceFailure,
    ColdPhaseKind,
    ColdRunHeader,
    walk_json_value,
)

#: The per-stream record schema version of the temperature-abort stream.
TEMPERATURE_ABORT_SCHEMA_VERSION: typing.Final = 5
#: The exact stream token of the temperature-abort stream.
TEMPERATURE_ABORT_STREAM: typing.Final = "temperature_abort"
#: The exact per-phase file name of the temperature-abort stream.
TEMPERATURE_ABORT_FILE_NAME: typing.Final = "temperature_abort.jsonl"
#: The per-stream record schema version of the MCP candidate stream.
MCP_CANDIDATE_SCHEMA_VERSION: typing.Final = 6
#: The exact stream token of the MCP candidate stream.
MCP_CANDIDATE_STREAM: typing.Final = "mcp_candidate"
#: The exact per-phase file name of the MCP candidate stream.
MCP_CANDIDATE_FILE_NAME: typing.Final = "mcp_candidate.jsonl"
#: The only admitted candidate distribution name.
MCP_CANDIDATE_DISTRIBUTION: typing.Final = "coffee-roaster-mcp"
#: The largest admitted candidate artefact byte length.
MCP_CANDIDATE_MAX_BYTE_LENGTH: typing.Final = 2**31 - 1

_VERSION_PATTERN: typing.Final = re.compile(
    r"\A(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\Z"
)
_SHA256_PATTERN: typing.Final = re.compile(r"\A[0-9a-f]{64}\Z")
_REVISION_PATTERN: typing.Final = re.compile(r"\A[0-9a-f]{40}\Z")

_RUN_ID_ADAPTER: pydantic.TypeAdapter[str] = pydantic.TypeAdapter(
    typing.Annotated[str, *ColdRunHeader.model_fields["run_id"].metadata]
)
_DIGEST_ADAPTER: pydantic.TypeAdapter[str] = pydantic.TypeAdapter(
    typing.Annotated[str, *ColdRunHeader.model_fields["identity_sha256"].metadata]
)


class ColdTemperatureScreenReason(enum.Enum):
    """Closed D209 temperature screen reasons, in declaration (and report) order.

    A reason is a classification only; it is never an outcome or a qualification.
    """

    SCREEN_INPUT_NOT_ADMITTED = "temperature_screen_input_not_admitted"
    PROJECTION_MALFORMED = "temperature_projection_malformed"
    NOT_OBSERVABLE = "temperature_not_observable"
    COUNTER_REGRESSED = "temperature_counter_regressed"
    PRIOR_MISSING = "temperature_prior_missing"
    NOT_OBSERVED_AFTER_STARTUP = "temperature_not_observed_after_startup"
    LAST_PACKET_NOT_VALID_CELSIUS = "temperature_last_packet_not_valid_celsius"
    VALUES_DISAGREE = "temperature_values_disagree"
    OUTSIDE_SCREEN = "temperature_outside_screen"
    PACKET_NOT_PROGRESSED = "temperature_packet_not_progressed"
    FAULT_COUNTED = "temperature_fault_counted"


class ColdMcpCandidateArtefactKind(enum.Enum):
    """The closed kind of the reviewed candidate artefact."""

    WHEEL = "wheel"


class ColdMcpCandidateAssertion(enum.Enum):
    """The closed basis of a candidate record: an operator assertion, never attestation."""

    OPERATOR_ASSERTED_REVIEWED_CANDIDATE = "operator_asserted_reviewed_candidate"


class ColdTemperatureRunFailure(enum.Enum):
    """Closed temperature-abort and MCP candidate ordering and uniqueness refusals."""

    PHASE_NOT_LATEST = "phase_not_latest"
    TICK_NOT_PAIRED = "tick_not_paired"
    ABORT_DUPLICATED = "abort_duplicated"
    CANDIDATE_DUPLICATED = "candidate_duplicated"
    CANDIDATE_AFTER_TICK = "candidate_after_tick"
    CANDIDATE_NOT_UNIQUE = "candidate_not_unique"


class ColdTemperatureRunError(Exception):
    """Closed temperature run evidence error with a fixed message and no input content."""

    failure: ColdTemperatureRunFailure

    def __init__(self, failure: ColdTemperatureRunFailure) -> None:
        """Create a content-free temperature run evidence refusal.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold temperature run evidence refused.")
        self.failure = failure


_MemberT = typing.TypeVar("_MemberT", bound=enum.Enum)


def _tokens(enum_type: type[_MemberT]) -> dict[str, _MemberT]:
    """Return an exact token-to-member table for one closed enum."""
    return {member.value: member for member in enum_type}


_PHASE_TOKENS: typing.Final = _tokens(ColdPhaseKind)
_DOMAIN_TOKENS: typing.Final = _tokens(ColdAbortDomain)
_REASON_TOKENS: typing.Final = _tokens(ColdTemperatureScreenReason)
_KIND_TOKENS: typing.Final = _tokens(ColdMcpCandidateArtefactKind)
_ASSERTION_TOKENS: typing.Final = _tokens(ColdMcpCandidateAssertion)


def _is_member(value: object, enum_type: type[enum.Enum]) -> bool:
    """Whether a value is exactly one of an enum's own members, found by identity."""
    return type(value) is enum_type and any(value is member for member in enum_type)


def _is_distribution(value: object) -> bool:
    """Whether a value is the exact ``str`` candidate distribution name."""
    return type(value) is str and value == MCP_CANDIDATE_DISTRIBUTION


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    """Whether a value is an exact ``str`` that fully matches one closed pattern."""
    return type(value) is str and pattern.fullmatch(value) is not None


def _is_version(value: object) -> bool:
    """Whether a value is an exact ``str`` three-part release version."""
    return _matches(_VERSION_PATTERN, value)


def _is_byte_length(value: object) -> bool:
    """Whether a value is an exact ``int`` within ``1..MCP_CANDIDATE_MAX_BYTE_LENGTH``."""
    return type(value) is int and 1 <= value <= MCP_CANDIDATE_MAX_BYTE_LENGTH


def _is_sha256(value: object) -> bool:
    """Whether a value is an exact lowercase hexadecimal SHA-256 ``str``."""
    return _matches(_SHA256_PATTERN, value)


def _is_revision(value: object) -> bool:
    """Whether a value is an exact lowercase hexadecimal 40-character revision ``str``."""
    return _matches(_REVISION_PATTERN, value)


def _is_not_attested(value: object) -> bool:
    """Whether a value is exactly ``False``."""
    return value is False


def _admitted_by(predicate: typing.Callable[[object], bool]) -> typing.Callable[[object], object]:
    """Return a before-validator that admits a value only if ``predicate`` holds."""

    def admit(value: object) -> object:
        if predicate(value):
            return value
        raise ValueError("value is not admitted")

    return admit


def _member_of(enum_type: type[enum.Enum]) -> typing.Callable[[object], object]:
    """Return a before-validator that admits only a real member of ``enum_type``."""
    return _admitted_by(lambda value: _is_member(value, enum_type))


class ColdMcpCandidateProvenance(pydantic.BaseModel):
    """One closed operator assertion of a reviewed MCP candidate artefact.

    It names the distribution, the version string the artefact reports, the
    artefact kind, byte length and SHA-256 digest, and the reviewed source
    revision.  ``installed_bytes_attested`` is always ``False``: this records an
    assertion and never attests authenticity or the bytes installed on a device.
    Direct construction may raise a ``ValidationError`` that embeds its input; the
    public boundaries are :func:`admit_mcp_candidate_document` and
    :func:`readmit_mcp_candidate_provenance`.
    """

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    distribution: typing.Annotated[str, pydantic.BeforeValidator(_admitted_by(_is_distribution))]
    reported_version: typing.Annotated[str, pydantic.BeforeValidator(_admitted_by(_is_version))]
    artefact_kind: typing.Annotated[
        ColdMcpCandidateArtefactKind,
        pydantic.BeforeValidator(_member_of(ColdMcpCandidateArtefactKind)),
    ]
    artefact_byte_length: typing.Annotated[
        int, pydantic.BeforeValidator(_admitted_by(_is_byte_length))
    ]
    artefact_sha256: typing.Annotated[str, pydantic.BeforeValidator(_admitted_by(_is_sha256))]
    reviewed_source_revision: typing.Annotated[
        str, pydantic.BeforeValidator(_admitted_by(_is_revision))
    ]
    assertion: typing.Annotated[
        ColdMcpCandidateAssertion, pydantic.BeforeValidator(_member_of(ColdMcpCandidateAssertion))
    ]
    installed_bytes_attested: typing.Annotated[
        bool, pydantic.BeforeValidator(_admitted_by(_is_not_attested))
    ]


_PROVENANCE_FIELDS: typing.Final[tuple[str, ...]] = tuple(ColdMcpCandidateProvenance.model_fields)
_PROVENANCE_FIELD_SET: typing.Final[frozenset[str]] = frozenset(_PROVENANCE_FIELDS)


def admit_mcp_candidate_document(raw: object) -> ColdMcpCandidateProvenance | None:
    """Admit one JSON-derived candidate provenance document, or return ``None``.

    This is the single shared candidate grammar.  For JSON-derived values it is
    total and never raises.  Exact guards run first: an exact ``dict``, the field
    count checked before any key is scanned, every key an exact ``str`` before any
    lookup, the exact key set, then each field's exact type, token or pattern, and
    finally strict model validation.  ``None`` carries no detail.

    Args:
        raw: One JSON-derived value.

    Returns:
        A strictly validated provenance, or ``None`` if any rule refuses it.
    """
    if type(raw) is not dict:
        return None
    mapping = typing.cast(dict[object, object], raw)
    if len(mapping) != len(_PROVENANCE_FIELDS):
        return None
    for key in mapping:
        if type(key) is not str:
            return None
    fields = typing.cast(dict[str, object], mapping)
    if frozenset(fields) != _PROVENANCE_FIELD_SET:
        return None
    kind_token = fields["artefact_kind"]
    assertion_token = fields["assertion"]
    kind = _KIND_TOKENS.get(kind_token) if type(kind_token) is str else None
    assertion = _ASSERTION_TOKENS.get(assertion_token) if type(assertion_token) is str else None
    if not (
        _is_distribution(fields["distribution"])
        and _is_version(fields["reported_version"])
        and kind is not None
        and assertion is not None
        and _is_byte_length(fields["artefact_byte_length"])
        and _is_sha256(fields["artefact_sha256"])
        and _is_revision(fields["reviewed_source_revision"])
        and _is_not_attested(fields["installed_bytes_attested"])
    ):
        return None
    try:
        admitted: ColdMcpCandidateProvenance | None = ColdMcpCandidateProvenance.model_validate(
            {**fields, "artefact_kind": kind, "assertion": assertion}, strict=True
        )
    except pydantic.ValidationError:
        # The error embeds input values, so it is discarded, never re-raised.
        admitted = None
    return admitted


def _declared_state(value: object, names: tuple[str, ...]) -> dict[str, object] | None:
    """Return a copy of an instance's exactly declared raw state, or ``None``.

    The raw state is read without attribute hooks.  ``None`` is returned for an
    uninitialised instance, a non-``dict`` state, a field count that differs (checked
    before any key is scanned), a non-``str`` key, non-empty extras, or a key set
    that differs from ``names``.
    """
    try:
        data: object = object.__getattribute__(value, "__dict__")
        extra: object = object.__getattribute__(value, "__pydantic_extra__")
    except AttributeError:
        return None
    if type(data) is not dict:
        return None
    state = typing.cast(dict[object, object], data)
    if len(state) != len(names):
        return None
    for key in state:
        if type(key) is not str:
            return None
    if extra is not None and (type(extra) is not dict or extra):
        return None
    fields = typing.cast(dict[str, object], state)
    if frozenset(fields) != frozenset(names):
        return None
    return {name: fields[name] for name in names}


def readmit_mcp_candidate_provenance(value: object) -> ColdMcpCandidateProvenance | None:
    """Re-admit one in-process candidate provenance's content, or return ``None``.

    Only an exact ``ColdMcpCandidateProvenance`` instance is considered.  Its raw
    declared state is read without attribute hooks; an uninitialised instance,
    undeclared, missing or non-``str``-keyed state, and non-empty extras are refused
    before any key is hashed or compared.  Both member fields must be real members,
    established by identity before their values are read.  The resulting raw
    document then runs :func:`admit_mcp_candidate_document` again, so a fresh
    provenance is returned and the input instance never is.  It never logs or renders.

    Args:
        value: Any in-process candidate provenance.

    Returns:
        A freshly validated provenance, or ``None``.
    """
    if type(value) is not ColdMcpCandidateProvenance:
        return None
    raw = _declared_state(value, _PROVENANCE_FIELDS)
    if raw is None:
        return None
    kind = raw["artefact_kind"]
    assertion = raw["assertion"]
    if not (
        _is_member(kind, ColdMcpCandidateArtefactKind)
        and _is_member(assertion, ColdMcpCandidateAssertion)
    ):
        return None
    raw["artefact_kind"] = typing.cast(ColdMcpCandidateArtefactKind, kind).value
    raw["assertion"] = typing.cast(ColdMcpCandidateAssertion, assertion).value
    return admit_mcp_candidate_document(raw)


# ------------------------------------------------------------------- records


def _grammar_admits(adapter: pydantic.TypeAdapter[str], value: str) -> bool:
    """Whether one exact string satisfies a delivered v1 header grammar."""
    try:
        adapter.validate_python(value, strict=True)
    except pydantic.ValidationError:
        return False
    return True


def _is_text(value: object) -> bool:
    """Whether a value is an exact ``str``."""
    return type(value) is str


def _is_run_id(value: object) -> bool:
    """Whether a value is an exact ``str`` satisfying the v1 header run-id grammar."""
    return type(value) is str and _grammar_admits(_RUN_ID_ADAPTER, value)


def _is_digest(value: object) -> bool:
    """Whether a value is an exact ``str`` satisfying the v1 identity-digest grammar."""
    return type(value) is str and _grammar_admits(_DIGEST_ADAPTER, value)


def _is_finite_float(value: object) -> bool:
    """Whether a value is an exact finite ``float`` (never an ``int``)."""
    return type(value) is float and math.isfinite(value)


def _is_tick(value: object) -> bool:
    """Whether a value is an exact non-negative ``int`` (never a ``bool`` or ``float``)."""
    return type(value) is int and value >= 0


def _is_engine_domain(value: object) -> bool:
    """Whether a value is exactly ``ColdAbortDomain.ENGINE``, found by identity."""
    return value is ColdAbortDomain.ENGINE


def _exact_version(expected: int) -> typing.Callable[[object], object]:
    """Return a before-validator that admits only the exact integer ``expected``."""
    return _admitted_by(lambda value: type(value) is int and value == expected)


def _readmit_candidate(value: object) -> object:
    """Re-admit a nested candidate provenance and return the fresh value."""
    fresh = readmit_mcp_candidate_provenance(value)
    if fresh is None:
        raise ValueError("candidate provenance is not admitted")
    return fresh


_RunId = typing.Annotated[str, pydantic.BeforeValidator(_admitted_by(_is_run_id))]
_Phase = typing.Annotated[ColdPhaseKind, pydantic.BeforeValidator(_member_of(ColdPhaseKind))]
_Recorded = typing.Annotated[
    str,
    pydantic.BeforeValidator(_admitted_by(_is_text)),
    pydantic.Field(max_length=MAX_TEXT_FIELD_BYTES),
]
_Monotonic = typing.Annotated[float, pydantic.BeforeValidator(_admitted_by(_is_finite_float))]
_Digest = typing.Annotated[str, pydantic.BeforeValidator(_admitted_by(_is_digest))]
_RECORD_CONFIG: typing.Final = pydantic.ConfigDict(
    frozen=True, extra="forbid", strict=True, allow_inf_nan=False
)


class ColdTemperatureAbortRecord(pydantic.BaseModel):
    """One flat, closed ``schema_version`` 5 temperature-abort record.

    It records one temperature screen reason against a phase's latest paired tick
    in the existing ``ENGINE`` abort domain.  Its recording instant is its own; it
    is never compared with the tick's.  Several distinct reasons for one tick are
    valid data.  It decides nothing and never qualifies a run.  Direct construction
    may raise a ``ValidationError`` that embeds its input; the public boundaries are
    :func:`validate_temperature_abort_record` and :func:`decode_temperature_abort_document`.
    """

    model_config = _RECORD_CONFIG

    schema_version: typing.Annotated[
        typing.Literal[5],
        pydantic.BeforeValidator(_exact_version(TEMPERATURE_ABORT_SCHEMA_VERSION)),
    ]
    stream: typing.Annotated[
        typing.Literal["temperature_abort"], pydantic.BeforeValidator(_admitted_by(_is_text))
    ]
    run_id: _RunId
    phase: _Phase
    recorded_at_utc: _Recorded
    monotonic_seconds: _Monotonic
    identity_sha256: _Digest
    tick: typing.Annotated[int, pydantic.BeforeValidator(_admitted_by(_is_tick))]
    domain: typing.Annotated[
        ColdAbortDomain, pydantic.BeforeValidator(_admitted_by(_is_engine_domain))
    ]
    reason: typing.Annotated[
        ColdTemperatureScreenReason,
        pydantic.BeforeValidator(_member_of(ColdTemperatureScreenReason)),
    ]


class ColdMcpCandidateRecord(pydantic.BaseModel):
    """One flat, closed ``schema_version`` 6 MCP candidate provenance record.

    It binds one operator-asserted candidate provenance to one phase header.  It has
    no tick, domain or reason.  Append order (after the header, before any tick) is
    a writer fact that a reader cannot prove across files.  Direct construction may
    raise a ``ValidationError`` that embeds its input; the public boundaries are
    :func:`validate_mcp_candidate_record` and :func:`decode_mcp_candidate_document`.
    """

    model_config = _RECORD_CONFIG

    schema_version: typing.Annotated[
        typing.Literal[6], pydantic.BeforeValidator(_exact_version(MCP_CANDIDATE_SCHEMA_VERSION))
    ]
    stream: typing.Annotated[
        typing.Literal["mcp_candidate"], pydantic.BeforeValidator(_admitted_by(_is_text))
    ]
    run_id: _RunId
    phase: _Phase
    recorded_at_utc: _Recorded
    monotonic_seconds: _Monotonic
    identity_sha256: _Digest
    candidate: typing.Annotated[
        ColdMcpCandidateProvenance, pydantic.BeforeValidator(_readmit_candidate)
    ]


_RecordT = typing.TypeVar("_RecordT", ColdTemperatureAbortRecord, ColdMcpCandidateRecord)
_ABORT_FIELDS: typing.Final[tuple[str, ...]] = tuple(ColdTemperatureAbortRecord.model_fields)
_CANDIDATE_FIELDS: typing.Final[tuple[str, ...]] = tuple(ColdMcpCandidateRecord.model_fields)
_SCALAR_TYPES: typing.Final[tuple[type[object], ...]] = (int, float, str)
#: Each record's member-valued fields with their exact enum class.
_MEMBER_FIELDS: typing.Final[dict[str, type[enum.Enum]]] = {
    "phase": ColdPhaseKind,
    "domain": ColdAbortDomain,
    "reason": ColdTemperatureScreenReason,
}


def _canonical_json(value: object) -> str:
    """Return the canonical JSON text every retained evidence line uses.

    A contract test pins this rendering to the store's ``canonical_json``.
    """
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _snapshot(
    record: object, model: type[_RecordT], names: tuple[str, ...]
) -> _RecordT | ColdEvidenceFailure:
    """Re-admit one record's content, returning a fresh snapshot or a closed failure.

    Raises:
        ColdEvidenceError: The shared walker's own member for a JSON bound breach.
        pydantic.ValidationError: If strict model validation refuses the values.
    """
    if type(record) is not model:
        return ColdEvidenceFailure.RECORD_NOT_VALIDATED
    values = _declared_state(record, names)
    if values is None:
        return ColdEvidenceFailure.RECORD_NOT_VALIDATED
    view: dict[str, object] = {}
    for name in names:
        value = values[name]
        enum_type = _MEMBER_FIELDS.get(name)
        if enum_type is not None:
            if not _is_member(value, enum_type):
                return ColdEvidenceFailure.RECORD_NOT_VALIDATED
            view[name] = typing.cast(enum.Enum, value).value
        elif name == "candidate":
            fresh = readmit_mcp_candidate_provenance(value)
            if fresh is None:
                return ColdEvidenceFailure.RECORD_NOT_VALIDATED
            values[name] = fresh
            view[name] = fresh.model_dump(mode="json")
        elif type(value) in _SCALAR_TYPES:
            view[name] = value
        else:
            return ColdEvidenceFailure.RECORD_NOT_VALIDATED
    walk_json_value(typing.cast(pydantic.JsonValue, view))
    recorded = values["recorded_at_utc"]
    if type(recorded) is str and len(recorded.encode("utf-8")) > MAX_TEXT_FIELD_BYTES:
        return ColdEvidenceFailure.TEXT_FIELD_TOO_LARGE
    validated = model.model_validate(values, strict=True)
    if len(_canonical_json(validated.model_dump(mode="json")).encode("utf-8")) > MAX_RECORD_BYTES:
        return ColdEvidenceFailure.RECORD_TOO_LARGE
    return validated


def _validate(record: object, model: type[_RecordT], names: tuple[str, ...]) -> _RecordT:
    """Run one re-admission and raise a closed, chain-free error on refusal."""
    try:
        result = _snapshot(record, model, names)
    except ColdEvidenceError as error:
        result = error.failure
    except (ArithmeticError, AttributeError, LookupError, TypeError, ValueError):
        # A ValidationError embeds input values, so it is discarded, never chained.
        result = ColdEvidenceFailure.RECORD_NOT_VALIDATED
    if type(result) is ColdEvidenceFailure:
        raise ColdEvidenceError(result)
    return typing.cast(_RecordT, result)


def validate_temperature_abort_record(record: object) -> ColdTemperatureAbortRecord:
    """Re-admit one temperature-abort record's content and return a fresh snapshot.

    This is the shared write and read boundary.  Subclasses, uninitialised
    instances, undeclared or missing state, a field count that differs, non-``str``
    keys, non-empty extras, foreign or fabricated members, and non-exact values are
    refused.  The admitted values are walked by the shared JSON walker, the recorded
    time is held to the shared UTF-8 text-field byte limit, the values are strictly
    validated, and the canonical record is held to the shared record byte cap.

    Args:
        record: Any in-process candidate record.

    Returns:
        A newly validated snapshot (never the input instance).

    Raises:
        ColdEvidenceError: ``RECORD_NOT_VALIDATED``, ``TEXT_FIELD_TOO_LARGE``,
            ``RECORD_TOO_LARGE``, or the shared walker's own member.  The error
            carries no input content, cause, or context.
    """
    return _validate(record, ColdTemperatureAbortRecord, _ABORT_FIELDS)


def validate_mcp_candidate_record(record: object) -> ColdMcpCandidateRecord:
    """Re-admit one MCP candidate record's content and return a fresh snapshot.

    The same boundary as :func:`validate_temperature_abort_record`; the nested
    provenance is re-admitted through :func:`readmit_mcp_candidate_provenance`.

    Args:
        record: Any in-process candidate record.

    Returns:
        A newly validated snapshot (never the input instance).

    Raises:
        ColdEvidenceError: ``RECORD_NOT_VALIDATED``, ``TEXT_FIELD_TOO_LARGE``,
            ``RECORD_TOO_LARGE``, or the shared walker's own member.  The error
            carries no input content, cause, or context.
    """
    return _validate(record, ColdMcpCandidateRecord, _CANDIDATE_FIELDS)


def _document_fields(document: object, names: tuple[str, ...]) -> dict[str, object] | None:
    """Return a JSON document's exactly named fields, or ``None``.

    The field count is checked before any key is scanned, and every key must be an
    exact ``str`` before the key set is compared.
    """
    if type(document) is not dict:
        return None
    mapping = typing.cast(dict[object, object], document)
    if len(mapping) != len(names):
        return None
    for key in mapping:
        if type(key) is not str:
            return None
    fields = typing.cast(dict[str, object], mapping)
    if frozenset(fields) != frozenset(names):
        return None
    return dict(fields)


def _token(table: dict[str, _MemberT], value: object) -> _MemberT | None:
    """Convert one exact ``str`` token by exact table lookup, or return ``None``."""
    return table.get(value) if type(value) is str else None


def _decoded(model: type[_RecordT], values: dict[str, object]) -> _RecordT | None:
    """Strictly validate converted values, discarding any error that embeds input."""
    try:
        decoded: _RecordT | None = model.model_validate(values, strict=True)
    except pydantic.ValidationError:
        decoded = None
    return decoded


def decode_temperature_abort_document(document: object) -> ColdTemperatureAbortRecord | None:
    """Strictly decode one JSON-derived retained temperature-abort document, or ``None``.

    For the values ``load_strict_json`` can produce the function is total and never
    raises.  Exact guards run first (an exact ``dict``, the field count before any
    key scan, exact ``str`` keys, the exact key set), then the phase, domain and
    reason tokens are converted by exact table lookup, then strict model validation
    applies the remaining grammar.

    Args:
        document: One JSON-derived value.

    Returns:
        The strictly validated record, or ``None`` if any rule refuses it.
    """
    fields = _document_fields(document, _ABORT_FIELDS)
    if fields is None:
        return None
    phase = _token(_PHASE_TOKENS, fields["phase"])
    domain = _token(_DOMAIN_TOKENS, fields["domain"])
    reason = _token(_REASON_TOKENS, fields["reason"])
    if phase is None or domain is None or reason is None:
        return None
    return _decoded(
        ColdTemperatureAbortRecord, {**fields, "phase": phase, "domain": domain, "reason": reason}
    )


def decode_mcp_candidate_document(document: object) -> ColdMcpCandidateRecord | None:
    """Strictly decode one JSON-derived retained MCP candidate document, or ``None``.

    For the values ``load_strict_json`` can produce the function is total and never
    raises.  Exact guards run first, the phase token is converted by exact table
    lookup, the nested candidate is admitted by :func:`admit_mcp_candidate_document`,
    then strict model validation applies the remaining grammar.

    Args:
        document: One JSON-derived value.

    Returns:
        The strictly validated record, or ``None`` if any rule refuses it.
    """
    fields = _document_fields(document, _CANDIDATE_FIELDS)
    if fields is None:
        return None
    phase = _token(_PHASE_TOKENS, fields["phase"])
    candidate = admit_mcp_candidate_document(fields["candidate"])
    if phase is None or candidate is None:
        return None
    return _decoded(ColdMcpCandidateRecord, {**fields, "phase": phase, "candidate": candidate})
