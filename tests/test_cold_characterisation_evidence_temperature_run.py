"""Behavioural tests for the D209 temperature-abort and MCP candidate evidence (#997 T1).

Records are written by the real descriptor-bound writer under a pytest temporary
root and read back through the real verified V6 reader; no MCP child, hardware, or
provider is involved.  Every accepted record is integrity data only: nothing here
screens a temperature, attests installed bytes, or qualifies a run.
"""

import ast
import enum
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
from roastpilot_agent.cold_characterisation import evidence_temperature_run as run_ev
from roastpilot_agent.cold_characterisation import evidence_terminal as terminal
from tests.test_cold_characterisation_evidence_builders import RUN_ID, header_for, tick_for
from tests.test_cold_characterisation_evidence_reader import line_of
from tests.test_cold_characterisation_evidence_store import (
    OFF,
    ON,
    SECRET,
    on_header,
    open_writer,
    run_dir,
)
from tests.test_cold_characterisation_evidence_temperature import craft, doc, temperature_for

Abort = run_ev.ColdTemperatureAbortRecord
Candidate = run_ev.ColdMcpCandidateRecord
Provenance = run_ev.ColdMcpCandidateProvenance
Kind = run_ev.ColdMcpCandidateArtefactKind
Assertion = run_ev.ColdMcpCandidateAssertion
RFail = run_ev.ColdTemperatureRunFailure
R = run_ev.ColdTemperatureScreenReason
Domain = schema.ColdAbortDomain
Failure = store.ColdEvidenceStoreFailure
EFail = schema.ColdEvidenceFailure
V6 = reader.ColdRetainedRunV6
OTHER_RUN_ID = "20260926T120000Z-cold-other"
RECORDED = "2026-09-26T12:00:01Z"
FOREIGN_DIGEST = "0" * 64
CANDIDATE_OFF = "records/recording_off/mcp_candidate.jsonl"
CANDIDATE_ON = "records/recording_on/mcp_candidate.jsonl"
ABORT_ON = "records/recording_on/temperature_abort.jsonl"
ABORT_OFF = "records/recording_off/temperature_abort.jsonl"
SOURCE = Path(run_ev.__file__)
#: Private helpers, reached through ``Any`` (unit exceptions).
PRIVATE: typing.Any = run_ev
#: Unvalidated construction, reached through ``Any`` so values may be forged.
CONSTRUCT: typing.Any = run_ev.ColdMcpCandidateProvenance.model_construct

PROVENANCE_DOC: typing.Final[dict[str, object]] = {
    "distribution": "coffee-roaster-mcp",
    "reported_version": "0.2.2",
    "artefact_kind": "wheel",
    "artefact_byte_length": 123456,
    "artefact_sha256": "a" * 64,
    "reviewed_source_revision": "b" * 40,
    "assertion": "operator_asserted_reviewed_candidate",
    "installed_bytes_attested": False,
}


class Str(str):
    """A ``str`` subclass (never admitted)."""


class IntSubclass(int):
    """An ``int`` subclass (never admitted)."""


class HostileKey(str):
    """A ``str`` subclass key that counts every hash and equality call."""

    calls = 0

    def __hash__(self) -> int:
        HostileKey.calls += 1
        return str.__hash__(self)

    def __eq__(self, other: object) -> bool:
        HostileKey.calls += 1
        return str.__eq__(self, other)


class DictSubclass(dict[object, object]):
    """A non-exact raw-state mapping (``__dict__`` accepts a ``dict`` subclass)."""


class SubAbort(Abort):
    """An abort record subclass with identical fields (never admitted)."""


class SubCandidate(Candidate):
    """A candidate record subclass with identical fields (never admitted)."""


class _Interrupt(BaseException):
    """A non-``Exception`` interruption."""


# ------------------------------------------------------------------- helpers


def provenance_doc(**update: object) -> dict[str, object]:
    """Return a valid raw provenance document with updates."""
    return {**PROVENANCE_DOC, **update}


def provenance_members(**update: object) -> dict[str, object]:
    """Return valid member-form provenance values with updates."""
    base: dict[str, object] = {
        **PROVENANCE_DOC,
        "artefact_kind": Kind.WHEEL,
        "assertion": Assertion.OPERATOR_ASSERTED_REVIEWED_CANDIDATE,
    }
    return {**base, **update}


def provenance(**update: object) -> run_ev.ColdMcpCandidateProvenance:
    """Strictly build one provenance through the model layer."""
    return Provenance.model_validate(provenance_members(**update), strict=True)


def candidate_values(header: schema.ColdRunHeader, /, **update: object) -> dict[str, object]:
    """Return valid candidate record values bound to ``header``, with updates."""
    base: dict[str, object] = {
        "schema_version": 6,
        "stream": "mcp_candidate",
        "run_id": header.run_id,
        "phase": header.phase,
        "recorded_at_utc": RECORDED,
        "monotonic_seconds": header.monotonic_seconds + 0.5,
        "identity_sha256": header.identity_sha256,
        "candidate": provenance(),
    }
    return {**base, **update}


def candidate_for(
    header: schema.ColdRunHeader, /, **update: object
) -> run_ev.ColdMcpCandidateRecord:
    """Strictly build one candidate record bound to ``header``."""
    return Candidate.model_validate(candidate_values(header, **update), strict=True)


def abort_values(
    tick: schema.ColdTickRecord, reason: run_ev.ColdTemperatureScreenReason, /, **update: object
) -> dict[str, object]:
    """Return valid abort record values naming ``tick``, with updates."""
    base: dict[str, object] = {
        "schema_version": 5,
        "stream": "temperature_abort",
        "run_id": tick.run_id,
        "phase": tick.phase,
        "recorded_at_utc": RECORDED,
        "monotonic_seconds": tick.monotonic_seconds + 0.25,
        "identity_sha256": tick.identity_sha256,
        "tick": tick.tick,
        "domain": Domain.ENGINE,
        "reason": reason,
    }
    return {**base, **update}


def abort_for(
    tick: schema.ColdTickRecord,
    reason: run_ev.ColdTemperatureScreenReason = R.OUTSIDE_SCREEN,
    /,
    **update: object,
) -> run_ev.ColdTemperatureAbortRecord:
    """Strictly build one abort record naming ``tick``."""
    return Abort.model_validate(abort_values(tick, reason, **update), strict=True)


class Run(typing.NamedTuple):
    """One open run with both phases bound, candidates, and paired ticks."""

    writer: store.ColdEvidenceWriter
    root: str
    off: schema.ColdRunHeader
    on: schema.ColdRunHeader
    off_ticks: tuple[schema.ColdTickRecord, ...]
    on_ticks: tuple[schema.ColdTickRecord, ...]


def v6_run(tmp_path: Path, *, ticks: int = 2, candidates: bool = True, pair: bool = True) -> Run:
    """Open a two-phase run: header, candidate, then each tick and its temperature."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    on = on_header(tmp_path, root)
    built: dict[schema.ColdPhaseKind, tuple[schema.ColdTickRecord, ...]] = {}
    for header in (off, on):
        writer.append(header)
        if candidates:
            writer.append_mcp_candidate(candidate_for(header))
        built[header.phase] = tuple(tick_for(header, index) for index in range(ticks))
        for tick in built[header.phase]:
            writer.append(tick)
            if pair:
                writer.append_tick_temperature(temperature_for(tick))
    return Run(writer, root, off, on, built[OFF], built[ON])


def read6(root: str, digest: str) -> reader.ColdRetainedRunV6:
    """Read the shared test run through the V6 reader."""
    return reader.read_retained_run_v6(root, run_id=RUN_ID, expected_manifest_sha256=digest)


def sealed_v6(tmp_path: Path) -> tuple[Run, str]:
    """Write and seal a run with two distinct aborts on the recording-on latest tick."""
    run = v6_run(tmp_path)
    run.writer.append_temperature_abort(abort_for(run.on_ticks[-1], R.OUTSIDE_SCREEN))
    run.writer.append_temperature_abort(abort_for(run.on_ticks[-1], R.VALUES_DISAGREE))
    return run, run.writer.seal().manifest_sha256


def expect_store(
    failure: store.ColdEvidenceStoreFailure, call: typing.Callable[[], object]
) -> None:
    """Assert one call raises exactly one closed store failure."""
    with pytest.raises(store.ColdEvidenceStoreError) as raised:
        call()
    assert raised.value.failure is failure


def expect_run(
    failure: run_ev.ColdTemperatureRunFailure, call: typing.Callable[[], object]
) -> None:
    """Assert one call raises exactly one closed, chain-free temperature run failure."""
    with pytest.raises(run_ev.ColdTemperatureRunError) as raised:
        call()
    assert raised.value.failure is failure
    assert raised.value.args == ("Cold temperature run evidence refused.",)
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


def rekeyed(record: pydantic.BaseModel, name: str, key: str) -> typing.Any:
    """Return a copy whose ``name`` key is replaced by ``key`` (original removed)."""
    state = state_of(record)
    value = state.pop(name)
    state[key] = value
    copy = with_state(record, state)
    keys = list(object.__getattribute__(copy, "__dict__"))
    assert name not in [k for k in keys if type(k) is str]
    assert any(type(k) is not str for k in keys)
    return copy


def fabricated(enum_type: type[enum.Enum]) -> typing.Any:
    """Return an exact-class enum object that is not a real member."""
    value = object.__new__(enum_type)
    assert type(value) is enum_type
    assert all(value is not member for member in enum_type)
    return value


def genuine_tick(tmp_path: Path) -> schema.ColdTickRecord:
    """Return one genuine recording-off tick snapshot without a writer."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    off = header_for(tmp_path, str(tmp_path.resolve()), OFF)
    snapshot = schema.validate_record(tick_for(off, 0))
    assert type(snapshot) is schema.ColdTickRecord
    return snapshot


