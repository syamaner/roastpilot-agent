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

from roastpilot_agent.config import AppConfig, MCPDeviceConfig, SafetyLimits
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
    MCPDeviceConfigEdit,
    PreFirstCrackLeversEdit,
    SafetyLimitsSnapshot,
    _config_file_path,  # pyright: ignore[reportPrivateUsage]
    _inject_saved_as_env,  # pyright: ignore[reportPrivateUsage]
    _load_saved_config,  # pyright: ignore[reportPrivateUsage]
    _make_field_meta,  # pyright: ignore[reportPrivateUsage]
    _merge_device_fields,  # pyright: ignore[reportPrivateUsage]
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


def test_write_saved_config_is_atomic_no_temp_file_left(tmp_path: Path) -> None:
    """_write_saved_config uses an atomic rename and leaves no temp file behind.

    The write goes through a sibling .config-tmp-*.yaml temp file; on success
    that file is renamed over the target, leaving no leftover temp files in the
    parent directory.  Verifies the atomic-write path (claude-review low, PR #425).
    """
    path = tmp_path / "config.yaml"
    _write_saved_config(path, {"advisor": {"model_slug": "openai/gpt-4o"}})

    assert path.exists()
    assert _load_saved_config(path) == {"advisor": {"model_slug": "openai/gpt-4o"}}
    # No .config-tmp-*.yaml sibling must survive a successful write.
    leftover = list(tmp_path.glob(".config-tmp-*.yaml"))
    assert leftover == [], f"temp files leaked after write: {leftover}"


def test_write_saved_config_cleans_temp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_write_saved_config removes the temp file when os.replace raises.

    Simulates a disk/rename error after the temp file is written to verify
    the cleanup path in the except BaseException block (tmp_path is not None).
    """
    import os as _os

    path = tmp_path / "config.yaml"

    call_count = 0

    def _failing_replace(src: str, dst: str) -> None:
        nonlocal call_count
        call_count += 1
        raise OSError("simulated rename failure")

    monkeypatch.setattr(_os, "replace", _failing_replace)

    with pytest.raises(OSError, match="simulated rename failure"):
        _write_saved_config(path, {"advisor": {"model_slug": "openai/gpt-4o"}})

    assert call_count == 1
    # The target must not exist (write failed).
    assert not path.exists()
    # The temp file must have been cleaned up.
    leftover = list(tmp_path.glob(".config-tmp-*.yaml"))
    assert leftover == [], f"temp file not cleaned up after failure: {leftover}"


def test_write_saved_config_cleans_nothing_when_tempfile_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_write_saved_config propagates the error when NamedTemporaryFile itself raises.

    When NamedTemporaryFile raises (e.g. no space left, permissions), tmp_path
    is still None and the except block takes the False branch — nothing to
    clean up.  Verifies the if tmp_path is not None False branch (the branch
    removed from # pragma: no branch by claude-review low, PR #425).
    """
    import tempfile as _tempfile

    path = tmp_path / "config.yaml"

    def _failing_ntf(**_kwargs: object) -> None:
        raise OSError("simulated no space left on device")

    monkeypatch.setattr(_tempfile, "NamedTemporaryFile", _failing_ntf)

    with pytest.raises(OSError, match="simulated no space left"):
        _write_saved_config(path, {"advisor": {"model_slug": "openai/gpt-4o"}})

    # Nothing was written and no temp file was created.
    assert not path.exists()
    leftover = list(tmp_path.glob(".config-tmp-*.yaml"))
    assert leftover == []


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


def test_snapshot_exposes_trim_depth_damping_fields() -> None:
    """ControllerConfigSnapshot exposes the two #412 damping fields (#443).

    Verifies default values, read_only=False (editable), and non-empty
    description strings for both ``late_maillard_trim_trim_depth_deadband_pp``
    and ``late_maillard_trim_trim_depth_slew_pp_per_tick``.
    """
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    deadband = snapshot.controller.late_maillard_trim_trim_depth_deadband_pp
    slew = snapshot.controller.late_maillard_trim_trim_depth_slew_pp_per_tick
    # Defaults match LateMaillardTrim field defaults from config.py (#412).
    assert deadband.effective_value == 2
    assert slew.effective_value == 3
    assert deadband.read_only is False
    assert slew.read_only is False
    assert deadband.description
    assert slew.description


def test_apply_config_edit_trim_depth_damping_fields() -> None:
    """Editing the two #412 damping fields writes them into the nested structure (#443)."""
    edit = AppConfigEdit(
        controller=ControllerConfigEdit(
            pre_first_crack_levers=PreFirstCrackLeversEdit(
                late_maillard_trim=LateMaillardTrimEdit(
                    trim_depth_deadband_pp=4,
                    trim_depth_slew_pp_per_tick=6,
                )
            )
        )
    )
    result = apply_config_edit(edit, {})
    trim = result["controller"]["pre_first_crack_levers"]["late_maillard_trim"]
    assert trim["trim_depth_deadband_pp"] == 4
    assert trim["trim_depth_slew_pp_per_tick"] == 6


def test_apply_config_edit_trim_depth_damping_out_of_range() -> None:
    """LateMaillardTrimEdit rejects out-of-range damping values (#443).

    trim_depth_deadband_pp: ge=0, le=20.
    trim_depth_slew_pp_per_tick: ge=1, le=20.
    """
    with pytest.raises(pydantic.ValidationError):
        LateMaillardTrimEdit(trim_depth_deadband_pp=-1)  # < 0
    with pytest.raises(pydantic.ValidationError):
        LateMaillardTrimEdit(trim_depth_deadband_pp=21)  # > 20
    with pytest.raises(pydantic.ValidationError):
        LateMaillardTrimEdit(trim_depth_slew_pp_per_tick=0)  # < 1
    with pytest.raises(pydantic.ValidationError):
        LateMaillardTrimEdit(trim_depth_slew_pp_per_tick=21)  # > 20


