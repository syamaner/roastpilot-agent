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

from roastpilot_agent.config import AppConfig
from roastpilot_agent.config_store import (
    DEFAULT_CONFIG_FILE_PATH,
    AdvisorConfigEdit,
    AdvisorConfigSnapshot,
    AppConfigEdit,
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
    """A YAML file that is a list (not a mapping) raises ValueError."""
    path = tmp_path / "bad.yaml"
    path.write_text("- item1\n- item2\n")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
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
    """A hand-edited saved file with a 'safety' section must NOT weaken limits.

    _inject_saved_as_env skips the 'safety' section so that even a hand-edited
    YAML file cannot silently lower a safety limit (D78 constraint 2).
    """
    monkeypatch.delenv("ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C", raising=False)

    # Simulate a hand-edited saved file with a lowered safety limit.
    saved_raw_with_safety: dict[str, Any] = {
        "safety": {"max_bean_temp_c": 180.0},
        "advisor": {"model_slug": "openai/gpt-4o-mini"},
    }
    _inject_saved_as_env(saved_raw_with_safety)

    # The safety section must NOT have been injected.
    assert "ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C" not in os.environ
    # The non-safety section IS injected.
    assert os.environ.get("ROASTPILOT_ADVISOR__MODEL_SLUG") == "openai/gpt-4o-mini"

    # Clean up the injected var.
    monkeypatch.delenv("ROASTPILOT_ADVISOR__MODEL_SLUG", raising=False)