def genuine_header(tmp_path: Path) -> schema.ColdRunHeader:
    """Return one genuine recording-off header without a writer."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    return header_for(tmp_path, str(tmp_path.resolve()), OFF)


# ---------------------------------------------------------------- RR1 round trip


def test_rr1_records_round_trip_through_canonical_bytes(tmp_path: Path) -> None:
    """RR1: each boundary snapshot round-trips through canonical bytes and its decoder."""
    abort = run_ev.validate_temperature_abort_record(abort_for(genuine_tick(tmp_path)))
    candidate = run_ev.validate_mcp_candidate_record(candidate_for(genuine_header(tmp_path)))
    for record, decode in (
        (abort, run_ev.decode_temperature_abort_document),
        (candidate, run_ev.decode_mcp_candidate_document),
    ):
        line = line_of(doc(record))
        decoded = decode(json.loads(line))
        assert decoded == record
        canonical = PRIVATE._canonical_json(doc(record))
        assert canonical == store.canonical_json(doc(record))
        assert line == canonical.encode("utf-8") + b"\n"
    assert run_ev.admit_mcp_candidate_document(provenance_doc()) == provenance()


def test_rr1_canonical_rendering_matches_the_store_over_hostile_shapes() -> None:
    """RR1: the module's canonical text equals the store's for unicode, nesting and order."""
    for value in ({"b": 1, "a": [1.5, "é", None]}, {"z": {"y": True}}, ["\U0001f525", 0.0]):
        assert PRIVATE._canonical_json(value) == store.canonical_json(value)


def test_rr1_records_carry_exactly_their_closed_fields() -> None:
    """RR1: closed field inventories, constants and configuration."""
    assert tuple(Abort.model_fields) == (
        "schema_version",
        "stream",
        "run_id",
        "phase",
        "recorded_at_utc",
        "monotonic_seconds",
        "identity_sha256",
        "tick",
        "domain",
        "reason",
    )
    assert tuple(Candidate.model_fields) == (
        "schema_version",
        "stream",
        "run_id",
        "phase",
        "recorded_at_utc",
        "monotonic_seconds",
        "identity_sha256",
        "candidate",
    )
    assert tuple(Provenance.model_fields) == tuple(PROVENANCE_DOC)
    for model in (Abort, Candidate, Provenance):
        config = dict(model.model_config)
        assert config == {
            **config,
            "frozen": True,
            "extra": "forbid",
            "strict": True,
            "allow_inf_nan": False,
        }
    assert (
        run_ev.TEMPERATURE_ABORT_SCHEMA_VERSION,
        run_ev.TEMPERATURE_ABORT_STREAM,
        run_ev.TEMPERATURE_ABORT_FILE_NAME,
        run_ev.MCP_CANDIDATE_SCHEMA_VERSION,
        run_ev.MCP_CANDIDATE_STREAM,
        run_ev.MCP_CANDIDATE_FILE_NAME,
        run_ev.MCP_CANDIDATE_DISTRIBUTION,
        run_ev.MCP_CANDIDATE_MAX_BYTE_LENGTH,
    ) == (
        5,
        "temperature_abort",
        "temperature_abort.jsonl",
        6,
        "mcp_candidate",
        "mcp_candidate.jsonl",
        "coffee-roaster-mcp",
        2**31 - 1,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reported_version", "123456789.0.987654321"),
        ("reported_version", "0.0.0"),
        ("reported_version", "1.10.0"),
        ("artefact_byte_length", 1),
        ("artefact_byte_length", 2**31 - 1),
        ("artefact_sha256", "0123456789abcdef" * 4),
        ("reviewed_source_revision", "0123456789abcdef0123456789abcdef01234567"),
    ],
)
def test_rr2_boundary_values_are_admitted(field: str, value: object) -> None:
    """RR2 positive controls: each grammar admits its boundary values on both paths."""
    admitted = run_ev.admit_mcp_candidate_document(provenance_doc(**{field: value}))
    assert admitted is not None
    assert getattr(admitted, field) == value
    assert getattr(provenance(**{field: value}), field) == value


# ----------------------------------------------------- RR2 provenance guards

#: One isolated refusal per provenance guard: (field, document value, direct value).
PROVENANCE_REFUSALS: list[tuple[str, str, object, object]] = [
    ("distribution-underscore", "distribution", "coffee_roaster_mcp", "coffee_roaster_mcp"),
    ("distribution-subclass", "distribution", Str("coffee-roaster-mcp"), Str("coffee-roaster-mcp")),
    ("version-prerelease", "reported_version", "0.2.2rc1", "0.2.2rc1"),
    ("version-prefix", "reported_version", "v0.2.2", "v0.2.2"),
    ("version-space", "reported_version", " 0.2.2", " 0.2.2"),
    ("version-leading-zero", "reported_version", "00.2.2", "00.2.2"),
    ("version-newline", "reported_version", "0.2.2\n", "0.2.2\n"),
    ("version-subclass", "reported_version", Str("0.2.2"), Str("0.2.2")),
    ("kind-sdist", "artefact_kind", "sdist", "sdist"),
    ("kind-fabricated", "artefact_kind", fabricated(Kind), fabricated(Kind)),
    ("length-zero", "artefact_byte_length", 0, 0),
    ("length-over", "artefact_byte_length", 2**31, 2**31),
    ("length-bool", "artefact_byte_length", True, True),
    ("length-float", "artefact_byte_length", 1.0, 1.0),
    ("length-subclass", "artefact_byte_length", IntSubclass(5), IntSubclass(5)),
    ("sha-upper", "artefact_sha256", "A" * 64, "A" * 64),
    ("sha-short", "artefact_sha256", "a" * 63, "a" * 63),
    ("sha-subclass", "artefact_sha256", Str("a" * 64), Str("a" * 64)),
    ("revision-short", "reviewed_source_revision", "b" * 39, "b" * 39),
    ("assertion-token", "assertion", "operator_asserted", "operator_asserted"),
    ("assertion-fabricated", "assertion", fabricated(Assertion), fabricated(Assertion)),
    ("attested-true", "installed_bytes_attested", True, True),
    ("attested-zero", "installed_bytes_attested", 0, 0),
    ("attested-none", "installed_bytes_attested", None, None),
]


@pytest.mark.parametrize(
    ("name", "field", "raw", "direct"),
    PROVENANCE_REFUSALS,
    ids=[case[0] for case in PROVENANCE_REFUSALS],
)
def test_rr2_each_provenance_guard_refuses_on_both_paths(
    name: str, field: str, raw: object, direct: object
) -> None:
    """RR2: the shared admission returns ``None``; direct construction raises."""
    del name
    assert run_ev.admit_mcp_candidate_document(provenance_doc(**{field: raw})) is None
    with pytest.raises(pydantic.ValidationError):
        Provenance.model_validate(provenance_members(**{field: direct}), strict=True)


def test_rr2_document_shape_guards() -> None:
    """RR2 (document only): count, key type and key set are checked in that order."""
    extra = {**provenance_doc(), "extra": 1}
    assert run_ev.admit_mcp_candidate_document(extra) is None
    subclass_key = provenance_doc()
    subclass_key[Str("assertion")] = subclass_key.pop("assertion")
    assert run_ev.admit_mcp_candidate_document(subclass_key) is None
    swapped = provenance_doc()
    del swapped["assertion"]
    swapped["unexpected"] = "operator_asserted_reviewed_candidate"
    assert len(swapped) == len(PROVENANCE_DOC)
    assert run_ev.admit_mcp_candidate_document(swapped) is None
    missing = provenance_doc()
    del missing["assertion"]
    assert run_ev.admit_mcp_candidate_document(missing) is None


def test_rr2_a_hostile_key_is_refused_without_calling_its_hooks() -> None:
    """RR2: a document with an extra hostile key is refused and its hooks never run.

    This proves hostile-key hook refusal only.  That the count is checked before the
    key scan is static source evidence, not a measured mutation kill.
    """
    document: dict[object, object] = {key: value for key, value in provenance_doc().items()}
    document[HostileKey("hostile")] = 1
    HostileKey.calls = 0
    assert run_ev.admit_mcp_candidate_document(document) is None
    assert HostileKey.calls == 0


def test_rr2_validation_error_after_the_exact_guards_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR2 (guard isolation): a model refusal after every exact guard returns ``None``."""

    def admit_any(value: object) -> bool:
        return True

    monkeypatch.setattr(PRIVATE, "_is_distribution", admit_any)
    assert run_ev.admit_mcp_candidate_document(provenance_doc(distribution="other")) is None


