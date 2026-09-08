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

import importlib.util
import os
import stat
import subprocess
import sys
from collections.abc import Callable
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

_MCP_CONFIG_AVAILABLE = importlib.util.find_spec("coffee_roaster_mcp.config") is not None


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
        "operator_home": Path("/home/pi"),
        "db_path": Path("/var/lib/roastpilot-agent/roastpilot.sqlite3"),
        "mcp_config_path": Path("/etc/roastpilot-agent/coffee-roaster-mcp.yaml"),
        "model_dir": Path("/var/lib/roastpilot-agent/models"),
        "serial_port": Path("/dev/serial/by-id/hottop"),
        "audio_device": "USB PnP Audio Device",
    }
    defaults.update(overrides)
    return ApplianceRenderInputs(**defaults)  # type: ignore[arg-type]


def _add_unknown_env_template_token(text: str) -> str:
    """Corrupt an env template with one token outside its closed set."""
    return text + "\nunknown=@@EVIL@@\n"


def _remove_required_env_template_token(text: str) -> str:
    """Corrupt an env template by removing its required PORT substitution."""
    return text.replace("PORT=@@PORT@@\n", "")


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


@pytest.mark.parametrize("marker", ["@@unknown@@", "@@NAME", "@@NAME!@@"])
def test_render_template_text_rejects_all_residual_marker_forms(marker: str) -> None:
    """T13/G18: malformed, lower-case, and truncated markers fail closed."""
    with pytest.raises(ApplianceRenderError, match="marker"):
        render_template_text(marker, {}, known_tokens=frozenset({"NAME"}), template_name="t")


