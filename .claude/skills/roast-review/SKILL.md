---
name: roast-review
description: Post-roast debrief — pull a completed roast's trace + MCP logs and assess what worked / what to change (T0, FC, drop, dev%, trim, RoR, safety verdicts) against the profile targets and the operator's known-good thresholds. Use after a roast to review it, or when asked "review the roast".
---

Codifies the post-roast review. Produce an honest, data-backed debrief — not a
summary — and end with the rate + fixture closeout. All temperatures Celsius.

## 1. Resolve the run

Default to the latest **completed** run; accept an explicit run id if given. The
live server (default `:8000`) is the easiest source; fall back to the DB.

!`curl -sf -m5 http://127.0.0.1:8000/api/roasts | python3 -c "import json,sys; d=json.load(sys.stdin); r=sorted(d.get('runs',[]),key=lambda x:x.get('started_at_utc',''),reverse=True); print('\n'.join(f\"{x['id']} | {x.get('started_at_utc','')[:19]} | {x.get('outcome')} | FC={x.get('first_crack_at_utc')}\" for x in r[:5]))"`

If the server is down, read the trace DB directly (`$ROASTPILOT_DB`, default
`~/roasts/roastpilot.sqlite3`) — `roast_runs`, `roast_events`, `telemetry`,
`safety_evaluations`, `advisor_decisions`. Remember the **clock mismatch**:
telemetry is on a run-relative `elapsed_seconds`; events are on absolute
`monotonic_seconds` — rebase events to the `t0_detected` event's monotonic.

## 2. Pull the data

For the chosen `<RID>`:
- `GET /api/roasts/<RID>/timeline` → `events` (kind, source, monotonic_seconds, payload), `safety_evaluations`, `advisor_decisions`, `commands`.
- `GET /api/roasts/<RID>/telemetry` → `points` (tick, elapsed_seconds, bean_temp_c, env_temp_c, bean_ror_c_per_min, heat/fan_level_percent, development_percent).
- MCP export: `~/roasts/logs/roasts/<MCP_SESSION>/{summary.json,roast.jsonl}` — the MCP session id ≠ the agent run id; match by start time. Read `summary.json → first_crack_model` (the model that *detected* FC; all-null ⇒ FC was operator-marked) and any per-window confidence (#175, once it lands).

## 3. Compute the milestones (rebased to T0)

- **T0**: bean temp at `t0_detected` (payload `bean_temp_c`). ⚠️ Flag if it fired near the **turning-point minimum (~150)** rather than the **charge/decline onset (~179)** — that is mcp #174, ~15 s late.
- **FC**: `first_crack` event `source` — `mcp` = the audio detector fired; **`operator` = the detector MISSED and you hand-marked it** (flag mcp #175; note how late vs the real crack ⇒ under-counts dev%).
- **Drop**: the `phase_changed → cooling` event; bean temp ≈ the peak bean temp; the **dev% at drop** = the last non-cooling `development_percent`. Was it the **auto-drop** (an `advisor_decisions` row with `decision.should_drop=true` that the safety guard ALLOWED) or **held** (guard REJECTed while the bean climbed — the #323 conflict) or your manual drop?
- **Control**: did the **trim** engage (heat 100 → ~65 before FC)? heat → 0 post-FC? fan used as a brake? **RoR shape** — steady declining (good), a crash (RoR → ~0 within 90 s of FC = baked), or a **flick** (RoR re-accelerates ~90–120 s post-FC = char)?
- **Safety**: count ALLOW/CLAMP/REJECT/RECOVERY/FAULT/EMERGENCY_STOP; surface any non-ALLOW with its reason.

## 4. Assess against targets + the operator thresholds

Compare to the run's frozen profile (`target_drop_temp_c`, `target_development_percent`) AND the empirical Hottop ground truth (memories `operator-hottop-roast-profile`, `per-origin-dtr-washed-highgrown`):
- **Drop**: ≤195–196 = good; **>197 = over-done/bitter**; >200 = likely bad. Compare bean-at-drop to the profile ceiling.
- **Dev %**: vs the profile target; washed high-grown wants ~18 % eventual, naturals ~13 %; a late operator FC mark deflates the measured number.
- **FC**: detector fired vs missed; display BT ~178 at the true crack.
- **RoR**: the dominant lever — a flick chars more than a high DTR does.

## 5. Output the review

A scannable debrief:
- **✅ What worked** — with the numbers.
- **❌ What to change** — with the numbers + the candidate follow-up issue for each (mcp #174/#175/#176, agent #380, etc.).
- One-line **verdict** (clean / over-done / needs control change) and whether it met the run's intent.

## 6. Closeout

Prompt the operator to:
- **Rate the roast** in the UI (the D42 corpus label) — score + notes.
- **Register the fixture**: `python scripts/store_to_fixture.py "$ROASTPILOT_DB" --out-dir ~/roasts/fixtures/<slug> --run-id <RID>` (gitignored working dir; real stores are never committed).
- File any new issue the review surfaced.
