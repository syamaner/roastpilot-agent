#!/usr/bin/env bash
#
# roast-live.sh — supervised live roast THROUGH the agent (issue #134).
# REQUIRES the Hottop (USB serial) + USB mic. Supervised, abort-ready.
#
# Points the agent's spawned coffee-roaster-mcp child at your proven MCP config,
# does setup, waits until the server is ACTUALLY serving, prints the dashboard
# URL + the safety banner, then runs. Stay at the machine.
#
# Usage:   OPENROUTER_API_KEY=sk-... ./scripts/roast-live.sh
#          (omit the key to run advisory-paused — controller still runs safety)
# Env:     PORT=8000
#          COFFEE_ROASTER_MCP_CONFIG=~/roasts/coffee-roaster-mcp.yaml  (default)
#          ROASTPILOT_DB=~/roasts/roastpilot.sqlite3  (default) — the agent
#            decision trace; persists across shutdown/restart (issue #161).
#          ADAPTIVE_TRIM=1  enable the #386 RoR-keyed ADAPTIVE late-Maillard trim
#            depth (default off = the proven fixed 65% cut). Interim opt-in until
#            the config UI lands; e.g. ADAPTIVE_TRIM=1 ./scripts/roast-live.sh
#          POST_FC_LOOP=1  the #405/D88 deterministic post-FC taper heat loop +
#            deterministic drop is now the DEFAULT (promoted 12 Jul, D88/D89 —
#            the 11 Jul validation roast passed structurally and the cup scored
#            9/10). This value is now a no-op affirmation of the default; the
#            banner still prints resolved state either way.
#          POST_FC_LOOP=0  BASELINE ARM: disable the taper, back to fully
#            advisor-driven post-FC (the roast-1..8 behaviour) for an A/B or a
#            regression check, e.g. POST_FC_LOOP=0 ./scripts/roast-live.sh
#          CEILING_GUARD=1  the D88 deterministic ceiling-guard drop (bean ≥
#            ceiling_guard_temp_c, default 196 °C → drop through the normal
#            safety path) is now the DEFAULT (promoted 12 Jul alongside
#            POST_FC_LOOP, same validation roast + tasting sign-off).
#            Independent of POST_FC_LOOP by design — a safety anchor, not a
#            taper feature; this value is now a no-op affirmation of the
#            default.
#          CEILING_GUARD=0  BASELINE ARM: disable the guard — the 196 °C
#            boundary reverts to the advisor's own judgment alone, e.g.
#            POST_FC_LOOP=0 CEILING_GUARD=0 ./scripts/roast-live.sh for the
#            full pre-promotion baseline
#          ROASTPILOT_ADVISOR__MODEL_SLUG / …__PROMPT_VERSION override the
#            advisor model + control-teaching prompt (schema defaults
#            openai/gpt-4o + c3). The banner prints the RESOLVED pair — env over
#            the saved ~/.roastpilot/config.yaml over the schema defaults, the
#            same order the agent uses — and tags it ⚠ EXPERIMENT when either
#            differs from the schema default, e.g. roast 8:
#              ROASTPILOT_ADVISOR__MODEL_SLUG=openai/gpt-4.1-mini ./scripts/roast-live.sh
#            ⚠ Setting either PINS that value for every roast in the session:
#            env beats the saved file, so the /config UI selector becomes a
#            silent no-op. Never use them to switch arms in a prompt A/B —
#            switch in /config between roasts and read the banner to confirm.
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PORT="${PORT:-8000}"
: "${COFFEE_ROASTER_MCP_CONFIG:=$HOME/roasts/coffee-roaster-mcp.yaml}"
export COFFEE_ROASTER_MCP_CONFIG
# Persist the live decision trace next to the MCP config + logs (issue #161),
# so the operator finds it and it survives a Ctrl-C / restart for recovery.
: "${ROASTPILOT_DB:=$HOME/roasts/roastpilot.sqlite3}"
export ROASTPILOT_DB

# Opt-in (#386): the RoR-keyed ADAPTIVE late-Maillard trim depth. Default OFF —
# the proven fixed 65% cut (roast-6 behaviour) stays the checked-in default; this
# only flips it when the operator asks. Maps to the agent's AppConfig nested env
# path (controller.pre_first_crack_levers.late_maillard_trim.adaptive_depth_enabled).
# Interim toggle until the agent config UI lands.
if [ "${ADAPTIVE_TRIM:-0}" = "1" ]; then
  export ROASTPILOT_CONTROLLER__PRE_FIRST_CRACK_LEVERS__LATE_MAILLARD_TRIM__ADAPTIVE_DEPTH_ENABLED=true
fi

