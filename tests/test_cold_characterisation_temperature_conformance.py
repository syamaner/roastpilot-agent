"""Temperature conformance policy 3 over retained V6 cold runs (#997 T1); hardware-free.

Behavioural cases write runs through the real writer (the advisory-policy fixture
plus one candidate per phase and one paired temperature per tick), seal them, and
read them back with the strict V6 reader.  Unit cases (labelled) forge carriers with
``model_construct``/``object.__new__``/``object.__setattr__``, monkeypatch the module
namespace, or inspect its AST.  Every expected finding tuple is written by hand.
"""

import ast
import enum
import typing
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent.cold_characterisation import advisory_conformance as ac
from roastpilot_agent.cold_characterisation import conformance
from roastpilot_agent.cold_characterisation import evidence_advisory as advisory
from roastpilot_agent.cold_characterisation import evidence_lifecycle as lifecycle
from roastpilot_agent.cold_characterisation import evidence_reader as reader
from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation import evidence_store as store
from roastpilot_agent.cold_characterisation import evidence_temperature as temperature
from roastpilot_agent.cold_characterisation import evidence_temperature_run as run_ev
from roastpilot_agent.cold_characterisation import evidence_terminal as terminal
from roastpilot_agent.cold_characterisation import temperature_conformance as tc
from roastpilot_agent.cold_characterisation.identity import REQUIRED_MCP_VERSION
from roastpilot_agent.cold_characterisation.temperature_projection import (
    ColdTickTemperatureProjection,
)
from tests.test_cold_characterisation_advisory_conformance import (
    _write_run,  # pyright: ignore[reportPrivateUsage]
    base,
)
from tests.test_cold_characterisation_conformance import (
    DRIVER,
    S_OFF,
    S_ON,
    Plan,
    Tick,
    audio,
    plan,
)
from tests.test_cold_characterisation_evidence_builders import RUN_ID
from tests.test_cold_characterisation_evidence_store import OFF, ON
from tests.test_cold_characterisation_evidence_temperature import temperature_for
from tests.test_cold_characterisation_evidence_temperature_run import (
    OTHER_RUN_ID,
    abort_for,
    candidate_for,
    provenance,
)
from tests.test_cold_characterisation_temperature_screen import obs

F = tc.ColdTemperatureConformanceFinding
Outcome = tc.ColdTemperatureConformanceOutcome
Result = tc.ColdTemperatureConformanceResult
AF = ac.ColdAdvisoryConformanceFinding
V6 = reader.ColdRetainedRunV6
Findings = tuple[tc.ColdTemperatureConformanceFinding, ...]
Phase = schema.ColdPhaseKind
SOURCE = Path(tc.__file__)
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
#: Private helpers and unvalidated construction, reached through ``Any`` (unit exceptions).
PRIVATE: typing.Any = tc
FORGE: typing.Any = V6.model_construct
FORGE_ADVISORY: typing.Any = ac.ColdAdvisoryConformanceResult.model_construct

#: Hand-derived from the advisory fixture ``plan()``: activation 10.0 and 1830.0.
ACTIVATION: typing.Final = {OFF: 10.0, ON: 1830.0}
SESSIONS: typing.Final = {OFF: S_OFF, ON: S_ON}
#: Three ticks beside the plan's own (+1, +2, +3 s): the last before and two at or after 60 s.
EXTRA_OFFSETS: typing.Final = (59.0, 60.0, 61.0)
TICKS_PER_PHASE: typing.Final = 6

Schedule = typing.Callable[[Phase, int], ColdTickTemperatureProjection]
AfterTick = typing.Callable[
    [store.ColdEvidenceWriter, schema.ColdRunHeader, schema.ColdTickRecord], None
]


class SubV6(V6):
    """A V6 carrier subclass (never admitted)."""


class DictSubclass(dict[object, object]):
    """A non-exact raw-state mapping (``__dict__`` accepts a ``dict`` subclass)."""


class SubTuple(tuple[object, ...]):
    """A tuple subclass (never admitted)."""


class SubAdvisoryResult(ac.ColdAdvisoryConformanceResult):
    """A policy-2 result subclass (never admitted)."""


FORGE_SUB_ADVISORY: typing.Any = SubAdvisoryResult.model_construct


class HostileKey(str):
    """A ``str`` subclass key that counts every hash and equality call."""

    calls = 0

    def __hash__(self) -> int:
        HostileKey.calls += 1
        return str.__hash__(self)

    def __eq__(self, other: object) -> bool:
        HostileKey.calls += 1
        return str.__eq__(self, other)


class Detonator:
    """A state object whose equality must never be called."""

    def __eq__(self, other: object) -> bool:
        raise AssertionError("equality called")

    __hash__ = object.__hash__


class _Interrupt(BaseException):
    """A non-``Exception`` interruption."""


# ------------------------------------------------------------------ fixture chain


def extended(tmp_path: Path) -> Plan:
    """The advisory fixture plan plus ticks at +59, +60 and +61 s in each phase."""
    run = plan(tmp_path)
    for phase in (OFF, ON):
        for offset in EXTRA_OFFSETS:
            index = len(run.ticks[phase])
            mono = ACTIVATION[phase] + offset
            run.ticks[phase].append(
                Tick(mono, index, SESSIONS[phase], offset, audio(index), {"driver": DRIVER})
            )
            run.hosts[phase].append(mono)
        assert len(run.ticks[phase]) == TICKS_PER_PHASE
    return run


