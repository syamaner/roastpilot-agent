"""Cold observation SSE contract fixture: the Python half of the #954 U3 bridge.

Every frame in ``tests/fixtures/contract/cold_observation_frames.json`` is built
here by the real U2 path: a synthetic, schema-validated retained tick is projected
by :func:`project_cold_observation` and rendered by :func:`render_observation_frame`,
and the heartbeat is the exact :data:`HEARTBEAT_FRAME` bytes.  The SPA half
(``web/src/lib/coldObservation.contract.test.ts``) loads the same file and asserts
the real TypeScript adapter admits every frame.

One case is deliberately **not** a retained tick: ``OBSERVED`` with a ``null``
roast-fan level is refused by the retained grammar, so it is built directly as a
public :class:`ColdObservationData` and tagged ``public_model_only``.

The default-on in-sync test is this fixture's drift gate.  It is regenerated only
with ``REGEN_CONTRACT_FIXTURES=1`` running this file;
``scripts/check_contract_drift.py`` neither generates nor checks it.  All ticks are
synthetic and hardware-free; private tick fields carry canaries that must never
reach the fixture.
"""

import json
import os
import typing
from pathlib import Path

import pytest

from roastpilot_agent.cold_characterisation import evidence_schema as schema
from roastpilot_agent.cold_characterisation.evidence_lifecycle import is_admissible_utc_instant
from roastpilot_agent.cold_observation_stream import (
    HEARTBEAT_FRAME,
    ColdObservationData,
    ColdStreamPhase,
    ColdStreamRoastFanOutcome,
    project_cold_observation,
    render_observation_frame,
)

FIXTURE_PATH: typing.Final = (
    Path(__file__).parent / "fixtures" / "contract" / "cold_observation_frames.json"
)
EPOCH: typing.Final = "0123456789abcdef"
MAX_EXACT_INT: typing.Final = 2**53 - 1

RUN_CANARY: typing.Final = "20260101T000000Z-fixturecanary-41"
DIGEST_CANARY: typing.Final = "beef" * 16
SESSION_CANARY: typing.Final = "FIXTURE-SESSION-CANARY"
DRIVER_CANARY: typing.Final = "FIXTURE-DRIVER-CANARY"
VENDOR_CANARY: typing.Final = "FIXTURE-VENDOR-CANARY"
AUDIO_CANARY: typing.Final = "FIXTURE-AUDIO-CANARY"
EXTRA_CANARY: typing.Final = "FIXTURE-EXTRA-CANARY"
#: The private main-fan level; its digits appear nowhere else in the fixture.
MAIN_FAN_CANARY: typing.Final = 87
PRIVATE_CANARIES: typing.Final = (
    "fixturecanary",
    DIGEST_CANARY,
    SESSION_CANARY,
    DRIVER_CANARY,
    VENDOR_CANARY,
    AUDIO_CANARY,
    EXTRA_CANARY,
    str(MAIN_FAN_CANARY),
)

UTC_MICROS: typing.Final = "2026-01-01T00:00:01.250000+00:00"
UTC_SECONDS: typing.Final = "2026-01-01T00:00:02+00:00"
UTC_ZULU: typing.Final = "2026-01-01T00:00:03Z"

_REGEN_HINT: typing.Final = (
    "committed cold observation fixture is out of sync with the server projection; "
    "regenerate with REGEN_CONTRACT_FIXTURES=1 python -m pytest "
    "tests/test_cold_observation_contract_fixture.py"
)


class TickSpec(typing.NamedTuple):
    """The published-field inputs of one synthetic retained tick case."""

    name: str
    utc: str = UTC_MICROS
    phase: schema.ColdPhaseKind = schema.ColdPhaseKind.RECORDING_OFF
    device: bool = True
    bean: float | None = 21.5
    env: float | None = 22.25
    heat: int = 0
    cooling: bool = False
    outcome: schema.ColdTickRoastFanOutcome = schema.ColdTickRoastFanOutcome.NOT_ELIGIBLE
    level: int | None = None


Outcome = schema.ColdTickRoastFanOutcome

