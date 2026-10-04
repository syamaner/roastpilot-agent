"""Closed version-1 admission of the per-tick cold temperature projection.

This pure leaf owns the closed vocabulary, the shared rule predicates, the
strict model, the total raw-admission function, and the in-process content
re-admission function for the temperature projection object a cold MCP tick
carries.  It imports no project, transport, or I/O module.  The cold MCP adapter
alone reads the raw tick tree; the versioned tick-temperature evidence module
re-admits already constructed projections and admits retained JSON documents.

Admission records and decides nothing.  It applies no temperature envelope,
freshness, liveness, or readiness policy: every admitted outcome, unit,
agreement, and value is data whose meaning belongs to later policy.  Agreement
is consistency between the raw and typed values, not independent sensor
corroboration, and ``observed`` does not imply liveness.
"""

import math
from collections.abc import Callable, Mapping
from enum import Enum
from typing import Final, TypeVar, cast

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

_MemberT = TypeVar("_MemberT", bound=Enum)

#: The only admitted projection version.
_PROJECTION_VERSION: Final = 1
#: The largest admitted counter (the largest exactly representable JSON integer).
_MAX_COUNTER: Final = 2**53 - 1
#: The largest admitted raw last-packet temperature (an unsigned 16-bit reading).
_MAX_LAST_PACKET_TEMP: Final = 65535.0


class ColdTemperatureOutcome(Enum):
    """Closed outcomes of one cold temperature projection."""

    OBSERVED = "observed"
    AWAITING_FIRST_PACKET = "awaiting_first_packet"
    NOT_ELIGIBLE = "not_eligible"
    UNSUPPORTED = "unsupported"
    MALFORMED = "malformed"


class ColdTemperatureConfiguredUnit(Enum):
    """Closed configured temperature units."""

    CELSIUS = "celsius"
    FAHRENHEIT = "fahrenheit"
    AUTO = "auto"


class ColdTemperatureReportedUnit(Enum):
    """Closed temperature units reported by the latest status packet."""

    CELSIUS = "celsius"
    FAHRENHEIT = "fahrenheit"
    UNKNOWN = "unknown"


class ColdTemperatureAgreement(Enum):
    """Closed raw/typed value agreement tokens (consistency, not corroboration)."""

    AGREE = "agree"
    DISAGREE = "disagree"
    INDETERMINATE = "indeterminate"


class ColdTemperatureProjectionFailure(Enum):
    """Closed reasons a raw temperature projection fails admission, in precedence order.

    ``PROJECTION_KEY_MISSING`` is reported only by the cold MCP adapter; this
    module's admission function never returns it.
    """

    PROJECTION_KEY_MISSING = "projection_key_missing"
    PROJECTION_NULL = "projection_null"
    PROJECTION_NOT_OBJECT = "projection_not_object"
    VERSION_NOT_ADMITTED = "version_not_admitted"
    FIELD_SET_MISMATCH = "field_set_mismatch"
    OUTCOME_NOT_ADMITTED = "outcome_not_admitted"
    VALUE_TYPE_NOT_EXACT = "value_type_not_exact"
    TOKEN_NOT_ADMITTED = "token_not_admitted"
    VALUE_NOT_ADMITTED = "value_not_admitted"
    SHAPE_INCONSISTENT = "shape_inconsistent"


_OUTCOME_TOKENS: Final[dict[str, ColdTemperatureOutcome]] = {
    member.value: member for member in ColdTemperatureOutcome
}
_CONFIGURED_UNIT_TOKENS: Final[dict[str, ColdTemperatureConfiguredUnit]] = {
    member.value: member for member in ColdTemperatureConfiguredUnit
}
_REPORTED_UNIT_TOKENS: Final[dict[str, ColdTemperatureReportedUnit]] = {
    member.value: member for member in ColdTemperatureReportedUnit
}
_AGREEMENT_TOKENS: Final[dict[str, ColdTemperatureAgreement]] = {
    member.value: member for member in ColdTemperatureAgreement
}

#: The exact projection field names, in canonical order.
FIELD_NAMES: Final[tuple[str, ...]] = (
    "projection_version",
    "outcome",
    "configured_temperature_unit",
    "reported_temperature_unit",
    "last_packet_valid",
    "last_packet_bean_temp_c",
    "last_packet_env_temp_c",
    "retained_bean_temp_c",
    "retained_env_temp_c",
    "value_agreement",
    "status_packet_count",
    "ignored_temperature_packet_count",
    "status_read_error_count",
    "command_loop_error_count",
)
_FIELD_NAME_SET: Final[frozenset[str]] = frozenset(FIELD_NAMES)

