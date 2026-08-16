# Roast Session Runbook — 2-roast D88 A/B (baseline vs post-FC taper + ceiling guard)

> **Updated 11 Jul for the D88 validation session (issue #495).** The treatment
> arm is no longer the D83 fixed-band RoR loop (falsified 9 Jul, D87) — it is
> the D88 **measured-anchor taper** plus the **decoupled 196 °C ceiling-guard
> drop**, each behind its own flag with its own banner line.

> **Promoted 12 Jul (D88/D89, operator-ratified on this session's validation
> roast + 9/10 tasting).** Both flags now default **ON** — every new roast
> runs the taper + ceiling guard unless explicitly disabled. This runbook's
> arms are FLIPPED from how the 11 Jul session ran them: what was "Roast 1 —
> #405 OFF (baseline)" is now the launch-line-away arm (`POST_FC_LOOP=0
> CEILING_GUARD=0`); what was "Roast 2 — D88 ON (validation)" is now the
> plain default (no env vars needed). Kept below in its original session
> order/wording as the historical record of how THIS validation ran; a
> FUTURE baseline-vs-treatment A/B should launch the arms in the opposite
> command order from what's written here.

**Bean:** Guatemala El Durazno (White Honey) — **2×250 g, same bean both roasts** (a clean A/B needs the same bean; this Hottop runs best at a 250 g load). Select "Guatemala El Durazno (White Honey)" in Start-Roast. Targets: **drop 195 °C, dev 13 %** (first-roast de-risk).

## Before the session (once, at the roaster)
- Plug in + power on: Hottop (USB serial), FC mic, **Yocto-Meteo ambient probe**. Warm the Hottop.
- Ensure `~/roasts/coffee-roaster-mcp.yaml` has `ambient: { mode: yoctopuce }` (ambient on for both roasts).
- 2×250 g Guatemala El Durazno + scale.

## Roast 1 — #405 OFF (baseline)
1. `./scripts/roast-live.sh` — as run in this session (11 Jul, pre-promotion):
   the post-FC RoR loop was OFF by default at the time. **Since the 12 Jul
   promotion, a plain launch is no longer the baseline** — reproducing THIS
   arm now needs the explicit off-toggles:
   `POST_FC_LOOP=0 CEILING_GUARD=0 ./scripts/roast-live.sh`.
2. **Check the banner:** real Hottop (not mock), advisor **REACHABLE** (gpt-4o), **post-FC RoR loop: disabled**, ambient reading present.
3. Run the **pre-roast-preflight** → must be **GO** (serial, mic, advisor reachable, and the cold-machine safety checks: emergency-stop → heat 0, over-limit → CLAMP/REJECT, restart → operator_recovery_required).
4. Charge → auto **T0** → watch **FC** → **drop** (~195 °C, ceiling 196) → cool. Confirm ambient shows at charge.
5. End the run.

## Between roasts
- **Cool the drum** (physical gate). Stop `roast-live.sh` (Ctrl-C).

### If MCP teardown cannot be confirmed

- Start remains blocked. Do not treat retrying Start as confirmation.
- Physically verify that the roaster is inactive and that the previous MCP child no longer holds
  the serial or audio devices.
- On `/start`, open **I have checked the hardware**, tick the explicit verification checkbox, enter
  what you verified, and submit the acknowledgement. It clears only the matching process-local stale
  MCP generation; it never issues heat, fan, cooling, or another MCP command.
- If the incident changed or a run became active/recovering, the server rejects the request. Recheck
  the current state rather than replaying the old confirmation.
- A controlled full agent restart after the same physical verification remains the cross-process
  recovery alternative. A possibly-active persisted roast still enters
  `operator_recovery_required` and cannot be bypassed by this acknowledgement.

## Roast 2 — D88 ON (validation)
1. `POST_FC_LOOP=1 CEILING_GUARD=1 ./scripts/roast-live.sh` — as run in this
   session. **Since the 12 Jul promotion this is now the DEFAULT** (both flags
   default `True`); a plain `./scripts/roast-live.sh` with no env vars gives
   the identical resolved config — the explicit `=1` affirmations above are a
   no-op, kept here as the session's original launch line.
2. **CONFIRM the banner shows BOTH loud lines** — `⚠️  POST-FC RoR LOOP: ENABLED`
   AND `⚠️  CEILING-GUARD DROP: ENABLED (… ≥ 196 °C)`. The flags are independent;
   each must be confirmed on its own. If either is missing, stop and fix before
   charging. **Post-promotion, "missing" means an explicit `=0` toggle (or an
   env var) is quietly turning a flag OFF from its new default** — the same
   stop-and-fix rule applies either direction now.
3. Quick preflight → charge → watch especially the **post-FC heat behaviour**:
   - when the post-FC RoR loop is enabled, engagement heat is the actual pre-FC
     heat at the true-FC edge: with an open trim window, only a lower
     bean-specific `pre_fc_heat` can bind below the resolved trim; otherwise
     `pre_fc_heat` replaces the flat pre-FC floor higher or lower. The D88 base
     cap is `max(1, min(static heat ceiling, engagement heat))`, including its
     1% anti-stall floor. With the loop disabled, post-FC heat is advisor-driven
     and the D88/D96 caps are not enforced;
   - with the loop enabled and recovery disabled, the taper should EASE heat down from that cap
     (its setpoint decays from the measured engagement RoR toward 4 °C/min
     over ~90 s) and must never rise above it;
   - with the loop enabled and bounded recovery enabled, heat may rise only to
     the active recovery ceiling: the greater of the D88 base cap and the
     configured recovery term (engagement plus configured headroom, bounded by
     the static heat ceiling). D162's runway-aware path retains that same cap
     and releases recovery authority at its configured cutoff before gliding
     back to the base cap. A rise beyond the applicable cap is a stop-and-record
     failure;
   - the deterministic drop fires at bean ≥ target AND dev ≥ target; the
     ceiling guard drops at bean ≥ 196 °C regardless of dev; the advisor keeps
     drop-earlier-only authority. Compare against roast 1.
4. End the run.

## After
- Run **roast-review** on both. Compare: did the #405 loop hold the RoR + release the drop cleanly vs the advisor-driven baseline? Note DTR, drop temp, and any oscillation.

## Safety
- Roast 2 is the **first hardware run of the D88 taper + ceiling guard** — supervise it. **Emergency stop is available from every phase.** When the post-FC loop is enabled and recovery is disabled, post-FC heat only ever moves DOWN from the D88 base cap, `max(1, min(static heat ceiling, actual pre-FC heat at FC))`; with an open trim, only a lower bean-specific `pre_fc_heat` can bind, otherwise it replaces the flat floor higher or lower. If bounded recovery is explicitly enabled with that loop, it may rise only to the active recovery ceiling before releasing through D162; a rise beyond that applicable cap is a stop-and-record event. With the loop disabled, post-FC heat is advisor-driven rather than D88/D96-capped. No drop lands above 196 °C with the guard on. A restart mid-run enters `operator_recovery_required` (no auto-resume of heat/fan).