TICK_CASES: typing.Final[tuple[TickSpec, ...]] = (
    TickSpec(
        "canonical_isoformat_micros",
        phase=schema.ColdPhaseKind.RECORDING_ON,
        outcome=Outcome.OBSERVED,
        level=40,
    ),
    TickSpec("utc_without_micros", utc=UTC_SECONDS),
    TickSpec("utc_zulu", utc=UTC_ZULU),
    TickSpec("device_absent", device=False),
    TickSpec("device_temperatures_null", bean=None, env=None),
    TickSpec("heat_max_exact_int", heat=MAX_EXACT_INT),
    TickSpec("heat_min_exact_int", heat=-MAX_EXACT_INT),
    TickSpec("heat_beyond_exact_int_projects_null", heat=MAX_EXACT_INT + 1),
    TickSpec("outcome_observed", outcome=Outcome.OBSERVED, level=55),
    TickSpec("outcome_not_eligible", outcome=Outcome.NOT_ELIGIBLE),
    TickSpec("outcome_unsupported", outcome=Outcome.UNSUPPORTED),
    TickSpec("outcome_unreadable", outcome=Outcome.UNREADABLE),
    TickSpec("outcome_malformed", outcome=Outcome.MALFORMED),
    TickSpec("observed_level_0", outcome=Outcome.OBSERVED, level=0),
    TickSpec("observed_level_100", outcome=Outcome.OBSERVED, level=100),
    TickSpec("cooling_on_true", cooling=True),
    TickSpec("cooling_on_false", cooling=False),
)


def _tick_payload(spec: TickSpec) -> dict[str, object]:
    """One synthetic tick payload carrying a canary in every private field."""
    device: dict[str, object] | None = (
        {
            "driver": DRIVER_CANARY,
            "connected": True,
            "bean_temp_c": spec.bean,
            "env_temp_c": spec.env,
            "heat_level_percent": spec.heat,
            "fan_level_percent": MAIN_FAN_CANARY,
            "cooling_on": spec.cooling,
            "raw_vendor_data": {VENDOR_CANARY: VENDOR_CANARY},
        }
        if spec.device
        else None
    )
    return {
        "schema_version": 1,
        "stream": "tick",
        "run_id": RUN_CANARY,
        "phase": spec.phase,
        "recorded_at_utc": spec.utc,
        "monotonic_seconds": 2.0,
        "identity_sha256": DIGEST_CANARY,
        "tick": 0,
        "device": device,
        "roast_fan": {"outcome": spec.outcome, "roast_fan_level_percent": spec.level},
        "session": {
            "session_id": SESSION_CANARY,
            "active": True,
            "session_purpose": "cold_characterisation",
            "phase": schema.ColdTickSessionPhase.PRE_ROAST,
            "elapsed_monotonic_seconds": 1.0,
        },
        "audio": {
            "mode": "audio",
            "status": "pending",
            "detected_at_utc": None,
            "detected_monotonic_seconds": None,
            "allow_manual_override": False,
            "reason": AUDIO_CANARY,
            "audio_running": True,
            "queued_window_count": 0,
            "emitted_window_count": 1,
            "dropped_window_count": 0,
            "processed_window_count": 1,
            "mic_peak_dbfs": -30.5,
            "mic_rms_dbfs": -42.0,
            "overflow_count_last_minute": 0,
            "estimated_lost_audio_ms_last_minute": 0.0,
            "total_overflow_count": 0,
            "max_consecutive_overflow_count": 0,
            "last_inference_duration_ms": 12.5,
            "max_inference_duration_ms": 20.0,
            "inference_overrun_count": 0,
        },
        "raw_audio_extra": {EXTRA_CANARY: EXTRA_CANARY},
    }


def build_retained_tick(spec: TickSpec) -> schema.ColdTickRecord:
    """Validate one synthetic tick through the retained schema (never constructed raw)."""
    return schema.ColdTickRecord.model_validate(_tick_payload(spec))


def public_model_only_observation() -> ColdObservationData:
    """``OBSERVED`` with a null level: reachable only through the public model."""
    return ColdObservationData(
        schema_version=1,
        cold_phase=ColdStreamPhase.RECORDING_ON,
        recorded_at_utc=UTC_MICROS,
        device_reported=True,
        bean_temp_c=21.5,
        env_temp_c=22.25,
        heat_percent=0,
        fan_percent=None,
        roast_fan_outcome=ColdStreamRoastFanOutcome.OBSERVED,
        roast_fan_percent=None,
        cooling_on=False,
    )


def _parse_frame(frame_text: str) -> tuple[str | None, str, str]:
    """Split one rendered single-data-line SSE frame into (id, event, data)."""
    fields: dict[str, str] = {}
    for line in frame_text.removesuffix("\n\n").split("\n"):
        key, _, value = line.partition(": ")
        fields[key] = value
    return fields.get("id"), fields["event"], fields["data"]


def _entry(name: str, source: str, frame_text: str) -> dict[str, object]:
    frame_id, event, data = _parse_frame(frame_text)
    return {
        "name": name,
        "source": source,
        "frame_text": frame_text,
        "id": frame_id,
        "event": event,
        "data": data,
    }


