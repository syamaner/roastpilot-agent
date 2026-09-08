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
    [[ "$install_root" == /* && "/${install_root#/}/" != *"/../"* ]] || die "invalid install root"
    [[ "$install_root" != *$'\n'* && "$install_root" != *$'\r'* ]] || die "invalid install root"
}

validate_destination() {
    local destination="$1" component current="/"
    [[ "$destination" == /* && "$destination" != *"/../"* ]] || die "invalid destination"
    IFS=/ read -r -a components <<< "${destination#/}"
    for component in "${components[@]}"; do
        [[ -n "$component" && "$component" != "." && "$component" != ".." ]] || die "invalid destination"
        current="${current%/}/$component"
        [[ ! -L "$current" ]] || die "destination traverses a symlink"
    done
}

prepare_destination_parents() {
    local destination
    for destination in "$@"; do
        validate_destination "$destination"
        run_privileged mkdir -p -- "$(dirname -- "$destination")"
    done
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
    [[ -z "$REQUESTED_VERSION" || "$REQUESTED_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._+!-]*$ ]] || die "invalid version selector"
    if [[ -n "$REQUESTED_WHEEL" ]]; then
        [[ "$REQUESTED_WHEEL" == /* && "$REQUESTED_WHEEL" != *"/../"* ]] || die "invalid wheel selector"
        [[ -f "$REQUESTED_WHEEL" && ! -L "$REQUESTED_WHEEL" ]] || die "wheel must be a regular file"
        REQUESTED_WHEEL="$(readlink -f -- "$REQUESTED_WHEEL")"
    fi
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
    # Parse only the two required os-release fields as inert text.  In
    # particular, never source a caller-selected file.
    local line key value id="" id_like=""
    [[ -r "$os_release" ]] || die "unsupported operating system"
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" == *=* ]] || die "malformed operating system data"
        key="${line%%=*}"
        value="${line#*=}"
        case "$key" in
            ID|ID_LIKE)
                if [[ "$value" == \"*\" ]]; then value="${value:1:${#value}-2}"; fi
                [[ "$value" =~ ^[A-Za-z0-9_[:space:]-]+$ ]] || die "malformed operating system data"
                if [[ "$key" == ID ]]; then
                    [[ -z "$id" ]] || die "malformed operating system data"
                    id="$value"
                else
                    [[ -z "$id_like" ]] || die "malformed operating system data"
                    id_like="$value"
                fi
                ;;
        esac
    done < "$os_release"
    [[ "$id" == "debian" || " $id_like " == *" debian "* ]] || die "unsupported OS/package manager"
    command -v apt-get >/dev/null || die "unsupported OS/package manager"
    if [[ "$INSTALL_ASSUME_YES" != 1 && "${ROASTPILOT_INSTALL_ASSUME_YES:-}" != 1 && ! -t 0 ]]; then
        die "stdin is not a TTY; pass --yes or set ROASTPILOT_INSTALL_ASSUME_YES=1"
    fi
    if [[ -z "$REQUESTED_HOSTNAME" && "$(hostnamectl --static)" != "roastpilot" ]]; then
        die "hostname is not roastpilot; re-run with --set-hostname roastpilot"
    fi
}

resolve_operator_identity() {
    local account record_name ignored operator_home
    INVOKING_USER="$(id -un)" || die "cannot determine invoking user"
    INVOKING_GROUP="$(id -gn)" || die "cannot determine invoking group"
    [[ "$INVOKING_USER" =~ ^[a-z_][a-z0-9_-]*$ && "$INVOKING_GROUP" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "unsafe operator identity"
    account="$(getent passwd "$INVOKING_USER")" || die "cannot determine invoking home"
    IFS=: read -r record_name ignored ignored ignored ignored operator_home ignored <<< "$account"
    [[ "$record_name" == "$INVOKING_USER" && "$operator_home" == /* && "$operator_home" != *"/../"* ]] || die "unsafe operator home"
    INVOKING_HOME="$operator_home"
}

installed_pipx_state() {
    local state
    state="$(pipx list --json)" || die "cannot inspect pipx state"
    printf '%s' "$state" | python3 -c '
import json, sys
try:
    venvs = json.load(sys.stdin)["venvs"]
    entry = venvs.get("roastpilot-agent")
    if entry is not None and not isinstance(entry, dict):
        raise ValueError()
    print("absent" if entry is None else json.dumps(entry))
except (ValueError, KeyError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
' || die "invalid pipx state"
}

pipx_matches() {
    local entry="$1" selector_kind="$2" selector_value="$3"
    printf '%s' "$entry" | python3 -c '
import json, sys
kind, expected = sys.argv[1:]
try:
    main = json.load(sys.stdin)["metadata"]["main_package"]
    actual = main["package_version"] if kind == "version" else main["package_or_url"]
    raise SystemExit(0 if actual == expected else 1)
except (KeyError, TypeError, json.JSONDecodeError):
    raise SystemExit(2)
' "$selector_kind" "$selector_value"
}

install_application() {
    local state package_spec match_status
    state="$(installed_pipx_state)"
    if [[ "$state" == "absent" ]]; then
        if [[ -n "$REQUESTED_WHEEL" ]]; then package_spec="$REQUESTED_WHEEL"
        elif [[ -n "$REQUESTED_VERSION" ]]; then package_spec="roastpilot-agent[pi]==$REQUESTED_VERSION"
        else package_spec="roastpilot-agent[pi]"; fi
        pipx install -- "$package_spec"
        return
    fi
    [[ -n "$REQUESTED_WHEEL$REQUESTED_VERSION" ]] || return 0
    if [[ -n "$REQUESTED_WHEEL" ]]; then
        if pipx_matches "$state" wheel "$REQUESTED_WHEEL"; then return; else match_status=$?; fi
    else
        if pipx_matches "$state" version "$REQUESTED_VERSION"; then return; else match_status=$?; fi
    fi
    [[ "$match_status" == 1 ]] || die "invalid pipx package metadata"
    pipx uninstall -- roastpilot-agent
    if [[ -n "$REQUESTED_WHEEL" ]]; then package_spec="$REQUESTED_WHEEL"; else package_spec="roastpilot-agent[pi]==$REQUESTED_VERSION"; fi
    pipx install -- "$package_spec"
}

resolve_appliance_executable() {
    local found resolved
    found="$(command -v roastpilot-agent)" || die "roastpilot-agent was not installed"
    [[ "$found" == /* ]] || die "roastpilot-agent path is unsafe"
    resolved="$(readlink -f -- "$found")" || die "roastpilot-agent path is unsafe"
    [[ "$resolved" == /* && -f "$resolved" && -x "$resolved" && ! -L "$resolved" ]] || die "roastpilot-agent executable is unsafe"
    APPLIANCE_EXECUTABLE="$resolved"
}

install_model_and_render() {
    local model_dir stage_dir stage_parent
    model_dir="$(rooted_path /var/lib/roastpilot-agent/models)"
    validate_destination "$model_dir"
    stage_parent="$(rooted_path /tmp)"
    validate_destination "$stage_parent"
    run_privileged mkdir -p -- "$stage_parent"
    stage_dir="$(run_privileged mktemp -d -- "$stage_parent/roastpilot-install.XXXXXX")"
    [[ "$stage_dir" == "$stage_parent/roastpilot-install."* ]] || die "unsafe staging directory"
    run_privileged chown "$INVOKING_USER:$INVOKING_GROUP" -- "$stage_dir"
    run_privileged chmod 0700 -- "$stage_dir"
    STAGE_DIR="$stage_dir"
    if [[ -n "$MODEL_FROM_DIR" ]]; then
        run_privileged "$APPLIANCE_EXECUTABLE" appliance model install --dest "$model_dir" --from-dir "$MODEL_FROM_DIR"
    else
        run_privileged "$APPLIANCE_EXECUTABLE" appliance model install --dest "$model_dir"
    fi
    "$APPLIANCE_EXECUTABLE" appliance render --output-dir "$stage_dir" --port "$PORT" \
        --operator-user "$INVOKING_USER" --operator-group "$INVOKING_GROUP" --operator-home "$INVOKING_HOME" --serial-port "$SERIAL_PORT" \
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
    done < "$STAGE_DIR/roastpilot-agent.env" | run_privileged tee -- "$env_file" >/dev/null
}

install_rendered_files() {
    local etc_dir var_dir env_file yaml_file unit_file prior_file prior_hostname
    etc_dir="$(rooted_path /etc/roastpilot-agent)"
    var_dir="$(rooted_path /var/lib/roastpilot-agent)"
    env_file="$etc_dir/roastpilot-agent.env"
    yaml_file="$etc_dir/coffee-roaster-mcp.yaml"
    unit_file="$(rooted_path /etc/systemd/system/roastpilot-agent.service)"
    prior_file="$var_dir/prior-static-hostname"
    prepare_destination_parents "$env_file" "$yaml_file" "$unit_file" "$prior_file" "$var_dir/models"
    # The renderer deliberately leaves this blank; inserting it only through
    # stdin keeps the key out of command arguments, logs, and the unit.
    if [[ -n "$API_KEY" ]]; then
        write_env_with_key "$env_file"
    else
        run_privileged install -m 0600 -- "$STAGE_DIR/roastpilot-agent.env" "$env_file"
    fi
    run_privileged chmod 0600 -- "$env_file"
    run_privileged install -m 0644 -- "$STAGE_DIR/coffee-roaster-mcp.appliance.yaml" "$yaml_file"
    run_privileged install -m 0644 -- "$STAGE_DIR/roastpilot-agent.service" "$unit_file"
    if ! id -nG "$INVOKING_USER" | tr ' ' '\n' | grep -Fxq dialout || ! id -nG "$INVOKING_USER" | tr ' ' '\n' | grep -Fxq audio; then
        run_privileged usermod -aG dialout,audio -- "$INVOKING_USER"
    fi
    if [[ -n "$REQUESTED_HOSTNAME" ]]; then
        prior_hostname="$(hostnamectl --static)"
        if [[ "$prior_hostname" != "$REQUESTED_HOSTNAME" ]]; then
            printf '%s\n' "$prior_hostname" | run_privileged tee -- "$prior_file" >/dev/null
            run_privileged chmod 0600 -- "$prior_file"
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
    STAGE_DIR=""
    trap '[[ -z "${STAGE_DIR:-}" ]] || run_privileged rm -rf -- "$STAGE_DIR"' EXIT
    parse_arguments "$@"
    preflight
    resolve_operator_identity
    run_privileged apt-get install -y libportaudio2 pipx avahi-daemon
    install_application
    resolve_appliance_executable
    install_model_and_render
    install_rendered_files
    enable_services
    summary
}

main "$@"