# The deterministic post-FC RoR-target heat loop + deterministic drop is now
# the DEFAULT (promoted 12 Jul, D88/D89 — the 11 Jul validation roast passed
# structurally and the cup scored 9/10; the config field's own default flipped
# to True, so leaving this unset already runs the taper). POST_FC_LOOP=1 is a
# no-op affirmation of that default; POST_FC_LOOP=0 is the BASELINE ARM —
# explicitly disables the taper for an A/B or a regression check, keeping the
# fully advisor-driven post-FC path (the roast-1..8 behaviour) one launch-line
# away.
if [ "${POST_FC_LOOP:-1}" = "0" ]; then
  export ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__ENABLED=false
elif [ "${POST_FC_LOOP:-1}" = "1" ]; then
  export ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__ENABLED=true
fi

# The D88 deterministic ceiling-guard drop is now the DEFAULT too (promoted
# 12 Jul alongside POST_FC_LOOP, same validation roast + tasting sign-off).
# Decoupled from POST_FC_LOOP by design (a safety anchor, not a taper feature)
# — it fires with the taper OFF too. CEILING_GUARD=1 is a no-op affirmation of
# the default; CEILING_GUARD=0 is the BASELINE ARM — the 196 °C boundary
# reverts to the advisor's own judgment alone. The agent banner prints the
# resolved state either way, so a typo here can never silently run a baseline
# as a treatment arm.
if [ "${CEILING_GUARD:-1}" = "0" ]; then
  export ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__CEILING_GUARD_DROP_ENABLED=false
elif [ "${CEILING_GUARD:-1}" = "1" ]; then
  export ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__CEILING_GUARD_DROP_ENABLED=true
fi

if [ ! -f "$COFFEE_ROASTER_MCP_CONFIG" ]; then
  echo "ERROR: MCP config not found: $COFFEE_ROASTER_MCP_CONFIG" >&2
  echo "       Set COFFEE_ROASTER_MCP_CONFIG to your coffee-roaster-mcp.yaml" >&2
  echo "       (template: docs/examples/coffee-roaster-mcp.known-good.yaml)." >&2
  exit 1
fi

# --- Pre-start cleanup (operator tooling, interim until #212 lands) ----------
# A wedged previous run (graceful shutdown can hang — #212, SIGTERM ignored) or
# a stray manually-launched coffee-roaster-mcp leaves the USB serial port / mic /
# :PORT held, which destabilises the new run (the duplicate-MCP contention +
# mcp_read_failures seen 14 Jun). Force-kill any lingering agent + MCP first.
# pkill returns non-zero when nothing matches, so guard each with `|| true`
# (set -e would otherwise abort). NOTE: a force-kill does NOT run the heat-off
# path — if a prior run left the Hottop commanded hot, power it off AT THE
# MACHINE; this only frees host resources so the new run can start clean.
echo "→ clearing any wedged/leftover roast processes (local-only safeguard)…"
# Match any ENTRYPOINT invocation of the agent, not just the 'serve' form: a
# replay/e2e harness runs as `roastpilot-agent --replay … --host 127.0.0.1
# --port 8000` (see web/playwright.config.ts webServer) — no 'serve' token, so
# a subcommand pattern missed it. On macOS a later specific 127.0.0.1:8000
# bind coexists with our 0.0.0.0:8000 (SO_REUSEADDR) and then receives ALL
# browser loopback traffic, so a survivor here answers /api/health with
# active_run_id=null while the real roast runs — the 12 Jul "start form after
# a live 201" incident class. The pattern anchors on `bin/roastpilot-agent`
# (the console-script path every real agent process carries in its cmdline) —
# a bare 'roastpilot-agent' pattern would also SIGKILL THIS LAUNCHER when it
# is invoked by absolute path (…/roastpilot-agent/scripts/roast-live.sh), or
# an editor open on a repo path (Codex P2, PR #518).
pkill -9 -f 'bin/roastpilot-agent'   2>/dev/null || true
pkill -9 -f 'coffee-roaster-mcp'     2>/dev/null || true   # agent child (also matches uvx coffee-roaster-mcp)
pkill -9 -f 'uvx coffee-roaster-mcp'             2>/dev/null || true   # any stray uvx coffee-roaster (belt-and-braces, matches kill-roast.sh)
sleep 1   # let the OS release the serial port + the :PORT socket

# HARD GUARD: refuse to start while ANYTHING still listens on :$PORT (either
# address family / any bind address). Coexisting listeners don't fail the bind
# on macOS — they silently split the traffic — so absence must be verified,
# not assumed. Fail CLOSED if lsof itself is missing: a safety check that
# can't run must abort, not silently pass (Codex P2, PR #518). Runs twice —
# here, and again immediately before launch (the venv/SPA build below leaves
# a window for a harness to bind in between; Codex P2, PR #518).
assert_port_free() {
  if ! command -v lsof >/dev/null 2>&1; then
    echo "✗ REFUSING TO START — lsof not found; cannot verify :$PORT is free." >&2
    echo "  Install lsof (ships with macOS) or check the port by hand." >&2
    exit 1
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "✗ REFUSING TO START — something is listening on :$PORT:" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
    echo "  Kill it (kill -9 <PID>) and re-run. A second listener on :$PORT" >&2
    echo "  hijacks the browser's health checks mid-roast (12 Jul incident)." >&2
    exit 1
  fi
}
assert_port_free

