"""Hardware-free behavioural contract tests for the Pi installer (#138, slice 3)."""

from __future__ import annotations

import json
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
  id)
    if [ "${1:-}" = -u ]; then echo 1000
    elif [ "${1:-}" = -un ]; then echo operator
    elif [ "${1:-}" = -gn ]; then echo operators
    else cat "$FAKE_GROUPS"; fi ;;
  getent) printf 'operator:x:1000:1000::%s:/bin/sh\\n' "$FAKE_OPERATOR_HOME" ;;
  uname) echo aarch64 ;;
  hostnamectl)
    if [ "${1:-}" = --static ]; then cat "$FAKE_HOSTNAME"
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
          mkdir -p "$2/onnx/int8"; : > "$2/onnx/int8/model_quantized.onnx"
          : > "$2/onnx/int8/preprocessor_config.json"
        fi; }; shift; done
    else
      out=''; while [ "$#" -gt 0 ]; do [ "$1" = --output-dir ] && { out="$2"; shift; }; shift; done
      mkdir -p "$out"; printf 'OPENROUTER_API_KEY=\\n' > "$out/roastpilot-agent.env"
      : > "$out/coffee-roaster-mcp.appliance.yaml"; : > "$out/roastpilot-agent.service"
    fi ;;
  tee) [ "${1:-}" = -- ] && shift; mkdir -p "$(dirname "$1")"; cat > "$1" ;;
  install) mode=0644; [ "${1:-}" = -m ] && { mode="$2"; shift 2; }
    [ "${1:-}" = -- ] && shift; cp "$1" "$2"; chmod "$mode" "$2" ;;
  mkdir) /bin/mkdir "$@" ;;
  chmod) [ "${2:-}" = -- ] && { mode="$1"; shift 2; /bin/chmod "$mode" "$@"; } || /bin/chmod "$@" ;;
  mktemp) shift; [ "${1:-}" = -- ] && shift; dir="${1%XXXXXX}fake"
    mkdir -p "$dir"; printf '%s\\n' "$dir" ;;
  rm) /bin/rm "$@" ;;
  cp) /bin/cp "$@" ;;
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
    groups = tmp_path / "groups"
    groups.write_text("dialout\n")
    environment = os.environ | {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_HOSTNAME": str(hostname),
        "FAKE_PIPX_STATE": str(tmp_path / "pipx-state"),
        "FAKE_GROUPS": str(groups),
        "ROASTPILOT_INSTALL_ROOT": str(tmp_path / "root"),
        "ROASTPILOT_INSTALL_OS_RELEASE": str(os_release),
        "HOME": str(tmp_path / "home"),
        "FAKE_OPERATOR_HOME": str(tmp_path / "operator-home"),
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


@pytest.mark.serial  # The subprocess installer shares fake PATH command state.
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
    var_dir = root / "var/lib/roastpilot-agent"
    assert stat.S_IMODE(var_dir.stat().st_mode) == 0o700
    assert f"chown <operator:operators> <--> <{env_file}>" in commands
    assert f"chown <operator:operators> <--> <{var_dir}>" in commands
    assert f"chmod <0700> <--> <{var_dir}>" in commands
    yaml_file = root / "etc/roastpilot-agent/coffee-roaster-mcp.yaml"
    unit_file = root / "etc/systemd/system/roastpilot-agent.service"
    prior_file = root / "var/lib/roastpilot-agent/prior-static-hostname"
    env_snapshot = (env_file.read_bytes(), stat.S_IMODE(env_file.stat().st_mode))
    yaml_snapshot = (yaml_file.read_bytes(), stat.S_IMODE(yaml_file.stat().st_mode))
    unit_snapshot = (unit_file.read_bytes(), stat.S_IMODE(unit_file.stat().st_mode))
    prior_snapshot = (prior_file.read_bytes(), stat.S_IMODE(prior_file.stat().st_mode))
    assert commands.count("usermod <-aG> <dialout,audio> <--> <operator>") == 1
    assert (
        commands.count("MODEL_FETCH <" + str(root / "var/lib/roastpilot-agent/models") + ">") == 1
    )
    hostname_before = (root.parent / "hostname").read_bytes()
    second = _run(environment, "--set-hostname", "roastpilot", "--api-key", key)
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
    assert not any("MODEL_FETCH" in line or "set-hostname" in line for line in second_commands)
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
    fake_agent = Path(environment["PATH"].split(os.pathsep)[0]) / "roastpilot-agent"
    source = fake_agent.resolve().read_text()
    fake_agent.resolve().write_text(
        source.replace(
            'if [ "$1 $2 $3" = "appliance model install" ]; then',
            'if [ "${FAKE_MODEL_FAIL:-}" = 1 ]; then exit 9; '
            'elif [ "$1 $2 $3" = "appliance model install" ]; then',
        )
    )
    failed_root = Path(environment["ROASTPILOT_INSTALL_ROOT"]).parent / "failed-root"
    failure_start = len(log.read_text())
    failed = _run(
        failing | {"ROASTPILOT_INSTALL_ROOT": str(failed_root)}, "--set-hostname", "roastpilot"
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
    assert "ROASTPILOT_INSTALL_ROOT" in text
    assert "pipx install --force" not in text
    escaped = _run(environment | {"ROASTPILOT_INSTALL_ROOT": "/tmp/root/../escape"})
    assert escaped.returncode != 0
    assert not log.exists()


@pytest.mark.serial  # Real subprocesses share one fake-command state and install root.
def test_rooted_staging_and_hostile_inputs_do_not_escape(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """Destination and inert-input guards reject escapes before install effects."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_ROOT"])
    completed = _run(environment, "--set-hostname", "roastpilot")
    assert completed.returncode == 0
    assert all(str(root) in line for line in log.read_text().splitlines() if "--output-dir" in line)

    escaped = _run(environment | {"ROASTPILOT_INSTALL_ROOT": str(root / ".." / "escape")})
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
    root = Path(environment["ROASTPILOT_INSTALL_ROOT"])
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
    rejected_key = _run(environment | {"HOME": hostile, "USER": "root"}, "--api-key", "bad\nkey")
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
    root = Path(environment["ROASTPILOT_INSTALL_ROOT"])
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
        ("set -euo pipefail", "set -uo pipefail", (), "model_failure", "unit_written"),
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
            ("--api-key", "mutation-secret"),
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
        agent = fake_bin / "roastpilot-agent"
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
    elif oracle == "unit_written":
        assert (
            Path(environment["ROASTPILOT_INSTALL_ROOT"])
            / "etc/systemd/system/roastpilot-agent.service"
        ).exists()
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
    assert "sudo <--> <true>" in log.read_text()


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
    root = Path(environment["ROASTPILOT_INSTALL_ROOT"])
    stage = root / "tmp/roastpilot-install.fake"
    model_dir = root / "var/lib/roastpilot-agent/models"
    assert "sudo <--> <apt-get> <install> <-y> <libportaudio2> <pipx> <avahi-daemon>" in events
    assert "pipx <install> <--> <roastpilot-agent[pi]>" in events
    assert f"roastpilot-agent <appliance> <model> <install> <--dest> <{model_dir}>" in events
    assert f"MODEL_FETCH <{model_dir}>" in events
    assert (
        f"roastpilot-agent <appliance> <render> <--output-dir> <{stage}> <--port> <8000>"
        " <--operator-user> <operator> <--operator-group> <operators>"
        f" <--operator-home> <{environment['FAKE_OPERATOR_HOME']}> <--serial-port> </dev/ttyUSB0>"
        " <--audio-device> <USB mic>"
    ) in events
    assert "usermod <-aG> <dialout,audio> <--> <operator>" in events
    assert f"tee <--> <{root / 'var/lib/roastpilot-agent/prior-static-hostname'}>" in events
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
        "sudo",
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