def build_fixture() -> dict[str, object]:
    """Build the whole fixture document from the real projection and renderer."""
    frames: list[dict[str, object]] = []
    sequence = 0
    for spec in TICK_CASES:
        sequence += 1
        data = project_cold_observation(build_retained_tick(spec))
        frames.append(
            _entry(spec.name, "retained_tick", render_observation_frame(EPOCH, sequence, data))
        )
    sequence += 1
    frames.append(
        _entry(
            "observed_null_level",
            "public_model_only",
            render_observation_frame(EPOCH, sequence, public_model_only_observation()),
        )
    )
    frames.append(_entry("heartbeat", "heartbeat", HEARTBEAT_FRAME))
    return {"epoch": EPOCH, "frames": frames}


def render_fixture() -> str:
    """The exact committed fixture text."""
    return json.dumps(build_fixture(), indent=2, ensure_ascii=False) + "\n"


def test_committed_cold_fixture_is_in_sync_with_server() -> None:
    """Default-on drift gate: committed bytes equal a fresh real build."""
    assert FIXTURE_PATH.read_text(encoding="utf-8") == render_fixture(), _REGEN_HINT


@pytest.mark.skipif(
    not os.environ.get("REGEN_CONTRACT_FIXTURES"),
    reason="fixture-write test: set REGEN_CONTRACT_FIXTURES=1 to regenerate on demand",
)
def test_write_cold_fixture() -> None:
    """Regenerate the committed fixture on demand only."""
    FIXTURE_PATH.write_text(render_fixture(), encoding="utf-8")
    assert FIXTURE_PATH.read_text(encoding="utf-8") == render_fixture()


@pytest.mark.parametrize("spec", TICK_CASES, ids=[spec.name for spec in TICK_CASES])
def test_retained_case_is_admitted_by_schema_and_utc_before_projection(spec: TickSpec) -> None:
    """Each retained case validates through the schema and the public UTC admission."""
    tick = build_retained_tick(spec)
    assert type(tick) is schema.ColdTickRecord
    assert is_admissible_utc_instant(tick.recorded_at_utc) is True
    assert tick.recorded_at_utc == spec.utc
    assert (tick.device is None) is (not spec.device)


def test_zulu_utc_is_admitted_by_both_schema_and_utc_function() -> None:
    """The ``Z`` form is included only because both admissions accept it."""
    assert is_admissible_utc_instant(UTC_ZULU) is True
    assert build_retained_tick(TickSpec("zulu", utc=UTC_ZULU)).recorded_at_utc == UTC_ZULU


def test_public_model_only_case_is_refused_by_retained_grammar() -> None:
    """``OBSERVED`` with a null level is not a retained tick, so it is labelled as such."""
    with pytest.raises(schema.ColdEvidenceError):
        build_retained_tick(TickSpec("observed_null", outcome=Outcome.OBSERVED, level=None))
    sources = {
        entry["name"]: entry["source"]
        for entry in typing.cast(list[dict[str, object]], build_fixture()["frames"])
    }
    assert sources["observed_null_level"] == "public_model_only"
    assert {source for name, source in sources.items() if name != "observed_null_level"} == {
        "retained_tick",
        "heartbeat",
    }


def test_fixture_contains_no_private_tick_value() -> None:
    """No run, digest, session, driver, vendor, audio, extra or main-fan value leaks."""
    text = FIXTURE_PATH.read_text(encoding="utf-8")
    for canary in PRIVATE_CANARIES:
        assert canary not in text


def test_fixture_frames_have_expected_shape() -> None:
    """Observation frames carry ordered opaque IDs; projection rules hold per case."""
    frames = typing.cast(list[dict[str, object]], build_fixture()["frames"])
    by_name = {typing.cast(str, entry["name"]): entry for entry in frames}
    observations = [entry for entry in frames if entry["event"] == "observation"]
    assert [entry["id"] for entry in observations] == [
        f"{EPOCH}-{index}" for index in range(1, len(observations) + 1)
    ]
    heartbeat = by_name["heartbeat"]
    assert (heartbeat["id"], heartbeat["data"]) == (None, "{}")

    def data(name: str) -> dict[str, object]:
        return typing.cast(dict[str, object], json.loads(typing.cast(str, by_name[name]["data"])))

    assert data("heat_beyond_exact_int_projects_null")["heat_percent"] is None
    assert data("heat_max_exact_int")["heat_percent"] == MAX_EXACT_INT
    assert data("heat_min_exact_int")["heat_percent"] == -MAX_EXACT_INT
    absent = data("device_absent")
    assert absent["device_reported"] is False
    assert [absent[key] for key in ("bean_temp_c", "env_temp_c", "heat_percent", "cooling_on")] == [
        None,
        None,
        None,
        None,
    ]
    assert data("utc_zulu")["recorded_at_utc"] == UTC_ZULU
    assert all(data(name)["fan_percent"] is None for name in by_name if name != "heartbeat")
