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
- **Owner-task hazards (get these right or the fix trades a crash for a hang):** the
  owner MUST resolve `ready` on EVERY exit path (success, startup failure, and a
  last-resort guard) or `start()`'s `await ready` hangs — so bound that await too; and
  the startup-failure reap (`_await_owner_finished`) MUST `owner.cancel()` before
  awaiting, because a `start()` cancelled mid-spawn leaves the owner blocked inside
  `__aenter__`, so a bare await never returns.
- **A raising stdio teardown is a NORMAL event on this rig — never `pragma: no cover`
  it as unreachable.** A child segfault (roast 2) breaks the stdio pipes, so
  `stack.aclose()` re-raises `BrokenResourceError` / `ClosedResourceError`. Treat it as
  an UNCONFIRMED stop and **fail closed**: force-kill the (pre-respawn, non-recycled)
  pid group and set `stop_unconfirmed = True`, exactly like the wedged-child timeout
  path — never let it record as a confirmed clean stop, or a respawn sails past the
  #431 unconfirmed-stop guard and a restart skips `operator_recovery_required`. Signature
  to watch: an `except Exception` in `stop()` / teardown that only logs (leaving
  `stop_unconfirmed` False), or a `# pragma: no cover` on a teardown-error branch.
- **Guarded by:** `tests/test_mcp_respawn_real_child.py` (both tests fail pre-fix on
  the logged cross-task scope error) + `test_milestone1_real_mcp.py` (same-task real
  child) + `test_mcp_device_respawn.py` (`test_force_terminate_hook_rearmed_on_respawn`
  now drives the REAL `start`/`stop` cycle) + `test_mcp_client.py`
  (`test_start_does_not_hang_when_owner_dies_before_ready`,
  `test_start_bounds_an_owner_that_never_reports_ready`,
  `test_start_cancelled_mid_flight_reaps_the_owner`,
  `test_stop_fails_closed_when_aclose_raises` — the raising-aclose fail-closed path).

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
