"""E2-S1/E2-S2: shared model vocabulary tests (component plan §3, §5; D7, D15).

Round-trip and invariant coverage for every shared enum, the typed safety
handshake's JSON round trip, and RoastProfile validation (D7).
"""

import json
import math
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, cast, get_args

import pydantic
import pytest

from roastpilot_agent.models import (
    DEFAULT_ROAST_STYLE,
    ROAST_STYLE_TARGETS,
    BeanProfile,
    BeanProfileDraft,
    BeanProfileInput,
    CatalogueRecommendation,
    CatalogueRecommendationList,
    MicHealth,
    MicStatus,
    RoastCommand,
    RoastDetail,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastStyle,
    RoastStyleTarget,
    RoastSummary,
    SseEvent,
    SseEventType,
    TastingEntryRequest,
    TelemetryEventData,
    TelemetryPoint,
    TimelineEvent,
    roast_style_target,
    sanitize_non_finite,
    weight_loss_percent,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict

ALL_SHARED_ENUMS: list[type[Enum]] = [
    RoastPhase,
    RoastCommand,
    RoastEventKind,
    RoastEventSource,
    SafetyVerdict,
    MicHealth,
    RoastStyle,
]


def test_sse_event_render_replaces_nested_non_finite_floats() -> None:
    """SSE data stays strict JSON when non-finite floats occur anywhere."""

    def reject_constant(token: str) -> object:
        raise ValueError(f"non-finite JSON token: {token}")

    event = SseEvent(
        event=SseEventType.TELEMETRY,
        data={
            "nan": float("nan"),
            "positive_infinity": float("inf"),
            "negative_infinity": float("-inf"),
            "nested": {"value": float("nan")},
            "items": [{"value": float("inf")}, {"value": float("-inf")}],
        },
    )

    data_line = next(line for line in event.render().splitlines() if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "), parse_constant=reject_constant)

    assert payload == {
        "nan": None,
        "positive_infinity": None,
        "negative_infinity": None,
        "nested": {"value": None},
        "items": [{"value": None}, {"value": None}],
    }


def test_sse_event_render_preserves_finite_wire_output() -> None:
    """Finite payloads keep the renderer's prior byte representation."""
    event = SseEvent(
        event=SseEventType.TELEMETRY,
        data={"bean_temp_c": 200.5, "nested": (1.5, {"value": 2.5})},
        id=9,
    )

    assert event.render() == (
        'id: 9\nevent: telemetry\ndata: {"bean_temp_c": 200.5, "nested": [1.5, {"value": 2.5}]}\n\n'
    )


def test_sse_event_render_does_not_mutate_shared_data() -> None:
    """Rendering leaves the shared fan-out event and nested containers intact."""
    non_finite = float("nan")
    event = SseEvent(
        event=SseEventType.TELEMETRY,
        data={"nested": {"value": non_finite}, "items": [non_finite]},
    )
    original_data = event.data
    original_nested = event.data["nested"]
    original_items = event.data["items"]

    event.render()

    assert event.data is original_data
    assert event.data["nested"] is original_nested
    assert event.data["items"] is original_items
    assert isinstance(original_nested, dict)
    assert isinstance(original_items, list)
    assert original_nested["value"] is non_finite
    assert original_items[0] is non_finite
    assert math.isnan(non_finite)


def test_wire_model_float_fields_are_sanitized_by_reflection() -> None:
    """Every float field in the named wire-model registry becomes strict JSON."""
    model_registry = (
        TelemetryEventData,
        RoastSummary,
        RoastDetail,
        TelemetryPoint,
        TimelineEvent,
    )
    assert {model.__name__ for model in model_registry} == {
        "TelemetryEventData",
        "RoastSummary",
        "RoastDetail",
        "TelemetryPoint",
        "TimelineEvent",
    }

    for model_type in model_registry:
        float_fields = {
            name
            for name, field in model_type.model_fields.items()
            if field.annotation is float or float in get_args(field.annotation)
        }
        assert float_fields, f"{model_type.__name__} has no reflected float fields"
        planted = cast("dict[str, Any]", {name: float("inf") for name in float_fields})
        raw = model_type.model_construct(**planted).model_dump(mode="json")
        sanitized = sanitize_non_finite(raw)

        json.dumps(sanitized, allow_nan=False)
        assert isinstance(sanitized, dict)
        assert all(sanitized[name] is None for name in float_fields)


@pytest.mark.parametrize("enum_type", ALL_SHARED_ENUMS)
def test_round_trip_by_value(enum_type: type[Enum]) -> None:
    """Every member reconstructs from its value (the serialization form)."""
    for member in enum_type:
        assert enum_type(member.value) is member


@pytest.mark.parametrize("enum_type", ALL_SHARED_ENUMS)
def test_members_are_not_str(enum_type: type[Enum]) -> None:
    """D15: plain Enum, never StrEnum — string comparison must not be possible."""
    assert all(not isinstance(member, str) for member in enum_type)


@pytest.mark.parametrize("enum_type", ALL_SHARED_ENUMS)
def test_values_are_unique_snake_case(enum_type: type[Enum]) -> None:
    """Values are the persisted/SSE wire form: unique, lowercase snake_case."""
    values = [member.value for member in enum_type]
    assert len(values) == len(set(values))
    for value in values:
        assert isinstance(value, str)
        assert value
        assert value == value.lower()
        assert " " not in value


def test_event_kinds_match_plan() -> None:
    """The agent event vocabulary from plan §5 (kinds) and §6 (SSE types,
    minus transport-only telemetry/heartbeat)."""
    assert {kind.value for kind in RoastEventKind} == {
        "run_started",
        "phase_changed",
        "charge_guidance",
        "t0_detected",
        "turning_point",
        "drying_end",
        "first_crack",
        "advisory",
        "command_executed",
        "command_failed",
        "safety_alert",
        "fault",
        "recovery_required",
        "recovery_acknowledged",
        "logs_exported",
        "run_completed",
    }


def test_event_sources_match_plan() -> None:
    """roast_events.source vocabulary from plan §5."""
    assert {source.value for source in RoastEventSource} == {
        "controller",
        "mcp",
        "operator",
        "advisor",
        "safety",
    }


def test_safety_evaluation_json_round_trip() -> None:
    """SafetyEvaluation survives a JSON round trip with the verdict typed."""
    evaluation = SafetyEvaluation(
        rule="max_bean_temp",
        verdict=SafetyVerdict.CLAMP,
        adjusted_heat=80,
        adjusted_fan=60,
        reason="max bean temp approached",
    )
    restored = SafetyEvaluation.model_validate_json(evaluation.model_dump_json())
    assert restored == evaluation
    assert restored.verdict is SafetyVerdict.CLAMP


def test_safety_evaluation_round_trip_without_adjusted_command() -> None:
    """Nullable adjusted values (D15) survive the round trip as None."""
    evaluation = SafetyEvaluation(
        rule="restart_recovery", verdict=SafetyVerdict.RECOVERY, reason="restart with active run"
    )
    restored = SafetyEvaluation.model_validate_json(evaluation.model_dump_json())
    assert restored == evaluation
    assert restored.adjusted_heat is None
    assert restored.adjusted_fan is None


def _profile(**overrides: object) -> dict[str, object]:
    """Valid RoastProfile kwargs; override per test case."""
    base: dict[str, object] = {
        "name": "Ethiopia light",
        "bean_origin": "Ethiopia",
        "bean_weight_grams": 250.0,
        "initial_heat_percent": 70,
        "initial_fan_percent": 40,
        "target_drop_temp_c": 205.0,
        "target_development_percent": 20.0,
    }
    base.update(overrides)
    return base


def test_roast_profile_defaults() -> None:
    """D7 defaults: guidance band 170-200 °C, varietal optional."""
    profile = RoastProfile.model_validate(_profile())
    assert profile.charge_guidance_min_c == 170.0
    assert profile.charge_guidance_max_c == 200.0
    assert profile.bean_varietal is None


def test_roast_profile_bean_identity_defaults() -> None:
    """#164 bean-identity fields default to unset / single-origin so a minimal
    profile (the pre-#164 shape) is valid unchanged."""
    profile = RoastProfile.model_validate(_profile())
    assert profile.country is None
    assert profile.farm is None
    assert profile.description is None
    assert profile.bean_species is None
    assert profile.is_blend is False


def test_roast_profile_pre_fc_levers_default_to_none() -> None:
    """D59 / #318: the per-bean deterministic pre-FC heat/fan targets default to
    ``None`` (the controller then falls back to the config levers), so every
    pre-#318 profile and frozen ``profile_json`` is valid unchanged."""
    profile = RoastProfile.model_validate(_profile())
    assert profile.pre_fc_heat is None
    assert profile.pre_fc_fan is None


def test_roast_profile_pre_fc_levers_round_trip() -> None:
    """D59: a profile that specifies the per-bean pre-FC targets round-trips the
    explicit values (the controller then drives them deterministically pre-FC)."""
    profile = RoastProfile.model_validate(_profile(pre_fc_heat=90, pre_fc_fan=20))
    assert profile.pre_fc_heat == 90
    assert profile.pre_fc_fan == 20
    # And survives a JSON serialize/deserialize (the frozen profile_json path).
    assert RoastProfile.model_validate_json(profile.model_dump_json()) == profile


@pytest.mark.parametrize("bad", [101, 200])
def test_roast_profile_pre_fc_levers_reject_above_percent_ceiling(bad: int) -> None:
    """D59: the per-bean pre-FC targets are bounded ``le=100`` like every lever; a
    value above the percent ceiling is rejected at construction (the runtime
    fan-ceiling bound is enforced by the policy — this is the field-level guard)."""
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(pre_fc_heat=bad))
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(pre_fc_fan=bad))