def test_render_template_text_rejects_token_marker_in_value() -> None:
    """T13/G18: a substitution cannot smuggle another token into output."""
    with pytest.raises(ApplianceRenderError, match="marker"):
        render_template_text(
            "hello @@NAME@@",
            {"NAME": "@@EVIL@@"},
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
            return real_read_template(name) + "\nBad=@@EVIL@@\n"
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
            return real_read_template(name) + "\nbogus: @@NOPE@@\n"
        return real_read_template(name)

    monkeypatch.setattr(render_module, "_read_template", fake_read_template)
    output_dir = tmp_path / "out"
    with pytest.raises(ApplianceRenderError, match="unknown token"):
        render_appliance_files(output_dir, _inputs())
    assert not output_dir.exists() or list(output_dir.iterdir()) == []


def test_render_appliance_files_aborts_when_required_template_token_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T13: a missing known service token fails before any output is emitted."""
    import roastpilot_agent.appliance.render as render_module

    real_read_template = render_module._read_template  # pyright: ignore[reportPrivateUsage]

    def fake_read_template(name: str) -> str:
        if name == render_module._SERVICE_TEMPLATE_NAME:  # pyright: ignore[reportPrivateUsage]
            return real_read_template(name).replace("Group=@@OPERATOR_GROUP@@\n", "")
        return real_read_template(name)

    monkeypatch.setattr(render_module, "_read_template", fake_read_template)
    output_dir = tmp_path / "out"
    with pytest.raises(ApplianceRenderError, match="missing required token"):
        render_appliance_files(output_dir, _inputs())
    assert not output_dir.exists() or list(output_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (_add_unknown_env_template_token, "unknown token"),
        (_remove_required_env_template_token, "missing required token"),
    ],
)
def test_render_appliance_files_aborts_on_env_template_token_corruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: Callable[[str], str],
    error: str,
) -> None:
    """T13: invalid env-template token sets fail before any output is emitted."""
    import roastpilot_agent.appliance.render as render_module

    real_read_template = render_module._read_template  # pyright: ignore[reportPrivateUsage]

    def fake_read_template(name: str) -> str:
        if name == render_module._ENV_TEMPLATE_NAME:  # pyright: ignore[reportPrivateUsage]
            return mutation(real_read_template(name))
        return real_read_template(name)

    monkeypatch.setattr(render_module, "_read_template", fake_read_template)
    output_dir = tmp_path / "out"
    with pytest.raises(ApplianceRenderError, match=error):
        render_appliance_files(output_dir, _inputs())
    assert not output_dir.exists() or list(output_dir.iterdir()) == []


def test_render_appliance_files_aborts_before_writing_on_malformed_last_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T13/G18: a lower-case marker in the last template leaves no output set."""
    import roastpilot_agent.appliance.render as render_module

    real_read_template = render_module._read_template  # pyright: ignore[reportPrivateUsage]

    def fake_read_template(name: str) -> str:
        if name == render_module._MCP_YAML_TEMPLATE_NAME:  # pyright: ignore[reportPrivateUsage]
            return real_read_template(name) + "\nbogus: @@audio_device@@\n"
        return real_read_template(name)

    monkeypatch.setattr(render_module, "_read_template", fake_read_template)
    output_dir = tmp_path / "out"
    with pytest.raises(ApplianceRenderError, match="marker"):
        render_appliance_files(output_dir, _inputs())
    assert not output_dir.exists() or list(output_dir.iterdir()) == []


# --- render_service_unit: AC2/AC7 (T11, T12) --------------------------------


def test_render_service_unit_user_and_group_substituted() -> None:
    text = render_service_unit(_inputs(operator_user="alice", operator_group="dialout"))
    assert "User=alice" in text
    assert "Group=dialout" in text


def test_render_service_unit_uses_the_validated_operator_home() -> None:
    text = render_service_unit(_inputs(operator_home=Path("/home/alice")))
    assert (
        "ExecStart=/home/alice/.local/bin/roastpilot-agent serve --host 0.0.0.0 --port ${PORT}"
        in text
    )


@pytest.mark.parametrize(
    "operator_home",
    [
        Path("relative-home"),
        Path("/home/pi#unsafe"),
        Path("/home/$OPENROUTER_API_KEY"),
        Path("/home/pi%h"),
    ],
)
def test_render_service_unit_rejects_unsafe_operator_home(operator_home: Path) -> None:
    with pytest.raises(ApplianceRenderError):
        render_service_unit(_inputs(operator_home=operator_home))


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


def test_render_service_unit_has_only_the_permitted_service_start_command() -> None:
    """T11: exactly one safe service section and no alternate start mechanism."""
    text = render_service_unit(_inputs())
    directives = _unit_directives(text)
    assert sum(raw.strip() == "[Service]" for raw in text.splitlines()) == 1
    assert sum(raw.startswith("ExecStart=") for raw in text.splitlines()) == 1
    assert "ExecStartPre" not in directives
    assert "Environment" not in directives
    assert "KillMode" not in directives
    assert directives["ExecStart"] == (
        "/home/pi/.local/bin/roastpilot-agent serve --host 0.0.0.0 --port ${PORT}"
    )
    assert directives["WorkingDirectory"] == "~"
    assert set(directives) == {
        "Description",
        "After",
        "Wants",
        "Type",
        "User",
        "Group",
        "EnvironmentFile",
        "ExecStart",
        "WorkingDirectory",
        "Restart",
        "RestartSec",
        "TimeoutStopSec",
        "NoNewPrivileges",
        "PrivateTmp",
        "WantedBy",
    }


def test_render_service_unit_binds_all_interfaces() -> None:
    """T12: the rendered unit binds --host 0.0.0.0 (appliance-only override)."""
    text = render_service_unit(_inputs())
    directives = _unit_directives(text)
    assert "--host 0.0.0.0" in directives["ExecStart"]
    assert directives["ExecStart"].endswith("serve --host 0.0.0.0 --port ${PORT}")


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


@pytest.mark.parametrize("port", [False, 0, -1, 65536, "9001"])
def test_render_rejects_invalid_port(port: object) -> None:
    with pytest.raises(ApplianceRenderError, match="integer"):
        render_env_file(_inputs(port=port))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("db_path", Path("/tmp/trace=unsafe.sqlite3")),
        ("mcp_config_path", Path("/etc/roastpilot-agent/config#unsafe.yaml")),
        ("model_dir", Path("/var/lib/roastpilot-agent/models=unsafe")),
    ],
)
def test_absolute_paths_reject_structural_characters(
    field: str, value: Path, tmp_path: Path
) -> None:
    """Absolute paths still reject env/YAML structural characters fail-closed."""
    output_dir = tmp_path / "out"
    with pytest.raises(ApplianceRenderError, match="unsafe structural"):
        render_appliance_files(output_dir, _inputs(**{field: value}))
    assert not output_dir.exists() or list(output_dir.iterdir()) == []


@pytest.mark.parametrize("character", ["\u200e", "\u2028", "\u2029"])
def test_absolute_paths_reject_unicode_format_and_separator_characters(
    character: str, tmp_path: Path
) -> None:
    """Unicode format and line-separator code points cannot enter path values."""
    output_dir = tmp_path / "out"
    unsafe_path = Path(f"/var/lib/roastpilot-agent/model{character}s")

    with pytest.raises(ApplianceRenderError, match="unsafe structural"):
        render_appliance_files(output_dir, _inputs(model_dir=unsafe_path))
    assert not output_dir.exists() or list(output_dir.iterdir()) == []


