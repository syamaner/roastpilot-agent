"""Tests for config_store.py: env-overrides-file persistence, per-field metadata,
and the AppConfigEdit / AppConfigSnapshot models (D76/D78, #418).

All tests are hardware-free and use temporary directories for the config file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pydantic
import pytest
from pydantic import BaseModel

from roastpilot_agent.config import AppConfig, SafetyLimits
from roastpilot_agent.config_store import (
    _ALL_SAFETY_ENV_KEYS,  # pyright: ignore[reportPrivateUsage]
    _NEVER_INJECT_NON_SAFETY_KEYS,  # pyright: ignore[reportPrivateUsage]
    _SAFETY_ENV_PREFIX,  # pyright: ignore[reportPrivateUsage]
    DEFAULT_CONFIG_FILE_PATH,
    AdvisorConfigEdit,
    AdvisorConfigSnapshot,
    AppConfigEdit,
    ConfigFileError,
    ControllerConfigEdit,
    ControllerConfigSnapshot,
    LateMaillardTrimEdit,
    PreFirstCrackLeversEdit,
    SafetyLimitsSnapshot,
    _config_file_path,  # pyright: ignore[reportPrivateUsage]
    _inject_saved_as_env,  # pyright: ignore[reportPrivateUsage]
    _load_saved_config,  # pyright: ignore[reportPrivateUsage]
    _write_saved_config,  # pyright: ignore[reportPrivateUsage]
    apply_config_edit,
    build_config_snapshot,
    load_app_config,
    load_saved_raw,
    persist_config_edit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a temp path and point ROASTPILOT_CONFIG_FILE at it."""
    path = tmp_path / "test-config.yaml"
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(path))
    return path


# ---------------------------------------------------------------------------
# _config_file_path
# ---------------------------------------------------------------------------


def test_config_file_path_default_when_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env var → falls back to the default path in the home directory."""
    monkeypatch.delenv("ROASTPILOT_CONFIG_FILE", raising=False)
    assert _config_file_path() == DEFAULT_CONFIG_FILE_PATH


def test_config_file_path_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ROASTPILOT_CONFIG_FILE overrides the default."""
    custom = tmp_path / "custom.yaml"
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(custom))
    assert _config_file_path() == custom


# ---------------------------------------------------------------------------
# _load_saved_config / _write_saved_config
# ---------------------------------------------------------------------------


def test_load_saved_config_absent_returns_empty(tmp_path: Path) -> None:
    """A missing file returns an empty dict without error."""
    result = _load_saved_config(tmp_path / "nonexistent.yaml")
    assert result == {}


def test_load_saved_config_empty_file_returns_empty(tmp_path: Path) -> None:
    """An empty YAML file returns {}."""
    path = tmp_path / "empty.yaml"
    path.write_text("")
    assert _load_saved_config(path) == {}


def test_load_saved_config_round_trips(tmp_path: Path) -> None:
    """Written config round-trips through load."""
    path = tmp_path / "config.yaml"
    data: dict[str, Any] = {"advisor": {"model_slug": "openai/gpt-4o-mini"}}
    _write_saved_config(path, data)
    loaded = _load_saved_config(path)
    assert loaded == data


def test_write_saved_config_creates_parent_dirs(tmp_path: Path) -> None:
    """Nested parent directories are created automatically."""
    path = tmp_path / "deeply" / "nested" / "config.yaml"
    _write_saved_config(path, {"advisor": {"temperature": 0.5}})
    assert path.exists()
    assert _load_saved_config(path) == {"advisor": {"temperature": 0.5}}


def test_load_saved_config_rejects_non_mapping(tmp_path: Path) -> None:
    """A YAML file that is a list (not a mapping) raises ConfigFileError."""
    path = tmp_path / "bad.yaml"
    path.write_text("- item1\n- item2\n")
    with pytest.raises(ConfigFileError, match="must be a YAML mapping"):
        _load_saved_config(path)


def test_load_saved_config_invalid_yaml_raises_config_file_error(tmp_path: Path) -> None:
    """A file with invalid YAML raises ConfigFileError with path + parse reason."""
    path = tmp_path / "broken.yaml"
    # Deliberately malformed YAML (unclosed bracket).
    path.write_text("advisor:\n  model_slug: [unclosed\n")
    with pytest.raises(ConfigFileError, match=str(path)):
        _load_saved_config(path)