def rising(phase: Phase, index: int) -> ColdTickTemperatureProjection:
    """The default schedule: one more accepted packet per tick, 20.0 °C, no faults."""
    del phase
    return obs(index + 1)


def write_tree(
    tmp_path: Path,
    *,
    schedule: Schedule = rising,
    candidates: tuple[Phase, ...] = (OFF, ON),
    extra: AfterTick | None = None,
) -> tuple[str, str]:
    """Write one candidate per listed phase and one paired temperature per tick; seal."""

    def after_header(writer: store.ColdEvidenceWriter, header: schema.ColdRunHeader) -> None:
        if header.phase in candidates:
            writer.append_mcp_candidate(candidate_for(header))

    def after_tick(
        writer: store.ColdEvidenceWriter,
        header: schema.ColdRunHeader,
        record: schema.ColdTickRecord,
    ) -> None:
        projection = schedule(header.phase, record.tick)
        writer.append_tick_temperature(temperature_for(record, temperature=projection))
        if extra is not None:
            extra(writer, header, record)

    return _write_run(
        tmp_path, extended(tmp_path), base(), after_header=after_header, after_tick=after_tick
    )


def read6(root: str, digest: str) -> reader.ColdRetainedRunV6:
    """Read the shared test run through the V6 reader."""
    return reader.read_retained_run_v6(root, run_id=RUN_ID, expected_manifest_sha256=digest)


def written(tmp_path: Path, **options: typing.Any) -> reader.ColdRetainedRunV6:
    """Write, seal and read one V6 run."""
    return read6(*write_tree(tmp_path, **options))


def check(run: object) -> tc.ColdTemperatureConformanceResult:
    """Run the checker under test."""
    return tc.check_temperature_conformance(run)


def expect(result: tc.ColdTemperatureConformanceResult, findings: Findings) -> None:
    """Assert the version, the outcome and the exact finding tuple."""
    assert type(result) is Result
    assert result.policy_version == 3
    assert result.outcome is (
        Outcome.NOT_CONFORMANT if findings else Outcome.TEMPERATURE_SCREENED_CONFORMANT
    )
    assert result.findings == findings


@pytest.fixture(scope="module")
def conformant(tmp_path_factory: pytest.TempPathFactory) -> reader.ColdRetainedRunV6:
    """The conforming V6 run, written once per module and never mutated."""
    return written(tmp_path_factory.mktemp("conformant"))


def values_of(run: reader.ColdRetainedRunV6) -> dict[str, typing.Any]:
    """Return a carrier's field values by name."""
    return {name: getattr(run, name) for name in V6.model_fields}


def forged(run: reader.ColdRetainedRunV6, /, **update: object) -> typing.Any:
    """Build an unvalidated V6 carrier from a genuine one with updates."""
    return FORGE(**{**values_of(run), **update})


def with_state(run: object, data: object) -> typing.Any:
    """Return a carrier whose raw ``__dict__`` is replaced."""
    copy = typing.cast(pydantic.BaseModel, run).model_copy()
    object.__setattr__(copy, "__dict__", data)
    return copy


def header(run: reader.ColdRetainedRunV6, phase: Phase) -> schema.ColdRunHeader:
    """Return the retained header of one phase."""
    return next(item.header for item in run.run.headers if item.header.phase is phase)


def last_tick(run: reader.ColdRetainedRunV6, phase: Phase) -> schema.ColdTickRecord:
    """Return the last retained v1 tick of one phase."""
    ticks = [
        typing.cast(schema.ColdTickRecord, record)
        for stream in run.run.streams
        if stream.phase is phase and stream.stream is schema.ColdEvidenceStream.TICK
        for record in stream.records
    ]
    return ticks[-1]


def terminal_for(run: reader.ColdRetainedRunV6) -> terminal.ColdFailedRunTerminalRecord:
    """A genuine failed-run terminal record for the recording-on phase."""
    return terminal.build_failed_run_terminal_record(
        header(run, ON),
        advisory_settlement=terminal.ColdFailedRunAdvisorySettlement.RECORDED_UNRESOLVED_NOT_INVOKED,
        provider_cancellation=terminal.ColdFailedRunProviderCancellation.NO_PROVIDER_TASK,
        lifecycle_records_retained=0,
        advisory_attempt_records_retained=0,
    )


#: Every stage-5 legacy read and binding the module calls by name (L3).
STAGE_5_READS: typing.Final = (
    "validate_record",
    "check_record_binding",
    "check_tick_temperature_binding",
    "check_temperature_run_binding",
    "check_tick_temperature_pairing",
)