def test_roast_profile_pre_fc_heat_rejects_near_zero_floor() -> None:
    """D59 (#318 follow-up): ``pre_fc_heat`` is bounded ``ge=10`` (not 0) to match
    ``LateMaillardTrim.trim_heat_percent`` and the "no near-zero heat during active
    roasting" invariant — a typo'd near-zero pre-FC heat would stall the roast, so
    it is rejected at construction. ``10`` (the boundary) and ``None`` are valid."""
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(pre_fc_heat=9))
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(pre_fc_heat=0))
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(pre_fc_heat=-1))
    assert RoastProfile.model_validate(_profile(pre_fc_heat=10)).pre_fc_heat == 10
    assert RoastProfile.model_validate(_profile(pre_fc_heat=None)).pre_fc_heat is None


def test_roast_profile_pre_fc_fan_allows_low_and_zero() -> None:
    """D59: ``pre_fc_fan`` keeps the ``ge=0`` floor (asymmetric with ``pre_fc_heat``'s
    ``ge=10``) — a low or zero pre-FC fan is a legitimate airflow choice, unlike a
    near-zero heat that would stall the roast."""
    assert RoastProfile.model_validate(_profile(pre_fc_fan=0)).pre_fc_fan == 0
    assert RoastProfile.model_validate(_profile(pre_fc_fan=9)).pre_fc_fan == 9
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(pre_fc_fan=-1))


def test_roast_profile_bean_identity_populated() -> None:
    """#164 bean-identity fields round-trip a fully-specified single origin."""
    profile = RoastProfile.model_validate(
        _profile(
            country="Ethiopia",
            farm="Gedeb — Worka Sakaro",
            description="Washed; jasmine, bergamot, stone fruit.",
            bean_species="arabica",
            bean_varietal="Heirloom",
            is_blend=False,
        )
    )
    assert profile.country == "Ethiopia"
    assert profile.farm == "Gedeb — Worka Sakaro"
    assert profile.description == "Washed; jasmine, bergamot, stone fruit."
    assert profile.bean_species == "arabica"
    assert profile.is_blend is False