def test_absolute_path_with_lexical_parent_part_is_rejected_without_normalisation(
    tmp_path: Path,
) -> None:
    """A caller cannot erase a lexical ``..`` before render-time validation."""
    output_dir = tmp_path / "out"
    path_with_parent_part = Path("/var/lib/roastpilot-agent/models/../other")
    assert ".." in path_with_parent_part.parts

    with pytest.raises(ApplianceRenderError, match="without '..'"):
        render_appliance_files(output_dir, _inputs(model_dir=path_with_parent_part))
    assert not output_dir.exists() or list(output_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operator_user", "pi\nExecStart=/bin/evil"),
        ("operator_group", "pi;root"),
        ("db_path", Path("/tmp/db\nEVIL=yes")),
        ("mcp_config_path", Path("relative.yaml")),
        ("serial_port", Path("/tmp/tty")),
        ("audio_device", "USB\nrecording: {enabled: true}"),
    ],
)
def test_operator_values_reject_systemd_env_and_yaml_injection(
    field: str, value: object, tmp_path: Path
) -> None:
    with pytest.raises(ApplianceRenderError):
        render_appliance_files(tmp_path / "not-written", _inputs(**{field: value}))


def test_audio_device_is_yaml_encoded_without_key_injection() -> None:
    parsed = yaml.safe_load(render_mcp_yaml(_inputs(audio_device='USB: "PnP" # 1')))
    assert parsed["audio"]["input_device"] == 'USB: "PnP" # 1'


# --- render_mcp_yaml: pi_inference exactness (T9), recording off (T10) -----


def test_render_mcp_yaml_pi_inference_values_are_exact() -> None:
    serial_port = Path("/dev/serial/by-id/hottop-custom")
    audio_device = "USB PnP Audio Device custom"
    text = render_mcp_yaml(
        _inputs(
            model_dir=Path("/opt/roastpilot/models"),
            serial_port=serial_port,
            audio_device=audio_device,
        )
    )
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

    roaster = parsed["roaster"]
    assert roaster["port"] == str(serial_port)

    audio = parsed["audio"]
    assert audio["window_seconds"] == 10.0
    assert audio["overlap"] == 0.3
    assert audio["source"] == "microphone"
    assert audio["input_device"] == audio_device


@pytest.mark.skipif(
    not _MCP_CONFIG_AVAILABLE, reason="coffee-roaster-mcp optional dependency is absent"
)
def test_rendered_mcp_yaml_loads_through_the_mcp_public_config_loader(tmp_path: Path) -> None:
    """T9: MCP's typed loader accepts every ratified rendered config key."""
    from coffee_roaster_mcp.config import load_config

    model_dir = Path("/var/lib/roastpilot-agent/models-loader-test")
    serial_port = Path("/dev/serial/by-id/hottop-loader-test")
    audio_device = "USB PnP Audio Device loader test"
    yaml_path = tmp_path / "coffee-roaster-mcp.appliance.yaml"
    yaml_path.write_text(
        render_mcp_yaml(
            _inputs(
                model_dir=model_dir,
                serial_port=serial_port,
                audio_device=audio_device,
            )
        ),
        encoding="utf-8",
    )

    loaded = load_config(yaml_path, environ={})

    assert loaded.transport.type == "stdio"
    assert loaded.roaster.driver == "hottop_kn8828b_2k_plus"
    assert loaded.roaster.port == str(serial_port)
    assert loaded.roaster.baudrate == 115200
    assert loaded.roaster.temperature_unit == "auto"
    assert loaded.roaster.command_interval_seconds == 0.3
    assert loaded.first_crack.mode == "audio"
    assert loaded.first_crack.repo_id == REPO_ID
    assert loaded.first_crack.revision == REVISION
    assert loaded.first_crack.precision == "int8"
    assert loaded.first_crack.local_model_dir == model_dir
    assert loaded.first_crack.onnx_threads == 2
    assert loaded.first_crack.confidence_threshold == 0.90
    assert loaded.first_crack.min_positive_windows == 3
    assert loaded.first_crack.confirmation_window_seconds == 30.0
    assert loaded.first_crack.allow_manual_override is True
    assert loaded.audio.source == "microphone"
    assert loaded.audio.input_device == audio_device
    assert loaded.audio.sample_rate == 16000
    assert loaded.audio.wav_path is None
    assert loaded.audio.replay_mode == "realtime"
    assert loaded.audio.window_seconds == 10.0
    assert loaded.audio.overlap == 0.3
    assert loaded.audio.hop_seconds is None
    assert loaded.recording.enabled is False


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
    assert set(parsed) == {"transport", "roaster", "first_crack", "audio"}
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
    assert result.output_dir == output_dir.resolve()


