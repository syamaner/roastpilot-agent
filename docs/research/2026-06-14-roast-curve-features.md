# Roast-curve-in-progress derived features — research synthesis (14 Jun 2026)

Feeds **#229** (curve-insight feature spike), **#223** (post-FC LLM context), and **D36**.
Produced by the `deep-research` harness (run `wf_8f0330f2-159`): 5 search angles → 21
sources → 94 extracted claims → 25 adversarially verified (3-vote, 2/3-to-kill) → 22
confirmed / 3 killed. The harness's auto-synthesis step failed on a socket error; this
report is the hand synthesis of the verified claim set.

**Source-quality caveat:** the *signal-processing / control-theory* claims rest on primary
peer-reviewed sources; the *roasting-practice* claims rest on authoritative-practitioner
blogs (Cropster, Scott Rao, Artisan-roasterscope, IKAWA, Mill47) — strong in-field, not
peer-reviewed. Quality is flagged per claim below.

---

## Verdict: feature ranking by real informativeness for live control

### Tier 1 — well-supported, build these

1. **RoR (first derivative of bean temp) + its smoothing.** RoR *is* `ΔBT/Δt`, the first
   derivative of the temperature curve (Artisan, primary-ish). Raw RoR is noisy because all
   sensors carry measurement noise, so RoR must be smoothed *beyond* smoothing the base
   curve. Smoothing buys smoothness at the cost of **lag proportional to the smoothing**, so
   smooth **early in the chain**. Implication for us: RoR is load-bearing, but the
   smoothness/lag tradeoff compounds with our 12–21 s detector lag — tune deliberately.
   *(artisan-roasterscope, blog)*

2. **Predicted-FC ETA by extrapolating smoothed RoR — NOT a hardcoded temperature.** This is
   the established practitioner method: Artisan's live dry-end / first-crack time predictions
   and projection lines are computed by **extrapolating the smoothed RoR**, not a fixed temp.
   *(artisan-roasterscope, blog)* Commercial ML precedent: **Cropster ships AI first-crack
   prediction**, surfaced as a window **~3 minutes before** predicted FC, trained on roast
   data rather than a threshold. *(cropster, secondary)* → **Directly validates** the D36 /
   #223 decision to feed a *profile-band ETA from RoR extrapolation* instead of "expected FC
   ~180". Feasibility is supported: the pre-FC bean-temp trajectory is **modellable**, not
   noise-dominated (a semi-empirical heat-transfer model hits **RMSE = 2.7 °C up to FC**).
   *(Birmingham, primary)*

3. **DTR (development time ratio), live.** Defined and computed live as
   `time-since-FC / total-time-since-charge`, as a percentage (e.g. `50/(540+50) = 8.5 %`).
   *(artisan-roasterscope + Scott Rao, blog/secondary)* → Confirms #220/#223's live DTR.

4. **Control-signal stability / "entropy" as the anti-thrash metric — on the COMMAND series,
   not the temperature.** See the entropy verdict below. This is where complexity metrics earn
   their place, and the literature gives concrete, cheap, online methods. *(primary control-
   theory sources)* → **Validates the #229/D36 framing.**

### Tier 2 — real phenomena, use as observed events (trust the signal, not the folklore)

5. **Crash and flick are RoR-derivative events, and the flick is anticipatable.** The flick is
   an **RoR rebound ~2 min after FC** (beans reheat, RoR kicks up); the crash is an **RoR drop
   early in FC**. Both are visible on the live RoR. **Cropster ships AI flick prediction**, so
   anticipation is demonstrably possible. *(cropster, secondary; mill47, blog)*
   **Caveat (adversarially killed claims):** the *mechanistic stories* are NOT well-supported
   — "flick = the cause of charred flavour (vs DTR)" was killed 0-3, and "crash = FC exothermic
   moisture loss drops bean temp" was killed 0-3. So: **treat crash/flick as observed RoR-shape
   events to detect/avoid, do not encode the folk causal mechanism.**