def test_roast_profile_blend_secondaries_in_description() -> None:
    """#164 blend model: ``is_blend`` true with secondaries in ``description`` —
    the primary carries the structured fields, no structured component list."""
    profile = RoastProfile.model_validate(
        _profile(
            country="Brazil",
            bean_species="arabica",
            is_blend=True,
            description="60% Brazil Cerrado + 40% Ethiopia Guji natural.",
        )
    )
    assert profile.is_blend is True
    assert profile.description is not None
    assert "Ethiopia" in profile.description


def test_roast_profile_optional_identity_blank_normalizes_to_none() -> None:
    """Whitespace-only optional identity fields normalize to ``None`` (unset),
    not a validation error — unlike the required ``name`` / ``bean_origin``."""
    profile = RoastProfile.model_validate(_profile(country="   ", farm="", description="  "))
    assert profile.country is None
    assert profile.farm is None
    assert profile.description is None


def test_roast_profile_strips_optional_identity_whitespace() -> None:
    """Surrounding whitespace is stripped from the optional identity fields."""
    profile = RoastProfile.model_validate(
        _profile(country="  Colombia  ", farm="  Finca El Injerto ")
    )
    assert profile.country == "Colombia"
    assert profile.farm == "Finca El Injerto"


@pytest.mark.parametrize("species", ["arabica", "robusta", "liberica", "excelsa"])
def test_roast_profile_bean_species_accepts_known_values(species: str) -> None:
    """All four botanical species literals are accepted."""
    profile = RoastProfile.model_validate(_profile(bean_species=species))
    assert profile.bean_species == species


def test_roast_profile_rejects_unknown_bean_species() -> None:
    """``bean_species`` is a constrained ``Literal`` — an unknown value is
    rejected (proves it is not a free ``str``)."""
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(bean_species="kona"))


def test_roast_profile_old_shape_json_back_compat() -> None:
    """A frozen ``roast_runs.profile_json`` from before #164 (no country / farm /
    description / bean_species / is_blend) still deserializes — completed runs
    are immutable, so this must never break."""
    old_json = json.dumps(
        {
            "name": "Ethiopia light",
            "bean_origin": "Ethiopia",
            "bean_varietal": "Heirloom",
            "bean_weight_grams": 250.0,
            "charge_guidance_min_c": 170.0,
            "charge_guidance_max_c": 200.0,
            "initial_heat_percent": 70,
            "initial_fan_percent": 40,
            "target_drop_temp_c": 205.0,
            "target_development_percent": 20.0,
        }
    )
    profile = RoastProfile.model_validate_json(old_json)
    assert profile.bean_origin == "Ethiopia"
    assert profile.bean_varietal == "Heirloom"
    # The #164 additions take their back-compat defaults.
    assert profile.country is None
    assert profile.farm is None
    assert profile.description is None
    assert profile.bean_species is None
    assert profile.is_blend is False
    # The #291 additions take their back-compat defaults too.
    assert profile.processing is None
    assert profile.altitude_m is None


def test_roast_profile_metadata_defaults() -> None:
    """#291 processing / altitude default to unset so a minimal profile is valid
    unchanged (the pre-#291 shape)."""
    profile = RoastProfile.model_validate(_profile())
    assert profile.processing is None
    assert profile.altitude_m is None


def test_roast_profile_metadata_populated() -> None:
    """#291 processing / altitude round-trip a fully-specified value."""
    profile = RoastProfile.model_validate(_profile(processing="natural", altitude_m=2100))
    assert profile.processing == "natural"
    assert profile.altitude_m == 2100


@pytest.mark.parametrize(
    "processing", ["washed", "natural", "honey", "anaerobic", "wet_hulled", "other"]
)
def test_roast_profile_processing_accepts_known_values(processing: str) -> None:
    """All six processing-method literals are accepted."""
    profile = RoastProfile.model_validate(_profile(processing=processing))
    assert profile.processing == processing


def test_roast_profile_rejects_unknown_processing() -> None:
    """``processing`` is a constrained ``Literal`` — an unknown value is rejected
    (proves it is not a free ``str``)."""
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(processing="carbonic"))


@pytest.mark.parametrize("altitude_m", [-1, 4001])
def test_roast_profile_rejects_out_of_range_altitude(altitude_m: int) -> None:
    """``altitude_m`` is bounded to a sane coffee-growing range (0–4000 m)."""
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(altitude_m=altitude_m))


def test_roast_profile_pre_291_json_back_compat() -> None:
    """A frozen #164-era ``profile_json`` (full bean identity but no #291
    processing / altitude) still deserializes — completed runs are immutable."""
    pre_291_json = json.dumps(
        {
            "name": "Ethiopia light",
            "bean_origin": "Ethiopia",
            "bean_varietal": "Heirloom",
            "country": "Ethiopia",
            "farm": "Gedeb — Worka Sakaro",
            "description": "Washed; jasmine, bergamot.",
            "bean_species": "arabica",
            "is_blend": False,
            "bean_weight_grams": 250.0,
            "charge_guidance_min_c": 170.0,
            "charge_guidance_max_c": 200.0,
            "initial_heat_percent": 70,
            "initial_fan_percent": 40,
            "target_drop_temp_c": 205.0,
            "target_development_percent": 20.0,
        }
    )
    profile = RoastProfile.model_validate_json(pre_291_json)
    assert profile.country == "Ethiopia"
    assert profile.bean_species == "arabica"
    # The #291 additions take their back-compat defaults.
    assert profile.processing is None
    assert profile.altitude_m is None


def test_roast_profile_source_url_defaults_to_none() -> None:
    """#315 source_url defaults to unset so a minimal profile is valid unchanged."""
    profile = RoastProfile.model_validate(_profile())
    assert profile.source_url is None


@pytest.mark.parametrize(
    "url",
    [
        "https://redber.co.uk/products/ethiopia-yirgacheffe-koke",
        "http://example.com/bean?lot=42",
        "https://shop.example.com",
    ],
)
def test_roast_profile_source_url_accepts_well_formed_http(url: str) -> None:
    """#315 source_url round-trips a well-formed http(s) URL unchanged."""
    profile = RoastProfile.model_validate(_profile(source_url=url))
    assert profile.source_url == url


