"""Behavioural tests for the versioned tick-temperature evidence format and V5 reader.

Records are written by the real descriptor-bound writer under a pytest temporary
root and read back through the real verified reader; no MCP child, hardware, or
provider is involved.  Every accepted record is integrity data only: nothing here
screens a temperature or qualifies a run.
"""

import ast
import enum
import functools
import json
import traceback
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import evidence_advisory as advisory
from roastpilot_agent.cold_characterisation import evidence_lifecycle as lifecycle
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation import evidence_temperature as temperature
from roastpilot_agent.cold_characterisation import evidence_terminal as terminal
from roastpilot_agent.cold_characterisation.temperature_projection import (
    ColdTemperatureOutcome,
    ColdTickTemperatureProjection,
    admit_cold_temperature_projection,
)
from tests.test_cold_characterisation_evidence_builders import (
    COLD_PACKAGE,
    RUN_ID,
    header_for,
    tick_for,
)
from tests.test_cold_characterisation_evidence_reader import line_of, rewrite
from tests.test_cold_characterisation_evidence_store import (
    OFF,
    ON,
    SECRET,
    craft_manifest,
    expect,
    on_header,
    open_writer,
    run_dir,
    write_full_run,
)
from tests.test_cold_characterisation_evidence_terminal import read4, sealed
from tests.test_cold_characterisation_temperature_projection import (
    ACCEPTED_SHAPES,
    celsius_agree,
    members,
    valid_projection,
)

Record = temperature.ColdTickTemperatureRecord
TFail = temperature.ColdTickTemperatureFailure
TState = temperature.ColdTickTemperatureEvidenceState
Failure = store.ColdEvidenceStoreFailure
EFail = schema.ColdEvidenceFailure
V5 = reader.ColdRetainedRunV5
TEMPERATURE_OFF = "records/recording_off/tick_temperature.jsonl"
TEMPERATURE_ON = "records/recording_on/tick_temperature.jsonl"
OTHER_RUN_ID = "20260926T120000Z-cold-other"
RECORDED = "2026-09-26T12:00:01Z"
#: The shared walker's largest admitted integer (``MAX_INT_DIGITS`` nines).
INT_BOUND = 10**schema.MAX_INT_DIGITS - 1


class Str(str):
    """A ``str`` subclass (never admitted)."""


class SubRecord(Record):
    """A record subclass with identical fields (never admitted)."""


class SubProjection(ColdTickTemperatureProjection):
    """A projection subclass with identical fields (never admitted)."""


class DictSubclass(dict[object, object]):
    """A non-exact raw-state mapping."""


class _Interrupt(BaseException):
    """A non-``Exception`` interruption."""


# ------------------------------------------------------------------- helpers


def values_for(source: schema.ColdTickRecord, /, **update: object) -> dict[str, object]:
    """Return valid raw record values paired with the ``source`` tick, with updates."""
    base: dict[str, object] = {
        "schema_version": 4,
        "stream": "tick_temperature",
        "run_id": source.run_id,
        "phase": source.phase,
        "recorded_at_utc": source.recorded_at_utc,
        "monotonic_seconds": source.monotonic_seconds,
        "identity_sha256": source.identity_sha256,
        "tick": source.tick,
        "temperature": valid_projection(),
    }
    return {**base, **update}


def temperature_for(
    source: schema.ColdTickRecord, /, **update: object
) -> temperature.ColdTickTemperatureRecord:
    """Strictly build one record paired with the ``source`` tick through the model layer."""
    return Record.model_validate(values_for(source, **update), strict=True)


def doc(record: pydantic.BaseModel) -> dict[str, typing.Any]:
    """Return a record's JSON document."""
    return record.model_dump(mode="json")


def read5(root: str, digest: str) -> reader.ColdRetainedRunV5:
    """Read the shared test run through the V5 reader."""
    return reader.read_retained_run_v5(root, run_id=RUN_ID, expected_manifest_sha256=digest)


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