def watch_stage_5(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
    """Replace each stage-5 name with a recording spy that delegates to the real function.

    The spies never refuse anything themselves, so they cannot cause a refusal.
    """
    reads: dict[str, list[object]] = {name: [] for name in STAGE_5_READS}
    for name in STAGE_5_READS:
        real: typing.Callable[..., object] = getattr(tc, name)
        record = reads[name]

        def spy(
            *args: object,
            _real: typing.Callable[..., object] = real,
            _record: list[object] = record,
            **kwargs: object,
        ) -> object:
            _record.append(args)
            return _real(*args, **kwargs)

        monkeypatch.setattr(tc, name, spy)
    return reads


def expected_stage_5_reads(run: reader.ColdRetainedRunV6) -> dict[str, int]:
    """Derive the stage-5 call counts of an accepted run from its own records.

    ``validate_record`` re-validates each header for binding and each retained v1
    tick; each header binds once; each temperature and each candidate or abort
    binds once; pairing runs once per phase.
    """
    ticks = sum(
        len(stream.records)
        for stream in run.run.streams
        if stream.stream is schema.ColdEvidenceStream.TICK
    )
    return {
        "validate_record": len(run.run.headers) + ticks,
        "check_record_binding": len(run.run.headers),
        "check_tick_temperature_binding": len(run.tick_temperatures),
        "check_temperature_run_binding": len(run.mcp_candidates) + len(run.temperature_aborts),
        "check_tick_temperature_pairing": len(schema.ColdPhaseKind),
    }


# ------------------------------------------------------------ PC1 positive


def test_pc1_the_fixture_run_is_temperature_screened_conformant(
    tmp_path: Path, conformant: reader.ColdRetainedRunV6
) -> None:
    """PC1 (AC1-AC6): the written, sealed and read run conforms; V3 refuses its new files."""
    assert len(conformant.tick_temperatures) == 2 * TICKS_PER_PHASE
    assert len(conformant.mcp_candidates) == 2
    expect(check(conformant), ())
    root, digest = write_tree(tmp_path)
    with pytest.raises(store.ColdEvidenceStoreError) as raised:
        reader.read_retained_run_v3(root, run_id=RUN_ID, expected_manifest_sha256=digest)
    assert raised.value.failure is store.ColdEvidenceStoreFailure.ENTRY_PATH_INVALID


def test_pc1_the_result_carries_exactly_three_closed_fields() -> None:
    """PC1 (L7): the exact public result inventory and vocabulary."""
    assert tuple(Result.model_fields) == ("policy_version", "outcome", "findings")
    assert tc.TEMPERATURE_CONFORMANCE_POLICY_VERSION == 3
    assert [(m.name, m.value) for m in Outcome] == [
        ("TEMPERATURE_SCREENED_CONFORMANT", "temperature_screened_conformant"),
        ("NOT_CONFORMANT", "not_conformant"),
    ]
    assert [m.name for m in F] == [
        "CARRIER_NOT_ADMITTED",
        "RECORD_NOT_READMITTED",
        "TICK_TEMPERATURE_ABSENT",
        "TEMPERATURE_ABORT_RECORDED",
        "FAILED_RUN_TERMINAL_PRESENT",
        "MCP_CANDIDATE_MISSING",
        "MCP_CANDIDATE_NOT_UNIQUE",
        "MCP_CANDIDATE_INCONSISTENT",
        "ADVISORY_POLICY_NOT_CONFORMANT",
        "EVIDENCE_BINDING_REFUSED",
        "TICK_TEMPERATURE_NOT_PAIRED",
        "MCP_CANDIDATE_VERSION_MISMATCH",
        "ACTIVATION_NOT_BOUND",
        "TEMPERATURE_SCREEN_VIOLATED",
        "CHECKER_INTERNAL_FAILURE",
    ]
    for kind in (Outcome, F):
        assert kind.__bases__ == (enum.Enum,)
        assert not issubclass(kind, (str, int))
        assert all(member.value == member.name.lower() for member in kind)


def test_pc1_a_hand_built_v6_is_judged_like_the_read_run(
    conformant: reader.ColdRetainedRunV6,
) -> None:
    """PC1 (unit): a validated rebuild conforms too; the result is not provenance."""
    expect(check(V6(**values_of(conformant))), ())


# ------------------------------------------------------------- PC2 guards


def test_pc2_absent_temperatures(conformant: reader.ColdRetainedRunV6) -> None:
    """PC2: absent temperatures are recorded and the retained ticks do not pair."""
    run = conformant.model_copy(
        update={
            "tick_temperature_state": temperature.ColdTickTemperatureEvidenceState.ABSENT,
            "tick_temperatures": (),
        }
    )
    expect(check(run), (F.TICK_TEMPERATURE_ABSENT, F.TICK_TEMPERATURE_NOT_PAIRED))


def test_pc2_one_dropped_temperature_does_not_pair(conformant: reader.ColdRetainedRunV6) -> None:
    """PC2: one temperature removed while the state stays ``PRESENT``."""
    run = conformant.model_copy(update={"tick_temperatures": conformant.tick_temperatures[:-1]})
    expect(check(run), (F.TICK_TEMPERATURE_NOT_PAIRED,))


def test_pc2_a_genuine_abort_is_recorded(tmp_path: Path) -> None:
    """PC2: a retained temperature abort on the last paired tick never conforms."""

    def abort_last(
        writer: store.ColdEvidenceWriter,
        header: schema.ColdRunHeader,
        record: schema.ColdTickRecord,
    ) -> None:
        if header.phase is ON and record.tick == TICKS_PER_PHASE - 1:
            writer.append_temperature_abort(abort_for(record))

    run = written(tmp_path, extra=abort_last)
    assert len(run.temperature_aborts) == 1
    expect(check(run), (F.TEMPERATURE_ABORT_RECORDED,))


def test_pc2_a_missing_candidate(tmp_path: Path, conformant: reader.ColdRetainedRunV6) -> None:
    """PC2: the recording-on candidate removed, as data and as a written tree."""
    run = conformant.model_copy(update={"mcp_candidates": conformant.mcp_candidates[:1]})
    expect(check(run), (F.MCP_CANDIDATE_MISSING,))
    expect(check(written(tmp_path, candidates=(OFF,))), (F.MCP_CANDIDATE_MISSING,))


def test_pc2_a_duplicated_candidate(conformant: reader.ColdRetainedRunV6) -> None:
    """PC2: the recording-off candidate duplicated."""
    off, on = conformant.mcp_candidates
    run = conformant.model_copy(update={"mcp_candidates": (off, off, on)})
    expect(check(run), (F.MCP_CANDIDATE_NOT_UNIQUE,))


def test_pc2_an_inconsistent_candidate(conformant: reader.ColdRetainedRunV6) -> None:
    """PC2: the recording-on provenance names a different artefact digest."""
    other = candidate_for(header(conformant, ON), candidate=provenance(artefact_sha256="c" * 64))
    run = conformant.model_copy(update={"mcp_candidates": (conformant.mcp_candidates[0], other)})
    expect(check(run), (F.MCP_CANDIDATE_INCONSISTENT,))


def test_pc2_a_candidate_version_mismatch(conformant: reader.ColdRetainedRunV6) -> None:
    """PC2: both candidates report 0.2.3 while the identities pin the MCP version."""
    assert REQUIRED_MCP_VERSION == "0.2.2"
    both = tuple(
        candidate_for(header(conformant, phase), candidate=provenance(reported_version="0.2.3"))
        for phase in (OFF, ON)
    )
    expect(
        check(conformant.model_copy(update={"mcp_candidates": both})),
        (F.MCP_CANDIDATE_VERSION_MISMATCH,),
    )


def test_pc2_a_foreign_candidate_digest_is_a_binding_refusal(
    conformant: reader.ColdRetainedRunV6,
) -> None:
    """PC2: a re-admissible candidate whose identity digest binds to no header."""
    foreign = candidate_for(header(conformant, ON), identity_sha256="0" * 64)
    run = conformant.model_copy(update={"mcp_candidates": (conformant.mcp_candidates[0], foreign)})
    expect(check(run), (F.EVIDENCE_BINDING_REFUSED,))


def test_pc2_an_eligible_temperature_outside_the_screen(tmp_path: Path) -> None:
    """PC2: one eligible recording-off temperature at 41.0 °C, written from the start."""

    def hot(phase: Phase, index: int) -> ColdTickTemperatureProjection:
        return obs(index + 1, bt=41.0) if (phase, index) == (OFF, 4) else obs(index + 1)

    expect(check(written(tmp_path, schedule=hot)), (F.TEMPERATURE_SCREEN_VIOLATED,))


def test_pc2_a_foreign_candidate_run_id_is_inconsistent_and_unbound(
    conformant: reader.ColdRetainedRunV6,
) -> None:
    """PC2 (L3): a re-admissible ON candidate whose run id alone differs.

    The altered record re-admits, so no parse refusal can mask the classification:
    stage 3 records the inconsistency and stage 5 the binding refusal.
    """
    on_header = header(conformant, ON)
    altered = candidate_for(on_header, run_id=OTHER_RUN_ID)
    assert altered.run_id == OTHER_RUN_ID != conformant.mcp_candidates[1].run_id
    assert altered.candidate == conformant.mcp_candidates[1].candidate
    assert run_ev.validate_mcp_candidate_record(altered) == altered
    run = conformant.model_copy(update={"mcp_candidates": (conformant.mcp_candidates[0], altered)})
    expect(check(run), (F.MCP_CANDIDATE_INCONSISTENT, F.EVIDENCE_BINDING_REFUSED))


def test_pc2_a_terminal_skips_policy_2(
    monkeypatch: pytest.MonkeyPatch, conformant: reader.ColdRetainedRunV6
) -> None:
    """PC2 (L2, L3): a retained terminal is recorded; policy 2 and every stage-5 read never run."""
    calls: list[object] = []

    def spy(run: object) -> object:
        calls.append(run)
        return ac.check_advisory_conformance(run)

    monkeypatch.setattr(tc, "check_advisory_conformance", spy)
    reads = watch_stage_5(monkeypatch)
    run = conformant.model_copy(
        update={
            "terminal_state": terminal.ColdFailedRunTerminalEvidenceState.PRESENT,
            "terminal": terminal_for(conformant),
        }
    )
    expect(check(run), (F.FAILED_RUN_TERMINAL_PRESENT,))
    assert calls == []
    assert {name: len(items) for name, items in reads.items()} == dict.fromkeys(STAGE_5_READS, 0)


# -------------------------------------------------------------- PC3 replay


def test_pc3_a_fault_counted_across_the_boundary_is_a_violation(tmp_path: Path) -> None:
    """PC3: a read error counted between the +59 s and +60 s ticks; the prior carries over."""

    def faulted(phase: Phase, index: int) -> ColdTickTemperatureProjection:
        return obs(index + 1, rerr=1) if phase is OFF and index >= 4 else obs(index + 1)

    expect(check(written(tmp_path, schedule=faulted)), (F.TEMPERATURE_SCREEN_VIOLATED,))


def test_pc3_counters_restarting_in_the_next_phase_conform(tmp_path: Path) -> None:
    """PC3: recording-on counters start far below recording-off's; replay resets per phase."""

    def restarting(phase: Phase, index: int) -> ColdTickTemperatureProjection:
        return obs(index + 101) if phase is OFF else obs(index + 1)

    expect(check(written(tmp_path, schedule=restarting)), ())


# ------------------------------------------------------- PC4 root admission


def _shuffled_key_state(run: reader.ColdRetainedRunV6) -> dict[object, object]:
    """A raw state whose ``run`` key is a hostile ``str`` subclass (count unchanged)."""
    state: dict[object, object] = dict(object.__getattribute__(run, "__dict__"))
    state[HostileKey("run")] = state.pop("run")
    return state


def _root_refusals(run: reader.ColdRetainedRunV6) -> list[tuple[str, object]]:
    """Return one forged root per stage-1 guard; each must be refused alone."""
    values = values_of(run)
    v5_values = {name: values[name] for name in reader.ColdRetainedRunV5.model_fields}
    uninitialised_with_state = V6.__new__(V6)
    object.__setattr__(uninitialised_with_state, "__dict__", dict(values))
    extra_key = {**values, "extra": 1}
    swapped = dict(values)
    swapped["other"] = swapped.pop("mcp_candidates")
    absent = dict(values)
    absent.pop("mcp_candidates")
    abort = abort_for(last_tick(run, ON))
    l_state = lifecycle.ColdLifecycleEvidenceState
    a_state = advisory.ColdAdvisoryAttemptEvidenceState
    f_state = terminal.ColdFailedRunTerminalEvidenceState
    t_state = temperature.ColdTickTemperatureEvidenceState
    return [
        ("v5", reader.ColdRetainedRunV5(**v5_values)),
        ("subclass", SubV6.model_construct(**values)),
        ("uninitialised", V6.__new__(V6)),
        ("no-extras-slot", uninitialised_with_state),
        ("extra-key", with_state(run, extra_key)),
        ("missing-plus-extra", with_state(run, swapped)),
        ("absent-slot", with_state(run, absent)),
        ("nonempty-extras", _with_extra(run, {"x": 1})),
        ("non-dict-extras", _with_extra(run, [])),
        ("dict-subclass-state", with_state(run, DictSubclass(values.items()))),
        ("run-not-retained", forged(run, run=object())),
        ("state-token", forged(run, tick_temperature_state="present")),
        ("lifecycle-token", forged(run, lifecycle_state="present")),
        ("advisory-token", forged(run, advisory_attempt_state="complete")),
        ("terminal-token", forged(run, terminal_state="absent")),
        ("present-empty", forged(run, tick_temperatures=())),
        (
            "absent-with-records",
            forged(run, tick_temperature_state=t_state.ABSENT),
        ),
        ("terminal-present-none", forged(run, terminal_state=f_state.PRESENT)),
        ("terminal-absent-record", forged(run, terminal=terminal_for(run))),
        (
            "terminal-not-a-record",
            forged(run, terminal=object(), terminal_state=f_state.PRESENT),
        ),
        ("lifecycle-absent-with-records", forged(run, lifecycle_state=l_state.ABSENT)),
        ("lifecycle-present-empty", forged(run, lifecycle=())),
        (
            "advisory-absent-with-records",
            forged(run, advisory_attempt_state=a_state.ABSENT),
        ),
        (
            "advisory-open-tail-on-resolution",
            forged(run, advisory_attempt_state=a_state.OPEN_TAIL),
        ),
        ("advisory-complete-empty", forged(run, advisory_attempts=())),
        ("advisory-last-not-a-record", forged(run, advisory_attempts=(object(),))),
        ("list-not-tuple", forged(run, tick_temperatures=list(run.tick_temperatures))),
        ("tuple-subclass", forged(run, mcp_candidates=SubTuple(run.mcp_candidates))),
        (
            "aborts-without-temperatures",
            forged(
                run,
                tick_temperature_state=t_state.ABSENT,
                tick_temperatures=(),
                temperature_aborts=(abort,),
            ),
        ),
        ("detonating-state", forged(run, lifecycle_state=Detonator())),
        ("not-a-carrier", object()),
    ]


def _with_extra(run: object, extra: object) -> typing.Any:
    """Return a carrier whose ``__pydantic_extra__`` is replaced."""
    copy = typing.cast(pydantic.BaseModel, run).model_copy()
    object.__setattr__(copy, "__pydantic_extra__", extra)
    return copy


def test_pc4_every_forged_root_is_refused_alone(conformant: reader.ColdRetainedRunV6) -> None:
    """PC4: each stage-1 guard refuses its forgery with exactly ``CARRIER_NOT_ADMITTED``."""
    for name, value in _root_refusals(conformant):
        assert check(value).findings == (F.CARRIER_NOT_ADMITTED,), name


def test_pc4_positive_control_for_the_forgery_helpers(
    conformant: reader.ColdRetainedRunV6,
) -> None:
    """PC4 positive control: the unmodified forgery path itself conforms."""
    expect(check(forged(conformant)), ())
    expect(check(with_state(conformant, values_of(conformant))), ())


def test_pc4_a_hostile_key_is_refused_before_any_hook(
    conformant: reader.ColdRetainedRunV6,
) -> None:
    """PC4: the field count matches, so key type alone refuses it; no hook runs."""
    run = with_state(conformant, _shuffled_key_state(conformant))
    HostileKey.calls = 0
    expect(check(run), (F.CARRIER_NOT_ADMITTED,))
    assert HostileKey.calls == 0


def test_pc4_admitted_but_nonconformant_attempt_states(
    conformant: reader.ColdRetainedRunV6,
) -> None:
    """PC4 controls: consistent ``ABSENT`` and ``OPEN_TAIL`` attempts pass admission only."""
    absent = forged(
        conformant,
        advisory_attempts=(),
        advisory_attempt_state=advisory.ColdAdvisoryAttemptEvidenceState.ABSENT,
    )
    expect(check(absent), (F.ADVISORY_POLICY_NOT_CONFORMANT,))
    open_tail = forged(
        conformant,
        advisory_attempts=conformant.advisory_attempts[:-1],
        advisory_attempt_state=advisory.ColdAdvisoryAttemptEvidenceState.OPEN_TAIL,
    )
    expect(check(open_tail), (F.ADVISORY_POLICY_NOT_CONFORMANT,))


# --------------------------------------------------------- PC5 re-admission


def test_pc5_forged_new_records_are_not_readmitted(conformant: reader.ColdRetainedRunV6) -> None:
    """PC5 (O5): a forged nested projection, abort domain and candidate field."""
    first = conformant.tick_temperatures[0]
    bad_temperature = first.model_copy(
        update={"temperature": first.temperature.model_copy(update={"projection_version": 2})}
    )
    temps = (bad_temperature, *conformant.tick_temperatures[1:])
    expect(
        check(conformant.model_copy(update={"tick_temperatures": temps})),
        (F.RECORD_NOT_READMITTED,),
    )
    host_abort = abort_for(last_tick(conformant, ON)).model_copy(
        update={"domain": schema.ColdAbortDomain.HOST}
    )
    expect(
        check(conformant.model_copy(update={"temperature_aborts": (host_abort,)})),
        (F.RECORD_NOT_READMITTED,),
    )
    off, on = conformant.mcp_candidates
    attested = on.model_copy(
        update={"candidate": on.candidate.model_copy(update={"installed_bytes_attested": True})}
    )
    expect(
        check(conformant.model_copy(update={"mcp_candidates": (off, attested)})),
        (F.RECORD_NOT_READMITTED,),
    )


# --------------------------------------------------------- PC6 policy-2 result


def _conformant_advisory(**update: object) -> typing.Any:
    """An unvalidated policy-2 result: conformant unless updated."""
    values: dict[str, object] = {
        "policy_version": 2,
        "outcome": ac.ColdAdvisoryConformanceOutcome.ADVISORY_CONFORMANT,
        "findings": (),
        "pre_advisory_findings": (),
    }
    return FORGE_ADVISORY(**{**values, **update})


def _advisory_state(**data: object) -> typing.Any:
    """A conformant policy-2 result whose raw ``__dict__`` is replaced."""
    result = _conformant_advisory()
    object.__setattr__(result, "__dict__", data)
    return result


def _advisory_refusals() -> list[tuple[str, object]]:
    """Every policy-2 result that is not the fully determined conformant shape."""
    conformant_values: dict[str, object] = dict(
        object.__getattribute__(_conformant_advisory(), "__dict__")
    )
    with_extra = _conformant_advisory()
    object.__setattr__(with_extra, "__pydantic_extra__", {"x": 1})
    swapped = dict(conformant_values)
    swapped["other"] = swapped.pop("pre_advisory_findings")
    return [
        ("findings-nonempty", _conformant_advisory(findings=(AF.ATTEMPTS_ABSENT,))),
        (
            "pre-findings-nonempty",
            _conformant_advisory(
                pre_advisory_findings=(conformance.ColdConformanceFinding.TICKS_ABSENT,)
            ),
        ),
        ("version-true", _conformant_advisory(policy_version=True)),
        ("version-float", _conformant_advisory(policy_version=2.0)),
        ("version-3", _conformant_advisory(policy_version=3)),
        ("findings-list", _conformant_advisory(findings=[])),
        ("pre-findings-list", _conformant_advisory(pre_advisory_findings=[])),
        ("outcome-token", _conformant_advisory(outcome="advisory_conformant")),
        ("extra-key", _advisory_state(**conformant_values, extra=1)),
        ("missing-plus-extra", _advisory_state(**swapped)),
        ("nonempty-extras", with_extra),
        (
            "uninitialised",
            ac.ColdAdvisoryConformanceResult.__new__(ac.ColdAdvisoryConformanceResult),
        ),
        ("subclass", FORGE_SUB_ADVISORY(**conformant_values)),
        ("not-a-result", object()),
        (
            "genuine-not-conformant",
            ac.ColdAdvisoryConformanceResult(
                policy_version=2,
                outcome=ac.ColdAdvisoryConformanceOutcome.NOT_CONFORMANT,
                findings=(AF.CARRIER_NOT_ADMITTED,),
                pre_advisory_findings=(),
            ),
        ),
    ]


@pytest.mark.parametrize(
    ("name", "result"), _advisory_refusals(), ids=[c[0] for c in _advisory_refusals()]
)
def test_pc6_only_the_fully_determined_conformant_result_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
    conformant: reader.ColdRetainedRunV6,
    name: str,
    result: object,
) -> None:
    """PC6 (L3): each other policy-2 result stops before any legacy read or binding."""
    del name
    lifecycle_reads: list[object] = []

    def spy_lifecycle(record: object) -> object:
        lifecycle_reads.append(record)
        return lifecycle.validate_lifecycle_record(typing.cast(typing.Any, record))

    def forged_policy_2(run: object) -> object:
        return result

    monkeypatch.setattr(tc, "check_advisory_conformance", forged_policy_2)
    monkeypatch.setattr(tc, "validate_lifecycle_record", spy_lifecycle)
    reads = watch_stage_5(monkeypatch)
    expect(check(conformant), (F.ADVISORY_POLICY_NOT_CONFORMANT,))
    assert lifecycle_reads == []
    assert {name: len(items) for name, items in reads.items()} == dict.fromkeys(STAGE_5_READS, 0)