def test_roast_profile_source_url_strips_whitespace() -> None:
    """Surrounding whitespace is stripped from a valid source_url."""
    profile = RoastProfile.model_validate(_profile(source_url="  https://example.com  "))
    assert profile.source_url == "https://example.com"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_roast_profile_source_url_blank_normalizes_to_none(blank: str | None) -> None:
    """A blank / whitespace-only / absent source_url normalizes to ``None`` (unset),
    not a validation error — lenient operator metadata, like the other optionals."""
    profile = RoastProfile.model_validate(_profile(source_url=blank))
    assert profile.source_url is None


@pytest.mark.parametrize(
    "bad_url",
    [
        "not-a-url",
        "ftp://example.com/bean",
        "javascript:alert(1)",
        "example.com",
        "https://",
        # Embedded userinfo — a credential that must never persist / render (#347).
        "https://user:pass@example.com/bean",
        "https://user@example.com/bean",
        # Malformed port — would yield a broken anchor (#347).
        "https://example.com:abc/bean",
        "https://example.com:99999/bean",
    ],
)
def test_roast_profile_rejects_malformed_source_url(bad_url: str) -> None:
    """#315/#347 source_url rejects a non-http(s) / hostless / unparseable value,
    a URL carrying userinfo (credential leak), or a malformed port — so the UI
    never renders a broken anchor and no credential reaches the corpus."""
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(source_url=bad_url))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path",
        "https://example.com:8443/bean?lot=42",
        "http://shop.example.com",
    ],
)
def test_roast_profile_accepts_clean_source_url_with_valid_port(url: str) -> None:
    """#347: a normal http(s) URL — including one with a valid explicit port —
    round-trips unchanged once the userinfo / bad-port guards are in place."""
    profile = RoastProfile.model_validate(_profile(source_url=url))
    assert profile.source_url == url


def test_roast_profile_pre_315_json_back_compat() -> None:
    """A frozen #291-era ``profile_json`` (no #315 source_url) still deserializes —
    completed runs are immutable, so this must never break."""
    pre_315_json = json.dumps(
        {
            "name": "Ethiopia light",
            "bean_origin": "Ethiopia",
            "bean_varietal": "Heirloom",
            "country": "Ethiopia",
            "farm": "Gedeb — Worka Sakaro",
            "description": "Washed; jasmine, bergamot.",
            "bean_species": "arabica",
            "is_blend": False,
            "processing": "washed",
            "altitude_m": 2100,
            "bean_weight_grams": 250.0,
            "charge_guidance_min_c": 170.0,
            "charge_guidance_max_c": 200.0,
            "initial_heat_percent": 70,
            "initial_fan_percent": 40,
            "target_drop_temp_c": 205.0,
            "target_development_percent": 20.0,
        }
    )
    profile = RoastProfile.model_validate_json(pre_315_json)
    assert profile.country == "Ethiopia"
    assert profile.processing == "washed"
    # The #315 addition takes its back-compat default.
    assert profile.source_url is None


def test_roast_detail_enabled_actions_defaults_to_empty() -> None:
    """``enabled_actions`` (E10 option (a), D25) defaults to an empty list when a
    detail is built without it — the API always populates it from the phase, but
    the field is non-optional with an empty default."""
    detail = RoastDetail(
        id="r1",
        agent_phase=RoastPhase.PREHEATING,
        profile=RoastProfile.model_validate(_profile()),
        started_at_utc="2026-06-07T13:00:00Z",
    )
    assert detail.enabled_actions == []


def test_roast_detail_mic_status_defaults_to_none() -> None:
    """``mic_status`` (#197) defaults to None — historical runs have no live
    capture-alive status; only the active run's detail is enriched."""
    detail = RoastDetail(
        id="r1",
        agent_phase=RoastPhase.PREHEATING,
        profile=RoastProfile.model_validate(_profile()),
        started_at_utc="2026-06-07T13:00:00Z",
    )
    assert detail.mic_status is None


@pytest.mark.parametrize(
    ("status", "audio_running", "expected"),
    [
        ("detected", True, MicHealth.OK),
        ("pending", True, MicHealth.OK),
        ("detected", False, MicHealth.IDLE),  # status alive but capture not running
        ("pending", False, MicHealth.IDLE),
        ("faulted", True, MicHealth.ERROR),
        ("faulted", False, MicHealth.ERROR),
        ("unavailable", True, MicHealth.ERROR),
        ("unavailable", False, MicHealth.ERROR),
        ("disabled", False, MicHealth.IDLE),
        ("manual", False, MicHealth.IDLE),
    ],
)
def test_mic_status_health_mapping(status: str, audio_running: bool, expected: MicHealth) -> None:
    """The derived MicHealth follows the documented capture-alive mapping (#197)."""
    mic = MicStatus.from_first_crack_status(
        status=status,  # type: ignore[arg-type]  # parametrized over the Literal values
        audio_running=audio_running,
        queued_window_count=1,
        emitted_window_count=2,
        dropped_window_count=0,
        processed_window_count=2,
        reason=None,
    )
    assert mic.mic_health is expected
    # Capture-alive fields are forwarded verbatim (no per-window work, #33).
    assert mic.fc_status == status
    assert (mic.queued_window_count, mic.emitted_window_count) == (1, 2)


