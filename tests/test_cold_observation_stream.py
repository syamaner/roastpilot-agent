"""Hardware-free tests for the closed cold observation projection and hub (#954 U2).

Every tick is synthetic.  Expected frames and resume rows are authored as literals
from the contract, never computed by the code under test.
"""

# pyright: reportPrivateUsage=false

import json
import logging
import math
import typing

import pytest

from roastpilot_agent import cold_observation_stream as stream
from roastpilot_agent.cold_characterisation import evidence_schema as schema

EPOCH = "0123456789abcdef"
OTHER_EPOCH = "fedcba9876543210"
RUN_CANARY = "20260926T120000Z-runcanary-77"
DIGEST_CANARY = "dead" * 16
SESSION_CANARY = "SESSION-CANARY-77"
DRIVER_CANARY = "DRIVER-CANARY-77"
VENDOR_CANARY = "VENDOR-CANARY-77"
AUDIO_CANARY = "AUDIO-CANARY-77"
EXTRA_CANARY = "EXTRA-CANARY-77"
CANARIES: typing.Final = (
    "runcanary",
    DIGEST_CANARY,
    SESSION_CANARY,
    DRIVER_CANARY,
    VENDOR_CANARY,
    AUDIO_CANARY,
    EXTRA_CANARY,
    "73",
)
UTC = "2026-09-26T12:00:02+00:00"
FIELDS: typing.Final = {
    "schema_version",
    "cold_phase",
    "recorded_at_utc",
    "device_reported",
    "bean_temp_c",
    "env_temp_c",
    "heat_percent",
    "fan_percent",
    "roast_fan_outcome",
    "roast_fan_percent",
    "cooling_on",
}


def make_tick(
    *,
    phase: schema.ColdPhaseKind = schema.ColdPhaseKind.RECORDING_OFF,
    device: bool = True,
    outcome: schema.ColdTickRoastFanOutcome = schema.ColdTickRoastFanOutcome.OBSERVED,
    level: int | None = 40,
    utc: str = UTC,
    bean: float | None = 21.5,
    env: float | None = 22.25,
    heat: int = 0,
    tick: int = 0,
) -> schema.ColdTickRecord:
    """One synthetic, schema-valid tick carrying a canary in every private field."""
    return schema.ColdTickRecord(
        schema_version=1,
        stream="tick",
        run_id=RUN_CANARY,
        phase=phase,
        recorded_at_utc=utc,
        monotonic_seconds=2.0,
        identity_sha256=DIGEST_CANARY,
        tick=tick,
        device=schema.ColdTickDeviceEvidence(
            driver=DRIVER_CANARY,
            connected=True,
            bean_temp_c=bean,
            env_temp_c=env,
            heat_level_percent=heat,
            fan_level_percent=73,
            cooling_on=False,
            raw_vendor_data={VENDOR_CANARY: VENDOR_CANARY},
        )
        if device
        else None,
        roast_fan=schema.ColdTickRoastFanEvidence(outcome=outcome, roast_fan_level_percent=level),
        session=schema.ColdTickSessionEvidence(
            session_id=SESSION_CANARY,
            active=True,
            session_purpose="cold_characterisation",
            phase=schema.ColdTickSessionPhase.PRE_ROAST,
            elapsed_monotonic_seconds=1.0,
        ),
        audio=schema.ColdTickAudioSample(
            mode="audio",
            status="pending",
            detected_at_utc=None,
            detected_monotonic_seconds=None,
            allow_manual_override=False,
            reason=AUDIO_CANARY,
            audio_running=True,
            queued_window_count=0,
            emitted_window_count=1,
            dropped_window_count=0,
            processed_window_count=1,
            mic_peak_dbfs=-30.5,
            mic_rms_dbfs=-42.0,
            overflow_count_last_minute=0,
            estimated_lost_audio_ms_last_minute=0.0,
            total_overflow_count=0,
            max_consecutive_overflow_count=0,
            last_inference_duration_ms=12.5,
            max_inference_duration_ms=20.0,
            inference_overrun_count=0,
        ),
        raw_audio_extra={EXTRA_CANARY: EXTRA_CANARY},
    )


