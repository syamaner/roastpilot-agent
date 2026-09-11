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
[ -z "${FAKE_SECRET_ENV_LOG:-}" ] || printf '%s OPENROUTER_API_KEY=<%s> OPENROUTER_API_KEY_FILE=<%s> OPENAI_API_KEY=<%s> ANTHROPIC_API_KEY=<%s> ROASTPILOT_API_KEY=<%s> ROASTPILOT_OPENROUTER_API_KEY=<%s> API_KEY=<%s>\\n' "$name" "${OPENROUTER_API_KEY-UNSET}" "${OPENROUTER_API_KEY_FILE-UNSET}" "${OPENAI_API_KEY-UNSET}" "${ANTHROPIC_API_KEY-UNSET}" "${ROASTPILOT_API_KEY-UNSET}" "${ROASTPILOT_OPENROUTER_API_KEY-UNSET}" "${API_KEY-UNSET}" >> "$FAKE_SECRET_ENV_LOG"
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
      if [ "${FAKE_HOSTNAME_QUERY_FAIL:-}" = 1 ] && [ ! -e "$FAKE_HOSTNAME_SET_MARKER" ]; then
        printf 'FAKE_HOSTNAME_QUERY_FAILURE\n' >> "$FAKE_LOG"
        exit 49
      fi
      if [ "${FAKE_HOSTNAME_VERIFY_FAIL:-}" = 1 ] && [ -e "$FAKE_HOSTNAME_SET_MARKER" ]; then
        printf 'FAKE_HOSTNAME_VERIFY_FAILURE\n' >> "$FAKE_LOG"
        echo wrong-host
      elif [ "${FAKE_HOSTNAME_VERIFY_QUERY_FAIL:-}" = 1 ] && [ -e "$FAKE_HOSTNAME_SET_MARKER" ]; then
        printf 'FAKE_HOSTNAME_VERIFY_QUERY_FAILURE\n' >> "$FAKE_LOG"
        exit 51
      else cat "$FAKE_HOSTNAME"; fi
    else
      [ "$1" = set-hostname ]
      printf '%s\\n' "$2" > "$FAKE_HOSTNAME"
      : > "$FAKE_HOSTNAME_SET_MARKER"
    fi ;;
  pipx)
    if [ -n "${FAKE_PIPX_ENV_LOG:-}" ]; then
      printf 'HOME=<%s> PIPX_HOME=<%s> PIPX_BIN_DIR=<%s> PIPX_DEFAULT_PYTHON=<%s>\\n' "${HOME-UNSET}" "${PIPX_HOME-UNSET}" "${PIPX_BIN_DIR-UNSET}" "${PIPX_DEFAULT_PYTHON-UNSET}" >> "$FAKE_PIPX_ENV_LOG"
    fi
    if [ "${1:-}" = environment ]; then
      [ "${2:-}" = --value ] && [ "${3:-}" = PIPX_HOME ] || exit 18
      if [ "${FAKE_PIPX_FAIL_FINAL_ROOT_PROBE:-}" = 1 ] && [ -e "$FAKE_PIPX_NORMAL_INSTALL_COUNT" ] && [ "$(cat "$FAKE_PIPX_NORMAL_INSTALL_COUNT")" = 1 ]; then
        printf 'FAKE_FINAL_ROOT_PROBE_FAILURE\\n' >> "$FAKE_LOG"
        printf '%s\\n' "$FAKE_PIPX_HOME/untrusted"
        exit 0
      fi
      printf '%s\\n' "$FAKE_PIPX_HOME"
    elif [ "${1:-}" = runpip ]; then
      venv="${2:-}"
      if [[ "$venv" == *-roastpilot-stage-* ]]; then
        [ "${FAKE_PIPX_FAIL_STAGE_VERIFY:-}" != 1 ] || exit 24
      elif [ -e "$FAKE_PIPX_NORMAL_INSTALL_COUNT" ] && [ "$(cat "$FAKE_PIPX_NORMAL_INSTALL_COUNT")" = 1 ]; then
        [ "${FAKE_PIPX_FAIL_FINAL_VERIFY:-}" != 1 ] || exit 24
      elif [ -e "$FAKE_PIPX_NORMAL_INSTALL_COUNT" ] && [ "$(cat "$FAKE_PIPX_NORMAL_INSTALL_COUNT")" -ge 2 ]; then
        [ "${FAKE_PIPX_FAIL_RESTORE_VERIFY:-}" != 1 ] || exit 24
      fi
      [ "${FAKE_PIPX_MCP_MISSING:-}" != 1 ] || exit 24
      case "${3:-}" in
        show)
          mcp_version="${FAKE_PIPX_MCP_VERSION:-0.2.0}"
          if [[ "$venv" == *-roastpilot-stage-* ]] && [ -n "${FAKE_PIPX_STAGE_MCP_VERSION:-}" ]; then
            mcp_version="$FAKE_PIPX_STAGE_MCP_VERSION"
            printf 'FAKE_STAGE_MCP_VERSION <%s>\\n' "$mcp_version" >> "$FAKE_LOG"
          elif [ -e "$FAKE_PIPX_NORMAL_INSTALL_COUNT" ] && [ "$(cat "$FAKE_PIPX_NORMAL_INSTALL_COUNT")" -ge 2 ] && [ -n "${FAKE_PIPX_RESTORE_MCP_VERSION:-}" ]; then
            mcp_version="$FAKE_PIPX_RESTORE_MCP_VERSION"
            printf 'FAKE_RESTORE_MCP_VERSION <%s>\\n' "$mcp_version" >> "$FAKE_LOG"
          fi
          if [ "${FAKE_PIPX_MCP_VERSION_MISSING:-}" = 1 ]; then
            printf 'FAKE_MCP_VERSION_MISSING\\n' >> "$FAKE_LOG"
            printf 'Name: coffee-roaster-mcp\\n'
          elif [ "${FAKE_PIPX_MCP_VERSION_DUPLICATE:-}" = 1 ]; then
            printf 'FAKE_MCP_VERSION_DUPLICATE\\n' >> "$FAKE_LOG"
            printf 'Name: coffee-roaster-mcp\\nVersion: %s\\nVersion: %s\\n' "$mcp_version" "$mcp_version"
          else
            printf 'Name: coffee-roaster-mcp\\nVersion: %s\\n' "$mcp_version"
          fi ;;
        freeze)
          if [ -n "${FAKE_PIPX_FREEZE_DIRECT_REFERENCE:-}" ]; then
            printf 'FAKE_PIPX_FREEZE_DIRECT_REFERENCE <%s>\\n' "$FAKE_PIPX_FREEZE_DIRECT_REFERENCE" >> "$FAKE_LOG"
            printf 'roastpilot-agent @ file://%s\\ncoffee-roaster-mcp==%s\\n' "$FAKE_PIPX_FREEZE_DIRECT_REFERENCE" "${FAKE_PIPX_MCP_VERSION:-0.2.0}"
          else
            printf 'roastpilot-agent==1.2\\ncoffee-roaster-mcp==%s\\n' "${FAKE_PIPX_MCP_VERSION:-0.2.0}"
          fi ;;
        wheel)
          [ "${FAKE_PIPX_FAIL_WHEELHOUSE:-}" != 1 ] || { printf 'FAKE_PIPX_WHEELHOUSE_FAILURE\\n' >> "$FAKE_LOG"; exit 54; }
          shift 3
          [ "${1:-}" = --wheel-dir ] || exit 55
          mkdir -p "$2"
          printf wheel > "$2/roastpilot_agent-1.2-py3-none-any.whl" ;;
      esac
    elif [ "${1:-}" = list ]; then
      if [ "${FAKE_PIPX_LIST_FAIL:-}" = 1 ]; then exit 17
      elif [ -n "${FAKE_PIPX_JSON:-}" ]; then cat "$FAKE_PIPX_JSON"
      elif [ -e "$FAKE_PIPX_STATE" ]; then cat "$FAKE_PIPX_STATE"
      else printf '{"venvs": {}}\\n'; fi
    elif [ "${1:-}" = install ]; then
      shift; suffix=''
      if [ "${1:-}" = --suffix ]; then suffix="$2"; shift 2; fi
      pip_args=''
      if [ "${1:-}" = --pip-args ]; then pip_args="$2"; shift 2; fi
      [ "${1:-}" = -- ] && shift
      package="$1"; version="${package##*==}"
      [ "$version" = "$package" ] && version=default
      if [ -n "$suffix" ]; then
        if [ "${FAKE_PIPX_FAIL_STAGE_AFTER_CREATE:-}" = 1 ]; then
          venv_bin="$FAKE_PIPX_HOME/venvs/roastpilot-agent$suffix/bin"
          mkdir -p "$venv_bin"
          printf 'FAKE_STAGE_PARTIAL_CREATION <%s>\n' "$venv_bin" >> "$FAKE_LOG"
          exit 52
        fi
        [ "${FAKE_PIPX_FAIL_STAGE_INSTALL:-}" != 1 ] || exit 25
        [ -z "${FAKE_DELETE_PRIOR_WHEEL:-}" ] || rm -f -- "$FAKE_DELETE_PRIOR_WHEEL"
      else
        if [ -n "$pip_args" ]; then
          case "$pip_args" in --no-index\\ --find-links=*) printf 'FAKE_OFFLINE_RESTORE <%s>\\n' "$pip_args" >> "$FAKE_LOG" ;; *) exit 56 ;; esac
        fi
        count=0; [ ! -e "$FAKE_PIPX_NORMAL_INSTALL_COUNT" ] || count=$(cat "$FAKE_PIPX_NORMAL_INSTALL_COUNT")
        count=$((count + 1)); printf '%s\\n' "$count" > "$FAKE_PIPX_NORMAL_INSTALL_COUNT"
        if [ "$count" = 1 ]; then [ "${FAKE_PIPX_FAIL_FINAL_INSTALL:-}" != 1 ] || exit 25
        else [ "${FAKE_PIPX_FAIL_RESTORE_INSTALL:-}" != 1 ] || exit 25; fi
      fi
      venv_bin="$FAKE_PIPX_HOME/venvs/roastpilot-agent$suffix/bin"
      mkdir -p "$venv_bin"
      cp "$FAKE_PIPX_MCP_TEMPLATE" "$venv_bin/coffee-roaster-mcp"
      chmod 0755 "$venv_bin/coffee-roaster-mcp"
      if [ -z "$suffix" ]; then
        printf '{"venvs":{"roastpilot-agent":{"metadata":' > "$FAKE_PIPX_STATE"
        printf '{"main_package":{"package_version":"%s",' "$version" >> "$FAKE_PIPX_STATE"
        printf '"package_or_url":"%s"}}}}}\\n' "$package" >> "$FAKE_PIPX_STATE"
      fi
    elif [ "${1:-}" = uninstall ]; then
      shift; [ "${1:-}" = -- ] && shift
      if [[ "${1:-}" == *-roastpilot-stage-* ]]; then
        [ "${FAKE_PIPX_FAIL_STAGE_CLEANUP:-}" != 1 ] || exit 26
      fi
      if [ "${1:-}" = roastpilot-agent ]; then
        [ "${FAKE_PIPX_FAIL_FRESH_CLEANUP:-}" != 1 ] || exit 53
        rm -f "$FAKE_PIPX_STATE"
      fi
    fi ;;
  roastpilot-agent)
    if [ "$1 $2 $3" = "appliance model install" ]; then
      shift 3; dest=''; from=''
      while [ "$#" -gt 0 ]; do
        [ "$1" = --dest ] && { dest="$2"; shift; }
        [ "$1" = --from-dir ] && { from="$2"; shift; }
        shift
      done
      if [ ! -e "$dest/onnx/int8/model_quantized.onnx" ]; then
        mkdir -p "$dest/onnx/int8"
        if [ -n "$from" ]; then
          cp "$from/onnx/int8/model_quantized.onnx" "$dest/onnx/int8/model_quantized.onnx"
          cp "$from/onnx/int8/preprocessor_config.json" "$dest/onnx/int8/preprocessor_config.json"
        else
          printf 'MODEL_FETCH <%s>\n' "$dest" >> "$FAKE_LOG"
          printf '%s' "${FAKE_MODEL_BYTES:-MODEL}" > "$dest/onnx/int8/model_quantized.onnx"
          printf CONFIG > "$dest/onnx/int8/preprocessor_config.json"
        fi
      fi
    else
      out=''; port=8000; audio=''; while [ "$#" -gt 0 ]; do
        [ "$1" = --output-dir ] && { out="$2"; shift; }
        [ "$1" = --port ] && { port="$2"; shift; }
        [ "$1" = --audio-device ] && { audio="$2"; shift; }
        case "$1" in --audio-device=*) audio="${1#--audio-device=}" ;; esac
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
  input_device: "$audio"
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
  tee) [ "${1:-}" = -- ] && shift; [ "${FAKE_TEE_FAIL:-}" != 1 ] || exit 19; [ "${FAKE_TEE_FAIL_TARGET:-}" != "$1" ] || exit 19; mkdir -p "$(dirname "$1")"; cat > "$1" ;;
  install) mode=0644; [ "${1:-}" = -m ] && { mode="$2"; shift 2; }
    [ "${1:-}" = -- ] && shift; cp "$1" "$2"; chmod "$mode" "$2" ;;
  test)
    if [ "${1:-}" = -d ] && [ "${FAKE_TEST_FAIL_D_PATH:-}" = "${!#}" ]; then
      count=0; [ ! -e "$FAKE_TEST_FAIL_D_COUNT_FILE" ] || count=$(cat "$FAKE_TEST_FAIL_D_COUNT_FILE")
      count=$((count + 1)); printf '%s\\n' "$count" > "$FAKE_TEST_FAIL_D_COUNT_FILE"
      [ "${FAKE_TEST_FAIL_D_ON_COUNT:-}" != "$count" ] || exit 48
    fi
    if [ -n "${FAKE_TEST_FAIL_PATH:-}" ] && [ "${!#}" = "$FAKE_TEST_FAIL_PATH" ]; then
      count=0; [ ! -e "$FAKE_TEST_FAIL_COUNT_FILE" ] || count=$(cat "$FAKE_TEST_FAIL_COUNT_FILE")
      count=$((count + 1)); printf '%s\\n' "$count" > "$FAKE_TEST_FAIL_COUNT_FILE"
      if [ -z "${FAKE_TEST_FAIL_ON_COUNT:-}" ] || [ "$count" = "$FAKE_TEST_FAIL_ON_COUNT" ]; then
        printf 'FAKE_TEST_FAILURE <%s>\\n' "${!#}" >> "$FAKE_LOG"
        exit 41
      fi
    fi
    if /bin/test "$@"; then
      if [ "${1:-}" = -d ] && [ "${!#}" = "${FAKE_MUTATE_AFTER_TEST_D_PATH:-}" ]; then
        [ -n "${FAKE_MUTATE_AFTER_TEST_D_TARGET:-}" ] || exit 46
        count=0; [ ! -e "$FAKE_MUTATE_AFTER_TEST_D_COUNT_FILE" ] || count=$(cat "$FAKE_MUTATE_AFTER_TEST_D_COUNT_FILE")
        count=$((count + 1)); printf '%s\n' "$count" > "$FAKE_MUTATE_AFTER_TEST_D_COUNT_FILE"
        if [ -z "${FAKE_MUTATE_AFTER_TEST_D_ON_COUNT:-}" ] || [ "$count" = "$FAKE_MUTATE_AFTER_TEST_D_ON_COUNT" ]; then
          /bin/rm -rf -- "$FAKE_MUTATE_AFTER_TEST_D_PATH"
          /bin/ln -s -- "$FAKE_MUTATE_AFTER_TEST_D_TARGET" "$FAKE_MUTATE_AFTER_TEST_D_PATH"
          printf 'FAKE_TEST_D_MUTATION <%s> <%s> <%s>\n' "$FAKE_MUTATE_AFTER_TEST_D_PATH" "$FAKE_MUTATE_AFTER_TEST_D_TARGET" "$count" >> "$FAKE_LOG"
        fi
      fi
      exit 0
    fi
    exit 1 ;;
  mkdir) /bin/mkdir "$@" ;;
  chmod) [ "${2:-}" = -- ] && { mode="$1"; shift 2;
    if [ "${FAKE_CHMOD_FAIL_TARGET:-}" = "$1" ]; then
      count=0; [ ! -e "$FAKE_CHMOD_FAIL_COUNT_FILE" ] || count=$(cat "$FAKE_CHMOD_FAIL_COUNT_FILE")
      count=$((count + 1)); printf '%s\n' "$count" > "$FAKE_CHMOD_FAIL_COUNT_FILE"
      [ -n "${FAKE_CHMOD_FAIL_ON_COUNT:-}" ] && [ "$count" != "$FAKE_CHMOD_FAIL_ON_COUNT" ] || exit 23
    fi
    /bin/chmod "$mode" "$@"; } || /bin/chmod "$@" ;;
  mktemp) is_dir=0; [ "${1:-}" = -d ] && { is_dir=1; shift; }; [ "${1:-}" = -- ] && shift; dir="${1%XXXXXX}fake"
    if [ "${FAKE_MKTEMP_TEMPLATE:-}" = "$1" ]; then
      dir="$FAKE_MKTEMP_RESULT"
      printf 'FAKE_MKTEMP_RESULT <%s>\\n' "$dir" >> "$FAKE_LOG"
    fi
    if [ "$is_dir" = 1 ]; then mkdir -p "$dir"; else mkdir -p "$(dirname "$dir")"; : > "$dir"; fi; printf '%s\\n' "$dir" ;;
  readlink)
    result=$(/usr/bin/readlink "$@")
    printf '%s\\n' "$result"
    if [ "${FAKE_MUTATE_AFTER_READLINK_PATH:-}" = "${!#}" ]; then
      /bin/rm -f -- "$FAKE_MUTATE_AFTER_READLINK_PATH"
      printf 'FAKE_READLINK_MUTATION <%s>\\n' "$FAKE_MUTATE_AFTER_READLINK_PATH" >> "$FAKE_LOG"
    fi ;;
  rm) [ -z "${FAKE_RM_FAIL_PATH:-}" ] || [ "${!#}" != "$FAKE_RM_FAIL_PATH" ] || exit 42; /bin/rm "$@" ;;
  cp) [ -z "${FAKE_CP_FAIL_PATH:-}" ] || [ "${!#}" != "$FAKE_CP_FAIL_PATH" ] || exit 43; /bin/cp "$@" ;;
  mv) /bin/mv "$@" ;;
  sha256sum)
    if [ "$#" = 0 ]; then cat >/dev/null; echo "content-digest  -"; exit 0; fi
    [ "${1:-}" = -- ] && shift
    if [ "${FAKE_SHA256_FAIL_PATH:-}" = "$1" ]; then
      count=0; [ ! -e "$FAKE_SHA256_FAIL_COUNT_FILE" ] || count=$(cat "$FAKE_SHA256_FAIL_COUNT_FILE")
      count=$((count + 1)); printf '%s\n' "$count" > "$FAKE_SHA256_FAIL_COUNT_FILE"
      [ "${FAKE_SHA256_FAIL_ON_COUNT:-}" != "$count" ] || { printf 'FAKE_SHA256_FAILURE <%s>\n' "$1" >> "$FAKE_LOG"; exit 50; }
    fi
    if [ "${FAKE_SHA256_BAD_PATH:-}" = "$1" ]; then printf 'FAKE_SHA256_CORRUPTION <%s>\n' "$1" >> "$FAKE_LOG"; echo "corrupt-digest  $1"; exit 0; fi
    content=$(cat "$1")
    case "$content" in
      MODEL) echo "022092cddd4c2cd740670c0a85786460699bc1b4f03e20f508182768d21545df  $1" ;;
      CONFIG) echo "8d04ba5a9c6fca5d39d0de2b1fd05ecf79deb589fbba279728bbebac39934231  $1" ;;
      *) echo "content-digest  $1" ;;
    esac ;;
  grep) /usr/bin/grep "$@" ;;
  tr) /usr/bin/tr "$@" ;;
  usermod) printf 'dialout audio\n' > "$FAKE_GROUPS" ;;
  chown)
    if [ "${FAKE_CHOWN_FAIL_TARGET:-}" = "${!#}" ]; then
      count=0; [ ! -e "$FAKE_CHOWN_FAIL_COUNT_FILE" ] || count=$(cat "$FAKE_CHOWN_FAIL_COUNT_FILE")
      count=$((count + 1)); printf '%s\n' "$count" > "$FAKE_CHOWN_FAIL_COUNT_FILE"
      [ -n "${FAKE_CHOWN_FAIL_ON_COUNT:-}" ] && [ "$count" != "$FAKE_CHOWN_FAIL_ON_COUNT" ] || exit 47
    fi ;;
  apt-get)
    if [ -n "${FAKE_APT_RESTORES_PIPX_PATH:-}" ]; then
      /bin/ln -s "$(/usr/bin/readlink -f -- "$0")" "$FAKE_APT_RESTORES_PIPX_PATH"
      printf 'FAKE_APT_RESTORED_PIPX <%s>\\n' "$FAKE_APT_RESTORES_PIPX_PATH" >> "$FAKE_LOG"
    fi ;;
  systemctl)
    if [ "${1:-}" = daemon-reload ] && [ -n "${FAKE_MUTATE_DROPIN_PATH:-}" ]; then
      /bin/mkdir -p -- "$FAKE_MUTATE_DROPIN_PATH"
      printf 'FAKE_DROPIN_MUTATION <%s>\n' "$FAKE_MUTATE_DROPIN_PATH" >> "$FAKE_LOG"
    fi
    if [ "${1:-}" = enable ] && [ -n "${FAKE_MUTATE_SYMLINK_PATH:-}" ]; then
      [ -n "${FAKE_MUTATE_SYMLINK_TARGET:-}" ] || exit 45
      /bin/rm -f -- "$FAKE_MUTATE_SYMLINK_PATH"
      /bin/ln -s -- "$FAKE_MUTATE_SYMLINK_TARGET" "$FAKE_MUTATE_SYMLINK_PATH"
      printf 'FAKE_SYMLINK_MUTATION <%s> <%s>\n' "$FAKE_MUTATE_SYMLINK_PATH" "$FAKE_MUTATE_SYMLINK_TARGET" >> "$FAKE_LOG"
    fi
    if [ "${1:-}" = daemon-reload ] && [ -n "${FAKE_DAEMON_RELOAD_FAIL_ON:-}" ]; then
      count=0; [ ! -e "$FAKE_DAEMON_RELOAD_COUNTER" ] || count=$(cat "$FAKE_DAEMON_RELOAD_COUNTER")
      count=$((count + 1)); printf '%s\n' "$count" > "$FAKE_DAEMON_RELOAD_COUNTER"
      [ "$count" != "$FAKE_DAEMON_RELOAD_FAIL_ON" ] || exit 44
    fi
    [ "${FAKE_SYSTEMCTL_FAIL:-}" != "${1:-}" ] || exit 31
    [ "${FAKE_AGENT_ENABLE_FAIL:-}" != 1 ] || [ "${1:-}" != enable ] || [ "${2:-}" != roastpilot-agent ] || exit 31
    if [ "${1:-}" = show ]; then
      [ "${2:-}" = -p ] && [ "${3:-}" = ActiveState ] && [ "${4:-}" = --value ] || exit 32
      if [ -n "${FAKE_SERVICE_STATE_SEQUENCE:-}" ]; then
        count=0; [ ! -e "$FAKE_SERVICE_STATE_COUNTER" ] || count=$(cat "$FAKE_SERVICE_STATE_COUNTER")
        count=$((count + 1)); printf '%s\\n' "$count" > "$FAKE_SERVICE_STATE_COUNTER"
        /usr/bin/sed -n "${count}p" "$FAKE_SERVICE_STATE_SEQUENCE"
      elif [ "${FAKE_SERVICE_STATE+x}" = x ]; then printf '%s\\n' "$FAKE_SERVICE_STATE"; else echo inactive; fi
      exit 0
    fi
    ;;
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
        "readlink",
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
        "test",
    ):
        (fake_bin / name).symlink_to(fake)
    agent = fake_bin / "roastpilot-agent"
    agent.write_text(fake.read_text())
    agent.chmod(0o755)
    operator_home = tmp_path / "operator-home"
    pipx_home = operator_home / ".local/share/pipx"
    pipx_agent = pipx_home / "venvs/roastpilot-agent/bin/roastpilot-agent"
    pipx_agent.parent.mkdir(parents=True)
    pipx_agent.write_text(fake.read_text())
    pipx_agent.chmod(0o755)
    pipx_mcp = pipx_agent.with_name("coffee-roaster-mcp")
    pipx_mcp.write_text("#!/bin/sh\nexit 0\n")
    pipx_mcp.chmod(0o755)
    pipx_mcp_template = tmp_path / "coffee-roaster-mcp-template"
    pipx_mcp_template.write_text(pipx_mcp.read_text())
    pipx_mcp_template.chmod(0o755)
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
        "FAKE_MUTATE_AFTER_TEST_D_COUNT_FILE": str(tmp_path / "test-d-mutation-count"),
        "FAKE_TEST_FAIL_COUNT_FILE": str(tmp_path / "test-fail-count"),
        "FAKE_TEST_FAIL_D_COUNT_FILE": str(tmp_path / "test-d-fail-count"),
        "FAKE_CHMOD_FAIL_COUNT_FILE": str(tmp_path / "chmod-fail-count"),
        "FAKE_CHOWN_FAIL_COUNT_FILE": str(tmp_path / "chown-fail-count"),
        "FAKE_SHA256_FAIL_COUNT_FILE": str(tmp_path / "sha256-fail-count"),
        "FAKE_HOSTNAME_SET_MARKER": str(tmp_path / "hostname-set"),
        "FAKE_PIPX_STATE": str(tmp_path / "pipx-state"),
        "FAKE_PIPX_MCP_TEMPLATE": str(pipx_mcp_template),
        "FAKE_PIPX_NORMAL_INSTALL_COUNT": str(tmp_path / "pipx-normal-install-count"),
        "FAKE_PIPX_HOME": str(pipx_home),
        "FAKE_PIPX_ENV_LOG": str(tmp_path / "pipx-environment.log"),
        "FAKE_GROUPS": str(groups),
        "FAKE_SERVICE_STATE_COUNTER": str(tmp_path / "service-state-counter"),
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
    xtrace: bool = False,
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
    ambient_secret_names = {
        "ROASTPILOT_API_KEY",
        "ROASTPILOT_OPENROUTER_API_KEY",
    }
    allow_ambient_secret = environment.get("FAKE_ALLOW_AMBIENT_SECRET") == "1"
    environment = {
        key: value
        for key, value in environment.items()
        if (key != "OPENROUTER_API_KEY" or allow_ambient_secret)
        and (
            not key.startswith("ROASTPILOT_")
            or key in allowed_installer_inputs
            or (allow_ambient_secret and key in ambient_secret_names)
        )
    }
    return subprocess.run(
        [
            "bash",
            *(["-x"] if xtrace else []),
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


def _has_service_mutation(events: list[str]) -> bool:
    """Return whether events contain service work beyond the read-only active probe."""
    return any(
        line.startswith("systemctl ")
        and line != "systemctl <show> <-p> <ActiveState> <--value> <roastpilot-agent>"
        for line in events
    )


def _has_roastpilot_agent_lifecycle_mutation(events: list[str]) -> bool:
    """Return whether events mutate either supported RoastPilot unit spelling."""
    return any(
        event.startswith("systemctl ")
        and any(f"<{unit}>" in event for unit in ("roastpilot-agent", "roastpilot-agent.service"))
        and (
            any(
                f"<{operation}>" in event
                for operation in ("start", "stop", "restart", "try-restart", "kill", "disable")
            )
            or ("<enable>" in event and "<--now>" in event)
        )
        for event in events
    )


def test_roastpilot_lifecycle_matcher_catches_service_unit_spelling() -> None:
    """Lifecycle checks must recognise the systemd service-name spelling too."""
    for operation in ("start", "stop", "restart", "try-restart", "kill", "disable"):
        assert _has_roastpilot_agent_lifecycle_mutation(
            [f"systemctl <{operation}> <roastpilot-agent.service>"]
        )
    assert _has_roastpilot_agent_lifecycle_mutation(
        ["systemctl <enable> <--now> <roastpilot-agent>"]
    )
    assert _has_roastpilot_agent_lifecycle_mutation(
        ["systemctl <enable> <--now> <roastpilot-agent.service>"]
    )
    assert not _has_roastpilot_agent_lifecycle_mutation(
        ["systemctl <enable> <roastpilot-agent.service>"]
    )


@pytest.mark.serial
def test_absent_pipx_is_installed_by_apt_before_first_application_install(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A fresh Pi reaches its first fake-only install after apt supplies pipx."""
    fake_bin, environment, log, _ = installer_harness
    pipx = fake_bin / "pipx"
    pipx.unlink()
    result = _run(
        environment | {"FAKE_APT_RESTORES_PIPX_PATH": str(pipx)},
        "--set-hostname",
        "roastpilot",
    )
    events = log.read_text().splitlines()
    assert result.returncode == 0, result.stderr
    marker = f"FAKE_APT_RESTORED_PIPX <{pipx}>"
    assert marker in events
    assert events.index(
        "apt-get <install> <-y> <libportaudio2> <pipx> <avahi-daemon>"
    ) < events.index(marker)
    assert any(event.startswith("pipx <install>") for event in events)


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
    assert "install failed after hostname change" not in first.stderr
    assert "unit may remain enabled" not in first.stderr
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
    assert commands.index("apt-get <install> <-y> <libportaudio2> <pipx> <avahi-daemon>") < next(
        index for index, command in enumerate(commands) if command.startswith("pipx <install>")
    )
    assert not any("systemctl <start> <roastpilot-agent>" in line for line in commands)
    var_dir = root / "var/lib/roastpilot-agent"
    assert stat.S_IMODE(var_dir.stat().st_mode) == 0o700
    assert any(
        line.startswith("chown <operator:operators> <-->") and ".roastpilot-env.fake" in line
        for line in commands
    )
    assert f"chown <--no-dereference> <operator:operators> <--> <{var_dir}>" in commands
    assert f"chmod <0700> <--> <{var_dir}>" in commands
    etc_dir = root / "etc/roastpilot-agent"
    assert commands.count(f"chown <--no-dereference> <root:operators> <--> <{etc_dir}>") == 1
    assert commands.count(f"chmod <0750> <--> <{etc_dir}>") == 1
    first_root_lock = next(
        index for index, line in enumerate(commands) if line == f"chmod <0700> <--> <{var_dir}>"
    )
    first_root_owner = next(
        index
        for index, line in enumerate(commands)
        if line == f"chown <--no-dereference> <root:root> <--> <{var_dir}>"
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
    assert f"chown <--no-dereference> <root:root> <--> <{var_dir}>" in second_commands
    assert f"chown <--no-dereference> <operator:operators> <--> <{var_dir}>" in second_commands
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
    no_tty = _run(environment, "--set-hostname", "roastpilot", yes=False, stdin="")
    assert no_tty.returncode != 0


@pytest.mark.serial  # The subprocess installer shares fake PATH command state.
def test_hostname_consent_start_and_failure_abort_before_service_enable(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """T21/T23/T23a: hostname consent, opt-in start, and immediate abort."""
    _, environment, log, hostname = installer_harness
    rejected = _run(environment)
    assert rejected.returncode != 0
    assert not any(
        event.startswith(("apt-get ", "pipx ", "roastpilot-agent ", "systemctl <enable>"))
        for event in log.read_text().splitlines()
    )
    started = _run(environment, "--set-hostname", "roastpilot", "--start")
    assert started.returncode == 0
    assert hostname.read_text().strip() == "roastpilot"
    assert "systemctl <start> <roastpilot-agent>" in log.read_text()
    # A model failure aborts before rendered files and service enable are reached.
    failing = environment | {"FAKE_MODEL_FAIL": "1"}
    fake_agent = (
        Path(environment["FAKE_OPERATOR_HOME"])
        / ".local/share/pipx/venvs/roastpilot-agent/bin/roastpilot-agent"
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
    assert not any(
        line.startswith("roastpilot-agent <appliance> <render>") for line in failure_events
    )
    assert not _has_service_mutation(failure_events)
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
    escaped = _run(
        environment | {"ROASTPILOT_INSTALL_TEST_ROOT": str(tmp_path / "root" / ".." / "escape")}
    )
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
    assert not any(
        event.startswith(("apt-get ", "pipx ", "roastpilot-agent ", "systemctl <enable>"))
        for event in _delta(log, len(before))
    )


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
    assert not _has_service_mutation(log.read_text().splitlines())


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
    assert wheel.returncode != 0
    assert not log.exists() or not any(
        event.startswith(("apt-get ", "pipx ", "roastpilot-agent ", "systemctl <enable>"))
        for event in log.read_text().splitlines()
    )


@pytest.mark.serial
def test_wheel_with_a_symlinked_parent_is_rejected_before_installer_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A wheel must be canonical, not merely a regular file through a symlinked parent."""
    _, environment, log, _ = installer_harness
    real_parent = tmp_path / "real-wheel-parent"
    real_parent.mkdir()
    (real_parent / "roastpilot-agent.whl").write_text("wheel")
    linked_parent = tmp_path / "linked-wheel-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    result = _run(
        environment,
        "--set-hostname",
        "roastpilot",
        "--wheel",
        str(linked_parent / "roastpilot-agent.whl"),
    )
    assert result.returncode != 0 and "wheel path must be canonical" in result.stderr
    assert not any(
        event.startswith(("apt-get ", "pipx ", "roastpilot-agent ", "systemctl "))
        for event in log.read_text().splitlines()
    )


@pytest.mark.serial
@pytest.mark.parametrize(
    ("identity", "value"), [("FAKE_ID_USER", "Operator"), ("FAKE_ID_GROUP", "operators!")]
)
def test_unsafe_operator_identity_fails_before_installer_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], identity: str, value: str
) -> None:
    """Invalid invoking account names fail before account lookup or mutable work."""
    _, environment, log, _ = installer_harness
    result = _run(environment | {identity: value}, "--set-hostname", "roastpilot")
    assert result.returncode != 0 and "unsafe operator identity" in result.stderr
    events = log.read_text().splitlines()
    assert not any(
        line.startswith(("getent ", "apt-get ", "pipx ", "roastpilot-agent ", "systemctl "))
        for line in events
    )


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
            'if [[ " $groups " != *" dialout "* || " $groups " != *" audio "* ]]; then',
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
    """G8-G13: each targeted script mutation changes a behavioural oracle."""
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
            / ".local/share/pipx/venvs/roastpilot-agent/bin/roastpilot-agent"
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
        stdin="" if not yes else "input",
        script=mutated,
    )
    if oracle == "proceeds":
        assert result.returncode == 0
    elif oracle == "fails":
        assert result.returncode != 0
        assert any(
            line.startswith("roastpilot-agent <appliance> <render>")
            for line in log.read_text().splitlines()
        )
    elif oracle == "usermod":
        assert "usermod <-aG> <dialout,audio> <--> <operator>" in log.read_text().splitlines()
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
    mutated.write_text(
        source.replace('main "$@"', 'run_privileged apt-get install -- g12-oracle\nmain "$@"')
    )
    truncated = tmp_path / "moved-main-truncated.sh"
    truncated.write_text(mutated.read_text().rsplit('main "$@"', 1)[0])
    result = subprocess.run(
        ["bash", str(truncated)], env=environment, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert log.read_text().splitlines() == ["apt-get <install> <--> <g12-oracle>"]
    unmutated = tmp_path / "unmutated-truncated.sh"
    unmutated.write_text(source.rsplit('main "$@"', 1)[0])
    assert (
        subprocess.run(
            ["bash", str(unmutated)], env=environment, text=True, capture_output=True
        ).returncode
        == 0
    )
    assert log.read_text().splitlines() == ["apt-get <install> <--> <g12-oracle>"]


@pytest.mark.serial  # Each parametrized subprocess receives a fresh fake state.
@pytest.mark.parametrize(
    ("selector", "initial", "expected"),
    [
        ((), None, ("install",)),
        ((), ("default", "roastpilot-agent[pi]"), ()),
        (("--version", "1.2"), ("1.2", "roastpilot-agent[pi]==1.2"), ()),
        (
            ("--version", "2.0"),
            ("1.2", "roastpilot-agent[pi]==1.2"),
            ("install", "uninstall", "install", "uninstall"),
        ),
        (("--wheel", "WHEEL"), ("default", "WHEEL_PI"), ()),
        (
            ("--wheel", "OTHER"),
            ("1.2", "WHEEL_PI"),
            ("install", "uninstall", "install", "uninstall"),
        ),
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
    wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
    other = tmp_path / "roastpilot_agent-2.0-py3-none-any.whl"
    wheel.write_text("wheel")
    other.write_text("other")
    selector = tuple(
        str(wheel) if item == "WHEEL" else str(other) if item == "OTHER" else item
        for item in selector
    )
    if initial is not None:
        version, package = initial
        package = (
            str(wheel)
            if package == "WHEEL"
            else f"{wheel}[pi]"
            if package == "WHEEL_PI"
            else package
        )
        _pipx_state(Path(environment["FAKE_PIPX_STATE"]), version, package)
    result = _run(environment, "--set-hostname", "roastpilot", *selector)
    assert result.returncode == 0, result.stderr
    pipx_actions = [
        line.split()[1].strip("<>")
        for line in log.read_text().splitlines()
        if line.startswith("pipx ")
    ]
    assert (
        tuple(action for action in pipx_actions if action in {"install", "uninstall"}) == expected
    )


@pytest.mark.serial  # pipx child environments are process-local fake state.
def test_pipx_children_use_only_the_resolved_invoking_home(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Every pipx subprocess receives the resolved home without ambient routing."""
    _, environment, log, _ = installer_harness
    hostile_environment = environment | {
        "HOME": "/hostile/home",
        "PIPX_HOME": "/hostile/pipx-home",
        "PIPX_BIN_DIR": "/hostile/pipx-bin",
        "PIPX_DEFAULT_PYTHON": "/hostile/python",
    }
    result = _run(hostile_environment, "--set-hostname", "roastpilot")
    assert result.returncode == 0, result.stderr
    records = Path(environment["FAKE_PIPX_ENV_LOG"]).read_text().splitlines()
    expected_home = environment["FAKE_OPERATOR_HOME"]
    expected_record = (
        f"HOME=<{expected_home}> PIPX_HOME=<UNSET> PIPX_BIN_DIR=<UNSET> PIPX_DEFAULT_PYTHON=<UNSET>"
    )
    assert records and all(record == expected_record for record in records)
    pipx_events = [line for line in log.read_text().splitlines() if line.startswith("pipx ")]
    assert len(records) == len(pipx_events)
    assert [line.split()[1] for line in pipx_events].count("<environment>") == 2


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
    assert not _has_service_mutation(events.splitlines())


@pytest.mark.serial  # Executable provenance mutates the isolated fake command path.
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
        " <--audio-device=USB mic> <--model-dir> </var/lib/roastpilot-agent/models>"
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
    source_int8 = source / "onnx/int8"
    source_int8.mkdir(parents=True)
    (source_int8 / "model_quantized.onnx").write_text("MODEL")
    (source_int8 / "preprocessor_config.json").write_text("CONFIG")
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
    assert not any(
        event.startswith(("apt-get ", "pipx ", "roastpilot-agent ", "systemctl "))
        for event in log.read_text().splitlines()
    )
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
        "[Service]\nExecStart=/bin/true",
        "[Unit]\nDescription=RoastPilot agent (native Pi appliance)\n[Service]\nUser=operator",
    ):
        start = len(log.read_text())
        result = _run(environment | {"FAKE_RENDERED_UNIT": unit}, "--set-hostname", "roastpilot")
        assert result.returncode != 0
        assert "rendered unit violates appliance contract" in result.stderr
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
        ("FAKE_RENDERED_UNIT", "# standalone continuation\\\nKillMode=mixed"),
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
    if mutation.startswith("# standalone"):
        assert "rendered input contains unsafe inline mutation" in result.stderr
    assert not _has_service_mutation(_delta(log, start))


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
    assert not any(
        line.startswith(
            ("apt-get ", "pipx ", "roastpilot-agent ", "tee ", "mv ", "chown ", "chmod ")
        )
        for line in events
    )


@pytest.mark.serial
def test_dropin_recheck_blocks_enablement_after_configuration_promotion(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A drop-in created after promotion is rejected before any enablement command."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    dropin = root / "etc/systemd/system/roastpilot-agent.service.d"
    result = _run(
        environment | {"FAKE_MUTATE_DROPIN_PATH": str(dropin)}, "--set-hostname", "roastpilot"
    )
    assert result.returncode != 0 and "service drop-ins are not permitted" in result.stderr
    events = log.read_text().splitlines()
    assert f"FAKE_DROPIN_MUTATION <{dropin}>" in events
    assert "systemctl <daemon-reload>" in events
    assert not any(event.startswith("systemctl <enable>") for event in events)


@pytest.mark.serial
def test_managed_etc_recheck_rejects_a_swapped_parent_before_configuration_promotion(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A swap after the privileged directory probe cannot receive ownership or config writes."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    etc = root / "etc/roastpilot-agent"
    attacker = tmp_path / "attacker-controlled-etc"
    attacker.mkdir()
    result = _run(
        environment
        | {
            "FAKE_MUTATE_AFTER_TEST_D_PATH": str(etc),
            "FAKE_MUTATE_AFTER_TEST_D_TARGET": str(attacker),
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0 and "managed configuration directory is unsafe" in result.stderr
    events = log.read_text().splitlines()
    assert f"FAKE_TEST_D_MUTATION <{etc}> <{attacker}> <1>" in events
    assert f"test <!> <-L> <{etc}>" in events
    assert not any(
        event.startswith(("chown ", "chmod ", "tee ", "mv ")) and f"<{attacker}>" in event
        for event in events
    )
    assert not any(
        event.startswith(("tee ", "mv ")) and "roastpilot-agent.env" in event for event in events
    )


@pytest.mark.serial
def test_managed_state_recheck_rejects_a_swapped_parent_before_ownership_change(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A state-parent swap is rejected before it receives ownership or mode changes."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    var_dir = root / "var/lib/roastpilot-agent"
    attacker = tmp_path / "attacker-controlled-state"
    attacker.mkdir()
    result = _run(
        environment
        | {
            "FAKE_MUTATE_AFTER_TEST_D_PATH": str(var_dir),
            "FAKE_MUTATE_AFTER_TEST_D_TARGET": str(attacker),
        },
        "--set-hostname",
        "roastpilot",
    )
    assert (
        result.returncode != 0 and f"managed state directory is unsafe: {var_dir}" in result.stderr
    )
    events = log.read_text().splitlines()
    assert f"FAKE_TEST_D_MUTATION <{var_dir}> <{attacker}> <1>" in events
    assert f"test <!> <-L> <{var_dir}>" in events
    assert not any(
        event.startswith(("chown ", "chmod ", "tee ", "mv ")) and f"<{attacker}>" in event
        for event in events
    )


@pytest.mark.serial
def test_model_parent_recheck_rejects_a_swapped_parent_before_promotion(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A model-parent swap is rejected before ownership, mode, or model promotion work."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    model_parent = root / "var/lib/roastpilot-agent/models/onnx"
    attacker = tmp_path / "attacker-controlled-models"
    attacker.mkdir()
    result = _run(
        environment
        | {
            "FAKE_MUTATE_AFTER_TEST_D_PATH": str(model_parent),
            "FAKE_MUTATE_AFTER_TEST_D_TARGET": str(attacker),
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0 and f"model directory is unsafe: {model_parent}" in result.stderr
    events = log.read_text().splitlines()
    assert f"FAKE_TEST_D_MUTATION <{model_parent}> <{attacker}> <1>" in events
    assert f"test <!> <-L> <{model_parent}>" in events
    assert not any(
        event.startswith(("chown ", "chmod ", "tee ", "mv ")) and f"<{attacker}>" in event
        for event in events
    )


@pytest.mark.serial
@pytest.mark.parametrize("boundary", ["etc", "var", "model"])
def test_directory_recheck_blocks_post_ownership_swap_before_mode_or_promotion(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path, boundary: str
) -> None:
    """The second directory probe catches a swap after ownership but before mode changes."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    path, ownership = {
        "etc": (root / "etc/roastpilot-agent", "chown <--no-dereference> <root:operators>"),
        "var": (root / "var/lib/roastpilot-agent", "chown <--no-dereference> <root:root>"),
        "model": (
            root / "var/lib/roastpilot-agent/models/onnx",
            "chown <--no-dereference> <root:operators>",
        ),
    }[boundary]
    attacker = tmp_path / f"attacker-{boundary}"
    attacker.mkdir()
    result = _run(
        environment
        | {
            "FAKE_MUTATE_AFTER_TEST_D_PATH": str(path),
            "FAKE_MUTATE_AFTER_TEST_D_TARGET": str(attacker),
            "FAKE_MUTATE_AFTER_TEST_D_ON_COUNT": "2",
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0 and str(path) in result.stderr
    events = log.read_text().splitlines()
    ownership_index = next(
        i for i, event in enumerate(events) if event == f"{ownership} <--> <{path}>"
    )
    mutation_index = next(
        i
        for i, event in enumerate(events)
        if event == f"FAKE_TEST_D_MUTATION <{path}> <{attacker}> <2>"
    )
    assert ownership_index < mutation_index
    assert f"test <!> <-L> <{path}>" in events
    assert not any(event.startswith("chmod ") and f"<{attacker}>" in event for event in events)
    assert not any(
        i > mutation_index
        and event.startswith(("tee ", "mv ", "roastpilot-agent <appliance> <model>"))
        for i, event in enumerate(events)
    )


@pytest.mark.serial
@pytest.mark.parametrize(
    ("phase", "probe_count"), [("before-ownership", "4"), ("after-ownership", "5")]
)
def test_successful_var_unlock_rechecks_after_each_privileged_boundary(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
    tmp_path: Path,
    phase: str,
    probe_count: str,
) -> None:
    """A swapped state directory cannot reach the unlock chmod on either side of chown."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    var_dir = root / "var/lib/roastpilot-agent"
    attacker = tmp_path / f"attacker-unlock-{phase}"
    attacker.mkdir()
    result = _run(
        environment
        | {
            "FAKE_MUTATE_AFTER_TEST_D_PATH": str(var_dir),
            "FAKE_MUTATE_AFTER_TEST_D_TARGET": str(attacker),
            "FAKE_MUTATE_AFTER_TEST_D_ON_COUNT": probe_count,
        },
        "--set-hostname",
        "roastpilot",
    )
    assert (
        result.returncode != 0 and f"managed state directory is unsafe: {var_dir}" in result.stderr
    )
    events = log.read_text().splitlines()
    mutation = next(
        i
        for i, event in enumerate(events)
        if event == f"FAKE_TEST_D_MUTATION <{var_dir}> <{attacker}> <{probe_count}>"
    )
    operator_chowns = [
        i
        for i, event in enumerate(events)
        if event == f"chown <--no-dereference> <operator:operators> <--> <{var_dir}>"
    ]
    if phase == "before-ownership":
        assert not operator_chowns
    else:
        assert operator_chowns == [next(i for i in operator_chowns if i < mutation)]
    assert f"test <!> <-L> <{var_dir}>" in events
    assert not any(
        i > mutation and event == f"chmod <0700> <--> <{var_dir}>" for i, event in enumerate(events)
    )
    assert not any(
        event.startswith(("chmod ", "tee ", "mv ")) and f"<{attacker}>" in event for event in events
    )


@pytest.mark.serial
@pytest.mark.parametrize(
    ("boundary", "owner", "mode", "probe_count"),
    [
        ("var/lib/roastpilot-agent", "operator:operators", "0700", "5"),
        ("etc/roastpilot-agent", "root:operators", "0750", "6"),
    ],
)
def test_cleanup_rechecks_after_ownership_before_restoring_mode(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
    tmp_path: Path,
    boundary: str,
    owner: str,
    mode: str,
    probe_count: str,
) -> None:
    """A cleanup swap after chown is visible, skips chmod, and does not halt remaining rollback."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    path = root / boundary
    attacker = tmp_path / f"attacker-cleanup-{path.name}"
    attacker.mkdir()
    result = _run(
        environment
        | {
            "FAKE_HOSTNAME_VERIFY_FAIL": "1",
            "FAKE_MUTATE_AFTER_TEST_D_PATH": str(path),
            "FAKE_MUTATE_AFTER_TEST_D_TARGET": str(attacker),
            "FAKE_MUTATE_AFTER_TEST_D_ON_COUNT": probe_count,
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode == 1
    assert "rollback incomplete; manual reconciliation required" in result.stderr
    events = log.read_text().splitlines()
    chown = max(
        i
        for i, event in enumerate(events)
        if event == f"chown <--no-dereference> <{owner}> <--> <{path}>"
    )
    mutation = next(
        i
        for i, event in enumerate(events)
        if event == f"FAKE_TEST_D_MUTATION <{path}> <{attacker}> <{probe_count}>"
    )
    assert chown < mutation
    assert f"test <!> <-L> <{path}>" in events
    assert not any(
        i > mutation and event == f"chmod <{mode}> <--> <{path}>" for i, event in enumerate(events)
    )
    assert any(event == "systemctl <daemon-reload>" for event in events)
    assert any(
        event.startswith("rm <-rf>") and "roastpilot-config-rollback" in event for event in events
    )


@pytest.mark.serial
@pytest.mark.parametrize(
    ("boundary", "owner", "mode", "entry_probe", "entry_failure"),
    [
        ("var/lib/roastpilot-agent", "operator:operators", "0700", "4", "missing"),
        ("var/lib/roastpilot-agent", "operator:operators", "0700", "4", "symlink"),
        ("etc/roastpilot-agent", "root:operators", "0750", "5", "missing"),
        ("etc/roastpilot-agent", "root:operators", "0750", "5", "symlink"),
    ],
)
def test_locked_cleanup_entry_failure_names_the_exact_manual_reconciliation_target(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
    boundary: str,
    owner: str,
    mode: str,
    entry_probe: str,
    entry_failure: str,
) -> None:
    """A missing locked directory at cleanup reports its intended restoration state."""
    _, environment, log, _ = installer_harness
    path = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"]) / boundary
    injection = (
        {"FAKE_TEST_FAIL_D_PATH": str(path), "FAKE_TEST_FAIL_D_ON_COUNT": entry_probe}
        if entry_failure == "missing"
        else {
            "FAKE_MUTATE_AFTER_TEST_D_PATH": str(path),
            "FAKE_MUTATE_AFTER_TEST_D_TARGET": str(path.parent),
            "FAKE_MUTATE_AFTER_TEST_D_ON_COUNT": entry_probe,
        }
    )
    result = _run(
        environment | injection | {"FAKE_HOSTNAME_VERIFY_FAIL": "1"},
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode == 1
    assert (
        f"install failed: reconcile {path} manually (expected directory owned by {owner} with mode {mode})"
        in result.stderr
    )
    assert "rollback incomplete; manual reconciliation required" in result.stderr
    events = log.read_text().splitlines()
    if entry_failure == "symlink":
        assert f"FAKE_TEST_D_MUTATION <{path}> <{path.parent}> <{entry_probe}>" in events
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
@pytest.mark.parametrize(
    ("boundary", "owner", "mode", "failure"),
    [
        ("var/lib/roastpilot-agent", "operator:operators", "0700", "chown"),
        ("var/lib/roastpilot-agent", "operator:operators", "0700", "chmod"),
        ("etc/roastpilot-agent", "root:operators", "0750", "chown"),
        ("etc/roastpilot-agent", "root:operators", "0750", "chmod"),
    ],
)
def test_locked_cleanup_chown_and_chmod_failures_keep_specific_manual_diagnostics(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
    boundary: str,
    owner: str,
    mode: str,
    failure: str,
) -> None:
    """Both locked parents retain their owner/mode diagnostics on cleanup failure."""
    _, environment, log, _ = installer_harness
    path = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"]) / boundary
    injection = (
        {"FAKE_CHOWN_FAIL_TARGET": str(path), "FAKE_CHOWN_FAIL_ON_COUNT": "2"}
        if failure == "chown"
        else {"FAKE_CHMOD_FAIL_TARGET": str(path), "FAKE_CHMOD_FAIL_ON_COUNT": "2"}
    )
    result = _run(
        environment | injection | {"FAKE_HOSTNAME_VERIFY_FAIL": "1"},
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode == 1
    message = (
        f"install failed: restore {path} ownership to {owner} manually"
        if failure == "chown"
        else f"install failed: restore {path} mode {mode} manually"
    )
    assert message in result.stderr
    assert "rollback incomplete; manual reconciliation required" in result.stderr
    assert not _has_roastpilot_agent_lifecycle_mutation(log.read_text().splitlines())


@pytest.mark.serial
def test_hostname_change_failure_keeps_hostname_and_names_manual_recovery_file(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A later failure records manual hostname recovery without changing it back automatically."""
    _, environment, log, hostname = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    prior = root / "var/lib/roastpilot-agent/prior-static-hostname"
    result = _run(environment | {"FAKE_SYSTEMCTL_FAIL": "enable"}, "--set-hostname", "roastpilot")
    assert result.returncode == 31
    assert hostname.read_text().strip() == "roastpilot"
    assert f"restore manually from {prior}" in result.stderr
    assert prior.read_text() == "old-host\n"
    assert "systemctl <enable> <--now> <avahi-daemon>" in log.read_text().splitlines()


@pytest.mark.serial
def test_successful_rollback_preserves_the_original_non_one_failure_status(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A successful rollback returns the original fake systemctl failure status unchanged."""
    _, environment, log, _ = installer_harness
    result = _run(environment | {"FAKE_SYSTEMCTL_FAIL": "enable"}, "--set-hostname", "roastpilot")
    assert result.returncode == 31
    assert "systemctl <enable> <--now> <avahi-daemon>" in log.read_text().splitlines()


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
@pytest.mark.parametrize(
    ("target", "diagnostic"),
    [
        ("model-temporary", "model promotion digest mismatch"),
        ("model-destination", "model destination digest mismatch"),
        ("env", "atomic destination digest mismatch"),
        ("yaml", "atomic destination digest mismatch"),
        ("unit", "atomic destination digest mismatch"),
    ],
)
def test_digest_corruption_is_detected_and_cleaned_up(
    installer_harness: tuple[Path, dict[str, str], Path, Path], target: str, diagnostic: str
) -> None:
    """Each privileged digest comparison rejects an exact fake-corrupted path."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    paths = {
        "model-temporary": root
        / "var/lib/roastpilot-agent/models/onnx/int8/.roastpilot-model.fake",
        "model-destination": root
        / "var/lib/roastpilot-agent/models/onnx/int8/model_quantized.onnx",
        "env": root / "etc/roastpilot-agent/roastpilot-agent.env",
        "yaml": root / "etc/roastpilot-agent/coffee-roaster-mcp.yaml",
        "unit": root / "etc/systemd/system/roastpilot-agent.service",
    }
    secret = "digest-secret"
    path = paths[target]
    result = _run(
        environment | {"ROASTPILOT_INSTALL_API_KEY": secret, "FAKE_SHA256_BAD_PATH": str(path)},
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0 and diagnostic in result.stderr
    events = log.read_text().splitlines()
    assert f"FAKE_SHA256_CORRUPTION <{path}>" in events
    assert secret not in result.stdout + result.stderr + log.read_text()
    assert not _has_roastpilot_agent_lifecycle_mutation(events)
    assert any(event.startswith("rm <-rf>") and "roastpilot-install" in event for event in events)


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
@pytest.mark.parametrize(("failure", "status"), [("chown", 47), ("chmod", 23)])
def test_validated_stage_directory_failures_are_cleanup_accounted(
    installer_harness: tuple[Path, dict[str, str], Path, Path], failure: str, status: int
) -> None:
    """A stage ownership or mode failure still removes the validated staging directory."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    stage = root / "tmp/roastpilot-install.fake"
    injection = (
        {"FAKE_CHOWN_FAIL_TARGET": str(stage)}
        if failure == "chown"
        else {"FAKE_CHMOD_FAIL_TARGET": str(stage)}
    )
    result = _run(environment | injection, "--set-hostname", "roastpilot")
    assert result.returncode == status
    events = log.read_text().splitlines()
    assert (
        f"chown <operator:operators> <--> <{stage}>"
        if failure == "chown"
        else f"chmod <0700> <--> <{stage}>"
    ) in events
    assert f"rm <-rf> <--> <{stage}>" in events
    assert not stage.exists()


@pytest.mark.serial
def test_unvalidated_stage_path_is_reported_without_recursive_cleanup(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A mktemp prefix mismatch remains retained rather than becoming an rm -rf target."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    stage_parent = root / "tmp"
    unexpected = stage_parent / "roastpilot-install.attacker/nested"
    result = _run(
        environment
        | {
            "FAKE_MKTEMP_TEMPLATE": str(stage_parent / "roastpilot-install.XXXXXX"),
            "FAKE_MKTEMP_RESULT": str(unexpected),
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode == 1
    assert f"retained staging directory at {unexpected}" in result.stderr
    events = log.read_text().splitlines()
    assert f"FAKE_MKTEMP_RESULT <{unexpected}>" in events
    assert f"rm <-rf> <--> <{unexpected}>" not in events


@pytest.mark.serial
@pytest.mark.parametrize(
    ("template", "diagnostic"),
    [
        (".roastpilot-model.XXXXXX", "retained untrusted model temporary"),
        (".roastpilot-env.XXXXXX", "retained untrusted roastpilot-env temporary"),
    ],
)
def test_untrusted_root_temporary_is_not_registered_for_cleanup(
    installer_harness: tuple[Path, dict[str, str], Path, Path], template: str, diagnostic: str
) -> None:
    """Model and config mktemp prefix failures retain rather than recursively remove an arbitrary path."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    parent = (
        root / "var/lib/roastpilot-agent/models/onnx/int8"
        if "model" in template
        else root / "etc/roastpilot-agent"
    )
    unexpected = parent / f"{template.removesuffix('XXXXXX')}attacker/nested"
    result = _run(
        environment
        | {
            "FAKE_MKTEMP_TEMPLATE": str(parent / template),
            "FAKE_MKTEMP_RESULT": str(unexpected),
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode == 1 and f"{diagnostic} at {unexpected}" in result.stderr
    events = log.read_text().splitlines()
    assert f"FAKE_MKTEMP_RESULT <{unexpected}>" in events
    assert f"mktemp <--> <{parent / template}>" in events
    assert f"rm <-f> <--> <{unexpected}>" not in events
    assert f"mv <-f> <--> <{unexpected}>" not in events


@pytest.mark.serial
def test_failed_registered_temporary_removal_requires_manual_reconciliation_without_secret_leak(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A retained secret-bearing temporary fails cleanup but does not stop later rollback work."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    temporary = root / "etc/roastpilot-agent/.roastpilot-env.fake"
    secret = "must-not-appear-in-cleanup-output"
    result = _run(
        environment
        | {
            "ROASTPILOT_INSTALL_API_KEY": secret,
            "FAKE_TEE_FAIL_TARGET": str(temporary),
            "FAKE_RM_FAIL_PATH": str(temporary),
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode == 1
    assert f"retained temporary at {temporary}" in result.stderr
    assert "rollback incomplete; manual reconciliation required" in result.stderr
    events = log.read_text().splitlines()
    assert f"tee <--> <{temporary}>" in events
    assert f"rm <-f> <--> <{temporary}>" in events
    assert temporary.exists()
    assert any(event == "systemctl <daemon-reload>" for event in events)
    assert any(
        event.startswith("rm <-rf>") and "roastpilot-config-rollback" in event for event in events
    )
    assert secret not in result.stdout + result.stderr + log.read_text()


@pytest.mark.serial
@pytest.mark.parametrize("retained", ["stage", "restore"])
def test_retained_cleanup_directory_requires_manual_reconciliation_and_continues(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path, retained: str
) -> None:
    """Each cleanup directory removal is accounted for without exposing staged secrets."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
    secret = "cleanup-directory-secret"
    wheel.write_text(secret)
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    stage = root / "tmp/roastpilot-install.fake"
    restore = Path(environment["FAKE_OPERATOR_HOME"]) / ".cache/roastpilot-restore.fake"
    target, diagnostic = (
        (stage, f"retained staging directory at {stage}")
        if retained == "stage"
        else (restore, f"retained restore artifact directory at {restore}")
    )
    temporary = root / "etc/roastpilot-agent/.roastpilot-env.fake"
    result = _run(
        environment
        | {
            "ROASTPILOT_INSTALL_API_KEY": secret,
            "FAKE_TEE_FAIL_TARGET": str(temporary),
            "FAKE_RM_FAIL_PATH": str(target),
        },
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert result.returncode == 1
    assert diagnostic in result.stderr
    assert "rollback incomplete; manual reconciliation required" in result.stderr
    events = log.read_text().splitlines()
    assert f"rm <-rf> <--> <{target}>" in events
    assert f"rm <-rf> <--> <{stage}>" in events
    assert f"rm <-rf> <--> <{restore}>" in events
    assert target.exists()
    last_directory_removal = max(
        i
        for i, event in enumerate(events)
        if event in (f"rm <-rf> <--> <{stage}>", f"rm <-rf> <--> <{restore}>")
    )
    reload = max(i for i, event in enumerate(events) if event == "systemctl <daemon-reload>")
    assert reload > last_directory_removal
    assert any(
        event.startswith("rm <-rf>") and "roastpilot-config-rollback" in event for event in events
    )
    assert not _has_roastpilot_agent_lifecycle_mutation(events)
    assert secret not in result.stdout + result.stderr + log.read_text()


@pytest.mark.serial
def test_failed_root_lock_restores_operator_access_in_cleanup(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A lock-transition chmod failure still restores the operator-owned parent."""
    _, environment, log, _ = installer_harness
    var_dir = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"]) / "var/lib/roastpilot-agent"
    result = _run(
        environment | {"FAKE_CHMOD_FAIL_TARGET": str(var_dir), "FAKE_CHMOD_FAIL_ON_COUNT": "1"},
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0
    events = log.read_text().splitlines()
    root_lock = next(
        i
        for i, line in enumerate(events)
        if line == f"chown <--no-dereference> <root:root> <--> <{var_dir}>"
    )
    failed_chmod = next(
        i for i, line in enumerate(events) if line == f"chmod <0700> <--> <{var_dir}>"
    )
    cleanup_unlock = next(
        i
        for i, line in enumerate(events[failed_chmod + 1 :], failed_chmod + 1)
        if line == f"chown <--no-dereference> <operator:operators> <--> <{var_dir}>"
    )
    assert root_lock < failed_chmod < cleanup_unlock
    assert stat.S_IMODE(var_dir.stat().st_mode) == 0o700


@pytest.mark.serial
@pytest.mark.parametrize(
    ("failure", "status"),
    [("chown", 47), ("chmod", 23)],
)
def test_cleanup_managed_directory_restore_failure_requires_manual_reconciliation(
    installer_harness: tuple[Path, dict[str, str], Path, Path], failure: str, status: int
) -> None:
    """A failed managed-directory cleanup member remains visible after later cleanup work."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    var_dir = root / "var/lib/roastpilot-agent"
    injection = (
        {"FAKE_CHOWN_FAIL_TARGET": str(var_dir), "FAKE_CHOWN_FAIL_ON_COUNT": "2"}
        if failure == "chown"
        else {"FAKE_CHMOD_FAIL_TARGET": str(var_dir), "FAKE_CHMOD_FAIL_ON_COUNT": "2"}
    )
    result = _run(
        environment | injection | {"FAKE_HOSTNAME_VERIFY_FAIL": "1"},
        "--set-hostname",
        "roastpilot",
    )
    assert (
        result.returncode == 1
        and "rollback incomplete; manual reconciliation required" in result.stderr
    )
    events = log.read_text().splitlines()
    failed = (
        f"chown <--no-dereference> <operator:operators> <--> <{var_dir}>"
        if failure == "chown"
        else f"chmod <0700> <--> <{var_dir}>"
    )
    assert failed in events
    assert any(
        event.startswith("rm <-rf>") and "roastpilot-config-rollback" in event for event in events
    )
    assert "FAKE_HOSTNAME_VERIFY_FAILURE" in events


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
@pytest.mark.parametrize("audio_device", ("AA", "111", "mmm"))
def test_repeated_character_audio_devices_are_accepted_without_edge_whitespace(
    installer_harness: tuple[Path, dict[str, str], Path, Path], audio_device: str
) -> None:
    """Audio-device trimming accepts repeated characters but rejects edge whitespace."""
    _, environment, log, _ = installer_harness
    accepted = _run(
        environment,
        "--set-hostname",
        "roastpilot",
        "--audio-device",
        audio_device,
    )
    assert accepted.returncode == 0, accepted.stderr
    render = next(
        event
        for event in log.read_text().splitlines()
        if event.startswith("roastpilot-agent <appliance> <render>")
    )
    assert f"<--audio-device={audio_device}>" in render
    for unsafe_device in (f" {audio_device}", f"{audio_device} ", f"{audio_device}\n"):
        rejected = _run(
            environment,
            "--set-hostname",
            "roastpilot",
            "--audio-device",
            unsafe_device,
        )
        assert rejected.returncode != 0


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
    assert not _has_service_mutation((log.read_text() if log.exists() else "").splitlines())


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
@pytest.mark.parametrize("state", ["missing", "outside-venv", "not-executable"])
def test_appliance_executable_provenance_fails_before_model_or_later_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], state: str
) -> None:
    """A bad pipx entry point cannot reach model, render, or service installation."""
    fake_bin, environment, log, _ = installer_harness
    operator_home = Path(environment["FAKE_OPERATOR_HOME"])
    expected = operator_home / ".local/bin/roastpilot-agent"
    resolved = operator_home / ".local/share/pipx/venvs/roastpilot-agent/bin/roastpilot-agent"
    if state == "missing":
        expected.unlink()
    elif state == "outside-venv":
        expected.unlink()
        expected.symlink_to(fake_bin / "roastpilot-agent")
    else:
        resolved.chmod(0o644)

    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    events = log.read_text().splitlines()
    assert not any(line.startswith("roastpilot-agent <appliance>") for line in events)
    assert not any(line.startswith("tee ") for line in events)
    assert not _has_service_mutation(events)


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
    Path(environment["FAKE_HOSTNAME_SET_MARKER"]).unlink()
    start = len(log.read_text())
    verify = _run(environment | {"FAKE_HOSTNAME_VERIFY_FAIL": "1"}, "--set-hostname", "roastpilot")
    assert verify.returncode != 0 and "hostname verification failed" in verify.stderr
    assert f"restore manually from {prior}" in verify.stderr
    assert prior.read_text() == "old-host\n"
    assert Path(environment["FAKE_HOSTNAME"]).read_text().strip() == "roastpilot"
    verify_events = _delta(log, start)
    assert "FAKE_HOSTNAME_VERIFY_FAILURE" in verify_events
    assert "hostnamectl <set-hostname> <roastpilot>" in verify_events
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
        i
        for i, line in enumerate(events)
        if line == f"chown <--no-dereference> <root:root> <--> <{var_dir}>"
    )
    hostname_set = next(
        i for i, line in enumerate(events) if line == "hostnamectl <set-hostname> <roastpilot>"
    )
    operator_unlock = next(
        i
        for i, line in enumerate(events)
        if line == f"chown <--no-dereference> <operator:operators> <--> <{var_dir}>"
    )
    assert root_lock < hostname_set < operator_unlock
    assert prior.read_text() == "old-host\n" and not prior.is_symlink()


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


@pytest.mark.serial  # These failure oracles share the fake pipx and service state.
def test_pipx_provenance_and_capability_fail_before_appliance_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Untrusted pipx roots and base-only environments cannot reach appliance work."""
    _, environment, log, _ = installer_harness
    bad_root = _run(
        environment | {"FAKE_PIPX_HOME": "/tmp/not-the-operator-pipx"},
        "--set-hostname",
        "roastpilot",
    )
    assert bad_root.returncode != 0
    assert "roastpilot-agent <appliance" not in log.read_text()
    log.write_text("")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "default", "roastpilot-agent")
    base_only = _run(environment | {"FAKE_PIPX_MCP_MISSING": "1"}, "--set-hostname", "roastpilot")
    assert base_only.returncode != 0
    events = log.read_text()
    assert "roastpilot-agent <appliance" not in events
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in events


@pytest.mark.serial  # Pipx-home variants alter the isolated executable provenance tree.
def test_pipx_reports_only_canonical_xdg_or_legacy_operator_homes(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Both supported layouts work; malformed reported roots fail before appliance effects."""
    _, environment, log, _ = installer_harness
    operator_home = Path(environment["FAKE_OPERATOR_HOME"])
    xdg = operator_home / ".local/share/pipx"
    legacy = operator_home / ".local/pipx"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    xdg.rename(legacy)
    entry = operator_home / ".local/bin/roastpilot-agent"
    entry.unlink()
    entry.symlink_to(legacy / "venvs/roastpilot-agent/bin/roastpilot-agent")
    assert (
        _run(
            environment | {"FAKE_PIPX_HOME": str(legacy)}, "--set-hostname", "roastpilot"
        ).returncode
        == 0
    )
    for reported in ("", "relative", str(legacy / ".." / "pipx")):
        log.write_text("")
        result = _run(environment | {"FAKE_PIPX_HOME": reported}, "--set-hostname", "roastpilot")
        assert result.returncode != 0
        assert "roastpilot-agent <appliance" not in log.read_text()


@pytest.mark.serial  # Reuse and retained-key checks require one fake installation lifecycle.
def test_rerun_reuses_verified_model_and_retains_a_valid_existing_key(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A valid installed model and key make a subsequent offline maintenance run safe."""
    _, environment, log, _ = installer_harness
    secret = "kept-private-on-rerun"
    assert (
        _run(
            environment | {"ROASTPILOT_INSTALL_API_KEY": secret}, "--set-hostname", "roastpilot"
        ).returncode
        == 0
    )
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    env_file = root / "etc/roastpilot-agent/roastpilot-agent.env"
    start = len(log.read_text())
    rerun = _run(environment, "--set-hostname", "roastpilot")
    assert rerun.returncode == 0 and secret not in rerun.stdout + rerun.stderr + log.read_text()
    assert f"OPENROUTER_API_KEY={secret}" in env_file.read_text()
    events = _delta(log, start)
    assert not any("MODEL_FETCH" in event for event in events)
    model_dir = root / "var/lib/roastpilot-agent/models"
    assert any(
        f"<--from-dir> <{model_dir}>" in event
        for event in events
        if event.startswith("roastpilot-agent <appliance> <model>")
    )


@pytest.mark.serial
def test_failed_reuse_digest_inspection_falls_back_to_fresh_model_acquisition(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """An unreadable prior model is not trusted and is reacquired through the renderer seam."""
    _, environment, log, _ = installer_harness
    assert _run(environment, "--set-hostname", "roastpilot").returncode == 0
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    quantized = root / "var/lib/roastpilot-agent/models/onnx/int8/model_quantized.onnx"
    start = len(log.read_text())
    result = _run(
        environment | {"FAKE_SHA256_FAIL_PATH": str(quantized), "FAKE_SHA256_FAIL_ON_COUNT": "1"},
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode == 0
    events = _delta(log, start)
    assert f"FAKE_SHA256_FAILURE <{quantized}>" in events
    assert any("MODEL_FETCH" in event for event in events)


@pytest.mark.serial  # Activation failures are asserted through a dedicated fake systemctl log.
def test_activation_orders_avahi_and_never_restarts_an_active_agent(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Avahi failure blocks agent enablement, while active --start requires a manual restart."""
    _, environment, log, _ = installer_harness
    avahi_failed = _run(
        environment | {"FAKE_SYSTEMCTL_FAIL": "enable"}, "--set-hostname", "roastpilot"
    )
    assert avahi_failed.returncode != 0
    assert "install failed after enabling Avahi; Avahi enablement may remain" in avahi_failed.stderr
    assert "install failed after enabling roastpilot-agent" not in avahi_failed.stderr
    events = log.read_text().splitlines()
    assert "systemctl <enable> <--now> <avahi-daemon>" in events
    assert "systemctl <enable> <roastpilot-agent>" not in events
    log.write_text("")
    active = _run(
        environment | {"FAKE_SERVICE_STATE": "active"}, "--set-hostname", "roastpilot", "--start"
    )
    assert active.returncode != 0 and "never restart during a roast" in active.stderr
    active_events = log.read_text()
    assert "systemctl <show> <-p> <ActiveState> <--value> <roastpilot-agent>" in active_events
    assert "systemctl <start> <roastpilot-agent>" not in active_events
    assert not any(word in active_events for word in ("restart", "try-restart", "stop", "kill"))


@pytest.mark.serial  # A staged replacement must leave the original fake venv untouched on failure.
def test_failed_staged_replacement_keeps_the_prior_application(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A replacement that cannot prove MCP capability never uninstalls the prior app."""
    _, environment, log, _ = installer_harness
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", "roastpilot-agent[pi]==1.2")
    failed = _run(
        environment | {"FAKE_PIPX_MCP_MISSING": "1"},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert failed.returncode != 0
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in log.read_text()
    assert "roastpilot-agent <appliance" not in log.read_text()
    assert "1.2" in Path(environment["FAKE_PIPX_STATE"]).read_text()


@pytest.mark.serial
@pytest.mark.parametrize("failure", ["FAKE_PIPX_FAIL_STAGE_INSTALL", "FAKE_PIPX_FAIL_STAGE_VERIFY"])
def test_replacement_staging_failures_preserve_the_prior_normal_environment(
    installer_harness: tuple[Path, dict[str, str], Path, Path], failure: str
) -> None:
    """A failed staged replacement never removes the usable normal environment."""
    _, environment, log, _ = installer_harness
    state = Path(environment["FAKE_PIPX_STATE"])
    _pipx_state(state, "1.2", "roastpilot-agent[pi]==1.2")
    result = _run(environment | {failure: "1"}, "--set-hostname", "roastpilot", "--version", "2.0")
    assert result.returncode != 0
    events = log.read_text().splitlines()
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in events
    assert '"package_version": "1.2"' in state.read_text()
    assert not any(line.startswith("roastpilot-agent <appliance>") for line in events)
    assert "Installed: unit enabled; model verified." not in result.stdout


@pytest.mark.serial
@pytest.mark.parametrize("failure", ["FAKE_PIPX_FAIL_FINAL_INSTALL", "FAKE_PIPX_FAIL_FINAL_VERIFY"])
def test_failed_final_replacement_restores_and_reverifies_the_prior_application(
    installer_harness: tuple[Path, dict[str, str], Path, Path], failure: str
) -> None:
    """A failed final replacement restores the exact prior package before appliance work."""
    _, environment, log, _ = installer_harness
    state = Path(environment["FAKE_PIPX_STATE"])
    prior_package = "roastpilot-agent[pi]==1.2"
    _pipx_state(state, "1.2", prior_package)
    result = _run(environment | {failure: "1"}, "--set-hostname", "roastpilot", "--version", "2.0")
    assert result.returncode != 0
    events = log.read_text().splitlines()
    assert json.loads(state.read_text())["venvs"]["roastpilot-agent"]["metadata"][
        "main_package"
    ] == {
        "package_version": "1.2",
        "package_or_url": prior_package,
    }
    normal_uninstalls = [
        line for line in events if line == "pipx <uninstall> <--> <roastpilot-agent>"
    ]
    assert len(normal_uninstalls) == 2
    assert any(event.endswith(f"<{prior_package}>") and "<--pip-args>" in event for event in events)
    assert any(event.startswith("FAKE_OFFLINE_RESTORE <") for event in events)
    assert sum(
        line == "pipx <runpip> <roastpilot-agent> <show> <coffee-roaster-mcp>" for line in events
    ) == (1 if failure == "FAKE_PIPX_FAIL_FINAL_INSTALL" else 2)
    assert any("roastpilot-stage-" in line and "<uninstall>" in line for line in events)
    assert not any(line.startswith("roastpilot-agent <appliance>") for line in events)
    assert "Installed: unit enabled; model verified." not in result.stdout
    assert "application/configuration skew may require manual reconciliation" not in result.stderr


@pytest.mark.serial
def test_failed_restoration_cleans_the_stage_and_fails_before_appliance_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A restoration failure is fatal but still attempts staged-environment cleanup."""
    _, environment, log, _ = installer_harness
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", "roastpilot-agent[pi]==1.2")
    result = _run(
        environment | {"FAKE_PIPX_FAIL_FINAL_INSTALL": "1", "FAKE_PIPX_FAIL_RESTORE_INSTALL": "1"},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert result.returncode != 0
    events = log.read_text().splitlines()
    assert any("roastpilot-stage-" in line and "<uninstall>" in line for line in events)
    assert not any(line.startswith("roastpilot-agent <appliance>") for line in events)
    assert "Installed: unit enabled; model verified." not in result.stdout


@pytest.mark.serial
def test_failed_restoration_verification_cleans_the_stage_and_fails_closed(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A restored package must re-prove MCP capability before recovery is reported."""
    _, environment, log, _ = installer_harness
    prior_package = "roastpilot-agent[pi]==1.2"
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", prior_package)
    result = _run(
        environment | {"FAKE_PIPX_FAIL_FINAL_INSTALL": "1", "FAKE_PIPX_FAIL_RESTORE_VERIFY": "1"},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert result.returncode != 0
    assert "replacement failed and prior application could not be restored" in result.stderr
    events = log.read_text().splitlines()
    assert any(event.endswith(f"<{prior_package}>") and "<--pip-args>" in event for event in events)
    assert any(event.startswith("FAKE_OFFLINE_RESTORE <") for event in events)
    assert "pipx <runpip> <roastpilot-agent> <show> <coffee-roaster-mcp>" in events
    assert any("roastpilot-stage-" in line and "<uninstall>" in line for line in events)
    assert not any(line.startswith("roastpilot-agent <appliance>") for line in events)
    assert "Installed: unit enabled; model verified." not in result.stdout


@pytest.mark.serial
def test_staged_cleanup_failure_is_fatal_before_appliance_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """An unremovable staged venv cannot be silently retained after replacement."""
    _, environment, log, _ = installer_harness
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", "roastpilot-agent[pi]==1.2")
    result = _run(
        environment | {"FAKE_PIPX_FAIL_STAGE_CLEANUP": "1"},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert result.returncode != 0
    events = log.read_text().splitlines()
    assert any(
        line.startswith("pipx <uninstall>") and "roastpilot-stage-" in line for line in events
    )
    assert "retained staged pipx environment" in result.stderr
    assert not any(line.startswith("roastpilot-agent <appliance>") for line in events)
    assert "Installed: unit enabled; model verified." not in result.stdout


@pytest.mark.serial
def test_staged_venv_is_cleaned_when_capability_resolution_dies(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A resolver die after staging still uninstalls the marker-owned staged venv."""
    _, environment, log, _ = installer_harness
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", "roastpilot-agent[pi]==1.2")
    result = _run(
        environment | {"FAKE_PIPX_HOME": str(tmp_path / "unsafe-pipx")},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert result.returncode != 0 and "pipx home is outside invoking-user boundary" in result.stderr
    events = log.read_text().splitlines()
    assert any(
        line.startswith("pipx <uninstall>") and "roastpilot-stage-" in line for line in events
    )
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
def test_fresh_local_wheel_install_includes_pi_extra_and_verifies_mcp(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A fresh local wheel is installed with the Pi extra before appliance work."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "roastpilot-agent.whl"
    wheel.write_text("wheel")
    result = _run(environment, "--set-hostname", "roastpilot", "--wheel", str(wheel))
    assert result.returncode == 0, result.stderr
    events = log.read_text().splitlines()
    assert f"pipx <install> <--> <{wheel}[pi]>" in events
    assert "pipx <runpip> <roastpilot-agent> <show> <coffee-roaster-mcp>" in events


@pytest.mark.serial
@pytest.mark.parametrize(
    ("content", "diagnostic"),
    [
        (
            "OPENROUTER_API_KEY=candidate-key\nOPENROUTER_API_KEY=two\nPORT=8000\nROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\nCOFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n",
            "existing environment file has duplicate API key",
        ),
        (
            "OPENROUTER_API_KEY=candidate-key\nPORT=8000\nROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\nCOFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\nUNEXPECTED=value\n",
            "existing environment file has an unexpected assignment",
        ),
        (
            "OPENROUTER_API_KEY=candidate-key\nPORT=8000\nROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\n",
            "existing environment file is missing a required member",
        ),
        (
            "OPENROUTER_API_KEY=candidate key\nPORT=8000\nROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\nCOFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n",
            "existing environment file has unsafe API key characters",
        ),
    ],
)
def test_malformed_existing_environment_fails_before_installer_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], content: str, diagnostic: str
) -> None:
    """Malformed retained-key inputs are rejected before apt, pipx, or rendering."""
    _, environment, log, _ = installer_harness
    env_file = (
        Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
        / "etc/roastpilot-agent/roastpilot-agent.env"
    )
    env_file.parent.mkdir(parents=True)
    env_file.write_text(content)
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    assert diagnostic in result.stderr
    output = result.stdout + result.stderr + (log.read_text() if log.exists() else "")
    assert "candidate-key" not in output
    assert not any(
        line.startswith(("apt-get ", "pipx ", "roastpilot-agent ", "systemctl "))
        for line in log.read_text().splitlines()
    )


@pytest.mark.serial
def test_unsafe_existing_environment_file_fails_before_installer_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A retained-key symlink is never followed into the installation lifecycle."""
    _, environment, log, _ = installer_harness
    env_file = (
        Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
        / "etc/roastpilot-agent/roastpilot-agent.env"
    )
    env_file.parent.mkdir(parents=True)
    target = tmp_path / "candidate-key-target"
    target.write_text("OPENROUTER_API_KEY=candidate-key\\n")
    env_file.symlink_to(target)
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    output = result.stdout + result.stderr + (log.read_text() if log.exists() else "")
    assert "candidate-key" not in output
    assert not any(
        line.startswith(("apt-get ", "pipx ", "roastpilot-agent ", "systemctl "))
        for line in log.read_text().splitlines()
    )


@pytest.mark.serial
def test_xtrace_is_disabled_before_preserving_an_existing_api_key(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Shell tracing cannot disclose a retained API-key fixture during parsing."""
    _, environment, log, _ = installer_harness
    env_file = (
        Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
        / "etc/roastpilot-agent/roastpilot-agent.env"
    )
    env_file.parent.mkdir(parents=True)
    fixture_value = "xtrace-retained-fixture"
    env_file.write_text(
        f"OPENROUTER_API_KEY={fixture_value}\nPORT=8000\n"
        "ROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\n"
        "COFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n"
    )
    result = _run(environment, "--set-hostname", "roastpilot", xtrace=True)
    assert result.returncode == 0, result.stderr
    assert fixture_value not in result.stdout + result.stderr + log.read_text()


@pytest.mark.serial
def test_xtrace_is_disabled_before_reading_an_inherited_installer_api_key(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Inherited installer credentials are not disclosed by a bash -x invocation."""
    _, environment, log, _ = installer_harness
    fixture_value = "xtrace-inherited-fixture"
    result = _run(
        environment | {"ROASTPILOT_INSTALL_API_KEY": fixture_value},
        "--set-hostname",
        "roastpilot",
        xtrace=True,
    )
    assert result.returncode == 0, result.stderr
    assert fixture_value not in result.stdout + result.stderr + log.read_text()


@pytest.mark.serial
@pytest.mark.parametrize("mutation", ["bad-digest", "missing-peer", "symlink", "incomplete"])
def test_only_complete_verified_installed_models_are_reused(
    installer_harness: tuple[Path, dict[str, str], Path, Path], mutation: str
) -> None:
    """Invalid installed model trees fall back to the normal verified fetch path."""
    _, environment, log, _ = installer_harness
    assert _run(environment, "--set-hostname", "roastpilot").returncode == 0
    model_dir = (
        Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"]) / "var/lib/roastpilot-agent/models"
    )
    quantized = model_dir / "onnx/int8/model_quantized.onnx"
    preprocessor = model_dir / "onnx/int8/preprocessor_config.json"
    if mutation == "bad-digest":
        quantized.write_text("WRONG")
    elif mutation == "missing-peer":
        preprocessor.unlink()
    elif mutation == "symlink":
        quantized.unlink()
        quantized.symlink_to(preprocessor)
    else:
        quantized.write_text("MODEL-partial")
    start = len(log.read_text())
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode == (1 if mutation == "symlink" else 0), result.stderr
    events = _delta(log, start)
    model_install = next(
        line for line in events if line.startswith("roastpilot-agent <appliance> <model>")
    )
    assert "<--from-dir>" not in model_install
    assert any(line.startswith("MODEL_FETCH") for line in events)


@pytest.mark.serial
def test_active_agent_without_start_fails_before_appliance_changes(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Maintenance never rewrites appliance state while the agent is active."""
    _, environment, log, _ = installer_harness
    result = _run(environment | {"FAKE_SERVICE_STATE": "active"}, "--set-hostname", "roastpilot")
    assert result.returncode != 0 and "never restart during a roast" in result.stderr
    events = log.read_text().splitlines()
    assert "systemctl <show> <-p> <ActiveState> <--value> <roastpilot-agent>" in events
    assert not any(line.startswith(("apt-get ", "pipx ", "roastpilot-agent ")) for line in events)


@pytest.mark.serial
@pytest.mark.parametrize("state", ["activating", "deactivating", "reloading", "", "garbled"])
def test_only_inactive_or_failed_service_states_are_admitted(
    installer_harness: tuple[Path, dict[str, str], Path, Path], state: str
) -> None:
    """Transient, malformed, and active states fail closed before installer effects."""
    _, environment, log, _ = installer_harness
    result = _run(environment | {"FAKE_SERVICE_STATE": state}, "--set-hostname", "roastpilot")
    assert result.returncode != 0 and "never restart during a roast" in result.stderr
    assert not any(
        line.startswith(("apt-get ", "pipx ", "roastpilot-agent "))
        for line in log.read_text().splitlines()
    )


@pytest.mark.serial
@pytest.mark.parametrize("mode", ["missing", "non-executable", "symlink"])
def test_missing_or_nonexecutable_mcp_console_fails_before_appliance_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], mode: str
) -> None:
    """Package metadata alone cannot prove the installed MCP console is usable."""
    _, environment, log, _ = installer_harness
    mcp = Path(environment["FAKE_PIPX_HOME"]) / "venvs/roastpilot-agent/bin/coffee-roaster-mcp"
    if mode == "missing":
        mcp.unlink()
    elif mode == "non-executable":
        mcp.chmod(0o644)
    else:
        target = mcp.with_name("coffee-roaster-mcp-real")
        mcp.rename(target)
        mcp.symlink_to(target)
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", "roastpilot-agent[pi]==1.2")
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode != 0 and "Pi/MCP" in result.stderr
    assert "roastpilot-agent <appliance" not in log.read_text()
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in log.read_text()


@pytest.mark.serial
def test_unavailable_prior_local_wheel_is_not_replaced(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """Replacement refuses before uninstalling an application without an exact restore artifact."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
    wheel.write_text("wheel")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    wheel.unlink()
    result = _run(environment, "--set-hostname", "roastpilot", "--version", "2.0")
    assert result.returncode != 0 and "cannot preserve exact prior local wheel" in result.stderr
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in log.read_text()


@pytest.mark.serial
def test_indexed_prior_wheelhouse_failure_aborts_before_uninstall(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """An indexed prior must have a captured wheelhouse before replacement starts."""
    _, environment, log, _ = installer_harness
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", "roastpilot-agent[pi]==1.2")
    result = _run(
        environment | {"FAKE_PIPX_FAIL_WHEELHOUSE": "1"},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    events = log.read_text().splitlines()
    assert result.returncode != 0
    assert "FAKE_PIPX_WHEELHOUSE_FAILURE" in events
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in events


@pytest.mark.serial
@pytest.mark.parametrize("mcp_version", ("0.1.9", "0.2.1"))
def test_mcp_version_must_match_the_e11_pin_before_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], mcp_version: str
) -> None:
    """A console entry point is insufficient unless the MCP distribution is E11-pinned."""
    _, environment, log, _ = installer_harness
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", "roastpilot-agent[pi]==1.2")
    result = _run(
        environment | {"FAKE_PIPX_MCP_VERSION": mcp_version}, "--set-hostname", "roastpilot"
    )
    assert result.returncode != 0 and "Pi/MCP" in result.stderr
    assert "pipx <runpip> <roastpilot-agent> <show> <coffee-roaster-mcp>" in log.read_text()
    assert "roastpilot-agent <appliance" not in log.read_text()


@pytest.mark.serial
@pytest.mark.parametrize(
    ("injection", "marker"),
    [
        ("FAKE_PIPX_MCP_VERSION_MISSING", "FAKE_MCP_VERSION_MISSING"),
        ("FAKE_PIPX_MCP_VERSION_DUPLICATE", "FAKE_MCP_VERSION_DUPLICATE"),
    ],
)
def test_mcp_show_requires_exactly_one_version_field(
    installer_harness: tuple[Path, dict[str, str], Path, Path], injection: str, marker: str
) -> None:
    """Missing or ambiguous pip-show version evidence fails before appliance work."""
    _, environment, log, _ = installer_harness
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", "roastpilot-agent[pi]==1.2")
    result = _run(environment | {injection: "1"}, "--set-hostname", "roastpilot")
    events = log.read_text().splitlines()
    assert result.returncode != 0 and "Pi/MCP" in result.stderr
    assert marker in events
    assert not any(event.startswith(("apt-get ", "roastpilot-agent ")) for event in events)


@pytest.mark.serial
def test_staged_and_restored_mcp_versions_must_match_the_e11_pin(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Capability verification applies the same MCP pin to stage and rollback venvs."""
    _, environment, log, _ = installer_harness
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", "roastpilot-agent[pi]==1.2")
    staged = _run(
        environment | {"FAKE_PIPX_STAGE_MCP_VERSION": "0.1.9"},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    staged_events = log.read_text().splitlines()
    assert (
        staged.returncode != 0
        and "staged replacement lacks required Pi/MCP capability" in staged.stderr
    )
    assert "FAKE_STAGE_MCP_VERSION <0.1.9>" in staged_events
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in staged_events

    start = len(log.read_text())
    restored = _run(
        environment
        | {
            "FAKE_PIPX_FAIL_FINAL_INSTALL": "1",
            "FAKE_PIPX_RESTORE_MCP_VERSION": "0.1.9",
        },
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    restored_events = _delta(log, start)
    assert restored.returncode != 0 and "prior application could not be restored" in restored.stderr
    assert "FAKE_RESTORE_MCP_VERSION <0.1.9>" in restored_events


@pytest.mark.serial
def test_build_tagged_prior_wheel_is_preserved_for_rollback(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A valid PEP 427 build tag remains an exact local rollback artifact."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "roastpilot_agent-1.2-1build-py3-none-any.whl"
    wheel.write_text("wheel")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    result = _run(
        environment | {"FAKE_PIPX_FAIL_FINAL_INSTALL": "1"},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert result.returncode != 0
    assert any(
        "roastpilot-restore" in event and wheel.name in event
        for event in log.read_text().splitlines()
    )


@pytest.mark.serial
def test_malformed_build_tagged_prior_wheel_is_rejected_before_uninstall(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A non-numeric PEP 427 build tag cannot become a rollback selector."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "roastpilot_agent-1.2-build-py3-none-any.whl"
    wheel.write_text("wheel")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    result = _run(environment, "--set-hostname", "roastpilot", "--version", "2.0")
    assert result.returncode != 0 and "cannot preserve exact prior local wheel" in result.stderr
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in log.read_text()


@pytest.mark.serial
def test_prior_local_wheel_adjacent_recheck_blocks_a_post_canonicalisation_delete(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """The source is rechecked after canonicalisation before any replacement action."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
    wheel.write_text("wheel")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    result = _run(
        environment | {"FAKE_MUTATE_AFTER_READLINK_PATH": str(wheel)},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert result.returncode != 0
    assert "cannot preserve exact prior local wheel" in result.stderr
    events = log.read_text().splitlines()
    assert f"FAKE_READLINK_MUTATION <{wheel}>" in events
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in events
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
@pytest.mark.parametrize(
    ("release", "diagnostic"),
    [
        ("ID debian\n", "malformed operating system data: missing ="),
        ('ID="debian;rm"\n', "malformed operating system data: unsafe ID value"),
        ("ID=debian\nID=debian\n", "malformed operating system data: duplicate ID"),
        (
            "ID=debian\nID_LIKE=debian\nID_LIKE=debian\n",
            "malformed operating system data: duplicate ID_LIKE",
        ),
        ("ID=fedora\nID_LIKE=rpm\n", "unsupported OS/package manager"),
    ],
)
def test_os_release_parser_rejects_each_untrusted_branch_before_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], release: str, diagnostic: str
) -> None:
    """Real newline release fixtures fail closed before package or service work."""
    _, environment, log, _ = installer_harness
    Path(environment["ROASTPILOT_INSTALL_OS_RELEASE"]).write_text(release)
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    assert diagnostic in result.stderr
    events = log.read_text().splitlines()
    assert not any(
        event.startswith(("apt-get ", "pipx ", "roastpilot-agent ", "systemctl <enable>"))
        for event in events
    )


@pytest.mark.serial
def test_dangling_existing_environment_symlink_is_rejected_before_package_work(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A dangling environment symlink remains present and cannot bypass the retained-key guard."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    env_file = root / "etc/roastpilot-agent/roastpilot-agent.env"
    env_file.parent.mkdir(parents=True)
    env_file.symlink_to(root / "missing-environment")
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    assert "existing environment file is unsafe" in result.stderr
    assert not any(
        event.startswith(("apt-get ", "pipx ", "roastpilot-agent ", "systemctl "))
        for event in log.read_text().splitlines()
    )


@pytest.mark.serial
@pytest.mark.parametrize(
    ("version", "package"),
    [
        ("bad/version", "roastpilot-agent[pi]"),
        ("1.2", "roastpilot-agent[pi]==bad/version"),
        ("1.2", "roastpilot-agent[pi]==1.3"),
    ],
)
def test_malformed_prior_application_specs_fail_before_replacement(
    installer_harness: tuple[Path, dict[str, str], Path, Path], version: str, package: str
) -> None:
    """Both pipx report shapes must yield one exact, safe appliance restoration spec."""
    _, environment, log, _ = installer_harness
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), version, package)
    result = _run(environment, "--set-hostname", "roastpilot", "--version", "2.0")
    assert result.returncode != 0
    assert "cannot preserve exact prior application" in result.stderr
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in log.read_text().splitlines()


@pytest.mark.serial
def test_hostname_query_failure_is_not_reported_as_a_hostname_mismatch(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A failed hostnamectl query has its own fail-closed diagnostic before installer effects."""
    _, environment, log, _ = installer_harness
    result = _run(environment | {"FAKE_HOSTNAME_QUERY_FAIL": "1"})
    assert result.returncode != 0
    assert "cannot determine static hostname" in result.stderr
    assert "hostname is not roastpilot" not in result.stderr
    events = log.read_text().splitlines()
    assert "FAKE_HOSTNAME_QUERY_FAILURE" in events
    assert not any(
        event.startswith(("apt-get ", "pipx ", "roastpilot-agent ", "systemctl "))
        for event in events
    )


@pytest.mark.serial
def test_dash_leading_audio_device_is_bound_as_one_appliance_option_value(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A dash-leading device name cannot be parsed as an independent appliance option."""
    _, environment, log, _ = installer_harness
    result = _run(environment, "--set-hostname", "roastpilot", "--audio-device", "--device-name")
    assert result.returncode == 0
    render = next(
        event
        for event in log.read_text().splitlines()
        if event.startswith("roastpilot-agent <appliance> <render>")
    )
    assert "<--audio-device=--device-name>" in render
    assert "<--audio-device> <--device-name>" not in render


@pytest.mark.serial
def test_prior_local_wheel_is_restored_from_the_private_copy_after_source_loss(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A late replacement failure restores bytes copied before the old source vanishes."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
    wheel.write_text("wheel")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    result = _run(
        environment | {"FAKE_DELETE_PRIOR_WHEEL": str(wheel), "FAKE_PIPX_FAIL_FINAL_INSTALL": "1"},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert result.returncode != 0 and not wheel.exists()
    events = log.read_text().splitlines()
    assert any(
        "roastpilot-restore" in event and "roastpilot_agent-1.2-py3-none-any.whl[pi]" in event
        for event in events
    )
    artifact = Path(environment["FAKE_OPERATOR_HOME"]) / ".cache/roastpilot-restore.fake"
    assert artifact.is_dir()
    assert (
        f"retain restore artifact directory at {artifact} for the restored local-wheel application"
        in result.stderr
    )
    assert "application/configuration skew" not in result.stderr
    assert f"rm <-rf> <--> <{artifact}>" not in events
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
def test_local_wheel_direct_reference_is_rewritten_to_the_private_copy(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """PEP-610 provenance is accepted only after it is redirected to the private copy."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
    wheel.write_text("wheel")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    result = _run(
        environment
        | {
            "FAKE_PIPX_FREEZE_DIRECT_REFERENCE": str(wheel),
            "FAKE_PIPX_FAIL_FINAL_INSTALL": "1",
        },
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    artifact = Path(environment["FAKE_OPERATOR_HOME"]) / ".cache/roastpilot-restore.fake"
    requirements = artifact / "requirements.txt"
    events = log.read_text().splitlines()
    assert result.returncode != 0
    assert f"FAKE_PIPX_FREEZE_DIRECT_REFERENCE <{wheel}>" in events
    assert f"file://{artifact / wheel.name}" in requirements.read_text()
    assert str(wheel) not in requirements.read_text()
    assert any(
        event.endswith(f"<{artifact / (wheel.name + '[pi]')}>")
        and f"<--pip-args> <--no-index --find-links={artifact}>" in event
        for event in events
    )


@pytest.mark.serial
def test_local_wheelhouse_capture_failure_aborts_before_uninstall(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A local prior wheel still requires a complete offline dependency wheelhouse."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
    wheel.write_text("wheel")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    result = _run(
        environment | {"FAKE_PIPX_FAIL_WHEELHOUSE": "1"},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    events = log.read_text().splitlines()
    assert result.returncode != 0
    assert "FAKE_PIPX_WHEELHOUSE_FAILURE" in events
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in events


@pytest.mark.serial
def test_prior_local_wheel_artifact_is_retained_before_post_reinstall_capability_failure(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A restored local wheel remains available when its immediately following capability check fails."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
    wheel.write_text("wheel")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    secret = "must-not-leak-from-retained-wheel"
    result = _run(
        environment
        | {
            "FAKE_DELETE_PRIOR_WHEEL": str(wheel),
            "FAKE_PIPX_FAIL_FINAL_INSTALL": "1",
            "FAKE_PIPX_FAIL_RESTORE_VERIFY": "1",
            "ROASTPILOT_INSTALL_API_KEY": secret,
        },
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    artifact = Path(environment["FAKE_OPERATOR_HOME"]) / ".cache/roastpilot-restore.fake"
    prior_copy = artifact / "roastpilot_agent-1.2-py3-none-any.whl"
    events = log.read_text().splitlines()
    assert result.returncode != 0
    assert "pipx <runpip> <roastpilot-agent> <show> <coffee-roaster-mcp>" in events
    assert artifact.is_dir() and prior_copy.is_file()
    assert (
        f"retain restore artifact directory at {artifact} for the restored local-wheel application"
        in result.stderr
    )
    assert "application/configuration skew may require manual reconciliation" in result.stderr
    assert "rollback incomplete; manual reconciliation required" not in result.stderr
    assert f"rm <-rf> <--> <{artifact}>" not in events
    assert secret not in result.stdout + result.stderr + log.read_text()
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
def test_final_root_probe_failure_restores_prior_local_wheel_before_exit(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A final resolver failure enters restoration instead of terminating the transaction."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
    secret = "retained-wheel-secret"
    wheel.write_text(secret)
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    result = _run(
        environment
        | {
            "FAKE_DELETE_PRIOR_WHEEL": str(wheel),
            "FAKE_PIPX_FAIL_FINAL_ROOT_PROBE": "1",
            "ROASTPILOT_INSTALL_API_KEY": secret,
        },
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    artifact = Path(environment["FAKE_OPERATOR_HOME"]) / ".cache/roastpilot-restore.fake"
    preserved = artifact / wheel.name
    events = log.read_text().splitlines()
    assert result.returncode != 0
    assert "FAKE_FINAL_ROOT_PROBE_FAILURE" in events
    assert any(
        event.endswith(f"<{artifact / (wheel.name + '[pi]')}>") and "<--pip-args>" in event
        for event in events
    )
    assert artifact.is_dir() and preserved.is_file()
    assert (
        f"retain restore artifact directory at {artifact} for the restored local-wheel application"
        in result.stderr
    )
    assert "application/configuration skew" not in result.stderr
    assert any("roastpilot-stage-" in event and "<uninstall>" in event for event in events)
    assert f"rm <-rf> <--> <{artifact}>" not in events
    assert secret not in result.stdout + result.stderr + log.read_text()
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
def test_non_wheel_prior_local_basename_is_rejected_before_uninstall(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """An arbitrary copied basename cannot become a pipx restoration selector."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "prior.whl"
    wheel.write_text("wheel")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    result = _run(environment, "--set-hostname", "roastpilot", "--version", "2.0")
    assert result.returncode != 0
    assert "cannot preserve exact prior local wheel" in result.stderr
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in log.read_text()


@pytest.mark.serial
def test_glob_character_in_validated_temporary_suffix_removes_only_that_exact_entry(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Exact temporary deregistration cannot glob-remove neighbouring live cleanup entries."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    parent = root / "var/lib/roastpilot-agent/models/onnx/int8"
    temporary = parent / ".roastpilot-model.*"
    result = _run(
        environment
        | {
            "FAKE_MKTEMP_TEMPLATE": str(parent / ".roastpilot-model.XXXXXX"),
            "FAKE_MKTEMP_RESULT": str(temporary),
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode == 0
    events = log.read_text().splitlines()
    assert f"FAKE_MKTEMP_RESULT <{temporary}>" in events
    assert any(event.startswith("mv ") and f"<{temporary}>" in event for event in events)
    assert f"rm <-f> <--> <{temporary}>" not in events


@pytest.mark.serial
def test_root_temporary_deregistration_is_exact_under_glob_suffix_mutation(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """Quoted exact comparison retains a glob-matching sibling; the unquoted mutant does not."""
    _, environment, log, _ = installer_harness
    target = tmp_path / ".roastpilot-model.*"
    sibling = tmp_path / ".roastpilot-model.sibling"
    source = INSTALLER.read_text()
    harness = """
ROOT_TEMPORARIES=("$1" "$2")
remove_root_temporary "$1"
for temporary in "${ROOT_TEMPORARIES[@]}"; do run_privileged rm -f -- "$temporary"; done
"""
    real = tmp_path / "exact-removal.sh"
    real.write_text(source.replace('main "$@"', harness))
    real.chmod(0o755)
    result = subprocess.run(
        ["bash", str(real), str(target), str(sibling)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert f"rm <-f> <--> <{sibling}>" in log.read_text().splitlines()
    log.write_text("")
    mutant = tmp_path / "pattern-removal.sh"
    mutant.write_text(
        source.replace('[[ "$temporary" == "$target" ]]', "[[ $temporary == $target ]]").replace(
            'main "$@"', harness
        )
    )
    mutant.chmod(0o755)
    mutated = subprocess.run(
        ["bash", str(mutant), str(target), str(sibling)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert mutated.returncode == 0
    assert f"rm <-f> <--> <{sibling}>" not in log.read_text().splitlines()


@pytest.mark.serial
def test_restrictive_umask_keeps_owned_directories_traversable(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """Installer-owned directory modes do not inherit an operator's restrictive umask."""
    _, environment, _, _ = installer_harness
    script = tmp_path / "umask-install.sh"
    script.write_text(
        INSTALLER.read_text().replace("set -euo pipefail", "set -euo pipefail\numask 077", 1)
    )
    script.chmod(0o755)
    assert _run(environment, "--set-hostname", "roastpilot", script=script).returncode == 0
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    assert stat.S_IMODE((root / "etc/roastpilot-agent").stat().st_mode) == 0o750
    for path in ("models", "models/onnx", "models/onnx/int8"):
        assert stat.S_IMODE((root / "var/lib/roastpilot-agent" / path).stat().st_mode) == 0o750
    assert stat.S_IMODE((root / "etc/systemd/system").stat().st_mode) == 0o700
    assert (
        stat.S_IMODE((root / "etc/roastpilot-agent/roastpilot-agent.env").stat().st_mode) == 0o600
    )


@pytest.mark.serial
def test_child_processes_do_not_receive_exported_secret_sentinels(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Only the protected env leaf retains the explicit installer secret."""
    _, environment, _, _ = installer_harness
    secret_log = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"]).parent / "secret-env.log"
    pre_scrub_log = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"]).parent / "pre-scrub.log"
    script = secret_log.with_name("pre-scrub-install.sh")
    script.write_text(
        INSTALLER.read_text().replace(
            "    scrub_child_secrets\n    parse_arguments",
            '    printf "ROASTPILOT_API_KEY=%s ROASTPILOT_OPENROUTER_API_KEY=%s\\n" "${ROASTPILOT_API_KEY+present}" "${ROASTPILOT_OPENROUTER_API_KEY+present}" > "$FAKE_PRE_SCRUB_LOG"\n    scrub_child_secrets\n    parse_arguments',
            1,
        )
    )
    script.chmod(0o755)
    result = _run(
        environment
        | {
            "FAKE_SECRET_ENV_LOG": str(secret_log),
            "FAKE_PRE_SCRUB_LOG": str(pre_scrub_log),
            "FAKE_ALLOW_AMBIENT_SECRET": "1",
            "OPENROUTER_API_KEY": "openrouter-sentinel",
            "OPENROUTER_API_KEY_FILE": "openrouter-file-sentinel",
            "OPENAI_API_KEY": "openai-sentinel",
            "ANTHROPIC_API_KEY": "anthropic-sentinel",
            "ROASTPILOT_API_KEY": "roastpilot-api-sentinel",
            "ROASTPILOT_OPENROUTER_API_KEY": "roastpilot-openrouter-sentinel",
            "API_KEY": "exported-api-sentinel",
            "ROASTPILOT_INSTALL_API_KEY": "installer-sentinel",
        },
        "--set-hostname",
        "roastpilot",
        script=script,
    )
    assert result.returncode == 0
    assert (
        pre_scrub_log.read_text()
        == "ROASTPILOT_API_KEY=present ROASTPILOT_OPENROUTER_API_KEY=present\n"
    )
    child_env = secret_log.read_text()
    for sentinel in (
        "openrouter-sentinel",
        "openrouter-file-sentinel",
        "openai-sentinel",
        "anthropic-sentinel",
        "roastpilot-api-sentinel",
        "roastpilot-openrouter-sentinel",
        "exported-api-sentinel",
    ):
        assert sentinel not in child_env
    env_file = (
        Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
        / "etc/roastpilot-agent/roastpilot-agent.env"
    )
    assert "installer-sentinel" in env_file.read_text()
    assert "installer-sentinel" not in result.stdout + result.stderr + child_env


@pytest.mark.serial
def test_state_sequence_blocks_prior_uninstall(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """The uninstall boundary rechecks state after the initial safe probe."""
    _, environment, log, _ = installer_harness
    sequence = tmp_path / "states"
    sequence.write_text("inactive\nactivating\n")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", "roastpilot-agent[pi]==1.2")
    result = _run(
        environment | {"FAKE_SERVICE_STATE_SEQUENCE": str(sequence)},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert result.returncode != 0 and "never restart during a roast" in result.stderr
    events = log.read_text().splitlines()
    assert events.count("systemctl <show> <-p> <ActiveState> <--value> <roastpilot-agent>") == 2
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in events
    assert any(
        line.startswith("pipx <uninstall> <--> <roastpilot-agent-roastpilot-stage-")
        for line in events
    )


@pytest.mark.serial
@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_terminal_inactive_states_admit_normal_runs(
    installer_harness: tuple[Path, dict[str, str], Path, Path], state: str
) -> None:
    """Both documented terminal service states permit a normal safe install."""
    _, environment, log, _ = installer_harness
    result = _run(environment | {"FAKE_SERVICE_STATE": state}, "--set-hostname", "roastpilot")
    assert result.returncode == 0, result.stderr
    probes = "systemctl <show> <-p> <ActiveState> <--value> <roastpilot-agent>"
    assert log.read_text().splitlines().count(probes) == 2


@pytest.mark.serial
def test_pre_promotion_state_change_aborts_before_live_mutations(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """An active transition after staging cannot promote or enable appliance state."""
    _, environment, log, _ = installer_harness
    sequence = tmp_path / "states"
    sequence.write_text("inactive\nactive\n")
    result = _run(
        environment | {"FAKE_SERVICE_STATE_SEQUENCE": str(sequence)}, "--set-hostname", "roastpilot"
    )
    assert result.returncode != 0 and "end any run safely" in result.stderr
    events = log.read_text().splitlines()
    probe = "systemctl <show> <-p> <ActiveState> <--value> <roastpilot-agent>"
    assert events.count(probe) == 2
    assert not any(
        line.startswith(
            (
                "tee ",
                "mv ",
                "hostnamectl <set",
                "usermod ",
                "systemctl <daemon",
                "systemctl <enable",
                "systemctl <start",
            )
        )
        for line in events
    )
    assert not any(
        line.startswith(("chown ", "chmod ")) and ("/root/etc/" in line or "/root/var/" in line)
        for line in events
    )
    assert any(line.startswith("rm <-rf>") and "roastpilot-install" in line for line in events)
    assert "Installed: unit enabled" not in result.stdout


@pytest.mark.serial
def test_final_start_recheck_never_disturbs_a_deactivating_service(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A last-moment transition blocks --start without restart-like commands."""
    _, environment, log, _ = installer_harness
    sequence = tmp_path / "states"
    sequence.write_text("inactive\ninactive\ndeactivating\n")
    result = _run(
        environment | {"FAKE_SERVICE_STATE_SEQUENCE": str(sequence)},
        "--set-hostname",
        "roastpilot",
        "--start",
    )
    assert result.returncode != 0 and "stop the service only when idle" in result.stderr
    assert "unit may remain enabled; rerun or inspect the installer state manually" in result.stderr
    events = log.read_text().splitlines()
    probe = "systemctl <show> <-p> <ActiveState> <--value> <roastpilot-agent>"
    assert events.count(probe) == 3
    assert "systemctl <enable> <roastpilot-agent>" in events
    assert not any(
        "systemctl <start>" in line
        or any(word in line for word in ("restart", "try-restart", "stop", "kill", "disable"))
        for line in events
    )


@pytest.mark.serial
def test_agent_enable_failure_warns_without_lifecycle_reversal(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A possibly partial enable is warned about but never automatically reversed."""
    _, environment, log, _ = installer_harness
    result = _run(environment | {"FAKE_AGENT_ENABLE_FAIL": "1"}, "--set-hostname", "roastpilot")
    assert result.returncode == 31
    assert "unit may remain enabled; rerun or inspect the installer state manually" in result.stderr
    events = log.read_text().splitlines()
    assert "systemctl <enable> <roastpilot-agent>" in events
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
@pytest.mark.parametrize(
    "environment",
    [
        {"FAKE_SYSTEMCTL_FAIL": "show"},
        {"FAKE_SERVICE_STATE": ""},
        {"FAKE_SERVICE_STATE": "bad state"},
    ],
)
def test_initial_service_probe_failures_are_effect_free(
    installer_harness: tuple[Path, dict[str, str], Path, Path], environment: dict[str, str]
) -> None:
    """Transport, empty, and malformed initial state evidence fails closed before apt."""
    _, base_environment, log, _ = installer_harness
    result = _run(base_environment | environment, "--set-hostname", "roastpilot")
    assert result.returncode != 0 and "never restart during a roast" in result.stderr
    events = log.read_text().splitlines()
    assert events.count("systemctl <show> <-p> <ActiveState> <--value> <roastpilot-agent>") == 1
    assert not any(
        line.startswith(prefix)
        for line in events
        for prefix in (
            "apt-get ",
            "pipx ",
            "roastpilot-agent ",
            "tee ",
            "mv ",
            "chown ",
            "chmod ",
            "hostnamectl <set",
            "usermod ",
            "systemctl <daemon",
            "systemctl <enable",
            "systemctl <start",
        )
    )


def _live_config_state(root: Path) -> dict[str, tuple[bytes, int] | None]:
    """Capture the three mutable appliance files and their observable modes."""
    paths = {
        "env": root / "etc/roastpilot-agent/roastpilot-agent.env",
        "yaml": root / "etc/roastpilot-agent/coffee-roaster-mcp.yaml",
        "unit": root / "etc/systemd/system/roastpilot-agent.service",
    }
    return {
        name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
        for name, path in paths.items()
    }


@pytest.mark.serial
@pytest.mark.parametrize("failure", ["yaml", "unit", "enable"])
def test_failed_configuration_generation_restores_the_prior_live_set(
    installer_harness: tuple[Path, dict[str, str], Path, Path], failure: str
) -> None:
    """A failure after live promotion restores every prior configuration member."""
    _, environment, _, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    etc = root / "etc/roastpilot-agent"
    unit_dir = root / "etc/systemd/system"
    etc.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    (etc / "roastpilot-agent.env").write_text(
        "OPENROUTER_API_KEY=old-key\nPORT=8000\n"
        "ROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\n"
        "COFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n"
    )
    (etc / "coffee-roaster-mcp.yaml").write_text("old-yaml\n")
    (unit_dir / "roastpilot-agent.service").write_text(
        "[Service]\nUser=operator\nGroup=operators\n"
    )
    for path, mode in (
        (etc / "roastpilot-agent.env", 0o600),
        (etc / "coffee-roaster-mcp.yaml", 0o640),
        (unit_dir / "roastpilot-agent.service", 0o644),
    ):
        path.chmod(mode)
    before = _live_config_state(root)
    extra = (
        {"FAKE_TEE_FAIL_TARGET": str(etc / ".roastpilot-yaml.fake")}
        if failure == "yaml"
        else {"FAKE_TEE_FAIL_TARGET": str(unit_dir / ".roastpilot-unit.fake")}
        if failure == "unit"
        else {"FAKE_SYSTEMCTL_FAIL": "enable"}
    )
    result = _run(environment | extra, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    assert _live_config_state(root) == before
    events = Path(environment["FAKE_LOG"]).read_text().splitlines()
    if failure == "yaml":
        assert f"tee <--> <{etc / '.roastpilot-yaml.fake'}>" in events
    elif failure == "unit":
        assert f"tee <--> <{unit_dir / '.roastpilot-unit.fake'}>" in events
    if failure == "enable":
        assert "systemctl <enable> <--now> <avahi-daemon>" in events
        assert events.count("systemctl <daemon-reload>") == 2
        reloads = [i for i, event in enumerate(events) if event == "systemctl <daemon-reload>"]
        restores = [
            i
            for i, event in enumerate(events)
            if event.startswith(("cp ", "rm ")) and "roastpilot-config-rollback" not in event
        ]
        assert reloads[-1] > max(restores)
        # Each member is inspected through the privileged seam; this is a
        # mutation guard against replacing any check with shell-local syntax.
        for name, destination in (
            ("roastpilot-agent.env", etc / "roastpilot-agent.env"),
            ("coffee-roaster-mcp.yaml", etc / "coffee-roaster-mcp.yaml"),
            ("roastpilot-agent.service", unit_dir / "roastpilot-agent.service"),
        ):
            expected_snapshot = root / "tmp/roastpilot-config-rollback.fake" / name
            assert events.count(f"test <-f> <{expected_snapshot}>") == 1
            assert events.count(f"test <-L> <{expected_snapshot}>") == 1
            assert f"cp <-p> <--> <{expected_snapshot}> <{destination}>" in events
        snapshot_chmod = next(
            event
            for event in events
            if "roastpilot-config-rollback" in event and event.startswith("chmod ")
        )
        assert "<0700>" in snapshot_chmod
        assert not any(
            "systemctl <start> <roastpilot-agent>" in event
            or any(word in event for word in ("restart", "try-restart", "stop", "kill"))
            for event in events
        )


@pytest.mark.serial
def test_failed_configuration_generation_removes_previously_absent_files(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Rollback removes configuration leaves that did not exist before promotion."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    result = _run(environment | {"FAKE_SYSTEMCTL_FAIL": "enable"}, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    assert _live_config_state(root) == {"env": None, "yaml": None, "unit": None}
    assert "systemctl <enable> <--now> <avahi-daemon>" in log.read_text().splitlines()


@pytest.mark.serial
def test_rollback_failure_still_reloads_and_discards_snapshot(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Rollback remains best-effort when its second daemon reload fails."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    etc = root / "etc/roastpilot-agent"
    unit_dir = root / "etc/systemd/system"
    etc.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    (etc / "roastpilot-agent.env").write_text(
        "OPENROUTER_API_KEY=\nPORT=8000\nROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\n"
        "COFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n"
    )
    (etc / "coffee-roaster-mcp.yaml").write_text("old-yaml\n")
    (unit_dir / "roastpilot-agent.service").write_text(
        "[Service]\nUser=operator\nGroup=operators\n"
    )
    before = _live_config_state(root)
    counter = root.parent / "daemon-count"
    result = _run(
        environment
        | {
            "FAKE_SYSTEMCTL_FAIL": "enable",
            "FAKE_DAEMON_RELOAD_FAIL_ON": "2",
            "FAKE_DAEMON_RELOAD_COUNTER": str(counter),
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0
    assert "rollback incomplete; manual reconciliation required" in result.stderr
    assert _live_config_state(root) == before
    events = log.read_text().splitlines()
    assert "systemctl <enable> <--now> <avahi-daemon>" in events
    assert events.count("systemctl <daemon-reload>") == 2
    assert counter.read_text().strip() == "2"
    assert any(
        event.startswith("rm <-rf>") and "roastpilot-config-rollback" in event for event in events
    )
    assert not list((root / "tmp").glob("roastpilot-config-rollback.*"))
    assert not any(
        any(word in event for word in ("start", "stop", "restart", "kill", "disable"))
        and "roastpilot-agent" in event
        for event in events
    )


@pytest.mark.serial
@pytest.mark.parametrize("failed_member", ["env", "yaml", "unit"])
def test_rollback_cp_failure_continues_to_later_members_and_cleanup(
    installer_harness: tuple[Path, dict[str, str], Path, Path], failed_member: str
) -> None:
    """A failed configuration restore member cannot skip reload or snapshot deletion."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    etc, unit_dir = root / "etc/roastpilot-agent", root / "etc/systemd/system"
    etc.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    env, yaml, unit = (
        etc / "roastpilot-agent.env",
        etc / "coffee-roaster-mcp.yaml",
        unit_dir / "roastpilot-agent.service",
    )
    rollback_fixture_value = "rollback-fixture-value"
    env.write_text(
        f"OPENROUTER_API_KEY={rollback_fixture_value}\nPORT=8000\nROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\nCOFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n"
    )
    yaml.write_bytes(b"prior-yaml\n")
    unit.write_bytes(b"[Service]\nUser=operator\nGroup=operators\n")
    for path, mode in ((env, 0o600), (yaml, 0o640), (unit, 0o644)):
        path.chmod(mode)
    before = _live_config_state(root)
    failed = {"env": env, "yaml": yaml, "unit": unit}[failed_member]
    result = _run(
        environment
        | {
            "ROASTPILOT_INSTALL_API_KEY": "new-api-key",
            "FAKE_SYSTEMCTL_FAIL": "enable",
            "FAKE_CP_FAIL_PATH": str(failed),
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0 and "manual reconciliation required" in result.stderr
    assert f"cannot restore configuration member at {failed}" in result.stderr
    events = log.read_text().splitlines()
    failed_cp = next(
        i
        for i, event in enumerate(events)
        if event.startswith("cp <-p>") and event.endswith(f"> <{failed}>")
    )
    failed_source = events[failed_cp].split("> <")[2]
    assert "roastpilot-config-rollback" in failed_source
    for member in (env, yaml, unit)[("env", "yaml", "unit").index(failed_member) + 1 :]:
        assert any(
            i > failed_cp and event.startswith("cp <-p>") and event.endswith(f"> <{member}>")
            for i, event in enumerate(events)
        )
    reload = max(i for i, event in enumerate(events) if event == "systemctl <daemon-reload>")
    assert reload > failed_cp and any(
        event.startswith("rm <-rf>") and "roastpilot-config-rollback" in event for event in events
    )
    assert not list((root / "tmp").glob("roastpilot-config-rollback.*"))
    after = _live_config_state(root)
    assert (
        after["env"] != before["env"] if failed_member == "env" else after["env"] == before["env"]
    )
    assert (
        after["yaml"] != before["yaml"]
        if failed_member == "yaml"
        else after["yaml"] == before["yaml"]
    )
    assert (
        after["unit"] != before["unit"]
        if failed_member == "unit"
        else after["unit"] == before["unit"]
    )
    assert not _has_roastpilot_agent_lifecycle_mutation(events)
    assert rollback_fixture_value not in result.stdout + result.stderr + log.read_text()


@pytest.mark.serial
def test_rollback_recheck_failure_is_not_masked_by_later_members(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A failed YAML recheck leaves rollback incomplete but continues through the unit."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    etc, unit_dir = root / "etc/roastpilot-agent", root / "etc/systemd/system"
    etc.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    env, yaml, unit = (
        etc / "roastpilot-agent.env",
        etc / "coffee-roaster-mcp.yaml",
        unit_dir / "roastpilot-agent.service",
    )
    env.write_text(
        "OPENROUTER_API_KEY=old\nPORT=8000\nROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\nCOFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n"
    )
    yaml.write_bytes(b"prior-yaml\n")
    unit.write_bytes(b"[Service]\nUser=operator\nGroup=operators\n")
    for path, mode in ((env, 0o600), (yaml, 0o640), (unit, 0o644)):
        path.chmod(mode)
    before = _live_config_state(root)
    result = _run(
        environment
        | {
            "FAKE_SYSTEMCTL_FAIL": "enable",
            "FAKE_TEST_FAIL_PATH": str(yaml),
            "FAKE_TEST_FAIL_ON_COUNT": "7",
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0 and "manual reconciliation required" in result.stderr
    assert f"cannot restore configuration member at {yaml}" in result.stderr
    events = log.read_text().splitlines()
    yaml_symlink_checks = [
        i for i, event in enumerate(events) if event == f"test <!> <-L> <{yaml}>"
    ]
    # The snapshot validation and pre-write recheck precede the injected
    # seventh probe; the final check is the rollback recheck.
    assert len(yaml_symlink_checks) == 3
    assert f"FAKE_TEST_FAILURE <{yaml}>" in events
    failed_recheck = yaml_symlink_checks[-1]
    unit_restore = next(
        i
        for i, event in enumerate(events)
        if i > failed_recheck and event.startswith("cp <-p>") and event.endswith(f"> <{unit}>")
    )
    assert unit_restore > failed_recheck
    assert any(
        event.startswith("rm <-rf>") and "roastpilot-config-rollback" in event for event in events
    )
    assert not list((root / "tmp").glob("roastpilot-config-rollback.*"))
    assert _live_config_state(root)["env"] == before["env"]
    assert _live_config_state(root)["unit"] == before["unit"]
    assert _live_config_state(root)["yaml"] != before["yaml"]


@pytest.mark.serial
def test_restore_snapshot_probe_failure_continues_later_rollback_and_cleanup(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """An indeterminate snapshot member is retained while later rollback work continues."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    etc, unit_dir = root / "etc/roastpilot-agent", root / "etc/systemd/system"
    etc.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    env, yaml, unit = (
        etc / "roastpilot-agent.env",
        etc / "coffee-roaster-mcp.yaml",
        unit_dir / "roastpilot-agent.service",
    )
    env.write_text(
        "OPENROUTER_API_KEY=old\nPORT=8000\nROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\n"
        "COFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n"
    )
    yaml.write_text("prior-yaml\n")
    unit.write_text("[Service]\nUser=operator\nGroup=operators\n")
    before = _live_config_state(root)
    snapshot_yaml = root / "tmp/roastpilot-config-rollback.fake/coffee-roaster-mcp.yaml"
    result = _run(
        environment | {"FAKE_SYSTEMCTL_FAIL": "enable", "FAKE_TEST_FAIL_PATH": str(snapshot_yaml)},
        "--set-hostname",
        "roastpilot",
    )
    events = log.read_text().splitlines()
    assert result.returncode != 0
    assert f"FAKE_TEST_FAILURE <{snapshot_yaml}>" in events
    assert f"cannot restore configuration member at {yaml}" in result.stderr
    assert "rollback incomplete; manual reconciliation required" in result.stderr
    assert f"rm <-f> <--> <{yaml}>" not in events
    assert _live_config_state(root)["yaml"] != before["yaml"]
    failure = events.index(f"FAKE_TEST_FAILURE <{snapshot_yaml}>")
    assert any(
        index > failure and event.startswith("cp <-p>") and event.endswith(f"> <{unit}>")
        for index, event in enumerate(events)
    )
    reload = max(
        index for index, event in enumerate(events) if event == "systemctl <daemon-reload>"
    )
    assert reload > failure
    assert any(
        index > reload and event.startswith("rm <-rf>") and "roastpilot-config-rollback" in event
        for index, event in enumerate(events)
    )
    assert not list((root / "tmp").glob("roastpilot-config-rollback.*"))
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
@pytest.mark.parametrize(
    ("target_kind", "diagnostic"),
    [
        ("model", "model promotion destination failed privileged recheck"),
        ("config", "atomic destination failed privileged recheck"),
    ],
)
def test_privileged_write_recheck_reports_its_failed_destination(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
    target_kind: str,
    diagnostic: str,
) -> None:
    """Each privileged write boundary reports the rejected path before failing closed."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    target = (
        root / "var/lib/roastpilot-agent/models/onnx/int8/model_quantized.onnx"
        if target_kind == "model"
        else root / "etc/roastpilot-agent/roastpilot-agent.env"
    )
    result = _run(
        environment
        | {"FAKE_TEST_FAIL_PATH": str(target)}
        | ({"FAKE_TEST_FAIL_ON_COUNT": "5"} if target_kind == "config" else {}),
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0
    assert diagnostic in result.stderr and str(target) in result.stderr
    events = log.read_text().splitlines()
    assert f"test <!> <-L> <{target}>" in events
    assert f"FAKE_TEST_FAILURE <{target}>" in events


@pytest.mark.serial
def test_snapshot_existing_members_are_decided_through_the_privileged_seam(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Existing configuration members are probed and copied only through sudo's seam."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    etc, unit_dir = root / "etc/roastpilot-agent", root / "etc/systemd/system"
    etc.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    env, yaml, unit = (
        etc / "roastpilot-agent.env",
        etc / "coffee-roaster-mcp.yaml",
        unit_dir / "roastpilot-agent.service",
    )
    env.write_text(
        "OPENROUTER_API_KEY=old\nPORT=8000\nROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\nCOFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n"
    )
    yaml.write_text("old-yaml\n")
    unit.write_text("[Service]\nUser=operator\nGroup=operators\n")
    result = _run(environment | {"FAKE_SYSTEMCTL_FAIL": "enable"}, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    events = log.read_text().splitlines()
    for member in (env, yaml, unit):
        assert f"test <-e> <{member}>" in events
        assert f"test <-f> <{member}>" in events
        assert f"test <-L> <{member}>" in events
        assert any(
            event.startswith(f"cp <-p> <--> <{member}> <") and "roastpilot-config-rollback" in event
            for event in events
        )


@pytest.mark.serial
def test_snapshot_member_probe_failure_is_not_treated_as_absence(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A failed privileged existence probe aborts before a live member can be replaced."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    env = root / "etc/roastpilot-agent/roastpilot-agent.env"
    env.parent.mkdir(parents=True)
    original = (
        "OPENROUTER_API_KEY=old\nPORT=8000\n"
        "ROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\n"
        "COFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n"
    )
    env.write_text(original)
    result = _run(
        environment
        | {
            "FAKE_TEST_FAIL_PATH": str(env),
            "FAKE_TEST_FAIL_ON_COUNT": "1",
        },
        "--set-hostname",
        "roastpilot",
    )
    events = log.read_text().splitlines()
    assert result.returncode != 0
    assert f"FAKE_TEST_FAILURE <{env}>" in events
    assert Path(environment["FAKE_TEST_FAIL_COUNT_FILE"]).read_text() == "3\n"
    assert f"cannot inspect existing configuration destination at {env}" in result.stderr
    assert env.read_text() == original
    assert not any(
        event.endswith(f"> <{env}>") and event.startswith(("tee ", "mv ", "rm <-f>", "cp <-p>"))
        for event in events
    )
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
def test_rollback_refuses_member_changed_to_symlink_and_continues(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A symlinked snapshot member is not restored while later rollback work proceeds."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    etc, unit_dir = root / "etc/roastpilot-agent", root / "etc/systemd/system"
    etc.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    env, yaml, unit = (
        etc / "roastpilot-agent.env",
        etc / "coffee-roaster-mcp.yaml",
        unit_dir / "roastpilot-agent.service",
    )
    env.write_text(
        "OPENROUTER_API_KEY=old\nPORT=8000\nROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\nCOFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n"
    )
    yaml.write_text("old-yaml\n")
    unit.write_text("[Service]\nUser=operator\nGroup=operators\n")
    target = tmp_path / "mutated-yaml"
    target.write_text("not-a-live-config\n")
    snapshot_yaml = root / "tmp/roastpilot-config-rollback.fake/coffee-roaster-mcp.yaml"
    result = _run(
        environment
        | {
            "FAKE_SYSTEMCTL_FAIL": "enable",
            "FAKE_MUTATE_SYMLINK_PATH": str(snapshot_yaml),
            "FAKE_MUTATE_SYMLINK_TARGET": str(target),
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0 and "manual reconciliation required" in result.stderr
    assert f"cannot restore configuration member at {yaml}" in result.stderr
    events = log.read_text().splitlines()
    assert f"FAKE_SYMLINK_MUTATION <{snapshot_yaml}> <{target}>" in events
    snapshot_file_test = next(
        i for i, event in enumerate(events) if event == f"test <-f> <{snapshot_yaml}>"
    )
    snapshot_link_test = next(
        i for i, event in enumerate(events) if event == f"test <-L> <{snapshot_yaml}>"
    )
    assert snapshot_file_test < snapshot_link_test
    assert not any(event == f"cp <-p> <--> <{snapshot_yaml}> <{yaml}>" for event in events)
    unit_restore = next(
        i
        for i, event in enumerate(events)
        if i > snapshot_link_test and event.startswith("cp <-p>") and event.endswith(f"> <{unit}>")
    )
    reload = max(i for i, event in enumerate(events) if event == "systemctl <daemon-reload>")
    assert reload > unit_restore
    assert any(
        event.startswith("rm <-rf>") and "roastpilot-config-rollback" in event for event in events
    )
    assert not list((root / "tmp").glob("roastpilot-config-rollback.*"))
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
def test_rollback_removal_failure_for_previously_absent_member_continues(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A failed absent-member removal is retained while later rollback work continues."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    env = root / "etc/roastpilot-agent/roastpilot-agent.env"
    yaml = root / "etc/roastpilot-agent/coffee-roaster-mcp.yaml"
    unit = root / "etc/systemd/system/roastpilot-agent.service"
    result = _run(
        environment | {"FAKE_SYSTEMCTL_FAIL": "enable", "FAKE_RM_FAIL_PATH": str(env)},
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0 and "manual reconciliation required" in result.stderr
    assert f"cannot restore configuration member at {env}" in result.stderr
    events = log.read_text().splitlines()
    failed_removal = next(i for i, event in enumerate(events) if event == f"rm <-f> <--> <{env}>")
    yaml_removal = next(
        i
        for i, event in enumerate(events)
        if i > failed_removal and event == f"rm <-f> <--> <{yaml}>"
    )
    unit_removal = next(
        i
        for i, event in enumerate(events)
        if i > yaml_removal and event == f"rm <-f> <--> <{unit}>"
    )
    reload = max(i for i, event in enumerate(events) if event == "systemctl <daemon-reload>")
    assert reload > unit_removal
    assert any(
        event.startswith("rm <-rf>") and "roastpilot-config-rollback" in event for event in events
    )
    assert not list((root / "tmp").glob("roastpilot-config-rollback.*"))
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
def test_snapshot_discard_failure_reports_the_retained_path_without_secret_contents(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A final snapshot-delete failure identifies only its path for reconciliation."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    env = root / "etc/roastpilot-agent/roastpilot-agent.env"
    env.parent.mkdir(parents=True)
    snapshot_fixture_value = "snapshot-fixture-value"
    env.write_text(
        f"OPENROUTER_API_KEY={snapshot_fixture_value}\nPORT=8000\nROASTPILOT_DB=/var/lib/roastpilot-agent/roastpilot.sqlite3\nCOFFEE_ROASTER_MCP_CONFIG=/etc/roastpilot-agent/coffee-roaster-mcp.yaml\n"
    )
    snapshot = root / "tmp/roastpilot-config-rollback.fake"
    result = _run(
        environment | {"FAKE_SYSTEMCTL_FAIL": "enable", "FAKE_RM_FAIL_PATH": str(snapshot)},
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0 and "manual reconciliation required" in result.stderr
    assert f"retained configuration snapshot at {snapshot}" in result.stderr
    assert snapshot_fixture_value not in result.stdout + result.stderr
    events = log.read_text().splitlines()
    assert f"rm <-rf> <--> <{snapshot}>" in events
    assert env.read_text().startswith(f"OPENROUTER_API_KEY={snapshot_fixture_value}\n")
    assert events.count("systemctl <daemon-reload>") == 2
    assert snapshot.is_dir()


@pytest.mark.serial
def test_success_snapshot_discard_failure_keeps_committed_configuration(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A post-enable snapshot cleanup failure cannot replay configuration rollback."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    snapshot = root / "tmp/roastpilot-config-rollback.fake"
    secret = "completion-secret-must-not-leak"
    result = _run(
        environment | {"FAKE_RM_FAIL_PATH": str(snapshot), "ROASTPILOT_INSTALL_API_KEY": secret},
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode == 1
    assert f"installation completed; retained configuration snapshot at {snapshot}" in result.stderr
    assert (
        "installation completed; manually remove the retained secret-bearing configuration snapshot"
        in result.stderr
    )
    assert secret not in result.stdout + result.stderr + log.read_text()
    for diagnostic in (
        "install failed after hostname change",
        "install failed after enabling roastpilot-agent",
        "install failed after enabling Avahi",
        "application/configuration skew may require manual reconciliation",
    ):
        assert diagnostic not in result.stderr
    assert snapshot.is_dir()
    assert _live_config_state(root)["env"] is not None
    assert _live_config_state(root)["yaml"] is not None
    assert _live_config_state(root)["unit"] is not None
    events = log.read_text().splitlines()
    assert f"rm <-rf> <--> <{snapshot}>" in events
    assert not any(
        event.startswith(("cp <-p>", "rm <-f>"))
        and any(
            name in event
            for name in (
                "roastpilot-agent.env",
                "coffee-roaster-mcp.yaml",
                "roastpilot-agent.service",
            )
        )
        for event in events
    )
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
@pytest.mark.parametrize("cleanup_kind", ["stage", "restore"])
def test_post_commit_cleanup_failure_reports_only_the_retained_target(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path, cleanup_kind: str
) -> None:
    """Committed installs never borrow rollback diagnostics for later artifact cleanup."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    if cleanup_kind == "stage":
        target = root / "tmp/roastpilot-install.fake"
        invocation = ("--set-hostname", "roastpilot")
    else:
        wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
        wheel.write_text("wheel")
        _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
        target = Path(environment["FAKE_OPERATOR_HOME"]) / ".cache/roastpilot-restore.fake"
        invocation = ("--set-hostname", "roastpilot", "--version", "2.0")
    result = _run(environment | {"FAKE_RM_FAIL_PATH": str(target)}, *invocation)
    assert result.returncode == 1
    retained_name = "staging" if cleanup_kind == "stage" else "restore artifact"
    assert (
        f"installation completed; manually remove retained {retained_name} directory at {target}"
        in result.stderr
    )
    for diagnostic in (
        "install failed after hostname change",
        "install failed after enabling roastpilot-agent",
        "install failed after enabling Avahi",
        "application/configuration skew",
        "rollback incomplete",
    ):
        assert diagnostic not in result.stderr
    events = log.read_text().splitlines()
    assert f"rm <-rf> <--> <{target}>" in events
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_fresh_incapable_application_is_removed_or_named_for_manual_cleanup(
    installer_harness: tuple[Path, dict[str, str], Path, Path], cleanup_fails: bool
) -> None:
    """A fresh incapable install is never left silently wedged in pipx."""
    _, environment, log, _ = installer_harness
    result = _run(
        environment
        | {"FAKE_PIPX_MCP_MISSING": "1"}
        | ({"FAKE_PIPX_FAIL_FRESH_CLEANUP": "1"} if cleanup_fails else {}),
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0
    events = log.read_text().splitlines()
    assert "pipx <uninstall> <--> <roastpilot-agent>" in events
    assert (
        "manually remove incapable roastpilot-agent environment" in result.stderr
    ) is cleanup_fails
    assert (
        "application/configuration skew may require manual reconciliation" in result.stderr
    ) is cleanup_fails
    assert not any(event.startswith("roastpilot-agent <appliance>") for event in events)
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
def test_partial_staged_venv_is_registered_before_failed_stage_install(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A staged-install failure after filesystem creation still invokes staged cleanup."""
    _, environment, log, _ = installer_harness
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", "roastpilot-agent[pi]==1.2")
    result = _run(
        environment | {"FAKE_PIPX_FAIL_STAGE_AFTER_CREATE": "1"},
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert result.returncode != 0
    events = log.read_text().splitlines()
    assert any(event.startswith("FAKE_STAGE_PARTIAL_CREATION") for event in events)
    assert any("roastpilot-stage-" in event and "<uninstall>" in event for event in events)


@pytest.mark.serial
@pytest.mark.parametrize("root", ["", "relative", "/"])
def test_test_mode_requires_a_nonempty_nonhost_install_root_before_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], root: str
) -> None:
    """Test mode cannot fall through to host paths without its explicit fake root."""
    _, environment, log, _ = installer_harness
    result = _run(
        environment | {"ROASTPILOT_INSTALL_TEST_ROOT": root}, "--set-hostname", "roastpilot"
    )
    assert result.returncode != 0
    assert (
        "test install root is required" in result.stderr or "invalid install root" in result.stderr
    )
    assert not log.exists()


@pytest.mark.serial
def test_second_hostname_query_failure_has_a_distinct_fail_closed_diagnostic(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """The post-update hostname query failure is not misreported as a value mismatch."""
    _, environment, log, _ = installer_harness
    result = _run(
        environment | {"FAKE_HOSTNAME_VERIFY_QUERY_FAIL": "1"}, "--set-hostname", "roastpilot"
    )
    assert result.returncode != 0
    assert "cannot verify hostname after update" in result.stderr
    assert "hostname verification failed" not in result.stderr
    assert "FAKE_HOSTNAME_VERIFY_QUERY_FAILURE" in log.read_text().splitlines()


@pytest.mark.serial
def test_exact_mktemp_prefix_with_empty_suffix_is_not_cleanup_eligible(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """The mktemp validator requires a real suffix, not merely an exact prefix match."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    prefix = root / "tmp/roastpilot-install."
    result = _run(
        environment
        | {"FAKE_MKTEMP_TEMPLATE": str(prefix) + "XXXXXX", "FAKE_MKTEMP_RESULT": str(prefix)},
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0
    events = log.read_text().splitlines()
    assert f"FAKE_MKTEMP_RESULT <{prefix}>" in events
    assert f"rm <-rf> <--> <{prefix}>" not in events


@pytest.mark.serial
@pytest.mark.parametrize("site", ["stage", "snapshot", "restore", "model", "env"])
def test_every_mktemp_site_rejects_an_exact_prefix_without_suffix(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path, site: str
) -> None:
    """Every temporary class requires a non-empty single-component mktemp suffix."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    if site == "stage":
        template = root / "tmp/roastpilot-install.XXXXXX"
    elif site == "snapshot":
        template = root / "tmp/roastpilot-config-rollback.XXXXXX"
    elif site == "restore":
        wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
        wheel.write_text("wheel")
        _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
        template = Path(environment["FAKE_OPERATOR_HOME"]) / ".cache/roastpilot-restore.XXXXXX"
    elif site == "model":
        template = root / "var/lib/roastpilot-agent/models/onnx/int8/.roastpilot-model.XXXXXX"
    else:
        template = root / "etc/roastpilot-agent/.roastpilot-env.XXXXXX"
    candidate = Path(str(template).removesuffix("XXXXXX"))
    result = _run(
        environment | {"FAKE_MKTEMP_TEMPLATE": str(template), "FAKE_MKTEMP_RESULT": str(candidate)},
        "--set-hostname",
        "roastpilot",
        *(("--version", "2.0") if site == "restore" else ()),
    )
    assert result.returncode != 0
    events = log.read_text().splitlines()
    assert f"FAKE_MKTEMP_RESULT <{candidate}>" in events
    assert not any(
        event.startswith(("rm ", "mv ")) and f"<{candidate}>" in event for event in events
    )


@pytest.mark.serial
def test_preupdate_hostname_query_failure_stops_before_hostname_or_service_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """The pre-update hostname query failure is distinct and has no follow-on mutations."""
    _, environment, log, _ = installer_harness
    result = _run(environment | {"FAKE_HOSTNAME_QUERY_FAIL": "1"}, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    assert "cannot determine static hostname before update" in result.stderr
    events = log.read_text().splitlines()
    assert "FAKE_HOSTNAME_QUERY_FAILURE" in events
    assert not any(event.startswith("hostnamectl <set-hostname>") for event in events)
    assert not any(
        event.startswith(("apt-get ", "pipx ", "systemctl <enable>")) for event in events
    )
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
@pytest.mark.parametrize(
    "record",
    [
        "other:x:1000:1000::/home/operator:/bin/sh",
        "operator:x:1000:1000::/:/bin/sh",
        "operator:x:1000:1000:://:/bin/sh",
        "operator:x:1000:1000::/home/./operator:/bin/sh",
        "operator:x:1000:1000::/home/../operator:/bin/sh",
        "operator:x:1000:1000::/home/operator/.:/bin/sh",
        "operator:x:1000:1000::/home/operator/..:/bin/sh",
        "operator:x:1000:1000::relative:/bin/sh",
        "operator:x:1000:1000::/home/operator name:/bin/sh",
        "operator:x:1000:1000::/home/operator#name:/bin/sh",
        "operator:x:1000:1000::/home/operator'name:/bin/sh",
        "operator:x:1000:1000::/home/$operator:/bin/sh",
        "operator:x:1000:1000::/home/operator%h:/bin/sh",
        "operator:x:1000:1000::/home/operator@@token:/bin/sh",
        "operator:x:1000:1000::/home/operator\u200b:/bin/sh",
        "operator:x:1000:1000::/home/operator\u2028:/bin/sh",
        "operator:x:1000:1000::/home/operator\u2029:/bin/sh",
    ],
)
def test_unsafe_getent_identity_and_home_shapes_fail_before_installer_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], record: str
) -> None:
    """Every guarded passwd identity/home shape is rejected before installer mutations."""
    _, environment, log, _ = installer_harness
    result = _run(environment | {"FAKE_GETENT_RECORD": record}, "--set-hostname", "roastpilot")
    assert result.returncode != 0 and "unsafe operator" in result.stderr
    assert not any(
        event.startswith(("apt-get ", "pipx ", "roastpilot-agent ", "systemctl <enable>"))
        for event in log.read_text().splitlines()
    )


@pytest.mark.serial
@pytest.mark.parametrize(
    ("override", "diagnostic"),
    [
        ({"FAKE_ID_USER": "a" * 33}, "unsafe operator identity"),
        ({"FAKE_ID_GROUP": "a" * 33}, "unsafe operator identity"),
        (
            {"FAKE_GETENT_RECORD": "operator:x:1000:0::/home/operator:/bin/sh"},
            "unsafe operator identity",
        ),
        (
            {"FAKE_GETENT_RECORD": "operator:x:1000:1000::/tmp/operator:/bin/sh"},
            "unsafe operator home",
        ),
        (
            {"FAKE_GETENT_RECORD": "operator:x:1000:1000::/var/tmp/operator:/bin/sh"},
            "unsafe operator home",
        ),
    ],
)
def test_renderer_identity_preconditions_fail_before_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
    override: dict[str, str],
    diagnostic: str,
) -> None:
    """Renderer-only identity constraints are enforced before package mutation."""
    _, environment, log, _ = installer_harness
    result = _run(environment | override, "--set-hostname", "roastpilot")
    events = log.read_text().splitlines()
    assert result.returncode != 0 and diagnostic in result.stderr
    assert any(event.startswith(("id ", "getent ")) for event in events)
    assert not any(event.startswith(("apt-get ", "pipx ", "roastpilot-agent ")) for event in events)


@pytest.mark.serial
def test_missing_dialout_with_audio_still_issues_the_exact_group_repair(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """Membership repair uses the exact combined dialout/audio usermod invocation."""
    _, environment, log, _ = installer_harness
    Path(environment["FAKE_GROUPS"]).write_text("audio\n")
    assert _run(environment, "--set-hostname", "roastpilot").returncode == 0
    assert "usermod <-aG> <dialout,audio> <--> <operator>" in log.read_text().splitlines()


@pytest.mark.serial
@pytest.mark.parametrize("wheel", ["/tmp/wheel\n.whl", "/tmp/wheel\r.whl"])
def test_wheel_control_characters_fail_before_path_or_installer_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], wheel: str
) -> None:
    """Wheel selectors reject control bytes before filesystem or installer work."""
    _, environment, log, _ = installer_harness
    result = _run(environment, "--set-hostname", "roastpilot", "--wheel", wheel)
    assert result.returncode != 0 and "wheel selector contains control characters" in result.stderr
    assert not log.exists()


@pytest.mark.serial
def test_untrusted_configuration_snapshot_path_is_retained_never_recursively_deleted(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A fake mktemp escape cannot become a privileged recursive-delete target."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    template = root / "tmp/roastpilot-config-rollback.XXXXXX"
    unexpected = root / "tmp/roastpilot-config-rollback.attacker/nested"
    result = _run(
        environment
        | {
            "FAKE_MKTEMP_TEMPLATE": str(template),
            "FAKE_MKTEMP_RESULT": str(unexpected),
        },
        "--set-hostname",
        "roastpilot",
    )
    assert result.returncode != 0
    assert f"retained untrusted configuration snapshot at {unexpected}" in result.stderr
    events = log.read_text().splitlines()
    assert f"FAKE_MKTEMP_RESULT <{unexpected}>" in events
    assert f"rm <-rf> <--> <{unexpected}>" not in events
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
def test_untrusted_restore_artifact_path_is_retained_never_recursively_deleted(
    installer_harness: tuple[Path, dict[str, str], Path, Path], tmp_path: Path
) -> None:
    """A fake user-cache mktemp escape cannot become a cleanup delete target."""
    _, environment, log, _ = installer_harness
    wheel = tmp_path / "roastpilot_agent-1.2-py3-none-any.whl"
    wheel.write_text("wheel")
    _pipx_state(Path(environment["FAKE_PIPX_STATE"]), "1.2", f"{wheel}[pi]")
    cache = Path(environment["FAKE_OPERATOR_HOME"]) / ".cache"
    template = cache / "roastpilot-restore.XXXXXX"
    unexpected = cache / "roastpilot-restore.attacker/nested"
    result = _run(
        environment
        | {
            "FAKE_MKTEMP_TEMPLATE": str(template),
            "FAKE_MKTEMP_RESULT": str(unexpected),
        },
        "--set-hostname",
        "roastpilot",
        "--version",
        "2.0",
    )
    assert result.returncode != 0
    assert f"retained untrusted restore artifact directory at {unexpected}" in result.stderr
    events = log.read_text().splitlines()
    assert f"FAKE_MKTEMP_RESULT <{unexpected}>" in events
    assert f"rm <-rf> <--> <{unexpected}>" not in events
    assert "pipx <uninstall> <--> <roastpilot-agent>" not in events
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
@pytest.mark.parametrize(
    ("failure", "expected_event"),
    [
        ("model", "FAKE_SHA256_CORRUPTION"),
        ("config", "tee <-->"),
        ("agent-enable", "systemctl <enable> <roastpilot-agent>"),
    ],
)
def test_application_change_warns_of_configuration_skew_for_later_failures(
    installer_harness: tuple[Path, dict[str, str], Path, Path], failure: str, expected_event: str
) -> None:
    """Every later failure after installation identifies possible app/config skew."""
    _, environment, log, _ = installer_harness
    root = Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
    if failure == "model":
        injection = {
            "FAKE_SHA256_BAD_PATH": str(
                root / "tmp/roastpilot-install.fake/models/onnx/int8/model_quantized.onnx"
            )
        }
    elif failure == "config":
        injection = {
            "FAKE_TEE_FAIL_TARGET": str(root / "etc/roastpilot-agent/.roastpilot-env.fake")
        }
    else:
        injection = {"FAKE_AGENT_ENABLE_FAIL": "1"}
    result = _run(environment | injection, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    assert (
        "install failed after application replacement; application/configuration skew may require manual reconciliation"
        in result.stderr
    )
    events = log.read_text().splitlines()
    assert any(expected_event in event for event in events)
    assert not _has_roastpilot_agent_lifecycle_mutation(events)


@pytest.mark.serial
def test_success_has_no_application_skew_or_avahi_residue_warning(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A completed installation does not emit failure-only residue diagnostics."""
    _, environment, _, _ = installer_harness
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode == 0
    assert "application/configuration skew may require manual reconciliation" not in result.stderr
    assert "install failed after enabling Avahi; Avahi enablement may remain" not in result.stderr


@pytest.mark.serial
@pytest.mark.parametrize(
    "unit_text",
    [
        "[Service]\nUser=other\nGroup=operators\n",
        "[Service]\nUser=operator\nGroup=other\n",
        "[Service]\nUser=operator\nGroup=operators\nUser=operator\n",
        "[Service]\n User=operator\nGroup=operators\n User=operator\n",
        "[Service]\n User=other\nGroup=operators\n",
        "[Service]\nUser=operator\n Group=other\n",
        "[Service]\nUser=operator\nGroup=operators\n User=other\n",
        "[Service]\nUser=operator\nGroup=operators\n Group=other\n",
        "[Service]\nUser=operator\nGroup=operators\nUser = other\n",
        "[Service]\nUser=operator\nGroup=operators\nGroup = other\n",
        "[Unit]\nUser=operator\n[Service]\nUser=operator\nGroup=operators\n",
        "[Unit]\nGroup=operators\n[Service]\nUser=operator\nGroup=operators\n",
        "[Service]\nUser=operator\nGroup=operators\n[Service]\n",
        "[Service]\nUser=operator\n",
    ],
)
def test_existing_unit_identity_evidence_fails_before_installer_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path], unit_text: str
) -> None:
    """Existing managed-unit identity evidence must be exact before apt or pipx."""
    _, environment, log, _ = installer_harness
    unit = (
        Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
        / "etc/systemd/system/roastpilot-agent.service"
    )
    unit.parent.mkdir(parents=True)
    unit.write_text(unit_text)
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    assert not any(
        line.startswith(("apt-get ", "pipx ", "roastpilot-agent "))
        for line in log.read_text().splitlines()
    )


@pytest.mark.serial
@pytest.mark.parametrize(
    "unit_text",
    [
        "[Service]\nUser=operator\nGroup=operators\n",
        "[Service]\n User=operator\n Group=operators\n",
        "[Service]\n User = operator \n Group = operators\n",
        "[Unit]\nDescription=managed\n[Service]\n User = operator \n Group = operators\n",
    ],
)
def test_matching_existing_unit_identity_allows_maintenance(
    installer_harness: tuple[Path, dict[str, str], Path, Path], unit_text: str
) -> None:
    """A matching existing unit, including indentation, remains eligible for maintenance."""
    _, environment, _, _ = installer_harness
    unit = (
        Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
        / "etc/systemd/system/roastpilot-agent.service"
    )
    unit.parent.mkdir(parents=True)
    unit.write_text(unit_text)
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode == 0, result.stderr
    committed = _live_config_state(Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"]))
    assert committed["env"] is not None and b"PORT=8000" in committed["env"][0]
    assert committed["yaml"] is not None and b"transport:" in committed["yaml"][0]
    assert committed["unit"] is not None and b"Description=RoastPilot" in committed["unit"][0]
    assert not list(
        (Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"]) / "tmp").glob(
            "roastpilot-config-rollback.*"
        )
    )


@pytest.mark.serial
def test_symlinked_existing_unit_identity_fails_before_installer_effects(
    installer_harness: tuple[Path, dict[str, str], Path, Path],
) -> None:
    """A symlink cannot provide trusted maintenance identity evidence."""
    _, environment, log, _ = installer_harness
    unit = (
        Path(environment["ROASTPILOT_INSTALL_TEST_ROOT"])
        / "etc/systemd/system/roastpilot-agent.service"
    )
    unit.parent.mkdir(parents=True)
    target = unit.with_name("other.service")
    target.write_text("[Service]\nUser=operator\nGroup=operators\n")
    unit.symlink_to(target)
    result = _run(environment, "--set-hostname", "roastpilot")
    assert result.returncode != 0
    assert not any(
        line.startswith(("apt-get ", "pipx ", "roastpilot-agent "))
        for line in log.read_text().splitlines()
    )
