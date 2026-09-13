# Raspberry Pi appliance deployment

This guide describes the native installer delivered for E11-S2. It is an
operator procedure for a trusted home LAN; it is not evidence from a Raspberry
Pi, Hottop, microphone, serial device, or live roast.

## Prerequisites

Use a Raspberry Pi 5 with the official 27 W USB-C power supply and an active
cooler. First-crack inference shares the Pi's CPU with the agent, MCP child,
USB serial link, and microphone, so passive cooling is not an equivalent
setup. The installer supports an aarch64 Debian-family system with `apt`; run
it as the intended non-root operator, not as root. `/usr/bin/sudo` must be
installed, and that operator must be authorised to use it: the installer's
privileged operations use that exact path.

Have the following operator-specific values before starting:

- A Hottop serial path such as `/dev/serial/by-id/...`; the installer accepts
  only an absolute `/dev` path.
- The microphone device substring used by `coffee-roaster-mcp`.
- An OpenRouter API key if advisory suggestions are wanted. The controller and
  safety policy remain in charge; no key means no advisor suggestions.
- A port from 1024 through 65535 (the default is `8000`).

Before maintenance, make the appliance safely inactive. End a roast safely,
wait until the service is idle, and do not start, restart, upgrade, or rerun
the installer during a roast. Run one installer invocation at a time.

## Install

The installer installs the `[pi]` package through `pipx`, installs the system
packages it needs, writes the managed configuration and unit, verifies the
pinned first-crack model, and enables Avahi and `roastpilot-agent`. It does not
start `roastpilot-agent` unless `--start` is supplied.

For a one-line install, review the arguments carefully and run this as the
non-root operator:

```sh
curl -fsSL https://raw.githubusercontent.com/syamaner/roastpilot-agent/main/packaging/pi/install.sh | bash -s -- --serial-port /dev/serial/by-id/REPLACE_ME --audio-device 'REPLACE_ME' --set-hostname roastpilot --start --yes
```

`--set-hostname roastpilot` is explicit consent to change the static hostname.
Without it, the installer requires the existing hostname already to be
`roastpilot`. Do not substitute another hostname: the installer accepts only
that value. It may request privilege through the system's normal mechanism.

For a safer download, inspect, then run path, keep the script as a file:

```sh
curl -fsSLo install-roastpilot-pi.sh https://raw.githubusercontent.com/syamaner/roastpilot-agent/main/packaging/pi/install.sh
less install-roastpilot-pi.sh
bash install-roastpilot-pi.sh --serial-port /dev/serial/by-id/REPLACE_ME --audio-device 'REPLACE_ME' --set-hostname roastpilot --start
```

The `--start` option is for an already-safe, inactive appliance. Otherwise the
unit is enabled and starts on the next boot; an operator can explicitly start
it only while there is no active roast. A service restart follows the existing
operator recovery flow and does not resume heat or fan.

## Non-interactive options

Use `--yes`, or set `ROASTPILOT_INSTALL_ASSUME_YES=1`, only when the operator
has already checked the target and arguments. `--serial-port` and
`--audio-device` are required; their environment equivalents are
`ROASTPILOT_INSTALL_SERIAL_PORT` and `ROASTPILOT_INSTALL_AUDIO_DEVICE`.

`ROASTPILOT_INSTALL_API_KEY` supplies the OpenRouter key only to this installer
process; `--api-key` is deliberately unsupported. `--port PORT` chooses the
HTTP port. For a selected package, choose exactly one of `--version VERSION`
or `--wheel /absolute/path/to/roastpilot_agent.whl`; the wheel also has the
`ROASTPILOT_INSTALL_WHEEL` environment alternative. `--allow-unsupported-arch`
is an explicit override, not a Pi substitute.

An air-gapped install uses `--from-dir DIR`. `DIR` must be an existing,
canonical absolute directory, not `/` or a symlink. It is a source tree for
the model files, not a package-wheel directory.

## Configuration

The installer writes these managed paths:

| Purpose | Path |
| --- | --- |
| Protected service environment | `/etc/roastpilot-agent/roastpilot-agent.env` |
| MCP configuration | `/etc/roastpilot-agent/coffee-roaster-mcp.yaml` |
| Systemd unit | `/etc/systemd/system/roastpilot-agent.service` |