def with_device(tick: schema.ColdTickRecord, **update: object) -> schema.ColdTickRecord:
    """A copy whose device carries constructed (unvalidated) values."""
    assert tick.device is not None
    return tick.model_copy(update={"device": tick.device.model_copy(update=update)})


def drain(subscription: stream.ColdSubscription) -> list[str | None]:
    """Every item currently queued for one subscription."""
    items: list[str | None] = []
    while not subscription.queue.empty():
        items.append(subscription.queue.get_nowait())
    return items


def frame_id(frame: str | None) -> str:
    assert frame is not None
    first = frame.split("\n", 1)[0]
    assert first.startswith("id: ")
    return first.removeprefix("id: ")


def frame_data(frame: str | None) -> dict[str, typing.Any]:
    assert frame is not None
    line = next(part for part in frame.split("\n") if part.startswith("data: "))
    return typing.cast(dict[str, typing.Any], json.loads(line.removeprefix("data: ")))


def published(count: int, **hub_kwargs: typing.Any) -> stream.ColdObservationHub:
    """A hub with a fixed epoch after ``count`` publishes."""
    hub = stream.ColdObservationHub(epoch=EPOCH, **hub_kwargs)
    for index in range(count):
        hub.publish(make_tick(tick=index))
    return hub


# ------------------------------------------------------------------ T-P*


def test_t_p1_closed_key_set_and_no_private_byte(caplog: pytest.LogCaptureFixture) -> None:
    """T-P1: exactly eleven fields; no private canary in any rendered byte or log."""
    caplog.set_level(logging.DEBUG)
    data = stream.project_cold_observation(make_tick())
    assert set(data.model_dump(mode="json")) == FIELDS
    assert set(stream.ColdObservationData.model_fields) == FIELDS
    hub = stream.ColdObservationHub(epoch=EPOCH)
    subscription = hub.subscribe(None)
    assert subscription is not None
    hub.publish(make_tick())
    (frame,) = drain(subscription)
    assert frame is not None
    for canary in CANARIES:
        leaked = (canary in frame, canary in caplog.text)
        assert leaked == (False, False)
    assert set(frame_data(frame)) == FIELDS


def test_t_p2_main_fan_never_leaks() -> None:
    """T-P2: ``fan_level_percent=73`` gives ``fan_percent`` null; 73 appears nowhere."""
    data = stream.project_cold_observation(make_tick())
    assert data.fan_percent is None
    text = stream.render_observation_frame(EPOCH, 1, data)
    assert "73" not in text
    assert frame_data(text)["fan_percent"] is None


@pytest.mark.parametrize(
    ("outcome", "public"),
    [
        (schema.ColdTickRoastFanOutcome.OBSERVED, stream.ColdStreamRoastFanOutcome.OBSERVED),
        (
            schema.ColdTickRoastFanOutcome.NOT_ELIGIBLE,
            stream.ColdStreamRoastFanOutcome.NOT_ELIGIBLE,
        ),
        (schema.ColdTickRoastFanOutcome.UNSUPPORTED, stream.ColdStreamRoastFanOutcome.UNSUPPORTED),
        (schema.ColdTickRoastFanOutcome.UNREADABLE, stream.ColdStreamRoastFanOutcome.UNREADABLE),
        (schema.ColdTickRoastFanOutcome.MALFORMED, stream.ColdStreamRoastFanOutcome.MALFORMED),
    ],
)
def test_t_p3_roast_fan_outcomes_map_by_identity(
    outcome: schema.ColdTickRoastFanOutcome, public: stream.ColdStreamRoastFanOutcome
) -> None:
    """T-P3: each outcome maps by identity; a level only with OBSERVED."""
    observed = outcome is schema.ColdTickRoastFanOutcome.OBSERVED
    data = stream.project_cold_observation(
        make_tick(outcome=outcome, level=0 if observed else None)
    )
    assert data.roast_fan_outcome is public
    assert data.roast_fan_percent == (0 if observed else None)
    if observed:
        assert data.roast_fan_percent == 0 and type(data.roast_fan_percent) is int
        assert stream.project_cold_observation(make_tick(level=100)).roast_fan_percent == 100