# ---------------------------------------------------------------------------
# build_config_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_all_defaults_no_saved_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no saved file and no env vars, every field shows the schema default
    as both saved_value (None) and effective_value (the default)."""
    # Clear any ROASTPILOT_ vars that might bleed in from the test runner.
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_"):
            monkeypatch.delenv(key, raising=False)

    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})

    # advisor.model_slug
    f = snapshot.advisor.model_slug
    assert f.saved_value is None
    assert f.effective_value == "openai/gpt-4o"
    assert f.default == "openai/gpt-4o"
    assert f.env_overridden is False
    assert f.read_only is False

    # safety fields must be read-only
    assert snapshot.safety.max_bean_temp_c.read_only is True
    assert snapshot.safety.bitter_ceiling_temp_c.read_only is True
    assert snapshot.safety.emergency_drop_temp_c.read_only is True
    assert snapshot.safety.min_seconds_between_commands.read_only is True

    # controller tick is read-only
    assert snapshot.controller.tick_interval_seconds.read_only is True

    # api_key_env is read-only and saved_value is always None
    assert snapshot.advisor.api_key_env.read_only is True
    assert snapshot.advisor.api_key_env.saved_value is None


def test_snapshot_saved_value_shown_when_present() -> None:
    """A value in the saved file appears as saved_value and effective_value."""
    saved_raw: dict[str, Any] = {
        "advisor": {"model_slug": "openai/gpt-4o-mini", "temperature": 0.1}
    }
    import os

    old_env: dict[str, str] = {}
    try:
        # Temporarily apply the saved values as if load_app_config() had run.
        for k in list(os.environ):
            if k.startswith("ROASTPILOT_"):
                old_env[k] = os.environ.pop(k)
        os.environ["ROASTPILOT_ADVISOR__MODEL_SLUG"] = "openai/gpt-4o-mini"
        os.environ["ROASTPILOT_ADVISOR__TEMPERATURE"] = "0.1"
        effective = AppConfig()
    finally:
        for k in list(os.environ):
            if k.startswith("ROASTPILOT_"):
                del os.environ[k]
        os.environ.update(old_env)

    snapshot = build_config_snapshot(effective, saved_raw)
    assert snapshot.advisor.model_slug.saved_value == "openai/gpt-4o-mini"
    assert snapshot.advisor.model_slug.effective_value == "openai/gpt-4o-mini"
    assert snapshot.advisor.temperature.saved_value == 0.1


def test_snapshot_env_overridden_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """When an env var overrides a field, env_overridden is True."""
    monkeypatch.setenv("ROASTPILOT_ADVISOR__MODEL_SLUG", "openai/gpt-4o-mini")
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    assert snapshot.advisor.model_slug.env_overridden is True
    assert snapshot.advisor.model_slug.effective_value == "openai/gpt-4o-mini"
    # saved_value stays None (nothing written to the file)
    assert snapshot.advisor.model_slug.saved_value is None


def test_snapshot_safety_saved_values_always_none() -> None:
    """Safety fields never show a saved value (they are show-only / read-only)."""
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {"safety": {"max_bean_temp_c": 230.0}})
    # Even if the YAML has a safety key, saved_value is always None for safety.
    assert snapshot.safety.max_bean_temp_c.saved_value is None
    assert snapshot.safety.max_bean_temp_c.effective_value == 230.0


def test_snapshot_defaults_match_schema() -> None:
    """Snapshot defaults match what AppConfig() produces."""
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    defaults = AppConfig()

    assert snapshot.safety.max_bean_temp_c.default == defaults.safety.max_bean_temp_c
    assert snapshot.safety.bitter_ceiling_temp_c.default == defaults.safety.bitter_ceiling_temp_c
    assert snapshot.advisor.model_slug.default == defaults.advisor.model_slug
    assert (
        snapshot.controller.tick_interval_seconds.default
        == defaults.controller.tick_interval_seconds
    )


def test_snapshot_pre_fc_levers_fields() -> None:
    """Pre-FC lever fields are present and editable."""
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    assert snapshot.controller.pre_fc_heat_target_percent.read_only is False
    assert snapshot.controller.pre_fc_heat_target_percent.effective_value == 100
    assert snapshot.controller.pre_fc_fan_target_percent.read_only is False
    assert snapshot.controller.pre_fc_fan_target_percent.effective_value == 30


def test_snapshot_late_maillard_trim_fields() -> None:
    """Late-Maillard trim fields are present with correct defaults."""
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    assert snapshot.controller.late_maillard_trim_enabled.effective_value is True
    assert snapshot.controller.late_maillard_trim_heat_percent.effective_value == 65
    assert snapshot.controller.late_maillard_trim_adaptive_depth_enabled.effective_value is False
    assert snapshot.controller.late_maillard_trim_k_ror.effective_value == 1.5


def test_snapshot_has_descriptions() -> None:
    """All snapshot fields carry a non-empty description string."""
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})

    def _check_section(section_cls: type[BaseModel], section: Any) -> None:
        for field_name in section_cls.model_fields:
            meta = getattr(section, field_name)
            assert isinstance(meta.description, str) and meta.description, (
                f"{field_name}.description is empty"
            )

    _check_section(ControllerConfigSnapshot, snapshot.controller)
    _check_section(AdvisorConfigSnapshot, snapshot.advisor)
    _check_section(SafetyLimitsSnapshot, snapshot.safety)


# ---------------------------------------------------------------------------
# apply_config_edit
# ---------------------------------------------------------------------------


def test_apply_config_edit_advisor_model_slug() -> None:
    """Editing model_slug updates the advisor section."""
    edit = AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini"))
    result = apply_config_edit(edit, {})
    assert result["advisor"]["model_slug"] == "openai/gpt-4o-mini"


def test_apply_config_edit_merges_not_overwrites() -> None:
    """Existing saved fields not in the edit are preserved."""
    existing: dict[str, Any] = {"advisor": {"temperature": 0.1}}
    edit = AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini"))
    result = apply_config_edit(edit, existing)
    assert result["advisor"]["temperature"] == 0.1
    assert result["advisor"]["model_slug"] == "openai/gpt-4o-mini"


def test_apply_config_edit_pre_fc_levers() -> None:
    """Editing pre-FC levers writes the correct nested structure."""
    edit = AppConfigEdit(
        controller=ControllerConfigEdit(
            pre_first_crack_levers=PreFirstCrackLeversEdit(heat_target_percent=90)
        )
    )
    result = apply_config_edit(edit, {})
    assert result["controller"]["pre_first_crack_levers"]["heat_target_percent"] == 90


def test_apply_config_edit_late_maillard_trim() -> None:
    """Editing trim fields writes the nested structure correctly."""
    edit = AppConfigEdit(
        controller=ControllerConfigEdit(
            pre_first_crack_levers=PreFirstCrackLeversEdit(
                late_maillard_trim=LateMaillardTrimEdit(
                    trim_heat_percent=60,
                    adaptive_depth_enabled=True,
                    k_ror=2.0,
                )
            )
        )
    )
    result = apply_config_edit(edit, {})
    trim = result["controller"]["pre_first_crack_levers"]["late_maillard_trim"]
    assert trim["trim_heat_percent"] == 60
    assert trim["adaptive_depth_enabled"] is True
    assert trim["k_ror"] == 2.0


def test_apply_config_edit_empty_edit_is_noop() -> None:
    """An empty AppConfigEdit does not mutate existing saved config."""
    existing: dict[str, Any] = {"advisor": {"model_slug": "openai/gpt-4o-mini"}}
    result = apply_config_edit(AppConfigEdit(), existing)
    assert result == existing


def test_apply_config_edit_rejects_invalid_value() -> None:
    """An out-of-range value raises ValidationError from the edit model itself."""
    with pytest.raises(pydantic.ValidationError):
        AppConfigEdit(advisor=AdvisorConfigEdit(temperature=5.0))  # > 2.0


def test_apply_config_edit_rejects_invalid_heat_percent() -> None:
    """A heat_target_percent > 100 raises ValidationError."""
    with pytest.raises(pydantic.ValidationError):
        AppConfigEdit(
            controller=ControllerConfigEdit(
                pre_first_crack_levers=PreFirstCrackLeversEdit(heat_target_percent=150)
            )
        )


def test_apply_config_edit_no_safety_fields() -> None:
    """AppConfigEdit has no safety field — the type-system excludes the path."""
    edit = AppConfigEdit()
    assert not hasattr(edit, "safety"), "AppConfigEdit must not expose a safety field"


# ---------------------------------------------------------------------------
# persist_config_edit / load_saved_raw (round-trip)
# ---------------------------------------------------------------------------


def test_persist_and_load_round_trip(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """persist_config_edit writes; load_saved_raw reads back the same data."""
    # Prevent any existing ROASTPILOT_ vars from contaminating the round-trip.
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_") and key != "ROASTPILOT_CONFIG_FILE":
            monkeypatch.delenv(key, raising=False)

    edit = AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini"))
    persist_config_edit(edit)

    saved = load_saved_raw()
    assert saved["advisor"]["model_slug"] == "openai/gpt-4o-mini"


def test_persist_creates_file(config_file: Path) -> None:
    """persist_config_edit creates the file if it doesn't exist."""
    assert not config_file.exists()
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(temperature=0.2)))
    assert config_file.exists()