def test_snapshot_trim_depth_damping_from_saved_file() -> None:
    """Saved damping values appear as saved_value in the snapshot (#443)."""
    saved_raw: dict[str, Any] = {
        "controller": {
            "pre_first_crack_levers": {
                "late_maillard_trim": {
                    "trim_depth_deadband_pp": 1,
                    "trim_depth_slew_pp_per_tick": 5,
                }
            }
        }
    }
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, saved_raw)
    assert snapshot.controller.late_maillard_trim_trim_depth_deadband_pp.saved_value == 1
    assert snapshot.controller.late_maillard_trim_trim_depth_slew_pp_per_tick.saved_value == 5


def test_apply_config_edit_deadband_gte_slew_raises_validation_error() -> None:
    """apply_config_edit raises ValidationError when deadband >= slew after merge (#443).

    Both values are individually in-range (ge/le on the Edit model passes), but
    the cross-field LateMaillardTrim validator in config.py rejects deadband >=
    slew because it silently disables adaptive movement after the first tick.
    The _validate_merged_config path constructs AppConfig from the merged dict
    and lets Pydantic surface the violation before any write reaches disk.
    """
    edit = AppConfigEdit(
        controller=ControllerConfigEdit(
            pre_first_crack_levers=PreFirstCrackLeversEdit(
                late_maillard_trim=LateMaillardTrimEdit(
                    trim_depth_deadband_pp=5,  # in [0, 20] — single-field OK
                    trim_depth_slew_pp_per_tick=3,  # in [1, 20] — single-field OK
                    # but 5 >= 3 → cross-field violation
                )
            )
        )
    )
    with pytest.raises(
        pydantic.ValidationError,
        match="trim_depth_deadband_pp must be strictly less",
    ):
        apply_config_edit(edit, {})


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


# ---------------------------------------------------------------------------
# P2: api_key_env must never be injected from the saved file
# ---------------------------------------------------------------------------


def test_api_key_env_not_injected_from_saved_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """advisor.api_key_env is listed in _NEVER_INJECT_NON_SAFETY_KEYS and must not
    be injected even when present in the saved-config file.

    Injecting api_key_env would silently redirect where the live advisor reads
    the API-key — a security-adjacent misconfiguration (Codex P2 finding, PR #425).
    """
    env_key = "ROASTPILOT_ADVISOR__API_KEY_ENV"
    # Confirm the guard is present in the constant.
    assert env_key in _NEVER_INJECT_NON_SAFETY_KEYS, (
        f"{env_key!r} is missing from _NEVER_INJECT_NON_SAFETY_KEYS — "
        "the guard against api_key_env injection was removed"
    )
    # Make sure the env var is not already set so we can observe injection.
    monkeypatch.setenv(env_key, "__sentinel__")
    monkeypatch.delenv(env_key)

    injected = _inject_saved_as_env({"advisor": {"api_key_env": "ATTACKER_KEY"}})

    assert env_key not in injected, "api_key_env must not appear in the injected set"
    assert os.environ.get(env_key) is None, "api_key_env must not be written to os.environ"


# ---------------------------------------------------------------------------
# P1: load_app_config is idempotent — no cross-request env pollution
# ---------------------------------------------------------------------------


def test_load_app_config_does_not_pollute_os_environ(
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
) -> None:
    """load_app_config() restores os.environ after building AppConfig.

    Before the fix, _inject_saved_as_env permanently wrote saved values into
    os.environ so a second call would see them as real env vars and report
    env_overridden=True for fields that were never set by the operator
    (cross-request env pollution, Codex P1 finding, PR #425).

    After the fix, the env snapshot/restore in load_app_config() means that
    after the call the injected keys are gone — verified here by checking that
    the call leaves no extra ROASTPILOT_ keys behind.
    """
    # Clear all ROASTPILOT_* keys except ROASTPILOT_CONFIG_FILE (set by fixture).
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_") and key != "ROASTPILOT_CONFIG_FILE":
            monkeypatch.delenv(key, raising=False)

    # Write a saved config with a recognisable value.
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini")))

    env_before = {k: v for k, v in os.environ.items() if k.startswith("ROASTPILOT_")}
    load_app_config()
    env_after = {k: v for k, v in os.environ.items() if k.startswith("ROASTPILOT_")}

    assert env_before == env_after, (
        "load_app_config() must not leave ROASTPILOT_* keys in os.environ; "
        f"unexpected additions: {set(env_after) - set(env_before)}"
    )


def test_load_app_config_sequential_calls_no_stale_env(
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
) -> None:
    """Two sequential load_app_config() calls each return env_overridden=False
    for a field that was never set as a real env var.

    Without the snapshot/restore fix, the injected key from the first call
    would remain in os.environ so the second call would classify it as
    env_overridden=True — incorrect.
    """
    from roastpilot_agent.config_store import build_config_snapshot

    for key in list(os.environ):
        if key.startswith("ROASTPILOT_") and key != "ROASTPILOT_CONFIG_FILE":
            monkeypatch.delenv(key, raising=False)

    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini")))

    # First call.
    effective1, injected1 = load_app_config()
    saved1 = load_saved_raw()
    snap1 = build_config_snapshot(effective1, saved1, injected1)
    assert snap1.advisor.model_slug.env_overridden is False

    # Second call — must produce the same result (no stale env keys).
    effective2, injected2 = load_app_config()
    saved2 = load_saved_raw()
    snap2 = build_config_snapshot(effective2, saved2, injected2)
    assert snap2.advisor.model_slug.env_overridden is False
    assert snap2.advisor.model_slug.effective_value == "openai/gpt-4o-mini"