#: The twelve value fields (all but version and outcome) with their exact
#: non-null raw type, in canonical order.
_VALUE_FIELD_TYPES: Final[tuple[tuple[str, type[object]], ...]] = (
    ("configured_temperature_unit", str),
    ("reported_temperature_unit", str),
    ("last_packet_valid", bool),
    ("last_packet_bean_temp_c", float),
    ("last_packet_env_temp_c", float),
    ("retained_bean_temp_c", float),
    ("retained_env_temp_c", float),
    ("value_agreement", str),
    ("status_packet_count", int),
    ("ignored_temperature_packet_count", int),
    ("status_read_error_count", int),
    ("command_loop_error_count", int),
)

_EMPTY_OUTCOMES: Final = frozenset(
    {
        ColdTemperatureOutcome.NOT_ELIGIBLE,
        ColdTemperatureOutcome.UNSUPPORTED,
        ColdTemperatureOutcome.MALFORMED,
    }
)
_VALID_REPORTED_UNITS: Final = frozenset(
    {ColdTemperatureReportedUnit.CELSIUS, ColdTemperatureReportedUnit.FAHRENHEIT}
)
_EXPLICIT_UNIT_PAIRS: Final = frozenset(
    {
        (ColdTemperatureConfiguredUnit.CELSIUS, ColdTemperatureReportedUnit.CELSIUS),
        (ColdTemperatureConfiguredUnit.FAHRENHEIT, ColdTemperatureReportedUnit.FAHRENHEIT),
    }
)


def _is_exact_version(value: object) -> bool:
    """Whether a value is exactly the integer projection version 1."""
    return type(value) is int and value == _PROJECTION_VERSION


def _is_counter(value: object) -> bool:
    """Whether a value is an exact non-negative integer no larger than 2**53-1."""
    return type(value) is int and 0 <= value <= _MAX_COUNTER


def _is_last_packet_temp(value: object) -> bool:
    """Whether a value is an exact, finite, integral float within 0.0..65535.0."""
    return (
        type(value) is float
        and math.isfinite(value)
        and value.is_integer()
        and 0.0 <= value <= _MAX_LAST_PACKET_TEMP
    )


def _is_retained_temp(value: object) -> bool:
    """Whether a value is an exact finite float (no range is applied at admission)."""
    return type(value) is float and math.isfinite(value)


def _is_optional_bool(value: object) -> bool:
    """Whether a value is ``None`` or an exact ``bool``."""
    return value is None or type(value) is bool


#: The eight numeric value fields with their shared value predicate, in canonical order.
_NUMERIC_PREDICATES: Final[tuple[tuple[str, Callable[[object], bool]], ...]] = (
    ("last_packet_bean_temp_c", _is_last_packet_temp),
    ("last_packet_env_temp_c", _is_last_packet_temp),
    ("retained_bean_temp_c", _is_retained_temp),
    ("retained_env_temp_c", _is_retained_temp),
    ("status_packet_count", _is_counter),
    ("ignored_temperature_packet_count", _is_counter),
    ("status_read_error_count", _is_counter),
    ("command_loop_error_count", _is_counter),
)