# ------------------------------------------------------- RR3 re-admission


def _uninitialised() -> run_ev.ColdMcpCandidateProvenance:
    """An instance created without ``__init__``."""
    return Provenance.__new__(Provenance)


def _readmission_refusals() -> list[tuple[str, object]]:
    """Return one instance per re-admission guard."""
    good = provenance()
    extra_count = state_of(good)
    extra_count["unexpected"] = 1
    swapped = state_of(good)
    swapped["unexpected"] = swapped.pop("assertion")
    absent = state_of(good)
    absent.pop("assertion")
    return [
        ("not-a-model", dict(PROVENANCE_DOC)),
        ("subclass-free-none", None),
        ("uninitialised", _uninitialised()),
        ("construct-attested", CONSTRUCT(**provenance_members(installed_bytes_attested=True))),
        ("raw-token-kind", good.model_copy(update={"artefact_kind": "wheel"})),
        ("raw-token-assertion", good.model_copy(update={"assertion": "x"})),
        ("fabricated-kind", good.model_copy(update={"artefact_kind": fabricated(Kind)})),
        ("nonempty-extra", with_extra(good, {"extra": 1})),
        ("non-dict-extra", with_extra(good, [])),
        ("dict-subclass-state", with_state(good, DictSubclass(state_of(good)))),
        ("extra-count", with_state(good, extra_count)),
        ("missing-plus-extra", with_state(good, swapped)),
        ("absent-slot", with_state(good, absent)),
        ("subclass-key", rekeyed(good, "distribution", Str("distribution"))),
    ]


@pytest.mark.parametrize(
    ("name", "value"), _readmission_refusals(), ids=[c[0] for c in _readmission_refusals()]
)
def test_rr3_readmission_refuses_each_forged_instance(name: str, value: object) -> None:
    """RR3: every forged or incomplete instance is refused with ``None``."""
    del name
    assert run_ev.readmit_mcp_candidate_provenance(value) is None


def test_rr3_hostile_key_hooks_never_run() -> None:
    """RR3: a hostile ``str``-subclass key is refused before any hash or equality."""
    good = provenance()
    state = state_of(good)
    state[HostileKey("distribution_x")] = state.pop("distribution")
    forged = with_state(good, state)
    HostileKey.calls = 0
    assert run_ev.readmit_mcp_candidate_provenance(forged) is None
    assert HostileKey.calls == 0


def test_rr3_a_valid_instance_is_readmitted_as_a_fresh_equal_copy() -> None:
    """RR3 positive control: a fresh, equal object that is never the input."""
    good = provenance()
    fresh = run_ev.readmit_mcp_candidate_provenance(good)
    assert fresh == good
    assert fresh is not good
    built = CONSTRUCT(**provenance_members())
    assert run_ev.readmit_mcp_candidate_provenance(built) == good


# ------------------------------------------------- RR4 direct construction


def _abort_direct_refusals() -> list[tuple[str, dict[str, object]]]:
    """Return one invalid value per abort field for direct construction."""
    return [
        ("version-4", {"schema_version": 4}),
        ("version-7", {"schema_version": 7}),
        ("version-true", {"schema_version": True}),
        ("version-float", {"schema_version": 5.0}),
        ("stream", {"stream": "tick"}),
        ("stream-subclass", {"stream": Str("temperature_abort")}),
        ("run-id", {"run_id": "not a run id"}),
        ("phase-token", {"phase": "recording_off"}),
        ("recorded-int", {"recorded_at_utc": 1}),
        ("mono-int", {"monotonic_seconds": 2}),
        ("mono-nan", {"monotonic_seconds": float("nan")}),
        ("digest", {"identity_sha256": "z" * 64}),
        ("domain-host", {"domain": Domain.HOST}),
        ("domain-fabricated", {"domain": fabricated(Domain)}),
        ("domain-token", {"domain": "engine"}),
        ("reason-token", {"reason": "temperature_outside_screen"}),
        ("reason-wrong-enum", {"reason": schema.ColdEngineAbortReason.CLOCK_INVALID}),
        ("reason-fabricated", {"reason": fabricated(R)}),
        ("tick-negative", {"tick": -1}),
        ("tick-bool", {"tick": True}),
        ("tick-float", {"tick": 1.0}),
        ("extra-field", {"extra": 1}),
    ]


@pytest.mark.parametrize(
    ("name", "update"), _abort_direct_refusals(), ids=[c[0] for c in _abort_direct_refusals()]
)
def test_rr4_direct_abort_construction_refuses(
    tmp_path: Path, name: str, update: dict[str, object]
) -> None:
    """RR4: strict direct construction refuses each invalid abort value."""
    del name
    tick = genuine_tick(tmp_path)
    abort_for(tick)
    with pytest.raises(pydantic.ValidationError):
        Abort.model_validate(abort_values(tick, R.OUTSIDE_SCREEN, **update), strict=True)


@pytest.mark.parametrize(
    "update",
    [
        {"schema_version": 5},
        {"schema_version": 7},
        {"schema_version": True},
        {"schema_version": 6.0},
        {"stream": "temperature_abort"},
        {"candidate": dict(PROVENANCE_DOC)},
        {"candidate": None},
        {"tick": 0},
    ],
)
def test_rr4_direct_candidate_construction_refuses(
    tmp_path: Path, update: dict[str, object]
) -> None:
    """RR4: strict direct construction refuses each invalid candidate value."""
    header = genuine_header(tmp_path)
    candidate_for(header)
    with pytest.raises(pydantic.ValidationError):
        Candidate.model_validate(candidate_values(header, **update), strict=True)


def test_rr4_every_screen_reason_is_an_admitted_abort_reason(tmp_path: Path) -> None:
    """RR4 positive control: every reason member constructs a valid ENGINE abort."""
    tick = genuine_tick(tmp_path)
    for reason in R:
        assert abort_for(tick, reason).reason is reason
        assert abort_for(tick, reason).domain is Domain.ENGINE


def _forbid_public_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make either public record boundary fail loudly if direct construction reached it."""

    def forbidden(record: object) -> typing.NoReturn:
        raise AssertionError("a public record boundary was called")

    monkeypatch.setattr(run_ev, "validate_mcp_candidate_record", forbidden)
    monkeypatch.setattr(run_ev, "validate_temperature_abort_record", forbidden)


def test_rr4_direct_candidate_construction_readmits_the_nested_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RR4 (L2) positive control: the nested provenance is a fresh, equal re-admission."""
    _forbid_public_boundaries(monkeypatch)
    header = genuine_header(tmp_path)
    good = provenance()
    record = Candidate.model_validate(candidate_values(header, candidate=good), strict=True)
    assert type(record.candidate) is Provenance
    assert record.candidate == good
    assert record.candidate is not good
    assert record.candidate.installed_bytes_attested is False


