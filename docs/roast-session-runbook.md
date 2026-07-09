# Roast Session Runbook — 2-roast #405 A/B (baseline vs post-FC RoR loop)

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

## Roast 2 — #405 ON (validation)
1. `POST_FC_LOOP=1 ./scripts/roast-live.sh`
2. **CONFIRM the banner shows `⚠️ POST-FC RoR LOOP: ENABLED`** — this is what makes roast 2 the treatment. If it doesn't, stop and fix before charging.
3. Quick preflight → charge → watch especially the **post-FC heat behaviour**: the deterministic RoR loop now drives heat and the deterministic drop fires when bean ≥ target AND dev ≥ target. Compare against roast 1.
4. End the run.

## After
- Run **roast-review** on both. Compare: did the #405 loop hold the RoR + release the drop cleanly vs the advisor-driven baseline? Note DTR, drop temp, and any oscillation.

## Safety
- Roast 2 is the **first hardware run of the deterministic post-FC loop** — supervise it. **Emergency stop is available from every phase.** Watch the drop does not overshoot 196 °C and the RoR loop does not oscillate. A restart mid-run enters `operator_recovery_required` (no auto-resume of heat/fan).