def _shape_holds(
    *,
    outcome: ColdTemperatureOutcome,
    configured: ColdTemperatureConfiguredUnit | None,
    reported: ColdTemperatureReportedUnit | None,
    last_packet_valid: bool | None,
    last_bean: float | None,
    last_env: float | None,
    retained_bean: float | None,
    retained_env: float | None,
    agreement: ColdTemperatureAgreement | None,
    status: int | None,
    ignored: int | None,
    read_errors: int | None,
    loop_errors: int | None,
) -> bool:
    """Whether member-form values satisfy shape rules SH1 to SH6.

    The outcome is branched on before ``last_packet_valid``, because both
    ordinary startup absence and an all-ignored latest packet carry ``False``.

    Returns:
        ``True`` only for a shape the version-1 projection can carry.
    """
    values = (
        configured,
        reported,
        last_packet_valid,
        last_bean,
        last_env,
        retained_bean,
        retained_env,
        agreement,
        status,
        ignored,
        read_errors,
        loop_errors,
    )
    if outcome in _EMPTY_OUTCOMES:  # SH1
        return all(value is None for value in values)
    if outcome is ColdTemperatureOutcome.AWAITING_FIRST_PACKET:  # SH2
        return (
            configured is not None
            and reported is None
            and last_packet_valid is False
            and last_bean is None
            and last_env is None
            and retained_bean is None
            and retained_env is None
            and agreement is ColdTemperatureAgreement.INDETERMINATE
            and status == 0
            and ignored == 0
            and read_errors is not None
            and loop_errors is not None
        )
    # SH3: OBSERVED is the only remaining closed outcome.
    if (
        configured is None
        or reported is None
        or last_packet_valid is None
        or agreement is None
        or status is None
        or ignored is None
        or read_errors is None
        or loop_errors is None
    ):
        return False
    if status < 1 or ignored > status:
        return False
    if (retained_bean is None) != (retained_env is None):
        return False
    if (retained_bean is not None) != (ignored < status):
        return False
    if reported in _VALID_REPORTED_UNITS:  # SH4
        if last_packet_valid is not True or ignored >= status:
            return False
        if (  # SH6
            configured is not ColdTemperatureConfiguredUnit.AUTO
            and (configured, reported) not in _EXPLICIT_UNIT_PAIRS
        ):
            return False
        if reported is ColdTemperatureReportedUnit.CELSIUS:  # SH4c
            if last_bean is None or last_env is None:
                return False
            agrees = last_bean == retained_bean and last_env == retained_env
            expected = (
                ColdTemperatureAgreement.AGREE if agrees else ColdTemperatureAgreement.DISAGREE
            )
            return agreement is expected
        return (  # SH4f
            last_bean is None
            and last_env is None
            and agreement is ColdTemperatureAgreement.INDETERMINATE
        )
    return (  # SH5: reported UNKNOWN
        last_packet_valid is False
        and last_bean is None
        and last_env is None
        and agreement is ColdTemperatureAgreement.INDETERMINATE
        and ignored >= 1
    )


