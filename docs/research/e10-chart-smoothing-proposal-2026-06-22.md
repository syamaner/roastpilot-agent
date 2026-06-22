# Display-only curve smoothing — proposal (#307, item 1)

Status: **proposal — awaiting operator approval before implementation.** Nothing in
this PR (#307) changes the smoothing behaviour; the scaling (item 2) and heat/fan
overlay (item 3) ship, the smoothing approach is a decision for the operator.

## The problem

The bean and RoR curves render a visible **staircase**. The Hottop thermocouple
resolution is coarse and the probe response is slow (multi-second), so the
server-derived `bean_ror_c_per_min` (a short-window slope at the 1 Hz tick)
quantises into discrete integer-ish steps, and the bean curve itself climbs in
small flat-then-jump segments. #205 already added display-only RoR smoothing (a
**centred 15 s moving average**, `web/src/lib/rorSmoothing.ts`); the operator's
read after roast 3 is that it is not enough — the staircase is still legible,
and the bean line is not smoothed at all.

## Hard constraint (why this is not a free knob)

This is **display-only** and must stay so: the controller feeds the advisor and the
safety policy the **raw** channels server-side, well before any SPA smoothing. The
binding risk is **lag**. Per the #205 caveat and the FC-detector-lag findings, the
roast already fights a ~12–21 s audio-detector lag on first crack; a display filter
that shifts the curve right (or rounds off a real feature) would:

- blunt the **post-charge RoR crash** and the **pre-FC flick** — shapes the operator
  reads to time the heat-cut and the drop;
- hide a genuine **crash / flick-back** (a real, fast drop in RoR), which is a safety-
  relevant read even though the SPA never acts on it.

So the bar is: dissolve the staircase **without** lagging the live signal or
flattening a real fast feature. "Smoother" is easy; "smoother with no added lag and
no feature loss" is the whole problem.

## Options considered

1. **Wider centred moving average (e.g. 15 → 25–30 s).** Cheapest change (one
   constant). Artisan commonly runs RoR smoothing at 15–30 s, so 25 s is defensible.
   - Pro: trivial, centred ⇒ ~zero net lag on the persisted curve, tail-edge-only lag
     live (the freshest point is smoothed least — exactly where lag would hurt).
   - Con: a flat box-car window of that width starts to **round off** the crash/flick.
     A box-car weights a 30 s-old sample as heavily as the current one, so a sharp
     feature gets materially flattened. Marginal gain over today for real risk.

2. **Savitzky–Golay (polynomial-fit smoothing).** Fits a low-order polynomial
   (quadratic/cubic) over each window by least-squares and takes the fitted centre.
   - Pro: this is the **right tool** for a quantised-staircase that overlays a smooth
     underlying trend. A quadratic SG **preserves peak height and slope** far better
     than a moving average at the same window width — it dissolves the steps while
     keeping the crash depth and the flick. Centred ⇒ no net lag on the persisted
     curve.
   - Con: more code than a constant bump (a small fixed-coefficient convolution, or a
     tiny local least-squares); needs the **gap-bounded / shrinking-tail-window**
     handling the current MA already does (a null neighbour must bound the window, the
     live tail must shrink rather than reach into a future it doesn't have). Edge
     handling at the live tail is where SG needs care (a one-sided fit at the newest
     point can over/undershoot — clamp to a smaller order or fall back to the raw
     point there).

3. **EMA (exponential moving average) / one-Euro filter.** Trailing IIR filters.
   - Pro: cheap, streaming-friendly.
   - Con: **trailing ⇒ structural lag** — it shifts the whole curve right by ~the
     time-constant, which is precisely the failure mode the #205 note rejected a
     trailing filter for. The one-Euro filter mitigates lag adaptively but adds
     tuning surface and non-determinism that fights the D26 pixel-snapshot gate.
     Wrong trade-off for a curve where feature *timing* is the point.

## Recommendation

**Savitzky–Golay, quadratic, applied to BOTH bean and RoR, centred, with the same
gap-bounding + shrinking-live-tail rules the current MA uses** — window in the
~15–25 s band (operator to confirm the exact width against a real roast).

Rationale: the staircase is a high-frequency quantisation artefact sitting on a
smooth physical trend, which is the textbook case where SG beats a moving average —
it removes the steps at a given window width while **preserving the crash depth and
the flick** that a box-car of the same width would round off. It keeps the centred /
no-net-lag property #205 deliberately chose, and the live tail stays least-smoothed
(shrinking window), so it does not lag the freshest reading the operator times FC on.

Smoothing the **bean** line too (not just RoR) addresses the operator's actual
complaint — today only RoR is smoothed, and the bean staircase is still visible.

### Risk / trade-off, stated plainly

- **Lag vs smoothness:** centred SG adds ~zero net lag on the persisted curve and
  only shrinking-tail lag live; that is the best available point on the trade-off, but
  it is not zero — the newest 1–2 live points are fit one-sided and could wobble, so
  the live tail should fall back to the raw point (or a tiny order) rather than a full
  fit. **The raw channel must stay the safety/advisor source of truth regardless.**
- **Feature loss:** SG preserves features far better than an MA but is not lossless;
  the window width is the dial. I would validate the chosen width against a roast-3
  replay (the −29 °C/min crash and the pre-FC flick must still read clearly) before
  pinning it, the same way #205's 15 s was justified.
- **Determinism:** SG is a fixed linear convolution ⇒ deterministic ⇒ compatible with
  the D26 pixel-snapshot gate (unlike an adaptive one-Euro filter).

### Cheap fallback

If the operator wants the smallest possible step now: **widen the existing centred MA
to ~25 s and extend it to the bean channel.** It is a near-trivial change to a shared
helper, strictly better than today for the staircase, and reversible — at the cost of
some crash/flick rounding that SG would avoid. I would still prefer SG; this is the
low-effort interim if SG is deferred.

## Implementation note (not done in #307)

`rorSmoothing.ts` lives in the shared `web/src/lib/` foundation, so any of these is a
**shared-foundation change** routed through the lead, not a page-local edit. The
gap-bounding and shrinking-live-tail logic already in `smoothRorForDisplay` is the
reusable scaffold; an SG version would generalise it from "average the window" to
"least-squares-fit the window and take the centre", reusing the same window/gap walk.