# ---------------------------------------------------------------------------
# P1: persist_config_edit creates parent directory on first run
# ---------------------------------------------------------------------------


def test_persist_config_edit_creates_parent_dir_on_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """persist_config_edit creates the parent directory when it does not exist.

    On a fresh install ~/.roastpilot/ does not exist.  filelock raises OSError
    when creating a .lock file whose parent directory is absent.  The fix adds
    path.parent.mkdir(parents=True, exist_ok=True) before FileLock construction
    (Codex P1 finding, PR #425).
    """
    nested_cfg = tmp_path / "subdir" / "another" / "config.yaml"
    # Verify the parent does NOT exist yet.
    assert not nested_cfg.parent.exists()
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(nested_cfg))

    # This must not raise — the call creates the parent dir.
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o")))

    assert nested_cfg.exists(), "config.yaml must have been written after directory creation"
    import yaml

    saved = yaml.safe_load(nested_cfg.read_text(encoding="utf-8"))
    assert saved["advisor"]["model_slug"] == "openai/gpt-4o"


# ---------------------------------------------------------------------------
# mcp_device tri-state inherit/override (#439)
# ---------------------------------------------------------------------------


def test_merge_device_fields_skip_absent_from_put_body() -> None:
    """Fields absent from model_fields_set are skipped — the saved section is
    unchanged (neither written nor cleared)."""
    target: dict[str, Any] = {"serial_port": "/dev/ttyUSB0"}
    # Only "audio_input_device" was explicitly set in the PUT body.
    _merge_device_fields(
        target,
        {"serial_port": "/dev/ttyUSB1", "audio_input_device": "USB PnP"},
        {"audio_input_device"},
    )
    # serial_port is absent from explicitly_set → unchanged.
    assert target["serial_port"] == "/dev/ttyUSB0"
    # audio_input_device is explicitly set and non-None → written.
    assert target["audio_input_device"] == "USB PnP"


def test_merge_device_fields_explicit_null_clears_saved_key() -> None:
    """Explicit null (field in model_fields_set, value None) deletes the key from
    the saved section — the operator is clearing the override back to inherit."""
    target: dict[str, Any] = {"serial_port": "/dev/ttyUSB0", "roaster_driver": "mock"}
    _merge_device_fields(
        target,
        {"serial_port": None, "roaster_driver": "mock"},
        {"serial_port"},  # serial_port explicitly set to null
    )
    # serial_port removed — back to inherit.
    assert "serial_port" not in target
    # roaster_driver not in explicitly_set → untouched.
    assert target["roaster_driver"] == "mock"


def test_merge_device_fields_explicit_null_on_absent_key_is_noop() -> None:
    """Clearing a key that was not in the saved section is a no-op (no KeyError)."""
    target: dict[str, Any] = {}
    _merge_device_fields(target, {"serial_port": None}, {"serial_port"})
    assert target == {}


def test_merge_device_fields_non_none_value_writes() -> None:
    """Explicitly set non-None value overwrites any existing saved key."""
    target: dict[str, Any] = {"serial_port": "/dev/ttyUSB0"}
    _merge_device_fields(
        target,
        {"serial_port": "/dev/ttyUSB1"},
        {"serial_port"},
    )
    assert target["serial_port"] == "/dev/ttyUSB1"


def test_apply_config_edit_mcp_device_set_override() -> None:
    """Setting an mcp_device field writes it into the saved section."""
    edit = AppConfigEdit(mcp_device=MCPDeviceConfigEdit(serial_port="/dev/ttyUSB0"))
    result = apply_config_edit(edit, {})
    assert result["mcp_device"]["serial_port"] == "/dev/ttyUSB0"


def test_apply_config_edit_mcp_device_clear_back_to_inherit() -> None:
    """Explicitly setting an mcp_device field to null removes it from the saved
    section — the hand-authored MCP yaml governs the field on the next spawn."""
    existing: dict[str, Any] = {"mcp_device": {"serial_port": "/dev/ttyUSB0"}}
    # Build the edit with serial_port explicitly set to None (clear).
    edit = AppConfigEdit(mcp_device=MCPDeviceConfigEdit.model_validate({"serial_port": None}))
    result = apply_config_edit(edit, existing)
    # Key must be deleted — not left as null in the YAML.
    assert "serial_port" not in result.get("mcp_device", {})


def test_apply_config_edit_mcp_device_unset_field_is_unchanged() -> None:
    """An mcp_device field absent from the PUT body leaves the saved value intact."""
    existing: dict[str, Any] = {
        "mcp_device": {"serial_port": "/dev/ttyUSB0", "roaster_driver": "mock"}
    }
    # Only fc_mode is explicitly set; serial_port and roaster_driver are not in body.
    edit = AppConfigEdit(mcp_device=MCPDeviceConfigEdit(fc_mode="audio"))
    result = apply_config_edit(edit, existing)
    # Untouched fields remain.
    assert result["mcp_device"]["serial_port"] == "/dev/ttyUSB0"
    assert result["mcp_device"]["roaster_driver"] == "mock"
    assert result["mcp_device"]["fc_mode"] == "audio"


def test_apply_config_edit_blank_roaster_driver_treated_as_inherit() -> None:
    """A blank roaster_driver string is treated as null — it must NOT write
    ``driver: ''`` into the MCP yaml as that would crash the next spawn."""
    existing: dict[str, Any] = {"mcp_device": {"roaster_driver": "hottop_kn8828b_2k_plus"}}
    # Simulate the FE sending "" for roaster_driver (operator cleared the text field).
    edit = AppConfigEdit(mcp_device=MCPDeviceConfigEdit.model_validate({"roaster_driver": ""}))
    result = apply_config_edit(edit, existing)
    # Blank string → null → cleared (inherit from yaml).
    assert "roaster_driver" not in result.get("mcp_device", {})


