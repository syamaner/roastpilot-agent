"""Failed-run terminal evidence: schema, re-admission, writer gate, and V4 reader (#954 OD10)."""

import ast
import enum
import functools
import json
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import advisory_conformance as ac
from roastpilot_agent.cold_characterisation import advisory_sampler as sampler
from roastpilot_agent.cold_characterisation import conformance
from roastpilot_agent.cold_characterisation import evidence_advisory as advisory
from roastpilot_agent.cold_characterisation import evidence_builders as builders
from roastpilot_agent.cold_characterisation import evidence_lifecycle as lifecycle
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation import evidence_terminal as terminal
from tests.test_cold_characterisation_conformance import (
    finalisation,
    header_of,
    host_record,
    plan,
    tick_record,
)
from tests.test_cold_characterisation_evidence_advisory import (
    attempt,
    bind_on,
    intend,
    open_off,
    read3,
    resolve,
)
from tests.test_cold_characterisation_evidence_builders import (
    COLD_PACKAGE,
    RUN_ID,
    _token_violations,  # pyright: ignore[reportPrivateUsage]
    header_for,
    tick_for,
)
from tests.test_cold_characterisation_evidence_lifecycle import (
    _FORBIDDEN_CAPABILITY_TEXT,  # pyright: ignore[reportPrivateUsage]
    OFF_SESSION,
    ON_SESSION,
    T0,
    _fence_identifiers,  # pyright: ignore[reportPrivateUsage]
    activated,
    expect_evidence,
    read2,
    stopped,
    terminated,
)
from tests.test_cold_characterisation_evidence_reader import line_of, rewrite
from tests.test_cold_characterisation_evidence_reader import read as read1
from tests.test_cold_characterisation_evidence_store import (
    OFF,
    ON,
    craft_manifest,
    expect,
    on_header,
    open_writer,
    run_dir,
)

Terminal = terminal.ColdFailedRunTerminalRecord
Settlement = terminal.ColdFailedRunAdvisorySettlement
Cancel = terminal.ColdFailedRunProviderCancellation
TFail = terminal.ColdFailedRunTerminalFailure
TState = terminal.ColdFailedRunTerminalEvidenceState
Failure = store.ColdEvidenceStoreFailure
Stop = lifecycle.ColdLifecycleChildStop
Reason = lifecycle.ColdRunTerminationReason
Termination = lifecycle.ColdRunTermination
V2 = reader.ColdRetainedRunV2
V3 = reader.ColdRetainedRunV3
V4 = reader.ColdRetainedRunV4
NOT_VALIDATED = schema.ColdEvidenceFailure.RECORD_NOT_VALIDATED
NOT_ADMITTED_JSON = schema.ColdEvidenceFailure.JSON_VALUE_TYPE_NOT_ADMITTED
UNRESOLVED = advisory.ColdAdvisoryResolution.UNRESOLVED_AT_PHASE_END
TERMINAL_OFF = "records/recording_off/failed_run_terminal.jsonl"
TERMINAL_ON = "records/recording_on/failed_run_terminal.jsonl"
OTHER_RUN_ID = "20260926T120000Z-cold-other"
COUNT_FIELDS = ("lifecycle_records_retained", "advisory_attempt_records_retained")
#: The shared walker's largest admitted integer (``MAX_INT_DIGITS`` nines).
INT_BOUND = 10**schema.MAX_INT_DIGITS - 1


class Scenario(typing.NamedTuple):
    """One open failed run: its writer, root, headers, and true retained counts."""

    writer: store.ColdEvidenceWriter
    root: str
    off: schema.ColdRunHeader
    on: schema.ColdRunHeader | None
    lifecycle: int
    advisory: int

    @property
    def latest(self) -> schema.ColdRunHeader:
        """The latest bound phase header."""
        return self.off if self.on is None else self.on


class Str(str):
    """A ``str`` subclass (never admitted)."""


class SubTerminal(Terminal):
    """A record subclass with the same fields (never admitted)."""


def scenario(tmp_path: Path, *, phase: schema.ColdPhaseKind = ON) -> Scenario:
    """Open a run with lifecycle, an unresolved advisory attempt, and an unconfirmed stop.

    The latest phase is ``phase``; the counts are the true retained line counts.
    """
    writer, root, off = open_off(tmp_path)
    writer.append_lifecycle(activated(OFF_SESSION, 10.0)(off, 0))
    for record in attempt(off, 0, 10.0, UNRESOLVED):
        writer.append_advisory_attempt(record)
    if phase is OFF:
        writer.append_lifecycle(stopped(Stop.UNCONFIRMED, 20.0)(off, 1))
        return Scenario(writer, root, off, None, 2, 2)
    on = bind_on(tmp_path, writer, root)
    writer.append_lifecycle(activated(ON_SESSION, 1830.0)(on, 1))
    writer.append_lifecycle(stopped(Stop.UNCONFIRMED, 1840.0)(on, 2))
    return Scenario(writer, root, off, on, 3, 2)


def build(
    header: schema.ColdRunHeader,
    lifecycle_records: int = 3,
    advisory_records: int = 2,
    settlement: terminal.ColdFailedRunAdvisorySettlement = Settlement.RECORDED_UNRESOLVED_INVOKED,
    cancellation: terminal.ColdFailedRunProviderCancellation = Cancel.REQUESTED,
) -> terminal.ColdFailedRunTerminalRecord:
    """Build one terminal through the real builder."""
    return terminal.build_failed_run_terminal_record(
        header,
        advisory_settlement=settlement,
        provider_cancellation=cancellation,
        lifecycle_records_retained=lifecycle_records,
        advisory_attempt_records_retained=advisory_records,
    )


def finish(s: Scenario) -> terminal.ColdFailedRunTerminalRecord:
    """Append the scenario's true terminal and return it."""
    record = build(s.latest, s.lifecycle, s.advisory)
    s.writer.append_failed_run_terminal(record)
    return record


def sealed(
    tmp_path: Path, *, phase: schema.ColdPhaseKind = ON
) -> tuple[str, str, Scenario, terminal.ColdFailedRunTerminalRecord]:
    """Write a failed run with its true terminal, seal it, and return its receipt."""
    s = scenario(tmp_path, phase=phase)
    record = finish(s)
    return s.root, s.writer.seal().manifest_sha256, s, record


def read4(root: str, digest: str) -> reader.ColdRetainedRunV4:
    """Read the shared test run through the V4 reader."""
    return reader.read_retained_run_v4(root, run_id=RUN_ID, expected_manifest_sha256=digest)


def doc(record: pydantic.BaseModel) -> dict[str, typing.Any]:
    """Return a record's JSON document."""
    return record.model_dump(mode="json")


def craft(root: str, files: dict[str, bytes | None]) -> str:
    """Write (or, for ``None``, delete) retained files, re-seal the manifest, return its digest."""
    for relative_path, data in files.items():
        path = run_dir(root) / relative_path
        if data is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return craft_manifest(run_dir(root))