def test_t_p3_a_level_beside_a_non_observed_outcome_is_not_published() -> None:
    """T-P3: a constructed level beside a non-observed outcome is never published."""
    tick = make_tick()
    forged = tick.model_copy(
        update={
            "roast_fan": tick.roast_fan.model_copy(
                update={"outcome": schema.ColdTickRoastFanOutcome.UNREADABLE}
            )
        }
    )
    data = stream.project_cold_observation(forged)
    assert data.roast_fan_outcome is stream.ColdStreamRoastFanOutcome.UNREADABLE
    assert data.roast_fan_percent is None
    out_of_range = tick.model_copy(
        update={"roast_fan": tick.roast_fan.model_copy(update={"roast_fan_level_percent": 101})}
    )
    assert stream.project_cold_observation(out_of_range).roast_fan_percent is None


def test_t_p4_non_finite_temperatures_become_null_and_finite_pass_exactly() -> None:
    """T-P4: constructed inf/nan give null; finite values pass exactly; no device, all null."""
    finite = stream.project_cold_observation(make_tick(bean=-0.5, env=231.125))
    assert (finite.bean_temp_c, finite.env_temp_c) == (-0.5, 231.125)
    for value in (math.inf, -math.inf, math.nan):
        data = stream.project_cold_observation(
            with_device(make_tick(), bean_temp_c=value, env_temp_c=value)
        )
        assert data.bean_temp_c is None and data.env_temp_c is None
        assert data.device_reported is True
    absent = stream.project_cold_observation(make_tick(device=False))
    assert absent.device_reported is False
    assert (absent.bean_temp_c, absent.env_temp_c, absent.heat_percent, absent.cooling_on) == (
        None,
        None,
        None,
        None,
    )
    assert absent.fan_percent is None
    nulls = stream.project_cold_observation(make_tick(bean=None, env=None))
    assert nulls.bean_temp_c is None and nulls.env_temp_c is None


def test_t_p5_heat_exactness_bound_and_cooling_type() -> None:
    """T-P5: ``2**53`` gives null; ``2**53 - 1`` passes; a non-bool cooling gives null."""
    assert stream.project_cold_observation(make_tick(heat=2**53)).heat_percent is None
    assert stream.project_cold_observation(make_tick(heat=-(2**53))).heat_percent is None
    assert stream.project_cold_observation(make_tick(heat=2**53 - 1)).heat_percent == 2**53 - 1
    assert stream.project_cold_observation(make_tick(heat=1 - 2**53)).heat_percent == 1 - 2**53
    assert stream.project_cold_observation(make_tick(heat=-7)).heat_percent == -7
    assert (
        stream.project_cold_observation(
            with_device(make_tick(), heat_level_percent=True)
        ).heat_percent
        is None
    )
    assert (
        stream.project_cold_observation(
            with_device(make_tick(), heat_level_percent=5.0)
        ).heat_percent
        is None
    )
    assert (
        stream.project_cold_observation(with_device(make_tick(), cooling_on=1)).cooling_on is None
    )
    assert (
        stream.project_cold_observation(with_device(make_tick(), cooling_on=True)).cooling_on
        is True
    )
    assert stream.project_cold_observation(make_tick()).cooling_on is False