def test_apply_config_edit_mcp_device_boolean_round_trip() -> None:
    """Boolean mcp_device fields can be set to True/False/None (tri-state round-trip).

    - Set True → written.
    - Clear to None (inherit) → deleted from saved section.
    """
    # Step 1: set recording_enabled = True.
    edit1 = AppConfigEdit(mcp_device=MCPDeviceConfigEdit(recording_enabled=True))
    saved1 = apply_config_edit(edit1, {})
    assert saved1["mcp_device"]["recording_enabled"] is True

    # Step 2: clear back to inherit (explicit None).
    edit2 = AppConfigEdit(
        mcp_device=MCPDeviceConfigEdit.model_validate({"recording_enabled": None})
    )
    saved2 = apply_config_edit(edit2, saved1)
    assert "recording_enabled" not in saved2.get("mcp_device", {})


def test_apply_config_edit_mcp_device_recording_devices_clear_to_inherit() -> None:
    """recording_devices explicit-null clears the saved key (clear to inherit).

    The backend stores recording_devices as a list in the YAML.  When the
    operator explicitly sets it to null in the PUT body, the saved key must
    be deleted so the hand-authored MCP yaml governs it on the next spawn.
    """
    existing: dict[str, Any] = {
        "mcp_device": {"recording_devices": ["USB PnP Sound Device", "ATR2100x-USB"]}
    }
    # Simulate the FE sending null for recording_devices (clear to inherit).
    edit = AppConfigEdit(mcp_device=MCPDeviceConfigEdit.model_validate({"recording_devices": None}))
    result = apply_config_edit(edit, existing)
    # Key must be deleted — inherit from hand-authored MCP yaml.
    assert "recording_devices" not in result.get("mcp_device", {})


def test_apply_config_edit_blank_serial_port_treated_as_inherit() -> None:
    """A blank serial_port string must NOT write port:'' to the MCP yaml.

    An empty string is not a valid serial port path; the blank-string guard
    must convert it to None so the key is deleted (inherit from yaml) (#439
    review fix — extends the guard to all three string device fields).
    """
    existing: dict[str, Any] = {"mcp_device": {"serial_port": "/dev/ttyUSB0"}}
    edit = AppConfigEdit(mcp_device=MCPDeviceConfigEdit.model_validate({"serial_port": ""}))
    result = apply_config_edit(edit, existing)
    assert "serial_port" not in result.get("mcp_device", {})


def test_apply_config_edit_blank_audio_input_device_treated_as_inherit() -> None:
    """A blank audio_input_device string must NOT write input_device:'' to the MCP yaml.

    An empty string is not a valid audio device name; the blank-string guard
    must convert it to None so the key is deleted (inherit from yaml) (#439
    review fix — extends the guard to all three string device fields).
    """
    existing: dict[str, Any] = {"mcp_device": {"audio_input_device": "USB PnP Sound Device"}}
    edit = AppConfigEdit(mcp_device=MCPDeviceConfigEdit.model_validate({"audio_input_device": ""}))
    result = apply_config_edit(edit, existing)
    assert "audio_input_device" not in result.get("mcp_device", {})


# ---------------------------------------------------------------------------
# #426 — JSON-blob section env var precedence + env_overridden flag
# ---------------------------------------------------------------------------


