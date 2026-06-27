---
name: pre-roast-preflight
description: Hardware + software readiness checks before charging beans — Hottop serial present, FC mic present, real driver (not mock), advisor reachable, the intended bean profile loaded, plus the cold-machine safety checks. Use before a supervised roast, after connecting hardware.
---

Codify the readiness checks that caught a powered-off Hottop before roast 4.
Produce a **go / no-go** with each check ✅/⚠️ and the single fix for any ⚠️.

## 1. Pre-launch (read-only, before `roast-live.sh`)

- **Hottop serial present** — must match the MCP config `port`:
  !`ls /dev/cu.usbserial-* 2>/dev/null || echo "⚠️  NO serial device — power on the Hottop / re-seat USB"`
- **MCP config sane** — real driver (NOT `mock`), FC mode `audio`, known-good model revision, the expected mic substring:
  !`grep -nE "driver:|port:|mode:|input_device:|revision:" "${COFFEE_ROASTER_MCP_CONFIG:-$HOME/roasts/coffee-roaster-mcp.yaml}" 2>/dev/null || echo "⚠️  MCP config not found"`
- **FC microphone present** — the configured `input_device` enumerates:
  !`system_profiler SPAudioDataType 2>/dev/null | grep -iE "USB|PnP|input" | head || echo "check the mic"`
- **Branch/seed** — the intended bean profile + targets are on the checked-out branch (`git branch --show-current`; `grep target_ src/roastpilot_agent/seed.py`).

If the serial device is absent: power on the Hottop, re-seat the USB *data* cable, re-check. **Do not launch until it appears.**

## 2. Once `roast-live.sh` is up

- **Health** — `GET /api/health`: `mcp_child: running`, **advisor `reachable`** (NOT "unreachable" — the expired-key trap that killed attempt 1), real driver:
  !`curl -sf -m5 http://127.0.0.1:8000/api/health | python3 -m json.tool 2>/dev/null || echo "server not up yet"`
- **Intended profile loaded** with the expected drop/dev targets:
  !`curl -sf -m5 http://127.0.0.1:8000/api/bean-profiles 2>/dev/null | python3 -c "import json,sys; [print(p['name'],'drop',p['target_drop_temp_c'],'dev',p['target_development_percent']) for p in json.load(sys.stdin).get('profiles',[])]" 2>/dev/null || echo "server not up yet"`
- Read the **startup banner**: real Hottop (not mock), FC mode audio + model loaded, advisor REACHABLE, the trace DB path.

## 3. Cold-machine safety checks — BEFORE any beans

Surface these for the operator to perform (dry, cold machine). They are the gate:
- **Emergency stop** → heat goes to 0, a fault is recorded, the dashboard shows e-stop.
- **Over-limit** → a heat above the limit is **CLAMPed / REJECTed** in the trace (architecturally enforced; observe a CLAMP/REJECT naturally if no inject path).
- **Restart → recovery** → kill the agent mid-(dry)-run → on restart it enters `operator_recovery_required` and commands nothing until the operator acts.

If any fails: **stop and fix — do not roast.**

## 4. Verdict

Report a clear **GO** (all ✅) or **NO-GO** with the one blocking ⚠️ and its fix.
Remind: fire extinguisher in reach, ventilation, you stay at the machine, hand near the kill.
