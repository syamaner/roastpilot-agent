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