class ColdTickTemperatureProjection(BaseModel):
    """Strict version-1 cold temperature projection admitted from one MCP tick.

    It records and decides nothing: no envelope, freshness, liveness, or
    readiness policy is applied.  Direct construction enforces the whole
    per-field grammar and every shape rule; production constructs it only
    through :func:`admit_cold_temperature_projection`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    projection_version: int
    outcome: ColdTemperatureOutcome
    configured_temperature_unit: ColdTemperatureConfiguredUnit | None
    reported_temperature_unit: ColdTemperatureReportedUnit | None
    last_packet_valid: bool | None
    last_packet_bean_temp_c: float | None
    last_packet_env_temp_c: float | None
    retained_bean_temp_c: float | None
    retained_env_temp_c: float | None
    value_agreement: ColdTemperatureAgreement | None
    status_packet_count: int | None
    ignored_temperature_packet_count: int | None
    status_read_error_count: int | None
    command_loop_error_count: int | None

    @field_validator("projection_version", mode="before")
    @classmethod
    def _admit_version(cls, value: object) -> object:
        """Admit only the exact integer version 1."""
        if not _is_exact_version(value):
            raise ValueError("projection version is not admitted")
        return value

    @field_validator("outcome", mode="before")
    @classmethod
    def _admit_outcome(cls, value: object) -> object:
        """Admit only an exact outcome member."""
        if type(value) is not ColdTemperatureOutcome:
            raise ValueError("outcome is not an exact member")
        return value

    @field_validator("configured_temperature_unit", mode="before")
    @classmethod
    def _admit_configured(cls, value: object) -> object:
        """Admit only ``None`` or an exact configured-unit member."""
        if value is not None and type(value) is not ColdTemperatureConfiguredUnit:
            raise ValueError("configured unit is not an exact member")
        return value

    @field_validator("reported_temperature_unit", mode="before")
    @classmethod
    def _admit_reported(cls, value: object) -> object:
        """Admit only ``None`` or an exact reported-unit member."""
        if value is not None and type(value) is not ColdTemperatureReportedUnit:
            raise ValueError("reported unit is not an exact member")
        return value

    @field_validator("value_agreement", mode="before")
    @classmethod
    def _admit_agreement(cls, value: object) -> object:
        """Admit only ``None`` or an exact agreement member."""
        if value is not None and type(value) is not ColdTemperatureAgreement:
            raise ValueError("agreement is not an exact member")
        return value

    @field_validator("last_packet_valid", mode="before")
    @classmethod
    def _admit_valid(cls, value: object) -> object:
        """Admit only ``None`` or an exact bool."""
        if not _is_optional_bool(value):
            raise ValueError("last packet validity is not an exact bool")
        return value

    @field_validator("last_packet_bean_temp_c", "last_packet_env_temp_c", mode="before")
    @classmethod
    def _admit_last_packet_temp(cls, value: object) -> object:
        """Admit only ``None`` or an exact integral float within the raw range."""
        if value is not None and not _is_last_packet_temp(value):
            raise ValueError("last packet temperature is not admitted")
        return value

    @field_validator("retained_bean_temp_c", "retained_env_temp_c", mode="before")
    @classmethod
    def _admit_retained_temp(cls, value: object) -> object:
        """Admit only ``None`` or an exact finite float."""
        if value is not None and not _is_retained_temp(value):
            raise ValueError("retained temperature is not admitted")
        return value

    @field_validator(
        "status_packet_count",
        "ignored_temperature_packet_count",
        "status_read_error_count",
        "command_loop_error_count",
        mode="before",
    )
    @classmethod
    def _admit_counter(cls, value: object) -> object:
        """Admit only ``None`` or an exact bounded non-negative integer."""
        if value is not None and not _is_counter(value):
            raise ValueError("counter is not admitted")
        return value

    @model_validator(mode="after")
    def _require_consistent_shape(self) -> "ColdTickTemperatureProjection":
        """Require one shape the version-1 projection can carry.

        Returns:
            This validated projection.

        Raises:
            ValueError: If the values violate any of the shape rules.
        """
        if not _shape_holds(
            outcome=self.outcome,
            configured=self.configured_temperature_unit,
            reported=self.reported_temperature_unit,
            last_packet_valid=self.last_packet_valid,
            last_bean=self.last_packet_bean_temp_c,
            last_env=self.last_packet_env_temp_c,
            retained_bean=self.retained_bean_temp_c,
            retained_env=self.retained_env_temp_c,
            agreement=self.value_agreement,
            status=self.status_packet_count,
            ignored=self.ignored_temperature_packet_count,
            read_errors=self.status_read_error_count,
            loop_errors=self.command_loop_error_count,
        ):
            raise ValueError("temperature projection shape is inconsistent")
        return self


def _first_value_failure(fields: dict[str, object]) -> ColdTemperatureProjectionFailure | None:
    """Return the first value-field failure (type, then token, then value), or ``None``."""
    for name, expected in _VALUE_FIELD_TYPES:
        value = fields[name]
        if value is not None and type(value) is not expected:
            return ColdTemperatureProjectionFailure.VALUE_TYPE_NOT_EXACT
    for name, table in (
        ("configured_temperature_unit", _CONFIGURED_UNIT_TOKENS),
        ("reported_temperature_unit", _REPORTED_UNIT_TOKENS),
        ("value_agreement", _AGREEMENT_TOKENS),
    ):
        token = fields[name]
        if token is not None and token not in table:
            return ColdTemperatureProjectionFailure.TOKEN_NOT_ADMITTED
    for name, predicate in _NUMERIC_PREDICATES:
        value = fields[name]
        if value is not None and not predicate(value):
            return ColdTemperatureProjectionFailure.VALUE_NOT_ADMITTED
    return None


def _member(table: Mapping[str, _MemberT], token: object) -> _MemberT | None:
    """Convert one already-admitted exact token (or ``None``) by exact table lookup."""
    if type(token) is str:
        return table[token]
    return None


def admit_cold_temperature_projection(
    raw: object,
) -> ColdTickTemperatureProjection | ColdTemperatureProjectionFailure:
    """Admit one raw JSON-derived temperature projection, or return its closed failure.

    The function never raises for a JSON-derived value and never renders its
    input.  Exact-type checks precede every hash, comparison, or arithmetic use
    of an input value, so a ``dict`` subclass or a non-``str`` key is refused
    before any lookup.

    Args:
        raw: The raw projection value parsed from one tick response.

    Returns:
        The strict projection, or the first closed failure in precedence order.
        ``PROJECTION_KEY_MISSING`` is never returned here.
    """
    if raw is None:
        return ColdTemperatureProjectionFailure.PROJECTION_NULL
    if type(raw) is not dict:
        return ColdTemperatureProjectionFailure.PROJECTION_NOT_OBJECT
    mapping = cast("dict[object, object]", raw)
    for key, _ in mapping.items():
        if type(key) is not str:
            return ColdTemperatureProjectionFailure.FIELD_SET_MISMATCH
    fields = cast("dict[str, object]", mapping)
    if "projection_version" not in fields or not _is_exact_version(fields["projection_version"]):
        return ColdTemperatureProjectionFailure.VERSION_NOT_ADMITTED
    if frozenset(fields) != _FIELD_NAME_SET:
        return ColdTemperatureProjectionFailure.FIELD_SET_MISMATCH
    raw_outcome = fields["outcome"]
    if type(raw_outcome) is not str or raw_outcome not in _OUTCOME_TOKENS:
        return ColdTemperatureProjectionFailure.OUTCOME_NOT_ADMITTED
    failure = _first_value_failure(fields)
    if failure is not None:
        return failure
    values = dict(fields)
    values["outcome"] = _OUTCOME_TOKENS[raw_outcome]
    values["configured_temperature_unit"] = _member(
        _CONFIGURED_UNIT_TOKENS, fields["configured_temperature_unit"]
    )
    values["reported_temperature_unit"] = _member(
        _REPORTED_UNIT_TOKENS, fields["reported_temperature_unit"]
    )
    values["value_agreement"] = _member(_AGREEMENT_TOKENS, fields["value_agreement"])
    try:
        projection = ColdTickTemperatureProjection.model_validate(values)
    except ValidationError:
        # The error embeds input values, so it is discarded, never re-raised.
        pass
    else:
        return projection
    return ColdTemperatureProjectionFailure.SHAPE_INCONSISTENT


#: The four member-valued fields with their exact enum class, in canonical order.
_MEMBER_FIELD_TYPES: Final[tuple[tuple[str, type[Enum]], ...]] = (
    ("outcome", ColdTemperatureOutcome),
    ("configured_temperature_unit", ColdTemperatureConfiguredUnit),
    ("reported_temperature_unit", ColdTemperatureReportedUnit),
    ("value_agreement", ColdTemperatureAgreement),
)


def _is_real_member(value: object, enum_type: type[Enum]) -> bool:
    """Whether a value is exactly one of an enum's own members, found by identity."""
    return type(value) is enum_type and any(value is member for member in enum_type)


