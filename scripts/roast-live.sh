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
#          ROASTPILOT_ADVISOR__MODEL_SLUG / ROASTPILOT_ADVISOR__PROMPT_VERSION
#            override the advisor model + control-teaching prompt (defaults
#            openai/gpt-4o + c3). The banner prints the resolved pair and tags it
#            ⚠ EXPERIMENT when non-default, e.g. roast 8:
#              ROASTPILOT_ADVISOR__MODEL_SLUG=openai/gpt-4.1-mini \
#              ROASTPILOT_ADVISOR__PROMPT_VERSION=c6 ./scripts/roast-live.sh
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
pkill -9 -f 'roastpilot-agent serve' 2>/dev/null || true
pkill -9 -f 'coffee-roaster-mcp'     2>/dev/null || true   # agent child (also matches uvx coffee-roaster-mcp)
pkill -9 -f 'uvx coffee-roaster-mcp'             2>/dev/null || true   # any stray uvx coffee-roaster (belt-and-braces, matches kill-roast.sh)
sleep 1   # let the OS release the serial port + the :PORT socket

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
# now), so it can never drift from runtime: this covers ADAPTIVE_TRIM=1, a directly
# exported var, AND pydantic's full truthy set (1/true/yes/on/…) identically, using
# the same parser the serving agent used (Augment #402).
TRIM="fixed 65% (proven roast-6 default)"
if python -c "import sys; from roastpilot_agent.config import AppConfig as A; sys.exit(0 if A().controller.pre_first_crack_levers.late_maillard_trim.adaptive_depth_enabled else 1)" 2>/dev/null; then
  TRIM="ADAPTIVE — #386 RoR-keyed depth (experiment, watch the cut)"
fi

# Drift-proof read of the resolved advisor model + prompt, tagged when non-default (e.g. roast 8 = mini+c6); rationale in the PR.
ADVISOR_CFG="$(python -c '
from roastpilot_agent.config import AppConfig

adv = AppConfig().advisor
fields = type(adv).model_fields
default = (
    adv.model_slug == fields["model_slug"].default
    and adv.prompt_version == fields["prompt_version"].default
)
tag = "" if default else "   ⚠ EXPERIMENT — non-default, watch it"
print(f"{adv.model_slug}  ·  prompt {adv.prompt_version}{tag}")
' 2>/dev/null || echo 'unresolved (config read failed — check the agent output above)')"

echo "→ starting agent + spawning MCP child (takes a few seconds — don't Ctrl-C yet)…"
echo "  MCP config: ${COFFEE_ROASTER_MCP_CONFIG}"
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
