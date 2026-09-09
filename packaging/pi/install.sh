#!/usr/bin/env bash
# Native Pi appliance installer (issue #138, E11-S2 slice 3).
#
# Keep every effect in functions and retain the one call at EOF: a partial
# `curl | bash` download defines functions but cannot make a privileged change.
set -euo pipefail

die() {
    printf '%s\n' "install failed: $*" >&2
    exit 1
}

run_privileged() {
    # This is deliberately the sole privilege seam.  Test mode removes
    # privilege; it never redirects a production privileged command.
    if [[ "${ROASTPILOT_INSTALL_TEST_MODE:-}" == "1" ]]; then
        "$@"
    else
        /usr/bin/sudo -- "$@"
    fi
}

rooted_path() {
    local install_root="${ROASTPILOT_INSTALL_TEST_ROOT:-/}"
    local absolute_path="$1"
    [[ "$absolute_path" == /* ]] || die "internal destination is not absolute"
    if [[ "$install_root" == "/" ]]; then
        printf '%s\n' "$absolute_path"
    else
        printf '%s%s\n' "${install_root%/}" "$absolute_path"
    fi
}

validate_install_root() {
    local install_root="${ROASTPILOT_INSTALL_TEST_ROOT:-/}"
    if [[ "${ROASTPILOT_INSTALL_TEST_MODE:-}" != "1" ]]; then
        [[ -z "${ROASTPILOT_INSTALL_TEST_ROOT:-}" && -z "${ROASTPILOT_INSTALL_ROOT:-}" ]] || die "test destination is unavailable in production"
        PATH=/usr/sbin:/usr/bin:/sbin:/bin
        export PATH
        return
    fi
    [[ "$install_root" == /* && "/${install_root#/}/" != *"/../"* ]] || die "invalid install root"
    [[ "$install_root" != *$'\n'* && "$install_root" != *$'\r'* ]] || die "invalid install root"
}

validate_destination() {
    local destination="$1" component current="/" components=()
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

recheck_sensitive_destination() {
    local destination="$1" parent
    parent="$(dirname -- "$destination")"
    # This repeats the unprivileged lexical check at the privilege boundary.
    # It is not an atomic no-follow guarantee, but catches a changed parent or
    # final symlink immediately before each sensitive root write.
    run_privileged test -d "$parent"
    run_privileged test ! -L "$parent"
    run_privileged test ! -L "$destination"
}

validate_no_control_characters() {
    local value="$1" description="$2"
    [[ "$value" != *[[:cntrl:]]* ]] || die "$description contains control characters"
}

validate_from_dir() {
    local candidate="$1" canonical
    validate_no_control_characters "$candidate" "--from-dir"
    [[ "$candidate" == /* ]] || die "--from-dir must be an absolute directory"
    [[ -d "$candidate" && ! -L "$candidate" ]] || die "--from-dir must be an existing directory"
    canonical="$(readlink -f -- "$candidate")" || die "--from-dir is unsafe"
    [[ "$candidate" == "$canonical" && "$canonical" != "/" ]] || die "--from-dir must be canonical"
    MODEL_FROM_DIR="$canonical"
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
  ROASTPILOT_INSTALL_API_KEY    optional key read only from the environment
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
    API_KEY="${ROASTPILOT_INSTALL_API_KEY:-}"
    unset ROASTPILOT_INSTALL_API_KEY
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
            --api-key) die "--api-key is not supported; use ROASTPILOT_INSTALL_API_KEY" ;;
            --serial-port) SERIAL_PORT="${2:?--serial-port needs a path}"; shift ;;
            --audio-device) AUDIO_DEVICE="${2:?--audio-device needs a value}"; shift ;;
            --set-hostname) REQUESTED_HOSTNAME="${2:?--set-hostname needs a value}"; shift ;;
            --help) usage; exit 0 ;;
            *) die "unknown option: $1" ;;
        esac
        shift
    done
    [[ -n "$SERIAL_PORT" ]] || die "--serial-port is required"
    [[ -n "$AUDIO_DEVICE" ]] || die "--audio-device is required"
    validate_no_control_characters "$API_KEY" "API key"
    [[ "$API_KEY" != *[[:space:]\\\"]* ]] || die "API key contains unsafe EnvironmentFile characters"
    validate_no_control_characters "$SERIAL_PORT" "serial port"
    validate_no_control_characters "$AUDIO_DEVICE" "audio device"
    [[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1024 && "$PORT" -le 65535 ]] || die "port must be a decimal number from 1024 to 65535"
    [[ "$SERIAL_PORT" == /dev/* && "$SERIAL_PORT" != *[[:space:]]* ]] || die "serial port must be an absolute /dev path"
    [[ "$AUDIO_DEVICE" == "${AUDIO_DEVICE#"${AUDIO_DEVICE##[![:space:]]}"}" && "$AUDIO_DEVICE" == "${AUDIO_DEVICE%"${AUDIO_DEVICE##*[![:space:]]}"}" && "$AUDIO_DEVICE" != *'@@'* ]] || die "audio device is unsafe"
    [[ -z "$REQUESTED_VERSION" || -z "$REQUESTED_WHEEL" ]] || die "choose --version or --wheel"
    [[ -z "$REQUESTED_VERSION" || "$REQUESTED_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._+!-]*$ ]] || die "invalid version selector"
    if [[ -n "$REQUESTED_WHEEL" ]]; then
        [[ "$REQUESTED_WHEEL" == /* && "$REQUESTED_WHEEL" != *"/../"* ]] || die "invalid wheel selector"
        [[ -f "$REQUESTED_WHEEL" && ! -L "$REQUESTED_WHEEL" ]] || die "wheel must be a regular file"
        REQUESTED_WHEEL="$(readlink -f -- "$REQUESTED_WHEEL")"
    fi
    if [[ -n "$MODEL_FROM_DIR" ]]; then validate_from_dir "$MODEL_FROM_DIR"; fi
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
    local account record_name operator_home
    INVOKING_USER="$(id -un)" || die "cannot determine invoking user"
    INVOKING_GROUP="$(id -gn)" || die "cannot determine invoking group"
    [[ "$INVOKING_USER" =~ ^[a-z_][a-z0-9_-]*$ && "$INVOKING_GROUP" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "unsafe operator identity"
    account="$(getent passwd "$INVOKING_USER")" || die "cannot determine invoking home"
    IFS=: read -r record_name _ _ _ _ operator_home _ <<< "$account"
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
    if "roastpilot-agent" not in venvs:
        print("absent")
        raise SystemExit(0)
    entry = venvs["roastpilot-agent"]
    if not isinstance(entry, dict):
        raise ValueError()
    print(json.dumps(entry))
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
    local expected resolved expected_venv
    # pipx's supported default installation is rooted in the invoking account,
    # never in a caller-controlled PATH.  Resolve its entry point before the
    # privileged model command and require the matching pipx venv provenance.
    expected="$INVOKING_HOME/.local/bin/roastpilot-agent"
    expected_venv="$INVOKING_HOME/.local/pipx/venvs/roastpilot-agent/bin/"
    [[ -L "$expected" ]] || die "roastpilot-agent pipx entry point is missing"
    resolved="$(readlink -f -- "$expected")" || die "roastpilot-agent path is unsafe"
    [[ "$resolved" == "$expected_venv"* && -f "$resolved" && -x "$resolved" && ! -L "$resolved" ]] || die "roastpilot-agent executable is unsafe"
    APPLIANCE_EXECUTABLE="$resolved"
}

install_model_and_render() {
    local model_dir stage_dir stage_parent model_stage
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
    model_stage="$stage_dir/models"
    mkdir -p -- "$model_stage"
    if [[ -n "$MODEL_FROM_DIR" ]]; then
        "$APPLIANCE_EXECUTABLE" appliance model install --dest "$model_stage" --from-dir "$MODEL_FROM_DIR"
    else
        "$APPLIANCE_EXECUTABLE" appliance model install --dest "$model_stage"
    fi
    promote_model_file "$model_stage/onnx/int8/model_quantized.onnx" "$model_dir/onnx/int8/model_quantized.onnx" "022092cddd4c2cd740670c0a85786460699bc1b4f03e20f508182768d21545df"
    promote_model_file "$model_stage/onnx/int8/preprocessor_config.json" "$model_dir/onnx/int8/preprocessor_config.json" "8d04ba5a9c6fca5d39d0de2b1fd05ecf79deb589fbba279728bbebac39934231"
    "$APPLIANCE_EXECUTABLE" appliance render --output-dir "$stage_dir" --port "$PORT" \
        --operator-user "$INVOKING_USER" --operator-group "$INVOKING_GROUP" --operator-home "$INVOKING_HOME" --serial-port "$SERIAL_PORT" \
        --audio-device "$AUDIO_DEVICE"
}

promote_model_file() {
    local source="$1" destination="$2" expected="$3" parent temporary actual
    [[ -f "$source" && ! -L "$source" ]] || die "model staging file is unsafe"
    actual="$(sha256sum -- "$source")"; [[ "${actual%% *}" == "$expected" ]] || die "model staging digest mismatch"
    parent="$(dirname -- "$destination")"
    validate_destination "$parent"
    run_privileged mkdir -p -- "$parent"
    recheck_sensitive_destination "$destination"
    temporary="$(run_privileged mktemp -- "$parent/.roastpilot-model.XXXXXX")"
    [[ "$temporary" == "$parent/.roastpilot-model."* ]] || die "unsafe model temporary path"
    # Root reads only its own temporary file; the unprivileged reader streams
    # already-verified bytes through stdin.
    cat -- "$source" | run_privileged tee -- "$temporary" >/dev/null
    actual="$(run_privileged sha256sum -- "$temporary")"; [[ "${actual%% *}" == "$expected" ]] || die "model promotion digest mismatch"
    run_privileged chmod 0644 -- "$temporary"
    run_privileged mv -f -- "$temporary" "$destination"
    actual="$(run_privileged sha256sum -- "$destination")"; [[ "${actual%% *}" == "$expected" ]] || die "model destination digest mismatch"
}

validate_rendered_env_template() {
    local line assignment_count=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" == OPENROUTER_API_KEY=* ]] && ((assignment_count += 1))
    done < "$STAGE_DIR/roastpilot-agent.env"
    [[ "$assignment_count" == 1 ]] || die "rendered env must contain exactly one OPENROUTER_API_KEY assignment"
}

write_env_with_key() {
    local env_file="$1" line staged
    staged="$STAGE_DIR/roastpilot-agent.env.final"
    (umask 077; : > "$staged")
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == OPENROUTER_API_KEY=* ]]; then
            printf 'OPENROUTER_API_KEY=%s\n' "$API_KEY"
        else
            printf '%s\n' "$line"
        fi
    done < "$STAGE_DIR/roastpilot-agent.env" > "$staged"
    install_env_atomically "$staged" "$env_file"
}

install_env_atomically() {
    local source="$1" destination="$2" parent temporary
    parent="$(dirname -- "$destination")"
    recheck_sensitive_destination "$destination"
    temporary="$(run_privileged mktemp -- "$parent/.roastpilot-env.XXXXXX")"
    [[ "$temporary" == "$parent/.roastpilot-env."* ]] || die "unsafe environment temporary path"
    cat -- "$source" | run_privileged tee -- "$temporary" >/dev/null
    run_privileged chmod 0600 -- "$temporary"
    run_privileged chown "$INVOKING_USER:$INVOKING_GROUP" -- "$temporary"
    run_privileged mv -f -- "$temporary" "$destination"
}

validate_rendered_unit() {
    local unit="$STAGE_DIR/roastpilot-agent.service" expected actual
    [[ -f "$unit" && ! -L "$unit" ]] || die "rendered unit is unsafe"
    expected=$'[Unit]\nDescription=RoastPilot agent (native Pi appliance)\nAfter=network-online.target sound.target\nWants=network-online.target\n[Service]\nType=simple\nUser='"$INVOKING_USER"$'\nGroup='"$INVOKING_GROUP"$'\nEnvironmentFile=/etc/roastpilot-agent/roastpilot-agent.env\nExecStart='"$INVOKING_HOME"$'/.local/bin/roastpilot-agent serve --host 0.0.0.0 --port ${PORT}\nWorkingDirectory=~\nRestart=on-failure\nRestartSec=5\nKillMode=mixed\nTimeoutStopSec=30\nNoNewPrivileges=true\nPrivateTmp=true\n[Install]\nWantedBy=multi-user.target'
    actual="$(sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' "$unit")"
    [[ "$actual" == "$expected" ]] || die "rendered unit violates appliance contract"
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
    # The service runs as the invoking operator and owns its database parent
    # and protected environment file.  Other configuration and the unit stay root-owned.
    run_privileged chown "$INVOKING_USER:$INVOKING_GROUP" -- "$var_dir"
    run_privileged chmod 0700 -- "$var_dir"
    # The renderer deliberately leaves this blank; inserting it only through
    # stdin keeps the key out of command arguments, logs, and the unit.
    validate_rendered_env_template
    validate_rendered_unit
    if [[ -n "$API_KEY" ]]; then
        write_env_with_key "$env_file"
    else
        install_env_atomically "$STAGE_DIR/roastpilot-agent.env" "$env_file"
    fi
    run_privileged chmod 0600 -- "$env_file"
    run_privileged chown "$INVOKING_USER:$INVOKING_GROUP" -- "$env_file"
    recheck_sensitive_destination "$yaml_file"
    run_privileged install -m 0644 -- "$STAGE_DIR/coffee-roaster-mcp.appliance.yaml" "$yaml_file"
    recheck_sensitive_destination "$unit_file"
    run_privileged install -m 0644 -- "$STAGE_DIR/roastpilot-agent.service" "$unit_file"
    if ! id -nG "$INVOKING_USER" | tr ' ' '\n' | grep -Fxq dialout || ! id -nG "$INVOKING_USER" | tr ' ' '\n' | grep -Fxq audio; then
        run_privileged usermod -aG dialout,audio -- "$INVOKING_USER"
    fi
    if [[ -n "$REQUESTED_HOSTNAME" ]]; then
        prior_hostname="$(hostnamectl --static)"
        if [[ "$prior_hostname" != "$REQUESTED_HOSTNAME" ]]; then
            recheck_sensitive_destination "$prior_file"
            run_privileged install -m 0600 -- /dev/null "$prior_file"
            printf '%s\n' "$prior_hostname" | run_privileged tee -- "$prior_file" >/dev/null
            run_privileged chown "$INVOKING_USER:$INVOKING_GROUP" -- "$prior_file"
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
    if [[ "$START_SERVICE" == 1 ]]; then
        printf '%s\n' "Open http://roastpilot.local:$PORT and inspect logs with: journalctl -u roastpilot-agent -f"
    else
        printf '%s\n' "The service was not started; after boot or an explicit start, open http://roastpilot.local:$PORT."
    fi
    if [[ -z "$API_KEY" ]]; then printf '%s\n' "Edit the protected env file to add the OpenRouter key before using advice."; fi
}

main() {
    STAGE_DIR=""
    trap '[[ -z "${STAGE_DIR:-}" ]] || run_privileged rm -rf -- "$STAGE_DIR"' EXIT
    # Test mode is deliberately unprivileged.  Every production lookup starts
    # from this closed path before parsing caller-controlled arguments.
    if [[ "${ROASTPILOT_INSTALL_TEST_MODE:-}" != "1" ]]; then
        PATH=/usr/sbin:/usr/bin:/sbin:/bin
        export PATH
    fi
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
