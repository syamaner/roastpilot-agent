"""Hardware-free behavioural contract tests for the Pi installer (#138, slice 3)."""
# ruff: noqa: E501

from __future__ import annotations

import grp
import json
import os
import pwd
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from roastpilot_agent.appliance.model_manifest import MANIFEST_FILES, REPO_ID, REVISION
from roastpilot_agent.appliance.render import (
    ApplianceRenderInputs,
    render_env_file,
    render_mcp_yaml,
    render_service_unit,
)

INSTALLER = Path(__file__).parents[1] / "packaging/pi/install.sh"


@pytest.fixture
def installer_harness(tmp_path: Path) -> tuple[Path, dict[str, str], Path, Path]:
    """Create one recorded-fake PATH and a root-free appliance destination.

    This is serial because the real script is run in subprocesses and mutates
    process-visible fake-command state plus a temporary installation root.
    """
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    hostname = tmp_path / "hostname"
    hostname.write_text("old-host\n")
    os_release = tmp_path / "os-release"
    os_release.write_text("ID=debian\nID_LIKE=debian\n")
    fake = fake_bin / "fake"
    fake.write_text(
        """#!/usr/bin/env bash
set -eu
name=$(basename "$0")
printf '%s' "$name" >> "$FAKE_LOG"
for arg in "$@"; do printf ' <%s>' "$arg" >> "$FAKE_LOG"; done
printf '\\n' >> "$FAKE_LOG"
case "$name" in
  sudo) shift; [ "${1:-}" = -- ] && shift; exec "$@" ;;
  id)
    if [ "${1:-}" = -u ]; then echo 1000
    elif [ "${1:-}" = -un ]; then echo "${FAKE_ID_USER:-operator}"
    elif [ "${1:-}" = -gn ]; then echo "${FAKE_ID_GROUP:-operators}"
    else cat "$FAKE_GROUPS"; fi ;;
  getent) if [ -n "${FAKE_GETENT_RECORD:-}" ]; then
      printf '%s\\n' "$FAKE_GETENT_RECORD"
    else
      printf 'operator:x:1000:1000::%s:/bin/sh\\n' "$FAKE_OPERATOR_HOME"
    fi ;;
  uname) echo aarch64 ;;
  hostnamectl)
    if [ "${1:-}" = --static ]; then
      if [ "${FAKE_HOSTNAME_VERIFY_FAIL:-}" = 1 ]; then echo wrong-host
      else cat "$FAKE_HOSTNAME"; fi
    else [ "$1" = set-hostname ]; printf '%s\\n' "$2" > "$FAKE_HOSTNAME"; fi ;;
  pipx)
    if [ "${1:-}" = list ]; then
      if [ "${FAKE_PIPX_LIST_FAIL:-}" = 1 ]; then exit 17
      elif [ -n "${FAKE_PIPX_JSON:-}" ]; then cat "$FAKE_PIPX_JSON"
      elif [ -e "$FAKE_PIPX_STATE" ]; then cat "$FAKE_PIPX_STATE"
      else printf '{"venvs": {}}\\n'; fi
    elif [ "${1:-}" = install ]; then
      shift; [ "${1:-}" = -- ] && shift
      package="$1"; version="${package##*==}"
      [ "$version" = "$package" ] && version=default
      printf '{"venvs":{"roastpilot-agent":{"metadata":' > "$FAKE_PIPX_STATE"
      printf '{"main_package":{"package_version":"%s",' "$version" >> "$FAKE_PIPX_STATE"
      printf '"package_or_url":"%s"}}}}}\\n' "$package" >> "$FAKE_PIPX_STATE"
    elif [ "${1:-}" = uninstall ]; then rm -f "$FAKE_PIPX_STATE"; fi ;;
  roastpilot-agent)
    if [ "$1 $2 $3" = "appliance model install" ]; then
      shift 3; while [ "$#" -gt 0 ]; do [ "$1" = --dest ] && {
        if [ ! -e "$2/onnx/int8/model_quantized.onnx" ]; then
          printf 'MODEL_FETCH <%s>\n' "$2" >> "$FAKE_LOG"
          mkdir -p "$2/onnx/int8"; printf '%s' "${FAKE_MODEL_BYTES:-MODEL}" > "$2/onnx/int8/model_quantized.onnx"
          printf CONFIG > "$2/onnx/int8/preprocessor_config.json"
        fi; }; shift; done
    else
      out=''; port=8000; while [ "$#" -gt 0 ]; do
        [ "$1" = --output-dir ] && { out="$2"; shift; }
        [ "$1" = --port ] && { port="$2"; shift; }
        shift
      done
      mkdir -p "$out"
      cat > "$out/roastpilot-agent.env" <<ENV
OPENROUTER_API_KEY=
PORT=$port
ROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3
COFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml
ENV
      [ -z "${FAKE_RENDERED_ENV:-}" ] || printf '%s\\n' "$FAKE_RENDERED_ENV" > "$out/roastpilot-agent.env"
      cat > "$out/coffee-roaster-mcp.appliance.yaml" <<YAML
transport:
  type: stdio
roaster:
  driver: hottop_kn8828b_2k_plus
  port: "/dev/ttyUSB0"
  baudrate: 115200
  temperature_unit: auto
  command_interval_seconds: 0.3
session:
  auto_t0_detection_enabled: true
  auto_t0_drop_threshold_c: 15.0
  ror_window_seconds: 60
  ror_min_sample_seconds: 10
first_crack:
  mode: audio
  repo_id: syamaner/coffee-first-crack-detection
  revision: b349a919c34b6130472da97c01817be404e4f629
  precision: int8
  local_model_dir: "/var/lib/roastpilot-agent/models"
  onnx_threads: 2
  confidence_threshold: 0.90
  min_positive_windows: 3
  confirmation_window_seconds: 30.0
  allow_manual_override: true
audio:
  source: microphone
  input_device: "USB mic"
  sample_rate: 16000
  wav_path: null
  replay_mode: realtime
  window_seconds: 10.0
  overlap: 0.3
  hop_seconds: null
YAML
      [ -z "${FAKE_RENDERED_YAML:-}" ] || printf '%s\n' "$FAKE_RENDERED_YAML" > "$out/coffee-roaster-mcp.appliance.yaml"
      cat > "$out/roastpilot-agent.service" <<UNIT
[Unit]
Description=RoastPilot agent (native Pi appliance)
After=network-online.target sound.target
Wants=network-online.target
[Service]
Type=simple
User=operator
Group=operators
EnvironmentFile=/etc/roastpilot-agent/roastpilot-agent.env
ExecStart=$FAKE_OPERATOR_HOME/.local/bin/roastpilot-agent serve --host 0.0.0.0 --port \\${PORT}
WorkingDirectory=~
Restart=on-failure
RestartSec=5
KillMode=mixed
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT
      [ -z "${FAKE_RENDERED_UNIT:-}" ] || printf '%s\\n' "$FAKE_RENDERED_UNIT" > "$out/roastpilot-agent.service"
    fi ;;
  tee) [ "${1:-}" = -- ] && shift; [ "${FAKE_TEE_FAIL:-}" != 1 ] || exit 19; mkdir -p "$(dirname "$1")"; cat > "$1" ;;
  install) mode=0644; [ "${1:-}" = -m ] && { mode="$2"; shift 2; }
    [ "${1:-}" = -- ] && shift; cp "$1" "$2"; chmod "$mode" "$2" ;;
  mkdir) /bin/mkdir "$@" ;;
  chmod) [ "${2:-}" = -- ] && { mode="$1"; shift 2; [ "${FAKE_CHMOD_FAIL_TARGET:-}" != "$1" ] || exit 23; /bin/chmod "$mode" "$@"; } || /bin/chmod "$@" ;;
  mktemp) is_dir=0; [ "${1:-}" = -d ] && { is_dir=1; shift; }; [ "${1:-}" = -- ] && shift; dir="${1%XXXXXX}fake"
    if [ "$is_dir" = 1 ]; then mkdir -p "$dir"; else mkdir -p "$(dirname "$dir")"; : > "$dir"; fi; printf '%s\\n' "$dir" ;;
  rm) /bin/rm "$@" ;;
  cp) /bin/cp "$@" ;;
  mv) /bin/mv "$@" ;;
  sha256sum)
    if [ "$#" = 0 ]; then cat >/dev/null; echo "content-digest  -"; exit 0; fi
    [ "${1:-}" = -- ] && shift
    content=$(cat "$1")
    case "$content" in
      MODEL) echo "022092cddd4c2cd740670c0a85786460699bc1b4f03e20f508182768d21545df  $1" ;;
      CONFIG) echo "8d04ba5a9c6fca5d39d0de2b1fd05ecf79deb589fbba279728bbebac39934231  $1" ;;
      *) echo "content-digest  $1" ;;
    esac ;;
  grep) /usr/bin/grep "$@" ;;
  tr) /usr/bin/tr "$@" ;;
  usermod) printf 'dialout audio\n' > "$FAKE_GROUPS" ;;
  apt-get|chown|systemctl) : ;;
esac
"""
    )
    fake.chmod(0o755)
    for name in (
        "sudo",
        "id",
        "getent",
        "uname",
        "hostnamectl",
        "pipx",
        "tee",
        "install",
        "mkdir",
        "chmod",
        "mktemp",
        "rm",
        "chown",
        "cp",
        "mv",
        "sha256sum",
        "apt-get",
        "usermod",
        "systemctl",
        "grep",
        "tr",
    ):
        (fake_bin / name).symlink_to(fake)
    agent = fake_bin / "roastpilot-agent"
    agent.write_text(fake.read_text())
    agent.chmod(0o755)
    operator_home = tmp_path / "operator-home"
    pipx_agent = operator_home / ".local/pipx/venvs/roastpilot-agent/bin/roastpilot-agent"
    pipx_agent.parent.mkdir(parents=True)
    pipx_agent.write_text(fake.read_text())
    pipx_agent.chmod(0o755)
    pipx_bin = operator_home / ".local/bin"
    pipx_bin.mkdir(parents=True)
    (pipx_bin / "roastpilot-agent").symlink_to(pipx_agent)
    groups = tmp_path / "groups"
    groups.write_text("dialout\n")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "OPENROUTER_API_KEY" and not key.startswith("ROASTPILOT_")
    } | {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_HOSTNAME": str(hostname),
        "FAKE_PIPX_STATE": str(tmp_path / "pipx-state"),
        "FAKE_GROUPS": str(groups),
        "ROASTPILOT_INSTALL_TEST_MODE": "1",
        "ROASTPILOT_INSTALL_TEST_ROOT": str(tmp_path / "root"),
        "ROASTPILOT_INSTALL_OS_RELEASE": str(os_release),
        "HOME": str(tmp_path / "home"),
        "FAKE_OPERATOR_HOME": str(operator_home),
        "USER": "operator",
    }
    return fake_bin, environment, log, hostname


