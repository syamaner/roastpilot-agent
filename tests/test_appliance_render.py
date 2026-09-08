"""Tests for the appliance systemd unit / env / MCP-YAML renderer (#138, slice 2).

Covers T9-T13 from the ratified implementation contract: exact
``pi_inference`` values (AC6), recording stays off (AC6), the systemd unit's
safety/hardening properties (AC2/AC7), and the renderer's closed-token
fail-closed behaviour (§2.3 item 4). CLI wiring is covered separately in
``tests/test_cli_appliance_render.py``; the env file's real-``AppConfig``
consumption contract is covered in
``tests/test_appliance_render_config_contract.py``.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from roastpilot_agent.appliance.model_manifest import REPO_ID, REVISION
from roastpilot_agent.appliance.render import (
    ApplianceRenderError,
    ApplianceRenderInputs,
    RenderedApplianceFiles,
    render_appliance_files,
    render_env_file,
    render_mcp_yaml,
    render_service_unit,
    render_template_text,
)


def _unit_directives(text: str) -> dict[str, str]:
    """Parse a rendered systemd unit's actual ``Key=Value`` directive lines.

    Ignores comments (``#``), section headers (``[...]``), and blank lines —
    so a comment that happens to *mention* a directive name in prose (e.g.
    explaining why ``PrivateDevices`` is intentionally omitted) never gets
    mistaken for the directive itself.
    """
    directives: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, separator, value = line.partition("=")
        if separator:
            directives[key] = value
    return directives


def _inputs(**overrides: object) -> ApplianceRenderInputs:
    defaults: dict[str, object] = {
        "port": 9001,
        "operator_user": "pi",
        "operator_group": "pi",
        "db_path": Path("/var/lib/roastpilot-agent/roastpilot.sqlite3"),
        "mcp_config_path": Path("/etc/roastpilot-agent/coffee-roaster-mcp.yaml"),
        "model_dir": Path("/var/lib/roastpilot-agent/models"),
    }
    defaults.update(overrides)
    return ApplianceRenderInputs(**defaults)  # type: ignore[arg-type]


# --- render_template_text: closed-token strictness (T13, G18) --------------


def test_render_template_text_substitutes_known_tokens() -> None:
    rendered = render_template_text(
        "hello @@NAME@@", {"NAME": "world"}, known_tokens=frozenset({"NAME"}), template_name="t"
    )
    assert rendered == "hello world"


def test_render_template_text_rejects_unsubstituted_known_token() -> None:
    """A known token with no supplied value fails closed (missing substitution)."""
    with pytest.raises(ApplianceRenderError, match="missing substitution"):
        render_template_text(
            "hello @@NAME@@", {}, known_tokens=frozenset({"NAME"}), template_name="t"
        )


def test_render_template_text_rejects_unknown_token_in_template() -> None:
    """A token present in the template but outside the closed set fails closed."""
    with pytest.raises(ApplianceRenderError, match="unknown token"):
        render_template_text(
            "hello @@EVIL@@", {}, known_tokens=frozenset({"NAME"}), template_name="t"
        )


def test_render_template_text_rejects_unknown_token_supplied() -> None:
    """A caller-supplied token outside the closed set fails closed."""
    with pytest.raises(ApplianceRenderError, match="unknown token"):
        render_template_text(
            "hello @@NAME@@",
            {"NAME": "world", "EVIL": "x"},
            known_tokens=frozenset({"NAME"}),
            template_name="t",
        )


def test_render_appliance_files_aborts_and_writes_nothing_on_bad_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T13: a corrupted template (unknown token) aborts before any file is written."""
    import roastpilot_agent.appliance.render as render_module

    def fake_read_template(name: str) -> str:
        if name == render_module._SERVICE_TEMPLATE_NAME:  # pyright: ignore[reportPrivateUsage]
            return "[Service]\nUser=@@OPERATOR_USER@@\nGroup=@@OPERATOR_GROUP@@\nBad=@@EVIL@@\n"
        return real_read_template(name)

    real_read_template = render_module._read_template  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(render_module, "_read_template", fake_read_template)

    output_dir = tmp_path / "out"
    with pytest.raises(ApplianceRenderError, match="unknown token"):
        render_appliance_files(output_dir, _inputs())

    assert not output_dir.exists() or list(output_dir.iterdir()) == []


def test_render_appliance_files_aborts_on_mcp_yaml_template_corruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T13: a corrupted MCP-YAML template also aborts before any file is written
    (the service unit template is rendered first, so this also proves the whole
    call fails closed even when the LAST template in render order is bad)."""
    import roastpilot_agent.appliance.render as render_module

    real_read_template = render_module._read_template  # pyright: ignore[reportPrivateUsage]

    def fake_read_template(name: str) -> str:
        if name == render_module._MCP_YAML_TEMPLATE_NAME:  # pyright: ignore[reportPrivateUsage]
            return "first_crack:\n  repo_id: @@FC_REPO_ID@@\n  bogus: @@NOPE@@\n"
        return real_read_template(name)

    monkeypatch.setattr(render_module, "_read_template", fake_read_template)
    output_dir = tmp_path / "out"
    with pytest.raises(ApplianceRenderError, match="unknown token"):
        render_appliance_files(output_dir, _inputs())
    assert not output_dir.exists() or list(output_dir.iterdir()) == []


# --- render_service_unit: AC2/AC7 (T11, T12) --------------------------------


def test_render_service_unit_user_and_group_substituted() -> None:
    text = render_service_unit(_inputs(operator_user="alice", operator_group="dialout"))
    assert "User=alice" in text
    assert "Group=dialout" in text


def test_render_service_unit_never_root() -> None:
    with pytest.raises(ApplianceRenderError, match="root"):
        render_service_unit(_inputs(operator_user="root", operator_group="pi"))
    with pytest.raises(ApplianceRenderError, match="root"):
        render_service_unit(_inputs(operator_user="pi", operator_group="root"))


def test_render_service_unit_rejects_empty_identity() -> None:
    with pytest.raises(ApplianceRenderError, match="non-empty"):
        render_service_unit(_inputs(operator_user=" ", operator_group="pi"))


def test_render_service_unit_environment_file_fails_closed() -> None:
    """EnvironmentFile has no leading '-' — a missing env file fails the unit loudly."""
    text = render_service_unit(_inputs())
    directives = _unit_directives(text)
    assert directives["EnvironmentFile"] == "/etc/roastpilot-agent/roastpilot-agent.env"


def test_render_service_unit_restart_and_timeouts_present() -> None:
    text = render_service_unit(_inputs())
    directives = _unit_directives(text)
    assert directives["Restart"] == "on-failure"
    assert directives["RestartSec"]
    assert directives["TimeoutStopSec"]


def test_render_service_unit_hardening_present_and_absent() -> None:
    text = render_service_unit(_inputs())
    directives = _unit_directives(text)
    assert directives["NoNewPrivileges"] == "true"
    assert directives["PrivateTmp"] == "true"
    assert "PrivateDevices" not in directives
    assert "ProtectHome" not in directives
    assert "DeviceAllow" not in directives


def test_render_service_unit_no_resume_flag_in_exec_start_or_exec_start_pre() -> None:
    """No ExecStartPre directive exists, and ExecStart carries no resume/start-run flag."""
    text = render_service_unit(_inputs())
    directives = _unit_directives(text)
    assert "ExecStartPre" not in directives
    exec_start = directives["ExecStart"]
    for forbidden in ("--resume", "resume", "--continue"):
        assert forbidden not in exec_start


def test_render_service_unit_binds_all_interfaces() -> None:
    """T12: the rendered unit binds --host 0.0.0.0 (appliance-only override)."""
    text = render_service_unit(_inputs())
    directives = _unit_directives(text)
    assert "--host 0.0.0.0" in directives["ExecStart"]
    assert "--port ${PORT}" in directives["ExecStart"]


def test_cli_serve_host_default_is_still_localhost() -> None:
    """T12: the appliance unit's 0.0.0.0 override never changed `serve`'s own default."""
    from roastpilot_agent import cli

    parser = cli._build_parser()  # pyright: ignore[reportPrivateUsage]
    args = parser.parse_args(["serve"])
    assert args.host == "127.0.0.1"


# --- render_env_file (contributes to T14's fixture; basic shape here) ------


def test_render_env_file_never_defaults_a_credential_value() -> None:
    text = render_env_file(_inputs())
    lines = [line for line in text.splitlines() if line.startswith("OPENROUTER_API_KEY=")]
    assert lines == ["OPENROUTER_API_KEY="]


def test_render_env_file_substitutes_port_db_and_mcp_config() -> None:
    text = render_env_file(
        _inputs(
            port=1234,
            db_path=Path("/tmp/x/db.sqlite3"),
            mcp_config_path=Path("/tmp/x/mcp.yaml"),
        )
    )
    assert "PORT=1234" in text
    assert "ROASTPILOT_DB=/tmp/x/db.sqlite3" in text
    assert "COFFEE_ROASTER_MCP_CONFIG=/tmp/x/mcp.yaml" in text


# --- render_mcp_yaml: pi_inference exactness (T9), recording off (T10) -----


def test_render_mcp_yaml_pi_inference_values_are_exact() -> None:
    text = render_mcp_yaml(_inputs(model_dir=Path("/opt/roastpilot/models")))
    parsed = yaml.safe_load(text)

    first_crack = parsed["first_crack"]
    assert first_crack["mode"] == "audio"
    assert first_crack["repo_id"] == REPO_ID
    assert first_crack["revision"] == REVISION
    assert first_crack["precision"] == "int8"
    assert first_crack["local_model_dir"] == "/opt/roastpilot/models"
    assert first_crack["onnx_threads"] == 2
    assert first_crack["confidence_threshold"] == 0.90
    assert first_crack["min_positive_windows"] == 3
    assert first_crack["confirmation_window_seconds"] == 30.0

    audio = parsed["audio"]
    assert audio["window_seconds"] == 10.0
    assert audio["overlap"] == 0.3
    assert audio["source"] == "microphone"


def test_render_mcp_yaml_repo_id_and_revision_come_from_the_manifest_not_inputs() -> None:
    """The manifest is the single source of truth; ApplianceRenderInputs has no override."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(ApplianceRenderInputs)}
    assert "repo_id" not in field_names
    assert "revision" not in field_names


def test_render_mcp_yaml_contains_no_recording_enabling_key() -> None:
    """T10/G20: coffee-roaster-mcp==0.2.0's RecordingConfig.enabled defaults false;
    the template omits `recording:` entirely rather than guessing."""
    text = render_mcp_yaml(_inputs())
    parsed = yaml.safe_load(text)
    assert "recording" not in parsed
    assert "enabled: true" not in text.lower()


def test_render_mcp_yaml_is_one_primary_audio_stream() -> None:
    text = render_mcp_yaml(_inputs())
    parsed = yaml.safe_load(text)
    assert isinstance(parsed["audio"], dict)
    assert "devices" not in parsed


# --- render_appliance_files: end-to-end write behaviour ---------------------


def test_render_appliance_files_writes_all_three_with_correct_modes(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    result = render_appliance_files(output_dir, _inputs())

    assert isinstance(result, RenderedApplianceFiles)
    assert result.service_path.read_text().startswith("# roastpilot-agent systemd unit")
    assert result.env_path.read_text().startswith("# roastpilot-agent appliance environment")
    parsed = yaml.safe_load(result.mcp_yaml_path.read_text())
    assert parsed["transport"]["type"] == "stdio"

    assert stat.S_IMODE(result.service_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(result.env_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.mcp_yaml_path.stat().st_mode) == 0o644


def test_render_appliance_files_is_idempotent(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    first = render_appliance_files(output_dir, _inputs())
    second = render_appliance_files(output_dir, _inputs())
    assert first.service_path.read_text() == second.service_path.read_text()
    assert first.env_path.read_text() == second.env_path.read_text()
    assert first.mcp_yaml_path.read_text() == second.mcp_yaml_path.read_text()


def test_render_appliance_files_creates_output_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "nested" / "out"
    result = render_appliance_files(output_dir, _inputs())
    assert result.output_dir.is_dir()


def test_atomic_write_removes_temp_file_on_mid_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rare mid-write OSError (e.g. a permission change) removes the
    temp file rather than leaving a stray ``.part`` behind."""
    import os

    import roastpilot_agent.appliance.render as render_module

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    target = tmp_path / "target.txt"

    with pytest.raises(OSError, match="simulated replace failure"):
        render_module._atomic_write(  # pyright: ignore[reportPrivateUsage]
            target, "content", mode=0o644
        )

    assert not target.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".target.txt.")]
    assert leftovers == []


def test_render_appliance_files_rolls_back_earlier_writes_on_later_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the third (MCP-YAML) atomic write fails, the first two files this
    call already wrote (the unit and the env file) are removed too — no
    partial artifact set survives."""
    import roastpilot_agent.appliance.render as render_module

    output_dir = tmp_path / "out"
    real_atomic_write = render_module._atomic_write  # pyright: ignore[reportPrivateUsage]
    calls = {"count": 0}

    def flaky_atomic_write(path: Path, content: str, *, mode: int) -> None:
        calls["count"] += 1
        if calls["count"] == 3:
            raise OSError("simulated disk full")
        real_atomic_write(path, content, mode=mode)

    monkeypatch.setattr(render_module, "_atomic_write", flaky_atomic_write)

    with pytest.raises(OSError, match="simulated disk full"):
        render_appliance_files(output_dir, _inputs())

    assert list(output_dir.iterdir()) == []