def test_persist_multiple_edits_accumulate(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two successive persist_config_edit calls accumulate, not overwrite."""
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_") and key != "ROASTPILOT_CONFIG_FILE":
            monkeypatch.delenv(key, raising=False)

    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini")))
    persist_config_edit(
        AppConfigEdit(advisor=AdvisorConfigEdit(prompt_version="c4", temperature=0.1))
    )
    saved = load_saved_raw()
    assert saved["advisor"]["model_slug"] == "openai/gpt-4o-mini"
    assert saved["advisor"]["prompt_version"] == "c4"
    assert saved["advisor"]["temperature"] == 0.1


def test_load_saved_raw_absent_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """load_saved_raw returns {} when the config file doesn't exist."""
    nonexistent = tmp_path / "nonexistent.yaml"
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(nonexistent))
    assert load_saved_raw() == {}


# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------


def test_safety_fields_are_all_read_only() -> None:
    """Every SafetyLimits field in the snapshot has read_only=True (D78-2)."""
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    safety = snapshot.safety
    for field_name in SafetyLimitsSnapshot.model_fields:
        meta = getattr(safety, field_name)
        assert meta.read_only is True, f"safety.{field_name} must be read_only"


def test_safety_fields_show_correct_values() -> None:
    """Safety field effective values match the running SafetyLimits (D35 §3)."""
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    assert snapshot.safety.max_bean_temp_c.effective_value == 230.0
    assert snapshot.safety.bitter_ceiling_temp_c.effective_value == 196.0
    assert snapshot.safety.emergency_drop_temp_c.effective_value == 198.0
    assert snapshot.safety.min_seconds_between_commands.effective_value == 2.0
    # Ordering invariant (D35 §3): bitter < emergency < max_bean
    assert (
        snapshot.safety.bitter_ceiling_temp_c.effective_value
        < snapshot.safety.emergency_drop_temp_c.effective_value
        < snapshot.safety.max_bean_temp_c.effective_value
    )
    # Newly added pre-T0 overrun fields (D78-2 completeness).
    assert snapshot.safety.pre_t0_max_bean_temp_c.effective_value == 200.0
    assert snapshot.safety.overrun_safe_fan_percent.effective_value == 100
    assert snapshot.safety.pre_t0_overrun_severity.effective_value == "recovery"
    # All three are read-only.
    assert snapshot.safety.pre_t0_max_bean_temp_c.read_only is True
    assert snapshot.safety.overrun_safe_fan_percent.read_only is True
    assert snapshot.safety.pre_t0_overrun_severity.read_only is True