def test_pc6_positive_control_the_real_policy_2_is_called_once_on_the_projection(
    monkeypatch: pytest.MonkeyPatch, conformant: reader.ColdRetainedRunV6
) -> None:
    """PC6: the delegating spy sees one exact V3 projection of the same slot objects."""
    calls: list[object] = []
    lifecycle_reads: list[object] = []

    def spy(run: object) -> ac.ColdAdvisoryConformanceResult:
        calls.append(run)
        return ac.check_advisory_conformance(run)

    def spy_lifecycle(record: object) -> object:
        lifecycle_reads.append(record)
        return lifecycle.validate_lifecycle_record(typing.cast(typing.Any, record))

    monkeypatch.setattr(tc, "check_advisory_conformance", spy)
    monkeypatch.setattr(tc, "validate_lifecycle_record", spy_lifecycle)
    reads = watch_stage_5(monkeypatch)
    expect(check(conformant), ())
    counts = {name: len(items) for name, items in reads.items()}
    assert counts == expected_stage_5_reads(conformant)
    assert counts == {
        "validate_record": 2 + 2 * TICKS_PER_PHASE,
        "check_record_binding": 2,
        "check_tick_temperature_binding": 2 * TICKS_PER_PHASE,
        "check_temperature_run_binding": 2,
        "check_tick_temperature_pairing": 2,
    }
    assert len(calls) == 1
    projection = typing.cast(reader.ColdRetainedRunV3, calls[0])
    assert type(projection) is reader.ColdRetainedRunV3
    assert projection.run is conformant.run
    assert projection.lifecycle_state is conformant.lifecycle_state
    assert projection.lifecycle is conformant.lifecycle
    assert projection.advisory_attempt_state is conformant.advisory_attempt_state
    assert projection.advisory_attempts is conformant.advisory_attempts
    assert len(lifecycle_reads) == 2