def _forged_nested_provenances() -> list[tuple[str, object]]:
    """Exact-class provenances whose content is not admitted, one isolated fault each."""
    good = provenance()
    return [
        ("attested-true", good.model_copy(update={"installed_bytes_attested": True})),
        ("raw-kind-token", good.model_copy(update={"artefact_kind": "wheel"})),
        ("raw-assertion-token", good.model_copy(update={"assertion": PROVENANCE_DOC["assertion"]})),
        ("fabricated-kind", good.model_copy(update={"artefact_kind": fabricated(Kind)})),
        ("construct-zero-length", CONSTRUCT(**provenance_members(artefact_byte_length=0))),
        ("construct-upper-sha", CONSTRUCT(**provenance_members(artefact_sha256="A" * 64))),
    ]


@pytest.mark.parametrize(
    ("name", "forged_provenance"),
    _forged_nested_provenances(),
    ids=[case[0] for case in _forged_nested_provenances()],
)
def test_rr4_direct_candidate_construction_refuses_a_forged_nested_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, forged_provenance: object
) -> None:
    """RR4 (L2): direct construction itself re-admits the nested provenance.

    Only the nested provenance differs from a valid record, and neither public
    record boundary is called, so returning the instance unchanged (or a same-type
    copy) instead of re-admitting it is detected here, not by a later boundary.
    """
    del name
    _forbid_public_boundaries(monkeypatch)
    header = genuine_header(tmp_path)
    assert type(forged_provenance) is Provenance
    assert run_ev.readmit_mcp_candidate_provenance(forged_provenance) is None
    control = Candidate.model_validate(candidate_values(header), strict=True)
    assert control.candidate == provenance()
    with pytest.raises(pydantic.ValidationError):
        Candidate.model_validate(candidate_values(header, candidate=forged_provenance), strict=True)


# ---------------------------------------------------------- RR5 boundaries


def _boundary_refusals(tmp_path: Path) -> list[tuple[str, object, schema.ColdEvidenceFailure]]:
    """Return each in-process forgery with the closed member its boundary raises."""
    abort = abort_for(genuine_tick(tmp_path))
    candidate = candidate_for(genuine_header(tmp_path / "header"))
    attested = provenance().model_copy(update={"installed_bytes_attested": True})
    not_validated = EFail.RECORD_NOT_VALIDATED
    return [
        ("abort-host", abort.model_copy(update={"domain": Domain.HOST}), not_validated),
        ("abort-phase-token", abort.model_copy(update={"phase": "recording_off"}), not_validated),
        ("abort-reason-token", abort.model_copy(update={"reason": "x"}), not_validated),
        ("abort-run-id-none", abort.model_copy(update={"run_id": None}), not_validated),
        ("abort-subclass-key", rekeyed(abort, "tick", Str("tick")), not_validated),
        ("abort-subclass", SubAbort.model_validate(doc_values(abort)), not_validated),
        ("abort-uninitialised", Abort.__new__(Abort), not_validated),
        ("abort-extra", with_extra(abort, {"x": 1}), not_validated),
        (
            "abort-big-tick",
            abort.model_copy(update={"tick": 10**32}),
            EFail.JSON_VALUE_TYPE_NOT_ADMITTED,
        ),
        (
            "abort-text",
            abort.model_copy(update={"recorded_at_utc": "é" * 1025}),
            EFail.TEXT_FIELD_TOO_LARGE,
        ),
        ("candidate-attested", candidate.model_copy(update={"candidate": attested}), not_validated),
        ("candidate-dict", candidate.model_copy(update={"candidate": {}}), not_validated),
        ("candidate-subclass", SubCandidate.model_validate(doc_values(candidate)), not_validated),
        ("candidate-subclass-key", rekeyed(candidate, "phase", Str("phase")), not_validated),
        (
            "candidate-text",
            candidate.model_copy(update={"recorded_at_utc": "é" * 1025}),
            EFail.TEXT_FIELD_TOO_LARGE,
        ),
        ("not-a-record", object(), not_validated),
    ]


def doc_values(record: pydantic.BaseModel) -> dict[str, object]:
    """Return a record's member-form field values."""
    return {name: getattr(record, name) for name in type(record).model_fields}


def test_rr5_boundaries_refuse_each_forgery_with_its_closed_member(tmp_path: Path) -> None:
    """RR5: every bypass the fresh re-admission closes raises its exact closed member."""
    for name, value, failure in _boundary_refusals(tmp_path):
        validate = (
            run_ev.validate_mcp_candidate_record
            if name.startswith("candidate") or name == "not-a-record"
            else run_ev.validate_temperature_abort_record
        )
        with pytest.raises(schema.ColdEvidenceError) as raised:
            validate(value)
        assert raised.value.failure is failure, name
        assert raised.value.args == ("Cold evidence admission failed.",)
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
    expect_evidence(
        EFail.RECORD_NOT_VALIDATED, lambda: run_ev.validate_temperature_abort_record(object())
    )


def test_rr5_text_boundary_positive_and_snapshot_freshness(tmp_path: Path) -> None:
    """RR5 positive controls: 1,024 two-byte characters fit; snapshots are fresh."""
    abort = abort_for(genuine_tick(tmp_path)).model_copy(update={"recorded_at_utc": "é" * 1024})
    snapshot = run_ev.validate_temperature_abort_record(abort)
    assert snapshot == abort and snapshot is not abort
    candidate = candidate_for(genuine_header(tmp_path / "header"))
    fresh = run_ev.validate_mcp_candidate_record(candidate)
    assert fresh == candidate and fresh is not candidate
    assert fresh.candidate is not candidate.candidate


