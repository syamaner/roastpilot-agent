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

---

## A query-cache refetch resolving is not proof the underlying fetch succeeded
*(fixed by #513, 12 Jul 2026)*

- **Signature:** `await.*refetchQueries` or `await.*invalidateQueries` (TanStack
  Query, `web/src/**`) where the awaiter treats the call RESOLVING as a success
  signal for control flow — navigation, a state latch, or the only user-visible
  feedback of an operation. Also: a code comment claiming a refetch/invalidate
  "surfaces" or "discovers" new server state before some later step, with no
  fallback if that refetch itself fails.
- **Wrong:** `LiveStartView` (`web/src/pages/live/LivePage.tsx`, the operator's
  actual start-roast path) did `await api.startRoast(profile); await
  queryClient.refetchQueries({ queryKey: roastKeys.health })` with no
  `navigate()` call, trusting that a successful health refetch alone would
  re-render the page into the live dashboard. `refetchQueries()` (and
  `invalidateQueries()`, which internally calls it) ALWAYS RESOLVES in this
  TanStack Query version, even when the underlying fetch genuinely failed —
  confirmed empirically against the raw `QueryClient` (`query.state.error` was
  populated with the real error; the awaited call still did not reject) — and
  even with `throwOnError: true` passed to `refetchQueries`. A transient
  `/health` failure right after a POST (e.g. a restart/MCP-respawn window)
  therefore left the operator on a silent, untouched-looking form with the
  roaster live and heating, no navigation, no error message, and no path to
  the emergency stop. The identical pattern existed on three further call
  sites: `StartRoastView` (`web/src/pages/home/StartRoastView.tsx`, the
  legacy `/start` route, still URL-reachable — very likely what the
  operator's screenshot actually showed, which explains the "Required."
  field-reset detail that could not be reproduced on `/live`);
  `DashboardPage`'s own idle branch (`web/src/pages/dashboard/
  DashboardPage.tsx`, unreachable via the current router but carries its
  own supported test contract); and `DashboardPage`'s
  `handleAcknowledgeFault` (same file) — lower severity (the operator keeps
  the FaultBanner/e-stop/dashboard the whole time this resolves, since heat
  is already off in `faulted` and stays off; the worst case was a
  silently-stale banner, not a stranded operator) but fixed anyway for
  consistency with this very entry and because it is precisely the recovery
  path an operator exercises on the next restart.
- **Right:** latch control flow on the PROVEN result of the mutation itself
  (e.g. the 201 from `api.startRoast`, or the acknowledge POST), never on a
  subsequent refetch/invalidate resolving. If you need the query cache to
  reflect a just-proven change, poll the resource directly (`api.health()`)
  with a bounded retry budget and write successes into the cache with
  `queryClient.setQueryData` — never rely on
  `refetchQueries`/`invalidateQueries` to signal failure to its awaiter.
  Pair with a visible transitional/failure UI state (never silently fall
  back to what looks like an untouched form, and never leave a control
  permanently disabled on total failure — re-enable it for a manual retry)
  and, where a real page navigation is available, a manual fallback action
  once retries are exhausted. Not every `invalidateQueries` site needs
  this: a `useMutation`'s `onSuccess: () => invalidateQueries(...)` that is
  `void`-called (not awaited by the caller), where the caller's own
  success/failure feedback comes from the mutation's own state, is safe —
  the worst case is a stale read until the next natural refetch, not
  stranded control flow (see the bean-profile CRUD hooks in
  `web/src/hooks/queries.ts`, `RoastRating.tsx`, `RoastedWeight.tsx`, and
  `DashboardPage`'s `run_completed` health-invalidation drain — audited, no
  fix needed, the dashboard keeps rendering live from SSE regardless).
- **Test-isolation trap when fixing this pattern:** a confirm-retry loop
  with a real (even short) `setTimeout` delay between attempts keeps
  running in its component closure after the test that triggered it
  finishes and `cleanup()` unmounts — React does not cancel in-flight
  promises on unmount. If the next test in the same file configures the
  SAME shared mock with `mockRejectedValueOnce`/`mockResolvedValueOnce`,
  the leaked loop from the PREVIOUS test can consume those queued
  once-values before the next test's own trigger fires, producing a
  flaky-looking failure that is 100% reproducible in full-file order but
  passes when the test is run in isolation. Fix: make every test that
  triggers the retry loop resolve the mock to the loop's actual success
  condition (or otherwise let it terminate) before the test ends, not just
  assert on the mocked read-model the render depends on.
- **Guarded by:** `web/src/pages/live/LiveStartFlow.integration.test.tsx`
  (real fetch-boundary integration tests — not the mocked-`useHealth`/
  mocked-`StartRoastForm` style that hid the original bug) +
  `LivePage.test.tsx`'s "start-roast flow (#513)" suite +
  `StartRoastView.test.tsx`'s active-run-banner tests +
  `DashboardPage.idle.test.tsx`'s "#513" tests (idle start flow AND the
  fault-acknowledge confirm/retry/failure states). All were confirmed to
  fail against the pre-fix code (`git stash` the source change, rerun)
  before the fix was restored — not vacuous coverage.

---

## Any confirm/retry loop in a component MUST guard against unmount
*(fixed by #513 qa follow-up, 12 Jul 2026)*

- **Signature:** a hand-rolled `for`/`while` retry loop inside a component
  (or its `useCallback`) that awaits an API call and a `setTimeout`-based
  delay between attempts, with no check of a mounted flag after each
  `await` — especially one that also calls `queryClient.setQueryData` or a
  `useState` setter. Grep for a new retry loop that does NOT import
  `runConfirmRetry` from `web/src/lib/confirmRetry.ts`.
- **Wrong:** the original #513 confirm-retry fix (the entry above) shipped
  three hand-rolled loops with no unmount guard. React does not cancel
  in-flight promises on unmount, so the loop's closure keeps running after
  `cleanup()`/navigation-away — an orphaned loop's next resolved attempt
  still calls `setQueryData` on the shared, app-wide query cache. Worse: a
  REMOUNT that starts its own fresh confirm loop (including this PR's own
  "Open live dashboard" fallback, which forces a remount) can then race the
  orphaned loop, both writing the same cache key from two different
  component instances. qa's probe proved this by unmounting mid-loop and
  asserting zero further `setQueryData` calls — it failed against the
  unguarded loops.
- **Right:** use the shared `runConfirmRetry` helper
  (`web/src/lib/confirmRetry.ts`) — never hand-roll the loop. It takes an
  `isMounted: () => boolean` callback checked after EVERY `await` (each
  attempt and each inter-attempt delay) and short-circuits with a distinct
  `"unmounted"` result before calling `onResult`/touching component state.
  Back it with a `mountedRef` set `true` on mount and `false` in a
  `useEffect` cleanup — plain `useRef(true)`, no library.
- **Guarded by:** `web/src/lib/confirmRetry.test.ts` (`isMounted` false
  mid-attempt and mid-delay never call `onResult`) + one component-level
  unmount test per confirm-loop site (`LiveStartFlow.integration.test.tsx`,
  `DashboardPage.idle.test.tsx` ×2) that unmounts mid-attempt via a
  controllably-stalled `api.health()` promise and asserts a `setQueryData`
  spy was never called. All three were fail-then-pass verified by
  temporarily hardcoding `isMounted: () => true`.
- **Sharpened nuance (#526, 13 Jul 2026):** the observable hazard this
  class describes is specifically a write to **shared/external state** —
  `queryClient.setQueryData`, module-level state, anything reachable from
  outside the component's own fiber. A guard on a **component-local
  `useState` setter** (no shared write at all) is NOT provably regression-
  tested the same way, and attempting one produces a VACUOUS test: five
  isolated probe components, checked against this repo's actual React 18 +
  jsdom + `@testing-library/react` stack in every configuration tried
  (plain unmount, `StrictMode`, `act()`-wrapped resolve), confirmed React 18
  logs **zero console output** — not the classic "Can't perform a React
  state update on an unmounted component" warning (removed for hooks-based
  updates starting React 18), not even the more general "not wrapped in
  `act()`" warning — for a `useState` setter called from an orphaned
  `.then()` after unmount. A local setter on an unmounted fiber is simply
  an inert no-op with zero cross-instance effect; a console-error-spy test
  or an unmount-then-remount cross-instance test both pass IDENTICALLY
  with or without such a guard (confirmed by reverting the guard and
  rerunning). **Still guard local-state confirm/fetch callbacks anyway** —
  it's the repo's safe-by-default convention and the value is
  FUTURE-PROOFING: the day a `.then()` like this grows a
  `queryClient.setQueryData` or any shared-state write (exactly how the
  original #513/#514 hazard arose), the guard is already in place. Do
  **not** write a "no console output" test to cover a local-state-only
  guard — it proves nothing. A non-vacuous regression test for this class
  requires a genuinely shared-state observable (a `setQueryData` spy, a
  module-level counter, cross-instance state) — see
  `LivePage.tsx`'s `fetchTerminalOutcome` effect for the documented example
  (a `mountedRef` guard kept for convention, with an in-code note
  explaining why it carries no regression test).

---

## Never treat unknown roaster status as idle
*(fixed by #513 qa follow-up, 12 Jul 2026; extended #513 post-#514 review, 12 Jul 2026)*

- **Signature:** a code comment reading something like "treat as idle" or
  "fall through to no-run state" attached to a `useHealth().isError` (or
  any other "the read failed"/"not yet known" state) branch that renders
  the SAME view as "no run is active" instead of a distinct explicit
  state. Also: a start/idle-gated component with an active-run check
  (`health.isSuccess && ...`) and an error check (`health.isError`) but NO
  guard for the pending state in between (neither true yet) — it falls
  through both `if`s to whatever renders last.
- **Wrong:** `LivePage.tsx` had `if (health.isError) { return
  <LiveStartView />; }` with the comment "Health error: active run unknown
  — treat as idle (fall through to no-run state)." Unknown is not idle: a
  run could genuinely be active and heating while `/health` is persistently
  failing (`useHealth`'s own `retry: 1` already absorbs a single blip
  before `isError` is true, so this is a real persistent failure, not
  noise), and falling through to the bare start form strands the operator
  without a path to the dashboard/emergency stop — the exact hazard class
  the rest of this batch fixes. `StartRoastView.tsx` had the same gap (it
  simply never handled `isError` at all, falling through past its
  active-run banner to the bare form) — and, caught by the post-merge
  review of #514, ALSO never handled the PENDING state (the initial
  `/health` fetch still in flight, before either `isSuccess` or `isError`
  is true): a reload of `/start` mid-roast showed the bare, untouched-
  looking form for one round-trip before the active-run banner appeared.
- **Right:** the rule is now general, not error-only: **a start form
  renders ONLY when health is resolved-success-and-idle; pending, error,
  and active-run states each render their own explicit state** — never
  fall through to the bare form from any state that isn't proven idle.
  `LivePage.tsx` already had the pending-state hold (`!health.isSuccess`);
  `StartRoastView.tsx` gained the matching hold, mirroring it exactly.
  Explain what's wrong (or that it's still loading) and offer a
  reload/retry path where relevant.
- **Guarded by:** `LivePage.test.tsx`'s "#513 medium" test (replaces the
  old test that asserted the WRONG behavior — falling through to
  `live-start-view` — with an assertion on `live-status-unknown`) +
  `StartRoastView.test.tsx`'s "#513 medium" test + its "loading hold (#513
  follow-up)" test (asserts the pending state shows `start-roast-loading`,
  never the bare form or either other explicit state). All fail-then-pass
  verified.

---

## `isSuccess: true` does not mean the read is CURRENT — a cached entry within `staleTime` is a silent no-fetch
*(fixed by #513 Codex follow-up on the #514/#515 review, 12 Jul 2026)*

- **Signature:** a gating component (a start form, an auth check, anything
  that decides whether to show a bare/default view based on server state)
  that reads a shared `useQuery`-family hook with a non-zero `staleTime`
  and branches on `isSuccess`/`isError` alone, with no distinction between
  "settled from THIS mount's own fetch" and "settled from a cache entry
  some OTHER mount populated up to `staleTime` ago." Also: `refetchOnMount:
  "always"` added to "fix" a staleness concern without also gating on
  something that tracks the NEW fetch settling — `isSuccess` stays `true`
  (from the stale cached data) for the entire duration of that forced
  background refetch.
- **Wrong:** `useHealth()` shares the app's `staleTime: 30_000` (correct
  for non-gating consumers like the header/nav, which should render
  stale-then-update). But `LiveStartView`/`LivePage` and `StartRoastView`
  gated their start form on plain `useHealth()`'s `isSuccess`/`isError` —
  confirmed empirically: a remount within that 30s window renders a CACHED
  `active_run_id` with `isSuccess: true` and issues **NO network request
  at all** (TanStack Query does not refetch on mount while data is still
  fresh by its own accounting). A second `roastpilot-agent` process (or
  another tab) starting a run in that window would be invisible to a
  fresh page load for up to 30s — the bare start form would render as
  "proof" no run is active from data that was never re-checked. This is
  the SAME hazard class as the rest of #513 (a start form rendering when
  it must not), reached via a different mechanism (a cache hit, not a
  failed refetch) — caught by Codex on the #515 PR, not self-discovered.
- **Right:** the two start-form gating views use a dedicated
  `useFreshHealthGate()` (`web/src/hooks/queries.ts`) instead of plain
  `useHealth()`. It forces `refetchOnMount: "always"` AND tracks
  `isFresh`: snapshot the `dataUpdatedAt` seen on THIS hook instance's
  first render (whatever it is — `0` with no cache, or a past timestamp if
  cached) once into a `useRef`, then `isFresh = dataUpdatedAt >
  initialSnapshot || isError` — `dataUpdatedAt` advances on every settled
  fetch even one that resolves with byte-identical data (confirmed
  empirically), so this is a reliable "this mount's own fetch has
  completed" signal that a naive `isSuccess`/`isFetching` check is not
  (both stay `true`/`true` or `true`/`false` in ways that don't
  distinguish stale-cached from freshly-confirmed). Gating views hold
  their loading state on `!isFresh`, not `!isSuccess`. Non-gating
  consumers (header/nav, `DashboardPage`'s idle branch — the last is
  itself unreachable via the current router and out of scope for this
  specific fold; flagged, not fixed here) keep plain `useHealth()`
  unchanged — they are meant to render stale-then-update.
- **Guarded by:** `queries.test.tsx`'s `useFreshHealthGate` suite (no cache
  → pending-then-fresh; a within-`staleTime` cached entry → `isFresh`
  stays `false` through the forced refetch even though `isSuccess` is
  already `true`, only flipping once the NEW value — a run started
  elsewhere — resolves; a persistent error settles `isFresh` to `true`,
  never stuck) + a component-level test per gating view
  (`LivePage.test.tsx`, `StartRoastView.test.tsx`) + a REAL fetch-boundary
  integration test per view in `LiveStartFlow.integration.test.tsx` (a
  real `QueryClient` configured with the app's actual `staleTime: 30_000`,
  primed with a cached idle snapshot via `setQueryData`, then a genuinely
  stalled `fetch` mock — the closest reproduction of the real hazard this
  repo can exercise). All fail-then-pass verified.

## A second listener on the roast port silently hijacks the browser's API traffic — kill by PROCESS FAMILY and verify port absence, never by subcommand pattern
*(fixed by the roast-live.sh hardening, 12 Jul 2026 — the #513 incident pair's root cause)*

- **Signature:** any `pkill`/cleanup that matches a SUBCOMMAND form (e.g.
  `pkill -f 'roastpilot-agent serve'`) in a script that then binds a port; any
  new tool/harness/webServer that starts `roastpilot-agent` in a different
  invocation form (e.g. `--replay ... --port 8000`, see
  `web/playwright.config.ts`); any launcher that binds `0.0.0.0:$PORT` without
  first verifying NOTHING listens on `$PORT`.
- **Wrong:** assuming a failed bind will surface a port conflict. On macOS a
  specific `127.0.0.1:$PORT` bind coexists with a `0.0.0.0:$PORT` wildcard bind
  (SO_REUSEADDR — uvicorn's default) and then receives ALL loopback
  connections: the browser talks to the impostor (an idle replay/e2e harness
  answering `/api/health` with `active_run_id: null`) while the real roast
  server logs only fragments of the traffic. No error anywhere; the operator
  is stranded on a start form with the machine live (#513, both incidents).
- **Right:** cleanup kills the whole process family (`pkill -9 -f
  'roastpilot-agent'`), then the launcher REFUSES to start while `lsof
  -nP -iTCP:"$PORT" -sTCP:LISTEN` finds anything, printing the offender.
  Absence of a second listener is verified, not assumed. If a live rig
  misbehaves while mocks are clean, run the curl + lsof pair WHILE the symptom
  shows — dead sockets keep no history.
- **Guarded by:** the hard guard in `scripts/roast-live.sh` (aborts with the
  offending pid list). No test — shell-launcher behaviour; validated manually
  12 Jul (clean single-listener launch, roast 13 completed).

## The gate you ran is not the gate CI runs unless you invoke the project's exact commands
*(learned twice in one batch, 12 Jul 2026 — #532's tsc miss + #522's venv miss)*

- **Signature:** any gate claim based on a bare tool invocation — `npx tsc --noEmit`
  (CI runs `npm run typecheck` = `tsc -b --noEmit`, project-reference mode, which
  catches arity/type errors the bare form misses — reproduced byte-for-byte);
  plain `python`/`pip` in a worktree (shell-aliased to the global interpreter on
  this machine, so `pip install -e .` silently lands in global site-packages and
  pyright resolves the wrong deps — ~20k phantom errors).
- **Wrong:** "typecheck clean" / "pytest green" from ad-hoc invocations.
- **Right:** the npm scripts (`npm run typecheck/lint/test/build`) and
  `.venv/bin/python -m ...` explicitly, verified via `sys.prefix` when in doubt.
- **Guarded by:** convention only — CI is the backstop, but pre-open claims must
  use the real commands.

## A bare test QueryClient silently defeats every staleness test
*(found by #532's fold verification, 12 Jul 2026)*

- **Signature:** `new QueryClient(...)` in a test file whose assertions involve
  staleTime/freshness/refetch behaviour, without mirroring the app's
  `queryClient.ts` defaults.
- **Wrong:** the library default `staleTime: 0` makes every seeded cache entry
  look stale, so a "stale cache renders wrongly" test passes with or without
  the fix — vacuous.
- **Right:** construct test clients with the app's actual cache defaults (or
  import the shared config); mutation-verify freshness tests against the exact
  refactor they claim to guard.
- **Guarded by:** the #532 integration suite does this; new freshness tests must.

## Safety-commit BEFORE any destructive verification step, mutation testing included
*(the 12 Jul near-miss — a repeat of the 9 Jul reviewer incident, self-inflicted)*

- **Signature:** `git checkout --` used to "restore after" a temporary mutation,
  scripted edit, or fail-then-pass check on UNCOMMITTED work.
- **Wrong:** `git checkout -- <file>` restores to the last COMMIT — if the work
  under test is uncommitted, the restore destroys it (lost a full fold pass).
- **Right:** commit first (local, no push), or back up to a scratch copy and
  restore from THAT. The safety-commit protocol applies to every destructive
  step, not just review handoffs and branch switches.
- **Guarded by:** protocol; the commit-before-verify habit.

## A `devices[...]` spread inside a Playwright project's `use` silently wins over the top-level viewport, and `toHaveScreenshot` defaults `fullPage: false` with no project-wide override available
*(fixed by #530, 14 Jul 2026)*

- **Signature:** a Playwright project's `use: { ...devices["Some Device"], ... }`
  spread in `playwright.config.ts` where the top-level `use` also sets its own
  `viewport` — object-spread precedence is POSITIONAL WITHIN THAT ONE OBJECT
  LITERAL, not "top-level vs project-level," so a `devices[...]` preset's own
  `viewport` silently overrides whatever the top-level `use.viewport` says
  unless re-asserted after the spread. Also: any `expect(page).toHaveScreenshot()`
  call with no `fullPage` option, on a page whose content can exceed the
  configured viewport height.
- **Wrong:** the `chromium` project spread `...devices["Desktop Chrome"]` (whose
  own `viewport: {1280,720}`) after which nothing re-asserted the intended
  `{1600,1000}` — every committed baseline was exactly 1280×720 regardless of
  the top-level config. Separately, even a correctly-applied fixed viewport
  can't capture a page taller than it; the detail page's real `scrollHeight` is
  2599px, so the actual "whole page" guarantee has to come from `fullPage:
  true`, not viewport size. `fullPage` was tried as a project-wide
  `expect.toHaveScreenshot` default first — `tsc -b` correctly rejects this;
  Playwright's project-wide config type for that assertion omits `fullPage`, it
  only exists on the per-call `toHaveScreenshot(name, { fullPage: true })`
  signature.
- **Right:** re-assert the intended `viewport` (and any other device field you
  care about) AFTER the `devices[...]` spread inside the SAME object literal.
  Set `{ fullPage: true }` explicitly on every `expect(page).toHaveScreenshot()`
  call site (not the project-wide default) for any suite whose docs/intent
  claim "whole page" coverage. A locator screenshot (`expect(locator).
  toHaveScreenshot()`) is unaffected either way — it already captures its full
  element.
- **Guarded by:** the regenerated `__screenshots__/**/*-linux.png` baselines
  (dimensions verified against measured `scrollHeight`, and the previously
  below-the-fold widgets — ratings, weight, tasting notes, roast conditions,
  export — visually confirmed present in the COMMITTED images, not just a local
  darwin run). No dedicated regression test beyond the baseline diff itself;
  the viewport/`fullPage` config is exercised by every snapshot spec in the
  suite.

## Codecov can process a report yet never post the `codecov/patch` status — re-run the upload job, don't add commits

- **Date:** 14 Jul 2026 (second occurrence; first seen on PR #542, 13 Jul)
- **Class:** a PR sits `BLOCKED` with every visible check green because the
  required `codecov/patch` commit status is simply ABSENT — not failed, not
  pending, absent. `main` requires it, so the merge box shows "expected"
  forever.
- **Wrong:** treating it as a coverage problem (it isn't — nothing failed), or
  re-triggering with a junk commit (hygiene violation), or waiting it out
  (30+ min of polling never resolved it).
- **Right:** diagnose in two reads, then re-run one job. (1) `gh api
  repos/<owner>/<repo>/commits/<head-sha>/status` — if `codecov/patch` is
  missing from the statuses list, the notification was lost, not the upload;
  (2) confirm via codecov's API
  (`https://api.codecov.io/api/v2/github/<owner>/repos/<repo>/commits/<sha>`)
  that the report state is `complete` — it typically is. Then re-run the
  specific job that uploads coverage: `gh run rerun --job <databaseId>`, where
  the numeric job id comes from
  `gh run view <run-id> --json jobs --jq '.jobs[] | {name, databaseId}'`
  (`--job` takes the job's databaseId, not its display name). A fresh upload
  triggers a fresh notification, and the status lands within minutes. No new
  commit, no CI-wide re-run.
- **Guarded by:** nothing automatic — this is infra, not code. The merge
  watchers poll `mergeStateStatus` and surface the stall; this entry is the
  runbook for the two-read diagnosis.

## FREEZE a teammate's branch the moment the lead opens its PR — pushes and review triggers race

- **Date:** 14 Jul 2026 (PR #547; the FIFO-on-idle mailbox lag's third crossing)
- **Class:** teammate mailboxes deliver FIFO on IDLE only, so a lead's ruling
  can land AFTER the teammate already proceeded — and, worse, a teammate's
  late fold can land AFTER the lead already opened the PR and started the
  codex-wait cycle. A push that arrives after the `@codex review` trigger
  fires makes the review verdict stale against the true head (the exact
  #518-class failure the wait rule exists to prevent).
- **Wrong:** treating "I pushed and reported" as the end of a teammate's turn
  while the lead independently opens/cycles the PR — two async actors, no
  branch ownership handoff.
- **Right:** the PR-open is the ownership handoff. The lead's open message
  MUST include an explicit FREEZE instruction (no further pushes without a
  lead go-ahead), and every teammate report of "pushed sha X" should
  re-freeze by default. If a late push does land pre-trigger, the cycle
  self-corrects (the trigger fires on whatever head CI greens); if it lands
  post-trigger, the lead re-runs the cycle on the final commit — never merge
  on a verdict that predates the head.
- **Guarded by:** process only — the runbook rule in
  `docs/agent-team-worktrees.md` (§ "Branch freeze on PR-open") and the
  codex-wait rule's "signal must postdate the final-commit trigger" clause in
  AGENTS.md.

## An operator-facing readout must resolve config through `load_app_config()`, never a bare `AppConfig()`

*(fixed by #746, 9 Aug 2026)*

- **Signature:** `grep -rn "AppConfig()" scripts/ src/` — any site that
  CONSTRUCTS `AppConfig()` and then PRINTS/renders one of its values for the
  operator. Especially `python -c` heredocs inside `scripts/*.sh`.
- **Wrong:** `AppConfig` is a pydantic `BaseSettings` with
  `env_prefix="ROASTPILOT_"` and **no YAML source**, so a bare `AppConfig()`
  sees environment variables and schema defaults only. It never reads the
  operator's saved `~/.roastpilot/config.yaml`. The serving agent resolves
  through `config_store.load_app_config()` (env ?? saved file ?? default), so
  the two views diverge for every value the operator set in the `/config` UI.
  `scripts/roast-live.sh` printed `prompt c3` on its pre-charge banner while
  the agent genuinely ran the saved `c10`, and its "non-default" tag never
  fired because the env-only view matched the defaults. A readout that
  disagrees with runtime is worse than no readout: it is trusted at exactly
  the moment (pre-charge) when the roast cannot be re-run.
- **Right:** resolve through `load_app_config()` and compare against the SCHEMA
  defaults when deciding whether to tag a value as non-default. Put the
  resolution in an importable seam (`roastpilot_agent/launch_banner.py`) with
  unit tests, not in a shell heredoc, and fail LOUD on a malformed saved config
  — print an explicit "unresolved" rather than a plausible-looking default.
  Note the two legitimate exceptions found in the same sweep, which must stay
  as they are: `config_store.load_app_config` itself, and
  `replay.create_replay_app`'s `config or AppConfig()` fallback (deliberate —
  a replay must reproduce a fixed recorded trajectory regardless of live
  config; see the replay entry above). `scripts/advisor_smoke.py` is also
  fine: the bare config it prints is the same object it then runs on, so its
  readout is honest.
- **Also:** resolving the right config is only half of it — the readout must
  report the value the code path actually *consumes*, and it must know WHICH
  code path consumes it. Four related traps in this same banner, all caught by
  the local Codex pass pre-open, over two rounds:
  (a) `advisor.model_slug` is shadowed by `model_slug_by_phase` (the advisor
  calls `model_for(phase)`, and that map ships populated and is not editable
  from `/config`), so the banner must print the phase-resolved model — the
  runtime bug is #747; (b) but the base slug is NOT unused: `healthcheck()`
  probes reachability with it, so an invalid one still drives the startup
  advisor status — say "gives no roast advice", not "unused"; (c) only
  `AUTO_ADVICE_PHASES` (DEVELOPMENT alone, under D35) consults the advisor at
  all, so deriving the readout from the per-phase model MAP would advertise a
  pre-FC model no advisory call can reach — import the controller's own gate;
  (d) `late_maillard_trim.enabled=False` makes `_trim_engaged` always false, so
  a depth or adaptive band left in the saved config is dead and must not be
  announced. Before printing a config value, follow it to its consumer, and
  check whether that consumer runs at all.
- **Guarded by:** `tests/test_launch_banner.py` — a saved-only `prompt_version`
  must reach the banner, a saved-only non-default must be tagged, a shadowed
  `model_slug` must be reported as shadowed, and a disabled trim must report
  the flat floor rather than its leftover depth.
