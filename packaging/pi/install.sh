#!/usr/bin/env bash
# Native Pi appliance installer (issue #138, E11-S2 slice 3).
#
# Keep every effect in functions and retain the one call at EOF: a partial
# `curl | bash` download defines functions but cannot make a privileged change.
set -euo pipefail

die() {
    printf '%s\n' "install failed: $*" >&2
    return 1
}

run_privileged() {
    # The installer refuses root, so this is deliberately the sole sudo seam.
    sudo -- "$@"
}

rooted_path() {
    local install_root="${ROASTPILOT_INSTALL_ROOT:-/}"
    local absolute_path="$1"
    [[ "$absolute_path" == /* ]] || die "internal destination is not absolute"
    if [[ "$install_root" == "/" ]]; then
        printf '%s\n' "$absolute_path"
    else
        printf '%s%s\n' "${install_root%/}" "$absolute_path"
    fi
}

validate_install_root() {
    local install_root="${ROASTPILOT_INSTALL_ROOT:-/}"
    [[ "$install_root" == /* ]] || die "install root must be absolute"
    [[ "/${install_root#/}/" != *"/../"* && "${install_root%/}" != "." ]] || die "install root must not contain .."
}

is_dns_label() {
    [[ "$1" =~ ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]]
}

usage() {
    cat <<'EOF'
Usage: install.sh --serial-port PATH --audio-device SUBSTRING [options]

Options:
  --yes                         permit non-interactive execution
  --allow-unsupported-arch      permit an architecture other than aarch64
  --version VERSION             install this roastpilot-agent version
  --wheel PATH                  install this local wheel (or ROASTPILOT_INSTALL_WHEEL)
  --from-dir DIR                model source for air-gapped installation
  --port PORT                   appliance HTTP port (default: 8000)
  --api-key KEY                 write this key only to the protected env file
  --set-hostname HOST           explicitly set the static hostname (roastpilot)
  --start                       start roastpilot-agent after enabling it
EOF
}

parse_arguments() {
    INSTALL_ASSUME_YES=0
    ALLOW_UNSUPPORTED_ARCH=0
    START_SERVICE=0
    REQUESTED_VERSION=""
    REQUESTED_WHEEL="${ROASTPILOT_INSTALL_WHEEL:-}"
    MODEL_FROM_DIR=""
    PORT="8000"
    API_KEY="${ROASTPILOT_INSTALL_API_KEY:-${OPENROUTER_API_KEY:-}}"
    SERIAL_PORT="${ROASTPILOT_INSTALL_SERIAL_PORT:-}"
    AUDIO_DEVICE="${ROASTPILOT_INSTALL_AUDIO_DEVICE:-}"
    REQUESTED_HOSTNAME=""
    while (($#)); do
        case "$1" in
            --yes) INSTALL_ASSUME_YES=1 ;;
            --allow-unsupported-arch) ALLOW_UNSUPPORTED_ARCH=1 ;;
            --start) START_SERVICE=1 ;;
            --version) REQUESTED_VERSION="${2:?--version needs a value}"; shift ;;
            --wheel) REQUESTED_WHEEL="${2:?--wheel needs a path}"; shift ;;
            --from-dir) MODEL_FROM_DIR="${2:?--from-dir needs a path}"; shift ;;
            --port) PORT="${2:?--port needs a value}"; shift ;;
            --api-key) API_KEY="${2:?--api-key needs a value}"; shift ;;
            --serial-port) SERIAL_PORT="${2:?--serial-port needs a path}"; shift ;;
            --audio-device) AUDIO_DEVICE="${2:?--audio-device needs a value}"; shift ;;
            --set-hostname) REQUESTED_HOSTNAME="${2:?--set-hostname needs a value}"; shift ;;
            --help) usage; return 0 ;;
            *) die "unknown option: $1" ;;
        esac
        shift
    done
    [[ -n "$SERIAL_PORT" ]] || die "--serial-port is required"
    [[ -n "$AUDIO_DEVICE" ]] || die "--audio-device is required"
    [[ "$API_KEY" != *$'\n'* && "$API_KEY" != *$'\r'* ]] || die "API key must be one line"
    [[ -z "$REQUESTED_VERSION" || -z "$REQUESTED_WHEEL" ]] || die "choose --version or --wheel"
    if [[ -n "$REQUESTED_HOSTNAME" ]]; then
        is_dns_label "$REQUESTED_HOSTNAME" || die "hostname must be a lowercase DNS label"
        [[ "$REQUESTED_HOSTNAME" == "roastpilot" ]] || die "only --set-hostname roastpilot is supported"
    fi
}

preflight() {
    validate_install_root
    [[ "$(id -u)" != "0" ]] || die "never run pipx as root"
    if [[ "$(uname -m)" != "aarch64" && "$ALLOW_UNSUPPORTED_ARCH" != 1 ]]; then
        die "this installer supports aarch64 only; pass --allow-unsupported-arch to override"
    fi
    local os_release="${ROASTPILOT_INSTALL_OS_RELEASE:-/etc/os-release}"
    [[ -r "$os_release" ]] || die "unsupported operating system"
    # Pi OS is Debian-family.  Do not infer another package manager.
    . "$os_release"
    [[ "${ID:-}" == "debian" || "${ID_LIKE:-}" == *debian* ]] || die "unsupported OS/package manager"
    command -v apt-get >/dev/null || die "unsupported OS/package manager"
    if [[ "$INSTALL_ASSUME_YES" != 1 && "${ROASTPILOT_INSTALL_ASSUME_YES:-}" != 1 && ! -t 0 ]]; then
        die "stdin is not a TTY; pass --yes or set ROASTPILOT_INSTALL_ASSUME_YES=1"
    fi
    if [[ -z "$REQUESTED_HOSTNAME" && "$(hostnamectl --static)" != "roastpilot" ]]; then
        die "hostname is not roastpilot; re-run with --set-hostname roastpilot"
    fi
}

installed_pipx_state() {
    pipx list --json 2>/dev/null || true
}

install_application() {
    local state package_spec
    state="$(installed_pipx_state)"
    if ! grep -Fq '"roastpilot-agent"' <<<"$state"; then
        if [[ -n "$REQUESTED_WHEEL" ]]; then package_spec="$REQUESTED_WHEEL"
        elif [[ -n "$REQUESTED_VERSION" ]]; then package_spec="roastpilot-agent[pi]==$REQUESTED_VERSION"
        else package_spec="roastpilot-agent[pi]"; fi
        pipx install "$package_spec"
        return
    fi
    if [[ -n "$REQUESTED_WHEEL" ]] && ! grep -Fq "$REQUESTED_WHEEL" <<<"$state"; then
        pipx uninstall roastpilot-agent
        pipx install "$REQUESTED_WHEEL"
    elif [[ -n "$REQUESTED_VERSION" ]] && ! grep -Fq "$REQUESTED_VERSION" <<<"$state"; then
        pipx uninstall roastpilot-agent
        pipx install "roastpilot-agent[pi]==$REQUESTED_VERSION"
    fi
}

install_model_and_render() {
    local model_dir stage_dir
    model_dir="$(rooted_path /var/lib/roastpilot-agent/models)"
    stage_dir="$(mktemp -d)"
    STAGE_DIR="$stage_dir"
    if [[ -n "$MODEL_FROM_DIR" ]]; then
        run_privileged roastpilot-agent appliance model install --dest "$model_dir" --from-dir "$MODEL_FROM_DIR"
    else
        run_privileged roastpilot-agent appliance model install --dest "$model_dir"
    fi
    roastpilot-agent appliance render --output-dir "$stage_dir" --port "$PORT" \
        --operator-user "$USER" --operator-home "$HOME" --serial-port "$SERIAL_PORT" \
        --audio-device "$AUDIO_DEVICE"
}

write_env_with_key() {
    local env_file="$1" line
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == OPENROUTER_API_KEY=* ]]; then
            printf 'OPENROUTER_API_KEY=%s\n' "$API_KEY"
        else
            printf '%s\n' "$line"
        fi
    done < "$STAGE_DIR/roastpilot-agent.env" | run_privileged tee "$env_file" >/dev/null
}

install_rendered_files() {
    local etc_dir var_dir env_file yaml_file unit_file prior_hostname
    etc_dir="$(rooted_path /etc/roastpilot-agent)"
    var_dir="$(rooted_path /var/lib/roastpilot-agent)"
    env_file="$etc_dir/roastpilot-agent.env"
    yaml_file="$etc_dir/coffee-roaster-mcp.yaml"
    unit_file="$(rooted_path /etc/systemd/system/roastpilot-agent.service)"
    run_privileged mkdir -p "$etc_dir" "$var_dir"
    # The renderer deliberately leaves this blank; inserting it only through
    # stdin keeps the key out of command arguments, logs, and the unit.
    if [[ -n "$API_KEY" ]]; then
        write_env_with_key "$env_file"
    else
        run_privileged install -m 0600 "$STAGE_DIR/roastpilot-agent.env" "$env_file"
    fi
    run_privileged chmod 0600 "$env_file"
    run_privileged install -m 0644 "$STAGE_DIR/coffee-roaster-mcp.appliance.yaml" "$yaml_file"
    run_privileged install -m 0644 "$STAGE_DIR/roastpilot-agent.service" "$unit_file"
    if ! id -nG "$USER" | tr ' ' '\n' | grep -Fxq dialout || ! id -nG "$USER" | tr ' ' '\n' | grep -Fxq audio; then
        run_privileged usermod -aG dialout,audio "$USER"
    fi
    if [[ -n "$REQUESTED_HOSTNAME" ]]; then
        prior_hostname="$(hostnamectl --static)"
        if [[ "$prior_hostname" != "$REQUESTED_HOSTNAME" ]]; then
            printf '%s\n' "$prior_hostname" | run_privileged tee "$(rooted_path /var/lib/roastpilot-agent/prior-static-hostname)" >/dev/null
            run_privileged chmod 0600 "$(rooted_path /var/lib/roastpilot-agent/prior-static-hostname)"
            run_privileged hostnamectl set-hostname "$REQUESTED_HOSTNAME"
            [[ "$(hostnamectl --static)" == "$REQUESTED_HOSTNAME" ]] || die "hostname verification failed"
        fi
    fi
}

enable_services() {
    run_privileged systemctl daemon-reload
    run_privileged systemctl enable roastpilot-agent
    run_privileged systemctl enable --now avahi-daemon
    if [[ "$START_SERVICE" == 1 ]]; then run_privileged systemctl start roastpilot-agent; fi
}

summary() {
    printf '%s\n' "Installed: unit enabled; model verified."
    printf '%s\n' "Open http://roastpilot.local:$PORT and inspect logs with: journalctl -u roastpilot-agent -f"
    if [[ -z "$API_KEY" ]]; then printf '%s\n' "Edit the protected env file to add the OpenRouter key before using advice."; fi
}

main() {
    local STAGE_DIR=""
    trap '[[ -z "${STAGE_DIR:-}" ]] || rm -rf -- "$STAGE_DIR"' EXIT
    parse_arguments "$@"
    preflight
    run_privileged apt-get install -y libportaudio2 pipx avahi-daemon
    install_application
    install_model_and_render
    install_rendered_files
    enable_services
    summary
}

main "$@"