6. **The management rule that grounds our whole anti-thrash design.** Crash/flick are avoided
   by a **steady decrease in heat for a gradually declining RoR**, explicitly **NOT** throttling
   heat "back and forth to compensate". *(mill47, blog)* → This is independent support for the
   deterministic steady-trim **floor** (#222) and the **direction-flip deadband** (#223/#228):
   the field's own advice is "don't twiddle".

### Tier 3 — weakly supported by this pass, validate empirically before trusting

7. **Turning point / recovery rate as early predictors.** The "TP temp / time-to-TP predicts
   the rest of the roast" intuition did **not** surface a confirmed supporting claim in this
   pass (the predictor angle resolved mostly to DTR + FC-prediction). It may still hold, but
   treat it as **folklore until validated on our own 47-roast `.alog` set** rather than assumed.

8. **~5 s sampling is favourable for RoR.** Larger sampling intervals give smoother RoR than
   sub-second sampling (confirmed but weaker, 2-1). Mild support for our 5 s sample cadence;
   not a strong result.

---

## The entropy question — clear verdict

**Do NOT compute entropy of the temperature curve.** Foundational result: entropy of a
near-constant signal is **dominated by noise** — for a constant series (`hₙ=0`), adding i.i.d.
noise of *any* magnitude drives permutation entropy to its **maximum** value. *(Bandt &
Pompe, primary)* The roast temperature curve is smooth and slowly-varying, so its entropy
measures sensor noise, not roast dynamics. The "PE is robust to noise without preprocessing"
escape hatch was **adversarially killed (1-2)**.

**Where entropy/complexity *does* earn its place: the control signal (heat/fan command
sequence)**, as a twiddle/oscillation measure — exactly the #218 failure mode. The control-
performance literature gives concrete, online-cheap methods:

- **Waveform-shape discrimination:** sticking-actuator oscillation gives an **asymmetric
  saw-tooth** in the control-error signal; an over-aggressive controller gives a **symmetric
  sinusoid**. The *shape*, not the presence of oscillation, is diagnostic — and it's
  separable by a simple **area-ratio metric** (areas before/after each peak), negligible
  compute, online. *(Choudhury et al., primary)*
- **Hurst exponent via DFA / rescaled-range (R/S)** on the control-error series assesses loop
  performance **model-free**, tracking the same trends as the established minimum-variance
  index. *(two primary sources)*
- General: permutation-entropy-family metrics genuinely track dynamic transitions (e.g. onset
  of chaos), capturing **shape/order** dynamics invariant to monotonic transforms (n=3..7).
  *(primary)*

**Operationalisation for #229 (cheapest → richest):** (1) fan/heat **change-count +
reversal-count** over a sliding window (the floor — directly the "fan-change count" already
in D36); (2) **area-ratio asymmetry** of the command series; (3) **Hurst/DFA** on the command
or control-error series if (1)/(2) prove too coarse. All are online-computable from 5 s
command samples.

---

## Artisan reference implementation (verified in source, 15 Jun)

To recover the claims the harness's Artisan fetch dropped, the Artisan source was cloned
(`github.com/artisan-roaster-scope/artisan`) and read directly. This is the canonical
reference implementation and it **corrects two folk descriptions** above with concrete,
liftable algorithms (citations are `file:line` in `src/artisanlib/`):

- **Live RoR is a least-squares slope, NOT a finite difference.** When `polyfitRoRcalc` is
  on, RoR is a **degree-1 `numpy.polyfit` over a configurable span** (default `deltaBTspan`
  20 s / `deltaBTsamples` 6), scaled to °/min; finite difference is only the fallback.
  `canvas.py:4672-4680` (polyfit), `4646-4658` (fallback), config `1689-1690`. Offline it
  uses **Savitzky-Golay** differentiation (`canvas.py:8932-8934`). → Our RoR feature should
  be a span polyfit slope, not `ΔBT/Δt`.
- **Smoothing:** live = **decay-weighted (exponential-recency) average**; offline = Hanning
  convolution on a resampled grid + median spike-filter (offline only). `canvas.py:8434-8464`.
  Confirms the smooth-early / lag tradeoff.
- **FC-ETA = extrapolation, and the exact shape matters.** `updateProjection()`:
  **linear** (current RoR) for the first **5 min** after charge, then **quadratic using the
  RoR *acceleration* (dRoR/dt)** thereafter. Crucially it projects off the **raw, unsmoothed
  RoR** (`unfiltereddelta2_pure`) to avoid the smoothing phase-lag. `canvas.py:6690-6712`
  (linear), `6738-6761` (quadratic accel), `6743` (raw-RoR note). → This is a directly
  liftable FC-ETA algorithm; note the smooth-for-display / project-off-raw split.
- **AUC (area under curve) is a live thermal-dose feature we missed** — trapezoidal
  integration each sample (`main.py:24370-24391`), with a **closed-form time-to-target-AUC**
  prediction by solving a quadratic on (RoR, accel). `main.py:24314-24317`. Worth adding to
  the candidate set as a thermal-dose / development signal.
- **Live turning-point detection exists** (a TP alarm fires when the post-charge minimum is
  found, `canvas.py:4635`; offline `util.py:921-948`). So TP is **live-computable** — but its
  *predictive value* is still the unproven part (Tier 3 above).
- **DTR is computed only after DROP in Artisan** (`main.py:22276-22284`), not displayed live.
  Our live "development-so-far" and live DTR-so-far are still trivially computable
  (`(now-FC)/(now-charge)`); just note Artisan's *canonical* DTR is a post-drop number.

## FC detection from the curve — the Scott Rao ET-RoR-trough method (15 Jun)

A dedicated pass on Scott Rao's curve-based FC method (his blog, primary). **It is a
different question from the FC-ETA above:** the ETA *anticipates* FC; Rao's method *detects
the event as it happens*.

- **The signal:** a **trough (local minimum) in the ET (environment/exhaust) RoR**, with a
  **simultaneous crash in the BT RoR**. Rao marks the ET-RoR trough as the "real" start of
  FC. He explicitly says the raw ET *curve's* arc is too vague — it's the **derivative (ET
  RoR)** that carries the mark. Source: Rao, *"How to Use Cropster To (Almost Always) Know
  Exactly When First Crack Began"* (scottrao.com, 25 Nov 2018). Artisan tracked an
  implementation (issue #309) but closed it without a validated, documented algorithm.
- **Retrospective, NOT predictive.** The trough is only confirmable once the curve turns
  back up, so it is **back-marked** after the fact (the Artisan proposal waits ~X s past a
  ~180 °C BT threshold with no new low, then marks the prior lowest ET-RoR point). **No
  advance warning** — do not confuse it with Cropster's predictive ML FC window.
- **Mechanism is asserted, not evidenced:** moisture/gas release at fracture increases hot-air
  flow past the ET probe (ET RoR up-turn) while escaping moisture cools bean surfaces and
  deflects heat (BT RoR crash). Rao himself: *"I don't claim to completely understand the
  dynamic."* Note this is a **moisture story for FC onset**, distinct from the *exothermic*
  flick later in FC — don't conflate.
- **Reliability:** hedged and unquantified — "**usually** the best indicator", with named
  failure cases (gas reduced just before the trough; erratic/gentle-cracking coffees, some
  decafs/naturals). Credible expert heuristic, **not** a measured study. The forum framing
  ("almost always know exactly") overstates his own qualified wording.
- **Telemetry demands:** needs (a) an **ET probe** and (b) **heavy RoR smoothing + a robust
  trough-finder** — RoR is a derivative, so a trough off raw ~5 s telemetry is dominated by
  noise/quantisation and will throw spurious minima. Rao frames it entirely inside Cropster's
  smoothed RoR.

**Verdict for us:** our rig *does* have an env-temp channel, so the ET-RoR trough is
computable in principle — but its realistic value is a **secondary thermal confirmation to
debounce / cross-check the audio FC mark** (tightening *when FC began*, partially offsetting
the 12–21 s audio lag), **only** with proper ET-RoR smoothing + trough detection. It is **not**
a standalone live detector and gives **no anticipation**. So: **audio stays the FC event
detector, the RoR-extrapolation ETA stays the anticipation tool, and the ET-RoR trough is an
optional Tier-2 cross-check** — worth prototyping on the recorded roasts (we have ET) to see
if it meaningfully sharpens the audio mark, but not load-bearing for D36's anticipatory trim.

## What this changes

- **#223 / D36:** confirmed as-written. The ETA-from-RoR (not hardcoded 180), live DTR,
  distance-from-reference, and the steady-RoR / no-twiddle anti-thrash design all have
  external support.
- **#229:** re-rank — promote control-signal stability + FC-ETA + RoR-curvature to Tier 1;
  **demote TP/recovery-as-predictor to "validate on our own data first"**; treat crash/flick
  as observed RoR events, drop the causal folklore; replace the vague "control-signal entropy"
  with the three concrete methods above.
- **Honest gaps:** TP/recovery predictive value is unproven here; most roasting-practice claims
  are practitioner-blog grade; the harness synthesis + one Artisan-prediction fetch failed
  (claims still captured from other sources).

## Key sources

Primary: Bandt & Pompe, *Permutation Entropy* (researchgate 11364831); Choudhury et al.,
stiction detection (sciencedirect S0959152404001106); Hurst/DFA control-performance
(sciencedirect S240589632200605X, springer s11071-017-3484-3); Birmingham roast-simulation
(RMSE 2.7 °C). Practitioner: Cropster (FC prediction, flick); Scott Rao + artisan-roasterscope
(RoR, smoothing, DTR); Mill47 (rise/crash/flick).