def test_mic_status_overflow_diagnostics_forwarded_and_default_to_zero() -> None:
    """#539: from_first_crack_status forwards the MCP 0.1.13 overflow
    diagnostics trio (coffee-roaster-mcp#190) verbatim when supplied, and
    defaults to 0/0.0/0 when omitted (a pre-0.1.13 caller)."""
    mic = MicStatus.from_first_crack_status(
        status="detected",
        audio_running=True,
        queued_window_count=1,
        emitted_window_count=2,
        dropped_window_count=0,
        processed_window_count=2,
        reason=None,
        overflow_count_last_minute=5,
        estimated_lost_audio_ms_last_minute=210.75,
        total_overflow_count=42,
    )
    assert mic.overflow_count_last_minute == 5
    assert mic.estimated_lost_audio_ms_last_minute == 210.75
    assert mic.total_overflow_count == 42

    defaulted = MicStatus.from_first_crack_status(
        status="detected",
        audio_running=True,
        queued_window_count=1,
        emitted_window_count=2,
        dropped_window_count=0,
        processed_window_count=2,
        reason=None,
    )
    assert defaulted.overflow_count_last_minute == 0
    assert defaulted.estimated_lost_audio_ms_last_minute == 0.0
    assert defaulted.total_overflow_count == 0


def test_mic_status_json_round_trip() -> None:
    """MicStatus serializes by enum value and reconstructs (the SSE/REST wire)."""
    mic = MicStatus.from_first_crack_status(
        status="detected",
        audio_running=True,
        queued_window_count=0,
        emitted_window_count=311,
        dropped_window_count=0,
        processed_window_count=311,
        reason=None,
    )
    raw = json.loads(mic.model_dump_json())
    assert raw["mic_health"] == "ok"
    assert raw["fc_status"] == "detected"
    assert MicStatus.model_validate(raw).mic_health is MicHealth.OK


def test_mic_status_fc_literal_matches_mcp_mirror() -> None:
    """The SPA-facing FC-status Literal mirrors the MCP one byte-for-byte (#197).

    ``models.FirstCrackStatusLiteral`` is hand-mirrored from
    ``mcp_client.FirstCrackRuntimeStatus`` (the modules can't import the other
    way without a cycle); pin them in sync so a future MCP rename can't drift the
    contract silently."""
    from typing import get_args

    from roastpilot_agent.mcp_client import FirstCrackRuntimeStatus
    from roastpilot_agent.models import FirstCrackStatusLiteral

    assert get_args(FirstCrackStatusLiteral) == get_args(FirstCrackRuntimeStatus)


def test_roast_profile_strips_whitespace() -> None:
    profile = RoastProfile.model_validate(_profile(name="  Ethiopia light  "))
    assert profile.name == "Ethiopia light"


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"name": "   "},
        {"bean_origin": ""},
        {"bean_varietal": "   "},
        {"bean_weight_grams": 0},
        {"bean_weight_grams": -50.0},
        {"initial_heat_percent": 101},
        {"initial_heat_percent": -1},
        {"initial_fan_percent": 101},
        {"target_drop_temp_c": 0},
        {"target_development_percent": 0},
        {"target_development_percent": 100},
        {"charge_guidance_min_c": 200.0},  # min == max
        {"charge_guidance_min_c": 210.0},  # min > max
        {"charge_guidance_max_c": 150.0},  # max < default min
    ],
)
def test_roast_profile_rejects_nonsense(overrides: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(**overrides))


# --- #405/D82: roast-style vocabulary (additive, Slice A) ---


def test_roast_style_targets_has_exactly_three_seeded_styles() -> None:
    """ROAST_STYLE_TARGETS carries exactly the three styles with the exact
    corpus-seeded (washed high-grown) values from D82."""
    assert set(ROAST_STYLE_TARGETS) == {RoastStyle.LIGHT, RoastStyle.MEDIUM, RoastStyle.DARK}
    assert ROAST_STYLE_TARGETS[RoastStyle.LIGHT] == RoastStyleTarget(
        drop_temp_c=188.0, dtr_target=15.0
    )
    assert ROAST_STYLE_TARGETS[RoastStyle.MEDIUM] == RoastStyleTarget(
        drop_temp_c=193.0, dtr_target=18.0
    )
    assert ROAST_STYLE_TARGETS[RoastStyle.DARK] == RoastStyleTarget(
        drop_temp_c=196.0, dtr_target=20.0
    )


def test_roast_style_target_helper_and_default() -> None:
    """roast_style_target() resolves the mapping; DEFAULT_ROAST_STYLE is MEDIUM."""
    target = roast_style_target(RoastStyle.MEDIUM)
    assert target.drop_temp_c == 193.0
    assert target.dtr_target == 18.0
    assert DEFAULT_ROAST_STYLE is RoastStyle.MEDIUM


@pytest.mark.parametrize(("bad_drop", "bad_dtr"), [(0, 18.0), (-5.0, 18.0)])
def test_roast_style_target_rejects_non_positive_drop_temp(bad_drop: float, bad_dtr: float) -> None:
    """RoastStyleTarget.drop_temp_c must be > 0."""
    with pytest.raises(pydantic.ValidationError):
        RoastStyleTarget(drop_temp_c=bad_drop, dtr_target=bad_dtr)


@pytest.mark.parametrize("bad_dtr", [0, 100, -1.0, 150.0])
def test_roast_style_target_rejects_out_of_range_dtr(bad_dtr: float) -> None:
    """RoastStyleTarget.dtr_target must be strictly between 0 and 100."""
    with pytest.raises(pydantic.ValidationError):
        RoastStyleTarget(drop_temp_c=193.0, dtr_target=bad_dtr)


def test_roast_profile_roast_style_defaults_to_none() -> None:
    """#405/D82: roast_style defaults to unset so a minimal profile (the
    pre-#405 shape) is valid unchanged."""
    profile = RoastProfile.model_validate(_profile())
    assert profile.roast_style is None


@pytest.mark.parametrize("style", [RoastStyle.LIGHT, RoastStyle.MEDIUM, RoastStyle.DARK])
def test_roast_profile_roast_style_round_trips(style: RoastStyle) -> None:
    """A RoastProfile with an explicit roast_style survives a JSON round trip."""
    profile = RoastProfile.model_validate(_profile(roast_style=style))
    assert profile.roast_style is style
    restored = RoastProfile.model_validate_json(profile.model_dump_json())
    assert restored.roast_style is style