def test_temperatures_are_celsius() -> None:
    """All temperature fields in the snapshot carry values in Celsius (sanity check).

    The safety triad values are the established empirical Hottop limits (°C);
    a Fahrenheit interpretation would be absurd (230 F = 110 C, below roasting
    temperature).
    """
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    # All temperature fields must be well above 100 (°C range for roasting).
    assert snapshot.safety.max_bean_temp_c.effective_value > 100
    assert snapshot.safety.bitter_ceiling_temp_c.effective_value > 100
    assert snapshot.safety.emergency_drop_temp_c.effective_value > 100
    assert snapshot.safety.pre_t0_max_bean_temp_c.effective_value > 100


def test_tick_interval_is_read_only() -> None:
    """Controller tick_interval_seconds is read-only (hardware-pinned)."""
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    assert snapshot.controller.tick_interval_seconds.read_only is True
    assert snapshot.controller.tick_interval_seconds.effective_value == 1.0


def test_api_key_env_is_read_only_and_masked() -> None:
    """The API key env var name is read-only; the value is never exposed."""
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    assert snapshot.advisor.api_key_env.read_only is True
    # The saved_value for api_key_env is always None — never written to the file.
    assert snapshot.advisor.api_key_env.saved_value is None
    # effective_value is the env var NAME, not the key contents.
    assert snapshot.advisor.api_key_env.effective_value == "OPENROUTER_API_KEY"