# ------------------------------------------------------ PC7 internal failure


def test_pc7_an_internal_rule_group_failure_never_conforms(
    monkeypatch: pytest.MonkeyPatch, conformant: reader.ColdRetainedRunV6
) -> None:
    """PC7: a screen exception is an internal failure; nothing else is invented."""

    def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("screen failed")

    monkeypatch.setattr(tc, "evaluate_temperature", explode)
    expect(check(conformant), (F.CHECKER_INTERNAL_FAILURE,))


def test_pc7_an_outer_failure_never_conforms(
    monkeypatch: pytest.MonkeyPatch, conformant: reader.ColdRetainedRunV6
) -> None:
    """PC7: an exception outside every guarded group is an internal failure."""

    def explode(run: object) -> object:
        raise RuntimeError("admission failed")

    monkeypatch.setattr(PRIVATE, "_admit", explode)
    expect(check(conformant), (F.CHECKER_INTERNAL_FAILURE,))


def test_pc7_a_base_exception_propagates(
    monkeypatch: pytest.MonkeyPatch, conformant: reader.ColdRetainedRunV6
) -> None:
    """PC7: a ``BaseException`` that is not an ``Exception`` is never caught."""

    def interrupt(*args: object, **kwargs: object) -> object:
        raise _Interrupt

    monkeypatch.setattr(tc, "evaluate_temperature", interrupt)
    with pytest.raises(_Interrupt):
        check(conformant)


