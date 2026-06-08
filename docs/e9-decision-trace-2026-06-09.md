# E9-S1 Decision-Trace Summary — 12-Step Mock Vertical Slice (Fake MCP)

Captured 2026-06-09 from the first green run of `tests/test_milestone1.py`
(plan §8: the vertical slice's decision trace is a talk/demo asset, recorded
the same day). This is the **fake-MCP slice (E9-S1)**: the full vertical slice
driven through `RoastService` + `RoastRunner` against `FakeMCPClient`, a
deterministic `FakeAdvisor`, a `FakeClock`, and a temp SQLite `RoastStore` —
hardware-free. The real-`coffee-roaster-mcp`-subprocess (mock-driver) variant
of the identical flow is **E9-S2**.

Produced by the `sim-roast-runner` review (per `.claude/agents/sim-roast-runner.md`).

## Run header

| Field | Value |
|---|---|
| Scenario | 12-step milestone: start → preheat → debounced auto-T0 → first crack → advisory → safety validation → heat/fan command → operator drop → stop-cooling → log export → restart recovery |
| Profile | House Espresso (Ethiopia Heirloom, 250 g, drop 205 °C, dev target 20 %) |
| Phases traversed | `starting → preheating → roasting_pre_first_crack → development → cooling → complete` |
| Tick model | `FakeClock` advanced 3.0 s/tick (clears the 2 s command rate limit + telemetry throttle); 9 controller ticks total |
| Advisor | `FakeAdvisor` returning a constant `target_heat=55, target_fan=45, should_drop=False, confidence=0.9` |
| Outcome | `complete` / `completed`; export manifest `ready=True`; restart re-reads phase `complete`, no active run (fresh roast unblocked) |
| Test | `tests/test_milestone1.py` → **1 passed** |

## Decision-trace table

Rows are per-tick controller activity. "Bean °C" is the telemetry frame in
force at that tick; advisor targets are the `RoastDecision` (heat/fan);
verdict/reason are from the `set_targets` safety evaluation (the heat/fan
write); the rightmost column is the executed MCP write.

| Tick | Phase (after) | Bean °C | Advisor h/f | Verdict | Reason | Executed command |
|---|---|---|---|---|---|---|
| start | preheating | 178.0 | — | ALLOW | `all_clear` — within bounds & rate limit | `start_session`; `set_targets(70/40)` (profile initial) |
| 1 | preheating | 178.0 | 55/45 | ALLOW | command within bounds and rate limit | `set_targets(55/45)` |
| 2 | preheating | 178.0 | 55/45 | ALLOW | command within bounds and rate limit | `set_targets(55/45)` |
| 3 | preheating | 95.0 (T0 frame, debounce 1/3) | — | ALLOW | telemetry within configured limits | — |
| 4 | roasting_pre_first_crack | 95.0 (T0 debounce 3/3) | 55/45 | ALLOW | `event_source_validity`: t0 from `mcp` accepted; `set_targets` within bounds | `set_targets(55/45)` |
| 5 | development | 196.0 (first_crack) | 55/45 | ALLOW | `event_source_validity`: first_crack from `mcp` accepted; `set_targets` within bounds | `set_targets(55/45)` |
| 6 | development | 205.0 | 55/45 | ALLOW | command within bounds and rate limit | `set_targets(55/45)` |
| 7 | cooling | 205.0 | — | ALLOW | `command_phase_validity`: `drop_beans` valid in `development` | `drop_beans` (operator) |
| 8 | complete | 205.0 | — | ALLOW | `command_phase_validity`: `stop_cooling` valid in `cooling` | `stop_cooling` (operator); `export_roast_log` |

**MCP write log (in order):** `start_session, set_targets ×6, drop_beans, stop_cooling, export_roast_log`.

**Trace storage note.** Advisory outcomes persist as `advisory` **events**
(each carrying the embedded `decision` + `evaluation`); the heat/fan verdicts
land in `safety_evaluations`. The dedicated `advisor_decisions` timeline array
is **empty** in this slice — it is the provider-call telemetry channel
exercised by the real `PydanticAIAdvisor` (provider/model/latency/context
hash), not by `FakeAdvisor`. Operator `drop_beans`/`stop_cooling` are the rows
in the `commands` (command_log) table; the heat/fan `set_targets` writes appear
as `command_executed` events. The "advisory → verdict → command" trace is fully
readable via `GET /api/roasts/{id}/timeline` (events + safety_evaluations +
command_log).

## Anomalies

None. All four phase changes fired on schedule (T0 only after the full 3-tick
debounce; FC on the first crack frame; drop → cooling; stop-cooling →
complete). The T0 debounce counted cleanly to 3 with no reset. No advisor
fallback path was taken. Every verdict is consistent with its inputs (heat
55 / fan 45 in-bounds; 3 s/tick spacing never trips the 2 s rate limit; both
operator commands phase-valid). Restart re-reads `complete`/`completed` with the
export manifest intact and `active_run()` is `None`.

## Demo-worthiness

The happy-path slice contains **zero CLAMP and zero REJECT** — all persisted
safety evaluations are ALLOW. This is expected and correct: a clean roast with
an in-bounds, well-paced advisor gives safety policy nothing to clamp or reject.
It therefore does not, on its own, satisfy the talk's "≥1 CLAMP and ≥1 REJECT"
requirement (D17 demo assets, recorded by end of August).

To produce those verdicts, drive a variant slice (all hardware-free, same fakes):

- **CLAMP** — script the `FakeAdvisor` to return an out-of-range target, e.g.
  `RoastDecision(target_heat=120, …)`. The bounds rule clamps to `heat 100`,
  emits `verdict=CLAMP` (rule `command_bounds`), and the executed `set_targets`
  carries the clamped `adjusted_heat=100`.
- **REJECT** — any of: (1) advance the clock < 2 s between two heat/fan ticks →
  `REJECT` (rate limit); (2) script `should_drop=True` outside the allowed
  window → `REJECT` (drop eligibility), no `drop_beans` issued; (3) feed a
  scripted `AdvisorFailureMode` → `REJECT` via `evaluate_advisor_failure` with
  the hold-current-targets fallback.

Recommended demo asset: this clean 12-step slice **plus** one stress tick
(over-range advisor target → CLAMP) and one sub-2 s tick (REJECT), so a single
timeline view shows ALLOW, CLAMP, and REJECT side by side.