def test_saved_file_safety_section_not_injected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hand-edited saved file with a lowercase 'safety' section must not weaken limits.

    _inject_saved_as_env skips the entire ROASTPILOT_SAFETY__ prefix at the
    section level — field-list-independent, so even fields not in the snapshot
    (pre_t0_max_bean_temp_c, overrun_safe_fan_percent, pre_t0_overrun_severity)
    cannot be injected from the file (D78-2).
    """
    monkeypatch.delenv("ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C", raising=False)
    # setenv+delenv pattern: ensures monkeypatch tracks MODEL_SLUG for cleanup
    # even though it didn't exist before this test.
    monkeypatch.setenv("ROASTPILOT_ADVISOR__MODEL_SLUG", "__sentinel__")
    monkeypatch.delenv("ROASTPILOT_ADVISOR__MODEL_SLUG")

    # Simulate a hand-edited saved file with a lowered safety limit.
    saved_raw_with_safety: dict[str, Any] = {
        "safety": {"max_bean_temp_c": 180.0},
        "advisor": {"model_slug": "openai/gpt-4o-mini"},
    }
    injected = _inject_saved_as_env(saved_raw_with_safety)

    # The safety section must NOT have been injected.
    assert "ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C" not in os.environ
    assert "ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C" not in injected
    # The non-safety section IS injected.
    assert os.environ.get("ROASTPILOT_ADVISOR__MODEL_SLUG") == "openai/gpt-4o-mini"
    assert "ROASTPILOT_ADVISOR__MODEL_SLUG" in injected
    # Teardown: monkeypatch restores MODEL_SLUG to absent (state after delenv above).


def test_saved_file_capital_safety_section_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capitalised 'Safety:' block cannot bypass the prefix-level guard.

    _inject_saved_as_env normalises section names via .upper() so any
    capitalisation of 'safety' maps to the ROASTPILOT_SAFETY__ prefix and is
    blocked as a whole.  Regression test for the BLOCKER 1 casing vector.
    """
    monkeypatch.delenv("ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C", raising=False)

    # Capital-S capitalisation — would bypass a naive `section == "safety"` check.
    injected = _inject_saved_as_env({"Safety": {"max_bean_temp_c": 300.0}})

    assert "ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C" not in os.environ
    assert "ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C" not in injected
    # Effective limit must remain at the schema default (230), not 300.
    effective = AppConfig()
    assert effective.safety.max_bean_temp_c == 230.0


def test_saved_file_allcaps_safety_section_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An all-caps 'SAFETY:' block is also rejected by the prefix-level guard."""
    monkeypatch.delenv("ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C", raising=False)

    injected = _inject_saved_as_env({"SAFETY": {"max_bean_temp_c": 300.0}})

    assert "ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C" not in os.environ
    assert "ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C" not in injected


def test_saved_file_tick_interval_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand-edited 'tick_interval_seconds' in the controller section is not injected.

    controller tick is hardware-pinned and read-only (D78 constraint 2).
    _NEVER_INJECT_NON_SAFETY_KEYS guards individual non-safety read-only fields.
    """
    monkeypatch.delenv("ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS", raising=False)

    injected = _inject_saved_as_env({"controller": {"tick_interval_seconds": 0.1}})

    assert "ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS" not in os.environ
    assert "ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS" not in injected


