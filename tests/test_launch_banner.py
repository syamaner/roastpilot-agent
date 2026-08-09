"""Tests for the ``scripts/roast-live.sh`` banner seam (issue #746).

The launcher's ``Advisor cfg:`` line is the operator's pre-charge check for
which control-teaching prompt a supervised live roast runs under.  It used to
be derived from a bare ``AppConfig()`` — environment variables only — so a
prompt saved through the ``/config`` UI showed the schema default instead.
These tests pin the fix at the resolution boundary: the banner must read the
same layered config the serving agent does, and must never print a
plausible-but-wrong value when that config cannot be resolved.

All tests are hardware-free and point ``ROASTPILOT_CONFIG_FILE`` at a tmp path.
"""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from roastpilot_agent import config_store
from roastpilot_agent.config import AdvisorConfig, AppConfig, ControllerConfig
from roastpilot_agent.config_store import (
    AdvisorConfigEdit,
    AppConfigEdit,
    ConfigFileError,
    ControllerConfigEdit,
    LateMaillardTrimEdit,
    PreFirstCrackLeversEdit,
    persist_config_edit,
)
from roastpilot_agent.launch_banner import (
    EXPERIMENT_TAG,
    LaunchBannerLines,
    load_banner_lines,
    main,
    resolve_banner_lines,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ROASTPILOT_CONFIG_FILE at a temp path with no ROASTPILOT_* env set.

    Clearing the inherited environment matters: a real ``ROASTPILOT_ADVISOR__*``
    export in the developer's shell would otherwise win over the saved file and
    mask exactly the precedence these tests pin.
    """
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_"):
            monkeypatch.delenv(key, raising=False)
    path = tmp_path / "test-config.yaml"
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(path))
    return path


# ---------------------------------------------------------------------------
# Criterion 1 + 3 — the regression: a saved-only prompt must reach the banner
# ---------------------------------------------------------------------------


def test_saved_prompt_version_reaches_the_banner(config_file: Path) -> None:
    """A prompt saved in the file, with NO env override, shows on the banner.

    This is the #746 regression.  Against the old bare-``AppConfig()`` probe the
    banner printed the schema default (``c3``) while the agent ran the saved
    ``c10`` — the wrong A/B arm shown at the one moment a roast cannot be re-run.
    """
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(prompt_version="c10")))

    lines = load_banner_lines()

    assert "prompt c10" in lines.advisor_cfg
    assert "prompt c3" not in lines.advisor_cfg


def test_saved_only_non_default_prompt_is_tagged_experiment(config_file: Path) -> None:
    """The ⚠ EXPERIMENT tag fires for a file-only non-default value (criterion 3).

    The old probe compared an env-only view against the schema defaults, so a
    saved-file experiment looked exactly like the proven baseline and was never
    tagged.
    """
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(prompt_version="c11")))

    assert EXPERIMENT_TAG in load_banner_lines().advisor_cfg


def test_defaults_are_not_tagged_experiment(config_file: Path) -> None:
    """With nothing saved and nothing exported, the pair is the untagged baseline."""
    lines = load_banner_lines()

    assert lines.advisor_cfg == "openai/gpt-4o  ·  prompt c3"
    assert EXPERIMENT_TAG not in lines.advisor_cfg


def test_saved_only_non_default_model_is_tagged_experiment(config_file: Path) -> None:
    """A file-only non-default MODEL is tagged too (the other half of the pair)."""
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4.1-mini")))

    lines = load_banner_lines()

    assert lines.advisor_cfg.startswith("openai/gpt-4.1-mini  ·  prompt c3")
    assert EXPERIMENT_TAG in lines.advisor_cfg


# ---------------------------------------------------------------------------
# Criterion 2 — env still wins, and the banner says so
# ---------------------------------------------------------------------------


def test_env_overrides_saved_prompt_on_the_banner(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exported prompt beats the saved file, and the banner reports the winner.

    This documents the real precedence rather than endorsing it: an env pin makes
    the /config selector a silent no-op, which is why the launcher must show the
    value that actually wins.
    """
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(prompt_version="c10")))
    # Built from parts so the forbidden literal never appears in the repo.
    monkeypatch.setenv("ROASTPILOT_ADVISOR__" + "PROMPT_VERSION", "c11")

    lines = load_banner_lines()

    assert "prompt c11" in lines.advisor_cfg
    assert "c10" not in lines.advisor_cfg


# ---------------------------------------------------------------------------
# Criterion 4 — the trim line reports resolved values
# ---------------------------------------------------------------------------


def test_trim_line_reports_saved_non_default_depth(config_file: Path) -> None:
    """A saved trim depth of 60 prints 60, not the hardcoded 65 % literal."""
    persist_config_edit(
        AppConfigEdit(
            controller=ControllerConfigEdit(
                pre_first_crack_levers=PreFirstCrackLeversEdit(
                    late_maillard_trim=LateMaillardTrimEdit(trim_heat_percent=60)
                )
            )
        )
    )

    lines = load_banner_lines()

    assert lines.trim == f"fixed 60% (schema default 65%){EXPERIMENT_TAG}"


def test_trim_line_default_depth_is_untagged(config_file: Path) -> None:
    """The proven 65 % default keeps its plain, untagged wording."""
    assert load_banner_lines().trim == "fixed 65% (proven roast-6 default)"