The protected environment contains `OPENROUTER_API_KEY`, `PORT`,
`ROASTPILOT_DB`, and `COFFEE_ROASTER_MCP_CONFIG`. Set the key in that protected
file rather than putting it on a command line. Editing `PORT` requires an
explicit, safely timed service restart. The rendered MCP configuration holds
the selected serial device and audio device; rerun the installer with the new
`--serial-port` or `--audio-device` only while the service is inactive.

The service runs `roastpilot-agent serve --host 0.0.0.0 --port ${PORT}` and
the agent spawns its MCP child over stdio. It installs one service unit, not a
separate MCP daemon.

## Data location

Persistent state is under `/var/lib/roastpilot-agent/`: the SQLite decision
trace is `/var/lib/roastpilot-agent/roastpilot.sqlite3`, and the local model
files are below `/var/lib/roastpilot-agent/models`. The generated MCP YAML
uses the MCP default relative `logs` export directory; because the service unit
sets `WorkingDirectory=~`, MCP exports are in the operator account's `~/logs`.
Treat all three locations as appliance data when planning storage, backup,
replacement, or removal.

## Model provenance and air-gapped preparation

The installer uses the appliance manifest's immutable model revision and
checks each model file's SHA-256 before promoting it into the managed model
directory. A connected, trusted staging machine can prepare a source tree with
the installed command:

```sh
roastpilot-agent appliance model install --dest /absolute/model-source
```

Transfer that complete directory by a trusted method, preserve its layout, and
on the Pi pass its canonical path with `--from-dir /absolute/model-source`.
The Pi then verifies the same manifest and digests locally; it need not fetch
model bytes during that installation.

## Upgrade and maintenance

`pipx upgrade roastpilot-agent` is the generic pipx command, but it does not
repeat this appliance installer's configuration rendering, model verification,
or service safeguards. After safely ending any roast and confirming the
appliance is inactive, stop the service before a managed upgrade:

```sh
sudo systemctl stop roastpilot-agent
```

Then rerun the installer, using `--version` or `--wheel` as appropriate. Never
stop the service during a roast. The installer stages a replacement and retains
configuration/application rollback handling if its replacement path fails;
follow any manual reconciliation message rather than assuming every external
system change was reversed. The unit remains enabled: use `--start` only for
an already-safe inactive appliance; otherwise it starts at the next boot, or
an operator may explicitly start it only while no roast is active.

Never treat a successful package operation as a reason to operate a roaster.
Maintenance does not authorise a live session, serial connection, microphone
use, or command to the machine.

## Logs

Inspect the service journal with:

```sh
journalctl -u roastpilot-agent -f
```

Use logs for diagnosis. They are not a substitute for operator observation or
for a separately authorised live-roast procedure.

## mDNS access

Avahi advertises the enabled service on the local network. After an explicitly
started service or a boot, browse to `http://roastpilot.local:8000`, replacing
the port if `PORT` was changed. This is intended for a device on the same home
LAN; it does not require a local display.

## Trust boundary

Keep this appliance on a trusted home LAN. Do not expose its port with public
port forwarding, reverse proxies, or Internet-facing tunnels. App-wide
authentication is deferred to #595, so this deployment does not provide that
boundary itself.

## Uninstall and rollback

For removal, first end any roast safely and confirm the service is inactive.
Disable and stop the unit, remove the pipx application, then decide separately
whether the managed configuration and persistent data should be retained for
diagnosis or removed. Avahi may be shared with other services and is therefore
not removed by this procedure.

```sh
sudo systemctl disable --now roastpilot-agent
pipx uninstall roastpilot-agent
```

An install that changes the hostname records the prior static hostname at
`/var/lib/roastpilot-agent/prior-static-hostname`. Changing it back is a
separate operator action. If the installer reports an incomplete rollback,
preserve its message and reconcile the stated paths manually; do not retry it
while a roast could be active.

## Outstanding evidence boundaries

D191/D192 characterisation, independent Pi evidence review,
complete-appliance validation, and separately authorised supervised live-roast
acceptance remain outstanding. This guide does not change those boundaries.