def _run(
    environment: dict[str, str],
    *args: str,
    yes: bool = True,
    stdin: str | None = "input",
    script: Path = INSTALLER,
) -> subprocess.CompletedProcess[str]:
    allowed_installer_inputs = {
        "ROASTPILOT_INSTALL_TEST_MODE",
        "ROASTPILOT_INSTALL_TEST_ROOT",
        "ROASTPILOT_INSTALL_OS_RELEASE",
        "ROASTPILOT_INSTALL_WHEEL",
        "ROASTPILOT_INSTALL_API_KEY",
        "ROASTPILOT_INSTALL_SERIAL_PORT",
        "ROASTPILOT_INSTALL_AUDIO_DEVICE",
        "ROASTPILOT_INSTALL_ASSUME_YES",
    }
    environment = {
        key: value
        for key, value in environment.items()
        if key != "OPENROUTER_API_KEY"
        and (not key.startswith("ROASTPILOT_") or key in allowed_installer_inputs)
    }
    return subprocess.run(
        [
            "bash",
            str(script),
            *(["--yes"] if yes else []),
            "--serial-port",
            "/dev/ttyUSB0",
            "--audio-device",
            "USB mic",
            *args,
        ],
        env=environment,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def _delta(log: Path, start: int) -> list[str]:
    """Return fake-command records emitted after one installer invocation."""
    return log.read_text()[start:].splitlines()


def _pipx_state(path: Path, version: str, package: str) -> None:
    """Write the canonical pipx list representation accepted by install.sh."""
    path.write_text(
        json.dumps(
            {
                "venvs": {
                    "roastpilot-agent": {
                        "metadata": {
                            "main_package": {
                                "package_version": version,
                                "package_or_url": package,
                            }
                        }
                    }
                }
            }
        )
        + "\n"
    )


@pytest.mark.serial
def test_real_renderer_outputs_pass_the_installer_closed_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The committed renderer, not a retyped fixture, satisfies install.sh's validators."""

    def non_root_user(_: str) -> SimpleNamespace:
        """Supply the fake appliance operator without consulting host accounts."""
        return SimpleNamespace(pw_uid=1000)

    def non_root_group(_: str) -> SimpleNamespace:
        """Supply the fake appliance group without consulting host accounts."""
        return SimpleNamespace(gr_gid=1000)

    monkeypatch.setattr(pwd, "getpwnam", non_root_user)
    monkeypatch.setattr(grp, "getgrnam", non_root_group)
    inputs = ApplianceRenderInputs(
        port=8123,
        operator_user="operator",
        operator_group="operators",
        operator_home=Path("/home/operator"),
        db_path=Path("/var/lib/roastpilot-agent/roastpilot.sqlite3"),
        mcp_config_path=Path("/etc/roastpilot-agent/coffee-roaster-mcp.yaml"),
        model_dir=Path("/var/lib/roastpilot-agent/models"),
        serial_port=Path("/dev/ttyUSB0"),
        audio_device="USB mic",
    )
    rendered_files = {
        "env": render_env_file(inputs),
        "yaml": render_mcp_yaml(inputs),
        "unit": render_service_unit(inputs),
    }
    fixture_paths = {name: tmp_path / f"rendered-{name}" for name in rendered_files}
    for name, rendered in rendered_files.items():
        fixture_paths[name].write_bytes(rendered.encode())
        fixture_paths[name].chmod(0o600)

    installer_bytes = INSTALLER.read_bytes()
    trailing_main = b'main "$@"\n'
    assert installer_bytes.endswith(trailing_main)
    sourceable_installer = tmp_path / "install-functions.sh"
    sourceable_installer.write_bytes(installer_bytes.removesuffix(trailing_main))
    sourceable_installer.chmod(0o600)

    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("ROASTPILOT_")
    } | {"PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            """source "$1"
PORT="$2"
SERIAL_PORT="$3"
AUDIO_DEVICE="$4"
INVOKING_USER="$5"
INVOKING_GROUP="$6"
INVOKING_HOME="$7"
validate_rendered_env "$(< "$8")"
validate_rendered_yaml "$(< "$9")"
validate_rendered_unit "$(< "${10}")"
""",
            "installer-contract",
            str(sourceable_installer),
            "8123",
            "/dev/ttyUSB0",
            "USB mic",
            "operator",
            "operators",
            "/home/operator",
            str(fixture_paths["env"]),
            str(fixture_paths["yaml"]),
            str(fixture_paths["unit"]),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_installer_model_identity_is_tied_to_the_manifest() -> None:
    """Installer pin literals stay coupled to the Python manifest source of truth."""
    source = INSTALLER.read_text()
    assert REPO_ID in source
    assert REVISION in source
    for manifest_file in MANIFEST_FILES:
        assert manifest_file.relative_path in source
        assert manifest_file.sha256 in source


@pytest.mark.serial  # The subprocess installer shares fake PATH command state.
def test_installer_full_run_is_idempotent_and_keeps_secret_protected(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """T16/T17/T24: ordered fake effects, convergence, modes, and no key leak."""
    _, environment, log, _ = installer_harness
    key = "not-for-output"
    first = _run(environment | {"ROASTPILOT_INSTALL_API_KEY": key}, "--set-hostname", "roastpilot")
    assert first.returncode == 0, first.stderr
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    env_file = root / "etc/roastpilot-agent/roastpilot-agent.env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert (
        stat.S_IMODE((root / "etc/systemd/system/roastpilot-agent.service").stat().st_mode) == 0o644
    )
    assert (
        stat.S_IMODE((root / "etc/roastpilot-agent/coffee-roaster-mcp.yaml").stat().st_mode)
        == 0o644
    )
    assert key not in first.stdout + first.stderr + log.read_text()
    assert key in env_file.read_text()
    rendered_without_key = env_file.read_text().replace(
        f"OPENROUTER_API_KEY={key}", "OPENROUTER_API_KEY="
    )
    assert rendered_without_key.splitlines() == [
        "OPENROUTER_API_KEY=",
        "PORT=8000",
        "ROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3",
        "COFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml",
    ]
    commands = log.read_text().splitlines()
    assert [line.split(" ", 1)[0] for line in commands].index("apt-get") < [
        line.split(" ", 1)[0] for line in commands
    ].index("pipx")
    assert not any("systemctl <start> <roastpilot-agent>" in line for line in commands)
    var_dir = root / "var/lib/roastpilot-agent"
    assert stat.S_IMODE(var_dir.stat().st_mode) == 0o700
    assert any(
        line.startswith("chown <operator:operators> <-->") and ".roastpilot-env.fake" in line
        for line in commands
    )
    assert f"chown <operator:operators> <--> <{var_dir}>" in commands
    assert f"chmod <0700> <--> <{var_dir}>" in commands
    first_root_lock = next(
        index for index, line in enumerate(commands) if line == f"chmod <0700> <--> <{var_dir}>"
    )
    first_root_owner = next(
        index
        for index, line in enumerate(commands)
        if line == f"chown <root:root> <--> <{var_dir}>"
    )
    first_model_promotion = next(
        index
        for index, line in enumerate(commands)
        if ".roastpilot-model.fake" in line and line.startswith("tee ")
    )
    operator_unlock = max(
        index for index, line in enumerate(commands) if line == f"chmod <0700> <--> <{var_dir}>"
    )
    assert first_root_owner < first_root_lock < first_model_promotion < operator_unlock
    yaml_file = root / "etc/roastpilot-agent/coffee-roaster-mcp.yaml"
    unit_file = root / "etc/systemd/system/roastpilot-agent.service"
    prior_file = root / "var/lib/roastpilot-agent/prior-static-hostname"
    env_snapshot = (env_file.read_bytes(), stat.S_IMODE(env_file.stat().st_mode))
    yaml_snapshot = (yaml_file.read_bytes(), stat.S_IMODE(yaml_file.stat().st_mode))
    unit_snapshot = (unit_file.read_bytes(), stat.S_IMODE(unit_file.stat().st_mode))
    prior_snapshot = (prior_file.read_bytes(), stat.S_IMODE(prior_file.stat().st_mode))
    assert commands.count("usermod <-aG> <dialout,audio> <--> <operator>") == 1
    assert sum("MODEL_FETCH" in command for command in commands) == 1
    hostname_before = (root.parent / "hostname").read_bytes()
    second = _run(environment | {"ROASTPILOT_INSTALL_API_KEY": key}, "--set-hostname", "roastpilot")
    assert second.returncode == 0, second.stderr + log.read_text()
    assert (env_file.read_bytes(), stat.S_IMODE(env_file.stat().st_mode)) == env_snapshot
    assert (yaml_file.read_bytes(), stat.S_IMODE(yaml_file.stat().st_mode)) == yaml_snapshot
    assert (unit_file.read_bytes(), stat.S_IMODE(unit_file.stat().st_mode)) == unit_snapshot
    assert (prior_file.read_bytes(), stat.S_IMODE(prior_file.stat().st_mode)) == prior_snapshot
    assert (root.parent / "hostname").read_bytes() == hostname_before
    second_commands = log.read_text().splitlines()[len(commands) :]
    assert not any(
        "usermod" in line or "pipx <install>" in line or "pipx <uninstall>" in line
        for line in second_commands
    )
    assert not any("set-hostname" in line for line in second_commands)
    assert f"chown <root:root> <--> <{var_dir}>" in second_commands
    assert f"chown <operator:operators> <--> <{var_dir}>" in second_commands
    assert (
        sum(
            line.startswith("roastpilot-agent <appliance> <model> <install>")
            for line in second_commands
        )
        == 1
    )
    assert not any("systemctl <start>" in line for line in second_commands)


@pytest.mark.serial  # The subprocess installer shares fake PATH command state.
def test_preflight_failures_happen_before_privileged_commands(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """T18/T19/T20 and G8-G10: root, arch, and non-TTY fail closed."""
    fake_bin, environment, log, _ = installer_harness
    root_id = fake_bin / "id"
    root_id.unlink()
    root_id.write_text("#!/bin/sh\necho 0\n")
    root_id.chmod(0o755)
    root = _run(environment)
    assert root.returncode != 0 and not log.exists()
    root_id.write_text(
        '#!/bin/sh\ncase "$1" in -u) echo 1000 ;; -un) echo operator ;; '
        "-gn) echo operators ;; *) echo 'dialout audio' ;; esac\n"
    )
    arch = fake_bin / "uname"
    arch.unlink()
    arch.write_text("#!/bin/sh\necho x86_64\n")
    arch.chmod(0o755)
    rejected = _run(environment)
    assert rejected.returncode != 0
    allowed = _run(environment, "--allow-unsupported-arch", "--set-hostname", "roastpilot")
    assert allowed.returncode == 0
    no_tty = _run(environment, "--set-hostname", "roastpilot", yes=False, stdin=None)
    assert no_tty.returncode != 0


@pytest.mark.serial  # The subprocess installer shares fake PATH command state.
def test_hostname_consent_start_and_failure_abort_before_service_enable(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """T21/T23/T23a: hostname consent, opt-in start, and immediate abort."""
    _, environment, log, hostname = installer_harness
    rejected = _run(environment)
    assert rejected.returncode != 0
    assert "sudo" not in log.read_text()
    started = _run(environment, "--set-hostname", "roastpilot", "--start")
    assert started.returncode == 0
    assert hostname.read_text().strip() == "roastpilot"
    assert "systemctl <start> <roastpilot-agent>" in log.read_text()
    # A model failure aborts before rendered files and service enable are reached.
    failing = environment | {"FAKE_MODEL_FAIL": "1"}
    fake_agent = (
        Path(environment["FAKE_OPERATOR_HOME"])
        / ".local/pipx/venvs/roastpilot-agent/bin/roastpilot-agent"
    )
    source = fake_agent.read_text()
    fake_agent.write_text(
        source.replace(
            'if [ "$1 $2 $3" = "appliance model install" ]; then',
            'if [ "${FAKE_MODEL_FAIL:-}" = 1 ]; then exit 9; '
            'elif [ "$1 $2 $3" = "appliance model install" ]; then',
        )
    )
    failed_root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"]).parent / "failed-root"
    failure_start = len(log.read_text())
    failed = _run(
        failing | {"ROASTPILOT_INSTALL_TEST_ROOT": str(failed_root)}, "--set-hostname", "roastpilot"
    )
    assert failed.returncode != 0
    assert not (failed_root / "etc/systemd/system/roastpilot-agent.service").exists()
    failure_events = _delta(log, failure_start)
    assert not any("systemctl" in line for line in failure_events)
    assert not any(
        "<etc/roastpilot-agent/roastpilot-agent.env>" in line
        or "<etc/roastpilot-agent/coffee-roaster-mcp.yaml>" in line
        or "<etc/systemd/system/roastpilot-agent.service>" in line
        for line in failure_events
    )
    stage = failed_root / "tmp/roastpilot-install.fake"
    assert f"rm <-rf> <--> <{stage}>" in failure_events
    assert not stage.exists()


@pytest.mark.serial  # A truncated subprocess must not share fake command state.
def test_truncated_or_mutated_script_has_no_privileged_effect(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """T22/G11/G12: partial bytes and a removed fail-fast setting cannot install."""
    _, environment, log, _ = installer_harness
    partial = tmp_path / "partial.sh"
    partial.write_text(INSTALLER.read_text().rsplit('main "$@"', 1)[0])
    partial.chmod(0o755)
    result = subprocess.run(
        ["bash", str(partial)], env=environment, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert not log.exists()
    text = INSTALLER.read_text()
    assert "set -euo pipefail" in text
    assert text.rstrip().endswith('main "$@"')
    assert "run_privileged()" in text and "sudo --" in text
    assert "ROASTPILOT_INSTALL_TEST_ROOT" in text
    assert "pipx install --force" not in text
    escaped = _run(environment | {"ROASTPILOT_INSTALL_TEST_ROOT": "/tmp/root/../escape"})
    assert escaped.returncode != 0
    assert not log.exists()


@pytest.mark.serial  # Real subprocesses share one fake-command state and install root.
def test_rooted_staging_and_hostile_inputs_do_not_escape(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """Destination and inert-input guards reject escapes before install effects."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    completed = _run(environment, "--set-hostname", "roastpilot")
    assert completed.returncode == 0
    assert all(str(root) in line for line in log.read_text().splitlines() if "--output-dir" in line)

    escaped = _run(environment | {"ROASTPILOT_INSTALL_TEST_ROOT": str(root / ".." / "escape")})
    assert escaped.returncode != 0
    os_release = tmp_path / "hostile-release"
    sentinel = tmp_path / "sentinel"
    os_release.write_text(f"ID=$(touch {sentinel})\n")
    before = log.read_text()
    hostile = _run(
        environment | {"ROASTPILOT_INSTALL_OS_RELEASE": str(os_release)},
        "--set-hostname",
        "roastpilot",
    )
    assert hostile.returncode != 0 and not sentinel.exists()
    assert "sudo" not in log.read_text()[len(before) :]


@pytest.mark.serial  # A hostile destination is exercised in its own fake root.
def test_destination_symlink_aborts_before_final_writes(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A symlinked destination component never receives installer output."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "etc").symlink_to(outside, target_is_directory=True)
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    assert not list(outside.iterdir())
    assert not any("systemctl" in line for line in log.read_text().splitlines())


@pytest.mark.serial  # Hostile arguments run only through the isolated fake PATH.
def test_hostile_values_are_inert_and_rejected_before_writes(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """Caller identity, key, and wheel values cannot become shell syntax."""
    _, environment, log, _ = installer_harness
    sentinel = tmp_path / "sentinel"
    hostile = f"$(touch {sentinel})"
    rejected_key = _run(
        environment | {"HOME": hostile, "USER": "root", "ROASTPILOT_INSTALL_API_KEY": "bad\nkey"}
    )
    assert rejected_key.returncode != 0 and not log.exists() and not sentinel.exists()
    wheel = _run(environment, "--wheel", "--bad")
    assert wheel.returncode != 0 and (not log.exists() or "sudo" not in log.read_text())


@pytest.mark.serial  # This checks one complete subprocess staging lifecycle.
def test_staging_only_mutates_and_cleans_the_unique_directory(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """The installer never changes a shared tmp parent and removes its exact stage."""
    _, environment, log, _ = installer_harness
    assert _run(environment, "--set-hostname", "roastpilot").returncode == 0
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    stage_parent = root / "tmp"
    events = log.read_text()
    assert f"chown <operator:operators> <--> <{stage_parent}>" not in events
    assert f"chmod <0700> <--> <{stage_parent}>" not in events
    stage = stage_parent / "roastpilot-install.fake"
    assert f"chown <operator:operators> <--> <{stage}>" in events
    assert f"chmod <0700> <--> <{stage}>" in events
    assert not stage.exists()


@pytest.mark.serial  # Each mutation executes the real harness in a fresh test root.
@pytest.mark.parametrize(
    ("needle", "replacement", "arguments", "environment_key", "oracle"),
    [
        ('[[ "$(id -u)" != "0" ]] || die "never run pipx as root"', ":", (), "root", "proceeds"),
        (
            'if [[ "$(uname -m)" != "aarch64" && "$ALLOW_UNSUPPORTED_ARCH" != 1 ]]; then',
            "if false; then",
            (),
            "arch",
            "proceeds",
        ),
        (
            'if [[ "$INSTALL_ASSUME_YES" != 1 && '
            '"${ROASTPILOT_INSTALL_ASSUME_YES:-}" != 1 && ! -t 0 ]]; then',
            "if false; then",
            (),
            "non_tty",
            "proceeds",
        ),
        ("set -euo pipefail", "set -uo pipefail", (), "model_failure", "fails"),
        (
            "if ! id -nG \"$INVOKING_USER\" | tr ' ' '\\n' | grep -Fxq dialout "
            "|| ! id -nG \"$INVOKING_USER\" | tr ' ' '\\n' | grep -Fxq audio; then",
            "if true; then",
            (),
            "second_run",
            "usermod",
        ),
        (
            "printf '%s\\n' \"Installed: unit enabled; model verified.\"",
            "printf '%s\\n' \"$API_KEY\"",
            (),
            "secret",
            "secret_leaked",
        ),
    ],
)
def test_contract_mutation_oracles_detect_removed_guards(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
    tmp_path: Path,
    needle: str,
    replacement: str,
    arguments: tuple[str, ...],
    environment_key: str,
    oracle: str,
) -> None:
    """G8-G13/G23: each targeted script mutation changes a behavioural oracle."""
    fake_bin, environment, log, _ = installer_harness
    source = INSTALLER.read_text()
    assert source.count(needle) == 1
    mutated = tmp_path / f"mutated-{environment_key}.sh"
    mutated.write_text(source.replace(needle, replacement, 1))
    mutated.chmod(0o755)
    run_environment = environment
    yes = True
    if environment_key == "root":
        root_id = fake_bin / "id"
        root_id.unlink()
        root_id.write_text(
            '#!/bin/sh\ncase "$1" in -u) echo 0 ;; -un) echo operator ;; '
            "-gn) echo operators ;; *) echo 'dialout audio' ;; esac\n"
        )
        root_id.chmod(0o755)
    elif environment_key == "arch":
        arch = fake_bin / "uname"
        arch.unlink()
        arch.write_text("#!/bin/sh\necho x86_64\n")
        arch.chmod(0o755)
    elif environment_key == "non_tty":
        yes = False
    elif environment_key == "model_failure":
        agent = (
            Path(environment["FAKE_OPERATOR_HOME"])
            / ".local/pipx/venvs/roastpilot-agent/bin/roastpilot-agent"
        )
        agent.write_text(
            agent.read_text().replace(
                'if [ "$1 $2 $3" = "appliance model install" ]; then',
                'if [ "${FAKE_MODEL_FAIL:-}" = 1 ] && '
                '[ "$1 $2 $3" = "appliance model install" ]; then exit 9; '
                'elif [ "$1 $2 $3" = "appliance model install" ]; then',
            )
        )
        run_environment = environment | {"FAKE_MODEL_FAIL": "1"}
    elif environment_key == "second_run":
        assert _run(run_environment, "--set-hostname", "roastpilot").returncode == 0
        log.write_text("")
    elif environment_key == "secret":
        run_environment = environment | {"ROASTPILOT_INSTALL_API_KEY": "mutation-secret"}
    result = _run(
        run_environment,
        "--set-hostname",
        "roastpilot",
        *arguments,
        yes=yes,
        stdin=None if not yes else "input",
        script=mutated,
    )
    if oracle == "proceeds":
        assert result.returncode == 0
    elif oracle == "fails":
        assert result.returncode != 0
    elif oracle == "usermod":
        assert "usermod" in log.read_text()
    else:
        assert "mutation-secret" in result.stdout


@pytest.mark.serial  # The truncated copy must use an isolated fake-command log.
def test_trailing_main_mutation_is_detected_by_truncation_oracle(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """G12: moving a privileged effect above the trailing entry point is observable."""
    _, environment, log, _ = installer_harness
    source = INSTALLER.read_text()
    assert source.count('main "$@"') == 1
    mutated = tmp_path / "moved-main.sh"
    mutated.write_text(source.replace('main "$@"', 'run_privileged true\nmain "$@"'))
    truncated = tmp_path / "moved-main-truncated.sh"
    truncated.write_text(mutated.read_text().rsplit('main "$@"', 1)[0])
    result = subprocess.run(
        ["bash", str(truncated)], env=environment, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert not log.exists()  # Test mode's privilege seam is structurally root-free.


@pytest.mark.serial  # Each parametrized subprocess receives a fresh fake state.
@pytest.mark.parametrize(
    ("selector", "initial", "expected"),
    [
        ((), None, ("install",)),
        ((), ("default", "roastpilot-agent[pi]"), ()),
        (("--version", "1.2"), ("1.2", "roastpilot-agent[pi]==1.2"), ()),
        (("--version", "2.0"), ("1.2", "roastpilot-agent[pi]==1.2"), ("uninstall", "install")),
        (("--wheel", "WHEEL"), ("default", "WHEEL"), ()),
        (("--wheel", "OTHER"), ("default", "WHEEL"), ("uninstall", "install")),
    ],
)
def test_pipx_selector_deltas_are_isolated(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
    tmp_path: Path,
    selector: tuple[str, ...],
    initial: tuple[str, str] | None,
    expected: tuple[str, ...],
) -> None:
    """A1: each canonical pipx selector has an isolated exact mutation delta."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "agent.whl"
    other = tmp_path / "other.whl"
    wheel.write_text("wheel")
    other.write_text("other")
    selector = tuple(
        str(wheel) if item == "WHEEL" else str(other) if item == "OTHER" else item
        for item in selector
    )
    if initial is not None:
        version, package = initial
        package = str(wheel) if package == "WHEEL" else package
        _pipx_state(Path(environment["FAKE_PIPX_STATE"]), version, package)
    result = _run(environment, "--set-hostname", "roastpilot", *selector)
    assert result.returncode == 0, result.stderr
    pipx_actions = [
        line.split()[1].strip("<>")
        for line in log.read_text().splitlines()
        if line.startswith("pipx ")
    ]
    assert tuple(action for action in pipx_actions if action != "list") == expected


@pytest.mark.serial  # Failure behaviour needs an isolated fake command log.
@pytest.mark.parametrize("state", ["fail", "malformed", "bad-metadata"])
def test_invalid_pipx_state_fails_before_destructive_or_privileged_work(
    installer_harness: tuple[Path, dict[str, str], Path, Path], state: str
) -> None:
    """A1: inspection failures do not uninstall, render, or mutate privileged files."""
    _, environment, log, _ = installer_harness
    if state == "fail":
        environment = environment | {"FAKE_PIPX_LIST_FAIL": "1"}
    else:
        source = Path(environment["FAKE_PIPX_STATE"])
        source.write_text(
            "not-json" if state == "malformed" else '{"venvs":{"roastpilot-agent":{}}}\n'
        )
    result = _run(environment, "--set-hostname", "roastpilot", "--version", "2.0")
    assert result.returncode != 0
    events = log.read_text()
    assert "pipx <uninstall>" not in events
    assert "roastpilot-agent <appliance" not in events
    assert "systemctl" not in events


@pytest.mark.serial  # This executes a full ordered fake-install lifecycle.
def test_full_flow_has_exact_key_order_and_no_real_command_resolution(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """T16: all mutation-capable names resolve to fakes and core effects order."""
    fake_bin, environment, log, _ = installer_harness
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode == 0, result.stderr
    events = log.read_text().splitlines()
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    stage = root / "tmp/roastpilot-install.fake"
    assert "apt-get <install> <-y> <libportaudio2> <pipx> <avahi-daemon>" in events
    assert "pipx <install> <--> <roastpilot-agent[pi]>" in events
    assert any("roastpilot-agent <appliance> <model> <install>" in event for event in events)
    assert any("MODEL_FETCH" in event for event in events)
    assert (
        f"roastpilot-agent <appliance> <render> <--output-dir> <{stage}> <--port> <8000>"
        " <--operator-user> <operator> <--operator-group> <operators>"
        f" <--operator-home> <{environment['FAKE_OPERATOR_HOME']}> <--serial-port> </dev/ttyUSB0>"
        " <--audio-device> <USB mic> <--model-dir> </var/lib/roastpilot-agent/models>"
        " <--mcp-config-path> </etc/roastpilot-agent/coffee-roaster-mcp.yaml>"
        " <--db-path> </var/lib/roastpilot-agent/roastpilot.sqlite3>"
    ) in events
    assert "usermod <-aG> <dialout,audio> <--> <operator>" in events
    assert any(line.startswith("tee ") and ".prior-static-hostname.fake" in line for line in events)
    assert f"rm <-rf> <--> <{stage}>" in events

    def first(prefix: str) -> int:
        """Return the position of a recorded command prefix."""
        return next(i for i, line in enumerate(events) if line.startswith(prefix))

    assert (
        first("apt-get")
        < first("pipx <install>")
        < first("roastpilot-agent <appliance> <model> <install>")
    )
    assert first("roastpilot-agent <appliance> <render>") < first("systemctl <daemon-reload>")
    assert any("systemctl <enable> <roastpilot-agent>" in line for line in events)
    assert any("systemctl <enable> <--now> <avahi-daemon>" in line for line in events)
    assert not any("systemctl <start> <roastpilot-agent>" in line for line in events)
    for command in (
        "apt-get",
        "pipx",
        "roastpilot-agent",
        "install",
        "tee",
        "chmod",
        "chown",
        "mkdir",
        "mktemp",
        "rm",
        "usermod",
        "hostnamectl",
        "systemctl",
    ):
        resolved = subprocess.run(
            ["bash", "-c", f"command -v {command}"],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert Path(resolved.stdout.strip()).parent == fake_bin


@pytest.mark.serial  # Help executes the installer parser in a dedicated process.
def test_help_exits_before_required_arguments_or_preflight(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Help is a non-mutating successful parser path."""
    _, environment, log, _ = installer_harness
    result = subprocess.run(
        ["bash", str(INSTALLER), "--help"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0 and "Usage:" in result.stdout
    assert not log.exists()


@pytest.mark.serial
def test_repair_inputs_are_rejected_before_privileged_work(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """Repair guards reject unsafe API, device, port, and source inputs."""
    _, environment, log, _ = installer_harness
    source = tmp_path / "models"
    source.mkdir()
    for arguments in (
        ("--api-key", "secret"),
        ("--port", "0"),
        ("--port", "65536"),
        ("--serial-port", "/dev/tty\nUSB0"),
        ("--serial-port", "/dev/tty#USB0"),
        ("--serial-port", '/dev/tty"USB0'),
        ("--serial-port", "/dev/tty\\USB0"),
        ("--audio-device", "mic\rname"),
        ("--audio-device", "mic#name"),
        ("--audio-device", 'mic"name'),
        ("--audio-device", "mic\\name"),
        ("--from-dir", "relative"),
        ("--from-dir", str(source / "..")),
    ):
        result = _run(environment, "--set-hostname", "roastpilot", *arguments)
        assert result.returncode != 0
    linked = tmp_path / "linked-models"
    linked.symlink_to(source, target_is_directory=True)
    assert (
        _run(environment, "--set-hostname", "roastpilot", "--from-dir", str(linked)).returncode != 0
    )
    assert not log.exists()
    accepted = _run(environment, "--set-hostname", "roastpilot", "--from-dir", str(source))
    assert accepted.returncode == 0
    assert f"<--from-dir> <{source}>" in log.read_text()


@pytest.mark.serial
def test_unsupported_equals_form_api_key_never_echoes_its_value(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Unsupported equals-form keys fail without disclosing their value."""
    _, environment, log, _ = installer_harness
    sentinel = "do-not-echo-this-api-key"
    result = _run(environment, "--set-hostname", "roastpilot", f"--api-key={sentinel}")
    assert result.returncode != 0
    output = result.stdout + result.stderr + (log.read_text() if log.exists() else "")
    assert sentinel not in output


@pytest.mark.serial
def test_secret_template_and_no_key_summary_fail_closed(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """The placeholder path is redacted and malformed templates never install a destination."""
    _, environment, log, _ = installer_harness
    no_key = _run(environment, "--set-hostname", "roastpilot")
    assert no_key.returncode == 0
    assert "service was not started" in no_key.stdout
    assert "OpenRouter key" in no_key.stdout
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    env_file = root / "etc/roastpilot-agent/roastpilot-agent.env"
    key_install = _run(
        environment | {"ROASTPILOT_INSTALL_API_KEY": "kept-out-of-argv"},
        "--set-hostname",
        "roastpilot",
    )
    assert key_install.returncode == 0
    events = log.read_text().splitlines()
    assert not any(event.endswith(f"<{env_file}>") and "install" in event for event in events)
    assert any(".roastpilot-env.fake" in event for event in events)
    before = env_file.read_bytes()
    bad = _run(
        environment | {"FAKE_RENDERED_ENV": "OPENROUTER_API_KEY=\nOPENROUTER_API_KEY=two"},
        "--set-hostname",
        "roastpilot",
    )
    assert bad.returncode != 0 and env_file.read_bytes() == before
    assert "two" not in bad.stdout + bad.stderr + log.read_text()


@pytest.mark.serial
def test_rendered_unit_and_atomic_env_repairs_fail_before_live_writes(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Mutable renderer output cannot add service behaviour or replace a live key."""
    _, environment, log, _ = installer_harness
    key = "kept-private"
    assert (
        _run(
            environment | {"ROASTPILOT_INSTALL_API_KEY": key}, "--set-hostname", "roastpilot"
        ).returncode
        == 0
    )
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    env_file = root / "etc/roastpilot-agent/roastpilot-agent.env"
    before = env_file.read_bytes()
    for unit in (
        "[Service]\\nExecStart=/bin/true",
        "[Unit]\\nDescription=RoastPilot agent (native Pi appliance)\\n[Service]\\nUser=operator",
    ):
        start = len(log.read_text())
        result = _run(environment | {"FAKE_RENDERED_UNIT": unit}, "--set-hostname", "roastpilot")
        assert result.returncode != 0
        assert env_file.read_bytes() == before
        assert not any(
            str(root / "etc/systemd/system/roastpilot-agent.service") in item
            for item in _delta(log, start)
        )
    result = _run(
        environment | {"ROASTPILOT_INSTALL_API_KEY": "replacement", "FAKE_TEE_FAIL": "1"},
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0 and env_file.read_bytes() == before


@pytest.mark.serial
@pytest.mark.parametrize(
    ("field", "mutation"),
    [
        ("FAKE_RENDERED_ENV", "PORT=8000 #\\\nROASTPILOT_DB=/tmp/evil"),
        ("FAKE_RENDERED_ENV", "PORT=8000; ROASTPILOT_DB=/tmp/evil"),
        (
            "FAKE_RENDERED_UNIT",
            "KillMode=mixed #\\\nTimeoutStopSec=30",
        ),
        ("FAKE_RENDERED_UNIT", "ExecStart=/bin/true # hidden"),
    ],
)
def test_unit_and_env_comment_or_continuation_mutations_fail_closed(
    installer_harness: tuple[Path, dict[str, str], Path, Path], field: str, mutation: str
) -> None:
    """Unit/env validators never erase inline comments, semicolons, or continuations."""
    _, environment, log, _ = installer_harness
    start = len(log.read_text()) if log.exists() else 0
    result = _run(environment | {field: mutation}, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    assert not any("systemctl" in line for line in _delta(log, start))


@pytest.mark.serial
def test_existing_service_dropin_refuses_before_unit_write_or_enable(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """An existing drop-in is a fail-closed active-service-upgrade boundary."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    dropin = root / "etc/systemd/system/roastpilot-agent.service.d"
    dropin.mkdir(parents=True)
    start = len(log.read_text()) if log.exists() else 0
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    events = _delta(log, start)
    assert not any("roastpilot-unit" in line for line in events)
    assert not any("systemctl <enable> <roastpilot-agent>" in line for line in events)


@pytest.mark.serial
def test_model_digests_cover_stage_root_snapshot_and_destination(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Each pinned model file is checked before, during, and after promotion."""
    _, environment, log, _ = installer_harness
    assert _run(environment, "--set-hostname", "roastpilot").returncode == 0
    digests = [line for line in log.read_text().splitlines() if line.startswith("sha256sum ")]
    # Model promotion retains its six checks; captured env/YAML/unit and the
    # prior hostname each receive an atomic destination readback check.
    assert len(digests) == 10
    assert sum("roastpilot-install.fake/models/" in line for line in digests) == 2
    assert sum(".roastpilot-model.fake" in line for line in digests) == 2
    assert (
        sum(
            "/var/lib/roastpilot-agent/models/onnx/int8/" in line
            and "roastpilot-install" not in line
            and ".roastpilot-model" not in line
            for line in digests
        )
        == 2
    )
    assert sum("roastpilot-agent.env" in line for line in digests) == 1
    assert sum("coffee-roaster-mcp.yaml" in line for line in digests) == 1
    assert sum("roastpilot-agent.service" in line for line in digests) == 1
    assert sum("prior-static-hostname" in line for line in digests) == 1


@pytest.mark.serial
def test_root_temp_is_cleaned_after_atomic_write_failure(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A failed root-temp write removes both the temp and the staging directory."""
    _, environment, log, _ = installer_harness
    result = _run(environment | {"FAKE_TEE_FAIL": "1"}, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    events = log.read_text().splitlines()
    assert any("rm <-f> <-->" in line and ".roastpilot-model.fake" in line for line in events)
    assert not list(
        (root / "var/lib/roastpilot-agent/models/onnx/int8").glob(".roastpilot-model.*")
    )


@pytest.mark.serial
def test_failed_root_lock_restores_operator_access_in_cleanup(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A lock-transition chmod failure still restores the operator-owned parent."""
    _, environment, log, _ = installer_harness
    var_dir = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"]) / "var/lib/roastpilot-agent"
    result = _run(
        environment | {"FAKE_CHMOD_FAIL_TARGET": str(var_dir)}, "--set-hostname", "roastpilot"
    )
    assert result.returncode != 0
    events = log.read_text().splitlines()
    root_lock = next(
        i for i, line in enumerate(events) if line == f"chown <root:root> <--> <{var_dir}>"
    )
    failed_chmod = next(
        i for i, line in enumerate(events) if line == f"chmod <0700> <--> <{var_dir}>"
    )
    cleanup_unlock = next(
        i
        for i, line in enumerate(events[failed_chmod + 1 :], failed_chmod + 1)
        if line == f"chown <operator:operators> <--> <{var_dir}>"
    )
    assert root_lock < failed_chmod < cleanup_unlock


@pytest.mark.serial
def test_repaired_input_bounds_and_test_mode_are_fail_closed(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Unsafe renderer inputs are rejected before the root-free seam is reached."""
    _, environment, log, _ = installer_harness
    for args in (
        ("--port", "1023"),
        ("--serial-port", "/tmp/ttyUSB0"),
        ("--audio-device", " mic"),
        ("--audio-device", "mic "),
        ("--audio-device", "mic@@token"),
        ("--serial-port", "/dev/ttyéUSB0"),
        ("--audio-device", "microphoneé"),
    ):
        assert _run(environment, "--set-hostname", "roastpilot", *args).returncode != 0
    assert not log.exists()


@pytest.mark.serial
def test_production_mode_rejects_redirected_test_root_before_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """The closed root validator rejects production test-root redirection."""
    _, environment, log, _ = installer_harness
    redirected_root = tmp_path / "redirected-root"
    production_environment = environment | {"ROASTPILOT_INSTALL_TEST_ROOT": str(redirected_root)}
    production_environment.pop("ROASTPILOT_INSTALL_TEST_MODE")

    source = INSTALLER.read_text()
    assert source.count('main "$@"') == 1
    sourceable = tmp_path / "install-functions.sh"
    sourceable.write_text(source.rsplit('main "$@"', 1)[0])
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; validate_install_root', "bash", str(sourceable)],
        env=production_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "test destination is unavailable in production" in result.stderr
    assert not log.exists()
    assert not redirected_root.exists()


@pytest.mark.serial  # The fake renderer lifecycle is process-scoped.
def test_yaml_hash_without_preceding_whitespace_is_not_normalised_away(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A hash inside an unquoted scalar remains contract-significant."""
    _, environment, log, _ = installer_harness
    result = _run(
        environment | {"FAKE_RENDERED_YAML": "transport:\n  type: stdio\n  value: 2#9"},
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0
    assert "systemctl" not in (log.read_text() if log.exists() else "")


@pytest.mark.serial  # This source-only guard has no production command path.
def test_production_os_release_override_is_not_a_runtime_input() -> None:
    """Only root-free test mode may select an alternate os-release fixture."""
    source = INSTALLER.read_text()
    assert "local os_release=/etc/os-release" in source
    assert (
        'if [[ "${ROASTPILOT_INSTALL_TEST_MODE:-}" == "1" ]]; then\n        os_release=' in source
    )


@pytest.mark.serial
def test_pipx_null_path_and_malicious_path_are_fail_closed_or_ignored(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Explicit null metadata fails, while PATH cannot select the root-run executable."""
    fake_bin, environment, log, _ = installer_harness
    null = _run(
        environment | {"FAKE_PIPX_JSON": '{"venvs":{"roastpilot-agent":null}}'},
        "--set-hostname",
        "roastpilot",
    )
    assert null.returncode != 0 and "roastpilot-agent <appliance" not in log.read_text()
    log.unlink()
    shadow = fake_bin / "roastpilot-agent"
    shadow.write_text("#!/bin/sh\necho PATH_SHADOW >&2\nexit 97\n")
    shadow.chmod(0o755)
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode == 0, result.stderr
    assert "PATH_SHADOW" not in result.stdout + result.stderr + log.read_text()


@pytest.mark.serial
def test_hostname_and_identity_repairs_are_observable_through_fake_seam(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Hostname validation, post-set verification, and hostile identity data fail closed."""
    _, environment, log, _ = installer_harness
    for hostname in ("Roastpilot", "roastpilot-", "other"):
        assert _run(environment, "--set-hostname", hostname).returncode != 0
    installed = _run(environment, "--set-hostname", "roastpilot")
    assert installed.returncode == 0
    events = log.read_text().splitlines()
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    prior = root / "var/lib/roastpilot-agent/prior-static-hostname"
    assert prior.read_text() == "old-host\n"
    assert stat.S_IMODE(prior.stat().st_mode) == 0o600
    assert not any(line.endswith(f"<{prior}>") and "chown" in line for line in events)
    Path(environment["FAKE_HOSTNAME"]).write_text("old-host\n")
    verify = _run(environment | {"FAKE_HOSTNAME_VERIFY_FAIL": "1"}, "--set-hostname", "roastpilot")
    assert verify.returncode != 0 and "hostname verification failed" in verify.stderr
    hostile = _run(
        environment | {"FAKE_GETENT_RECORD": "operator:x:1000:1000::relative:/bin/sh"},
        "--set-hostname",
        "roastpilot",
    )
    assert hostile.returncode != 0
    assert any(
        line.startswith("getent <passwd> <operator>") for line in log.read_text().splitlines()
    )


@pytest.mark.serial  # The fake hostname and root-free parent are shared state.
def test_hostname_write_stays_inside_locked_parent_and_abort_recovers_access(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Prior-hostname persistence cannot follow a pre-existing symlink or unlock early."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    var_dir = root / "var/lib/roastpilot-agent"
    var_dir.mkdir(parents=True)
    outside = root.parent / "outside-prior"
    outside.write_text("unchanged\n")
    prior = var_dir / "prior-static-hostname"
    prior.symlink_to(outside)
    rejected = _run(environment, "--set-hostname", "roastpilot")
    assert rejected.returncode != 0
    assert outside.read_text() == "unchanged\n"
    assert prior.is_symlink()
    prior.unlink()
    log.write_text("")
    result = _run(environment | {"FAKE_HOSTNAME_VERIFY_FAIL": "1"}, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    events = log.read_text().splitlines()
    root_lock = next(
        i for i, line in enumerate(events) if line == f"chown <root:root> <--> <{var_dir}>"
    )
    hostname_set = next(
        i for i, line in enumerate(events) if line == "hostnamectl <set-hostname> <roastpilot>"
    )
    operator_unlock = next(
        i for i, line in enumerate(events) if line == f"chown <operator:operators> <--> <{var_dir}>"
    )
    assert root_lock < hostname_set < operator_unlock
    assert prior.read_text() == "wrong-host\n" and not prior.is_symlink()


@pytest.mark.serial  # Renderer mutation cases share the fake command seam.
def test_closed_renderer_contract_rejects_yaml_mutations_before_etc_writes(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Pinned Pi YAML rejects threshold, recording, and model-path drift."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    base = """transport:
  type: stdio
roaster:
  driver: hottop_kn8828b_2k_plus
  port: /dev/ttyUSB0
  baudrate: 115200
  temperature_unit: auto
  command_interval_seconds: 0.3
session:
  auto_t0_detection_enabled: true
  auto_t0_drop_threshold_c: 15.0
  ror_window_seconds: 60
  ror_min_sample_seconds: 10
first_crack:
  mode: audio
  repo_id: syamaner/coffee-first-crack-detection
  revision: b349a919c34b6130472da97c01817be404e4f629
  precision: int8
  local_model_dir: /var/lib/roastpilot-agent/models
  onnx_threads: 2
  confidence_threshold: 0.90
  min_positive_windows: 3
  confirmation_window_seconds: 30.0
  allow_manual_override: true
audio:
  source: microphone
  input_device: USB mic
  sample_rate: 16000
  wav_path: null
  replay_mode: realtime
  window_seconds: 10.0
  overlap: 0.3
  hop_seconds: null"""
    for replacement in (
        base.replace("confidence_threshold: 0.90", "confidence_threshold: 0.91"),
        base + "\nrecording:\n  enabled: true",
        base.replace(
            "local_model_dir: /var/lib/roastpilot-agent/models", "local_model_dir: /tmp/model"
        ),
        base.replace("port: /dev/ttyUSB0", "port: /dev/ttyUSB1"),
        base.replace("input_device: USB mic", "input_device: USB mic two"),
    ):
        start = len(log.read_text()) if log.exists() else 0
        result = _run(
            environment | {"FAKE_RENDERED_YAML": replacement}, "--set-hostname", "roastpilot"
        )
        assert result.returncode != 0
        assert not (root / "etc/roastpilot-agent/coffee-roaster-mcp.yaml").exists()
        assert not (
            root / "var/lib/roastpilot-agent/models/onnx/int8/model_quantized.onnx"
        ).exists()
        assert not any(str(root / "etc/roastpilot-agent") in line for line in _delta(log, start))


@pytest.mark.serial  # The fake model source is process-scoped fixture state.
def test_model_snapshot_digest_failure_precedes_live_destinations(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Unexpected model bytes cannot reach either model or /etc destinations."""
    _, environment, _, _ = installer_harness
    result = _run(environment | {"FAKE_MODEL_BYTES": "WRONG"}, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    assert not (root / "etc").exists()
    assert not (root / "var/lib/roastpilot-agent/models/onnx/int8/model_quantized.onnx").exists()


@pytest.mark.serial
def test_promoted_model_files_are_mode_0644(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Promoted model destinations are world-readable, root-owned appliance assets."""
    _, environment, _, _ = installer_harness
    assert _run(environment, "--set-hostname", "roastpilot").returncode == 0
    model_dir = (
        Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
        / "var/lib/roastpilot-agent/models/onnx/int8"
    )
    assert stat.S_IMODE((model_dir / "model_quantized.onnx").stat().st_mode) == 0o644
    assert stat.S_IMODE((model_dir / "preprocessor_config.json").stat().st_mode) == 0o644


@pytest.mark.serial  # API-key cases exercise the same subprocess harness.
def test_api_key_allowlist_rejects_environmentfile_metacharacters(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Only portable OpenRouter-token characters can enter EnvironmentFile."""
    _, environment, log, _ = installer_harness
    assert (
        _run(
            environment | {"ROASTPILOT_INSTALL_API_KEY": "sk-or-v1.valid_key:1"},
            "--set-hostname",
            "roastpilot",
        ).returncode
        == 0
    )
    for key in ("bad key", "bad'key", 'bad"key', "bad\\key", "bad;key", "bad$key"):
        assert (
            _run(
                environment | {"ROASTPILOT_INSTALL_API_KEY": key}, "--set-hostname", "roastpilot"
            ).returncode
            != 0
        )
    assert not any("bad" in line for line in log.read_text().splitlines())


@pytest.mark.serial  # Nondefault port uses the isolated renderer fake.
def test_nondefault_port_is_pinned_across_env_and_service(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A valid nondefault bind port reaches both closed service inputs."""
    _, environment, _, _ = installer_harness
    assert _run(environment, "--set-hostname", "roastpilot", "--port", "8123").returncode == 0
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    assert "PORT=8123\n" in (root / "etc/roastpilot-agent/roastpilot-agent.env").read_text()
    assert (
        "--host 0.0.0.0 --port ${PORT}"
        in (root / "etc/systemd/system/roastpilot-agent.service").read_text()
    )
