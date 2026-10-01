"""Closed, versioned per-phase advisory-attempt evidence for cold characterisation.

This module is pure: it defines the per-stream ``schema_version`` 2 records written
to ``records/<phase>/advisory_attempt.jsonl``, their lossless revalidation, two
header-bound builders, and the run-wide append-order state shared by the writer
and the reader.  It performs no I/O, reads no clock, calls no provider, and decides
nothing about a run.  An intent line is appended before an advisory call and one
resolution line after it; neither is ever rewritten.  Failed, abandoned, and
unresolved attempts are recorded as data, and no request, decision, evaluation,
or usage is fabricated for them.  Unknown values stay unknown (``None``).
"""

import enum
import hashlib
import json
import math
import typing

import pydantic

from roastpilot_agent.cold_characterisation.evidence_lifecycle import (
    is_admissible_monotonic,
    is_admissible_utc_instant,
)
from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_RECORD_BYTES,
    MAX_TEXT_FIELD_BYTES,
    ColdEvidenceError,
    ColdEvidenceFailure,
    ColdPhaseKind,
    ColdRunHeader,
    ColdSafetyEvaluation,
    ColdSafetyVerdict,
    validate_record,
    walk_json_value,
)

#: Ordinary resource bound on one retained canonical advisor context; not a safety threshold.
MAX_ADVISORY_CONTEXT_BYTES: typing.Final = 65_536
#: Whole canonical line bound; the reader frames lines at ``MAX_RECORD_BYTES`` plus LF.
_RECORD_LINE_LIMIT = MAX_RECORD_BYTES

_RUN_ID_ADAPTER: pydantic.TypeAdapter[str] = pydantic.TypeAdapter(
    typing.Annotated[str, *ColdRunHeader.model_fields["run_id"].metadata]
)
_DIGEST_ADAPTER: pydantic.TypeAdapter[str] = pydantic.TypeAdapter(
    typing.Annotated[str, *ColdRunHeader.model_fields["identity_sha256"].metadata]
)


class ColdAdvisoryEntry(enum.Enum):
    """Closed advisory-attempt line kinds; each line is immutable once appended."""

    INTENT = "intent"
    RESOLUTION = "resolution"


class ColdAdvisoryContextSource(enum.Enum):
    """Where an attempt's advisor context was taken from."""

    RETAINED_TICK = "retained_tick"


class ColdAdvisoryResolution(enum.Enum):
    """How one advisory attempt resolved, as recorded; no member means success or health."""

    RETURNED_DECISION = "returned_decision"
    RETURNED_UNSAFE_OUTPUT = "returned_unsafe_output"
    RETURNED_MALFORMED_OUTPUT = "returned_malformed_output"
    RETURNED_PROVIDER_ERROR = "returned_provider_error"
    RAISED_UNCLASSIFIED = "raised_unclassified"
    ABANDONED_AFTER_BOUND = "abandoned_after_bound"
    UNRESOLVED_AT_PHASE_END = "unresolved_at_phase_end"


class ColdAdvisoryInvocationState(enum.Enum):
    """Whether the caller knew the provider call had been invoked."""

    INVOKED = "invoked"
    NOT_INVOKED = "not_invoked"


class ColdAdvisoryRationaleState(enum.Enum):
    """Whether a returned rationale was retained, and if not, why; never truncated."""

    RETAINED = "retained"
    NOT_RETAINED_OVER_BOUND = "not_retained_over_bound"
    NOT_RETAINED_NOT_ENCODABLE = "not_retained_not_encodable"


class ColdAdvisoryEvaluationState(enum.Enum):
    """Whether a typed safety evaluation was recorded for a returned decision."""

    RECORDED = "recorded"
    NOT_RECORDED = "not_recorded"


class ColdAdvisoryUsageState(enum.Enum):
    """Whether admitted token usage was recorded for a returned call."""

    RECORDED = "recorded"
    NOT_RECORDED = "not_recorded"


class ColdAdvisoryUsageBasis(enum.Enum):
    """The basis of recorded usage counts; never a claim of provider-reported zero."""

    PRODUCTION_NORMALISED = "production_normalised"


class ColdAdvisoryAttemptFailure(enum.Enum):
    """Closed advisory-attempt ordering refusals."""

    ATTEMPT_INDEX_NOT_CONTIGUOUS = "attempt_index_not_contiguous"
    ATTEMPT_ALREADY_OPEN = "attempt_already_open"
    RESOLUTION_WITHOUT_OPEN_ATTEMPT = "resolution_without_open_attempt"
    RESOLUTION_NOT_MATCHING = "resolution_not_matching"
    PHASE_NOT_LATEST = "phase_not_latest"
    PHASE_REGRESSED = "phase_regressed"
    OPEN_ATTEMPT_CROSSES_PHASE = "open_attempt_crosses_phase"
    OBSERVED_TIME_REGRESSED = "observed_time_regressed"


class ColdAdvisoryAttemptEvidenceState(enum.Enum):
    """Structural state of a retained advisory stream; no member is ever healthy.

    ``COMPLETE`` means only that every retained attempt has a resolution, which may
    be failed, abandoned, or unresolved at phase end; it never means success,
    health, or conformance.  ``OPEN_TAIL`` means the last retained attempt has no
    resolution.  ``ABSENT`` means no attempt was retained.
    """

    ABSENT = "absent"
    COMPLETE = "complete"
    OPEN_TAIL = "open_tail"