def test_rr5_record_byte_cap_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RR5 (guard isolation): the canonical record byte cap raises ``RECORD_TOO_LARGE``.

    Every field is otherwise bounded far below the shared cap, so the cap is
    lowered here to isolate the guard; this is not a reachable production input.
    """
    abort = abort_for(genuine_tick(tmp_path))
    monkeypatch.setattr(PRIVATE, "MAX_RECORD_BYTES", 10)
    expect_evidence(EFail.RECORD_TOO_LARGE, lambda: run_ev.validate_temperature_abort_record(abort))


# --------------------------------------------------------- RR6 containment


def _assert_contained(error: BaseException) -> None:
    """Assert a closed error discloses no canary text anywhere."""
    rendered = "".join(traceback.format_exception(error))
    for text in (str(error), repr(error), repr(error.args), rendered):
        assert SECRET not in text
    assert error.__cause__ is None
    assert error.__context__ is None


def test_rr6_boundaries_never_disclose_input_content(tmp_path: Path) -> None:
    """RR6: canaries in an extra key, a token and a typed field never surface."""
    abort = abort_for(genuine_tick(tmp_path))
    candidate = candidate_for(genuine_header(tmp_path / "header"))
    keyed = state_of(abort)
    keyed[SECRET] = keyed.pop("tick")
    for value, validate in (
        (abort.model_copy(update={"reason": SECRET}), run_ev.validate_temperature_abort_record),
        (with_state(abort, keyed), run_ev.validate_temperature_abort_record),
        (
            abort.model_copy(update={"domain": Domain.HOST, "run_id": SECRET}),
            run_ev.validate_temperature_abort_record,
        ),
        (
            candidate.model_copy(
                update={
                    "candidate": provenance().model_copy(update={"artefact_byte_length": SECRET})
                }
            ),
            run_ev.validate_mcp_candidate_record,
        ),
    ):
        with pytest.raises(schema.ColdEvidenceError) as raised:
            validate(value)
        _assert_contained(raised.value)
    assert run_ev.admit_mcp_candidate_document(provenance_doc(artefact_byte_length=SECRET)) is None
    assert run_ev.admit_mcp_candidate_document({**provenance_doc(), SECRET: 1}) is None
    document = doc(abort)
    document["reason"] = SECRET
    assert run_ev.decode_temperature_abort_document(document) is None


def test_rr6_the_v6_reader_never_discloses_a_crafted_line(tmp_path: Path) -> None:
    """RR6: a crafted candidate line carrying a canary is refused without content."""
    run, _ = sealed_v6(tmp_path)
    document = doc(candidate_for(run.off))
    document["candidate"]["artefact_byte_length"] = SECRET
    digest = craft(run.root, {CANDIDATE_OFF: line_of(document)})
    with pytest.raises(store.ColdEvidenceStoreError) as raised:
        read6(run.root, digest)
    assert raised.value.failure is Failure.LINE_MALFORMED
    _assert_contained(raised.value)


# ------------------------------------------------------------ RR7 decoders


def _json_kinds() -> list[object]:
    """Every invalid JSON-derived value kind (the valid object is RR1)."""
    values: list[object] = [None, True, 0, -1, 10**40, 1.5, "", "x", [], [1], {}, {"k": []}]
    return [json.loads(json.dumps(value)) for value in values]


@pytest.mark.parametrize("value", _json_kinds())
def test_rr7_decoders_are_total_over_json_kinds(tmp_path: Path, value: object) -> None:
    """RR7: each invalid JSON kind decodes to ``None`` as a document and as a candidate."""
    assert run_ev.decode_temperature_abort_document(value) is None
    assert run_ev.decode_mcp_candidate_document(value) is None
    assert run_ev.admit_mcp_candidate_document(value) is None
    document = doc(candidate_for(genuine_header(tmp_path)))
    document["candidate"] = value
    assert run_ev.decode_mcp_candidate_document(document) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phase", "recording_middle"),
        ("phase", 1),
        ("domain", "host"),
        ("domain", "unknown"),
        ("reason", "temperature_unknown"),
        ("tick", -1),
        ("schema_version", 5.0),
        ("recorded_at_utc", "é" * 2049),
    ],
)
def test_rr7_abort_document_field_refusals(tmp_path: Path, field: str, value: object) -> None:
    """RR7: each invalid abort document field decodes to ``None``."""
    document = doc(abort_for(genuine_tick(tmp_path)))
    assert run_ev.decode_temperature_abort_document(dict(document)) is not None
    document[field] = value
    assert run_ev.decode_temperature_abort_document(document) is None


def test_rr7_document_shape_refusals(tmp_path: Path) -> None:
    """RR7: count, key type and key-set refusals for both decoders."""
    for document, decode in (
        (doc(abort_for(genuine_tick(tmp_path))), run_ev.decode_temperature_abort_document),
        (doc(candidate_for(genuine_header(tmp_path))), run_ev.decode_mcp_candidate_document),
    ):
        assert decode({**document, "extra": 1}) is None
        keyed: dict[object, object] = {key: value for key, value in document.items()}
        keyed[Str("stream")] = keyed.pop("stream")
        assert decode(keyed) is None
        swapped = dict(document)
        swapped["other"] = swapped.pop("stream")
        assert decode(swapped) is None
    candidate = doc(candidate_for(genuine_header(tmp_path)))
    candidate["phase"] = "recording_middle"
    assert run_ev.decode_mcp_candidate_document(candidate) is None
    candidate = doc(candidate_for(genuine_header(tmp_path)))
    candidate["schema_version"] = 5
    assert run_ev.decode_mcp_candidate_document(candidate) is None


# ---------------------------------------------------------- RR8 vocabulary


def test_rr8_vocabularies_are_closed_plain_enums() -> None:
    """RR8: names and tokens are pinned; no ``StrEnum`` or ``str``/``int`` mixin."""
    assert [(m.name, m.value) for m in Kind] == [("WHEEL", "wheel")]
    assert [(m.name, m.value) for m in Assertion] == [
        ("OPERATOR_ASSERTED_REVIEWED_CANDIDATE", "operator_asserted_reviewed_candidate")
    ]
    assert [(m.name, m.value) for m in RFail] == [
        ("PHASE_NOT_LATEST", "phase_not_latest"),
        ("TICK_NOT_PAIRED", "tick_not_paired"),
        ("ABORT_DUPLICATED", "abort_duplicated"),
        ("CANDIDATE_DUPLICATED", "candidate_duplicated"),
        ("CANDIDATE_AFTER_TICK", "candidate_after_tick"),
        ("CANDIDATE_NOT_UNIQUE", "candidate_not_unique"),
    ]
    assert len(R) == 11
    for kind in (R, Kind, Assertion, RFail):
        assert kind.__bases__ == (enum.Enum,)
        assert not issubclass(kind, (str, int))
    assert [m.name for m in Domain] == [
        "HOST",
        "IDENTITY",
        "EVIDENCE",
        "MCP",
        "ADVISOR",
        "OPERATOR",
        "ENGINE",
    ]
    assert issubclass(run_ev.ColdTemperatureRunError, Exception)
    assert run_ev.ColdTemperatureRunError(RFail.TICK_NOT_PAIRED).args == (
        "Cold temperature run evidence refused.",
    )


def test_rr8_module_structure() -> None:
    """RR8: one project import, no I/O, no broad ``except``, no fenced words."""
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    plain: set[str] = set()
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            named.add(node.module)
    assert plain == {"enum", "json", "math", "re", "typing", "pydantic"}
    assert named == {"roastpilot_agent.cold_characterisation.evidence_schema"}
    for text in ("isinstance(", "except Exception", "subprocess", "StrEnum", "print("):
        assert text not in source, text


# ------------------------------------------------------------------ RW writer


def test_rw1_happy_path_writes_and_reads_every_record(tmp_path: Path) -> None:
    """RW1: header, candidate, ticks and temperatures, two distinct aborts, then seal."""
    run, digest = sealed_v6(tmp_path)
    retained = read6(run.root, digest)
    assert retained.mcp_candidates == (candidate_for(run.off), candidate_for(run.on))
    assert retained.temperature_aborts == (
        abort_for(run.on_ticks[-1], R.OUTSIDE_SCREEN),
        abort_for(run.on_ticks[-1], R.VALUES_DISAGREE),
    )
    assert len(retained.tick_temperatures) == 4


def _usable(run: Run) -> None:
    """Prove the writer is still usable: a following valid append succeeds."""
    run.writer.append(tick_for(run.on, len(run.on_ticks)))


def test_rw2_duplicate_candidate_is_refused_and_the_writer_stays_usable(tmp_path: Path) -> None:
    """RW2: a second candidate in one phase."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    writer.append_mcp_candidate(candidate_for(off))
    expect_run(RFail.CANDIDATE_DUPLICATED, lambda: writer.append_mcp_candidate(candidate_for(off)))
    writer.append(tick_for(off, 0))


def test_rw2_candidate_after_a_tick_is_refused(tmp_path: Path) -> None:
    """RW2: a candidate after its phase retains a tick."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    tick = tick_for(off, 0)
    writer.append(tick)
    expect_run(RFail.CANDIDATE_AFTER_TICK, lambda: writer.append_mcp_candidate(candidate_for(off)))
    writer.append_tick_temperature(temperature_for(tick))


def test_rw2_a_candidate_for_an_earlier_phase_is_refused(tmp_path: Path) -> None:
    """RW2 (L3): OFF has no candidate and no tick, so only the latest-phase guard refuses."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    on = on_header(tmp_path, root)
    writer.append(off)
    writer.append(on)
    expect_run(RFail.PHASE_NOT_LATEST, lambda: writer.append_mcp_candidate(candidate_for(off)))
    writer.append_mcp_candidate(candidate_for(on))
    writer.append(tick_for(on, 0))


def test_rw2_an_abort_for_an_earlier_phase_is_refused(tmp_path: Path) -> None:
    """RW2: the OFF latest tick is paired, so only the latest-phase guard refuses."""
    run = v6_run(tmp_path, candidates=False)
    expect_run(
        RFail.PHASE_NOT_LATEST,
        lambda: run.writer.append_temperature_abort(abort_for(run.off_ticks[-1])),
    )
    run.writer.append_temperature_abort(abort_for(run.on_ticks[-1]))
    _usable(run)