@pytest.mark.parametrize(
    "anchors",
    [((OFF, 1), (ON, 5)), ((ON, 0), (ON, 5)), ((OFF, 0), (ON, 99))],
    ids=["not-activation", "wrong-phase", "beyond-the-lifecycle"],
)
def test_pc7_activation_guard_isolation(
    monkeypatch: pytest.MonkeyPatch,
    conformant: reader.ColdRetainedRunV6,
    anchors: tuple[tuple[Phase, int], ...],
) -> None:
    """PC7 (guard isolation): policy 2 already fixes the anchors on real input.

    The activation guard is defensive on admitted input, so the anchor table is
    patched to reach it; an unbound activation records its finding and skips replay.
    """
    monkeypatch.setattr(PRIVATE, "_ACTIVATIONS", anchors)
    expect(check(conformant), (F.ACTIVATION_NOT_BOUND,))


def test_pc7_version_guard_isolation(
    monkeypatch: pytest.MonkeyPatch, conformant: reader.ColdRetainedRunV6
) -> None:
    """PC7 (guard isolation): an identity without the version key is a mismatch."""
    monkeypatch.setattr(PRIVATE, "_MCP_VERSION_KEY", "no_such_key")
    expect(check(conformant), (F.MCP_CANDIDATE_VERSION_MISMATCH,))