def test_all_safety_env_keys_covers_all_real_safety_model_fields() -> None:
    """_ALL_SAFETY_ENV_KEYS covers every SafetyLimits.model_fields field (all 10).

    This is the drift-prevention test: if a new SafetyLimits field is added to
    config.py, this test will fail until _ALL_SAFETY_ENV_KEYS is regenerated
    (it is derived from SafetyLimits.model_fields, so it auto-updates).
    The section-level prefix guard blocks all of them regardless; this assertion
    documents that the derivation is correct and complete.
    """
    expected = frozenset(
        f"{_SAFETY_ENV_PREFIX}{name.upper()}" for name in SafetyLimits.model_fields
    )
    assert expected == _ALL_SAFETY_ENV_KEYS
    # Confirm we have all 10 SafetyLimits fields (not just the 7 in the old snapshot).
    assert len(_ALL_SAFETY_ENV_KEYS) == len(SafetyLimits.model_fields)


def test_snapshot_covers_all_safety_model_fields() -> None:
    """SafetyLimitsSnapshot exposes every SafetyLimits.model_fields field (D78-2).

    D78-2 requires GET to SHOW every SafetyLimits value as read-only.  This
    asserts the snapshot field names match the real model field names so a new
    SafetyLimits field can never be silently hidden from the UI.
    """
    snapshot_fields = set(SafetyLimitsSnapshot.model_fields)
    model_fields = set(SafetyLimits.model_fields)
    assert snapshot_fields == model_fields, (
        f"SafetyLimitsSnapshot is missing fields: {model_fields - snapshot_fields}; "
        f"has extra fields: {snapshot_fields - model_fields}"
    )


