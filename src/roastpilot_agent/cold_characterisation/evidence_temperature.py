"""Closed, versioned per-tick temperature evidence for cold characterisation.

This module is pure: it defines the per-stream ``schema_version`` 4 record written to
``records/<phase>/tick_temperature.jsonl``, its in-process content re-admission, its
retained-document decoder, and pure positional pairing with the v1 tick stream.  It
performs no I/O, reads no clock, calls no provider, and decides nothing about a run.

The record retains one tick's closed version-1 temperature projection beside the
identity values that pair it with exactly one retained v1 tick.  It applies no
temperature envelope, freshness, liveness, or readiness rule, carries no session
identity (the paired tick retains it), and never qualifies a run.  Agreement inside
the projection is consistency between raw and typed values, not sensor
corroboration, and ``observed`` does not imply liveness.

Honest limits: the tick line and its temperature line are two separate durable
writes, never a transaction; pairing is positional and proves consistency of the
identity values only; re-admission proves content, never provenance.
"""

import collections.abc
import enum
import json
import math
import typing

import pydantic

from roastpilot_agent.cold_characterisation.evidence_schema import (
    MAX_RECORD_BYTES,
    MAX_TEXT_FIELD_BYTES,
    ColdEvidenceError,
    ColdEvidenceFailure,
    ColdPhaseKind,
    ColdRunHeader,
    ColdTickRecord,
    walk_json_value,
)
from roastpilot_agent.cold_characterisation.temperature_projection import (
    ColdTickTemperatureProjection,
    admit_cold_temperature_projection,
    readmit_cold_temperature_projection,
)

#: The per-stream record schema version of the tick-temperature stream.
TICK_TEMPERATURE_SCHEMA_VERSION: typing.Final = 4
#: The exact stream token of the tick-temperature stream.
TICK_TEMPERATURE_STREAM: typing.Final = "tick_temperature"
#: The exact per-phase file name of the tick-temperature stream.
TICK_TEMPERATURE_FILE_NAME: typing.Final = "tick_temperature.jsonl"

_RUN_ID_ADAPTER: pydantic.TypeAdapter[str] = pydantic.TypeAdapter(
    typing.Annotated[str, *ColdRunHeader.model_fields["run_id"].metadata]
)
_DIGEST_ADAPTER: pydantic.TypeAdapter[str] = pydantic.TypeAdapter(
    typing.Annotated[str, *ColdRunHeader.model_fields["identity_sha256"].metadata]
)
_PHASES_BY_VALUE: typing.Final[dict[str, ColdPhaseKind]] = {
    phase.value: phase for phase in ColdPhaseKind
}
_SCALAR_TYPES: typing.Final[tuple[type[object], ...]] = (int, float, str)


class ColdTickTemperatureEvidenceState(enum.Enum):
    """Whether a retained run carries tick-temperature evidence.

    ``ABSENT`` means only that no tick-temperature line is retained.  It does not
    prove historical origin: a new tree whose files are missing, or a stripped and
    re-sealed tree, also reads ``ABSENT``.  ``PRESENT`` means only that full
    positional pairing with the retained v1 ticks holds.  Neither member is
    screening, provenance, or health.
    """

    ABSENT = "absent"
    PRESENT = "present"


class ColdTickTemperatureFailure(enum.Enum):
    """Closed tick-temperature ordering and pairing refusals."""

    PHASE_NOT_LATEST = "phase_not_latest"
    TICK_NOT_RETAINED = "tick_not_retained"
    TEMPERATURE_DUPLICATED = "temperature_duplicated"
    PAIRING_MISMATCHED = "pairing_mismatched"
    STREAM_EMPTY = "stream_empty"


