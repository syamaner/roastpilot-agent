# Roast Session Runbook — 2-roast D88 A/B (baseline vs post-FC taper + ceiling guard)

> **Updated 11 Jul for the D88 validation session (issue #495).** The treatment
> arm is no longer the D83 fixed-band RoR loop (falsified 9 Jul, D87) — it is
> the D88 **measured-anchor taper** plus the **decoupled 196 °C ceiling-guard
> drop**, each behind its own flag with its own banner line.

**Bean:** Guatemala El Durazno (White Honey) — **2×250 g, same bean both roasts** (a clean A/B needs the same bean; this Hottop runs best at a 250 g load). Select "Guatemala El Durazno (White Honey)" in Start-Roast. Targets: **drop 195 °C, dev 13 %** (first-roast de-risk).

## Before the session (once, at the roaster)
- Plug in + power on: Hottop (USB serial), FC mic, **Yocto-Meteo ambient probe**. Warm the Hottop.
- Ensure `~/roasts/coffee-roaster-mcp.yaml` has `ambient: { mode: yoctopuce }` (ambient on for both roasts).
- 2×250 g Guatemala El Durazno + scale.

## Roast 1 — #405 OFF (baseline)
1. `./scripts/roast-live.sh`  — plain; the post-FC RoR loop is OFF by default.
2. **Check the banner:** real Hottop (not mock), advisor **REACHABLE** (gpt-4o), **post-FC RoR loop: disabled**, ambient reading present.
3. Run the **pre-roast-preflight** → must be **GO** (serial, mic, advisor reachable, and the cold-machine safety checks: emergency-stop → heat 0, over-limit → CLAMP/REJECT, restart → operator_recovery_required).
4. Charge → auto **T0** → watch **FC** → **drop** (~195 °C, ceiling 196) → cool. Confirm ambient shows at charge.
5. End the run.

## Between roasts
- **Cool the drum** (physical gate). Stop `roast-live.sh` (Ctrl-C).

## Roast 2 — D88 ON (validation)
1. `POST_FC_LOOP=1 CEILING_GUARD=1 ./scripts/roast-live.sh`
2. **CONFIRM the banner shows BOTH loud lines** — `⚠️ POST-FC RoR LOOP: ENABLED`
   AND `⚠️ CEILING-GUARD DROP: ENABLED (… ≥ 196 °C)`. The flags are independent;
   each must be confirmed on its own. If either is missing, stop and fix before
   charging.
3. Quick preflight → charge → watch especially the **post-FC heat behaviour**:
   - the taper should EASE heat down from its value at FC engagement (setpoint
     decays from the measured engagement RoR toward 4 °C/min over ~90 s);
   - heat must **never rise above its value at engagement** (the never-add-heat
     clamp is law — if you see heat climb post-FC, that is a D88 failure:
     e-stop and record);
   - the deterministic drop fires at bean ≥ target AND dev ≥ target; the
     ceiling guard drops at bean ≥ 196 °C regardless of dev; the advisor keeps
     drop-earlier-only authority. Compare against roast 1.
4. End the run.

## After
- Run **roast-review** on both. Compare: did the #405 loop hold the RoR + release the drop cleanly vs the advisor-driven baseline? Note DTR, drop temp, and any oscillation.

## Safety
- Roast 2 is the **first hardware run of the D88 taper + ceiling guard** — supervise it. **Emergency stop is available from every phase.** The structural expectations: post-FC heat only ever moves DOWN from its engagement value (72→91 % class runaways are impossible by construction — seeing one is a stop-and-record event), and no drop lands above 196 °C with the guard on. A restart mid-run enters `operator_recovery_required` (no auto-resume of heat/fan).