def test_all_safety_fields_not_injectable_from_capital_safety_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand-edited 'Safety:' block cannot inject ANY of the 10 SafetyLimits fields.

    Direct reproduction of the BLOCKER 2 round-2 finding: previously only the 7
    snapshot fields were guarded; pre_t0_max_bean_temp_c, overrun_safe_fan_percent,
    and pre_t0_overrun_severity were injectable.  The section-prefix guard closes
    all of them regardless of field-list size.
    """
    # Build a Safety: block with EVERY SafetyLimits field set to an adversarial value.
    adversarial_safety: dict[str, Any] = {
        "max_bean_temp_c": 999.0,
        "max_env_temp_c": 999.0,
        "pre_t0_max_bean_temp_c": 999.0,
        "overrun_safe_fan_percent": 0,
        "pre_t0_overrun_severity": "fault",
        "min_seconds_between_commands": 0.001,
        "max_consecutive_mcp_failures": 999,
        "max_consecutive_advisor_failures": 999,
        "bitter_ceiling_temp_c": 10.0,
        "emergency_drop_temp_c": 11.0,
    }

    # Use capital-S to reproduce the casing bypass vector.
    injected = _inject_saved_as_env({"Safety": adversarial_safety})

    # None of the safety env keys must be set.
    for env_key in _ALL_SAFETY_ENV_KEYS:
        assert env_key not in os.environ, f"{env_key} was injected from a 'Safety:' block"
        assert env_key not in injected

    # The effective config must use schema defaults for the three previously-unguarded fields.
    effective = AppConfig()
    assert effective.safety.pre_t0_max_bean_temp_c == 200.0
    assert effective.safety.overrun_safe_fan_percent == 100
    assert effective.safety.pre_t0_overrun_severity == "recovery"
    assert effective.safety.max_bean_temp_c == 230.0


def test_never_inject_non_safety_keys_matches_doc() -> None:
    """_NEVER_INJECT_NON_SAFETY_KEYS contains the controller tick key."""
    assert "ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS" in _NEVER_INJECT_NON_SAFETY_KEYS


# ---------------------------------------------------------------------------
# env_overridden false-positive fix (MEDIUM 3)
# ---------------------------------------------------------------------------


def test_env_overridden_false_for_saved_file_value(
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
) -> None:
    """A value that came from the saved file must NOT show env_overridden=True.

    When _inject_saved_as_env injects a saved value as a ROASTPILOT_* env var
    and load_app_config threads the injected_keys to build_config_snapshot,
    env_overridden must be False for that field — the env var was set ON BEHALF
    of the saved value, not by the operator.
    """
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_") and key != "ROASTPILOT_CONFIG_FILE":
            monkeypatch.delenv(key, raising=False)

    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini")))

    effective, injected_keys = load_app_config()
    saved_raw = load_saved_raw()
    snapshot = build_config_snapshot(effective, saved_raw, injected_keys)

    # model_slug came from the saved file, not from an operator env var.
    assert snapshot.advisor.model_slug.saved_value == "openai/gpt-4o-mini"
    assert snapshot.advisor.model_slug.effective_value == "openai/gpt-4o-mini"
    assert snapshot.advisor.model_slug.env_overridden is False


def test_env_overridden_true_for_real_env_var(
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
) -> None:
    """A value set by the operator via a real ROASTPILOT_* env var is env_overridden."""
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_") and key != "ROASTPILOT_CONFIG_FILE":
            monkeypatch.delenv(key, raising=False)

    # Saved file has model_slug; a real env var also overrides it.
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini")))
    monkeypatch.setenv("ROASTPILOT_ADVISOR__MODEL_SLUG", "openai/gpt-4.1-mini")

    effective, injected_keys = load_app_config()
    saved_raw = load_saved_raw()
    snapshot = build_config_snapshot(effective, saved_raw, injected_keys)

    # The env var won over the saved value.
    assert snapshot.advisor.model_slug.effective_value == "openai/gpt-4.1-mini"
    # env_overridden must be True — the env var is an operator override.
    assert snapshot.advisor.model_slug.env_overridden is True


# ---------------------------------------------------------------------------
# load_app_config integration test (MEDIUM 4)
# ---------------------------------------------------------------------------


def test_load_app_config_integration(
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
) -> None:
    """load_app_config: saved file value reflected; conflicting env var wins (D78-1).

    This is the end-to-end integration test for the env-overrides-file
    resolution: write a saved config, call load_app_config(), verify the
    effective config reflects it, then add a real env var and verify it wins.
    """
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_") and key != "ROASTPILOT_CONFIG_FILE":
            monkeypatch.delenv(key, raising=False)

    # Write a saved config with a non-default advisor model.
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini")))

    # load_app_config() should reflect the saved value.
    effective, injected_keys = load_app_config()
    assert effective.advisor.model_slug == "openai/gpt-4o-mini"
    assert "ROASTPILOT_ADVISOR__MODEL_SLUG" in injected_keys

    # A real env var overrides the saved value (env-overrides-file, D78-1).
    monkeypatch.setenv("ROASTPILOT_ADVISOR__MODEL_SLUG", "openai/gpt-4.1")
    effective2, injected_keys2 = load_app_config()
    assert effective2.advisor.model_slug == "openai/gpt-4.1"
    # The env var was NOT in the injected set (it was already in the environment).
    assert "ROASTPILOT_ADVISOR__MODEL_SLUG" not in injected_keys2


# ---------------------------------------------------------------------------
# base_trim field (MEDIUM 5)
# ---------------------------------------------------------------------------


def test_snapshot_has_base_trim_field() -> None:
    """ControllerConfigSnapshot exposes late_maillard_trim_base_trim (MEDIUM 5)."""
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    # Default base_trim = 65.
    assert snapshot.controller.late_maillard_trim_base_trim.effective_value == 65
    assert snapshot.controller.late_maillard_trim_base_trim.read_only is False
    assert snapshot.controller.late_maillard_trim_base_trim.description


def test_apply_config_edit_base_trim() -> None:
    """Editing base_trim writes the correct nested structure (MEDIUM 5)."""
    edit = AppConfigEdit(
        controller=ControllerConfigEdit(
            pre_first_crack_levers=PreFirstCrackLeversEdit(
                late_maillard_trim=LateMaillardTrimEdit(base_trim=60)
            )
        )
    )
    result = apply_config_edit(edit, {})
    assert result["controller"]["pre_first_crack_levers"]["late_maillard_trim"]["base_trim"] == 60


def test_snapshot_base_trim_from_saved_file() -> None:
    """A base_trim in the saved file appears as saved_value in the snapshot."""
    saved_raw: dict[str, Any] = {
        "controller": {"pre_first_crack_levers": {"late_maillard_trim": {"base_trim": 55}}}
    }
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, saved_raw)
    assert snapshot.controller.late_maillard_trim_base_trim.saved_value == 55


# ---------------------------------------------------------------------------
# Coverage: _inject_section edge cases
# ---------------------------------------------------------------------------


def test_inject_section_non_dict_value_already_in_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an env var is already set, the saved value does NOT override it."""
    monkeypatch.setenv("ROASTPILOT_ADVISOR__TIMEOUT_SECONDS", "30.0")
    # Saved file has a different value.
    injected = _inject_saved_as_env({"advisor": {"timeout_seconds": 99.0}})
    # The existing env var must win.
    assert os.environ["ROASTPILOT_ADVISOR__TIMEOUT_SECONDS"] == "30.0"
    # The key is NOT in the injected set (we did not write it).
    assert "ROASTPILOT_ADVISOR__TIMEOUT_SECONDS" not in injected