@pytest.mark.parametrize(
    "case", ["bad_utc", "offset_utc", "not_a_tick", "subclass", "phase", "outcome", "broken"]
)
def test_t_p6_refusals_are_fixed_and_carry_no_canary(case: str) -> None:
    """T-P6: an inadmissible UTC, a non-tick or a missing mapping raises the fixed error."""
    tick = make_tick()

    class SubTick(schema.ColdTickRecord):
        pass

    value: object = {
        "bad_utc": tick.model_copy(update={"recorded_at_utc": f"not-a-time-{SESSION_CANARY}"}),
        "offset_utc": tick.model_copy(update={"recorded_at_utc": "2026-09-26T12:00:00+01:00"}),
        "not_a_tick": {"recorded_at_utc": UTC},
        "subclass": SubTick.model_validate(tick.model_dump()),
        "phase": tick.model_copy(update={"phase": schema.ColdTickSessionPhase.FAULT}),
        "outcome": tick.model_copy(
            update={
                "roast_fan": tick.roast_fan.model_copy(
                    update={"outcome": stream.ColdStreamRoastFanOutcome.OBSERVED}
                )
            }
        ),
        "broken": schema.ColdTickRecord.model_construct(),
    }[case]
    with pytest.raises(stream.ColdObservationProjectionError) as raised:
        stream.project_cold_observation(typing.cast(schema.ColdTickRecord, value))
    assert str(raised.value) == "Cold observation projection refused."
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    for canary in CANARIES:
        leaked = canary in str(raised.value)
        assert not leaked


def test_projection_maps_both_phases_by_identity() -> None:
    """Both evidence phases map to the public phase by identity."""
    off = stream.project_cold_observation(make_tick())
    on = stream.project_cold_observation(make_tick(phase=schema.ColdPhaseKind.RECORDING_ON))
    assert off.cold_phase is stream.ColdStreamPhase.RECORDING_OFF
    assert on.cold_phase is stream.ColdStreamPhase.RECORDING_ON


# ------------------------------------------------------------------ T-H*


def test_t_h1_exact_frame_bytes_and_heartbeat() -> None:
    """T-H1: exact observation frame bytes and IDs; the heartbeat is exact and has no ID."""
    hub = stream.ColdObservationHub(epoch=EPOCH)
    subscription = hub.subscribe(None)
    assert subscription is not None
    hub.publish(make_tick())
    hub.publish(
        make_tick(
            phase=schema.ColdPhaseKind.RECORDING_ON,
            device=False,
            level=None,
            outcome=schema.ColdTickRoastFanOutcome.UNSUPPORTED,
        )
    )
    first, second = drain(subscription)
    assert first == (
        "id: 0123456789abcdef-1\nevent: observation\ndata: "
        '{"bean_temp_c":21.5,"cold_phase":"recording_off","cooling_on":false,'
        '"device_reported":true,"env_temp_c":22.25,"fan_percent":null,"heat_percent":0,'
        '"recorded_at_utc":"2026-09-26T12:00:02+00:00","roast_fan_outcome":"observed",'
        '"roast_fan_percent":40,"schema_version":1}\n\n'
    )
    assert second == (
        "id: 0123456789abcdef-2\nevent: observation\ndata: "
        '{"bean_temp_c":null,"cold_phase":"recording_on","cooling_on":null,'
        '"device_reported":false,"env_temp_c":null,"fan_percent":null,"heat_percent":null,'
        '"recorded_at_utc":"2026-09-26T12:00:02+00:00","roast_fan_outcome":"unsupported",'
        '"roast_fan_percent":null,"schema_version":1}\n\n'
    )
    assert stream.HEARTBEAT_FRAME == "event: heartbeat\ndata: {}\n\n"
    assert [kind.value for kind in stream.ColdStreamFrameKind] == ["observation", "heartbeat"]


def test_t_h1_random_epoch_is_sixteen_lower_hex() -> None:
    """The default epoch is 16 lower-case hex digits and differs across hubs."""
    hub = stream.ColdObservationHub()
    subscription = hub.subscribe(None)
    assert subscription is not None
    hub.publish(make_tick())
    epoch, sequence = frame_id(drain(subscription)[0]).split("-")
    assert len(epoch) == 16 and set(epoch) <= set("0123456789abcdef")
    assert sequence == "1"