def test_rw2_an_abort_without_any_tick_is_refused(tmp_path: Path) -> None:
    """RW2: no tick retained in the phase."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    expect_run(
        RFail.TICK_NOT_PAIRED, lambda: writer.append_temperature_abort(abort_for(tick_for(off, 0)))
    )
    writer.append(tick_for(off, 0))


def test_rw2_an_abort_on_an_unpaired_latest_tick_is_refused(tmp_path: Path) -> None:
    """RW2: the latest tick has no temperature line yet."""
    run = v6_run(tmp_path)
    tick = tick_for(run.on, 2)
    run.writer.append(tick)
    expect_run(RFail.TICK_NOT_PAIRED, lambda: run.writer.append_temperature_abort(abort_for(tick)))
    run.writer.append_tick_temperature(temperature_for(tick))
    run.writer.append_temperature_abort(abort_for(tick))


def test_rw2_an_abort_on_an_earlier_paired_tick_is_refused(tmp_path: Path) -> None:
    """RW2: the abort names a paired tick that is not the latest one."""
    run = v6_run(tmp_path)
    expect_run(
        RFail.TICK_NOT_PAIRED,
        lambda: run.writer.append_temperature_abort(abort_for(run.on_ticks[0])),
    )
    run.writer.append_temperature_abort(abort_for(run.on_ticks[-1]))


def test_rw2_a_repeated_abort_reason_is_refused(tmp_path: Path) -> None:
    """RW2: the same ``(phase, tick, reason)`` twice."""
    run = v6_run(tmp_path)
    latest = run.on_ticks[-1]
    run.writer.append_temperature_abort(abort_for(latest, R.FAULT_COUNTED))
    expect_run(
        RFail.ABORT_DUPLICATED,
        lambda: run.writer.append_temperature_abort(abort_for(latest, R.FAULT_COUNTED)),
    )
    run.writer.append_temperature_abort(abort_for(latest, R.PRIOR_MISSING))


@pytest.mark.parametrize(
    ("update", "failure"),
    [
        ({"run_id": OTHER_RUN_ID}, Failure.RUN_ID_MISMATCHED),
        ({"identity_sha256": FOREIGN_DIGEST}, Failure.IDENTITY_DIGEST_MISMATCHED),
    ],
)
def test_rw2_candidate_binding_refusals_are_isolated(
    tmp_path: Path, update: dict[str, object], failure: store.ColdEvidenceStoreFailure
) -> None:
    """RW2 (L3): header only, no candidate or tick, so binding is the only refusing guard.

    Only the run id or the identity digest differs from a valid candidate, and a
    following valid candidate append succeeds.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    writer.append(off)
    forged = candidate_for(off, **update)
    assert run_ev.validate_mcp_candidate_record(forged) == forged
    expect_store(failure, lambda: writer.append_mcp_candidate(forged))
    writer.append_mcp_candidate(candidate_for(off))


@pytest.mark.parametrize(
    ("update", "failure"),
    [
        ({"run_id": OTHER_RUN_ID}, Failure.RUN_ID_MISMATCHED),
        ({"identity_sha256": FOREIGN_DIGEST}, Failure.IDENTITY_DIGEST_MISMATCHED),
    ],
)
def test_rw2_abort_binding_refusals_are_isolated(
    tmp_path: Path, update: dict[str, object], failure: store.ColdEvidenceStoreFailure
) -> None:
    """RW2 (L3): the latest ON tick is paired, so binding is the only refusing guard."""
    run = v6_run(tmp_path, candidates=False, ticks=1)
    forged = abort_for(run.on_ticks[-1], R.OUTSIDE_SCREEN, **update)
    assert run_ev.validate_temperature_abort_record(forged) == forged
    expect_store(failure, lambda: run.writer.append_temperature_abort(forged))
    run.writer.append_temperature_abort(abort_for(run.on_ticks[-1]))


def test_rw2_records_before_their_header_are_refused(tmp_path: Path) -> None:
    """RW2: refusal-order evidence: ``HEADER_MISSING`` precedes the latest-phase check.

    The ON records are both header-less and not in the latest phase, so this shows
    which refusal wins; it is not a single-guard acceptance probe.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    off = header_for(tmp_path, root, OFF)
    on = on_header(tmp_path, root)
    writer.append(off)
    expect_store(Failure.HEADER_MISSING, lambda: writer.append_mcp_candidate(candidate_for(on)))
    expect_store(
        Failure.HEADER_MISSING,
        lambda: writer.append_temperature_abort(abort_for(tick_for(on, 0))),
    )
    writer.append_mcp_candidate(candidate_for(off))


def test_rw2_content_refusals_propagate_without_poisoning(tmp_path: Path) -> None:
    """RW2: a forged record is refused by re-admission and the writer stays usable."""
    run = v6_run(tmp_path, candidates=False)
    forged = abort_for(run.on_ticks[-1]).model_copy(update={"domain": Domain.HOST})
    expect_evidence(EFail.RECORD_NOT_VALIDATED, lambda: run.writer.append_temperature_abort(forged))
    _usable(run)


class Prepared(typing.NamedTuple):
    """One writer with exactly one valid pending append for the method under test."""

    writer: store.ColdEvidenceWriter
    header: schema.ColdRunHeader
    append: typing.Callable[[], None]


def prepared(tmp_path: Path, method: str) -> Prepared:
    """Prepare a writer whose next candidate (header only) or abort (paired tick) is valid."""
    if method == "candidate":
        tmp_path.mkdir(parents=True, exist_ok=True)
        writer, root = open_writer(tmp_path)
        off = header_for(tmp_path, root, OFF)
        writer.append(off)
        return Prepared(writer, off, lambda: writer.append_mcp_candidate(candidate_for(off)))
    run = v6_run(tmp_path)
    latest = run.on_ticks[-1]
    return Prepared(
        run.writer, run.on, lambda: run.writer.append_temperature_abort(abort_for(latest))
    )


def _private(writer: store.ColdEvidenceWriter) -> typing.Any:
    """Return the writer for private state reads (unit exception)."""
    return writer


def _state(writer: store.ColdEvidenceWriter) -> tuple[set[object], set[object]]:
    """Return copies of the writer's candidate and abort state."""
    return set(_private(writer)._candidate_phases), set(_private(writer)._abort_keys)


@pytest.mark.parametrize("method", ["candidate", "abort"])
def test_rw3_the_pending_append_is_valid(tmp_path: Path, method: str) -> None:
    """RW3 positive control: the prepared append succeeds and advances only its state."""
    case = prepared(tmp_path, method)
    candidates, aborts = _state(case.writer)
    case.append()
    after = _state(case.writer)
    assert (len(after[0]) - len(candidates), len(after[1]) - len(aborts)) == (
        (1, 0) if method == "candidate" else (0, 1)
    )