def test_render_appliance_files_rejects_symlink_output_dir(tmp_path: Path) -> None:
    """A final-component symlink cannot redirect rendered appliance files."""
    real_output_dir = tmp_path / "real-output"
    real_output_dir.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(real_output_dir, target_is_directory=True)

    with pytest.raises(ApplianceRenderError, match="must not be a symlink"):
        render_appliance_files(output_link, _inputs())

    assert list(real_output_dir.iterdir()) == []


def test_render_appliance_files_rejects_symlinked_output_ancestor(tmp_path: Path) -> None:
    """An existing ancestor symlink cannot redirect a newly-created output directory."""
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ApplianceRenderError, match="symlinked ancestor"):
        render_appliance_files(linked_parent / "out", _inputs())

    assert list(real_parent.iterdir()) == []


def test_render_module_has_no_direct_control_path_imports() -> None:
    """The renderer is a packaging helper, not a roaster-control dependency."""
    import ast

    import roastpilot_agent.appliance.render as render_module

    tree = ast.parse(Path(render_module.__file__).read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
    forbidden = {
        "roastpilot_agent.controller",
        "roastpilot_agent.safety",
        "roastpilot_agent.mcp_client",
    }
    assert imported_modules.isdisjoint(forbidden)


def test_render_module_never_transitively_imports_control_path() -> None:
    """A fresh interpreter proves renderer imports do not reach roast control."""
    script = (
        "import sys\n"
        "import roastpilot_agent.appliance.render\n"
        "loaded = {m for m in sys.modules if m.startswith('roastpilot_agent.')}\n"
        "forbidden = {\n"
        "    'roastpilot_agent.controller',\n"
        "    'roastpilot_agent.safety',\n"
        "    'roastpilot_agent.mcp_client',\n"
        "}\n"
        "print(','.join(sorted(loaded & forbidden)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", result.stderr


def test_stage_write_removes_temp_file_on_mid_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rare mid-write OSError (e.g. a permission change) removes the
    temp file rather than leaving a stray ``.part`` behind."""
    import roastpilot_agent.appliance.render as render_module

    def failing_chmod(path: object, mode: object) -> None:
        raise OSError("simulated chmod failure")

    monkeypatch.setattr(os, "chmod", failing_chmod)
    target = tmp_path / "target.txt"

    with pytest.raises(OSError, match="simulated chmod failure"):
        render_module._stage_write(  # pyright: ignore[reportPrivateUsage]
            target, "content", mode=0o644
        )

    assert not target.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".target.txt.")]
    assert leftovers == []


def test_render_appliance_files_rolls_back_preexisting_set_on_third_commit_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A third commit failure restores the old bytes and modes of every file."""
    output_dir = tmp_path / "out"
    render_appliance_files(output_dir, _inputs())
    old = {
        path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in output_dir.iterdir()
    }
    real_replace = os.replace
    calls = {"commits": 0}

    def flaky_replace(
        src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        if str(src).endswith(".part"):
            calls["commits"] += 1
            if calls["commits"] == 3:
                raise OSError("simulated third commit failure")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    with pytest.raises(OSError, match="simulated third commit failure"):
        render_appliance_files(output_dir, _inputs())
    assert {
        path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in output_dir.iterdir()
    } == old
    assert not list(output_dir.glob(".*.part"))
    assert not list(output_dir.glob(".*.backup"))


def test_render_appliance_files_leaves_no_files_on_first_commit_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A first-time replacement failure leaves neither artifacts nor staging files."""
    real_replace = os.replace

    def failing_replace(
        src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        if str(src).endswith(".part"):
            raise OSError("simulated first commit failure")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    output_dir = tmp_path / "out"
    with pytest.raises(OSError, match="first commit failure"):
        render_appliance_files(output_dir, _inputs())
    assert list(output_dir.iterdir()) == []


def test_render_appliance_files_mixed_rollback_restores_old_and_removes_new(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A late commit failure preserves old targets and removes newly created ones."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    old_service = output_dir / "roastpilot-agent.service"
    old_service.write_text("old service", encoding="utf-8")
    old_mode = 0o640
    os.chmod(old_service, old_mode)

    real_replace = os.replace
    calls = {"commits": 0}

    def flaky_replace(
        src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        if str(src).endswith(".part"):
            calls["commits"] += 1
            if calls["commits"] == 3:
                raise OSError("simulated mixed commit failure")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError, match="mixed commit failure"):
        render_appliance_files(output_dir, _inputs())

    assert old_service.read_text(encoding="utf-8") == "old service"
    assert stat.S_IMODE(old_service.stat().st_mode) == old_mode
    assert not (output_dir / "roastpilot-agent.env").exists()
    assert not (output_dir / "coffee-roaster-mcp.appliance.yaml").exists()
    assert not list(output_dir.glob(".*.part"))
    assert not list(output_dir.glob(".*.backup"))