def test_roast_profile_pre_405_json_back_compat() -> None:
    """A frozen ``roast_runs.profile_json`` from before #405 (no roast_style)
    still deserializes with roast_style unset — completed runs are immutable,
    so this must never break."""
    pre_405_json = json.dumps(
        {
            "name": "Ethiopia light",
            "bean_origin": "Ethiopia",
            "bean_varietal": "Heirloom",
            "bean_weight_grams": 250.0,
            "charge_guidance_min_c": 170.0,
            "charge_guidance_max_c": 200.0,
            "initial_heat_percent": 70,
            "initial_fan_percent": 40,
            "target_drop_temp_c": 205.0,
            "target_development_percent": 20.0,
        }
    )
    profile = RoastProfile.model_validate_json(pre_405_json)
    assert profile.bean_origin == "Ethiopia"
    # The #405 addition takes its back-compat default.
    assert profile.roast_style is None


def test_roast_profile_rejects_unknown_roast_style() -> None:
    """roast_style is a constrained Enum — an unknown value is rejected."""
    with pytest.raises(pydantic.ValidationError):
        RoastProfile.model_validate(_profile(roast_style="extra_dark"))


def test_roast_profile_json_round_trip() -> None:
    profile = RoastProfile.model_validate(
        _profile(
            bean_varietal="Heirloom",
            country="Ethiopia",
            farm="Gedeb — Worka Sakaro",
            description="Washed; jasmine, bergamot.",
            bean_species="arabica",
            is_blend=False,
            processing="washed",
            altitude_m=2100,
        )
    )
    restored = RoastProfile.model_validate_json(profile.model_dump_json())
    assert restored == profile


# --- #303: BeanProfile template model ---


def _bean_profile(**overrides: object) -> dict[str, object]:
    """Valid BeanProfile kwargs; override per test case."""
    base: dict[str, object] = {
        "id": "abc123",
        "created_at": "2026-06-21T00:00:00+00:00",
        "updated_at": "2026-06-21T00:00:00+00:00",
        "name": "Ethiopia light",
        "bean_origin": "Ethiopia",
        "default_bean_weight_grams": 250.0,
        "initial_heat_percent": 70,
        "initial_fan_percent": 40,
        "target_drop_temp_c": 205.0,
        "target_development_percent": 20.0,
    }
    base.update(overrides)
    return base


def test_bean_profile_shares_every_roast_profile_field_except_weight() -> None:
    """#303 DRY guarantee: the two models cannot drift — BeanProfile carries
    every RoastProfile field except ``bean_weight_grams``, plus the library-only
    id / timestamps / default weight."""
    roast_fields = set(RoastProfile.model_fields)
    bean_fields = set(BeanProfile.model_fields)
    # RoastProfile's only non-shared field is the per-roast charge weight.
    assert roast_fields - bean_fields == {"bean_weight_grams"}
    # BeanProfile adds exactly the library bookkeeping + the default weight.
    assert bean_fields - roast_fields == {
        "id",
        "created_at",
        "updated_at",
        "default_bean_weight_grams",
    }


def test_bean_profile_input_has_no_server_owned_fields() -> None:
    """#303: the create/update body omits the store-owned id + timestamps."""
    input_fields = set(BeanProfileInput.model_fields)
    assert "id" not in input_fields
    assert "created_at" not in input_fields
    assert "updated_at" not in input_fields
    assert "bean_weight_grams" not in input_fields
    assert "default_bean_weight_grams" in input_fields