def test_t_h2_ring_holds_exactly_the_last_frames_as_text() -> None:
    """T-H2: after R+5 publishes the ring holds exactly the last R frames, as ``str``."""
    hub = published(9, queue_frames=8, ring_frames=4)
    assert [held for held, _frame in hub._ring] == [6, 7, 8, 9]
    assert all(type(frame) is str for _held, frame in hub._ring)
    assert [frame_id(frame) for _held, frame in hub._ring] == [
        f"{EPOCH}-6",
        f"{EPOCH}-7",
        f"{EPOCH}-8",
        f"{EPOCH}-9",
    ]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        " ",
        f" {EPOCH}-3",
        f"{EPOCH}-3 ",
        f"{EPOCH.upper()}-3",
        f"{EPOCH}-0",
        f"{EPOCH}-03",
        f"{EPOCH}-" + "1" * 17,
        f"{EPOCH}-٣",
        f"{EPOCH[:15]}é-3",
        f"{EPOCH}-1234567890123456" + "7",
        f"{EPOCH[:15]}-3",
        f"{EPOCH}0-3",
        f"{EPOCH}-3\n",
        f"{EPOCH}--3",
        3,
        b"0123456789abcdef-3",
    ],
    ids=lambda _raw: "malformed",
)
def test_t_h3_malformed_ids_are_a_fresh_connection(raw: object) -> None:
    """T-H3: each malformed ID is ignored (fresh connection: no replay)."""
    accepted = stream.parse_last_event_id(raw) is not None
    assert not accepted
    hub = published(5)
    subscription = hub.subscribe(raw)
    assert subscription is not None
    assert len(drain(subscription)) == 0


def test_t_h3_the_longest_valid_id_is_accepted() -> None:
    """A 33-character ID with a 16-digit sequence is accepted; 34 characters is not."""
    longest = f"{EPOCH}-1234567890123456"
    assert len(longest) == 33
    assert stream.parse_last_event_id(longest) == (EPOCH, 1234567890123456)
    assert stream.parse_last_event_id(longest + "7") is None
    assert stream.parse_last_event_id(f"{EPOCH}-1") == (EPOCH, 1)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        (f"{OTHER_EPOCH}-7", []),
        (f"{EPOCH}-10", []),
        (f"{EPOCH}-11", []),
        (f"{EPOCH}-9", [10]),
        (f"{EPOCH}-7", [8, 9, 10]),
        (f"{EPOCH}-6", [7, 8, 9, 10]),
        (f"{EPOCH}-5", []),
        (f"{EPOCH}-1", []),
    ],
    ids=[
        "absent",
        "foreign_epoch",
        "seq_eq_last",
        "seq_gt_last",
        "last_minus_1",
        "mid",
        "oldest_minus_1",
        "oldest_minus_2",
        "far_gap",
    ],
)
def test_t_h4_resume_rows(raw: str | None, expected: list[int]) -> None:
    """T-H4: every resume row, including the oldest-1 / oldest-2 boundary (ring 7..10)."""
    hub = published(10, queue_frames=8, ring_frames=4)
    assert [held for held, _frame in hub._ring] == [7, 8, 9, 10]
    subscription = hub.subscribe(raw)
    assert subscription is not None
    assert [int(frame_id(frame).split("-")[1]) for frame in drain(subscription)] == expected


def test_t_h4_empty_ring_replays_nothing() -> None:
    """T-H4: with an empty ring no ID replays anything."""
    hub = stream.ColdObservationHub(epoch=EPOCH)
    subscription = hub.subscribe(f"{EPOCH}-1")
    assert subscription is not None
    assert drain(subscription) == []


def test_t_h5_admission_limit_refuses_immediately() -> None:
    """T-H5: full means immediate refusal; admission returns after unsubscribe; idempotent."""
    hub = stream.ColdObservationHub(max_subscribers=2)
    first, second = hub.subscribe(None), hub.subscribe(None)
    assert first is not None and second is not None
    assert hub.subscriber_count == 2
    assert not hub.admission_available()
    assert hub.subscribe(None) is None
    hub.unsubscribe(first)
    hub.unsubscribe(first)
    assert hub.subscriber_count == 1
    assert hub.admission_available()
    third = hub.subscribe(None)
    assert third is not None and hub.subscriber_count == 2