class ColdAdvisoryAttemptError(RuntimeError):
    """Closed advisory-attempt ordering error with a fixed message and no input content."""

    failure: ColdAdvisoryAttemptFailure

    def __init__(self, failure: ColdAdvisoryAttemptFailure) -> None:
        """Create a content-free advisory-attempt failure.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold advisory attempt evidence refused.")
        self.failure = failure


def _utf8_size(value: str) -> int | None:
    """Return a string's UTF-8 size, or ``None`` if it holds a surrogate."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _canonical_text(value: object) -> str:
    """Return the store's canonical JSON text for an already admitted value."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def _grammar_admits(adapter: pydantic.TypeAdapter[str], value: str) -> bool:
    """Whether one exact string satisfies a delivered v1 header grammar."""
    try:
        adapter.validate_python(value, strict=True)
    except pydantic.ValidationError:
        return False
    return True


class _ColdAdvisoryAttemptBase(pydantic.BaseModel):
    """Common flat fields and exact scalar admission for both advisory-attempt lines."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    schema_version: typing.Literal[2]
    stream: typing.Literal["advisory_attempt"]
    run_id: str
    identity_sha256: str
    phase: ColdPhaseKind
    recorded_at_utc: str
    monotonic_seconds: float
    attempt_index: int

    @pydantic.field_validator(
        "schema_version",
        "attempt_index",
        "context_tick",
        "context_byte_length",
        "usage_input_tokens",
        "usage_output_tokens",
        "usage_total_tokens",
        "usage_reasoning_tokens",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _require_count(cls, value: object) -> object:
        """Refuse a ``bool``, ``float``, negative, or non-``int`` instead of coercing it."""
        if value is None or (type(value) is int and value >= 0):
            return value
        raise ValueError("value must be an exact non-negative int")

    @pydantic.field_validator(
        "entry",
        "stream",
        "run_id",
        "identity_sha256",
        "context_canonical_json",
        "context_sha256",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _require_exact_text(cls, value: object) -> object:
        """Refuse a non-``str`` or ``str``-subclass value."""
        if type(value) is str:
            return value
        raise ValueError("value must be an exact str")

    @pydantic.field_validator("run_id", mode="after")
    @classmethod
    def _require_run_id(cls, value: str) -> str:
        """Apply the v1 header run-id grammar."""
        if _grammar_admits(_RUN_ID_ADAPTER, value):
            return value
        raise ValueError("run id grammar")

    @pydantic.field_validator("identity_sha256", mode="after")
    @classmethod
    def _require_digest(cls, value: str) -> str:
        """Apply the v1 header identity-digest grammar."""
        if _grammar_admits(_DIGEST_ADAPTER, value):
            return value
        raise ValueError("digest grammar")

    @pydantic.field_validator(
        "recorded_at_utc", "invocation_utc", "resolved_utc", mode="before", check_fields=False
    )
    @classmethod
    def _require_utc_instant(cls, value: object) -> object:
        """Refuse anything but an exact bounded UTC instant string (or null)."""
        if value is None or is_admissible_utc_instant(value):
            return value
        raise ValueError("value must be an exact UTC instant")

    @pydantic.field_validator(
        "monotonic_seconds",
        "context_tick_monotonic",
        "invocation_monotonic",
        "resolved_monotonic",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _require_monotonic(cls, value: object) -> object:
        """Refuse anything but an exact finite non-negative ``float`` (or null)."""
        if value is None or is_admissible_monotonic(value):
            return value
        raise ValueError("value must be an exact non-negative finite float")

    @pydantic.field_validator(
        "context_target_drop_temp_c",
        "context_charge_guidance_min_c",
        "context_charge_guidance_max_c",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _require_finite_temperature(cls, value: object) -> object:
        """Refuse anything but an exact finite Celsius ``float`` (or null); no range."""
        if value is None or (type(value) is float and math.isfinite(value)):
            return value
        raise ValueError("value must be an exact finite float")

    @pydantic.field_validator(
        "configured_call_bound_seconds",
        "configured_dwell_seconds",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _require_positive_seconds(cls, value: object) -> object:
        """Refuse anything but an exact finite positive ``float``; recorded, never compared."""
        if type(value) is float and math.isfinite(value) and value > 0.0:
            return value
        raise ValueError("value must be an exact finite positive float")

    @pydantic.field_validator(
        "context_profile_name",
        "descriptor_provider",
        "descriptor_model",
        "descriptor_prompt_version",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _require_label(cls, value: object) -> object:
        """Refuse anything but an exact, bounded, encodable, non-blank ``str``."""
        if type(value) is str and value.strip():
            size = _utf8_size(value)
            if size is not None and size <= MAX_TEXT_FIELD_BYTES:
                return value
        raise ValueError("value must be a bounded non-blank str")

    @pydantic.field_validator(
        "rationale", "evaluation_rule", "evaluation_reason", mode="before", check_fields=False
    )
    @classmethod
    def _require_bounded_text(cls, value: object) -> object:
        """Refuse anything but an exact, bounded, encodable ``str`` (or null)."""
        if value is None:
            return value
        if type(value) is str:
            size = _utf8_size(value)
            if size is not None and size <= MAX_TEXT_FIELD_BYTES:
                return value
        raise ValueError("value must be a bounded str")

    @pydantic.field_validator("requested_heat", "requested_fan", mode="before", check_fields=False)
    @classmethod
    def _require_lever(cls, value: object) -> object:
        """Refuse anything but an exact ``int`` from 0 to 100 (or null)."""
        if value is None or (type(value) is int and 0 <= value <= 100):
            return value
        raise ValueError("value must be an exact int from 0 to 100")

    @pydantic.field_validator(
        "evaluation_input_heat",
        "evaluation_input_fan",
        "evaluation_adjusted_heat",
        "evaluation_adjusted_fan",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _require_exact_int(cls, value: object) -> object:
        """Refuse anything but an exact ``int`` (or null); ranges come from the evaluation."""
        if value is None or type(value) is int:
            return value
        raise ValueError("value must be an exact int")

    @pydantic.field_validator("should_drop", mode="before", check_fields=False)
    @classmethod
    def _require_exact_bool(cls, value: object) -> object:
        """Refuse anything but an exact ``bool`` (or null)."""
        if value is None or type(value) is bool:
            return value
        raise ValueError("value must be an exact bool")

    @pydantic.field_validator("confidence", mode="before", check_fields=False)
    @classmethod
    def _require_confidence(cls, value: object) -> object:
        """Refuse anything but an exact finite ``float`` from 0 to 1 (or null)."""
        if value is None or (type(value) is float and 0.0 <= value <= 1.0):
            return value
        raise ValueError("value must be an exact float from 0 to 1")


class ColdAdvisoryIntentRecord(_ColdAdvisoryAttemptBase):
    """One immutable intent line, appended before an advisory call may be invoked.

    ``recorded_at_utc``/``monotonic_seconds`` are an intent lower bound only: they are
    not proof that a provider request was issued, not the exact request start, and
    not an upper bound on it.  The caller's actual invocation instant, when known,
    is retained only in the matching resolution.  The context spec fields are
    copied from the operator's admitted spec as supplied, with no defaults, and
    must equal the same keys in the retained canonical context.  The configured
    per-call bound and dwell are recorded, never compared.
    """

    entry: typing.Literal["intent"]
    context_source: ColdAdvisoryContextSource
    context_tick: int
    context_tick_monotonic: float
    context_profile_name: str
    context_target_drop_temp_c: float
    context_charge_guidance_min_c: float | None
    context_charge_guidance_max_c: float | None
    context_canonical_json: str
    context_byte_length: int
    context_sha256: str
    descriptor_provider: str
    descriptor_model: str
    descriptor_prompt_version: str
    configured_call_bound_seconds: float
    configured_dwell_seconds: float

    @pydantic.model_validator(mode="after")
    def _require_context(self) -> typing.Self:
        """Apply the sealed-envelope context grammar and the spec binding, in order."""
        if self.context_tick_monotonic > self.monotonic_seconds:
            raise ValueError("context tick follows the recording instant")
        text = self.context_canonical_json
        if len(text) > MAX_ADVISORY_CONTEXT_BYTES:
            raise ValueError("context too large")
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_ADVISORY_CONTEXT_BYTES or len(encoded) != self.context_byte_length:
            raise ValueError("context length mismatched")
        if hashlib.sha256(encoded).hexdigest() != self.context_sha256:
            raise ValueError("context digest mismatched")
        parsed: object = None
        admitted = False
        try:
            parsed = json.loads(text)
            walk_json_value(typing.cast(pydantic.JsonValue, parsed))
            admitted = True
        except (ValueError, RecursionError, ColdEvidenceError):
            pass
        if not admitted or type(parsed) is not dict:
            raise ValueError("context is not one canonical JSON object")
        context = typing.cast(dict[str, object], parsed)
        if _canonical_text(context) != text:
            raise ValueError("context is not one canonical JSON object")
        for key, expected in (
            ("profile_name", self.context_profile_name),
            ("target_drop_temp_c", self.context_target_drop_temp_c),
            ("charge_guidance_min_c", self.context_charge_guidance_min_c),
            ("charge_guidance_max_c", self.context_charge_guidance_max_c),
        ):
            if key not in context or type(context[key]) is not type(expected):
                raise ValueError("context spec mismatched")
            if context[key] != expected:
                raise ValueError("context spec mismatched")
        return self


_DECISION_FIELDS: tuple[str, ...] = ("requested_heat", "requested_fan", "should_drop", "confidence")
_EVALUATION_FIELDS: tuple[str, ...] = tuple(
    f"evaluation_{name}" for name in ColdSafetyEvaluation.model_fields
)
_USAGE_COUNT_FIELDS: tuple[str, ...] = (
    "usage_basis",
    "usage_input_tokens",
    "usage_output_tokens",
    "usage_total_tokens",
)
_USAGE_KINDS = frozenset(
    {ColdAdvisoryResolution.RETURNED_DECISION, ColdAdvisoryResolution.RETURNED_UNSAFE_OUTPUT}
)


class ColdAdvisoryResolutionRecord(_ColdAdvisoryAttemptBase):
    """One immutable resolution line closing the open attempt with the same index.

    ``invocation_*`` is the caller's actual invocation instant, present only when it
    is known (``INVOKED``); an interruption before invocation leaves it unknown.
    ``resolved_*`` is the return or raise instant, the abandonment instant, or the
    phase-end observation instant.  A decision, rationale, and evaluation appear
    only for a returned decision; usage only for a returned decision or unsafe
    output.  Usage counts are production-normalised: a zero is not provider-reported
    zero, and their freshness is not provable from these bytes.  Abandonment does
    not prove the provider call stopped.  Nothing here compares a timing to the
    configured bound or dwell, and nothing here qualifies a run.
    """

    entry: typing.Literal["resolution"]
    resolution: ColdAdvisoryResolution
    invocation_state: ColdAdvisoryInvocationState
    invocation_utc: str | None
    invocation_monotonic: float | None
    resolved_utc: str
    resolved_monotonic: float
    requested_heat: int | None
    requested_fan: int | None
    should_drop: bool | None
    confidence: float | None
    rationale_state: ColdAdvisoryRationaleState | None
    rationale: str | None
    evaluation_state: ColdAdvisoryEvaluationState | None
    evaluation_rule: str | None
    evaluation_verdict: ColdSafetyVerdict | None
    evaluation_input_heat: int | None
    evaluation_input_fan: int | None
    evaluation_adjusted_heat: int | None
    evaluation_adjusted_fan: int | None
    evaluation_reason: str | None
    usage_state: ColdAdvisoryUsageState | None
    usage_basis: ColdAdvisoryUsageBasis | None
    usage_input_tokens: int | None
    usage_output_tokens: int | None
    usage_total_tokens: int | None
    usage_reasoning_tokens: int | None

    @pydantic.model_validator(mode="after")
    def _require_presence_matrix(self) -> typing.Self:
        """Apply the closed per-resolution presence matrix and the in-record time order."""
        kind = self.resolution
        decision = kind is ColdAdvisoryResolution.RETURNED_DECISION
        invoked = self.invocation_state is ColdAdvisoryInvocationState.INVOKED
        if not invoked and kind is not ColdAdvisoryResolution.UNRESOLVED_AT_PHASE_END:
            raise ValueError("only an unresolved attempt may be not invoked")
        if (self.invocation_utc is not None) is not invoked or (
            self.invocation_monotonic is not None
        ) is not invoked:
            raise ValueError("invocation instants break the matrix")
        if any((getattr(self, name) is not None) is not decision for name in _DECISION_FIELDS):
            raise ValueError("decision fields break the matrix")
        if (self.rationale_state is not None) is not decision or (
            self.rationale is not None
        ) is not (self.rationale_state is ColdAdvisoryRationaleState.RETAINED):
            raise ValueError("rationale breaks the matrix")
        if (self.evaluation_state is not None) is not decision:
            raise ValueError("evaluation state breaks the matrix")
        if self.evaluation_state is ColdAdvisoryEvaluationState.RECORDED:
            evaluation = ColdSafetyEvaluation.model_validate(
                {
                    name: getattr(self, f"evaluation_{name}")
                    for name in ColdSafetyEvaluation.model_fields
                },
                strict=True,
            )
            if (
                evaluation.input_heat != self.requested_heat
                or evaluation.input_fan != self.requested_fan
            ):
                raise ValueError("evaluation input differs from the request")
        elif any(getattr(self, name) is not None for name in _EVALUATION_FIELDS):
            raise ValueError("evaluation fields break the matrix")
        if (self.usage_state is not None) is not (kind in _USAGE_KINDS):
            raise ValueError("usage state breaks the matrix")
        usage = self.usage_state is ColdAdvisoryUsageState.RECORDED
        if any((getattr(self, name) is not None) is not usage for name in _USAGE_COUNT_FIELDS) or (
            not usage and self.usage_reasoning_tokens is not None
        ):
            raise ValueError("usage fields break the matrix")
        if self.resolved_monotonic > self.monotonic_seconds or (
            self.invocation_monotonic is not None
            and self.invocation_monotonic > self.resolved_monotonic
        ):
            raise ValueError("instants out of order")
        return self


ColdAdvisoryAttemptRecord: typing.TypeAlias = (
    ColdAdvisoryIntentRecord | ColdAdvisoryResolutionRecord
)
_RECORD_CLASSES: tuple[type[ColdAdvisoryAttemptRecord], ...] = (
    ColdAdvisoryIntentRecord,
    ColdAdvisoryResolutionRecord,
)
ADMITTED_ENUM_TYPES: typing.Final[tuple[type[enum.Enum], ...]] = (
    ColdPhaseKind,
    ColdSafetyVerdict,
    ColdAdvisoryContextSource,
    ColdAdvisoryResolution,
    ColdAdvisoryInvocationState,
    ColdAdvisoryRationaleState,
    ColdAdvisoryEvaluationState,
    ColdAdvisoryUsageState,
    ColdAdvisoryUsageBasis,
)
_SCALAR_TYPES: tuple[type[object], ...] = (bool, int, float, str)


def _is_admitted_member(value: object) -> bool:
    """Whether a value is a real member of an admitted enum, found by identity."""
    for admitted in ADMITTED_ENUM_TYPES:
        if type(value) is admitted:
            return any(value is member for member in admitted)
    return False


def _raw_fields(model: pydantic.BaseModel) -> dict[str, object]:
    """Return a forged-proof copy of exactly one exact model's declared raw values.

    Raises:
        ValueError: If undeclared or missing state, a non-``str`` key, or a value
            that is not ``None``, an exact scalar, or a real admitted member appears.
    """
    data = object.__getattribute__(model, "__dict__")
    extra = object.__getattribute__(model, "__pydantic_extra__")
    if type(data) is not dict or extra is not None and (type(extra) is not dict or extra):
        raise ValueError("model carries undeclared state")
    raw = typing.cast(dict[object, object], data)
    if any(type(name) is not str for name in raw) or set(raw) != set(type(model).model_fields):
        raise ValueError("model field set is not exact")
    values: dict[str, object] = {}
    for name, value in raw.items():
        if not (value is None or type(value) in _SCALAR_TYPES or _is_admitted_member(value)):
            raise ValueError("model value type is not admitted")
        values[typing.cast(str, name)] = value
    return values


def validate_advisory_attempt_record(
    record: ColdAdvisoryAttemptRecord,
) -> ColdAdvisoryAttemptRecord:
    """Losslessly revalidate one advisory-attempt record and return a fresh snapshot.

    Subclasses, forged ``model_construct``/``model_copy`` instances, undeclared or
    missing state, foreign or fabricated enum members, and non-exact values are all
    refused before any model method runs.

    Args:
        record: One exact in-process intent or resolution record.

    Returns:
        A newly validated snapshot of the same class.

    Raises:
        ColdEvidenceError: ``RECORD_NOT_VALIDATED``; ``RECORD_TOO_LARGE`` for an
            over-long canonical line; or the shared walker's own member for a JSON
            bound breach.
    """
    try:
        record_class = next(item for item in _RECORD_CLASSES if type(record) is item)
        values = _raw_fields(record)
        view = {
            name: value.value if isinstance(value, enum.Enum) else value
            for name, value in values.items()
        }
        walk_json_value(typing.cast(pydantic.JsonValue, view))
        validated = record_class.model_validate(values, strict=True)
        line = _canonical_text(validated.model_dump(mode="json")).encode("utf-8")
        if len(line) > _RECORD_LINE_LIMIT:
            raise ColdEvidenceError(ColdEvidenceFailure.RECORD_TOO_LARGE)
    except ColdEvidenceError as error:
        failure = error
    except Exception:
        failure = ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    else:
        return validated
    raise failure


class ColdAdvisoryUsageReading(pydantic.BaseModel):
    """Admitted production-normalised token usage for one returned advisory call.

    Its field set equals the production usage reading.  Production maps absent
    provider counts to zero, so a zero here is not provider-reported zero, and
    nothing here can prove the reading is fresh rather than a previous call's.
    """

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    input_tokens: int
    output_tokens: int
    total_tokens: int
    reasoning_tokens: int | None

    @pydantic.field_validator(
        "input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", mode="before"
    )
    @classmethod
    def _require_count(cls, value: object) -> object:
        """Refuse a ``bool``, ``float``, negative, or non-``int`` instead of coercing it."""
        if value is None or (type(value) is int and value >= 0):
            return value
        raise ValueError("value must be an exact non-negative int")


_NONE_TYPE = type(None)


def _admit_raw(*pairs: tuple[object, tuple[type[object], ...]]) -> None:
    """Refuse any builder argument whose exact type is not admitted, before derivation."""
    for value, admitted in pairs:
        if not any(type(value) is kind for kind in admitted):
            raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)


def _admit_header(header: ColdRunHeader) -> ColdRunHeader:
    """Admit and snapshot the exact phase header a builder binds to."""
    if type(header) is not ColdRunHeader:
        raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    return typing.cast(ColdRunHeader, validate_record(header))


def _construct(
    record_class: type[ColdAdvisoryAttemptRecord], values: dict[str, object]
) -> ColdAdvisoryAttemptRecord:
    """Strictly construct one record, mapping a parser error to a closed failure."""
    try:
        record = record_class.model_validate(values, strict=True)
    except pydantic.ValidationError:
        failure = ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    else:
        return validate_advisory_attempt_record(record)
    raise failure


def _common(
    header: ColdRunHeader, attempt_index: int, recorded_at_utc: str, monotonic_seconds: float
) -> dict[str, object]:
    """Return the common header-bound fields of one line."""
    return {
        "schema_version": 2,
        "stream": "advisory_attempt",
        "run_id": header.run_id,
        "identity_sha256": header.identity_sha256,
        "phase": header.phase,
        "recorded_at_utc": recorded_at_utc,
        "monotonic_seconds": monotonic_seconds,
        "attempt_index": attempt_index,
    }


def build_advisory_intent_record(
    *,
    header: ColdRunHeader,
    attempt_index: int,
    recorded_at_utc: str,
    monotonic_seconds: float,
    context_tick: int,
    context_tick_monotonic: float,
    context: dict[str, object],
    profile_name: str,
    target_drop_temp_c: float,
    charge_guidance_min_c: float | None,
    charge_guidance_max_c: float | None,
    provider: str,
    model: str,
    prompt_version: str,
    configured_call_bound_seconds: float,
    configured_dwell_seconds: float,
) -> ColdAdvisoryIntentRecord:
    """Build one header-bound intent line from a retained tick's advisor context.

    The recording instant is an intent lower bound only and not proof that a
    provider request was issued.  ``context`` is an exact ``dict`` (the advisor
    context's ``mode="json"`` dump); it is walked before it is canonicalised, then
    its canonical text, byte length, and SHA-256 are derived.  The spec values are
    the operator's admitted spec as supplied, with no defaults.  Every raw argument
    type is admitted before any derivation.

    Args:
        header: The exact bound phase header; only its run, phase, and digest are used.
        attempt_index: This phase's next attempt index.
        recorded_at_utc: Append instant (UTC).
        monotonic_seconds: Append instant (monotonic).
        context_tick: Index of the retained tick the context was built from.
        context_tick_monotonic: That tick's monotonic instant.
        context: The advisor context as a JSON-shaped ``dict``.
        profile_name: Admitted spec profile name.
        target_drop_temp_c: Admitted spec target drop temperature (Celsius).
        charge_guidance_min_c: Admitted spec charge guidance minimum, or ``None``.
        charge_guidance_max_c: Admitted spec charge guidance maximum, or ``None``.
        provider: Advisor descriptor provider.
        model: Advisor descriptor model.
        prompt_version: Advisor descriptor prompt version.
        configured_call_bound_seconds: Configured per-call bound; recorded only.
        configured_dwell_seconds: Configured post-completion dwell; recorded only.

    Returns:
        A validated intent snapshot.

    Raises:
        ColdEvidenceError: ``RECORD_NOT_VALIDATED``, ``RECORD_TOO_LARGE``, or the
            shared walker's own member for a context bound breach.
    """
    snapshot = _admit_header(header)
    _admit_raw(
        (attempt_index, (int,)),
        (recorded_at_utc, (str,)),
        (monotonic_seconds, (float,)),
        (context_tick, (int,)),
        (context_tick_monotonic, (float,)),
        (context, (dict,)),
        (profile_name, (str,)),
        (target_drop_temp_c, (float,)),
        (charge_guidance_min_c, (float, _NONE_TYPE)),
        (charge_guidance_max_c, (float, _NONE_TYPE)),
        (provider, (str,)),
        (model, (str,)),
        (prompt_version, (str,)),
        (configured_call_bound_seconds, (float,)),
        (configured_dwell_seconds, (float,)),
    )
    walk_json_value(typing.cast(pydantic.JsonValue, context))
    text = _canonical_text(context)
    encoded = text.encode("utf-8")
    values = {
        **_common(snapshot, attempt_index, recorded_at_utc, monotonic_seconds),
        "entry": ColdAdvisoryEntry.INTENT.value,
        "context_source": ColdAdvisoryContextSource.RETAINED_TICK,
        "context_tick": context_tick,
        "context_tick_monotonic": context_tick_monotonic,
        "context_profile_name": profile_name,
        "context_target_drop_temp_c": target_drop_temp_c,
        "context_charge_guidance_min_c": charge_guidance_min_c,
        "context_charge_guidance_max_c": charge_guidance_max_c,
        "context_canonical_json": text,
        "context_byte_length": len(encoded),
        "context_sha256": hashlib.sha256(encoded).hexdigest(),
        "descriptor_provider": provider,
        "descriptor_model": model,
        "descriptor_prompt_version": prompt_version,
        "configured_call_bound_seconds": configured_call_bound_seconds,
        "configured_dwell_seconds": configured_dwell_seconds,
    }
    return typing.cast(ColdAdvisoryIntentRecord, _construct(ColdAdvisoryIntentRecord, values))


def _rationale_state(rationale: str) -> ColdAdvisoryRationaleState:
    """Classify a rationale for retention; the length check precedes any encoding."""
    if len(rationale) > MAX_TEXT_FIELD_BYTES:
        return ColdAdvisoryRationaleState.NOT_RETAINED_OVER_BOUND
    size = _utf8_size(rationale)
    if size is None:
        return ColdAdvisoryRationaleState.NOT_RETAINED_NOT_ENCODABLE
    if size > MAX_TEXT_FIELD_BYTES:
        return ColdAdvisoryRationaleState.NOT_RETAINED_OVER_BOUND
    return ColdAdvisoryRationaleState.RETAINED


def _is_exact_str(value: object) -> bool:
    """Whether a value is an exact ``str``."""
    return type(value) is str


def _is_exact_int(value: object) -> bool:
    """Whether a value is an exact ``int`` (never a ``bool``)."""
    return type(value) is int


def _is_optional_int(value: object) -> bool:
    """Whether a value is ``None`` or an exact ``int``."""
    return value is None or type(value) is int


def _is_verdict(value: object) -> bool:
    """Whether a value is a real safety-verdict member, found by identity."""
    return any(value is member for member in ColdSafetyVerdict)


_CarrierTable: typing.TypeAlias = tuple[
    type[pydantic.BaseModel], dict[str, typing.Callable[[object], bool]]
]
#: Closed per-field exact-type admission for each embedded carrier, checked before flattening.
_EVALUATION_CARRIER: _CarrierTable = (
    ColdSafetyEvaluation,
    {
        "rule": _is_exact_str,
        "verdict": _is_verdict,
        "input_heat": _is_optional_int,
        "input_fan": _is_optional_int,
        "adjusted_heat": _is_optional_int,
        "adjusted_fan": _is_optional_int,
        "reason": _is_exact_str,
    },
)
_USAGE_CARRIER: _CarrierTable = (
    ColdAdvisoryUsageReading,
    {
        "input_tokens": _is_exact_int,
        "output_tokens": _is_exact_int,
        "total_tokens": _is_exact_int,
        "reasoning_tokens": _is_optional_int,
    },
)


def _admit_carrier(carrier: object, table: _CarrierTable) -> dict[str, object]:
    """Admit one embedded carrier's exact class, raw state, and per-field exact types.

    Semantic checks (ranges, lengths, the request binding) stay with the strict
    record; this layer only refuses a carrier whose shape or raw types are not exact.

    Raises:
        ColdEvidenceError: ``RECORD_NOT_VALIDATED`` for any refused carrier.
    """
    carrier_class, admitted = table
    if type(carrier) is not carrier_class:
        raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    data = object.__getattribute__(carrier, "__dict__")
    extra = object.__getattribute__(carrier, "__pydantic_extra__")
    if type(data) is not dict or extra is not None and (type(extra) is not dict or extra):
        raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    raw = typing.cast(dict[object, object], data)
    if any(type(name) is not str for name in raw) or set(raw) != set(carrier_class.model_fields):
        raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    if not all(check(raw[name]) for name, check in admitted.items()):
        raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    return {name: raw[name] for name in admitted}


def build_advisory_resolution_record(
    *,
    header: ColdRunHeader,
    attempt_index: int,
    recorded_at_utc: str,
    monotonic_seconds: float,
    resolution: ColdAdvisoryResolution,
    invocation_utc: str | None,
    invocation_monotonic: float | None,
    resolved_utc: str,
    resolved_monotonic: float,
    requested_heat: int | None = None,
    requested_fan: int | None = None,
    should_drop: bool | None = None,
    confidence: float | None = None,
    rationale: str | None = None,
    evaluation: ColdSafetyEvaluation | None = None,
    usage: ColdAdvisoryUsageReading | None = None,
) -> ColdAdvisoryResolutionRecord:
    """Build one header-bound resolution line for the open attempt.

    Every supplied (non-``None``) argument is carried into the record; a payload
    the resolution kind forbids is then refused by validation, never dropped or
    fabricated.  The invocation state is ``INVOKED`` when an invocation instant is
    supplied and ``NOT_INVOKED`` when both are absent.  A rationale is never
    truncated: over ``MAX_TEXT_FIELD_BYTES`` (checked by length before encoding,
    then by UTF-8 size) or not encodable, it is not retained, its bounded state
    says why, and the levers, evaluation, and usage are kept.  The embedded
    evaluation and usage carriers are shape-checked exactly before flattening.

    Producer obligations for a later slice, not enforced here: take usage only from
    a post-call ``last_usage`` object that is not the pre-call object (otherwise
    record none); map a provider error to ``RETURNED_PROVIDER_ERROR``, malformed
    output to ``RETURNED_MALFORMED_OUTPUT``, unsafe output to
    ``RETURNED_UNSAFE_OUTPUT``, and any other ``Exception`` to
    ``RAISED_UNCLASSIFIED``; sample the invocation instant after the intent append
    returns and immediately before the await.

    Args:
        header: The exact bound phase header; only its run, phase, and digest are used.
        attempt_index: The open attempt's index.
        recorded_at_utc: Append instant (UTC).
        monotonic_seconds: Append instant (monotonic).
        resolution: How the attempt resolved.
        invocation_utc: Known invocation instant (UTC), or ``None`` if unknown.
        invocation_monotonic: Known invocation instant (monotonic), or ``None``.
        resolved_utc: Return, raise, abandonment, or phase-end instant (UTC).
        resolved_monotonic: The same instant (monotonic).
        requested_heat: Returned requested heat, if any.
        requested_fan: Returned requested fan, if any.
        should_drop: Returned drop request, if any.
        confidence: Returned confidence, if any.
        rationale: Returned rationale, if any.
        evaluation: Typed safety evaluation of the request, if any.
        usage: Admitted usage reading, if any.

    Returns:
        A validated resolution snapshot.

    Raises:
        ColdEvidenceError: ``RECORD_NOT_VALIDATED`` or ``RECORD_TOO_LARGE``.
    """
    snapshot = _admit_header(header)
    _admit_raw(
        (attempt_index, (int,)),
        (recorded_at_utc, (str,)),
        (monotonic_seconds, (float,)),
        (resolution, (ColdAdvisoryResolution,)),
        (invocation_utc, (str, _NONE_TYPE)),
        (invocation_monotonic, (float, _NONE_TYPE)),
        (resolved_utc, (str,)),
        (resolved_monotonic, (float,)),
        (requested_heat, (int, _NONE_TYPE)),
        (requested_fan, (int, _NONE_TYPE)),
        (should_drop, (bool, _NONE_TYPE)),
        (confidence, (float, _NONE_TYPE)),
        (rationale, (str, _NONE_TYPE)),
        (evaluation, (ColdSafetyEvaluation, _NONE_TYPE)),
        (usage, (ColdAdvisoryUsageReading, _NONE_TYPE)),
    )
    if not _is_admitted_member(resolution):
        raise ColdEvidenceError(ColdEvidenceFailure.RECORD_NOT_VALIDATED)
    evaluation_values = (
        None if evaluation is None else _admit_carrier(evaluation, _EVALUATION_CARRIER)
    )
    usage_values = None if usage is None else _admit_carrier(usage, _USAGE_CARRIER)
    decision = resolution is ColdAdvisoryResolution.RETURNED_DECISION
    absent_evaluation = ColdAdvisoryEvaluationState.NOT_RECORDED if decision else None
    absent_usage = ColdAdvisoryUsageState.NOT_RECORDED if resolution in _USAGE_KINDS else None
    rationale_state = None if rationale is None else _rationale_state(rationale)
    invoked = invocation_utc is not None or invocation_monotonic is not None
    values: dict[str, object] = {
        **{name: None for name in (*_EVALUATION_FIELDS, *_USAGE_COUNT_FIELDS)},
        "usage_reasoning_tokens": None,
        **_common(snapshot, attempt_index, recorded_at_utc, monotonic_seconds),
        "entry": ColdAdvisoryEntry.RESOLUTION.value,
        "resolution": resolution,
        "invocation_state": (
            ColdAdvisoryInvocationState.INVOKED
            if invoked
            else ColdAdvisoryInvocationState.NOT_INVOKED
        ),
        "invocation_utc": invocation_utc,
        "invocation_monotonic": invocation_monotonic,
        "resolved_utc": resolved_utc,
        "resolved_monotonic": resolved_monotonic,
        "requested_heat": requested_heat,
        "requested_fan": requested_fan,
        "should_drop": should_drop,
        "confidence": confidence,
        "rationale_state": rationale_state,
        "rationale": (
            rationale if rationale_state is ColdAdvisoryRationaleState.RETAINED else None
        ),
        "evaluation_state": (
            absent_evaluation if evaluation is None else ColdAdvisoryEvaluationState.RECORDED
        ),
        "usage_state": absent_usage if usage is None else ColdAdvisoryUsageState.RECORDED,
    }
    if evaluation_values is not None:
        values.update((f"evaluation_{name}", value) for name, value in evaluation_values.items())
    if usage_values is not None:
        values.update((f"usage_{name}", value) for name, value in usage_values.items())
        values["usage_basis"] = ColdAdvisoryUsageBasis.PRODUCTION_NORMALISED
    return typing.cast(
        ColdAdvisoryResolutionRecord, _construct(ColdAdvisoryResolutionRecord, values)
    )


_PHASE_ORDER: tuple[ColdPhaseKind, ...] = tuple(ColdPhaseKind)


class ColdAdvisorySequence:
    """Pure run-wide advisory-attempt order shared by the writer and the reader.

    Indices are contiguous per phase from 0; at most one attempt is open across the
    run; a resolution closes the open attempt; observed instants never regress and
    an invocation never precedes its intent; phases never regress and an open
    attempt never crosses a phase.  An open tail is retained data, never healthy.
    No window, count, or dwell rule applies here.
    """

    __slots__ = ("_last_observed", "_last_phase", "_next_index", "_open")

    def __init__(self) -> None:
        """Create empty ordering state."""
        self._next_index: dict[ColdPhaseKind, int] = {}
        self._open: tuple[ColdPhaseKind, int, float] | None = None
        self._last_phase: ColdPhaseKind | None = None
        self._last_observed: float | None = None

    @property
    def open_attempt(self) -> tuple[ColdPhaseKind, int] | None:
        """The open attempt's phase and index, or ``None``."""
        return None if self._open is None else (self._open[0], self._open[1])

    def next_index(self, phase: ColdPhaseKind) -> int:
        """Return the only index the next intent in ``phase`` may carry.

        Args:
            phase: The phase to query.

        Returns:
            The phase's next contiguous attempt index.
        """
        return self._next_index.get(phase, 0)

    def check(self, record: ColdAdvisoryAttemptRecord) -> None:
        """Refuse a record that would break the run-wide order; changes nothing.

        Args:
            record: A validated advisory-attempt snapshot.

        Raises:
            ColdAdvisoryAttemptError: With the first closed refusal that applies.
        """
        opened = self._open
        if self._last_phase is not None and _PHASE_ORDER.index(record.phase) < _PHASE_ORDER.index(
            self._last_phase
        ):
            raise ColdAdvisoryAttemptError(ColdAdvisoryAttemptFailure.PHASE_REGRESSED)
        if opened is not None and record.phase is not opened[0]:
            raise ColdAdvisoryAttemptError(ColdAdvisoryAttemptFailure.OPEN_ATTEMPT_CROSSES_PHASE)
        if isinstance(record, ColdAdvisoryIntentRecord):
            if opened is not None:
                raise ColdAdvisoryAttemptError(ColdAdvisoryAttemptFailure.ATTEMPT_ALREADY_OPEN)
            if record.attempt_index != self.next_index(record.phase):
                raise ColdAdvisoryAttemptError(
                    ColdAdvisoryAttemptFailure.ATTEMPT_INDEX_NOT_CONTIGUOUS
                )
            if self._last_observed is not None and record.monotonic_seconds < self._last_observed:
                raise ColdAdvisoryAttemptError(ColdAdvisoryAttemptFailure.OBSERVED_TIME_REGRESSED)
            return
        if opened is None:
            raise ColdAdvisoryAttemptError(
                ColdAdvisoryAttemptFailure.RESOLUTION_WITHOUT_OPEN_ATTEMPT
            )
        if record.attempt_index != opened[1]:
            raise ColdAdvisoryAttemptError(ColdAdvisoryAttemptFailure.RESOLUTION_NOT_MATCHING)
        first = (
            record.resolved_monotonic
            if record.invocation_monotonic is None
            else record.invocation_monotonic
        )
        if first < opened[2]:
            raise ColdAdvisoryAttemptError(ColdAdvisoryAttemptFailure.OBSERVED_TIME_REGRESSED)

    def commit(self, record: ColdAdvisoryAttemptRecord) -> None:
        """Advance the state past one checked record.

        Args:
            record: The record most recently accepted by :meth:`check`.
        """
        if isinstance(record, ColdAdvisoryIntentRecord):
            self._open = (record.phase, record.attempt_index, record.monotonic_seconds)
            self._next_index[record.phase] = record.attempt_index + 1
        else:
            self._open = None
        self._last_phase = record.phase
        self._last_observed = record.monotonic_seconds