def test_pc7_identity_version_reads_refuse_hostile_shapes() -> None:
    """PC7 (unit): the fresh-identity version read refuses every non-exact shape."""
    construct: typing.Any = store.ColdRetainedIdentityV1.model_construct
    base_values: dict[str, object] = {
        "run_id": RUN_ID,
        "pi_evidence_root": "/pi",
        "runtime_config_extras": {},
        "server_info_extras": {},
    }
    hostile: dict[object, object] = {"coffee_roaster_mcp_version": "0.2.2"}
    hostile[HostileKey("x")] = 1
    read = PRIVATE._reported_version
    assert read(construct(**base_values, known={"coffee_roaster_mcp_version": "0.2.2"})) == "0.2.2"
    HostileKey.calls = 0
    shapes: list[object] = [[], {"coffee_roaster_mcp_version": 2}, hostile, {}]
    for known in shapes:
        assert read(construct(**base_values, known=known)) is None
    assert HostileKey.calls == 0
    assert read(store.ColdRetainedIdentityV1.__new__(store.ColdRetainedIdentityV1)) is None


# -------------------------------------------------------------- PC8 result


def _fabricated_finding() -> object:
    """An exact-class finding object that is not a real member."""
    value = object.__new__(F)
    assert type(value) is F
    assert all(value is not member for member in F)
    return value