def test_json_blob_env_var_beats_saved_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A top-level JSON-blob env var for a section (e.g. ROASTPILOT_ADVISOR='{"model_slug":"..."}')
    takes precedence over saved-file values for the fields it sets (#426).

    Before the fix, _inject_saved_as_env injected ROASTPILOT_ADVISOR__MODEL_SLUG
    from the saved file, which pydantic-settings used as the effective value even
    when ROASTPILOT_ADVISOR='{"model_slug":"json-blob-model"}' was also set,
    because scalar nested env vars win over the JSON blob in pydantic-settings'
    resolution order.  The fix skips injection for the whole section when a
    section-level JSON blob key is already set in the environment.
    """
    import json

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("advisor:\n  model_slug: saved-file-model\n", encoding="utf-8")
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv("ROASTPILOT_ADVISOR", json.dumps({"model_slug": "json-blob-model"}))

    effective, injected = load_app_config()

    # JSON blob must win over the saved file.
    assert effective.advisor.model_slug == "json-blob-model"
    # No ROASTPILOT_ADVISOR__* keys should have been injected for this section.
    assert not any("ROASTPILOT_ADVISOR__" in k for k in injected)


def test_json_blob_env_var_sets_env_overridden_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fields set by a top-level JSON-blob env var report env_overridden=True (#426).

    Before the fix, env_overridden was False for these fields because the check
    only looked for the per-field scalar env var (ROASTPILOT_ADVISOR__MODEL_SLUG),
    which is absent from os.environ when a JSON blob covers the section.
    """
    import json

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("advisor:\n  model_slug: saved-file-model\n", encoding="utf-8")
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv(
        "ROASTPILOT_ADVISOR",
        json.dumps({"model_slug": "json-blob-model", "temperature": 0.7}),
    )

    effective, injected = load_app_config()
    saved_raw = _load_saved_config(cfg_file)
    snap = build_config_snapshot(effective, saved_raw, injected)

    # Both fields set in the JSON blob must be flagged env_overridden.
    assert snap.advisor.model_slug.env_overridden is True
    assert snap.advisor.temperature.env_overridden is True
    # saved_value is still the file value (the badge shows what's saved vs effective).
    assert snap.advisor.model_slug.saved_value == "saved-file-model"
    assert snap.advisor.model_slug.effective_value == "json-blob-model"


def test_scalar_env_var_precedence_unaffected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scalar env vars (ROASTPILOT_ADVISOR__MODEL_SLUG) still win over the saved file
    and still report env_overridden=True — the JSON-blob fix must not regress them.
    Fields NOT shadowed by any env var retain env_overridden=False.
    """
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "advisor:\n  model_slug: saved-model\n  temperature: 0.3\n", encoding="utf-8"
    )
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv("ROASTPILOT_ADVISOR__MODEL_SLUG", "scalar-env-model")

    effective, injected = load_app_config()
    saved_raw = _load_saved_config(cfg_file)
    snap = build_config_snapshot(effective, saved_raw, injected)

    # Scalar env var wins for model_slug.
    assert effective.advisor.model_slug == "scalar-env-model"
    assert snap.advisor.model_slug.env_overridden is True

    # temperature comes from the saved file (injected) — not env_overridden.
    assert effective.advisor.temperature == 0.3
    assert snap.advisor.temperature.env_overridden is False


def test_json_blob_env_var_does_not_affect_other_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON-blob env var for ROASTPILOT_ADVISOR only skips advisor injection;
    other sections (e.g. controller) continue to be injected from the saved file.
    """
    import json

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "advisor:\n  model_slug: saved-advisor-model\n"
        "controller:\n  pre_first_crack_levers:\n    heat_target_percent: 80\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv("ROASTPILOT_ADVISOR", json.dumps({"model_slug": "json-blob-model"}))

    effective, injected = load_app_config()
    saved_raw = _load_saved_config(cfg_file)
    snap = build_config_snapshot(effective, saved_raw, injected)

    # Advisor: JSON blob governs.
    assert effective.advisor.model_slug == "json-blob-model"
    assert snap.advisor.model_slug.env_overridden is True

    # Controller: saved-file injection still works.
    assert effective.controller.pre_first_crack_levers.heat_target_percent == 80
    assert snap.controller.pre_fc_heat_target_percent.env_overridden is False


def test_partial_json_blob_keeps_non_blob_saved_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial JSON blob (only some fields) must not revert non-blob fields to defaults.

    The bug (#426 blocker): a whole-section skip dropped all saved-file injection
    for a section, causing non-blob fields to silently fall back to code defaults
    (e.g. saved timeout_seconds=99.0 reverting to default 10.0).

    Fix: inject saved-file scalars only for fields NOT present in the blob.
    pydantic-settings scalar nested env vars beat the section JSON blob, so
    injecting a scalar for a non-blob field does not compete with the blob.
    """
    import json

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "advisor:\n  model_slug: saved-model\n  timeout_seconds: 99.0\n  temperature: 0.8\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(cfg_file))
    # Partial blob: only model_slug — timeout_seconds and temperature are NOT in the blob.
    monkeypatch.setenv("ROASTPILOT_ADVISOR", json.dumps({"model_slug": "blob-model"}))

    effective, injected = load_app_config()
    saved_raw = _load_saved_config(cfg_file)
    snap = build_config_snapshot(effective, saved_raw, injected)

    # Blob field wins.
    assert effective.advisor.model_slug == "blob-model"
    assert snap.advisor.model_slug.env_overridden is True

    # Non-blob fields keep their saved values (NOT code defaults).
    assert effective.advisor.timeout_seconds == 99.0, (
        "timeout_seconds must use the saved value (99.0), not the code default (10.0)"
    )
    assert effective.advisor.temperature == 0.8, (
        "temperature must use the saved value (0.8), not the code default (0.0)"
    )
    assert snap.advisor.timeout_seconds.env_overridden is False
    assert snap.advisor.temperature.env_overridden is False


def test_malformed_json_blob_inject_saved_as_env_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed JSON-blob section env var must not crash _inject_saved_as_env.

    The safe fallback is to skip the entire section (no saved-file injection for
    it), which avoids both a crash and the original shadow bug.  The test covers
    _inject_saved_as_env directly because AppConfig() itself raises
    SettingsError for a malformed blob (pydantic-settings' own validation).
    """
    monkeypatch.setenv("ROASTPILOT_ADVISOR", "not json")

    # Must not raise.
    injected = _inject_saved_as_env(
        {"advisor": {"model_slug": "saved-model", "timeout_seconds": 55.0}}
    )

    # Full section skip on malformed blob — no advisor scalars injected.
    assert not any("ROASTPILOT_ADVISOR__" in k for k in injected)
    assert "ROASTPILOT_ADVISOR__MODEL_SLUG" not in os.environ


def test_non_dict_json_blob_in_inject_saved_as_env_skips_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A syntactically valid JSON blob that is NOT a dict (e.g. '[]') triggers the
    non-dict fallback in _inject_saved_as_env: the section is skipped entirely
    (same as a malformed blob — safe, no competing scalars injected).

    Covers the `else: continue` branch at the non-dict path (line 1487).
    """
    monkeypatch.setenv("ROASTPILOT_ADVISOR", "[]")  # valid JSON, not a dict

    injected = _inject_saved_as_env(
        {"advisor": {"model_slug": "saved-model", "timeout_seconds": 30.0}}
    )

    # Non-dict blob → full section skip → no advisor scalars injected.
    assert not any("ROASTPILOT_ADVISOR__" in k for k in injected)
    assert "ROASTPILOT_ADVISOR__MODEL_SLUG" not in os.environ


def test_non_dict_json_blob_in_make_field_meta_leaves_blob_overridden_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-dict JSON blob (e.g. '[]') for a section env var leaves blob_overridden=False
    in _make_field_meta — the isinstance(parsed, dict) branch is False.

    Covers the branch-not-taken path at line 564→569 in _make_field_meta.
    """
    monkeypatch.setenv("ROASTPILOT_ADVISOR", "[]")  # valid JSON, not a dict

    meta = _make_field_meta(
        saved_value="saved-model",
        effective_value="saved-model",
        default_value="openai/gpt-4o",
        env_var="ROASTPILOT_ADVISOR__MODEL_SLUG",
        read_only=False,
        description="test",
        injected_keys=frozenset(),
    )

    # Non-dict blob: blob_overridden=False; scalar var not set → env_overridden=False.
    assert meta.env_overridden is False


def test_malformed_json_blob_in_make_field_meta_leaves_blob_overridden_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed JSON blob (e.g. 'not json') for a section env var leaves
    blob_overridden=False in _make_field_meta — the ValueError path is hit.

    Covers lines 567-568 (except ValueError/TypeError → pass) in _make_field_meta.
    """
    monkeypatch.setenv("ROASTPILOT_ADVISOR", "not json")

    meta = _make_field_meta(
        saved_value="saved-model",
        effective_value="saved-model",
        default_value="openai/gpt-4o",
        env_var="ROASTPILOT_ADVISOR__MODEL_SLUG",
        read_only=False,
        description="test",
        injected_keys=frozenset(),
    )

    # Malformed blob: blob_overridden=False; scalar var not set → env_overridden=False.
    assert meta.env_overridden is False


# ---------------------------------------------------------------------------
# P2-B: uppercase blob keys must NOT cause saved-value skip (#426 P2-B)
# ---------------------------------------------------------------------------


def test_uppercase_blob_key_does_not_skip_saved_value_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON blob with an uppercase key (e.g. "MODEL_SLUG") must NOT skip
    injection of the saved model_slug value.

    pydantic-settings only consumes snake_case field keys.  An uppercase blob
    key like MODEL_SLUG is silently ignored by pydantic, so injecting the
    saved scalar is correct — the field must resolve to the saved value, not
    the schema default (#426 P2-B).
    """
    monkeypatch.setenv("ROASTPILOT_ADVISOR", '{"MODEL_SLUG": "bad-uppercase-key"}')
    # Simulate a clean env (no per-field scalar set by the operator).
    monkeypatch.delenv("ROASTPILOT_ADVISOR__MODEL_SLUG", raising=False)

    injected = _inject_saved_as_env(
        {"advisor": {"model_slug": "saved-model", "timeout_seconds": 30.0}}
    )

    # The uppercase blob key must NOT suppress injection of the saved scalar.
    assert "ROASTPILOT_ADVISOR__MODEL_SLUG" in injected
    assert os.environ.get("ROASTPILOT_ADVISOR__MODEL_SLUG") == "saved-model"


def test_uppercase_blob_key_leaves_blob_overridden_false_in_make_field_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON blob key in uppercase (e.g. MODEL_SLUG) must NOT set
    blob_overridden=True for the matching snake_case field.

    pydantic ignores the uppercase key so the field's effective value does not
    come from the blob — blob_overridden should be False (#426 P2-B).
    """
    monkeypatch.setenv("ROASTPILOT_ADVISOR", '{"MODEL_SLUG": "bad-uppercase-key"}')
    monkeypatch.delenv("ROASTPILOT_ADVISOR__MODEL_SLUG", raising=False)

    meta = _make_field_meta(
        saved_value="saved-model",
        effective_value="saved-model",
        default_value="openai/gpt-4o",
        env_var="ROASTPILOT_ADVISOR__MODEL_SLUG",
        read_only=False,
        description="test",
        injected_keys=frozenset(),
    )

    # Uppercase blob key: pydantic ignores it → env_overridden must be False.
    assert meta.env_overridden is False


# ---------------------------------------------------------------------------
# P2-A: nested JSON blob must cover nested scalar fields (#426 P2-A)
# ---------------------------------------------------------------------------


def test_nested_json_blob_does_not_shadow_nested_saved_scalar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON blob covering a nested sub-section must prevent injection of
    saved scalars for fields present in that nested blob dict.

    E.g. ROASTPILOT_CONTROLLER='{"pre_first_crack_levers":{"heat_target_percent":80}}'
    must not inject ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__HEAT_TARGET_PERCENT
    from the saved config, because the nested blob value should win (#426 P2-A).
    """
    blob = '{"pre_first_crack_levers": {"heat_target_percent": 80}}'
    monkeypatch.setenv("ROASTPILOT_CONTROLLER", blob)
    nested_key = "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__HEAT_TARGET_PERCENT"
    monkeypatch.delenv(nested_key, raising=False)

    saved = {
        "controller": {
            "pre_first_crack_levers": {"heat_target_percent": 55, "fan_target_percent": 30}
        }
    }
    injected = _inject_saved_as_env(saved)

    fan_key = "ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__FAN_TARGET_PERCENT"

    # heat_target_percent is in the blob → must NOT be injected (blob wins).
    assert nested_key not in injected
    assert os.environ.get(nested_key) is None

    # fan_target_percent is NOT in the blob → saved value must be injected.
    assert fan_key in injected
    assert os.environ.get(fan_key) == "30"


# ---------------------------------------------------------------------------
# Ambient environmental sensor fields (D85, #342/#474)
# ---------------------------------------------------------------------------


def test_snapshot_exposes_ambient_fields() -> None:
    """MCPDeviceConfigSnapshot exposes ambient_mode/device/poll_interval_seconds (#474).

    The schema default is ``None`` for all three — "not managed", matching the
    tri-state inherit/override convention for the other optional device fields
    (#439): the MCP's own ``AmbientConfig`` default (mode=disabled, poll=30.0)
    governs until an operator override is saved.
    """
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    assert snapshot.mcp_device.ambient_mode.default is None
    assert snapshot.mcp_device.ambient_device.default is None
    assert snapshot.mcp_device.ambient_poll_interval_seconds.default is None
    # Device/hardware config, not safety — editable (D78 excludes only SafetyLimits).
    assert snapshot.mcp_device.ambient_mode.read_only is False
    assert snapshot.mcp_device.ambient_device.read_only is False
    assert snapshot.mcp_device.ambient_poll_interval_seconds.read_only is False


def test_ambient_round_trip_disabled_to_yoctopuce(
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
) -> None:
    """PUT ambient_mode disabled→yoctopuce + device + poll persists and GET reflects it
    (env-overrides-file, #474 acceptance criterion)."""
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_") and key != "ROASTPILOT_CONFIG_FILE":
            monkeypatch.delenv(key, raising=False)

    persist_config_edit(
        AppConfigEdit(
            mcp_device=MCPDeviceConfigEdit(
                ambient_mode="yoctopuce",
                ambient_device="METEOMK2-1",
                ambient_poll_interval_seconds=15.0,
            )
        )
    )

    effective, injected_keys = load_app_config()
    saved_raw = load_saved_raw()
    snapshot = build_config_snapshot(effective, saved_raw, injected_keys)

    assert effective.mcp_device.ambient_mode == "yoctopuce"
    assert effective.mcp_device.ambient_device == "METEOMK2-1"
    assert effective.mcp_device.ambient_poll_interval_seconds == 15.0

    assert snapshot.mcp_device.ambient_mode.saved_value == "yoctopuce"
    assert snapshot.mcp_device.ambient_mode.effective_value == "yoctopuce"
    assert snapshot.mcp_device.ambient_mode.env_overridden is False
    assert snapshot.mcp_device.ambient_device.saved_value == "METEOMK2-1"
    assert snapshot.mcp_device.ambient_poll_interval_seconds.saved_value == 15.0

    # A real operator env var overrides the saved value (env-overrides-file).
    monkeypatch.setenv("ROASTPILOT_MCP_DEVICE__AMBIENT_MODE", "disabled")
    effective2, injected_keys2 = load_app_config()
    saved_raw2 = load_saved_raw()
    snapshot2 = build_config_snapshot(effective2, saved_raw2, injected_keys2)
    assert snapshot2.mcp_device.ambient_mode.effective_value == "disabled"
    assert snapshot2.mcp_device.ambient_mode.env_overridden is True
    # The saved file value is unaffected by the env override.
    assert snapshot2.mcp_device.ambient_mode.saved_value == "yoctopuce"


def test_ambient_all_none_default_no_saved_section(
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
) -> None:
    """Back-compat: an unedited ambient config (all None) writes no ambient_* keys
    and the effective values stay at the MCP-matching defaults (#474)."""
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_") and key != "ROASTPILOT_CONFIG_FILE":
            monkeypatch.delenv(key, raising=False)

    # An edit that touches only a non-ambient device field.
    persist_config_edit(AppConfigEdit(mcp_device=MCPDeviceConfigEdit(serial_port="/dev/ttyUSB0")))

    saved_raw = load_saved_raw()
    assert "ambient_mode" not in saved_raw.get("mcp_device", {})
    assert "ambient_device" not in saved_raw.get("mcp_device", {})
    assert "ambient_poll_interval_seconds" not in saved_raw.get("mcp_device", {})

    effective, _ = load_app_config()
    assert effective.mcp_device.ambient_mode is None
    assert effective.mcp_device.ambient_device is None
    assert effective.mcp_device.ambient_poll_interval_seconds is None


# ---------------------------------------------------------------------------
# ConfigFieldMeta.yaml_value (#482) — the mcp_device snapshot mirrors the
# hand-authored MCP yaml's current value for each managed field.
# ---------------------------------------------------------------------------


def test_snapshot_yaml_value_populated_from_source_yaml(tmp_path: Path) -> None:
    """A managed key present in the hand-authored yaml appears as yaml_value.

    This is the exact #482 scenario: fc_mode is unconfigured at the agent's own
    config layer (saved=None, effective=None) but the hand-authored yaml says
    "audio" — the snapshot must surface that real value so the FE never renders
    a bogus concrete option in its place.
    """
    yaml_path = tmp_path / "coffee-roaster-mcp.yaml"
    yaml_path.write_text("first_crack:\n  mode: audio\n", encoding="utf-8")
    effective = AppConfig(mcp_device=MCPDeviceConfig(mcp_yaml_source_path=yaml_path))
    snapshot = build_config_snapshot(effective, {})

    fc_mode_meta = snapshot.mcp_device.fc_mode
    assert fc_mode_meta.saved_value is None
    assert fc_mode_meta.effective_value is None
    assert fc_mode_meta.yaml_value == "audio"


def test_snapshot_yaml_value_none_when_no_source_yaml() -> None:
    """With no hand-authored yaml resolvable, yaml_value fails soft to None."""
    effective = AppConfig()  # default MCPDeviceConfig: no mcp_yaml_source_path
    snapshot = build_config_snapshot(effective, {})
    assert snapshot.mcp_device.fc_mode.yaml_value is None
    assert snapshot.mcp_device.serial_port.yaml_value is None


def test_snapshot_yaml_value_none_when_source_yaml_missing(tmp_path: Path) -> None:
    """A configured but nonexistent yaml path fails soft to yaml_value=None
    (never a GET /api/config 500 — that's load_app_config's job)."""
    missing = tmp_path / "does_not_exist.yaml"
    effective = AppConfig(mcp_device=MCPDeviceConfig(mcp_yaml_source_path=missing))
    snapshot = build_config_snapshot(effective, {})
    assert snapshot.mcp_device.fc_mode.yaml_value is None


def test_snapshot_yaml_value_absent_from_non_mcp_device_fields() -> None:
    """yaml_value defaults to None for controller/advisor/safety fields — the
    concept only applies to mcp_device (the agent has no "yaml" for those)."""
    effective = AppConfig()
    snapshot = build_config_snapshot(effective, {})
    assert snapshot.controller.pre_fc_heat_target_percent.yaml_value is None
    assert snapshot.advisor.model_slug.yaml_value is None
    assert snapshot.safety.max_bean_temp_c.yaml_value is None


def test_snapshot_yaml_value_every_mcp_device_field_populated(tmp_path: Path) -> None:
    """Every mcp_device field in the snapshot resolves its own yaml section/key —
    not just fc_mode. Guards against a copy-paste miss on one of the 13 fields."""
    yaml_path = tmp_path / "coffee-roaster-mcp.yaml"
    yaml_path.write_text(
        "roaster:\n  port: /dev/ttyUSB0\n  driver: hottop\n"
        "audio:\n  input_device: USB PnP\n"
        "recording:\n  enabled: true\n  autocapture: false\n  devices: [USB PnP]\n"
        "first_crack:\n  mode: audio\n  confidence_threshold: 0.6\n"
        "session:\n  auto_t0_detection_enabled: true\n  auto_t0_drop_threshold_c: 12.0\n"
        "ambient:\n  mode: yoctopuce\n  device: METEOMK2-1\n  poll_interval_seconds: 30.0\n",
        encoding="utf-8",
    )
    effective = AppConfig(mcp_device=MCPDeviceConfig(mcp_yaml_source_path=yaml_path))
    snapshot = build_config_snapshot(effective, {})
    dev = snapshot.mcp_device

    assert dev.serial_port.yaml_value == "/dev/ttyUSB0"
    assert dev.roaster_driver.yaml_value == "hottop"
    assert dev.audio_input_device.yaml_value == "USB PnP"
    assert dev.recording_enabled.yaml_value is True
    assert dev.recording_autocapture.yaml_value is False
    assert dev.recording_devices.yaml_value == ["USB PnP"]
    assert dev.fc_mode.yaml_value == "audio"
    assert dev.fc_confidence_threshold.yaml_value == 0.6
    assert dev.auto_t0_detection_enabled.yaml_value is True
    assert dev.auto_t0_drop_threshold_c.yaml_value == 12.0
    assert dev.ambient_mode.yaml_value == "yoctopuce"
    assert dev.ambient_device.yaml_value == "METEOMK2-1"
    assert dev.ambient_poll_interval_seconds.yaml_value == 30.0


def test_snapshot_yaml_value_explicit_override_still_shown_alongside(tmp_path: Path) -> None:
    """An operator override (saved_value set) does not suppress yaml_value —
    the FE decides what to render; the snapshot always carries both."""
    yaml_path = tmp_path / "coffee-roaster-mcp.yaml"
    yaml_path.write_text("first_crack:\n  mode: audio\n", encoding="utf-8")
    effective = AppConfig(
        mcp_device=MCPDeviceConfig(mcp_yaml_source_path=yaml_path, fc_mode="manual")
    )
    saved_raw = {"mcp_device": {"fc_mode": "manual"}}
    snapshot = build_config_snapshot(effective, saved_raw)

    fc_mode_meta = snapshot.mcp_device.fc_mode
    assert fc_mode_meta.saved_value == "manual"
    assert fc_mode_meta.effective_value == "manual"
    # yaml_value still reflects the hand-authored yaml's own value, independent
    # of the operator's override.
    assert fc_mode_meta.yaml_value == "audio"


def test_snapshot_yaml_value_degrades_safely_when_only_mcp_env_carries_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qa gap 2: the MCPConfig.env forwarding step (precedence step 2 in
    resolve_mcp_yaml_source_path) is structurally unreachable from
    GET /api/config — build_config_snapshot has no parameter carrying a live
    MCPConfig.env, so passing mcp_env=None there always skips straight from
    step 1 to step 3 (os.environ). This proves the degradation is safe: an
    operator who relies ONLY on step 2 (rare; MCPConfig.env is a runtime
    spawn-time construct, not exported to the ambient environment) gets
    yaml_value=None here, not a crash and not a stale value pretending to be
    fresh. The realistic path (the same yaml also exported into os.environ,
    step 3 — what roast-live.sh actually does) is asserted separately below
    as the "this is what actually works" contrast.
    """
    yaml_path = tmp_path / "coffee-roaster-mcp.yaml"
    yaml_path.write_text("first_crack:\n  mode: audio\n", encoding="utf-8")

    # Nothing in os.environ, no explicit mcp_yaml_source_path — only a
    # hypothetical "step 2" carrier exists, which build_config_snapshot has no
    # way to thread through (it calls resolve_mcp_yaml_source_path(dev,
    # mcp_env=None) unconditionally).
    monkeypatch.delenv("COFFEE_ROASTER_MCP_CONFIG", raising=False)
    effective = AppConfig(mcp_device=MCPDeviceConfig())

    # GET /api/config's real call path (via get_config -> build_config_snapshot)
    # never 500s here — the snapshot still builds successfully.
    snapshot = build_config_snapshot(effective, {})
    assert snapshot.mcp_device.fc_mode.yaml_value is None

    # Contrast: exporting the SAME yaml into os.environ (step 3 — the
    # realistic roast-live.sh path) is picked up correctly, proving the
    # degradation above is specific to the unreachable step 2, not a general
    # resolver bug.
    monkeypatch.setenv("COFFEE_ROASTER_MCP_CONFIG", str(yaml_path))
    snapshot_via_step3 = build_config_snapshot(effective, {})
    assert snapshot_via_step3.mcp_device.fc_mode.yaml_value == "audio"