@pytest.mark.parametrize("method", ["candidate", "abort"])
def test_rw3_a_write_fault_poisons_and_leaves_state_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """RW3: an ``OSError`` raises ``WRITE_FAILED``, poisons, and records nothing."""
    case = prepared(tmp_path, method)
    before = _state(case.writer)

    def fail(descriptor: int, data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_all", fail)
    expect_store(Failure.WRITE_FAILED, case.append)
    assert _state(case.writer) == before
    expect_store(Failure.WRITER_POISONED, lambda: case.writer.append(tick_for(case.header, 9)))


@pytest.mark.parametrize("method", ["candidate", "abort"])
def test_rw3_an_interruption_during_the_write_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """RW3: a ``BaseException`` during the write propagates and poisons the writer."""
    case = prepared(tmp_path, method)
    before = _state(case.writer)

    def interrupt(descriptor: int, data: bytes) -> None:
        raise _Interrupt

    monkeypatch.setattr(store, "_write_all", interrupt)
    with pytest.raises(_Interrupt):
        case.append()
    assert _state(case.writer) == before
    expect_store(Failure.WRITER_POISONED, lambda: case.writer.append(tick_for(case.header, 9)))


@pytest.mark.parametrize("method", ["candidate", "abort"])
def test_rw3_an_unexpected_ordering_error_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """RW3: an unexpected exception during binding propagates and poisons the writer."""
    case = prepared(tmp_path, method)

    def explode(state: object, record: object) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(store, "check_temperature_run_binding", explode)
    with pytest.raises(RuntimeError):
        case.append()
    expect_store(Failure.WRITER_POISONED, lambda: case.writer.append(tick_for(case.header, 9)))


def test_rw3_terminal_and_seal_refusals_come_first(tmp_path: Path) -> None:
    """RW3: after a failed-run terminal, then after a seal, both methods are refused."""
    run = v6_run(tmp_path, candidates=False)
    run.writer.append_failed_run_terminal(
        terminal.build_failed_run_terminal_record(
            run.on,
            advisory_settlement=terminal.ColdFailedRunAdvisorySettlement.RECORDED_UNRESOLVED_NOT_INVOKED,
            provider_cancellation=terminal.ColdFailedRunProviderCancellation.NO_PROVIDER_TASK,
            lifecycle_records_retained=0,
            advisory_attempt_records_retained=0,
        )
    )
    for call in (
        lambda: run.writer.append_mcp_candidate(candidate_for(run.on)),
        lambda: run.writer.append_temperature_abort(abort_for(run.on_ticks[-1])),
    ):
        with pytest.raises(terminal.ColdFailedRunTerminalError) as raised:
            call()
        assert raised.value.failure is terminal.ColdFailedRunTerminalFailure.APPENDED_AFTER_TERMINAL
    run.writer.seal()
    expect_store(
        Failure.WRITER_SEALED, lambda: run.writer.append_mcp_candidate(candidate_for(run.on))
    )
    expect_store(
        Failure.WRITER_SEALED,
        lambda: run.writer.append_temperature_abort(abort_for(run.on_ticks[-1])),
    )


def header_only_on(tmp_path: Path) -> tuple[store.ColdEvidenceWriter, schema.ColdRunHeader]:
    """Open a writer whose latest phase is ON with only its header: no candidate, no tick."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    writer, root = open_writer(tmp_path)
    writer.append(header_for(tmp_path, root, OFF))
    on = on_header(tmp_path, root)
    writer.append(on)
    return writer, on


def test_rw3_a_header_only_candidate_is_appendable(tmp_path: Path) -> None:
    """RW3 (L4) positive control: the same candidate appends on an equivalent writer."""
    writer, on = header_only_on(tmp_path)
    writer.append_mcp_candidate(candidate_for(on))
    assert _private(writer)._candidate_phases == {ON}


@pytest.mark.parametrize("guard", ["terminal", "sealed"])
def test_rw3_a_header_only_candidate_is_refused_after_terminal_or_seal(
    tmp_path: Path, guard: str
) -> None:
    """RW3 (L4): with no candidate or tick in ON, only the appendability guard can refuse.

    Each guard runs on its own fresh writer.  The terminal and the seal are first shown
    to succeed on the header-only phase; nothing here claims a sealed writer could
    durably append.
    """
    writer, on = header_only_on(tmp_path)
    candidate = candidate_for(on)
    if guard == "terminal":
        writer.append_failed_run_terminal(
            terminal.build_failed_run_terminal_record(
                on,
                advisory_settlement=terminal.ColdFailedRunAdvisorySettlement.RECORDED_UNRESOLVED_NOT_INVOKED,
                provider_cancellation=terminal.ColdFailedRunProviderCancellation.NO_PROVIDER_TASK,
                lifecycle_records_retained=0,
                advisory_attempt_records_retained=0,
            )
        )
        assert _private(writer)._terminal_appended is True
        with pytest.raises(terminal.ColdFailedRunTerminalError) as raised:
            writer.append_mcp_candidate(candidate)
        assert raised.value.failure is terminal.ColdFailedRunTerminalFailure.APPENDED_AFTER_TERMINAL
    else:
        sealed = writer.seal()
        assert sealed.run_id == RUN_ID
        assert len(sealed.identity_bindings) == 2
        expect_store(Failure.WRITER_SEALED, lambda: writer.append_mcp_candidate(candidate))
    assert _private(writer)._candidate_phases == set()


# ------------------------------------------------------------------ RV reader


def test_rv1_a_resealed_copy_of_genuine_lines_reads_equal(tmp_path: Path) -> None:
    """RV1: re-writing genuine lines from the same run and re-sealing reads the same."""
    run, digest = sealed_v6(tmp_path)
    first = read6(run.root, digest)
    files: dict[str, bytes | None] = {
        path: (run_dir(run.root) / path).read_bytes()
        for path in (CANDIDATE_OFF, CANDIDATE_ON, ABORT_ON)
    }
    resealed = craft(run.root, files)
    second = read6(run.root, resealed)
    assert second.temperature_aborts == first.temperature_aborts
    assert second.mcp_candidates == first.mcp_candidates
    assert second.tick_temperatures == first.tick_temperatures


EARLIER_READERS: typing.Final[dict[str, typing.Callable[..., object]]] = {
    "v1": reader.read_retained_run,
    "v2": reader.read_retained_run_v2,
    "v3": reader.read_retained_run_v3,
    "v4": reader.read_retained_run_v4,
    "v5": reader.read_retained_run_v5,
}


@pytest.mark.parametrize("version", list(EARLIER_READERS))
@pytest.mark.parametrize("path", [CANDIDATE_OFF, ABORT_OFF])
def test_rv2_earlier_readers_refuse_either_new_file(
    tmp_path: Path, version: str, path: str
) -> None:
    """RV2: V1-V5 refuse a v1-only tree to which only one new file is added."""
    run = v6_run(tmp_path, candidates=False, pair=False)
    digest = run.writer.seal().manifest_sha256
    read = EARLIER_READERS[version]
    read(run.root, run_id=RUN_ID, expected_manifest_sha256=digest)
    line = line_of(
        doc(candidate_for(run.off) if path == CANDIDATE_OFF else abort_for(run.off_ticks[-1]))
    )
    crafted = craft(run.root, {path: line})
    expect_store(
        Failure.ENTRY_PATH_INVALID,
        lambda: read(run.root, run_id=RUN_ID, expected_manifest_sha256=crafted),
    )


def _rewrite(run: Run, path: str, document: dict[str, typing.Any] | bytes) -> str:
    """Replace one retained file with raw bytes, or one document's line, and re-seal."""
    data = document if isinstance(document, bytes) else line_of(document)
    return craft(run.root, {path: data})


def _documents(run: Run) -> dict[str, dict[str, typing.Any]]:
    """Return the genuine candidate and abort documents of the sealed run."""
    return {
        CANDIDATE_OFF: doc(candidate_for(run.off)),
        ABORT_ON: doc(abort_for(run.on_ticks[-1], R.OUTSIDE_SCREEN)),
    }


def _abort_lines(run: Run) -> bytes:
    """Return the genuine two-line abort file."""
    return line_of(doc(abort_for(run.on_ticks[-1], R.OUTSIDE_SCREEN))) + line_of(
        doc(abort_for(run.on_ticks[-1], R.VALUES_DISAGREE))
    )


@pytest.mark.parametrize("path", [CANDIDATE_OFF, ABORT_ON])
@pytest.mark.parametrize(
    ("name", "change", "failure"),
    [
        ("version-missing", {"schema_version": None}, Failure.SCHEMA_VERSION_UNKNOWN),
        ("version-4", {"schema_version": 4}, Failure.SCHEMA_VERSION_UNKNOWN),
        ("version-true", {"schema_version": True}, Failure.SCHEMA_VERSION_UNKNOWN),
        ("version-float", {"schema_version": "float"}, Failure.SCHEMA_VERSION_UNKNOWN),
        (
            "version-wins",
            {"schema_version": 7, "stream": "tick"},
            Failure.SCHEMA_VERSION_UNKNOWN,
        ),
        ("stream", {"stream": "tick"}, Failure.LINE_MALFORMED),
        ("decode", {"run_id": "not a run id"}, Failure.LINE_MALFORMED),
        ("digest", {"identity_sha256": FOREIGN_DIGEST}, Failure.IDENTITY_DIGEST_MISMATCHED),
    ],
)
def test_rv3_v6_line_refusals(
    tmp_path: Path,
    path: str,
    name: str,
    change: dict[str, object],
    failure: store.ColdEvidenceStoreFailure,
) -> None:
    """RV3: each per-line guard, from the same run, with only the intended fault."""
    del name
    run, digest = sealed_v6(tmp_path)
    read6(run.root, digest)
    document = _documents(run)[path]
    own = 6 if path == CANDIDATE_OFF else 5
    for key, value in change.items():
        if value is None:
            del document[key]
        elif value == "float":
            document[key] = float(own)
        else:
            document[key] = value
    crafted = _rewrite(run, path, document)
    expect_store(failure, lambda: read6(run.root, crafted))


def _genuine_other_phase(run: Run, kind: str) -> tuple[str, str, bytes]:
    """Return (wrong directory, proper directory, line) for a genuine other-phase record.

    The candidate is the run's genuine ON candidate; the abort is a genuine OFF abort
    naming OFF's latest paired tick.  Both carry their own phase's identity digest.
    """
    if kind == "candidate":
        return CANDIDATE_OFF, CANDIDATE_ON, line_of(doc(candidate_for(run.on)))
    return ABORT_ON, ABORT_OFF, line_of(doc(abort_for(run.off_ticks[-1], R.OUTSIDE_SCREEN)))


@pytest.mark.parametrize("kind", ["candidate", "abort"])
def test_rv3_a_genuine_record_in_its_proper_directory_is_readable(
    tmp_path: Path, kind: str
) -> None:
    """RV3 (L3) positive control: the same line in its own phase's directory reads."""
    run, _ = sealed_v6(tmp_path)
    _, proper, line = _genuine_other_phase(run, kind)
    crafted = craft(run.root, {proper: line})
    retained = read6(run.root, crafted)
    records = retained.mcp_candidates if kind == "candidate" else retained.temperature_aborts
    assert line_of(doc(records[-1] if kind == "candidate" else records[0])) == line


@pytest.mark.parametrize("kind", ["candidate", "abort"])
def test_rv3_a_genuine_record_in_the_wrong_directory_is_refused(tmp_path: Path, kind: str) -> None:
    """RV3 (L3): only the directory phase is wrong; binding, pairing and uniqueness hold.

    The wrong directory's file holds that single self-consistent line, so removing the
    directory-phase guard would let the read succeed rather than fail elsewhere.
    """
    run, _ = sealed_v6(tmp_path)
    wrong, _, line = _genuine_other_phase(run, kind)
    crafted = _rewrite(run, wrong, line)
    expect_store(Failure.LINE_MALFORMED, lambda: read6(run.root, crafted))


@pytest.mark.parametrize("path", [CANDIDATE_OFF, ABORT_ON])
def test_rv3_non_canonical_lines_are_refused(tmp_path: Path, path: str) -> None:
    """RV3: reordered keys are not the canonical line."""
    run, _ = sealed_v6(tmp_path)
    document = _documents(run)[path]
    reordered = json.dumps(dict(reversed(document.items())), separators=(",", ":")).encode()
    crafted = _rewrite(run, path, reordered + b"\n")
    expect_store(Failure.LINE_NOT_CANONICAL, lambda: read6(run.root, crafted))


def test_rv3_an_empty_candidate_file_is_refused_by_framing(tmp_path: Path) -> None:
    """RV3: a real zero-byte file is ``LINE_MALFORMED`` before any uniqueness check."""
    run, _ = sealed_v6(tmp_path)
    crafted = _rewrite(run, CANDIDATE_OFF, b"")
    expect_store(Failure.LINE_MALFORMED, lambda: read6(run.root, crafted))


@pytest.mark.parametrize("path", [CANDIDATE_OFF, ABORT_ON])
@pytest.mark.parametrize("line", [b"[]\n", b'"text"\n', b"not json\n"])
def test_rv3_a_line_that_is_not_a_json_object_is_refused(
    tmp_path: Path, path: str, line: bytes
) -> None:
    """RV3: a strict-JSON non-object or malformed line, before any version check."""
    run, _ = sealed_v6(tmp_path)
    crafted = _rewrite(run, path, line)
    expect_store(Failure.LINE_MALFORMED, lambda: read6(run.root, crafted))


@pytest.mark.parametrize(("version", "stream"), [(5, "temperature_abort"), (6, "mcp_candidate")])
def test_rv3_an_empty_framed_line_is_refused(version: int, stream: str) -> None:
    """RV3 (defensive guard isolation): an empty framed line, unit-called directly.

    The shared framer never yields an empty line for a real file, so the line
    reader's own empty-line guard is reached through the private helper.
    """
    reader_any: typing.Any = reader
    expect_store(
        Failure.LINE_MALFORMED,
        lambda: reader_any._temperature_run_document(
            b"", versions=frozenset({version}), stream=stream
        ),
    )


def test_rv3_two_candidate_lines_are_not_unique(tmp_path: Path) -> None:
    """RV3: a candidate file with two genuine lines."""
    run, _ = sealed_v6(tmp_path)
    line = line_of(doc(candidate_for(run.off)))
    crafted = _rewrite(run, CANDIDATE_OFF, line + line)
    expect_run(RFail.CANDIDATE_NOT_UNIQUE, lambda: read6(run.root, crafted))


def test_rv3_zero_framed_candidate_lines_are_not_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RV3 (defensive guard isolation): a framed read that yields no candidate line."""
    run, digest = sealed_v6(tmp_path)
    real = store.read_verified_lines

    def framed(
        tree: store.ColdVerifiedTree, path: str, *, max_line_bytes: int
    ) -> tuple[bytes, ...]:
        if path == CANDIDATE_OFF:
            return ()
        return tuple(real(tree, path, max_line_bytes=max_line_bytes))

    monkeypatch.setattr(reader, "read_verified_lines", framed)
    expect_run(RFail.CANDIDATE_NOT_UNIQUE, lambda: read6(run.root, digest))


def test_rv3_an_abort_naming_a_missing_tick_is_not_paired(tmp_path: Path) -> None:
    """RV3: an abort whose tick index is not retained in its phase."""
    run, _ = sealed_v6(tmp_path)
    document = _documents(run)[ABORT_ON]
    document["tick"] = 99
    crafted = _rewrite(run, ABORT_ON, document)
    expect_run(RFail.TICK_NOT_PAIRED, lambda: read6(run.root, crafted))


def test_rv3_an_abort_without_tick_temperatures_is_not_paired(tmp_path: Path) -> None:
    """RV3: an abort naming a retained tick in a tree with no tick temperatures."""
    run = v6_run(tmp_path, pair=False)
    digest = run.writer.seal().manifest_sha256
    retained = read6(run.root, digest)
    assert retained.tick_temperatures == ()
    crafted = _rewrite(run, ABORT_OFF, doc(abort_for(run.off_ticks[-1])))
    expect_run(RFail.TICK_NOT_PAIRED, lambda: read6(run.root, crafted))


def test_rv3_a_duplicate_abort_line_is_refused(tmp_path: Path) -> None:
    """RV3: the same ``(phase, tick, reason)`` twice in one file."""
    run, _ = sealed_v6(tmp_path)
    line = line_of(doc(abort_for(run.on_ticks[-1])))
    crafted = _rewrite(run, ABORT_ON, line + line)
    expect_run(RFail.ABORT_DUPLICATED, lambda: read6(run.root, crafted))
    distinct = _rewrite(run, ABORT_ON, _abort_lines(run))
    assert len(read6(run.root, distinct).temperature_aborts) == 2


# ------------------------------------------------------------ RV4 RV5 carrier


def test_rv4_v6_reads_a_v5_shaped_tree_with_empty_new_tuples(tmp_path: Path) -> None:
    """RV4: paired temperatures without the new files read with empty tuples."""
    run = v6_run(tmp_path, candidates=False)
    retained = read6(run.root, run.writer.seal().manifest_sha256)
    assert retained.temperature_aborts == () and retained.mcp_candidates == ()
    assert len(retained.tick_temperatures) == 4


def test_rv4_v6_reads_a_historical_tree_as_absent(tmp_path: Path) -> None:
    """RV4: a tree without temperatures reads ``ABSENT`` with every new tuple empty."""
    run = v6_run(tmp_path, candidates=False, pair=False)
    retained = read6(run.root, run.writer.seal().manifest_sha256)
    assert retained.tick_temperature_state is temperature.ColdTickTemperatureEvidenceState.ABSENT
    assert (retained.tick_temperatures, retained.temperature_aborts, retained.mcp_candidates) == (
        (),
        (),
        (),
    )


def test_rv5_the_v6_carrier_refuses_aborts_without_temperatures(tmp_path: Path) -> None:
    """RV5: the fifth carrier check, with V5's four checks still applied."""
    run, digest = sealed_v6(tmp_path)
    retained = read6(run.root, digest)
    values = {name: getattr(retained, name) for name in V6.model_fields}
    V6.model_validate(values)
    with pytest.raises(pydantic.ValidationError):
        V6.model_validate(
            {
                **values,
                "tick_temperature_state": temperature.ColdTickTemperatureEvidenceState.ABSENT,
                "tick_temperatures": (),
            }
        )
    for update in (
        {"lifecycle_state": lifecycle.ColdLifecycleEvidenceState.PRESENT},
        {"advisory_attempt_state": advisory.ColdAdvisoryAttemptEvidenceState.COMPLETE},
        {"terminal_state": terminal.ColdFailedRunTerminalEvidenceState.PRESENT},
        {"tick_temperature_state": temperature.ColdTickTemperatureEvidenceState.ABSENT},
    ):
        with pytest.raises(pydantic.ValidationError):
            V6.model_validate({**values, **update})
    assert tuple(V6.model_fields) == (
        "run",
        "lifecycle_state",
        "lifecycle",
        "advisory_attempt_state",
        "advisory_attempts",
        "terminal_state",
        "terminal",
        "tick_temperature_state",
        "tick_temperatures",
        "temperature_aborts",
        "mcp_candidates",
    )