def expect_temperature(
    failure: temperature.ColdTickTemperatureFailure, call: typing.Callable[[], object]
) -> None:
    """Assert one call raises exactly one closed, chain-free tick-temperature failure."""
    with pytest.raises(temperature.ColdTickTemperatureError) as raised:
        call()
    assert raised.value.failure is failure
    assert raised.value.args == ("Cold tick temperature evidence refused.",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def expect_evidence(failure: schema.ColdEvidenceFailure, call: typing.Callable[[], object]) -> None:
    """Assert one call raises exactly one closed, chain-free evidence failure."""
    with pytest.raises(schema.ColdEvidenceError) as raised:
        call()
    assert raised.value.failure is failure
    assert raised.value.args == ("Cold evidence admission failed.",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def validate(record: object) -> temperature.ColdTickTemperatureRecord:
    """Re-admit any candidate through the shared boundary."""
    return temperature.validate_tick_temperature_record(record)


class Run(typing.NamedTuple):
    """One open run with both phases bound and the ticks each phase retains."""

    writer: store.ColdEvidenceWriter
    root: str
    off: schema.ColdRunHeader
    on: schema.ColdRunHeader
    off_ticks: tuple[schema.ColdTickRecord, ...]
    on_ticks: tuple[schema.ColdTickRecord, ...]


def paired_run(tmp_path: Path, *, ticks: int = 2, pair: bool = True) -> Run:
    """Open a two-phase run whose every tick (optionally) is followed by its temperature."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    off_ticks = tuple(tick_for(off, index) for index in range(ticks))
    for tick in off_ticks:
        writer.append(tick)
        if pair:
            writer.append_tick_temperature(temperature_for(tick))
    on = on_header(tmp_path, root)
    writer.append(on)
    on_ticks = tuple(tick_for(on, index) for index in range(ticks))
    for tick in on_ticks:
        writer.append(tick)
        if pair:
            writer.append_tick_temperature(temperature_for(tick))
    return Run(writer, root, off, on, off_ticks, on_ticks)


def sealed_run(tmp_path: Path) -> tuple[Run, str]:
    """Write and seal a fully paired two-phase run, returning its receipt digest."""
    run = paired_run(tmp_path)
    return run, run.writer.seal().manifest_sha256


def lines(*records: pydantic.BaseModel) -> bytes:
    """Return the canonical lines of several records."""
    return b"".join(line_of(doc(record)) for record in records)


def off_only_run(tmp_path: Path) -> tuple[store.ColdEvidenceWriter, str, schema.ColdRunHeader]:
    """Open a run with only the recording-off header bound."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    return writer, root, off


def with_state(record: pydantic.BaseModel, data: object) -> typing.Any:
    """Return a copy of a model whose raw ``__dict__`` is replaced."""
    copy = record.model_copy()
    object.__setattr__(copy, "__dict__", data)
    return copy


def with_extra(record: pydantic.BaseModel, extra: object) -> typing.Any:
    """Return a copy of a model whose ``__pydantic_extra__`` is replaced."""
    copy = record.model_copy()
    object.__setattr__(copy, "__pydantic_extra__", extra)
    return copy


def state_of(record: pydantic.BaseModel) -> dict[object, object]:
    """Return a copy of a model's raw ``__dict__``."""
    return dict(object.__getattribute__(record, "__dict__"))


def fabricated_phase() -> typing.Any:
    """Return an exact-class phase object that is not a real member."""
    fabricated = object.__new__(schema.ColdPhaseKind)
    assert type(fabricated) is schema.ColdPhaseKind
    assert all(fabricated is not member for member in schema.ColdPhaseKind)
    return fabricated


def genuine_tick(tmp_path: Path) -> schema.ColdTickRecord:
    """Return one genuine, schema-revalidated recording-off tick without a writer."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    snapshot = schema.validate_record(tick_for(off, 0))
    assert type(snapshot) is schema.ColdTickRecord
    return snapshot


# --------------------------------------------------------------- E1 round trip


@pytest.mark.parametrize(
    ("name", "raw"), ACCEPTED_SHAPES, ids=[case[0] for case in ACCEPTED_SHAPES]
)
def test_e1_every_projection_shape_round_trips_through_canonical_bytes(
    tmp_path: Path, name: str, raw: dict[str, object]
) -> None:
    """E1: a record from a real tick snapshot and an admitted projection round-trips."""
    del name
    projection = admit_cold_temperature_projection(raw)
    assert type(projection) is ColdTickTemperatureProjection
    tick = genuine_tick(tmp_path)
    record = validate(temperature_for(tick, temperature=projection))
    line = line_of(doc(record))

    decoded = temperature.decode_tick_temperature_document(json.loads(line))

    assert decoded == record
    assert decoded is not None and decoded.temperature == projection
    assert temperature.pairs_with(tick, record)
    canonical = temperature._canonical_json(doc(record))  # pyright: ignore[reportPrivateUsage]
    assert canonical == store.canonical_json(doc(record))
    assert line == canonical.encode("utf-8") + b"\n"


def test_e1_canonical_rendering_matches_the_store_over_hostile_shapes() -> None:
    """E1: the module's canonical text equals the store's for unicode, nesting, and order."""
    render = temperature._canonical_json  # pyright: ignore[reportPrivateUsage]
    for value in ({"b": 1, "a": [1.5, "é", None]}, {"z": {"y": True}}, ["\U0001f525", 0.0]):
        assert render(value) == store.canonical_json(value)
    with pytest.raises(ValueError):
        render(float("nan"))


def test_e1_record_carries_exactly_the_nine_closed_fields() -> None:
    """E1: the record's closed field set and configuration."""
    assert tuple(Record.model_fields) == (
        "schema_version",
        "stream",
        "run_id",
        "phase",
        "recorded_at_utc",
        "monotonic_seconds",
        "identity_sha256",
        "tick",
        "temperature",
    )
    config = dict(Record.model_config)
    assert config == {
        **config,
        "frozen": True,
        "extra": "forbid",
        "strict": True,
        "allow_inf_nan": False,
    }
    assert (temperature.TICK_TEMPERATURE_SCHEMA_VERSION, temperature.TICK_TEMPERATURE_STREAM) == (
        4,
        "tick_temperature",
    )
    assert temperature.TICK_TEMPERATURE_FILE_NAME == "tick_temperature.jsonl"


# --------------------------------------------------- E2 direct construction


def _forged_projection() -> ColdTickTemperatureProjection:
    """Return an exact-class projection whose content is invalid."""
    return valid_projection().model_copy(update={"status_packet_count": -1})


def _direct_refusals() -> list[tuple[str, dict[str, object]]]:
    """Return one invalid value per field for direct model construction."""
    return [
        ("version-3", {"schema_version": 3}),
        ("version-5", {"schema_version": 5}),
        ("version-true", {"schema_version": True}),
        ("version-float", {"schema_version": 4.0}),
        ("stream-wrong", {"stream": "tick"}),
        ("stream-subclass", {"stream": Str("tick_temperature")}),
        ("run-id-malformed", {"run_id": "cold"}),
        ("run-id-not-text", {"run_id": 1}),
        ("digest-malformed", {"identity_sha256": "F" * 64}),
        ("phase-raw-string", {"phase": "recording_off"}),
        ("phase-fabricated", {"phase": fabricated_phase()}),
        ("recorded-subclass", {"recorded_at_utc": Str(RECORDED)}),
        ("recorded-too-long", {"recorded_at_utc": "x" * (schema.MAX_TEXT_FIELD_BYTES + 1)}),
        ("monotonic-int", {"monotonic_seconds": 5}),
        ("monotonic-inf", {"monotonic_seconds": float("inf")}),
        ("monotonic-nan", {"monotonic_seconds": float("nan")}),
        ("tick-negative", {"tick": -1}),
        ("tick-bool", {"tick": True}),
        ("tick-float", {"tick": 1.0}),
        ("temperature-null", {"temperature": None}),
        ("temperature-dict", {"temperature": celsius_agree()}),
        (
            "temperature-subclass",
            {"temperature": SubProjection.model_validate(members(celsius_agree()))},
        ),
        ("temperature-forged", {"temperature": _forged_projection()}),
        ("extra-field", {"extra": None}),
    ]


_DIRECT = _direct_refusals()


@pytest.mark.parametrize(("name", "update"), _DIRECT, ids=[case[0] for case in _DIRECT])
def test_e2_direct_construction_refuses_each_invalid_field(
    tmp_path: Path, name: str, update: dict[str, object]
) -> None:
    """E2: strict model construction refuses each invalid value in isolation."""
    del name
    tick = genuine_tick(tmp_path)
    temperature_for(tick)
    with pytest.raises(pydantic.ValidationError):
        temperature_for(tick, **update)


def test_e2_the_record_temperature_is_a_fresh_readmitted_projection(tmp_path: Path) -> None:
    """E2: the model stores a fresh re-admitted projection, never the supplied instance."""
    supplied = valid_projection()
    record = temperature_for(genuine_tick(tmp_path), temperature=supplied)
    assert record.temperature == supplied
    assert record.temperature is not supplied


def test_e2_direct_construction_readmits_nested_field_content(tmp_path: Path) -> None:
    """E2: a shape-consistent forged projection is refused by nested field re-admission.

    The forgery changes only ``projection_version``, so the projection's own
    after-validator (shape rules) still passes and ``model_validate`` returns it
    unchanged; only field-level re-admission inside the record can refuse it.
    """
    forged = valid_projection().model_copy(update={"projection_version": 2})
    assert ColdTickTemperatureProjection.model_validate(forged) is forged
    with pytest.raises(pydantic.ValidationError):
        temperature_for(genuine_tick(tmp_path), temperature=forged)


# ------------------------------------------------- E3 shared re-admission


def _str_subclass_keyed(record: pydantic.BaseModel) -> typing.Any:
    """Return a copy whose exact ``"tick"`` key is re-keyed as an equal ``str`` subclass.

    The field set still compares equal, so only the exact-key guard can refuse it.
    """
    data = state_of(record)
    value = data.pop("tick")
    data[Str("tick")] = value
    assert [type(key) for key in data if key == "tick"] == [Str]
    assert set(data) == set(Record.model_fields)
    return with_state(record, data)


def _e3_refusals(
    tick: schema.ColdTickRecord,
) -> list[tuple[str, object, schema.ColdEvidenceFailure]]:
    """Return one isolated re-admission refusal per guard."""
    valid = temperature_for(tick)
    not_validated = EFail.RECORD_NOT_VALIDATED
    missing: dict[str, typing.Any] = values_for(tick)
    del missing["tick"]
    forged: dict[str, typing.Any] = values_for(tick, temperature=_forged_projection())
    return [
        ("not-a-record", tick, not_validated),
        ("subclass", SubRecord.model_validate(values_for(tick), strict=True), not_validated),
        ("uninitialised", Record.__new__(Record), not_validated),
        ("constructed-forged-projection", Record.model_construct(**forged), not_validated),
        ("constructed-missing-field", Record.model_construct(**missing), not_validated),
        ("extra-state", with_extra(valid, {"extra": 1}), not_validated),
        ("extra-list", with_extra(valid, []), not_validated),
        ("dict-subclass-state", with_state(valid, DictSubclass(state_of(valid))), not_validated),
        ("non-str-key", with_state(valid, {**state_of(valid), 1: 1}), not_validated),
        ("extra-key", with_state(valid, {**state_of(valid), "extra": 1}), not_validated),
        ("str-subclass-key", _str_subclass_keyed(valid), not_validated),
        ("tick-negative", valid.model_copy(update={"tick": -1}), not_validated),
        ("tick-list", valid.model_copy(update={"tick": [1]}), not_validated),
        (
            "temperature-forged",
            valid.model_copy(update={"temperature": _forged_projection()}),
            not_validated,
        ),
        (
            "temperature-dict",
            valid.model_copy(update={"temperature": celsius_agree()}),
            not_validated,
        ),
        ("phase-fabricated", valid.model_copy(update={"phase": fabricated_phase()}), not_validated),
        ("phase-int", valid.model_copy(update={"phase": 1}), not_validated),
        ("recorded-int", valid.model_copy(update={"recorded_at_utc": 5}), not_validated),
        (
            "recorded-subclass",
            valid.model_copy(update={"recorded_at_utc": Str(RECORDED)}),
            not_validated,
        ),
        ("monotonic-int", valid.model_copy(update={"monotonic_seconds": 2}), not_validated),
        (
            "tick-beyond-bound",
            valid.model_copy(update={"tick": INT_BOUND + 1}),
            EFail.JSON_VALUE_TYPE_NOT_ADMITTED,
        ),
        (
            "recorded-2050-bytes",
            valid.model_copy(update={"recorded_at_utc": "é" * 1025}),
            EFail.TEXT_FIELD_TOO_LARGE,
        ),
        (
            "recorded-lone-surrogate",
            valid.model_copy(update={"recorded_at_utc": "\ud800"}),
            EFail.JSON_VALUE_TYPE_NOT_ADMITTED,
        ),
    ]


def test_e3_each_readmission_guard_refuses_in_isolation(tmp_path: Path) -> None:
    """E3: each re-admission guard raises its closed, chain-free member."""
    for name, candidate, failure in _e3_refusals(genuine_tick(tmp_path)):
        with pytest.raises(schema.ColdEvidenceError) as raised:
            validate(candidate)
        assert raised.value.failure is failure, name
        assert raised.value.__cause__ is None, name
        assert raised.value.__context__ is None, name


def test_e3_a_valid_record_returns_a_fresh_snapshot(tmp_path: Path) -> None:
    """E3: a valid record is re-admitted as an equal, fresh snapshot with a fresh projection."""
    record = temperature_for(genuine_tick(tmp_path))
    snapshot = validate(record)
    assert snapshot == record
    assert snapshot is not record
    assert snapshot.temperature is not record.temperature


def test_e3_the_byte_limit_admits_exactly_2048_utf8_bytes(tmp_path: Path) -> None:
    """E3: 1024 two-byte characters (2048 bytes) are admitted; the writer's tick limit agrees."""
    tick = genuine_tick(tmp_path)
    exact = validate(temperature_for(tick).model_copy(update={"recorded_at_utc": "é" * 1024}))
    assert len(exact.recorded_at_utc.encode("utf-8")) == schema.MAX_TEXT_FIELD_BYTES
    beyond = tick.model_copy(update={"recorded_at_utc": "é" * 1025})
    expect_evidence(EFail.TEXT_FIELD_TOO_LARGE, lambda: schema.validate_record(beyond))


def test_e3_constructed_instances_with_valid_content_are_admitted(tmp_path: Path) -> None:
    """E3 (honest limit): valid content is admitted as a fresh snapshot, provenance is not."""
    tick = genuine_tick(tmp_path)
    values: dict[str, typing.Any] = values_for(tick)
    constructed = Record.model_construct(**values)
    snapshot = validate(constructed)
    assert snapshot == temperature_for(tick)
    assert snapshot is not constructed


def test_e3_model_validate_on_an_instance_is_not_readmission(tmp_path: Path) -> None:
    """E3: ``model_validate`` returns a forged record unchanged; the boundary refuses it."""
    forged = temperature_for(genuine_tick(tmp_path)).model_copy(update={"tick": -1})
    assert Record.model_validate(forged) is forged
    expect_evidence(EFail.RECORD_NOT_VALIDATED, lambda: validate(forged))


def test_e3_the_canonical_record_byte_cap_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3: a canonical record beyond the shared record cap is refused (cap lowered to reach it)."""
    record = temperature_for(genuine_tick(tmp_path))
    size = len(store.canonical_json(doc(record)).encode("utf-8"))
    monkeypatch.setattr(temperature, "MAX_RECORD_BYTES", size)
    assert validate(record) == record
    monkeypatch.setattr(temperature, "MAX_RECORD_BYTES", size - 1)
    expect_evidence(EFail.RECORD_TOO_LARGE, lambda: validate(record))


# ---------------------------------------------------------- E4 containment


def _canary_projections() -> list[ColdTickTemperatureProjection]:
    """Return projections carrying a credential-shaped canary in three places."""
    return [
        with_extra(valid_projection(), {SECRET: SECRET}),
        valid_projection().model_copy(update={"outcome": SECRET}),
        valid_projection().model_copy(update={"status_packet_count": SECRET}),
    ]


def _assert_contained(error: BaseException) -> None:
    """Assert a closed error renders no canary and carries no cause or context."""
    rendered = "".join(traceback.format_exception(error))
    for text in (str(error), repr(error), repr(error.args), rendered):
        assert SECRET not in text
    assert error.__cause__ is None
    assert error.__context__ is None


def test_e4_readmission_and_decode_return_content_free_failures(tmp_path: Path) -> None:
    """E4: return-only boundaries carry no canary in their closed value."""
    from roastpilot_agent.cold_characterisation import temperature_projection as leaf

    for projection in _canary_projections():
        result = leaf.readmit_cold_temperature_projection(projection)
        assert type(result) is leaf.ColdTemperatureProjectionFailure
        assert SECRET not in repr(result) and SECRET not in str(result)
    base = doc(temperature_for(genuine_tick(tmp_path)))
    projection = base["temperature"]
    for document in (
        {**base, SECRET: SECRET},
        {**base, "temperature": {**projection, "outcome": SECRET}},
        {**base, "temperature": {**projection, "status_packet_count": SECRET}},
        {**base, "tick": SECRET},
    ):
        assert temperature.decode_tick_temperature_document(document) is None


def test_e4_the_shared_boundary_and_writer_raise_contained_errors(tmp_path: Path) -> None:
    """E4: ``validate_tick_temperature_record`` and the writer never render the canary."""
    writer, _off, tick = _refusal_writer(tmp_path)
    valid = temperature_for(tick)
    candidates: list[object] = [
        valid.model_copy(update={"temperature": projection}) for projection in _canary_projections()
    ]
    candidates += [
        with_extra(valid, {SECRET: SECRET}),
        valid.model_copy(update={"tick": SECRET}),
        valid.model_copy(update={"run_id": SECRET}),
    ]
    for candidate in candidates:
        with pytest.raises(schema.ColdEvidenceError) as raised:
            validate(candidate)
        _assert_contained(raised.value)
        with pytest.raises(schema.ColdEvidenceError) as raised:
            writer.append_tick_temperature(typing.cast(typing.Any, candidate))
        _assert_contained(raised.value)
    writer.append_tick_temperature(valid)


def test_e4_the_v5_reader_raises_contained_errors(tmp_path: Path) -> None:
    """E4: crafted lines carrying the canary refuse with a content-free store error."""
    run, _digest = sealed_run(tmp_path)
    document = doc(temperature_for(run.on_ticks[0]))
    crafted: list[dict[str, typing.Any]] = [
        {**document, SECRET: SECRET},
        {**document, "temperature": {**document["temperature"], "outcome": SECRET}},
        {**document, "temperature": {**document["temperature"], "status_packet_count": SECRET}},
        {**document, "temperature": {**document["temperature"], SECRET: SECRET}},
    ]
    for index, item in enumerate(crafted):
        digest = rewrite(run.root, TEMPERATURE_ON, line_of(item))
        with pytest.raises(store.ColdEvidenceStoreError) as raised:
            read5(run.root, digest)
        assert raised.value.failure is Failure.LINE_MALFORMED, index
        _assert_contained(raised.value)


# --------------------------------------------------------- W1-W3 writer


def test_w1_paired_run_is_written_canonically_and_reads_present(tmp_path: Path) -> None:
    """W1: both phases pair; disk bytes are canonical lines; V5 reads ``PRESENT``."""
    run, digest = sealed_run(tmp_path)
    expected = [temperature_for(tick) for tick in (*run.off_ticks, *run.on_ticks)]
    for path, records in ((TEMPERATURE_OFF, expected[:2]), (TEMPERATURE_ON, expected[2:])):
        assert (run_dir(run.root) / path).read_bytes() == lines(*records)

    v5 = read5(run.root, digest)

    assert v5.tick_temperature_state is TState.PRESENT
    assert list(v5.tick_temperatures) == expected
    v4_fields = {name: getattr(v5, name) for name in reader.ColdRetainedRunV4.model_fields}
    assert v4_fields["terminal_state"] is terminal.ColdFailedRunTerminalEvidenceState.ABSENT
    ticks = [
        record
        for stream in v5.run.streams
        for record in stream.records
        if type(record) is schema.ColdTickRecord
    ]
    assert len(ticks) == 4


def _refusal_writer(
    tmp_path: Path,
) -> tuple[store.ColdEvidenceWriter, schema.ColdRunHeader, schema.ColdTickRecord]:
    """Open a recording-off run with one written, unpaired tick."""
    writer, _root, off = off_only_run(tmp_path)
    tick = tick_for(off, 0)
    writer.append(tick)
    snapshot = schema.validate_record(tick)
    assert type(snapshot) is schema.ColdTickRecord
    return writer, off, snapshot


def _still_usable(writer: store.ColdEvidenceWriter, tick: schema.ColdTickRecord) -> None:
    """A valid pairing append still succeeds after a refusal."""
    writer.append_tick_temperature(temperature_for(tick))


def test_w2_tick_not_retained_leaves_the_writer_usable(tmp_path: Path) -> None:
    """W2: no tick in the phase yet."""
    writer, _root, off = off_only_run(tmp_path)
    tick = schema.validate_record(tick_for(off, 0))
    assert type(tick) is schema.ColdTickRecord
    expect_temperature(
        TFail.TICK_NOT_RETAINED, lambda: writer.append_tick_temperature(temperature_for(tick))
    )
    writer.append(tick)
    _still_usable(writer, tick)


def test_w2_phase_not_latest(tmp_path: Path) -> None:
    """W2: a recording-off record after the recording-on header is bound."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    off_tick = schema.validate_record(tick_for(off, 0))
    assert type(off_tick) is schema.ColdTickRecord
    writer.append(off_tick)
    on = on_header(tmp_path, root)
    writer.append(on)
    expect_temperature(
        TFail.PHASE_NOT_LATEST, lambda: writer.append_tick_temperature(temperature_for(off_tick))
    )
    on_tick = schema.validate_record(tick_for(on, 0))
    assert type(on_tick) is schema.ColdTickRecord
    writer.append(on_tick)
    _still_usable(writer, on_tick)


@pytest.mark.parametrize(
    "update",
    [{"tick": 1}, {"recorded_at_utc": "2026-09-26T12:00:02Z"}, {"monotonic_seconds": 2.5}],
    ids=["tick", "recorded-at", "monotonic"],
)
def test_w2_pairing_mismatch(tmp_path: Path, update: dict[str, object]) -> None:
    """W2: a record whose tick index, recorded time, or monotonic seconds differs."""
    writer, _off, tick = _refusal_writer(tmp_path)
    expect_temperature(
        TFail.PAIRING_MISMATCHED,
        lambda: writer.append_tick_temperature(temperature_for(tick, **update)),
    )
    _still_usable(writer, tick)


def test_w2_temperature_duplicated(tmp_path: Path) -> None:
    """W2: a second temperature for the same latest tick; a new tick pairs again."""
    writer, off, tick = _refusal_writer(tmp_path)
    writer.append_tick_temperature(temperature_for(tick))
    expect_temperature(
        TFail.TEMPERATURE_DUPLICATED, lambda: writer.append_tick_temperature(temperature_for(tick))
    )
    following = schema.validate_record(tick_for(off, 1))
    assert type(following) is schema.ColdTickRecord
    writer.append(following)
    _still_usable(writer, following)


def test_w2_binding_refusals_precede_pairing(tmp_path: Path) -> None:
    """W2: run id, header, and digest binding refuse before any pairing rule."""
    writer, _off, tick = _refusal_writer(tmp_path)
    expect(
        Failure.RUN_ID_MISMATCHED,
        lambda: writer.append_tick_temperature(temperature_for(tick, run_id=OTHER_RUN_ID)),
    )
    expect(
        Failure.HEADER_MISSING,
        lambda: writer.append_tick_temperature(temperature_for(tick, phase=ON)),
    )
    expect(
        Failure.IDENTITY_DIGEST_MISMATCHED,
        lambda: writer.append_tick_temperature(
            temperature_for(tick, identity_sha256="f" * 64, tick=7)
        ),
    )
    _still_usable(writer, tick)


def test_w2_forged_record_is_refused_by_readmission(tmp_path: Path) -> None:
    """W2: a forged record raises the closed evidence error and the writer stays usable."""
    writer, _off, tick = _refusal_writer(tmp_path)
    forged = temperature_for(tick).model_copy(update={"tick": -1})
    expect_evidence(EFail.RECORD_NOT_VALIDATED, lambda: writer.append_tick_temperature(forged))
    _still_usable(writer, tick)


def test_w3_append_after_the_terminal_is_refused(tmp_path: Path) -> None:
    """W3: a failed-run terminal ends every later append, including temperatures."""
    writer, off, tick = _refusal_writer(tmp_path)
    writer.append_failed_run_terminal(
        terminal.build_failed_run_terminal_record(
            off,
            advisory_settlement=terminal.ColdFailedRunAdvisorySettlement.NOT_RECORDED_CLOCK_INVALID,
            provider_cancellation=terminal.ColdFailedRunProviderCancellation.NO_PROVIDER_TASK,
            lifecycle_records_retained=0,
            advisory_attempt_records_retained=0,
        )
    )
    with pytest.raises(terminal.ColdFailedRunTerminalError) as raised:
        writer.append_tick_temperature(temperature_for(tick))
    assert raised.value.failure is terminal.ColdFailedRunTerminalFailure.APPENDED_AFTER_TERMINAL


def test_w3_sealed_writer_refuses(tmp_path: Path) -> None:
    """W3: a sealed writer refuses a temperature append."""
    writer, _off, tick = _refusal_writer(tmp_path)
    writer.seal()
    expect(Failure.WRITER_SEALED, lambda: writer.append_tick_temperature(temperature_for(tick)))


def _paired(writer: store.ColdEvidenceWriter) -> dict[schema.ColdPhaseKind, schema.ColdTickRecord]:
    """Return a copy of the writer's committed pairing state."""
    return dict(writer._paired_tick)  # pyright: ignore[reportPrivateUsage]


def test_w3_write_fault_poisons_without_advancing_the_pairing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3: a write fault poisons the writer and leaves the pairing state unchanged."""
    writer, _off, tick = _refusal_writer(tmp_path)

    def fail(_descriptor: int, _data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_all", fail)
    expect(Failure.WRITE_FAILED, lambda: writer.append_tick_temperature(temperature_for(tick)))
    assert _paired(writer) == {}
    expect(Failure.WRITER_POISONED, lambda: writer.append_tick_temperature(temperature_for(tick)))


def test_w3_write_interrupt_poisons_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3: a non-``Exception`` interruption during the write poisons and propagates."""
    writer, _off, tick = _refusal_writer(tmp_path)

    def interrupt(_descriptor: int, _data: bytes) -> None:
        raise _Interrupt

    monkeypatch.setattr(store, "_write_all", interrupt)
    with pytest.raises(_Interrupt):
        writer.append_tick_temperature(temperature_for(tick))
    assert _paired(writer) == {}
    expect(Failure.WRITER_POISONED, lambda: writer.append_tick_temperature(temperature_for(tick)))


def test_w3_unexpected_check_failure_poisons_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3: an unexpected failure while checking poisons the writer and propagates."""
    writer, _off, tick = _refusal_writer(tmp_path)

    def broken(_tick: object, _record: object) -> bool:
        raise RuntimeError("broken")

    monkeypatch.setattr(store, "pairs_with", broken)
    with pytest.raises(RuntimeError):
        writer.append_tick_temperature(temperature_for(tick))
    expect(Failure.WRITER_POISONED, lambda: writer.append_tick_temperature(temperature_for(tick)))


def test_w3_tick_write_fault_does_not_record_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3: the latest-tick state advances only after a durable tick write."""
    writer, off, _tick = _refusal_writer(tmp_path)
    before = dict(writer._last_tick)  # pyright: ignore[reportPrivateUsage]

    def fail(_descriptor: int, _data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_all", fail)
    expect(Failure.WRITE_FAILED, lambda: writer.append(tick_for(off, 1)))
    assert writer._last_tick == before  # pyright: ignore[reportPrivateUsage]


def test_w3_only_ticks_advance_the_latest_tick(tmp_path: Path) -> None:
    """W3: a non-tick record leaves the latest tick unchanged."""
    from tests.test_cold_characterisation_evidence_builders import host_for

    writer, off, tick = _refusal_writer(tmp_path)
    writer.append(host_for(off))
    assert writer._last_tick == {OFF: tick}  # pyright: ignore[reportPrivateUsage]
    _still_usable(writer, tick)


# ----------------------------------------------------------- H1-H2 history


def test_h1_existing_trees_read_unchanged_and_v5_reads_absent(tmp_path: Path) -> None:
    """H1: a tree written as today reads under V1-V4 and V5 reads ``ABSENT`` with equal fields."""
    from tests.test_cold_characterisation_evidence_advisory import read3
    from tests.test_cold_characterisation_evidence_lifecycle import read2
    from tests.test_cold_characterisation_evidence_reader import read as read1

    root, sealed_receipt, _records = write_full_run(tmp_path / "full")
    digest = sealed_receipt.manifest_sha256
    v1 = read1(root, digest)
    assert read2(root, digest).run == v1
    assert read3(root, digest).run == v1
    assert read4(root, digest).run == v1
    v5 = read5(root, digest)
    assert v5.run == v1
    assert v5.tick_temperature_state is TState.ABSENT
    assert v5.tick_temperatures == ()
    root, digest, _scenario, _record = sealed(tmp_path / "terminal")
    v4 = read4(root, digest)
    v5 = read5(root, digest)
    assert {name: getattr(v5, name) for name in reader.ColdRetainedRunV4.model_fields} == {
        name: getattr(v4, name) for name in reader.ColdRetainedRunV4.model_fields
    }
    assert (v5.tick_temperature_state, v5.tick_temperatures) == (TState.ABSENT, ())


@pytest.mark.parametrize("name", ["v1", "v2", "v3", "v4"])
def test_h2_earlier_readers_refuse_a_tree_holding_the_file(tmp_path: Path, name: str) -> None:
    """H2: V1-V4 refuse a tree that holds a tick-temperature file."""
    from tests.test_cold_characterisation_evidence_advisory import read3
    from tests.test_cold_characterisation_evidence_lifecycle import read2
    from tests.test_cold_characterisation_evidence_reader import read as read1

    readers: dict[str, typing.Callable[[str, str], object]] = {
        "v1": read1,
        "v2": read2,
        "v3": read3,
        "v4": read4,
    }
    run, digest = sealed_run(tmp_path)
    expect(Failure.ENTRY_PATH_INVALID, lambda: readers[name](run.root, digest))


# ------------------------------------------------------------- H3 V5 refusals


def test_h3_empty_file_is_refused_by_the_shared_line_framer(tmp_path: Path) -> None:
    """H3: an empty or blank file never reaches decoding; the shared framer refuses it."""
    run, _digest = sealed_run(tmp_path / "empty")
    digest = rewrite(run.root, TEMPERATURE_ON, b"")
    expect(Failure.LINE_MALFORMED, lambda: read5(run.root, digest))
    run, _digest = sealed_run(tmp_path / "blank")
    digest = rewrite(run.root, TEMPERATURE_ON, b"\n")
    expect(Failure.LINE_MALFORMED, lambda: read5(run.root, digest))


def test_h3_stream_empty_guard_refuses_a_file_that_frames_no_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H3: the reader's own empty-stream guard refuses a file that frames zero lines.

    The shared framer already refuses an empty file, so the guard is isolated by
    having the framed read of the tick-temperature file return no lines.
    """
    run, digest = sealed_run(tmp_path)
    real = store.read_verified_lines

    def framed(
        tree: store.ColdVerifiedTree, relative_path: str, *, max_line_bytes: int
    ) -> tuple[bytes, ...]:
        if relative_path == TEMPERATURE_ON:
            return ()
        return real(tree, relative_path, max_line_bytes=max_line_bytes)

    monkeypatch.setattr(reader, "read_verified_lines", framed)
    expect_temperature(TFail.STREAM_EMPTY, lambda: read5(run.root, digest))


def test_h3_pairing_refusals(tmp_path: Path) -> None:
    """H3: missing, reordered, duplicated, altered, orphan, and absent-phase files are refused."""

    def altered(on: list[Record], **update: object) -> bytes:
        return lines(on[0]) + line_of({**doc(on[1]), **update})

    cases: dict[str, typing.Callable[[list[Record]], dict[str, bytes | None]]] = {
        "last-line-missing": lambda on: {TEMPERATURE_ON: lines(on[0])},
        "reordered": lambda on: {TEMPERATURE_ON: lines(on[1], on[0])},
        "duplicated": lambda on: {TEMPERATURE_ON: lines(on[0], on[0], on[1])},
        "duplicated-in-place": lambda on: {TEMPERATURE_ON: lines(on[0], on[0])},
        "on-file-absent": lambda _on: {TEMPERATURE_ON: None},
        "off-file-absent": lambda _on: {TEMPERATURE_OFF: None},
        "recorded-altered": lambda on: {
            TEMPERATURE_ON: altered(on, recorded_at_utc="2026-09-26T12:00:09Z")
        },
        "monotonic-altered": lambda on: {TEMPERATURE_ON: altered(on, monotonic_seconds=9.5)},
    }
    for index, (name, files) in enumerate(cases.items()):
        run, _digest = sealed_run(tmp_path / str(index))
        digest = craft(run.root, files([temperature_for(tick) for tick in run.on_ticks]))
        with pytest.raises(temperature.ColdTickTemperatureError) as raised:
            read5(run.root, digest)
        assert raised.value.failure is TFail.PAIRING_MISMATCHED, name


def test_h3_a_file_in_a_phase_without_ticks_is_refused(tmp_path: Path) -> None:
    """H3: a temperature line in a phase whose header is bound but which retains no tick."""
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    off_tick = tick_for(off, 0)
    writer.append(off_tick)
    writer.append_tick_temperature(temperature_for(off_tick))
    on = on_header(tmp_path, root)
    writer.append(on)
    writer.seal()
    digest = craft(root, {TEMPERATURE_ON: lines(temperature_for(tick_for(on, 0)))})
    expect_temperature(TFail.PAIRING_MISMATCHED, lambda: read5(root, digest))


def test_h3_unpaired_ticks_in_a_tree_with_some_temperatures_are_refused(tmp_path: Path) -> None:
    """H3: the writer allows a later unpaired tick; the V5 reader's global pairing refuses it."""
    run = paired_run(tmp_path)
    run.writer.append(tick_for(run.on, 2))
    digest = run.writer.seal().manifest_sha256
    expect_temperature(TFail.PAIRING_MISMATCHED, lambda: read5(run.root, digest))


def test_h3_a_file_in_a_phase_without_a_header_is_refused(tmp_path: Path) -> None:
    """H3: a recording-on file with only the recording-off header bound."""
    writer, root, off = off_only_run(tmp_path)
    tick = tick_for(off, 0)
    writer.append(tick)
    writer.append_tick_temperature(temperature_for(tick))
    writer.seal()
    on = on_header(tmp_path, root)
    digest = craft(root, {TEMPERATURE_ON: lines(temperature_for(tick_for(on, 0)))})
    expect(Failure.HEADER_MISSING, lambda: read5(root, digest))


Document = dict[str, typing.Any]
Change = typing.Callable[[Document], object]


def _set(**update: object) -> Change:
    """Return a change that replaces top-level document values."""
    return lambda base: {**base, **update}


def _set_projection(**update: object) -> Change:
    """Return a change that replaces values inside the nested projection."""
    return lambda base: {**base, "temperature": {**base["temperature"], **update}}


def _drop(key: str) -> Change:
    """Return a change that removes one top-level key."""
    return lambda base: {name: value for name, value in base.items() if name != key}


def _reread(
    tmp_path: Path,
    change: Change,
    render: typing.Callable[[object], bytes] = line_of,
) -> tuple[str, str]:
    """Seal the paired run, then re-craft its first recording-on line from that same run.

    The first line is the run's own genuine recording-on document (valid run,
    phase, digest, and pairing with ``on_ticks[0]``) with only the intended change
    applied; the second line stays genuine, so every other rule holds.
    """
    run, _digest = sealed_run(tmp_path)
    first = doc(temperature_for(run.on_ticks[0]))
    second = lines(temperature_for(run.on_ticks[1]))
    return run.root, rewrite(run.root, TEMPERATURE_ON, render(change(first)) + second)


def test_h3_re_sealed_genuine_lines_from_the_same_run_read_present(tmp_path: Path) -> None:
    """H3 control: the unchanged re-crafted lines re-seal and read ``PRESENT``."""
    run, _digest = sealed_run(tmp_path)
    expected = tuple(temperature_for(tick) for tick in (*run.off_ticks, *run.on_ticks))
    first = doc(temperature_for(run.on_ticks[0]))
    second = lines(temperature_for(run.on_ticks[1]))
    digest = rewrite(run.root, TEMPERATURE_ON, line_of(first) + second)

    v5 = read5(run.root, digest)

    assert v5.tick_temperature_state is TState.PRESENT
    assert v5.tick_temperatures == expected
    assert all(record.temperature == valid_projection() for record in v5.tick_temperatures)


def test_h3_unknown_versions_are_refused_before_any_other_malformation(tmp_path: Path) -> None:
    """H3: missing, other, float, and bool versions are unknown; that wins over later faults."""
    changes: list[Change] = [
        _drop("schema_version"),
        *(_set(schema_version=version) for version in (1, 2, 3, 5, 4.0, True)),
        _set(schema_version=5, stream="tick", extra=1, temperature=None),
    ]
    for index, change in enumerate(changes):
        root, digest = _reread(tmp_path / str(index), change)
        expect(Failure.SCHEMA_VERSION_UNKNOWN, functools.partial(read5, root, digest))


_MALFORMED_CHANGES: list[tuple[str, Change]] = [
    ("stream-tick", _set(stream="tick")),
    ("stream-not-text", _set(stream=4)),
    ("extra-key", _set(extra=1)),
    ("missing-key", _drop("tick")),
    ("unknown-phase", _set(phase="recording_sideways")),
    ("phase-not-text", _set(phase=1)),
    ("projection-null", _set(temperature=None)),
    ("projection-not-object", _set(temperature=[])),
    ("projection-version", _set_projection(projection_version=2)),
    ("projection-field-set", _set_projection(extra=None)),
    ("projection-outcome", _set_projection(outcome="stale")),
    ("projection-type", _set_projection(status_packet_count="5")),
    ("projection-integer-temperature", _set_projection(last_packet_bean_temp_c=20)),
    ("projection-token", _set_projection(reported_temperature_unit="auto")),
    ("projection-value", _set_projection(status_packet_count=-1)),
    ("projection-shape", _set_projection(value_agreement="disagree")),
    ("tick-negative", _set(tick=-1)),
    ("tick-float", _set(tick=0.0)),
    ("monotonic-integer", _set(monotonic_seconds=2)),
    ("bad-run-id", _set(run_id="cold")),
    ("bad-digest", _set(identity_sha256="F" * 64)),
]


def test_h3_malformed_lines_are_refused(tmp_path: Path) -> None:
    """H3: every closed decode refusal reads as ``LINE_MALFORMED``."""
    for index, (name, change) in enumerate(_MALFORMED_CHANGES):
        root, digest = _reread(tmp_path / str(index), change)
        with pytest.raises(store.ColdEvidenceStoreError) as raised:
            read5(root, digest)
        assert raised.value.failure is Failure.LINE_MALFORMED, name


def test_h3_a_line_from_another_phase_is_refused(tmp_path: Path) -> None:
    """H3: a genuine recording-off line placed in the recording-on file."""
    run, _digest = sealed_run(tmp_path)
    digest = rewrite(run.root, TEMPERATURE_ON, lines(temperature_for(run.off_ticks[0])))
    expect(Failure.LINE_MALFORMED, lambda: read5(run.root, digest))


@pytest.mark.parametrize("data", [b"[]\n", b"3\n", b'"x"\n', b"{\n", b"null\n"])
def test_h3_non_object_and_invalid_json_lines_are_refused(tmp_path: Path, data: bytes) -> None:
    """H3: a line that is not one strict JSON object is malformed."""
    run, _digest = sealed_run(tmp_path)
    digest = rewrite(run.root, TEMPERATURE_ON, data)
    expect(Failure.LINE_MALFORMED, lambda: read5(run.root, digest))


def test_h3_non_canonical_lines_are_refused(tmp_path: Path) -> None:
    """H3: unsorted keys or non-compact separators are not canonical; only spelling differs."""

    def unsorted(document: object) -> bytes:
        return json.dumps(document, separators=(",", ":")).encode() + b"\n"

    def spaced(document: object) -> bytes:
        return json.dumps(document, sort_keys=True).encode() + b"\n"

    for index, render in enumerate((unsorted, spaced)):
        root, digest = _reread(tmp_path / str(index), lambda base: base, render)
        first = (run_dir(root) / TEMPERATURE_ON).read_bytes().split(b"\n")[0] + b"\n"
        assert first != line_of(json.loads(first))
        expect(Failure.LINE_NOT_CANONICAL, functools.partial(read5, root, digest))


def test_h3_reader_binding_refusals(tmp_path: Path) -> None:
    """H3: another valid run id, or a wrong valid-hex digest alone, is refused by binding."""
    root, digest = _reread(tmp_path / "run", _set(run_id=OTHER_RUN_ID))
    expect(Failure.RUN_ID_MISMATCHED, lambda: read5(root, digest))
    root, digest = _reread(tmp_path / "digest", _set(identity_sha256="f" * 64))
    expect(Failure.IDENTITY_DIGEST_MISMATCHED, lambda: read5(root, digest))


def test_h3_walker_bounds_apply_before_decoding(tmp_path: Path) -> None:
    """H3: a crafted ``10**32`` tick keeps the shared walker's member."""
    root, digest = _reread(tmp_path / "beyond", _set(tick=INT_BOUND + 1))
    expect_evidence(EFail.JSON_VALUE_TYPE_NOT_ADMITTED, lambda: read5(root, digest))


def test_h3_unknown_record_file_is_refused_by_v5(tmp_path: Path) -> None:
    """H3: any other ``records/`` entry is refused, never ignored."""
    run, _digest = sealed_run(tmp_path)
    digest = craft(run.root, {"records/recording_on/other.jsonl": b"{}\n"})
    expect(Failure.ENTRY_PATH_INVALID, lambda: read5(run.root, digest))


def test_h3_tampered_bytes_under_the_old_receipt_are_refused_before_any_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H3: altered temperature bytes under the original receipt refuse at verification."""
    calls: list[bytes] = []
    real = reader._read_tick_temperature_line  # pyright: ignore[reportPrivateUsage]

    def spy(line: bytes, **kwargs: typing.Any) -> temperature.ColdTickTemperatureRecord:
        calls.append(line)
        return real(line, **kwargs)

    monkeypatch.setattr(reader, "_read_tick_temperature_line", spy)
    run, digest = sealed_run(tmp_path)
    assert len(read5(run.root, digest).tick_temperatures) == 4
    assert len(calls) == 4
    calls.clear()
    path = run_dir(run.root) / TEMPERATURE_ON
    data = bytearray(path.read_bytes())
    data[data.index(b'"status_packet_count":5') + 22] = ord("6")
    path.write_bytes(bytes(data))
    expect(Failure.FILE_DIGEST_MISMATCHED, lambda: read5(run.root, digest))
    assert calls == []


# ------------------------------------------------------------------ H4 carrier


def test_h4_v5_carrier_states_must_match_their_records(tmp_path: Path) -> None:
    """H4: tick-temperature state must match its records; V4's checks still hold."""
    run, digest = sealed_run(tmp_path)
    genuine = read5(run.root, digest)
    fields: dict[str, typing.Any] = {name: getattr(genuine, name) for name in V5.model_fields}
    assert V5(**fields) == genuine
    updates: list[dict[str, typing.Any]] = [
        {"tick_temperature_state": TState.ABSENT},
        {"tick_temperature_state": TState.PRESENT, "tick_temperatures": ()},
        {"terminal_state": terminal.ColdFailedRunTerminalEvidenceState.PRESENT},
        {"lifecycle_state": lifecycle.ColdLifecycleEvidenceState.PRESENT},
        {"advisory_attempt_state": advisory.ColdAdvisoryAttemptEvidenceState.OPEN_TAIL},
    ]
    for update in updates:
        with pytest.raises(pydantic.ValidationError):
            V5(**{**fields, **update})
    absent_fields: dict[str, typing.Any] = {
        **fields,
        "tick_temperature_state": TState.ABSENT,
        "tick_temperatures": (),
    }
    absent = V5(**absent_fields)
    assert absent.tick_temperatures == ()


def test_h4_v5_is_flat_and_nests_no_earlier_carrier() -> None:
    """H4: V5 is a flat carrier; no field is an earlier carrier."""
    earlier = (reader.ColdRetainedRunV2, reader.ColdRetainedRunV3, reader.ColdRetainedRunV4)
    assert V5.__bases__ == (pydantic.BaseModel,)
    for field in V5.model_fields.values():
        assert field.annotation not in earlier
    assert list(V5.model_fields)[:7] == list(reader.ColdRetainedRunV4.model_fields)
    assert list(V5.model_fields)[7:] == ["tick_temperature_state", "tick_temperatures"]


# --------------------------------------------------------- H5 decode totality


def _json_kinds() -> list[object]:
    """Return one representative of every JSON kind, round-tripped through text."""
    values: list[object] = [
        None,
        True,
        False,
        0,
        -1,
        10**40,
        1.5,
        "",
        "observed",
        [],
        [1],
        {},
        {"k": 1},
    ]
    return [json.loads(json.dumps(value)) for value in values]


def test_h5_decode_is_total_over_json_derived_values(tmp_path: Path) -> None:
    """H5: any JSON value as the document or any field returns a record or ``None``."""
    base = doc(temperature_for(genuine_tick(tmp_path)))
    assert temperature.decode_tick_temperature_document(base) is not None
    results: list[object] = []
    for value in _json_kinds():
        results.append(temperature.decode_tick_temperature_document(value))
        for field in base:
            results.append(temperature.decode_tick_temperature_document({**base, field: value}))
        results.append(temperature.decode_tick_temperature_document({**base, "é": value}))
    for result in results:
        assert result is None or type(result) is Record
    assert temperature.decode_tick_temperature_document({1: 1}) is None


@pytest.mark.parametrize("value", [None, True, 0, 1.5, "x", [], [1], {}, {"k": 1}])
def test_h5_each_json_kind_as_the_projection_reads_malformed(tmp_path: Path, value: object) -> None:
    """H5: an invalid projection of every JSON kind reads exactly as ``LINE_MALFORMED``."""
    root, digest = _reread(tmp_path / "run", _set(temperature=value))
    expect(Failure.LINE_MALFORMED, lambda: read5(root, digest))


# ------------------------------------------------------------- pairing unit


def test_pairing_requires_every_identity_value(tmp_path: Path) -> None:
    """Pairing: each differing identity value breaks the pair; lengths must match."""
    tick = genuine_tick(tmp_path)
    record = temperature_for(tick)
    assert temperature.pairs_with(tick, record)
    for update in (
        {"run_id": OTHER_RUN_ID},
        {"phase": ON},
        {"identity_sha256": "f" * 64},
        {"tick": 1},
        {"recorded_at_utc": "2026-09-26T12:00:02Z"},
        {"monotonic_seconds": 2.5},
    ):
        assert not temperature.pairs_with(tick, temperature_for(tick, **update)), update
    temperature.check_tick_temperature_pairing((), ())
    temperature.check_tick_temperature_pairing((tick,), (record,))
    for ticks, records in (((tick,), ()), ((), (record,)), ((tick, tick), (record,))):
        expect_temperature(
            TFail.PAIRING_MISMATCHED,
            functools.partial(temperature.check_tick_temperature_pairing, ticks, records),
        )


# --------------------------------------------------------------- S1-S2 fences


_SOURCE = Path(temperature.__file__).read_text(encoding="utf-8")


def test_s1_module_imports_only_pure_modules_and_its_two_project_modules() -> None:
    """S1: only the schema and the projection leaf from the project; no I/O or effect module."""
    project: set[str] = set()
    others: set[str] = set()
    for node in ast.walk(ast.parse(_SOURCE)):
        if isinstance(node, ast.Import):
            others.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            if node.module.startswith("roastpilot_agent"):
                project.add(node.module)
            else:
                others.add(node.module)
    assert project == {
        "roastpilot_agent.cold_characterisation.evidence_schema",
        "roastpilot_agent.cold_characterisation.temperature_projection",
    }
    forbidden = {"os", "sys", "io", "subprocess", "socket", "pathlib", "asyncio", "logging", "time"}
    assert {name.split(".")[0] for name in others} & forbidden == set()
    assert others <= {"__future__", "collections.abc", "enum", "json", "math", "typing", "pydantic"}
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(_SOURCE))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "print" not in calls
    assert "isinstance" not in calls
    assert "except Exception" not in _SOURCE


def test_s2_vocabularies_are_closed_plain_enums() -> None:
    """S2: no ``StrEnum`` and no ``str`` mixin; the closed member lists."""
    assert [member.name for member in TState] == ["ABSENT", "PRESENT"]
    assert [member.name for member in TFail] == [
        "PHASE_NOT_LATEST",
        "TICK_NOT_RETAINED",
        "TEMPERATURE_DUPLICATED",
        "PAIRING_MISMATCHED",
        "STREAM_EMPTY",
    ]
    for kind in (TState, TFail):
        assert kind.__bases__ == (enum.Enum,)
        assert not issubclass(kind, (str, int))
        assert all(member.value == member.name.lower() for member in kind)
    assert "StrEnum" not in _SOURCE
    assert issubclass(temperature.ColdTickTemperatureError, Exception)
    assert temperature.ColdTickTemperatureError(TFail.STREAM_EMPTY).args == (
        "Cold tick temperature evidence refused.",
    )


def test_s2_runtime_callers_name_only_their_contracted_api() -> None:
    """FX2 (#997 T2): each runtime cold module names exactly its contracted D209 API.

    The owners are unconstrained; the engine, the orchestrator and the builders name
    exactly their sets (docstrings included); every other cold module names none.
    """
    texts = (
        "read_retained_run_v5",
        "append_tick_temperature",
        "evidence_temperature",
        "read_retained_run_v6",
        "append_mcp_candidate",
        "append_temperature_abort",
        "check_temperature_conformance",
        "check_revised_conformance",
        "evaluate_temperature",
    )
    owners = {
        "evidence_store.py",
        "evidence_reader.py",
        "evidence_temperature.py",
        "evidence_temperature_run.py",
        "temperature_screen.py",
        "temperature_conformance.py",
    }
    contracted: dict[str, set[str]] = {
        "engine.py": {
            "evidence_temperature",
            "append_tick_temperature",
            "append_temperature_abort",
            "evaluate_temperature",
        },
        "two_phase.py": {
            "evidence_temperature",
            "append_tick_temperature",
            "append_temperature_abort",
            "append_mcp_candidate",
            "read_retained_run_v6",
            "check_revised_conformance",
        },
        "evidence_builders.py": {"evidence_temperature"},
    }
    seen: set[str] = set()
    for path in sorted(COLD_PACKAGE.glob("*.py")):
        if path.name in owners:
            continue
        source = path.read_text(encoding="utf-8")
        present = {text for text in texts if text in source}
        assert present == contracted.get(path.name, set()), path.name
        seen.add(path.name)
    assert set(contracted) <= seen
    assert ColdTemperatureOutcome.OBSERVED.value == "observed"