@pytest.mark.parametrize(
    "values",
    [
        {"policy_version": True},
        {"policy_version": 3.0},
        {"policy_version": 2},
        {"outcome": "not_conformant"},
        {"findings": [F.CARRIER_NOT_ADMITTED]},
        {"findings": (F.TICK_TEMPERATURE_NOT_PAIRED, F.TICK_TEMPERATURE_ABSENT)},
        {"findings": (F.CARRIER_NOT_ADMITTED, F.CARRIER_NOT_ADMITTED)},
        {"findings": ("carrier_not_admitted",)},
        {"findings": (_fabricated_finding(),)},
        {"findings": (AF.CARRIER_NOT_ADMITTED,)},
        {"outcome": Outcome.TEMPERATURE_SCREENED_CONFORMANT},
        {"findings": ()},
    ],
)
def test_pc8_the_result_model_refuses_each_disagreement(values: dict[str, object]) -> None:
    """PC8: version, members, order, uniqueness and outcome agreement are enforced.

    The base is a valid not-conformant result; each case changes one value.
    """
    base_values: dict[str, object] = {
        "policy_version": 3,
        "outcome": Outcome.NOT_CONFORMANT,
        "findings": (F.CARRIER_NOT_ADMITTED,),
    }
    Result(**typing.cast(typing.Any, base_values))
    with pytest.raises(pydantic.ValidationError):
        Result(**typing.cast(typing.Any, {**base_values, **values}))


# ------------------------------------------------------------ PC9 history


def test_pc9_a_historical_tree_never_conforms_and_keeps_its_interpretation(
    tmp_path: Path,
) -> None:
    """PC9 (L4): a policy-2-conformant V3-only tree, read through V6 and through V3."""
    root, digest = _write_run(tmp_path, plan(tmp_path), base())
    expect(
        check(read6(root, digest)),
        (F.TICK_TEMPERATURE_ABSENT, F.MCP_CANDIDATE_MISSING, F.TICK_TEMPERATURE_NOT_PAIRED),
    )
    retained = reader.read_retained_run_v3(root, run_id=RUN_ID, expected_manifest_sha256=digest)
    result = ac.check_advisory_conformance(retained)
    assert result.outcome is ac.ColdAdvisoryConformanceOutcome.ADVISORY_CONFORMANT
    assert (result.findings, result.pre_advisory_findings) == ((), ())


# ------------------------------------------------------------ PC10 structure


def _imports() -> tuple[set[str], set[str]]:
    """Return plain and ``from`` import module names."""
    plain: set[str] = set()
    named: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            named.add(node.module)
    return plain, named


def test_pc10_imports_are_the_allow_list() -> None:
    """PC10: no policy-1, runtime, I/O or provider import."""
    plain, named = _imports()
    cold = "roastpilot_agent.cold_characterisation."
    assert plain == {"enum", "typing", "pydantic"}
    allowed = {
        cold + name
        for name in (
            "advisory_conformance",
            "evidence_reader",
            "evidence_schema",
            "evidence_lifecycle",
            "evidence_advisory",
            "evidence_terminal",
            "evidence_store",
            "evidence_temperature",
            "evidence_temperature_run",
            "temperature_screen",
            "temperature_projection",
        )
    }
    assert named <= allowed
    assert cold + "conformance" not in named


def test_pc10_syntax_fences() -> None:
    """PC10: two broad handlers, one internal projection, no ``isinstance`` or fenced text."""
    source = SOURCE.read_text(encoding="utf-8")
    broad = [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "Exception"
    ]
    assert len(broad) == 2
    owners = {
        function.name
        for function in ast.walk(TREE)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if node in broad
    }
    assert owners == {"_guarded", "check_temperature_conformance"}
    assert source.count("model_construct") == 1
    for text in ("isinstance(", "subprocess", "StrEnum", "print(", "two_phase"):
        assert text not in source, text


def test_pc10_activation_positions_are_policy_2_anchors() -> None:
    """PC10: the activation positions equal policy 2's anchors (0 and 5)."""
    anchors = ac._ANCHORS  # pyright: ignore[reportPrivateUsage]
    assert [(phase, start) for phase, start, _ in anchors] == list(PRIVATE._ACTIVATIONS)
    assert [start for _, start in PRIVATE._ACTIVATIONS] == [0, 5]