def test_bean_profile_validates_and_strips_like_roast_profile() -> None:
    """#303: the shared validators apply — whitespace-only name rejected,
    optional identity blanks normalize to None."""
    with pytest.raises(pydantic.ValidationError):
        BeanProfile.model_validate(_bean_profile(name="   "))
    profile = BeanProfile.model_validate(_bean_profile(farm="  ", description="  "))
    assert profile.farm is None
    assert profile.description is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"default_bean_weight_grams": 0},
        {"default_bean_weight_grams": -10.0},
        {"id": ""},
        {"initial_heat_percent": 101},
        {"charge_guidance_min_c": 210.0},  # min > max
    ],
)
def test_bean_profile_rejects_nonsense(overrides: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        BeanProfile.model_validate(_bean_profile(**overrides))


def test_bean_profile_to_roast_profile_copies_shared_fields() -> None:
    """#303: to_roast_profile builds a RoastProfile from the template + the
    per-roast charge weight, copying every shared field and dropping the
    library bookkeeping."""
    bean = BeanProfile.model_validate(
        _bean_profile(
            country="Ethiopia",
            farm="Koke Washing Station",
            bean_varietal="Dega",
            bean_species="arabica",
            processing="natural",
            altitude_m=1885,
            pre_fc_heat=90,
            pre_fc_fan=20,
            roast_style=RoastStyle.LIGHT,
            default_bean_weight_grams=250.0,
        )
    )
    roast = bean.to_roast_profile(bean_weight_grams=200.0)
    assert isinstance(roast, RoastProfile)
    assert roast.bean_weight_grams == 200.0  # the entered per-roast weight wins
    # The D59 per-bean pre-FC targets carry through to the per-roast profile.
    assert roast.pre_fc_heat == 90
    assert roast.pre_fc_fan == 20
    # #405/D82: roast_style carries through too (a shared field, not excluded).
    assert roast.roast_style is RoastStyle.LIGHT
    # Every shared field copied verbatim.
    for field in set(RoastProfile.model_fields) - {"bean_weight_grams"}:
        assert getattr(roast, field) == getattr(bean, field)


def test_bean_profile_json_round_trip() -> None:
    bean = BeanProfile.model_validate(
        _bean_profile(
            country="Ethiopia",
            processing="natural",
            altitude_m=1885,
        )
    )
    restored = BeanProfile.model_validate_json(bean.model_dump_json())
    assert restored == bean


# --- #573 phase 1: BeanProfileDraft (add-bean-from-URL) ---


def _bean_draft(**overrides: object) -> dict[str, object]:
    base = _bean_profile(
        source_url="https://example.com/products/kenya-aa",
        field_sources={"name": "on_page", "target_development_percent": "origin_estimated"},
        scouting_note="Scouting run — de-risked first-roast targets.",
    )
    base.update(overrides)
    return base


def test_bean_profile_draft_has_no_server_owned_fields() -> None:
    """#573: like BeanProfileInput, a draft carries no id/timestamps — it is
    never persisted directly."""
    draft_fields = set(BeanProfileDraft.model_fields)
    assert "id" not in draft_fields
    assert "created_at" not in draft_fields
    assert "updated_at" not in draft_fields
    assert "bean_weight_grams" not in draft_fields
    assert "default_bean_weight_grams" in draft_fields
    assert "field_sources" in draft_fields
    assert "scouting_note" in draft_fields


def test_bean_profile_draft_shares_bean_profile_input_fields() -> None:
    """#573: a draft is BeanProfileInput's shape PLUS the two draft-only
    fields — so the operator can submit a (possibly edited) draft straight
    to POST /api/bean-profiles by dropping field_sources/scouting_note."""
    input_fields = set(BeanProfileInput.model_fields)
    draft_fields = set(BeanProfileDraft.model_fields)
    assert input_fields <= draft_fields
    assert draft_fields - input_fields == {
        "draft_attempt_id",
        "field_sources",
        "field_evidence",
        "scouting_note",
    }


def test_bean_profile_draft_validates_and_strips_like_bean_profile() -> None:
    """#573: the shared base validators apply — whitespace-only name
    rejected, optional identity blanks normalize to None."""
    with pytest.raises(pydantic.ValidationError):
        BeanProfileDraft.model_validate(_bean_draft(name="   "))
    draft = BeanProfileDraft.model_validate(_bean_draft(farm="  ", description="  "))
    assert draft.farm is None
    assert draft.description is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"default_bean_weight_grams": 0},
        {"default_bean_weight_grams": -10.0},
        {"scouting_note": ""},
        {"charge_guidance_min_c": 210.0},  # min > max
        {"source_url": "not-a-url"},
    ],
)
def test_bean_profile_draft_rejects_nonsense(overrides: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        BeanProfileDraft.model_validate(_bean_draft(**overrides))


def test_bean_profile_draft_field_sources_round_trip() -> None:
    """#573: the per-field provenance map (on_page vs origin_estimated)
    survives a JSON round trip untouched."""
    draft = BeanProfileDraft.model_validate(
        _bean_draft(
            field_sources={
                "name": "on_page",
                "bean_origin": "on_page",
                "altitude_m": "origin_estimated",
                "target_drop_temp_c": "origin_estimated",
            }
        )
    )
    restored = BeanProfileDraft.model_validate_json(draft.model_dump_json())
    assert restored == draft
    assert restored.field_sources["name"] == "on_page"
    assert restored.field_sources["target_drop_temp_c"] == "origin_estimated"


def test_bean_profile_draft_field_sources_rejects_unknown_source() -> None:
    """#573: field_sources is a closed two-value vocabulary — an unknown
    provenance string is rejected, not silently accepted."""
    with pytest.raises(pydantic.ValidationError):
        BeanProfileDraft.model_validate(
            _bean_draft(field_sources={"name": "guessed_by_the_operator"})
        )


def test_bean_profile_draft_field_evidence_defaults_empty() -> None:
    """#627: a draft built without field_evidence (every pre-#627 caller)
    defaults to an empty map, not a required field — back-compat."""
    draft = BeanProfileDraft.model_validate(_bean_draft())
    assert draft.field_evidence == {}


def test_bean_profile_draft_field_evidence_round_trip() -> None:
    """#627: the per-field cited-quote map survives a JSON round trip
    untouched, same convention as field_sources."""
    draft = BeanProfileDraft.model_validate(
        _bean_draft(
            field_evidence={
                "processing": "Fully washed and dried on raised beds.",
                "altitude_m": "Grown at 1,900 masl.",
            }
        )
    )
    restored = BeanProfileDraft.model_validate_json(draft.model_dump_json())
    assert restored == draft
    assert restored.field_evidence["processing"] == "Fully washed and dried on raised beds."
    assert restored.field_evidence["altitude_m"] == "Grown at 1,900 masl."
    # Absent from field_evidence stays absent — no synthesized entry for a
    # field with no captured quote.
    assert "bean_species" not in restored.field_evidence
    assert "is_blend" not in restored.field_evidence


def test_catalogue_recommendation_strips_bidi_controls_from_display_text() -> None:
    recommendation = CatalogueRecommendation(
        candidate_id="candidate-01",
        product_url="https://vendor.example/products/kiambu",
        name="Kiambu \u202eLot",
        country="Ken\u2066ya",
        processing="washed",
        score=1,
        reason_codes=["missing_country"],
        reasons=["Adds Ken\u202eya to the active bean roster."],
    )
    assert recommendation.name == "Kiambu Lot"
    assert recommendation.country == "Kenya"
    assert recommendation.reasons == ["Adds Kenya to the active bean roster."]


def test_catalogue_recommendation_rejects_bidi_controls_in_product_url() -> None:
    with pytest.raises(pydantic.ValidationError, match="unsafe display characters"):
        CatalogueRecommendation(
            candidate_id="candidate-01",
            product_url="https://vendor.example/products/\u202ekiambu",
            name="Kiambu Lot",
            country="Kenya",
            processing="washed",
            score=1,
            reason_codes=["missing_country"],
            reasons=["Adds Kenya to the active bean roster."],
        )


def test_catalogue_recommendation_rejects_unsafe_url_and_oversized_reason() -> None:
    base = {
        "candidate_id": "candidate-01",
        "product_url": "https://vendor.example/products/kiambu",
        "name": "Kiambu Lot",
        "country": "Kenya",
        "processing": "washed",
        "score": 1,
        "reason_codes": ["missing_country"],
        "reasons": ["Adds Kenya to the active bean roster."],
    }
    with pytest.raises(pydantic.ValidationError, match="unsafe display characters"):
        CatalogueRecommendation.model_validate(
            base | {"product_url": r"https://vendor.example/\evil.example/products/a"}
        )
    with pytest.raises(pydantic.ValidationError, match="at most 600 characters"):
        CatalogueRecommendation.model_validate(base | {"reasons": ["x" * 601]})


def test_catalogue_recommendation_list_requires_extracted_subset() -> None:
    with pytest.raises(pydantic.ValidationError, match="extracted_count cannot exceed"):
        CatalogueRecommendationList(recommendations=[], discovered_count=1, extracted_count=2)


@pytest.mark.parametrize(
    ("charge", "roasted", "expected"),
    [
        (250.0, 221.0, 11.6),  # the issue's worked example
        (250.0, 250.0, 0.0),  # no loss
        (1000.0, 850.0, 15.0),
        (250.0, None, None),  # un-weighed
        (0.0, 200.0, None),  # non-positive charge → no denominator
        (250.0, 0.0, None),  # non-positive roasted
        (250.0, -5.0, None),
        (250.0, 300.0, None),  # roasted > charge → tare/scale error
    ],
)
def test_weight_loss_percent(charge: float, roasted: float | None, expected: float | None) -> None:
    """#388: weight loss % = (charge - roasted) / charge * 100, None on bad inputs."""
    assert weight_loss_percent(charge_weight_grams=charge, roasted_weight_grams=roasted) == expected


def test_tasting_entry_tasted_at_none_passes_through() -> None:
    """#522: the operator not supplying a tasting instant stays honestly
    unknown — never defaulted, never rejected."""
    request = TastingEntryRequest(stars=4)
    assert request.tasted_at_utc is None
    # Also cover the field EXPLICITLY passed as None (Pydantic v2 only runs a
    # field_validator when the field is provided, so the omitted-field case
    # above alone does not exercise the validator's None branch).
    assert TastingEntryRequest(stars=4, tasted_at_utc=None).tasted_at_utc is None


def test_tasting_entry_rejects_unparseable_tasted_at() -> None:
    """#522 Codex P2: a malformed tasted_at_utc must fail validation (422 at
    the API boundary) rather than persist verbatim and poison the exact
    degassing-offset corpus signal #522 exists to capture. A "T" separator IS
    present (distinct from the bare-date-rejection test below), so this
    exercises fromisoformat's own parse failure, not the bare-date guard."""
    with pytest.raises(pydantic.ValidationError, match="tasted_at_utc"):
        TastingEntryRequest(stars=3, tasted_at_utc="2026-07-12Tnot-a-time")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Naive (no offset) is assumed already UTC.
        ("2026-07-12T18:00:00", "2026-07-12T18:00:00+00:00"),
        # Already UTC-offset round-trips unchanged (in value; format is pinned).
        ("2026-07-12T18:00:00+00:00", "2026-07-12T18:00:00+00:00"),
        # A non-UTC offset is converted TO UTC, not stored as given.
        ("2026-07-12T20:00:00+02:00", "2026-07-12T18:00:00+00:00"),
        ("2026-07-12T14:00:00-04:00", "2026-07-12T18:00:00+00:00"),
    ],
)
def test_tasting_entry_normalizes_tasted_at_to_utc(raw: str, expected: str) -> None:
    """#522 Codex P2: naive input is assumed UTC (never a guessed local zone);
    offset input is converted to UTC — every stored value is the SAME UTC
    instant regardless of what offset the operator's client happened to send."""
    request = TastingEntryRequest(stars=3, tasted_at_utc=raw)
    assert request.tasted_at_utc == expected


