"""E2-S1/E2-S2: shared model vocabulary tests (component plan §3, §5; D7, D15).

Round-trip and invariant coverage for every shared enum, the typed safety
handshake's JSON round trip, and RoastProfile validation (D7).
"""

import json
from enum import Enum

import pydantic
import pytest

from roastpilot_agent.models import (
    BeanProfile,
    BeanProfileInput,
    MicHealth,
    MicStatus,
    RoastCommand,
    RoastDetail,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict

ALL_SHARED_ENUMS: list[type[Enum]] = [
    RoastPhase,
    RoastCommand,
    RoastEventKind,
    RoastEventSource,
    SafetyVerdict,
    MicHealth,
]


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
            default_bean_weight_grams=250.0,
        )
    )
    roast = bean.to_roast_profile(bean_weight_grams=200.0)
    assert isinstance(roast, RoastProfile)
    assert roast.bean_weight_grams == 200.0  # the entered per-roast weight wins
    # The D59 per-bean pre-FC targets carry through to the per-roast profile.
    assert roast.pre_fc_heat == 90
    assert roast.pre_fc_fan == 20
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