def test_inject_saved_as_env_non_dict_section_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A top-level section value that is not a dict is silently skipped."""
    monkeypatch.delenv("ROASTPILOT_ADVISOR__MODEL_SLUG", raising=False)
    # 'advisor' maps to a scalar (invalid YAML structure) — must not crash.
    injected = _inject_saved_as_env({"advisor": "should_be_a_dict"})
    assert "ROASTPILOT_ADVISOR__MODEL_SLUG" not in os.environ
    assert not injected


# ---------------------------------------------------------------------------
# Coverage: apply_config_edit branching
# ---------------------------------------------------------------------------


def test_apply_config_edit_controller_no_levers() -> None:
    """ControllerConfigEdit with no levers set is a no-op for controller section."""
    edit = AppConfigEdit(controller=ControllerConfigEdit())
    result = apply_config_edit(edit, {})
    # An empty controller edit still creates the controller key but no levers.
    assert "controller" in result
    assert "pre_first_crack_levers" not in result.get("controller", {})


def test_apply_config_edit_fan_target_only() -> None:
    """Only fan_target_percent set — heat_target_percent branch not taken."""
    # fan_target_percent must stay at or below fan_ceiling_percent (default 30).
    # Use 20 (below the 30 ceiling) to exercise the fan branch without the
    # heat branch.
    edit = AppConfigEdit(
        controller=ControllerConfigEdit(
            pre_first_crack_levers=PreFirstCrackLeversEdit(fan_target_percent=20)
        )
    )
    result = apply_config_edit(edit, {})
    levers = result["controller"]["pre_first_crack_levers"]
    assert levers["fan_target_percent"] == 20
    assert "heat_target_percent" not in levers


def test_inject_section_nested_dict_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_inject_section recurses into nested dicts (e.g. controller.pre_first_crack_levers).

    Covers the recursive _inject_section call branch in config_store.py.
    Uses monkeypatch.setenv with a sentinel first so monkeypatch tracks the key
    and restores/removes it on teardown — avoids the pytest delenv-when-absent
    limitation (monkeypatch.delenv is a no-op when the key doesn't yet exist).
    """
    key = "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__HEAT_TARGET_PERCENT"
    # Setting a sentinel via monkeypatch ensures monkeypatch TRACKS this key
    # and will DELETE it (restore to absent) if we then delete it, or restore
    # to the sentinel if we don't explicitly delete.  We delete it next so that
    # _inject_saved_as_env sees a clean slate AND monkeypatch will restore-to-absent
    # on teardown.
    monkeypatch.setenv(key, "__sentinel__")
    monkeypatch.delenv(key)

    # Simulate a saved file with a nested controller section.
    injected = _inject_saved_as_env(
        {"controller": {"pre_first_crack_levers": {"heat_target_percent": 90}}}
    )

    assert os.environ.get(key) == "90"
    assert key in injected
    # Teardown: monkeypatch restores "key absent" (the state after delenv above).


def test_inject_section_bool_value_serialised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boolean values are serialised to 'true'/'false' for pydantic-settings coercion.

    Covers the bool branch inside _inject_section in config_store.py.
    Uses the same sentinel-then-delenv pattern as
    test_inject_section_nested_dict_recursion to ensure cleanup.
    """
    bool_env_key = "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__ENABLED"
    monkeypatch.setenv(bool_env_key, "__sentinel__")
    monkeypatch.delenv(bool_env_key)

    injected = _inject_saved_as_env(
        {"controller": {"pre_first_crack_levers": {"late_maillard_trim": {"enabled": False}}}}
    )

    assert os.environ.get(bool_env_key) == "false"
    assert bool_env_key in injected
    # Teardown: monkeypatch restores "key absent" (the state after delenv above).
