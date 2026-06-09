# E9-S2 Decision-Trace Summary — Vertical Slice Against the REAL `coffee-roaster-mcp`

Captured 2026-06-09 from `tests/test_milestone1_real_mcp.py`. Unlike the E9-S1
slice (`test_milestone1.py`, fake MCP), this run spawns the real
`coffee-roaster-mcp==0.1.3` as a **stdio subprocess in mock-driver mode**
(`MCPServerProcess` → `RoasterControlAdapter`): no fake reader/executor, the
closed loop talks to the live server over MCP, and the server's own mock roaster
driver simulates the temperature curve and runs the auto-T0 detector. Still 100%
hardware-free (mock driver, `first_crack_mode=disabled` — no Hottop, no
microphone, no model download). Produced by the `sim-roast-runner` review and
validated by `mcp-contract-checker` (zero drift vs installed 0.1.3).

## Run header

| Field | Value |
|---|---|
| Scenario | E9-S2 milestone vertical slice — controller drives the REAL MCP child |
| MCP server | `coffee-roaster-mcp` real subprocess; `bootstrap_safe=True`, `roaster_driver=mock` (asserted at startup) |
| Config | temp YAML via `COFFEE_ROASTER_MCP_CONFIG`: `driver: mock`, `first_crack.mode: disabled`, `auto_t0_detection_enabled: true`, `auto_t0_drop_threshold_c: 5` |
| Advisor | `_ChargeDropAdvisor` — context-aware: heat 100 / fan 0 until bean ≥ 55 °C, then heat 0 / fan 100 to engineer the bean-temp drop the server's auto-T0 detector needs |
| Clock | `FakeClock`, +3.0 s/tick (clears the 2 s command rate limit); mock advances 1 virtual second per state read → deterministic |
| Phases traversed | `starting → preheating → roasting_pre_first_crack → development → cooling → complete` |
| Outcome | `completed`; `export_manifest` present (real server wrote CSV/JSONL/summary) |
| Ticks | 64 (T0 at tick 61, FC at 62, drop at 63, stop-cooling/finalize at 64) |
| Wall-clock | ~2.8 s |
| Restart check | `store.active_run()` is `None` after `shutdown()` — completed run recoverable, not active |

## Decision-trace table (inflection points)

Bean °C is the **real mock server's** simulated thermocouple; advisor targets are
the post-safety executed heat/fan. Every verdict is **ALLOW** (`all_clear` for
commands; phase/event-validity rules at the transitions).

| Tick | Agent phase | Bean °C (real mock) | Advisor h/f | Verdict | Rule | Executed command |
|---|---|---|---|---|---|---|
| 0–1 | preheating | 20.2 | 100 / 0 | ALLOW | all_clear | set_targets 100/0 |
| 10 | preheating | 28.2 | 100 / 0 | ALLOW | all_clear | set_targets 100/0 (charge_guidance emitted) |
| 25 | preheating | 53.3 | 100 / 0 | ALLOW | all_clear | set_targets 100/0 (last ramp tick) |
| **26** | preheating | **55.2** | **0 / 100** | ALLOW | all_clear | **set_targets 0/100 — advisor cuts heat** |
| 39–42 | preheating | **64.5 (peak)** | 0 / 100 | ALLOW | all_clear | set_targets 0/100 |
| 60 | preheating | 58.9 | 0 / 100 | ALLOW | all_clear | set_targets 0/100 (falling) |
| **61** | **roasting_pre_first_crack** | **58.5** | 0 / 100 | ALLOW | event_source_validity (t0 from `mcp`) | **auto-T0 fires → debounced transition** |
| **62** | **development** | 58.0 | 0 / 100 | ALLOW | command_phase_validity + event_source_validity (first_crack from `operator`) | **operator MARK_FIRST_CRACK** |
| **63** | **cooling** | 57.0 | — | ALLOW | command_phase_validity (drop_beans valid in development) | **operator DROP_BEANS** |
| **64** | **complete** | 56.1 | — | ALLOW | command_phase_validity (stop_cooling valid in cooling) | **operator STOP_COOLING → finalize + LOGS_EXPORTED** |

## How the engineered charge drop tripped auto-T0

The mock driver simulates its own bean curve; the advisor shapes it the way a
real charge does:

1. **Ramp (ticks 1–25):** heat 100 / fan 0; bean climbs 20.2 → 53.3 °C.
2. **Heat cut (tick 26):** bean crosses the 55 °C ceiling; advisor flips to
   heat 0 / fan 100. Bean coasts up under residual heat to a **peak 64.5 °C**.
3. **Drop (ticks 42 → 61):** heat off + full fan, bean falls 64.5 → 58.5 °C — a
   ~6 °C drop, exceeding the configured `auto_t0_drop_threshold_c: 5`.
4. **T0 at tick 61:** the real server's detector flags T0 at bean 58.5 °C
   (`t0_detected`, source `mcp`, `debounce_ticks=3`); the controller debounces
   and transitions `preheating → roasting_pre_first_crack`. Genuine server-side
   detection over the real MCP boundary — not a scripted frame.

## Anomalies

None. No missed transitions, no debounce resets beyond the expected 3-tick T0
debounce, no advisor fallbacks, no verdict inconsistent with its inputs. Phase
progression and the operator-override sequence match the test's assertions, and
the post-restart store reports no active run. (The mock keeps a single
rising-then-falling arc and never re-heats, so post-T0 bean temp simply continues
its gentle fan-cooled decline — expected for this mock scenario, which carries no
curve targets per D7.)

## Demo-worthiness

All-ALLOW happy path — every tick's command/telemetry evaluation returned ALLOW.
It proves the real-MCP closed loop end to end (live subprocess, server-side
auto-T0, operator overrides, full safety on the drain, real log export) but
contains no CLAMP and no REJECT. To force those (D17 demo assets):
- **CLAMP** — `_ChargeDropAdvisor` requests an out-of-range target (e.g.
  `target_heat=120`) → bounds rule clamps to `[0,100]`, `verdict=clamp`.
- **REJECT** — a wrong-phase write (e.g. `set_heat` in cooling) or two writes
  inside the 2 s rate limit → `verdict=reject`.
