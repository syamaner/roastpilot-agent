"""Hardware-free behavioural contract tests for the Pi installer (#138, slice 3)."""
# ruff: noqa: E501

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

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
  id) if [ "${1:-}" = -u ]; then echo 1000; elif [ "${1:-}" = -gn ]; then echo operators; else echo 'dialout audio'; fi ;;
  uname) echo aarch64 ;;
  hostnamectl) if [ "${1:-}" = --static ]; then cat "$FAKE_HOSTNAME"; else [ "$1" = set-hostname ]; printf '%s\\n' "$2" > "$FAKE_HOSTNAME"; fi ;;
  pipx)
    if [ "${1:-}" = list ]; then
      [ -e "$FAKE_PIPX_STATE" ] && cat "$FAKE_PIPX_STATE" || printf '{"venvs": {}}\\n'
    elif [ "${1:-}" = install ]; then
      shift; [ "${1:-}" = -- ] && shift
      printf '%s\\n' '{"venvs": {"roastpilot-agent": {"metadata": {"main_package": {"package_version": "1.0", "package_or_url": "roastpilot-agent[pi]"}}}}}' > "$FAKE_PIPX_STATE"
    elif [ "${1:-}" = uninstall ]; then rm -f "$FAKE_PIPX_STATE"; fi ;;
  roastpilot-agent)
    if [ "$1 $2 $3" = "appliance model install" ]; then
      shift 3; while [ "$#" -gt 0 ]; do [ "$1" = --dest ] && { mkdir -p "$2/onnx/int8"; : > "$2/onnx/int8/model_quantized.onnx"; : > "$2/onnx/int8/preprocessor_config.json"; }; shift; done
    else
      out=''; while [ "$#" -gt 0 ]; do [ "$1" = --output-dir ] && { out="$2"; shift; }; shift; done
      mkdir -p "$out"; printf 'OPENROUTER_API_KEY=\\n' > "$out/roastpilot-agent.env"; : > "$out/coffee-roaster-mcp.appliance.yaml"; : > "$out/roastpilot-agent.service"
    fi ;;
  tee) [ "${1:-}" = -- ] && shift; mkdir -p "$(dirname "$1")"; cat > "$1" ;;
  install) mode=0644; [ "${1:-}" = -m ] && { mode="$2"; shift 2; }; [ "${1:-}" = -- ] && shift; cp "$1" "$2"; chmod "$mode" "$2" ;;
  mkdir) /bin/mkdir "$@" ;;
  chmod) [ "${2:-}" = -- ] && { mode="$1"; shift 2; /bin/chmod "$mode" "$@"; } || /bin/chmod "$@" ;;
  cp) /bin/cp "$@" ;;
  grep) /usr/bin/grep "$@" ;;
  tr) /usr/bin/tr "$@" ;;
  apt-get|usermod|systemctl) : ;;
esac
"""
    )
    fake.chmod(0o755)
    for name in (
        "sudo",
        "id",
        "uname",
        "hostnamectl",
        "pipx",
        "tee",
        "install",
        "mkdir",
        "chmod",
        "cp",
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
    environment = os.environ | {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_HOSTNAME": str(hostname),
        "FAKE_PIPX_STATE": str(tmp_path / "pipx-state"),
        "ROASTPILOT_INSTALL_ROOT": str(tmp_path / "root"),
        "ROASTPILOT_INSTALL_OS_RELEASE": str(os_release),
        "HOME": str(tmp_path / "home"),
        "USER": "operator",
    }
    return fake_bin, environment, log, hostname


def _run(
    environment: dict[str, str], *args: str, yes: bool = True, stdin: str | None = "input"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(INSTALLER),
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


@pytest.mark.serial
def test_installer_full_run_is_idempotent_and_keeps_secret_protected(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """T16/T17/T24: ordered fake effects, convergence, modes, and no key leak."""
    _, environment, log, _ = installer_harness
    key = "not-for-output"
    first = _run(environment, "--set-hostname", "roastpilot", "--api-key", key)
    assert first.returncode == 0, first.stderr
    root = Path(environment["ROASTPILOT_INSTALL_ROOT"])
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
    commands = log.read_text().splitlines()
    assert [line.split(" ", 1)[0] for line in commands].index("apt-get") < [
        line.split(" ", 1)[0] for line in commands
    ].index("pipx")
    assert not any("systemctl <start> <roastpilot-agent>" in line for line in commands)
    before = env_file.read_bytes()
    second = _run(environment, "--set-hostname", "roastpilot", "--api-key", key)
    assert second.returncode == 0, second.stderr + log.read_text()
    assert env_file.read_bytes() == before
    second_commands = log.read_text().splitlines()[len(commands) :]
    assert not any("usermod" in line or "pipx <install>" in line for line in second_commands)


@pytest.mark.serial
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
    root_id.write_text("#!/bin/sh\necho 1000\n")
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


@pytest.mark.serial
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
    fake_agent = Path(environment["PATH"].split(os.pathsep)[0]) / "roastpilot-agent"
    source = fake_agent.resolve().read_text()
    fake_agent.resolve().write_text(
        source.replace(
            'if [ "$1 $2 $3" = "appliance model install" ]; then',
            'if [ "${FAKE_MODEL_FAIL:-}" = 1 ]; then exit 9; elif [ "$1 $2 $3" = "appliance model install" ]; then',
        )
    )
    failed_root = Path(environment["ROASTPILOT_INSTALL_ROOT"]).parent / "failed-root"
    failed = _run(
        failing | {"ROASTPILOT_INSTALL_ROOT": str(failed_root)}, "--set-hostname", "roastpilot"
    )
    assert failed.returncode != 0
    assert not (failed_root / "etc/systemd/system/roastpilot-agent.service").exists()


@pytest.mark.serial
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
    assert "ROASTPILOT_INSTALL_ROOT" in text
    assert "pipx install --force" not in text
    escaped = _run(environment | {"ROASTPILOT_INSTALL_ROOT": "/tmp/root/../escape"})
    assert escaped.returncode != 0
    assert not log.exists()