def readmit_cold_temperature_projection(
    value: object,
) -> ColdTickTemperatureProjection | ColdTemperatureProjectionFailure:
    """Re-admit one in-process projection's content, or return its closed failure.

    Only an exact ``ColdTickTemperatureProjection`` instance is considered.  Its
    raw declared state is read without attribute hooks; undeclared, missing, or
    non-``str``-keyed state is refused before any key is hashed or compared.  Each
    member field must be ``None`` or a real member of its own enum, established by
    identity before its value is read.  The resulting raw document then runs the
    whole :func:`admit_cold_temperature_projection` grammar again, so a fresh,
    validated projection is returned and the input instance is never returned.

    The function never raises for an exact instance, never logs, and never renders
    its input.  Honest limit: this re-admits content, not provenance.  An exact
    class instance built by ``model_construct`` or ``model_copy`` whose content is
    valid is admitted as a fresh snapshot, because it is the same as validated
    content.

    Args:
        value: Any in-process candidate projection.

    Returns:
        A freshly validated projection, or the first closed failure.
        ``PROJECTION_KEY_MISSING`` is never returned here.
    """
    if value is None:
        return ColdTemperatureProjectionFailure.PROJECTION_NULL
    if type(value) is not ColdTickTemperatureProjection:
        return ColdTemperatureProjectionFailure.PROJECTION_NOT_OBJECT
    try:
        data: object = object.__getattribute__(value, "__dict__")
        extra: object = object.__getattribute__(value, "__pydantic_extra__")
    except AttributeError:
        # An uninitialised instance has no declared state to re-admit.
        data = extra = None
    if type(data) is not dict:
        return ColdTemperatureProjectionFailure.FIELD_SET_MISMATCH
    if extra is not None and (type(extra) is not dict or extra):
        return ColdTemperatureProjectionFailure.FIELD_SET_MISMATCH
    state = cast("dict[object, object]", data)
    for key in state:
        if type(key) is not str:
            return ColdTemperatureProjectionFailure.FIELD_SET_MISMATCH
    fields = cast("dict[str, object]", state)
    if frozenset(fields) != _FIELD_NAME_SET:
        return ColdTemperatureProjectionFailure.FIELD_SET_MISMATCH
    raw: dict[str, object] = {name: fields[name] for name in FIELD_NAMES}
    for name, enum_type in _MEMBER_FIELD_TYPES:
        member = fields[name]
        if member is None:
            continue
        if not _is_real_member(member, enum_type):
            return ColdTemperatureProjectionFailure.VALUE_TYPE_NOT_EXACT
        raw[name] = cast("Enum", member).value
    return admit_cold_temperature_projection(raw)