def test_tasting_entry_rejects_bare_date() -> None:
    """#522 Codex P2: a bare date parses as midnight via fromisoformat, but
    silently inventing a midnight instant would shift the degassing offset by
    up to 24h — reject it explicitly rather than accept an under-specified
    instant."""
    with pytest.raises(pydantic.ValidationError, match="time component"):
        TastingEntryRequest(stars=3, tasted_at_utc="2026-07-13")


def test_tasting_entry_dedupes_attributes_and_defects() -> None:
    """#522 Codex P2: a duplicated tag is normalized away (first-occurrence
    order preserved), not rejected — the corpus never double-counts one
    signal from a single entry."""
    request = TastingEntryRequest(
        stars=4,
        attributes=["sweetness", "acidity", "sweetness", "body"],
        defects=["bitter", "bitter", "flat"],
    )
    assert request.attributes == ["sweetness", "acidity", "body"]
    assert request.defects == ["bitter", "flat"]


def test_tasting_entry_dedupe_is_a_noop_on_already_unique_tags() -> None:
    """#522 Codex P2 follow-up: dedup must not reorder or drop already-unique
    tags — a regression here would silently corrupt every ordinary entry."""
    request = TastingEntryRequest(stars=5, attributes=["sweetness", "acidity", "body"])
    assert request.attributes == ["sweetness", "acidity", "body"]


def test_tasting_entry_rejects_materially_future_tasted_at() -> None:
    """#522 Codex round 3: a tasting cannot happen before it happens — a
    tasted_at_utc well beyond any honest clock skew must 422."""
    far_future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    with pytest.raises(pydantic.ValidationError, match="future"):
        TastingEntryRequest(stars=3, tasted_at_utc=far_future)


def test_tasting_entry_accepts_tasted_at_within_clock_skew_tolerance() -> None:
    """#522 Codex round 3: an honest client clock running a little ahead of
    the server's must NOT 422 — only a materially future value should."""
    slightly_ahead = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    request = TastingEntryRequest(stars=3, tasted_at_utc=slightly_ahead)
    assert request.tasted_at_utc is not None


def test_tasting_entry_accepts_tasted_at_at_the_present_instant() -> None:
    """#522 Codex round 3: "now" itself (0 skew) must round-trip cleanly —
    the boundary case just inside the tolerance window, not past it."""
    now = datetime.now(UTC).isoformat()
    request = TastingEntryRequest(stars=4, tasted_at_utc=now)
    assert request.tasted_at_utc is not None
