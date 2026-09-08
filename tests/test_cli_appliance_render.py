"""Tests for the ``roastpilot-agent appliance render`` CLI wiring (#138, slice 2).

Rendering behavior itself (token substitution, content, atomic writes) is
covered end to end in ``tests/test_appliance_render.py``; these tests cover
only argument parsing, defaults, dispatch from ``main()``, and output
formatting — using a fake ``render_appliance_files`` so no real filesystem
write happens here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from roastpilot_agent import cli
from roastpilot_agent.appliance import render as render_module
from roastpilot_agent.appliance.render import (
    ApplianceRenderError,
    ApplianceRenderInputs,
    RenderedApplianceFiles,
)


def _result(output_dir: Path) -> RenderedApplianceFiles:
    return RenderedApplianceFiles(
        output_dir=output_dir,
        service_path=output_dir / "roastpilot-agent.service",
        env_path=output_dir / "roastpilot-agent.env",
        mcp_yaml_path=output_dir / "coffee-roaster-mcp.appliance.yaml",
    )


# --- parser -----------------------------------------------------------------


def test_appliance_render_parser_requires_output_dir() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._build_appliance_parser().parse_args(["render"])  # pyright: ignore[reportPrivateUsage]
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "arguments", [["--serial-port", "/dev/ttyUSB0"], ["--audio-device", "USB PnP"]]
)
def test_appliance_render_parser_requires_both_hardware_inputs(
    tmp_path: Path, arguments: list[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
            ["render", "--output-dir", str(tmp_path), *arguments]
        )
    assert exc_info.value.code == 2


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_appliance_render_parser_rejects_invalid_ports(tmp_path: Path, port: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
            [
                "render",
                "--output-dir",
                str(tmp_path),
                "--port",
                port,
                "--serial-port",
                "/dev/ttyUSB0",
                "--audio-device",
                "USB PnP",
            ]
        )
    assert exc_info.value.code == 2


def test_appliance_render_parser_defaults(tmp_path: Path) -> None:
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "render",
            "--output-dir",
            str(tmp_path),
            "--serial-port",
            "/dev/ttyUSB0",
            "--audio-device",
            "USB PnP",
        ]
    )
    assert args.appliance_command == "render"
    assert args.output_dir == tmp_path
    assert args.port == 8000
    assert args.operator_group is None
    assert args.json_output is False
    assert isinstance(args.db_path, Path)
    assert isinstance(args.mcp_config_path, Path)
    assert isinstance(args.model_dir, Path)
    assert args.serial_port == Path("/dev/ttyUSB0")
    assert args.audio_device == "USB PnP"


def test_appliance_render_parser_all_flags(tmp_path: Path) -> None:
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "render",
            "--output-dir",
            str(tmp_path / "out"),
            "--port",
            "9001",
            "--operator-user",
            "alice",
            "--operator-group",
            "dialout",
            "--db-path",
            str(tmp_path / "db.sqlite3"),
            "--mcp-config-path",
            str(tmp_path / "mcp.yaml"),
            "--model-dir",
            str(tmp_path / "models"),
            "--serial-port",
            "/dev/ttyUSB0",
            "--audio-device",
            "USB PnP",
            "--json",
        ]
    )
    assert args.output_dir == tmp_path / "out"
    assert args.port == 9001
    assert args.operator_user == "alice"
    assert args.operator_group == "dialout"
    assert args.db_path == tmp_path / "db.sqlite3"
    assert args.mcp_config_path == tmp_path / "mcp.yaml"
    assert args.model_dir == tmp_path / "models"
    assert args.serial_port == Path("/dev/ttyUSB0")
    assert args.audio_device == "USB PnP"
    assert args.json_output is True


# --- _run_appliance_render ---------------------------------------------------


def test_run_appliance_render_success_plain_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "out"
    result = _result(output_dir)
    seen: dict[str, object] = {}

    def fake_render_appliance_files(
        given_output_dir: Path, inputs: ApplianceRenderInputs
    ) -> RenderedApplianceFiles:
        seen["output_dir"] = given_output_dir
        seen["inputs"] = inputs
        return result

    monkeypatch.setattr(render_module, "render_appliance_files", fake_render_appliance_files)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "render",
            "--output-dir",
            str(output_dir),
            "--operator-user",
            "pi",
            "--serial-port",
            "/dev/ttyUSB0",
            "--audio-device",
            "USB PnP",
        ]
    )
    exit_code = cli._run_appliance_render(args)  # pyright: ignore[reportPrivateUsage]

    assert exit_code == 0
    assert seen["output_dir"] == output_dir
    inputs = seen["inputs"]
    assert isinstance(inputs, ApplianceRenderInputs)
    assert inputs.operator_user == "pi"
    assert inputs.operator_group == "pi"  # defaults to operator_user when unset
    out = capsys.readouterr().out
    assert str(output_dir) in out
    assert str(result.service_path) in out


def test_run_appliance_render_operator_group_defaults_to_operator_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_render_appliance_files(
        given_output_dir: Path, inputs: ApplianceRenderInputs
    ) -> RenderedApplianceFiles:
        seen["inputs"] = inputs
        return _result(given_output_dir)

    monkeypatch.setattr(render_module, "render_appliance_files", fake_render_appliance_files)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "render",
            "--output-dir",
            str(tmp_path),
            "--operator-user",
            "alice",
            "--serial-port",
            "/dev/ttyUSB0",
            "--audio-device",
            "USB PnP",
        ]
    )
    cli._run_appliance_render(args)  # pyright: ignore[reportPrivateUsage]

    inputs = seen["inputs"]
    assert isinstance(inputs, ApplianceRenderInputs)
    assert inputs.operator_group == "alice"


def test_run_appliance_render_explicit_operator_group_not_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_render_appliance_files(
        given_output_dir: Path, inputs: ApplianceRenderInputs
    ) -> RenderedApplianceFiles:
        seen["inputs"] = inputs
        return _result(given_output_dir)

    monkeypatch.setattr(render_module, "render_appliance_files", fake_render_appliance_files)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "render",
            "--output-dir",
            str(tmp_path),
            "--operator-user",
            "alice",
            "--operator-group",
            "dialout",
            "--serial-port",
            "/dev/ttyUSB0",
            "--audio-device",
            "USB PnP",
        ]
    )
    cli._run_appliance_render(args)  # pyright: ignore[reportPrivateUsage]

    inputs = seen["inputs"]
    assert isinstance(inputs, ApplianceRenderInputs)
    assert inputs.operator_group == "dialout"


def test_run_appliance_render_success_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "out"
    result = _result(output_dir)

    def fake_render_appliance_files(
        given_output_dir: Path, inputs: ApplianceRenderInputs
    ) -> RenderedApplianceFiles:
        return result

    monkeypatch.setattr(render_module, "render_appliance_files", fake_render_appliance_files)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "render",
            "--output-dir",
            str(output_dir),
            "--operator-user",
            "pi",
            "--serial-port",
            "/dev/ttyUSB0",
            "--audio-device",
            "USB PnP",
            "--json",
        ]
    )
    exit_code = cli._run_appliance_render(args)  # pyright: ignore[reportPrivateUsage]

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == result.to_json_dict()


def test_run_appliance_render_failure_prints_message_and_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_render_appliance_files(
        given_output_dir: Path, inputs: ApplianceRenderInputs
    ) -> RenderedApplianceFiles:
        raise ApplianceRenderError("refusing to render a systemd unit with User/Group 'root'")

    monkeypatch.setattr(render_module, "render_appliance_files", fake_render_appliance_files)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "render",
            "--output-dir",
            str(tmp_path),
            "--operator-user",
            "root",
            "--serial-port",
            "/dev/ttyUSB0",
            "--audio-device",
            "USB PnP",
        ]
    )
    exit_code = cli._run_appliance_render(args)  # pyright: ignore[reportPrivateUsage]

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "appliance render failed" in out
    assert "root" in out


def test_run_appliance_render_oserror_prints_friendly_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_render_appliance_files(
        given_output_dir: Path, inputs: ApplianceRenderInputs
    ) -> RenderedApplianceFiles:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(render_module, "render_appliance_files", fake_render_appliance_files)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "render",
            "--output-dir",
            str(tmp_path),
            "--operator-user",
            "pi",
            "--serial-port",
            "/dev/ttyUSB0",
            "--audio-device",
            "USB PnP",
        ]
    )
    exit_code = cli._run_appliance_render(args)  # pyright: ignore[reportPrivateUsage]

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "appliance render failed" in out
    assert "Permission denied" in out


# --- dispatch from main() ----------------------------------------------------


def test_main_dispatches_appliance_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "out"
    result = _result(output_dir)

    def fake_render_appliance_files(
        given_output_dir: Path, inputs: ApplianceRenderInputs
    ) -> RenderedApplianceFiles:
        return result

    monkeypatch.setattr(render_module, "render_appliance_files", fake_render_appliance_files)
    monkeypatch.setattr(
        "sys.argv",
        [
            "roastpilot-agent",
            "appliance",
            "render",
            "--output-dir",
            str(output_dir),
            "--operator-user",
            "pi",
            "--serial-port",
            "/dev/ttyUSB0",
            "--audio-device",
            "USB PnP",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["output_dir"] == str(output_dir)