def expect_terminal(
    failure: terminal.ColdFailedRunTerminalFailure, call: typing.Callable[[], object]
) -> None:
    """Assert one call raises exactly one closed, chain-free terminal failure."""
    with pytest.raises(terminal.ColdFailedRunTerminalError) as raised:
        call()
    assert raised.value.failure is failure
    assert raised.value.args == ("Cold failed-run terminal evidence refused.",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def header(tmp_path: Path, phase: schema.ColdPhaseKind = OFF) -> schema.ColdRunHeader:
    """Return one genuine phase header without a writer."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    return header_for(tmp_path, str(tmp_path.resolve()), phase)


def values(source: schema.ColdRunHeader, **update: object) -> dict[str, object]:
    """Return valid raw terminal values bound to ``source``, with updates."""
    base: dict[str, object] = {
        "schema_version": 3,
        "stream": "failed_run_terminal",
        "run_id": source.run_id,
        "identity_sha256": source.identity_sha256,
        "phase": source.phase,
        "advisory_settlement": Settlement.RECORDED_UNRESOLVED_INVOKED,
        "provider_cancellation": Cancel.REQUESTED,
        "lifecycle_records_retained": 3,
        "advisory_attempt_records_retained": 2,
    }
    return {**base, **update}


def model(source: schema.ColdRunHeader, **update: object) -> terminal.ColdFailedRunTerminalRecord:
    """Strictly validate raw terminal values at the model layer."""
    return Terminal.model_validate(values(source, **update), strict=True)


def refused_model(source: schema.ColdRunHeader, **update: object) -> None:
    """Assert the model layer refuses one update."""
    with pytest.raises(pydantic.ValidationError):
        model(source, **update)


def raw(record: pydantic.BaseModel) -> dict[object, object]:
    """Return a copy of a model's raw ``__dict__``."""
    return dict(object.__getattribute__(record, "__dict__"))


def fields_of(record: pydantic.BaseModel) -> dict[str, typing.Any]:
    """Return a copy of a model's raw field values keyed by name."""
    return {typing.cast(str, name): value for name, value in raw(record).items()}


def with_dict(record: pydantic.BaseModel, data: dict[object, object]) -> typing.Any:
    """Return a copy of a record whose ``__dict__`` is replaced."""
    copy = record.model_copy()
    object.__setattr__(copy, "__dict__", data)
    return copy


def validate(record: object) -> terminal.ColdFailedRunTerminalRecord:
    """Re-admit any candidate through the content validator."""
    return terminal.validate_failed_run_terminal_record(typing.cast(typing.Any, record))


# ------------------------------------------------------------ T1-T8 model layer


def test_t1_field_set_is_exact_and_asserts_nothing_beyond_observations() -> None:
    """T1: nine fields; no time, seal, stop, child, safety, session, or text field."""
    assert list(Terminal.model_fields) == [
        "schema_version",
        "stream",
        "run_id",
        "identity_sha256",
        "phase",
        "advisory_settlement",
        "provider_cancellation",
        "lifecycle_records_retained",
        "advisory_attempt_records_retained",
    ]
    banned = ("time", "utc", "monotonic", "seal", "stop", "child", "safe", "session", "text")
    for name in Terminal.model_fields:
        assert not any(token in name for token in (*banned, "error", "reason", "rationale"))
    text_fields = {n for n, f in Terminal.model_fields.items() if f.annotation is str}
    assert text_fields == {"run_id", "identity_sha256"}
    assert Terminal.model_config.get("frozen") is True
    assert Terminal.model_config.get("extra") == "forbid"
    assert Terminal.model_config.get("strict") is True


def test_t2_schema_version_is_exactly_the_int_three(tmp_path: Path) -> None:
    """T2: 2, 4, ``True`` and ``"3"`` are refused; 3 is admitted."""
    source = header(tmp_path)
    assert model(source).schema_version == 3
    for version in (2, 4, True, "3", 3.0):
        refused_model(source, schema_version=version)


def test_t3_stream_is_exactly_the_terminal_stream(tmp_path: Path) -> None:
    """T3: another stream name is refused."""
    source = header(tmp_path)
    for stream in ("lifecycle", "advisory_attempt", Str("failed_run_terminal")):
        refused_model(source, stream=stream)


@pytest.mark.parametrize("field", COUNT_FIELDS)
def test_t4_counts_are_exact_non_negative_ints(tmp_path: Path, field: str) -> None:
    """T4: ``-1``, ``True``, ``1.0`` and ``"1"`` are refused; 0 and 5 are admitted."""
    source = header(tmp_path)
    for count in (0, 5):
        assert getattr(model(source, **{field: count}), field) == count
    for count in (-1, True, 1.0, "1", None):
        refused_model(source, **{field: count})


def test_t5_identity_grammars_apply(tmp_path: Path) -> None:
    """T5: a malformed run id or digest is refused."""
    source = header(tmp_path)
    for run_id in ("cold-run", "20260926T120000Z-COLD", ""):
        refused_model(source, run_id=run_id)
    for digest in ("F" * 64, "f" * 63, "g" * 64, ""):
        refused_model(source, identity_sha256=digest)


def test_t6_extra_and_missing_keys_are_refused(tmp_path: Path) -> None:
    """T6: an extra key is refused; each missing key is refused."""
    source = header(tmp_path)
    refused_model(source, extra_field=1)
    for name in Terminal.model_fields:
        missing = values(source)
        del missing[name]
        with pytest.raises(pydantic.ValidationError):
            Terminal.model_validate(missing, strict=True)


def test_t7_record_is_frozen(tmp_path: Path) -> None:
    """T7: assignment raises."""
    record = model(header(tmp_path))
    with pytest.raises(pydantic.ValidationError):
        record.lifecycle_records_retained = 4  # pyright: ignore[reportAttributeAccessIssue]


def test_t8_unknown_and_foreign_enum_values_are_refused(tmp_path: Path) -> None:
    """T8: an unknown value string or a foreign member is refused; members are admitted."""
    source = header(tmp_path)
    refused_model(source, advisory_settlement="no_open_attempt")
    refused_model(source, provider_cancellation="requested")
    refused_model(source, phase="recording_off")
    refused_model(source, provider_cancellation=sampler.ColdAdvisoryCancellationRequest.REQUESTED)
    refused_model(
        source,
        advisory_settlement=sampler.ColdAdvisorySettlementClosure.RECORDED_UNRESOLVED_INVOKED,
    )


def test_t10_model_layer_refuses_a_str_subclass_run_id(tmp_path: Path) -> None:
    """T10 (model layer): ``model_validate`` refuses a ``str``-subclass identity value."""
    source = header(tmp_path)
    refused_model(source, run_id=Str(source.run_id))
    refused_model(source, identity_sha256=Str(source.identity_sha256))


# ------------------------------------------------ T9-T14 content re-admission


def test_t9_a_subclass_with_valid_content_is_refused(tmp_path: Path) -> None:
    """T9: an exact-class check refuses a subclass instance."""
    source = header(tmp_path)
    forged = SubTerminal.model_validate(values(source), strict=True)
    expect_evidence(NOT_VALIDATED, lambda: validate(forged))


def test_t10_constructed_str_subclass_is_refused(tmp_path: Path) -> None:
    """T10: a constructed record with a ``str``-subclass value is refused."""
    source = header(tmp_path)
    for name in ("run_id", "identity_sha256", "stream"):
        forged = Terminal.model_construct(**values(source, **{name: Str(values(source)[name])}))  # pyright: ignore[reportArgumentType]
        expect_evidence(NOT_VALIDATED, functools.partial(validate, forged))


def test_t11_forged_pydantic_extra_is_refused(tmp_path: Path) -> None:
    """T11: a non-empty ``__pydantic_extra__`` is refused."""
    forged = model(header(tmp_path)).model_copy()
    object.__setattr__(forged, "__pydantic_extra__", {"undeclared": 1})
    expect_evidence(NOT_VALIDATED, lambda: validate(forged))
    forged_type = model(header(tmp_path)).model_copy()
    object.__setattr__(forged_type, "__pydantic_extra__", [])
    expect_evidence(NOT_VALIDATED, lambda: validate(forged_type))


class _SpyDict(dict[object, object]):
    """A ``dict`` subclass standing in for ``__dict__`` (never admitted)."""


def test_t12_missing_extra_and_non_str_keys_are_refused(tmp_path: Path) -> None:
    """T12: a constructed record missing a field, or with an extra or non-``str`` key."""
    genuine = model(header(tmp_path))
    missing = raw(genuine)
    del missing["phase"]
    forgeries = [
        with_dict(genuine, missing),
        with_dict(genuine, {**raw(genuine), "extra": 1}),
        with_dict(genuine, {**raw(genuine), 1: 1}),
        with_dict(genuine, _SpyDict(raw(genuine))),
    ]
    constructed = values(header(tmp_path))
    del constructed["stream"]
    forgeries.append(Terminal.model_construct(**constructed))  # pyright: ignore[reportArgumentType]
    for forged in forgeries:
        expect_evidence(NOT_VALIDATED, functools.partial(validate, forged))


def test_t13_fake_foreign_and_non_exact_values_are_refused(tmp_path: Path) -> None:
    """T13: a fabricated or foreign member, a ``bool`` count, or a float count is refused."""
    genuine = model(header(tmp_path))
    updates: list[dict[str, object]] = [
        {"provider_cancellation": object.__new__(Cancel)},
        {"advisory_settlement": object.__new__(Settlement)},
        {"phase": object.__new__(schema.ColdPhaseKind)},
        {"provider_cancellation": sampler.ColdAdvisoryCancellationRequest.REQUESTED},
        {"advisory_settlement": Cancel.REQUESTED},
        {"lifecycle_records_retained": True},
        {"advisory_attempt_records_retained": 2.0},
        {"schema_version": 2},
        {"phase": None},
    ]
    for update in updates:
        forged = genuine.model_copy(update=update)
        expect_evidence(NOT_VALIDATED, functools.partial(validate, forged))


def test_t14_honest_limit_exact_constructed_valid_content_is_admitted(tmp_path: Path) -> None:
    """T14: exact-class constructed or copied valid content is admitted as a fresh snapshot.

    Content re-admission cannot prove provenance; this pins that honest limit.
    """
    source = header(tmp_path)
    constructed = Terminal.model_construct(**values(source))  # pyright: ignore[reportArgumentType]
    snapshot = validate(constructed)
    assert snapshot is not constructed
    assert type(snapshot) is Terminal
    assert snapshot == model(source)
    copied = model(source).model_copy()
    assert validate(copied) == copied
    assert validate(copied) is not copied


EnumT = typing.TypeVar("EnumT", bound=enum.Enum)


def populated_forgery(member: EnumT) -> EnumT:
    """Return an exact-type instance carrying a real member's name and value, not a member.

    Unlike a bare ``object.__new__`` instance, its ``.value`` and ``.name`` read like the
    real member's, so only member-identity admission can refuse it.
    """
    forged = object.__new__(type(member))
    object.__setattr__(forged, "_value_", member.value)
    object.__setattr__(forged, "_name_", member.name)
    return forged


_IDENTITY_CASES: list[tuple[str, enum.Enum]] = [
    ("phase", OFF),
    ("advisory_settlement", Settlement.RECORDED_UNRESOLVED_INVOKED),
    ("provider_cancellation", Cancel.REQUESTED),
]


def _assert_forgery_reads_like(forged: enum.Enum, member: enum.Enum) -> None:
    """Precondition: the forgery is exact-typed, value/name equal, and no real member."""
    assert type(forged) is type(member)
    assert (forged.name, forged.value) == (member.name, member.value)
    assert not any(forged is real for real in type(member))


@pytest.mark.parametrize(("field", "member"), _IDENTITY_CASES, ids=[c[0] for c in _IDENTITY_CASES])
def test_l3_populated_member_forgery_is_refused_by_the_validator(
    tmp_path: Path, field: str, member: enum.Enum
) -> None:
    """L3: a populated non-member of an admitted enum type is refused by identity alone.

    Strict model validation admits the same typed instance, so the refusal comes from
    member-identity admission, not from a missing attribute or the model.
    """
    source = header(tmp_path)
    forged = populated_forgery(member)
    _assert_forgery_reads_like(forged, member)
    admitted_by_model = model(source, **{field: forged})
    assert getattr(admitted_by_model, field) is forged
    expect_evidence(NOT_VALIDATED, lambda: validate(admitted_by_model))
    genuine = model(source, **{field: member})
    assert getattr(validate(genuine), field) is member


@pytest.mark.parametrize(("field", "member"), _IDENTITY_CASES, ids=[c[0] for c in _IDENTITY_CASES])
def test_l3_populated_member_forgery_is_refused_by_the_writer(
    tmp_path: Path, field: str, member: enum.Enum
) -> None:
    """L3: the writer refuses the populated forgery, writes nothing, and stays usable."""
    s = scenario(tmp_path, phase=OFF)
    genuine = build(s.latest, s.lifecycle, s.advisory)
    forged_member = populated_forgery(member)
    _assert_forgery_reads_like(forged_member, member)
    forged = genuine.model_copy(update={field: forged_member})
    expect_evidence(NOT_VALIDATED, lambda: s.writer.append_failed_run_terminal(forged))
    assert not (run_dir(s.root) / TERMINAL_OFF).exists()
    s.writer.append(tick_for(s.latest, 1))
    s.writer.append_failed_run_terminal(genuine)
    retained = read4(s.root, s.writer.seal().manifest_sha256)
    assert retained.terminal == genuine


def test_l3_builder_refuses_populated_settlement_and_cancellation_forgeries(
    tmp_path: Path,
) -> None:
    """L3: the builder's two enum arguments refuse a populated forgery after strict construction.

    Strict model construction alone accepts the forged typed instance (shown first), so
    the builder's refusal comes from its final content re-admission.  The phase is
    header-derived and re-admitted at the header boundary; no claim is made here for it.
    """
    source = header(tmp_path)
    settlement = populated_forgery(Settlement.RECORDED_UNRESOLVED_INVOKED)
    cancellation = populated_forgery(Cancel.REQUESTED)
    _assert_forgery_reads_like(settlement, Settlement.RECORDED_UNRESOLVED_INVOKED)
    _assert_forgery_reads_like(cancellation, Cancel.REQUESTED)
    assert model(source, advisory_settlement=settlement).advisory_settlement is settlement
    assert model(source, provider_cancellation=cancellation).provider_cancellation is cancellation
    expect_evidence(NOT_VALIDATED, lambda: build(source, settlement=settlement))
    expect_evidence(NOT_VALIDATED, lambda: build(source, cancellation=cancellation))
    record = build(source)
    assert record.advisory_settlement is Settlement.RECORDED_UNRESOLVED_INVOKED
    assert record.provider_cancellation is Cancel.REQUESTED


@pytest.mark.parametrize("field", COUNT_FIELDS)
def test_l1_validator_keeps_the_shared_integer_bound(tmp_path: Path, field: str) -> None:
    """L1: the walker admits ``10**32 - 1`` and refuses ``10**32`` with its own member."""
    source = header(tmp_path)
    at_bound = model(source, **{field: INT_BOUND})
    assert getattr(validate(at_bound), field) == INT_BOUND
    beyond = model(source, **{field: INT_BOUND + 1})
    expect_evidence(NOT_ADMITTED_JSON, lambda: validate(beyond))
    constructed = Terminal.model_construct(**values(source, **{field: INT_BOUND + 1}))  # pyright: ignore[reportArgumentType]
    expect_evidence(NOT_ADMITTED_JSON, lambda: validate(constructed))


# ------------------------------------------------------------ T15-T18 builder


def test_t15_builder_refuses_a_tick_as_header(tmp_path: Path) -> None:
    """T15: a valid tick carries run, digest, and phase but is not a header."""
    source = header(tmp_path)
    tick = tick_for(source, 0)
    expect_evidence(NOT_VALIDATED, lambda: build(typing.cast(schema.ColdRunHeader, tick)))
    expect_evidence(NOT_VALIDATED, lambda: build(typing.cast(schema.ColdRunHeader, object())))


def test_t16_builder_refuses_an_invalid_constructed_header(tmp_path: Path) -> None:
    """T16: a constructed header with valid run, digest, and phase but no identity."""
    source = header(tmp_path)
    forged = schema.ColdRunHeader.model_construct(**{**fields_of(source), "identity": None})
    expect_evidence(NOT_VALIDATED, lambda: build(forged))


def test_t17_builder_admits_a_valid_constructed_header(tmp_path: Path) -> None:
    """T17: a valid constructed header yields the same record as the genuine header."""
    source = header(tmp_path)
    constructed = schema.ColdRunHeader.model_construct(**fields_of(source))
    assert build(constructed) == build(source) == model(source)


def test_t18_builder_refuses_bad_arguments_and_fixes_version_and_stream(tmp_path: Path) -> None:
    """T18: bad counts or the wrong enum are refused; version and stream are fixed."""
    source = header(tmp_path)
    record = build(source, 0, 0)
    assert (record.schema_version, record.stream) == (3, "failed_run_terminal")
    assert (record.run_id, record.identity_sha256, record.phase) == (
        source.run_id,
        source.identity_sha256,
        source.phase,
    )
    for count in (-1, True, 1.0):
        bad = typing.cast(int, count)
        expect_evidence(NOT_VALIDATED, functools.partial(build, source, bad))
        expect_evidence(NOT_VALIDATED, functools.partial(build, source, 0, bad))
    expect_evidence(
        NOT_VALIDATED, lambda: build(source, settlement=typing.cast(Settlement, Cancel.REQUESTED))
    )
    expect_evidence(
        NOT_VALIDATED,
        lambda: build(
            source,
            cancellation=typing.cast(Cancel, sampler.ColdAdvisoryCancellationRequest.REQUESTED),
        ),
    )


@pytest.mark.parametrize("field", COUNT_FIELDS)
def test_l1_builder_keeps_the_shared_integer_bound(tmp_path: Path, field: str) -> None:
    """L1: the builder admits ``10**32 - 1`` and the walker refuses ``10**32``."""
    source = header(tmp_path)
    position = COUNT_FIELDS.index(field)
    counts = [0, 0]
    counts[position] = INT_BOUND
    assert getattr(build(source, counts[0], counts[1]), field) == INT_BOUND
    counts[position] = INT_BOUND + 1
    expect_evidence(NOT_ADMITTED_JSON, lambda: build(source, counts[0], counts[1]))


# ----------------------------------------------------- T19-T20 order function


def _order(record: terminal.ColdFailedRunTerminalRecord, **update: typing.Any) -> None:
    """Run the pure order check with matching arguments, updated as given."""
    arguments: dict[str, typing.Any] = {
        "latest_phase": OFF,
        "lifecycle_terminated": False,
        "lifecycle_records": 3,
        "advisory_records": 2,
    }
    terminal.check_failed_run_terminal_order(record, **{**arguments, **update})


def test_t19_each_order_refusal_fires_alone(tmp_path: Path) -> None:
    """T19: each order refusal fires by itself; matching arguments pass."""
    record = model(header(tmp_path))
    assert _order(record) is None
    expect_terminal(TFail.ALREADY_TERMINATED, lambda: _order(record, lifecycle_terminated=True))
    expect_terminal(TFail.PHASE_NOT_LATEST, lambda: _order(record, latest_phase=ON))
    expect_terminal(TFail.LIFECYCLE_COUNT_MISMATCHED, lambda: _order(record, lifecycle_records=4))
    expect_terminal(TFail.LIFECYCLE_COUNT_MISMATCHED, lambda: _order(record, lifecycle_records=2))
    expect_terminal(TFail.ADVISORY_COUNT_MISMATCHED, lambda: _order(record, advisory_records=3))
    expect_terminal(TFail.ADVISORY_COUNT_MISMATCHED, lambda: _order(record, advisory_records=1))


def test_t20_order_refusal_priority(tmp_path: Path) -> None:
    """T20: termination, then phase, then lifecycle count, then advisory count."""
    record = model(header(tmp_path))
    broken = {"latest_phase": ON, "lifecycle_records": 9, "advisory_records": 9}
    expect_terminal(
        TFail.ALREADY_TERMINATED, lambda: _order(record, lifecycle_terminated=True, **broken)
    )
    expect_terminal(TFail.PHASE_NOT_LATEST, lambda: _order(record, **broken))
    expect_terminal(
        TFail.LIFECYCLE_COUNT_MISMATCHED,
        lambda: _order(record, lifecycle_records=9, advisory_records=9),
    )


# ------------------------------------------------------------- T21-T29 writer


def test_t21_failed_run_round_trips_with_an_unconfirmed_child_stop(tmp_path: Path) -> None:
    """T21: both headers, lifecycle with ``UNCONFIRMED`` stop, one attempt, then the terminal."""
    root, digest, s, record = sealed(tmp_path)
    retained = read4(root, digest)
    assert retained.terminal_state is TState.PRESENT
    assert retained.terminal == record
    assert retained.terminal is not None and retained.terminal.phase is ON
    assert len(retained.lifecycle) == s.lifecycle == record.lifecycle_records_retained
    assert retained.lifecycle[-1].child_stop is Stop.UNCONFIRMED
    assert not any(
        item.event is lifecycle.ColdLifecycleEvent.RUN_TERMINATED for item in retained.lifecycle
    )
    assert len(retained.advisory_attempts) == s.advisory == record.advisory_attempt_records_retained
    assert retained.advisory_attempt_state is advisory.ColdAdvisoryAttemptEvidenceState.COMPLETE
    assert [h.header.phase for h in retained.run.headers] == [OFF, ON]
    assert (run_dir(root) / TERMINAL_ON).read_bytes() == line_of(doc(record))
    assert not (run_dir(root) / TERMINAL_OFF).exists()


_PAIRS = [(settled, cancelled) for settled in Settlement for cancelled in Cancel]


@pytest.mark.parametrize("phase", [OFF, ON], ids=["off", "on"])
@pytest.mark.parametrize(
    ("settlement", "cancellation"), _PAIRS, ids=[f"{a.value}-{b.value}" for a, b in _PAIRS]
)
def test_l1_every_admitted_pair_round_trips_in_both_phases(
    tmp_path: Path,
    phase: schema.ColdPhaseKind,
    settlement: terminal.ColdFailedRunAdvisorySettlement,
    cancellation: terminal.ColdFailedRunProviderCancellation,
) -> None:
    """L1: every stored settlement and cancellation pair, terminal in either phase."""
    s = scenario(tmp_path, phase=phase)
    record = build(s.latest, s.lifecycle, s.advisory, settlement, cancellation)
    s.writer.append_failed_run_terminal(record)
    retained = read4(s.root, s.writer.seal().manifest_sha256)
    assert retained.terminal == record
    assert retained.terminal is not None
    assert retained.terminal.advisory_settlement is settlement
    assert retained.terminal.provider_cancellation is cancellation
    assert retained.terminal.phase is phase
    assert len(retained.lifecycle) == s.lifecycle
    assert len(retained.advisory_attempts) == s.advisory


AfterCall = typing.Callable[[Scenario, Path], None]
_AFTER: dict[str, AfterCall] = {
    "tick": lambda s, _p: s.writer.append(tick_for(s.latest, 1)),
    "header": lambda s, p: s.writer.append(on_header(p, s.root)),
    "lifecycle": lambda s, _p: s.writer.append_lifecycle(
        stopped(Stop.CONFIRMED, 1850.0)(s.latest, s.lifecycle)
    ),
    "advisory": lambda s, _p: s.writer.append_advisory_attempt(intend(s.latest, 0, at=1850.0)),
    "terminal": lambda s, _p: s.writer.append_failed_run_terminal(
        build(s.latest, s.lifecycle, s.advisory)
    ),
}


@pytest.mark.parametrize("name", list(_AFTER))
def test_t22_every_append_after_the_terminal_is_refused(tmp_path: Path, name: str) -> None:
    """T22: each record append refuses after the terminal; the writer still seals."""
    s = scenario(tmp_path, phase=OFF if name == "header" else ON)
    record = finish(s)
    expect_terminal(TFail.APPENDED_AFTER_TERMINAL, lambda: _AFTER[name](s, tmp_path))
    expect_terminal(TFail.APPENDED_AFTER_TERMINAL, lambda: _AFTER[name](s, tmp_path))
    retained = read4(s.root, s.writer.seal().manifest_sha256)
    assert retained.terminal == record
    assert len(retained.lifecycle) == s.lifecycle
    assert len(retained.advisory_attempts) == s.advisory
    phases = [item.header.phase for item in retained.run.headers]
    assert phases == ([OFF] if s.on is None else [OFF, ON])
    ticks = [st for st in retained.run.streams if st.stream is schema.ColdEvidenceStream.TICK]
    assert all(len(st.records) == 1 for st in ticks)


def test_t22_controls_each_after_call_appends_without_a_terminal(tmp_path: Path) -> None:
    """Control: each refused call above is a valid append when no terminal exists."""
    for name, call in _AFTER.items():
        s = scenario(tmp_path / name, phase=OFF if name == "header" else ON)
        call(s, tmp_path / name)


def test_t23_close_after_the_terminal_behaves_as_today(tmp_path: Path) -> None:
    """T23: closing an unsealed writer after the terminal still makes it unusable."""
    s = scenario(tmp_path)
    finish(s)
    s.writer.close()
    s.writer.close()
    expect(Failure.WRITER_POISONED, lambda: s.writer.append(tick_for(s.latest, 1)))
    expect(Failure.WRITER_POISONED, lambda: s.writer.seal())


def test_t24_binding_refusals(tmp_path: Path) -> None:
    """T24: run id, missing header, and digest mismatches are refused without poisoning."""
    s = scenario(tmp_path, phase=OFF)
    genuine = build(s.latest, s.lifecycle, s.advisory)
    other_run = genuine.model_copy(update={"run_id": OTHER_RUN_ID})
    expect(Failure.RUN_ID_MISMATCHED, lambda: s.writer.append_failed_run_terminal(other_run))
    unbound_on = build(on_header(tmp_path, s.root), s.lifecycle, s.advisory)
    expect(Failure.HEADER_MISSING, lambda: s.writer.append_failed_run_terminal(unbound_on))
    digest = genuine.model_copy(update={"identity_sha256": "f" * 64})
    expect(Failure.IDENTITY_DIGEST_MISMATCHED, lambda: s.writer.append_failed_run_terminal(digest))
    s.writer.append_failed_run_terminal(genuine)
    assert read4(s.root, s.writer.seal().manifest_sha256).terminal == genuine


def test_t24_binding_never_mutates_state(tmp_path: Path) -> None:
    """The binding check binds no header and leaves state unchanged."""
    source = header(tmp_path)
    state = store.ColdBindingState(RUN_ID)
    expect(
        Failure.HEADER_MISSING,
        lambda: store.check_failed_run_terminal_binding(state, model(source)),
    )
    assert state.headers == ()


def _terminated_scenario(tmp_path: Path) -> Scenario:
    """An ON scenario whose lifecycle carries a v2 failed ``RUN_TERMINATED`` record."""
    s = scenario(tmp_path)
    spec = terminated(Termination.FAILED, 1850.0, Reason.CHILD_STOP_UNCONFIRMED)
    s.writer.append_lifecycle(spec(s.latest, s.lifecycle))
    return s._replace(lifecycle=s.lifecycle + 1)


def test_t25_order_refusals_leave_the_writer_usable(tmp_path: Path) -> None:
    """T25: phase, counts, and v2 termination refuse; a tick and the true terminal follow."""
    s = scenario(tmp_path)
    cases = [
        (TFail.PHASE_NOT_LATEST, build(s.off, s.lifecycle, s.advisory)),
        (TFail.LIFECYCLE_COUNT_MISMATCHED, build(s.latest, s.lifecycle + 1, s.advisory)),
        (TFail.LIFECYCLE_COUNT_MISMATCHED, build(s.latest, s.lifecycle - 1, s.advisory)),
        (TFail.ADVISORY_COUNT_MISMATCHED, build(s.latest, s.lifecycle, s.advisory + 1)),
        (TFail.ADVISORY_COUNT_MISMATCHED, build(s.latest, s.lifecycle, 0)),
    ]
    for index, (failure, record) in enumerate(cases, start=1):
        expect_terminal(failure, functools.partial(s.writer.append_failed_run_terminal, record))
        s.writer.append(tick_for(s.latest, index))
    assert not (run_dir(s.root) / TERMINAL_ON).exists()
    record = finish(s)
    assert read4(s.root, s.writer.seal().manifest_sha256).terminal == record


def test_t25_v2_termination_refuses_the_terminal(tmp_path: Path) -> None:
    """T25: a committed v2 ``RUN_TERMINATED`` contradicts a failed-run terminal."""
    s = _terminated_scenario(tmp_path)
    record = build(s.latest, s.lifecycle, s.advisory)
    expect_terminal(TFail.ALREADY_TERMINATED, lambda: s.writer.append_failed_run_terminal(record))
    s.writer.append(tick_for(s.latest, 1))
    retained = read4(s.root, s.writer.seal().manifest_sha256)
    assert retained.terminal_state is TState.ABSENT


def test_t26_refused_advisory_append_does_not_advance_the_count(tmp_path: Path) -> None:
    """T26: a resolution without an open attempt is refused and is not counted."""
    s = scenario(tmp_path)
    with pytest.raises(advisory.ColdAdvisoryAttemptError) as raised:
        s.writer.append_advisory_attempt(resolve(s.latest, 0, UNRESOLVED, start=1850.0))
    without_open = advisory.ColdAdvisoryAttemptFailure.RESOLUTION_WITHOUT_OPEN_ATTEMPT
    assert raised.value.failure is without_open
    record = finish(s)
    assert record.advisory_attempt_records_retained == 2
    assert read4(s.root, s.writer.seal().manifest_sha256).terminal == record


def test_t26_committed_advisory_appends_are_counted(tmp_path: Path) -> None:
    """Each durably written attempt line advances the writer's count by one."""
    s = scenario(tmp_path)
    s.writer.append_advisory_attempt(intend(s.latest, 0, at=1850.0))
    expect_terminal(
        TFail.ADVISORY_COUNT_MISMATCHED,
        lambda: s.writer.append_failed_run_terminal(build(s.latest, s.lifecycle, s.advisory)),
    )
    s.writer.append_failed_run_terminal(build(s.latest, s.lifecycle, s.advisory + 1))


def test_t27_refused_terminal_does_not_set_the_terminal_gate(tmp_path: Path) -> None:
    """T27: after a refused terminal, ordinary appends still succeed."""
    s = scenario(tmp_path)
    wrong = build(s.latest, s.lifecycle + 1, s.advisory)
    expect_terminal(
        TFail.LIFECYCLE_COUNT_MISMATCHED, lambda: s.writer.append_failed_run_terminal(wrong)
    )
    s.writer.append(tick_for(s.latest, 1))
    s.writer.append_lifecycle(stopped(Stop.CONFIRMED, 1850.0)(s.latest, s.lifecycle))


def test_t28_sealed_writer_refuses_the_terminal(tmp_path: Path) -> None:
    """T28: a sealed writer keeps ``WRITER_SEALED`` for the terminal append."""
    s = scenario(tmp_path)
    s.writer.seal()
    expect(
        Failure.WRITER_SEALED,
        lambda: s.writer.append_failed_run_terminal(build(s.latest, s.lifecycle, s.advisory)),
    )


def test_t28_sealed_refusal_precedes_the_terminal_gate(tmp_path: Path) -> None:
    """T28/G25: after terminal and seal, appends report ``WRITER_SEALED`` first."""
    s = scenario(tmp_path)
    finish(s)
    s.writer.seal()
    expect(Failure.WRITER_SEALED, lambda: s.writer.append(tick_for(s.latest, 1)))
    expect(
        Failure.WRITER_SEALED,
        lambda: s.writer.append_failed_run_terminal(build(s.latest, s.lifecycle, s.advisory)),
    )


def test_t28_terminal_write_fault_poisons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T28: a write fault raises ``WRITE_FAILED``; every later call is ``WRITER_POISONED``."""
    s = scenario(tmp_path)
    record = build(s.latest, s.lifecycle, s.advisory)

    def fault(_descriptor: int, _data: bytes) -> None:
        raise OSError("synthetic write fault")

    monkeypatch.setattr(store, "_write_all", fault)
    expect(Failure.WRITE_FAILED, lambda: s.writer.append_failed_run_terminal(record))
    expect(Failure.WRITER_POISONED, lambda: s.writer.append_failed_run_terminal(record))
    expect(Failure.WRITER_POISONED, lambda: s.writer.append(tick_for(s.latest, 1)))


class _Interrupt(BaseException):
    """A non-``Exception`` interrupt raised by a test seam."""


def test_terminal_write_interrupt_poisons_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``BaseException`` during the write propagates and poisons the writer."""
    s = scenario(tmp_path)
    record = build(s.latest, s.lifecycle, s.advisory)

    def interrupt(_descriptor: int, _data: bytes) -> None:
        raise _Interrupt

    monkeypatch.setattr(store, "_write_all", interrupt)
    with pytest.raises(_Interrupt):
        s.writer.append_failed_run_terminal(record)
    expect(Failure.WRITER_POISONED, lambda: s.writer.append_failed_run_terminal(record))


def test_unexpected_check_failure_poisons_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected error in the binding or order checks propagates and poisons."""
    s = scenario(tmp_path)
    record = build(s.latest, s.lifecycle, s.advisory)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic check failure")

    monkeypatch.setattr(store, "check_failed_run_terminal_order", broken)
    with pytest.raises(RuntimeError):
        s.writer.append_failed_run_terminal(record)
    expect(Failure.WRITER_POISONED, lambda: s.writer.append(tick_for(s.latest, 1)))


def test_t29_constructed_str_subclass_terminal_is_refused_by_the_writer(tmp_path: Path) -> None:
    """T29: content re-admission refuses a ``str``-subclass value; nothing is written."""
    s = scenario(tmp_path)
    genuine = build(s.latest, s.lifecycle, s.advisory)
    forged_fields: dict[str, typing.Any] = {**fields_of(genuine), "run_id": Str(RUN_ID)}
    forged = Terminal.model_construct(**forged_fields)
    expect_evidence(NOT_VALIDATED, lambda: s.writer.append_failed_run_terminal(forged))
    assert not (run_dir(s.root) / TERMINAL_ON).exists()
    s.writer.append(tick_for(s.latest, 1))


@pytest.mark.parametrize("field", COUNT_FIELDS)
def test_l1_writer_refuses_beyond_the_integer_bound(tmp_path: Path, field: str) -> None:
    """L1: ``10**32`` is refused by the walker; ``10**32 - 1`` reaches the order check."""
    s = scenario(tmp_path)
    beyond = model(s.latest, **{field: INT_BOUND + 1})
    expect_evidence(NOT_ADMITTED_JSON, lambda: s.writer.append_failed_run_terminal(beyond))
    at_bound = model(s.latest, **{field: INT_BOUND})
    failure = (
        TFail.LIFECYCLE_COUNT_MISMATCHED
        if field == "lifecycle_records_retained"
        else TFail.ADVISORY_COUNT_MISMATCHED
    )
    expect_terminal(failure, lambda: s.writer.append_failed_run_terminal(at_bound))
    finish(s)


# ------------------------------------------------------- T30-T31 contract pins


def test_t30_enum_values_equal_the_stored_sampler_subsets() -> None:
    """T30: settlement and cancellation equal the sampler's stored, unresolved subsets."""
    settlement_excluded = {"no_open_attempt", "recorded_completed_call", "not_recorded_reentrant"}
    stored_settlement = [
        (m.name, m.value)
        for m in sampler.ColdAdvisorySettlementClosure
        if m.value not in settlement_excluded
    ]
    assert [(m.name, m.value) for m in Settlement] == stored_settlement
    stored_cancellation = [
        (m.name, m.value)
        for m in sampler.ColdAdvisoryCancellationRequest
        if m.value not in {"not_settled", "in_progress"}
    ]
    assert [(m.name, m.value) for m in Cancel] == stored_cancellation
    assert len(Settlement) == 5 and len(Cancel) == 6
    assert [m.name for m in TFail] == [
        "ALREADY_TERMINATED",
        "PHASE_NOT_LATEST",
        "LIFECYCLE_COUNT_MISMATCHED",
        "ADVISORY_COUNT_MISMATCHED",
        "APPENDED_AFTER_TERMINAL",
        "TERMINAL_DUPLICATED",
    ]
    assert [m.name for m in TState] == ["ABSENT", "PRESENT"]
    for kind in (Settlement, Cancel, TFail, TState):
        assert kind.__bases__ == (enum.Enum,)
        assert not issubclass(kind, (str, int))
        assert all(member.value == member.name.lower() for member in kind)
    assert issubclass(terminal.ColdFailedRunTerminalError, Exception)
    assert terminal.ColdFailedRunTerminalError(TFail.TERMINAL_DUPLICATED).args == (
        "Cold failed-run terminal evidence refused.",
    )


def test_t31_frozen_grammars_and_profiles_are_not_widened() -> None:
    """T31: v2 termination, lifecycle and store failures, and V1-V3 profiles are unchanged."""
    assert [m.name for m in Reason] == [
        "PHASE_ADMISSION_REFUSED",
        "PHASE_ABORTED",
        "PHASE_FAILED_UNEXPECTEDLY",
        "FINALISATION_FAILED",
        "FINALISATION_NOT_CLEAN",
        "FINALISATION_RECORD_NOT_RETAINED",
        "TEARDOWN_UNCONFIRMED",
        "RESPAWN_FAILED",
        "RECONNECT_FAILED",
        "RECORDING_ON_IDENTITY_REFUSED",
        "PHASE_IDENTITY_DELTA_NOT_ADMITTED",
        "TRANSITION_BUDGET_EXCEEDED",
        "CLOCK_INVALID",
        "CHILD_STOP_UNCONFIRMED",
        "UNEXPECTED_FAILURE",
    ]
    assert [m.name for m in lifecycle.ColdLifecycleFailure] == [
        "SEQUENCE_NOT_CONTIGUOUS",
        "PHASE_NOT_LATEST",
        "PHASE_REGRESSED",
        "RECORDING_TIME_REGRESSED",
        "APPENDED_AFTER_TERMINATION",
    ]
    assert [m.name for m in Failure] == [
        "PLATFORM_UNSUPPORTED",
        "ROOT_NOT_ABSOLUTE",
        "ROOT_PROTECTED",
        "ROOTS_OVERLAP",
        "ROOT_UNUSABLE",
        "RUN_DIR_EXISTS",
        "OWNERSHIP_OR_MODE_MISMATCH",
        "RUN_ID_MISMATCHED",
        "HEADER_MISSING",
        "HEADER_DUPLICATED",
        "HEADER_BINDING_MISMATCHED",
        "IDENTITY_DIGEST_MISMATCHED",
        "FINALISATION_INDEX_MISMATCHED",
        "WRITER_SEALED",
        "WRITER_POISONED",
        "WRITE_FAILED",
        "SEAL_TREE_INVALID",
        "SEAL_TREE_CHANGED",
        "SEAL_LIMIT_EXCEEDED",
        "FILE_CHANGED",
        "FILE_READ_INCOMPLETE",
        "MANIFEST_MALFORMED",
        "MANIFEST_DIGEST_MISMATCHED",
        "MANIFEST_COPIES_DIFFER",
        "SIDECAR_INCONSISTENT",
        "INVENTORY_MISMATCHED",
        "ENTRY_PATH_INVALID",
        "ENTRY_DUPLICATED",
        "FILE_NOT_REGULAR",
        "FILE_DIGEST_MISMATCHED",
        "LINE_MALFORMED",
        "LINE_TOO_LARGE",
        "JSON_DUPLICATE_KEY",
        "JSON_NOT_FINITE",
        "LINE_NOT_CANONICAL",
        "SCHEMA_VERSION_UNKNOWN",
        "IDENTITY_NOT_V1",
    ]
    profile = reader._ReadProfile  # pyright: ignore[reportPrivateUsage]
    assert {m.name: set(m.value) for m in profile} == {
        "V1": set(),
        "V2": {"lifecycle.jsonl"},
        "V3": {"lifecycle.jsonl", "advisory_attempt.jsonl"},
        "V4": {"lifecycle.jsonl", "advisory_attempt.jsonl", "failed_run_terminal.jsonl"},
        "V5": {
            "lifecycle.jsonl",
            "advisory_attempt.jsonl",
            "failed_run_terminal.jsonl",
            "tick_temperature.jsonl",
        },
        "V6": {
            "lifecycle.jsonl",
            "advisory_attempt.jsonl",
            "failed_run_terminal.jsonl",
            "tick_temperature.jsonl",
            "temperature_abort.jsonl",
            "mcp_candidate.jsonl",
        },
    }
    versions = (
        reader._SCHEMA_VERSIONS,  # pyright: ignore[reportPrivateUsage]
        reader._LIFECYCLE_SCHEMA_VERSIONS,  # pyright: ignore[reportPrivateUsage]
        reader._ADVISORY_SCHEMA_VERSIONS,  # pyright: ignore[reportPrivateUsage]
        reader._TERMINAL_SCHEMA_VERSIONS,  # pyright: ignore[reportPrivateUsage]
    )
    assert versions == (frozenset({1}), frozenset({2}), frozenset({2}), frozenset({3}))
    temperature_run_versions = (
        reader._TEMPERATURE_ABORT_SCHEMA_VERSIONS,  # pyright: ignore[reportPrivateUsage]
        reader._MCP_CANDIDATE_SCHEMA_VERSIONS,  # pyright: ignore[reportPrivateUsage]
    )
    assert temperature_run_versions == (frozenset({5}), frozenset({6}))


# ------------------------------------------------------------ T32-T45 reader


_EARLIER_READERS: dict[str, typing.Callable[[str, str], object]] = {
    "v1": read1,
    "v2": read2,
    "v3": read3,
}


@pytest.mark.parametrize("name", list(_EARLIER_READERS))
def test_t32_earlier_readers_refuse_a_terminal_tree(tmp_path: Path, name: str) -> None:
    """T32: V1, V2, and V3 refuse a tree holding the terminal file."""
    root, digest, _s, _record = sealed(tmp_path)
    expect(Failure.ENTRY_PATH_INVALID, lambda: _EARLIER_READERS[name](root, digest))


def test_t33_a_v3_tree_reads_absent_with_identical_streams(tmp_path: Path) -> None:
    """T33: without a terminal, V4 equals V3 and reports ``ABSENT``."""
    s = scenario(tmp_path)
    digest = s.writer.seal().manifest_sha256
    v3 = read3(s.root, digest)
    v4 = read4(s.root, digest)
    assert v4.terminal_state is TState.ABSENT
    assert v4.terminal is None
    assert (v4.run, v4.lifecycle_state, v4.lifecycle) == (v3.run, v3.lifecycle_state, v3.lifecycle)
    assert (v4.advisory_attempt_state, v4.advisory_attempts) == (
        v3.advisory_attempt_state,
        v3.advisory_attempts,
    )


def test_t34_terminal_in_a_phase_without_a_header_is_refused(tmp_path: Path) -> None:
    """T34: an ON terminal file with only the OFF header bound."""
    root, _digest, _s, record = sealed(tmp_path, phase=OFF)
    digest = craft(root, {TERMINAL_ON: line_of({**doc(record), "phase": ON.value})})
    expect(Failure.HEADER_MISSING, lambda: read4(root, digest))


def _reread(tmp_path: Path, **update: object) -> tuple[str, str]:
    """Seal the true ON run, then replace its terminal line with an updated document."""
    root, _digest, _s, record = sealed(tmp_path)
    return root, rewrite(root, TERMINAL_ON, line_of({**doc(record), **update}))


def test_t35_t36_terminal_version_is_exactly_the_int_three(tmp_path: Path) -> None:
    """T35/T36: versions 2, 4, 3.0, ``"3"``, ``true`` or absent are unknown."""
    for index, version in enumerate((2, 4, 3.0, "3", True, None)):
        root, digest = _reread(tmp_path / str(index), schema_version=version)
        expect(Failure.SCHEMA_VERSION_UNKNOWN, functools.partial(read4, root, digest))
    root, _digest, _s, record = sealed(tmp_path / "missing")
    document = doc(record)
    del document["schema_version"]
    digest = rewrite(root, TERMINAL_ON, line_of(document))
    expect(Failure.SCHEMA_VERSION_UNKNOWN, lambda: read4(root, digest))


def test_t37_terminal_stream_is_exact(tmp_path: Path) -> None:
    """T37: another stream value, or a non-string, is malformed."""
    for index, stream in enumerate(("lifecycle", 3, "failed_run_terminal ")):
        root, digest = _reread(tmp_path / str(index), stream=stream)
        expect(Failure.LINE_MALFORMED, functools.partial(read4, root, digest))


_MALFORMED: list[tuple[str, dict[str, object]]] = [
    ("unknown-key", {"zzz": 1}),
    ("unknown-settlement", {"advisory_settlement": "no_open_attempt"}),
    ("unknown-cancellation", {"provider_cancellation": "not_settled"}),
    ("unknown-phase", {"phase": "recording_sideways"}),
    ("negative-lifecycle-count", {"lifecycle_records_retained": -1}),
    ("negative-advisory-count", {"advisory_attempt_records_retained": -1}),
    ("bool-count", {"lifecycle_records_retained": True}),
    ("float-count", {"lifecycle_records_retained": 3.0}),
    ("text-count", {"advisory_attempt_records_retained": "2"}),
    ("bad-run-id", {"run_id": "cold"}),
    ("bad-digest", {"identity_sha256": "F" * 64}),
]


@pytest.mark.parametrize(("label", "update"), _MALFORMED, ids=[c[0] for c in _MALFORMED])
def test_t38_malformed_terminal_lines_are_refused(
    tmp_path: Path, label: str, update: dict[str, object]
) -> None:
    """T38: unknown keys or values, wrong types, and negative counts are malformed."""
    del label
    root, digest = _reread(tmp_path, **update)
    expect(Failure.LINE_MALFORMED, lambda: read4(root, digest))


def test_t39_a_line_from_another_phase_file_is_refused(tmp_path: Path) -> None:
    """T39: a genuine OFF terminal line placed in the ON terminal file."""
    root, _digest, s, _record = sealed(tmp_path)
    off_line = line_of(doc(build(s.off, s.lifecycle, s.advisory)))
    digest = rewrite(root, TERMINAL_ON, off_line)
    expect(Failure.LINE_MALFORMED, lambda: read4(root, digest))


@pytest.mark.parametrize("data", [b"[]\n", b"3\n", b'"x"\n', b"{\n", b"null\n"])
def test_t38_non_object_and_invalid_json_lines_are_refused(tmp_path: Path, data: bytes) -> None:
    """T38: a line that is not one strict JSON object is malformed."""
    root, _digest, _s, _record = sealed(tmp_path)
    digest = rewrite(root, TERMINAL_ON, data)
    expect(Failure.LINE_MALFORMED, lambda: read4(root, digest))


def test_t40_non_canonical_lines_are_refused(tmp_path: Path) -> None:
    """T40: unsorted keys or non-compact separators are not canonical."""
    root, _digest, _s, record = sealed(tmp_path)
    document = doc(record)
    for data in (
        json.dumps(document, separators=(",", ":")),
        json.dumps(document, sort_keys=True),
    ):
        assert data.encode() + b"\n" != line_of(document)
        digest = rewrite(root, TERMINAL_ON, data.encode() + b"\n")
        expect(Failure.LINE_NOT_CANONICAL, functools.partial(read4, root, digest))


def test_t41_binding_refusals_in_the_reader(tmp_path: Path) -> None:
    """T41: a wrong valid-hex digest, or another valid run id, is refused."""
    root, digest = _reread(tmp_path / "digest", identity_sha256="f" * 64)
    expect(Failure.IDENTITY_DIGEST_MISMATCHED, lambda: read4(root, digest))
    root, digest = _reread(tmp_path / "run", run_id=OTHER_RUN_ID)
    expect(Failure.RUN_ID_MISMATCHED, lambda: read4(root, digest))


def test_t42_duplicate_terminals_are_refused(tmp_path: Path) -> None:
    """T42: two lines in one file, or terminal files in both phases."""
    root, _digest, _s, record = sealed(tmp_path / "lines")
    digest = rewrite(root, TERMINAL_ON, line_of(doc(record)) * 2)
    expect_terminal(TFail.TERMINAL_DUPLICATED, lambda: read4(root, digest))
    root, _digest, s, _record = sealed(tmp_path / "files")
    digest = craft(root, {TERMINAL_OFF: line_of(doc(build(s.off, s.lifecycle, s.advisory)))})
    expect_terminal(TFail.TERMINAL_DUPLICATED, lambda: read4(root, digest))


def test_t43_crafted_order_breaches_are_refused(tmp_path: Path) -> None:
    """T43: crafted counts, a non-latest phase, and a v2 termination are refused."""
    root, digest = _reread(tmp_path / "lifecycle", lifecycle_records_retained=4)
    expect_terminal(TFail.LIFECYCLE_COUNT_MISMATCHED, lambda: read4(root, digest))
    root, digest = _reread(tmp_path / "advisory", advisory_attempt_records_retained=3)
    expect_terminal(TFail.ADVISORY_COUNT_MISMATCHED, lambda: read4(root, digest))
    root, _digest, s, _record = sealed(tmp_path / "phase")
    off_line = line_of(doc(build(s.off, s.lifecycle, s.advisory)))
    digest = craft(root, {TERMINAL_ON: None, TERMINAL_OFF: off_line})
    expect_terminal(TFail.PHASE_NOT_LATEST, lambda: read4(root, digest))
    t = _terminated_scenario(tmp_path / "terminated")
    t.writer.seal()
    line = line_of(doc(build(t.latest, t.lifecycle, t.advisory)))
    digest = craft(t.root, {TERMINAL_ON: line})
    expect_terminal(TFail.ALREADY_TERMINATED, lambda: read4(t.root, digest))


@pytest.mark.parametrize("field", COUNT_FIELDS)
def test_l1_reader_keeps_the_shared_integer_bound(tmp_path: Path, field: str) -> None:
    """L1: a crafted ``10**32`` is refused by the walker; ``10**32 - 1`` reaches order."""
    root, digest = _reread(tmp_path / "beyond", **{field: INT_BOUND + 1})
    expect_evidence(NOT_ADMITTED_JSON, lambda: read4(root, digest))
    root, digest = _reread(tmp_path / "bound", **{field: INT_BOUND})
    failure = (
        TFail.LIFECYCLE_COUNT_MISMATCHED
        if field == "lifecycle_records_retained"
        else TFail.ADVISORY_COUNT_MISMATCHED
    )
    expect_terminal(failure, lambda: read4(root, digest))


def test_t44_empty_file_and_walker_bounds(tmp_path: Path) -> None:
    """T44: an empty file is malformed; an over-long undeclared key keeps the walker's member."""
    root, digest = _reread(tmp_path / "key", **{"k" * (schema.MAX_JSON_KEY_BYTES + 1): 1})
    expect_evidence(schema.ColdEvidenceFailure.JSON_KEY_INVALID, lambda: read4(root, digest))
    root, _digest, _s, _record = sealed(tmp_path / "empty")
    digest = rewrite(root, TERMINAL_ON, b"")
    expect(Failure.LINE_MALFORMED, lambda: read4(root, digest))
    root, _digest, _s, _record = sealed(tmp_path / "blank")
    digest = rewrite(root, TERMINAL_ON, b"\n")
    expect(Failure.LINE_MALFORMED, lambda: read4(root, digest))


def test_tampered_terminal_is_refused_before_any_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1: tampered terminal bytes under the old receipt refuse at verification; no parse."""
    calls: list[bytes] = []
    real = reader._read_failed_run_terminal_line  # pyright: ignore[reportPrivateUsage]

    def spy(line: bytes, **kwargs: typing.Any) -> terminal.ColdFailedRunTerminalRecord:
        calls.append(line)
        return real(line, **kwargs)

    monkeypatch.setattr(reader, "_read_failed_run_terminal_line", spy)
    root, digest, _s, record = sealed(tmp_path)
    assert read4(root, digest).terminal == record
    assert len(calls) == 1
    calls.clear()
    path = run_dir(root) / TERMINAL_ON
    data = bytearray(path.read_bytes())
    data[data.index(b'"lifecycle_records_retained":3') + 29] = ord("4")
    path.write_bytes(bytes(data))
    expect(Failure.FILE_DIGEST_MISMATCHED, lambda: read4(root, digest))
    assert calls == []


def test_t45_v4_carrier_states_must_match_their_records(tmp_path: Path) -> None:
    """T45: terminal, lifecycle, and advisory states must match the records they carry."""
    root, digest, _s, _record = sealed(tmp_path)
    genuine = read4(root, digest)
    fields: dict[str, typing.Any] = {name: getattr(genuine, name) for name in V4.model_fields}
    assert V4(**fields) == genuine
    updates: list[dict[str, typing.Any]] = [
        {"terminal_state": TState.PRESENT, "terminal": None},
        {"terminal_state": TState.ABSENT},
        {"lifecycle_state": lifecycle.ColdLifecycleEvidenceState.ABSENT},
        {"lifecycle": (), "lifecycle_state": lifecycle.ColdLifecycleEvidenceState.PRESENT},
        {"advisory_attempt_state": advisory.ColdAdvisoryAttemptEvidenceState.OPEN_TAIL},
    ]
    for update in updates:
        with pytest.raises(pydantic.ValidationError):
            V4(**{**fields, **update})
    absent_fields: dict[str, typing.Any] = {
        **fields,
        "terminal_state": TState.ABSENT,
        "terminal": None,
    }
    absent = V4(**absent_fields)
    assert absent.terminal is None


# ------------------------------------------------------ T46-T47 never qualifies


def test_t46_v4_carrier_is_admitted_by_neither_policy(tmp_path: Path) -> None:
    """T46: policy 1 and policy 2 refuse a V4 carrier as not admitted."""
    root, digest, _s, _record = sealed(tmp_path)
    v4 = read4(root, digest)
    first = conformance.check_pre_advisory_conformance(v4)
    assert first.outcome is conformance.ColdConformanceOutcome.NOT_CONFORMANT
    assert first.findings == (conformance.ColdConformanceFinding.CARRIER_NOT_ADMITTED,)
    second = ac.check_advisory_conformance(v4)
    assert second.outcome is ac.ColdAdvisoryConformanceOutcome.NOT_CONFORMANT
    assert second.findings == (ac.ColdAdvisoryConformanceFinding.CARRIER_NOT_ADMITTED,)
    assert second.pre_advisory_findings == ()


def _annotation_types(annotation: object) -> set[object]:
    """Return an annotation and every type nested in its arguments."""
    found: set[object] = {annotation}
    for argument in typing.get_args(annotation):
        found |= _annotation_types(argument)
    return found


def test_t47_v4_is_not_and_nests_no_v2_or_v3_carrier() -> None:
    """T47: V4 is flat; no field is or contains a V2 or V3 carrier."""
    assert not issubclass(V4, (V2, V3))
    assert V4.__bases__ == (pydantic.BaseModel,)
    for field in V4.model_fields.values():
        for kind in _annotation_types(field.annotation):
            assert kind not in (V2, V3)
            assert not (isinstance(kind, type) and issubclass(kind, (V2, V3)))


def _failed_plan_run(tmp_path: Path) -> reader.ColdRetainedRunV4:
    """Write policy 1's conforming plan minus only its v2 terminal, plus a failed-run terminal."""
    run = plan(tmp_path)
    assert run.lifecycle[-1].event is lifecycle.ColdLifecycleEvent.RUN_TERMINATED
    run.lifecycle.pop()
    writer, root = open_writer(tmp_path)
    sequence = 0
    latest: schema.ColdRunHeader | None = None
    for phase in run.phases:
        latest = header_of(run.documents[phase], phase, run.headers[phase])
        writer.append(latest)
        for tick in run.ticks[phase]:
            writer.append(tick_record(latest, tick))
        for mono in run.hosts[phase]:
            writer.append(host_record(latest, mono))
        for payload, mono in run.results[phase]:
            writer.append(finalisation(latest, payload, mono))
        for entry in run.lifecycle:
            if entry.phase is not phase:
                continue
            fields = dict(entry.fields)
            if entry.event is lifecycle.ColdLifecycleEvent.OBSERVATION_WINDOW_ELAPSED:
                fields.setdefault("tick_count", len(run.ticks[phase]))
            writer.append_lifecycle(
                builders.build_lifecycle_record(
                    header=latest,
                    sequence=sequence,
                    event=entry.event,
                    event_utc=T0,
                    event_monotonic_seconds=entry.ev,
                    recorded_at_utc=T0,
                    monotonic_seconds=entry.rec,
                    **fields,
                )
            )
            sequence += 1
    assert latest is not None
    writer.append_failed_run_terminal(build(latest, sequence, 0))
    return read4(root, writer.seal().manifest_sha256)


def test_t47_projected_prefix_lacks_only_its_terminal(tmp_path: Path) -> None:
    """T47/L1: a hand-projected V2 of an otherwise conforming failed run finds only the gap.

    The lifecycle prefix is the conforming plan minus only its v2 terminal, so the
    findings are exactly the committed ``n8_terminal_missing`` pair for the same plan
    and cannot come from unrelated missing records; the failed-run terminal is never
    projected into any policy input.
    """
    v4 = _failed_plan_run(tmp_path)
    assert v4.terminal_state is TState.PRESENT
    present = lifecycle.ColdLifecycleEvidenceState.PRESENT
    projected = V2(run=v4.run, lifecycle_state=present, lifecycle=v4.lifecycle)
    first = conformance.check_pre_advisory_conformance(projected)
    assert first.outcome is conformance.ColdConformanceOutcome.NOT_CONFORMANT
    assert first.findings == (
        conformance.ColdConformanceFinding.LIFECYCLE_GRAMMAR_MISMATCH,
        conformance.ColdConformanceFinding.TERMINAL_ABSENT,
    )
    projected_v3 = V3(
        run=v4.run,
        lifecycle_state=present,
        lifecycle=v4.lifecycle,
        advisory_attempt_state=v4.advisory_attempt_state,
        advisory_attempts=v4.advisory_attempts,
    )
    second = ac.check_advisory_conformance(projected_v3)
    assert second.outcome is ac.ColdAdvisoryConformanceOutcome.NOT_CONFORMANT
    assert conformance.ColdConformanceFinding.TERMINAL_ABSENT in second.pre_advisory_findings


# ----------------------------------------------------------------- T48-T50 fences


_TERMINAL_SOURCE = (COLD_PACKAGE / "evidence_terminal.py").read_text(encoding="utf-8")


def test_t48_terminal_module_imports_exactly_its_allow_list() -> None:
    """T48: the pure module imports only stdlib ``enum``/``typing``, pydantic, and the schema."""
    imported: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(ast.parse(_TERMINAL_SOURCE)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module)
            names.update(alias.name for alias in node.names)
    assert imported == {
        "enum",
        "typing",
        "pydantic",
        "roastpilot_agent.cold_characterisation.evidence_schema",
    }
    assert names == {
        "ColdEvidenceError",
        "ColdEvidenceFailure",
        "ColdPhaseKind",
        "ColdRunHeader",
        "validate_record",
        "walk_json_value",
    }


def test_t49_terminal_module_carries_no_capability_or_decision_names() -> None:
    """T49: no actuator or limit text, and no verdict/evaluation/report/outcome/qualif name."""
    for text in _FORBIDDEN_CAPABILITY_TEXT:
        assert text not in _TERMINAL_SOURCE
    identifiers = _fence_identifiers(_TERMINAL_SOURCE)
    assert _token_violations("evidence_terminal.py", identifiers) == set()
    assert {name for name in identifiers if "qualif" in name.lower()} == set()
    assert "subprocess" in _FORBIDDEN_CAPABILITY_TEXT


def test_t50_no_runtime_module_reaches_the_terminal_api() -> None:
    """T50: only the two-phase orchestrator (5c-ii-b) names the terminal API at runtime."""
    texts = ("evidence_terminal", "append_failed_run_terminal", "read_retained_run_v4")
    for name in ("engine.py", "advisory_sampler.py", "advisory_run_owner.py"):
        source = (COLD_PACKAGE / name).read_text(encoding="utf-8")
        for text in texts:
            assert text not in source, (name, text)
    two_phase_source = (COLD_PACKAGE / "two_phase.py").read_text(encoding="utf-8")
    assert all(text in two_phase_source for text in texts)