def test_t_h6_overflow_delivers_queued_frames_then_the_sentinel() -> None:
    """T-H6: undrained overflow delivers 1..Q, then the sentinel, then removal; resume Q."""
    hub = stream.ColdObservationHub(epoch=EPOCH, queue_frames=3, ring_frames=3)
    slow = hub.subscribe(None)
    assert slow is not None
    for index in range(4):
        hub.publish(make_tick(tick=index))
    assert hub.subscriber_count == 0
    items = drain(slow)
    assert [frame_id(item) for item in items[:3]] == [f"{EPOCH}-1", f"{EPOCH}-2", f"{EPOCH}-3"]
    assert items[3:] == [None]
    hub.publish(make_tick(tick=4))
    assert drain(slow) == []
    resumed = hub.subscribe(f"{EPOCH}-3")
    assert resumed is not None
    assert [frame_id(item) for item in drain(resumed)] == [f"{EPOCH}-4", f"{EPOCH}-5"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ring_frames": 0},
        {"ring_frames": 5, "queue_frames": 4},
        {"max_subscribers": 0},
        {"epoch": "0123456789ABCDEF"},
        {"epoch": "0123456789abcde"},
        {"epoch": "0123456789abcdef0"},
        {"epoch": 12},
    ],
    ids=lambda _kwargs: "refused",
)
def test_t_h7_constructor_refusals(kwargs: dict[str, typing.Any]) -> None:
    """T-H7: each refused bound or epoch raises a fixed ``ValueError``."""
    with pytest.raises(ValueError) as raised:
        stream.ColdObservationHub(**kwargs)
    assert str(raised.value).startswith("Cold observation hub")


def test_t_h7_the_boundary_bounds_are_admitted() -> None:
    """``ring == queue`` and one subscriber are admitted."""
    hub = stream.ColdObservationHub(max_subscribers=1, queue_frames=1, ring_frames=1)
    assert hub.admission_available()


def test_t_h8_after_close() -> None:
    """T-H8: publish is a no-op, subscribe refuses, existing subscribers get the sentinel."""
    hub = stream.ColdObservationHub(epoch=EPOCH)
    subscription = hub.subscribe(None)
    assert subscription is not None
    hub.publish(make_tick())
    hub.close()
    assert hub.closed
    hub.publish(make_tick(tick=1))
    assert hub.subscribe(None) is None
    assert not hub.admission_available()
    items = drain(subscription)
    assert [frame_id(items[0])] == [f"{EPOCH}-1"] and items[1:] == [None]
    assert hub.subscriber_count == 0
    assert [held for held, _frame in hub._ring] == [1]


@pytest.mark.asyncio
async def test_t_h9_subscribing_before_any_publish_waits_for_the_first() -> None:
    """T-H9: a subscriber yields nothing until the first publish; then the frame."""
    hub = stream.ColdObservationHub(epoch=EPOCH)
    subscription = hub.subscribe(None)
    assert subscription is not None
    assert subscription.queue.empty()
    hub.publish(make_tick())
    frame = await subscription.get()
    assert frame_id(frame) == f"{EPOCH}-1"


def test_publish_projection_error_propagates_and_consumes_no_sequence() -> None:
    """A projection error propagates out of publish (failing the run) with no frame."""
    hub = stream.ColdObservationHub(epoch=EPOCH)
    subscription = hub.subscribe(None)
    assert subscription is not None
    with pytest.raises(stream.ColdObservationProjectionError):
        hub.publish(make_tick().model_copy(update={"recorded_at_utc": "bad"}))
    assert drain(subscription) == [] and len(hub._ring) == 0
    hub.publish(make_tick())
    assert frame_id(drain(subscription)[0]) == f"{EPOCH}-1"