class ColdTickTemperatureError(Exception):
    """Closed tick-temperature error with a fixed message and no input content."""

    failure: ColdTickTemperatureFailure

    def __init__(self, failure: ColdTickTemperatureFailure) -> None:
        """Create a content-free tick-temperature refusal.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold tick temperature evidence refused.")
        self.failure = failure


def _grammar_admits(adapter: pydantic.TypeAdapter[str], value: str) -> bool:
    """Whether one exact string satisfies a delivered v1 header grammar."""
    try:
        adapter.validate_python(value, strict=True)
    except pydantic.ValidationError:
        return False
    return True


def _is_phase(value: object) -> bool:
    """Whether a value is a real ``ColdPhaseKind`` member, found by identity."""
    return type(value) is ColdPhaseKind and any(value is member for member in ColdPhaseKind)


class ColdTickTemperatureRecord(pydantic.BaseModel):
    """One flat, closed ``schema_version`` 4 tick-temperature record.

    It pairs one retained v1 tick (run, phase, identity digest, tick index, and the
    tick's recorded time values) with that tick's closed temperature projection.  It
    records and decides nothing, and it never qualifies a run.

    Direct construction may raise a ``ValidationError`` that embeds its input; no
    containment claim is made for it.  The public boundaries are
    :func:`validate_tick_temperature_record` and
    :func:`decode_tick_temperature_document`.
    """

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )

    schema_version: typing.Literal[4]
    stream: typing.Literal["tick_temperature"]
    run_id: str
    phase: ColdPhaseKind
    recorded_at_utc: str = pydantic.Field(max_length=MAX_TEXT_FIELD_BYTES)
    monotonic_seconds: float
    identity_sha256: str
    tick: int
    temperature: ColdTickTemperatureProjection

    @pydantic.field_validator("schema_version", mode="before")
    @classmethod
    def _require_version(cls, value: object) -> object:
        """Admit only the exact integer version 4."""
        if type(value) is int and value == TICK_TEMPERATURE_SCHEMA_VERSION:
            return value
        raise ValueError("schema version is not admitted")

    @pydantic.field_validator(
        "stream", "run_id", "recorded_at_utc", "identity_sha256", mode="before"
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

    @pydantic.field_validator("phase", mode="before")
    @classmethod
    def _require_phase(cls, value: object) -> object:
        """Admit only a real phase member, refusing raw strings and fabricated members."""
        if _is_phase(value):
            return value
        raise ValueError("phase is not a real member")

    @pydantic.field_validator("monotonic_seconds", mode="before")
    @classmethod
    def _require_finite_float(cls, value: object) -> object:
        """Admit only an exact finite ``float``, refusing an ``int`` instead of coercing it."""
        if type(value) is float and math.isfinite(value):
            return value
        raise ValueError("monotonic seconds must be an exact finite float")

    @pydantic.field_validator("tick", mode="before")
    @classmethod
    def _require_tick(cls, value: object) -> object:
        """Admit only an exact non-negative ``int`` (never a ``bool`` or ``float``)."""
        if type(value) is int and value >= 0:
            return value
        raise ValueError("tick must be an exact non-negative int")

    @pydantic.field_validator("temperature", mode="before")
    @classmethod
    def _readmit_temperature(cls, value: object) -> object:
        """Re-admit the projection's content and return a fresh validated projection."""
        fresh = readmit_cold_temperature_projection(value)
        if type(fresh) is ColdTickTemperatureProjection:
            return fresh
        raise ValueError("temperature projection is not admitted")


_FIELD_NAMES: typing.Final[tuple[str, ...]] = tuple(ColdTickTemperatureRecord.model_fields)
_FIELD_NAME_SET: typing.Final[frozenset[str]] = frozenset(_FIELD_NAMES)


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


def _raw_fields(record: object) -> dict[str, object] | None:
    """Return a copy of exactly the declared raw field values, or ``None``.

    ``None`` is returned for missing or undeclared state, a non-``str`` key, or a
    value that is not an exact ``int``, ``float``, or ``str``, a real phase member,
    or an exact projection instance.
    """
    try:
        data: object = object.__getattribute__(record, "__dict__")
        extra: object = object.__getattribute__(record, "__pydantic_extra__")
    except AttributeError:
        return None
    if type(data) is not dict:
        return None
    if extra is not None and (type(extra) is not dict or extra):
        return None
    state = typing.cast(dict[object, object], data)
    for key in state:
        if type(key) is not str:
            return None
    if frozenset(typing.cast(dict[str, object], state)) != _FIELD_NAME_SET:
        return None
    values: dict[str, object] = {}
    for name in _FIELD_NAMES:
        value = state[name]
        if not (
            type(value) in _SCALAR_TYPES
            or _is_phase(value)
            or type(value) is ColdTickTemperatureProjection
        ):
            return None
        values[name] = value
    return values


def _snapshot(record: object) -> ColdTickTemperatureRecord | ColdEvidenceFailure:
    """Re-admit one record's content, returning a fresh snapshot or a closed failure.

    Raises:
        ColdEvidenceError: The shared walker's own member for a JSON bound breach.
        pydantic.ValidationError: If strict model validation refuses the values.
    """
    if type(record) is not ColdTickTemperatureRecord:
        return ColdEvidenceFailure.RECORD_NOT_VALIDATED
    values = _raw_fields(record)
    if values is None:
        return ColdEvidenceFailure.RECORD_NOT_VALIDATED
    fresh = readmit_cold_temperature_projection(values["temperature"])
    phase = values["phase"]
    if type(fresh) is not ColdTickTemperatureProjection or not _is_phase(phase):
        return ColdEvidenceFailure.RECORD_NOT_VALIDATED
    view: dict[str, object] = dict(values)
    view["phase"] = typing.cast(ColdPhaseKind, phase).value
    view["temperature"] = fresh.model_dump(mode="json")
    walk_json_value(typing.cast(pydantic.JsonValue, view))
    recorded = values["recorded_at_utc"]
    # The walker has already encoded every admitted string, so this cannot fail.
    if type(recorded) is str and len(recorded.encode("utf-8")) > MAX_TEXT_FIELD_BYTES:
        return ColdEvidenceFailure.TEXT_FIELD_TOO_LARGE
    validated = ColdTickTemperatureRecord.model_validate(
        {**values, "temperature": fresh}, strict=True
    )
    if len(_canonical_json(validated.model_dump(mode="json")).encode("utf-8")) > MAX_RECORD_BYTES:
        return ColdEvidenceFailure.RECORD_TOO_LARGE
    return validated


def validate_tick_temperature_record(record: object) -> ColdTickTemperatureRecord:
    """Re-admit one record's content and return a freshly validated snapshot.

    This is the shared write and read boundary.  Subclasses, undeclared or missing
    state, non-``str`` keys, foreign or fabricated phase members, and non-exact
    values are refused.  The nested projection is re-admitted through
    ``readmit_cold_temperature_projection``.  The admitted values (the phase and
    projection rendered as JSON) are walked by the shared JSON walker, the recorded
    time is held to the shared UTF-8 text-field byte limit, the values are strictly
    validated, and the canonical record is held to the shared record byte cap.

    Honest limits: this re-admits content; it cannot prove provenance.  An exact
    class instance built by ``model_construct`` or ``model_copy`` whose content
    would validate is admitted as a fresh snapshot.

    Args:
        record: Any in-process candidate record.

    Returns:
        A newly validated snapshot (never the input instance).

    Raises:
        ColdEvidenceError: ``RECORD_NOT_VALIDATED``, ``TEXT_FIELD_TOO_LARGE``,
            ``RECORD_TOO_LARGE``, or the shared walker's own member.  The error
            carries no input content, cause, or context.
    """
    try:
        result = _snapshot(record)
    except ColdEvidenceError as error:
        result = error.failure
    except (ArithmeticError, AttributeError, LookupError, TypeError, ValueError):
        # A ValidationError embeds input values, so it is discarded, never chained.
        result = ColdEvidenceFailure.RECORD_NOT_VALIDATED
    if type(result) is ColdEvidenceFailure:
        raise ColdEvidenceError(result)
    return typing.cast(ColdTickTemperatureRecord, result)


def decode_tick_temperature_document(document: object) -> ColdTickTemperatureRecord | None:
    """Strictly decode one JSON-derived retained document, or return ``None``.

    The domain is the values ``load_strict_json`` can produce; for them the function
    is total and never raises.  Exact guards run first: an exact ``dict`` with exact
    ``str`` keys equal to the nine field names, an exact phase token converted by
    exact table lookup, and a projection admitted by
    ``admit_cold_temperature_projection``.  Strict model validation then applies the
    remaining grammar.  No normalisation is applied, and ``None`` carries no detail.

    Args:
        document: One JSON-derived value.

    Returns:
        The strictly validated record, or ``None`` if any rule refuses it.
    """
    if type(document) is not dict:
        return None
    mapping = typing.cast(dict[object, object], document)
    for key in mapping:
        if type(key) is not str:
            return None
    fields = typing.cast(dict[str, object], mapping)
    if frozenset(fields) != _FIELD_NAME_SET:
        return None
    token = fields["phase"]
    phase = _PHASES_BY_VALUE.get(token) if type(token) is str else None
    if phase is None:
        return None
    temperature = admit_cold_temperature_projection(fields["temperature"])
    if type(temperature) is not ColdTickTemperatureProjection:
        return None
    try:
        decoded: ColdTickTemperatureRecord | None = ColdTickTemperatureRecord.model_validate(
            {**fields, "phase": phase, "temperature": temperature}, strict=True
        )
    except pydantic.ValidationError:
        # The error embeds input values, so it is discarded, never re-raised.
        decoded = None
    return decoded


def pairs_with(tick: ColdTickRecord, temperature: ColdTickTemperatureRecord) -> bool:
    """Whether one temperature record carries exactly one tick's identity values.

    The run id, phase, identity digest, tick index, recorded time, and monotonic
    seconds must all be equal.  Equality is consistency of retained values only.

    Args:
        tick: One retained v1 tick snapshot.
        temperature: One tick-temperature snapshot.

    Returns:
        ``True`` only if every paired value is equal.
    """
    return (
        tick.run_id == temperature.run_id
        and tick.phase is temperature.phase
        and tick.identity_sha256 == temperature.identity_sha256
        and tick.tick == temperature.tick
        and tick.recorded_at_utc == temperature.recorded_at_utc
        and tick.monotonic_seconds == temperature.monotonic_seconds
    )


def check_tick_temperature_pairing(
    ticks: collections.abc.Sequence[ColdTickRecord],
    temperatures: collections.abc.Sequence[ColdTickTemperatureRecord],
) -> None:
    """Refuse unless one phase's temperature records pair one-to-one, in order, with its ticks.

    Args:
        ticks: The phase's retained v1 ticks in file order.
        temperatures: The phase's tick-temperature records in file order.

    Raises:
        ColdTickTemperatureError: ``PAIRING_MISMATCHED`` on a length difference or
            any positional pair that does not satisfy :func:`pairs_with`.
    """
    if len(ticks) != len(temperatures) or not all(
        pairs_with(tick, temperature) for tick, temperature in zip(ticks, temperatures, strict=True)
    ):
        raise ColdTickTemperatureError(ColdTickTemperatureFailure.PAIRING_MISMATCHED)