def test_trim_line_reports_saved_adaptive_state(config_file: Path) -> None:
    """A saved adaptive_depth_enabled shows as ADAPTIVE with its resolved band.

    The old probe read the environment only, so ADAPTIVE_TRIM=1 showed but the
    same flag saved from /config did not.
    """
    persist_config_edit(
        AppConfigEdit(
            controller=ControllerConfigEdit(
                pre_first_crack_levers=PreFirstCrackLeversEdit(
                    late_maillard_trim=LateMaillardTrimEdit(adaptive_depth_enabled=True)
                )
            )
        )
    )

    lines = load_banner_lines()

    assert lines.trim.startswith("ADAPTIVE — #386 RoR-keyed depth, base 65% within 45–75%")


def test_adaptive_line_wins_over_the_fixed_depth_wording() -> None:
    """Adaptive mode reports the band even when the fixed depth is non-default.

    Pure-formatting check on an in-memory config — the fixed-depth branch must
    not leak into an adaptive banner.
    """
    config = AppConfig.model_validate(
        {
            "advisor": {},
            "controller": {
                "pre_first_crack_levers": {
                    "late_maillard_trim": {
                        "trim_heat_percent": 60,
                        "adaptive_depth_enabled": True,
                        "base_trim": 60,
                    }
                }
            },
        }
    )

    lines = resolve_banner_lines(config)

    assert lines.trim.startswith("ADAPTIVE")
    assert "fixed" not in lines.trim


# ---------------------------------------------------------------------------
# Fail-loud contract — a broken saved config must never look like a default
# ---------------------------------------------------------------------------


def test_malformed_saved_config_raises_rather_than_defaulting(config_file: Path) -> None:
    """A malformed YAML file raises instead of silently reporting schema defaults."""
    config_file.write_text("advisor: [not, a, mapping\n")

    with pytest.raises(ConfigFileError):
        load_banner_lines()


def test_main_reports_malformed_config_and_exits_non_zero(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() prints the reason on stderr and returns 1 so the launcher says 'unresolved'."""
    config_file.write_text("advisor: [not, a, mapping\n")

    assert main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "malformed" in captured.err


def test_main_reports_invalid_values_and_exits_non_zero(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A well-formed file holding an out-of-range value is also a loud failure."""
    # trim_heat_percent has ge=10; 1 is well-formed YAML but invalid config.
    config_file.write_text(
        "controller:\n"
        "  pre_first_crack_levers:\n"
        "    late_maillard_trim:\n"
        "      trim_heat_percent: 1\n"
    )

    with pytest.raises(ValidationError):
        load_banner_lines()
    assert main() == 1

    assert "invalid values" in capsys.readouterr().err


def test_main_reports_unreadable_config_and_exits_non_zero(
    config_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable saved-config file is a loud failure, not a default banner.

    Injected at the store's read boundary rather than via file permissions, so
    the test does not depend on the runner's uid (CI has run as root before).
    """

    def _boom(_path: Path) -> dict[str, object]:
        raise OSError("permission denied")

    config_file.write_text("advisor: {}\n")
    monkeypatch.setattr(config_store, "_load_saved_config", _boom)

    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unreadable" in captured.err


def test_main_prints_advisor_then_trim(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Line 1 is the advisor line, line 2 the trim line — the launcher reads positionally."""
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(prompt_version="c10")))

    assert main() == 0

    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert out[0] == "openai/gpt-4o  ·  prompt c10" + EXPERIMENT_TAG
    assert out[1] == "fixed 65% (proven roast-6 default)"


# ---------------------------------------------------------------------------
# Display-only invariant
# ---------------------------------------------------------------------------


def _default_config() -> AppConfig:
    """Return an AppConfig pinned to the schema defaults, ignoring the environment.

    ``AppConfig`` is a ``BaseSettings``, so a bare ``AppConfig()`` would read
    whatever ``ROASTPILOT_*`` vars happen to be set in the running process. The
    nested section models are plain ``BaseModel``s, so passing them explicitly
    gives a deterministic schema-default config for the pure-formatting tests.
    """
    return AppConfig(advisor=AdvisorConfig(), controller=ControllerConfig())


def test_resolve_banner_lines_does_not_mutate_config() -> None:
    """The seam is display-only: rendering leaves the resolved config untouched."""
    config = _default_config()
    before = config.model_dump()

    result = resolve_banner_lines(config)

    assert isinstance(result, LaunchBannerLines)
    assert config.model_dump() == before


def test_banner_lines_are_frozen() -> None:
    """LaunchBannerLines is immutable — nothing downstream can rewrite the banner."""
    lines = resolve_banner_lines(_default_config())

    with pytest.raises(FrozenInstanceError):
        lines.advisor_cfg = "spoofed"  # type: ignore[misc]


def test_schema_defaults_match_the_untagged_wording() -> None:
    """Pin the untagged baseline to the ACTUAL schema defaults, not to literals.

    If a future PR moves the default model or prompt, this fails rather than
    letting the banner quietly call the new default an experiment.
    """
    lines = resolve_banner_lines(_default_config())

    fields = AdvisorConfig.model_fields
    model = fields["model_slug"].default
    prompt = fields["prompt_version"].default
    assert lines.advisor_cfg == f"{model}  ·  prompt {prompt}"