echo "→ preparing (venv, deps, SPA build)…"
if [ ! -x .venv/bin/python ]; then
  python3.11 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q -e . --group dev
# ALWAYS rebuild the SPA on start. The earlier mtime-staleness heuristic missed
# the real roast-2 failure mode: the server was already running on an old bundle
# when fresh FE was pulled, so it kept serving stale assets. A clean build is
# ~1 s — simpler and bulletproof than guessing freshness. `npm install` stays
# conditional on node_modules being absent (the slow step).
echo "  building SPA…"
(
  cd web || exit 1                       # never build from the repo root if cd fails
  [ -d node_modules ] || npm install     # slow step, only when missing
  npm run build
)

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
[ -n "$IP" ] || IP="$(ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127' | head -1 || true)"
[ -n "$IP" ] || IP="127.0.0.1"

ADV="advisor configured"
[ -n "${OPENROUTER_API_KEY:-}" ] || ADV="ADVISORY-PAUSED (no OPENROUTER_API_KEY)"

# Derive the banner from the agent's OWN resolved config (the .venv is active by
# now), so it can never drift from runtime. The seam is
# roastpilot_agent.launch_banner, which resolves through the SAME
# config_store.load_app_config the serving agent uses — env over the operator's
# saved ~/.roastpilot/config.yaml over the schema defaults (#746). A bare
# AppConfig() reads the environment ONLY (BaseSettings, no YAML source), so it
# used to print the schema-default prompt while the agent genuinely ran the
# version saved from the /config UI — the wrong arm shown at the one moment a
# roast cannot be re-run. This also covers ADAPTIVE_TRIM=1, a directly exported
# var, AND pydantic's full truthy set (1/true/yes/on/…) identically, because it
# is the agent's own parser (Augment #402).
#
# Line 1 = the "Advisor cfg:" text, line 2 = the "Pre-FC trim:" text. A
# malformed/unreadable saved config fails LOUD (reason on stderr, non-zero
# exit) and both lines read "unresolved" — never a plausible-but-wrong version.
BANNER_UNRESOLVED='unresolved (config read failed — see the error above)'
BANNER_LINES="$(python -m roastpilot_agent.launch_banner)" || BANNER_LINES=""
ADVISOR_CFG="$(printf '%s\n' "$BANNER_LINES" | sed -n '1p')"
TRIM="$(printf '%s\n' "$BANNER_LINES" | sed -n '2p')"
[ -n "$ADVISOR_CFG" ] || ADVISOR_CFG="$BANNER_UNRESOLVED"
[ -n "$TRIM" ] || TRIM="$BANNER_UNRESOLVED"

echo "→ starting agent + spawning MCP child (takes a few seconds — don't Ctrl-C yet)…"
echo "  MCP config: ${COFFEE_ROASTER_MCP_CONFIG}"
# Re-check right before launch: the venv install + SPA build above take long
# enough for a replay/e2e harness to bind :$PORT after the first check.
assert_port_free
roastpilot-agent serve --host 0.0.0.0 --port "$PORT" &
SRV=$!
trap 'kill "$SRV" 2>/dev/null || true' INT TERM

# Wait until serving, OR exits during startup (e.g. MCP child fails closed).
for _ in $(seq 1 120); do
  if ! kill -0 "$SRV" 2>/dev/null; then
    echo "✗ agent exited during startup — likely the MCP child / Hottop could not start (fail-closed). See output above." >&2
    wait "$SRV" || true
    exit 1
  fi
  if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/api/health"; then
    cat <<BANNER

════════════════════════════════════════════════════════════════════
  ✅ READY — SUPERVISED LIVE ROAST (#134). Stay at the machine, abort-ready.

  Advisor    : ${ADV}
  Advisor cfg: ${ADVISOR_CFG}
  Pre-FC trim: ${TRIM}
  Dashboard  : http://${IP}:${PORT}/
  Trace DB   : ${ROASTPILOT_DB}  (persists after shutdown — #161)

  ✓  Ctrl-C now SAFELY STOPS: graceful shutdown commands heat → 0 through
     the safety path before the MCP child stops (#142). Prefer the in-UI
     EMERGENCY STOP for an immediate stop while the server is up; cut machine
     power only if a HARD kill (kill -9 / power loss) left it commanded hot —
     that is uncatchable, so a restart re-enters operator_recovery_required
     and never auto-resumes heat/fan.

  ⚠  Run the §1 COLD pre-flight BEFORE charging beans:
       • e-stop drives heat → 0
       • an over-limit command is CLAMPed/REJECTed in the trace
       • kill the agent mid-(dry)-run → restart enters recovery, commands nothing
════════════════════════════════════════════════════════════════════

BANNER
    wait "$SRV"
    exit $?
  fi
  sleep 0.5
done

echo "✗ agent did not become ready within ~60s." >&2
kill "$SRV" 2>/dev/null || true
exit 1
