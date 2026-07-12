# Recently-fixed anti-patterns — the batch's shared memory (#453)

`pr-preflight` (Step 3) consults this list before you open a PR. Each entry is a
class of bug a recent PR fixed, plus a grep-able signature. **If your diff matches a
signature, apply the same fix.** When you fix a *class* of bug, add an entry here so
the next sibling PR in the batch is warned — agents run fresh-context and don't carry
a sibling's just-learned lesson otherwise, which is how #409 reintroduced #404's bug
in the same batch.

Keep entries short-lived: prune once the pattern is no longer a live hazard (the
underlying code is gone or a test now guards it repo-wide). This is a *hazard list*,
not a changelog.

Format: one entry per anti-pattern.
- **Signature:** a grep pattern / file glob that flags a likely reintroduction.
- **Wrong / Right:** the mistake and the fix.
- **Guarded by:** the test (if any) that now catches it repo-wide.

---

## The MCP stdio session's cancel scopes must enter AND exit in ONE owning task; stop/respawn is a request to that task
*(fixed by #484, 9 Jul 2026)*

- **Signature:** any code in `mcp_client.py` that enters `stdio_client` /
  `ClientSession` (an `AsyncExitStack`/`async with` over the MCP transport) in one
  place and calls `.aclose()` / exits it elsewhere — or any `MCPServerProcess.start`
  / `.stop` / respawn path where `start()` and `stop()` can run in *different tasks*
  (e.g. spawn in the serve/lifespan task, stop from a request handler like
  `start_roast`). Also: a respawn test that only uses `FakeMCPProcess` and never the
  real child.
- **Wrong:** exiting the stdio session's context stack from a task other than the one
  that entered it → anyio raises
  `Attempted to exit cancel scope in a different task than it was entered in`, which
  the old `stop()` swallowed (half-torn-down child, orphan, and under the real serve
  loop an `athrow(): async generator already running` → CancelledError cascade →
  store teardown → 500 → process exit).
- **Right:** a dedicated owner task (`_run_session`) holds the `async with` stack open
  for the session's whole lifetime and exits it IN-TASK on request; `start()` /
  `stop()` are cross-task REQUESTS (a `ready` future + a `_stop_requested` event), so
  `aclose()` always runs where the scope was entered. **And test any respawn path
  against the REAL `coffee-roaster-mcp` (mock driver), driving stop/start from a task
  other than the spawn task — a fake hides the cross-task teardown bug.**
- **Owner-task hazards (get these right or the fix trades a crash for a hang / an
  orphan):** (a) the owner MUST resolve `ready` on EVERY exit path (success, startup
  failure, last-resort guard) or `start()`'s `await ready` hangs — so bound that await
  too, and **bound it on `startup_timeout + call_timeout + margin`**, not
  `startup_timeout` alone: the owner runs `initialize()` THEN `get_server_info` in
  sequence before it reports ready, so a large `call_timeout_seconds` would else
  false-fail startup (Codex #492-2). (b) The startup-failure reap
  (`_await_owner_finished`) MUST **await the owner's natural completion FIRST (bounded),
  and `cancel()` only on overrun** — NOT cancel-first: on a post-enter startup failure
  the owner is mid-`aclose` in its own task, and cancelling it there aborts the in-task
  child cleanup → orphan (Codex #492-1). Cancel-only-on-overrun still unblocks the
  genuinely-stuck `start()`-cancelled-in-`__aenter__` case.
- **A raising OR cancelled stop teardown is a NORMAL event on this rig — never
  `pragma: no cover` it as unreachable, and always fail closed.** A child segfault
  (roast 2) breaks the stdio pipes → `stack.aclose()` re-raises `BrokenResourceError` /
  `ClosedResourceError`; and `stop()`'s own task can be cancelled mid-wait (shutdown).
  BOTH mean the child state is UNCONFIRMED, so **fail closed**: force-kill the
  (pre-respawn, non-recycled) pid group and set `stop_unconfirmed = True`, exactly like
  the wedged-child timeout path (shared `_fail_closed_teardown`) — and for a
  cancellation, mark-then-**RE-RAISE** (never swallow a cancellation). Never let either
  record as a confirmed clean stop, or a respawn sails past the #431 unconfirmed-stop
  guard and a restart skips `operator_recovery_required`. Signature to watch: an
  `except Exception` in `stop()`/teardown that only logs (leaving `stop_unconfirmed`
  False), a missing `except asyncio.CancelledError` on a fail-closed shutdown path, or a
  `# pragma: no cover` on a teardown-error branch.
- **Guarded by:** `tests/test_mcp_respawn_real_child.py` (both tests fail pre-fix on
  the logged cross-task scope error) + `test_milestone1_real_mcp.py` (same-task real
  child) + `test_mcp_device_respawn.py` (`test_force_terminate_hook_rearmed_on_respawn`
  now drives the REAL `start`/`stop` cycle) + `test_mcp_client.py`
  (`test_start_does_not_hang_when_owner_dies_before_ready`,
  `test_start_bounds_an_owner_that_never_reports_ready`,
  `test_start_cancelled_mid_flight_reaps_the_owner`,
  `test_start_failure_lets_owner_teardown_complete_not_cancelled` (P1 reap-ordering),
  `test_ready_timeout_bound_includes_call_timeout` (P2 bound),
  `test_stop_fails_closed_when_aclose_raises` (raising-aclose fail-closed),
  `test_stop_cancelled_mid_wait_fails_closed_and_reraises` (P3 cancelled-stop
  fail-closed + re-raise)).

## Chart / event markers must anchor to the charge-referenced origin, not the detection-fire frame
*(fixed by #404, reintroduced by #409, 1 Jul 2026)*

- **Signature:** a new chart marker or timeline placement in `web/` computed from a
  detection/fire timestamp or a raw event tick — grep the diff for marker/timeline
  placement using `payload.tick`, a detection-frame time, or an un-rebased event
  time near a `*Marker` / `EventTimeline` / `LiveCurve` change.
- **Wrong:** placing the marker at the point detection *fired* (e.g. T0 at the
  detection frame ~bean 141 °C / +11 s, or a landmark one tick early).
- **Right:** anchor to the **payload / backdated charge-referenced origin**
  (`t0ElapsedSeconds` / `elapsed − charge_elapsed`); for a landmark, use the
  payload-anchored time and a charge-referenced timeline clock, not the
  debounce-relative clock.
- **Guarded by:** the #404 marker-position test + the #409 payload-anchored-marker
  test. Add a marker-placement assertion for any NEW marker.

---

## Post-FC control-loop setpoints must anchor to MEASURED values, never a fixed band
*(fixed by #405 D88, 9 Jul 2026)*

- **Signature:** a closed-loop setpoint/target constant chosen ahead of time (a
  fixed `target_*` config default) that a PI/PID loop chases, especially post-FC
  heat/RoR control — grep for a new fixed numeric target field on a control-loop
  config (`PostFirstCrackControl` or similar) that isn't derived from a live
  reading at engagement.
- **Wrong:** a fixed RoR-band target (D83's `target_ror_c_per_min=8.0`) that sat
  ABOVE the measured post-FC engagement RoR (6.1 °C/min) — the loop read "too
  slow" from tick one and actuated a runaway heat climb (72→91 %) while the
  advisor recommended 0 %, fully policy-legal (every safety verdict was ALLOW).
- **Right:** anchor the setpoint to the MEASURED value at engagement and taper
  DOWN over a fixed duration (D88); clamp the loop's output so it can never
  exceed the heat/lever value the roast held at the moment of engagement (the
  never-add-heat-beyond-entry clamp, maxed with a 1 % anti-stall floor so a
  0-value handoff cannot pin the loop at a stall).
- **Guarded by:** `test_roast2_runaway_is_structurally_impossible` and the B1/B2/C1
  ratification tests in `tests/test_post_fc_control.py`.

---

## Two independent per-tick writers to the SAME actuator collide on the rate limit
*(fixed by #498, 11 Jul 2026)*

- **Signature:** two distinct code paths that can each call
  `_executor.set_targets` / `evaluate_command` with `seconds_since_last_command
  =self._seconds_since_last_command()` inside the same `tick()`, especially when
  both are gated by cadence timers with the SAME default interval (grep for a
  second call site issuing `set_targets` for a lever a deterministic loop also
  writes, or a new "advisor also actuates lever X" change touching
  `_run_advisory`'s post-FC branch).
- **Wrong:** the deterministic post-FC taper (`_apply_deterministic_post_fc_levers`,
  runs FIRST in `tick()`'s order) and the advisor's own consult
  (`_run_advisory`) each independently called `evaluate_command` with the real
  elapsed time and then wrote directly — both cadences default to 5 s, so a
  same-tick collision was the COMMON case: the taper's write consumed the
  tick's ONE `min_seconds_between_commands` slot, and the advisor's write hit
  `command_rate_limited` REJECT almost every time heat also moved (i.e. almost
  every tick that mattered).
- **Right:** coalesce to ONE writer. The non-primary path (the advisor's fan
  consult) safety-evaluates with `seconds_since_last_command=None` (it is a
  BOUNDS CHECK deriving a target, not a write attempt, so the rate limit must
  not gate it) and stores the clamped result in a held target
  (`self._post_fc_desired_fan_percent`); the SOLE writer
  (`_apply_deterministic_post_fc_levers`) reads the held target back and
  applies `(this tick's own computed value, the held target)` together in ONE
  `set_targets` call, firing whenever EITHER field differs from current (not
  only when the primary field moves — the held target's own tick-idempotence
  case still needs a real write). Clear the held target wherever the loop's
  own per-engagement state clears (`transition_to`, mirroring
  `_post_fc_engaged`), so a later engagement never inherits a stale one.
- **Guarded by:**
  `test_post_fc_loop_taper_heat_move_and_advisor_fan_move_both_land_same_tick`
  (fails pre-fix — the advisor's fan write REJECTed by the collision; passes
  post-fix) in `tests/test_controller.py`.

---

## A prompt splice chain can contradict itself in the FULLY ASSEMBLED text even when each fragment is individually correct
*(fixed by #499 Codex follow-up, 11 Jul 2026)*

- **Signature:** a new teaching section added to `advisor.py`'s base `c1`
  control-teaching prompt (or any versioned prompt others splice onto), tested
  ONLY against `control_teaching_prompt("c1")` — never against the live
  default (`c3`) or the most-spliced version (`c6`). Also: a claim in new
  prompt text that two context fields "always differ" / "are never equal" —
  grep for `RoastControlPolicy`'s capping logic (`min(hard_ceiling, profile.
  target)`) before asserting two told numbers can never coincide.
- **Wrong:** #499 added a joint-drop-objective section to `c1` teaching "a
  modest overshoot of one target while closing the other is preferred to an
  early drop." Every fragment-level test (`control_teaching_prompt("c1")`)
  passed. But `c2` (spliced onto c1, so it lands AFTER the new section in the
  assembled `c3`/`c6` text) already said "NEVER overshoot the drop target...
  the LATEST acceptable drop" — a LATER-spliced section directly contradicting
  an EARLIER one in the text the live model actually receives. Separately, the
  new section claimed the drop-temp target and the bitter ceiling are
  "DIFFERENT numbers" — false whenever a profile's `target_drop_temp_c` is at
  or below the hard bitter ceiling, since the control policy CAPS the told
  ceiling to the target in that case (`min(196, 195) == 195`), making them
  numerically identical.
- **Right:** (1) audit the FULL assembled text of every downstream version
  (`c2` through the newest, at minimum the live default `c3` and the
  newest/most-spliced version) for sentences that survive from an earlier
  splice and contradict a new section — not just the base fragment the new
  section was added to. (2) Never claim two told numbers always differ in
  VALUE unless one of them is genuinely uncapped by any profile field (here,
  `emergency_drop_temp_c` — never capped, and a validator pins it strictly
  above the bitter ceiling); teach that the MEANING differs regardless of
  whether the values happen to coincide.
- **Guarded by:** `test_assembled_prompt_carries_the_joint_objective_and_no_
  contradiction` and `test_assembled_prompt_joint_objective_precedes_every_
  later_section` (parametrized over `c3`/`c6`, asserting on the FULLY
  ASSEMBLED text, not the c1 fragment) in `tests/test_advisor.py`.

---

## Changing a `ControllerConfig` default silently rewrites replayed history unless replay pins its own baseline
*(fixed by #495 D88/D89 promotion follow-up, 12 Jul 2026)*

- **Signature:** any change to a `ControllerConfig` / `PostFirstCrackControl`
  field's *default value* (not just adding a field). Also: `replay.py`'s
  `build_replay_service` / `create_replay_app` taking `config: AppConfig | None
  = None` and falling through to a bare `AppConfig()` with no note that the
  fallback is now a moving target; or the CLI's `--replay` path (`cli.py`)
  passing the *live* resolved config straight into replay.
- **Wrong:** `ReplaySource` drives recorded telemetry through the REAL
  `RoastController.tick()` — including `_apply_deterministic_post_fc_levers`
  and `_maybe_ceiling_guard_drop` — with no "is this replay or live" gate. The
  #495 promotion flipped `PostFirstCrackControl.enabled` and
  `ceiling_guard_drop_enabled` from `False` to `True`. Replaying the
  `cooling-complete` fixture (which reaches 206 °C) under the new bare-default
  config reinterprets the recording: the ceiling guard fires a same-tick
  `source: policy` drop the instant the fixture's bean temperature crosses
  196 °C, pre-empting the fixture's own recorded `source: operator` drop 15 s
  later and skipping the advisory guidance the operator actually saw. The
  phase NAME sequence is misleadingly unchanged (`development` is still
  visited for one tick before the guard fires) — the divergence is in *which
  actor* issues the drop and *on what reading*, not the phase shape, so a
  test asserting only on phase-timeline shape can pass by coincidence.
- **Right:** replay is a fixed-recording player, not a live simulator — it
  must reproduce a FIXED recorded trajectory regardless of what a config
  default becomes later. `build_replay_service` / `create_replay_app` now
  pin the pre-#495 baseline (`enabled=False, ceiling_guard_drop_enabled=
  False`) INSIDE the factory whenever no caller opts out, via a
  `use_live_post_fc_control: bool = False` parameter; passing an *explicit*
  `AppConfig()` no longer matters (the pin applies regardless of whether a
  config was supplied at all) — only the explicit opt-out flag reaches live
  defaults. When you flip a config default, check every replay/simulation
  entry point that re-drives history through the same code path the live
  controller uses, not just the tests that construct configs directly.
- **Guarded by:** `test_replay_pins_the_baseline_post_fc_control_by_default`
  (bare vs. explicit-`AppConfig()` replay produce the IDENTICAL phase
  timeline) and `test_replay_live_post_fc_control_opt_out_diverges_from_the_
  recording` (the opt-out's drop is `source: policy` / `reason:
  ceiling_guard`, not the fixture's own `source: operator` drop) in
  `tests/test_replay.py`.
