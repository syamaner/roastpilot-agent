# Curve-feature validation on our recorded roasts (18 Jun 2026)

**Issue:** #229 (D36 curve-insight spike). **Pure data analysis — no LLM / API
calls, no key.** Tells **#275** (the context builder) which derived features are
worth feeding the control loop and which stay display-only.

**Dataset:** the operator's consolidated `.artisan-fixtures/artisan-*` set,
**N = 28** roasts (gitignored; read-only). Each roast is `roast.jsonl` of ~1 s
telemetry (bean °C, env °C, heat %, fan %) plus three events — `beans_added`,
`first_crack_detected`, `beans_dropped`. Temperatures are Celsius throughout.

**Method:** `scripts/curve_feature_eval.py` (deterministic, numpy-only). Every
feature is computed on a **5 s-decimated** view of each roast — what the live
loop would actually see — with the 1 s view kept only as a noise-floor
reference. RoR is a degree-1 least-squares slope over a trailing span (Artisan's
live-RoR method), 30 s for RoR and 60 s for RoR-of-RoR (curvature). Regenerate
with:

```
.venv/bin/python scripts/curve_feature_eval.py --fixtures-dir .artisan-fixtures
```

**Note on `summary.json`:** its `first_crack_temp_c` field is unreliable in
these fixtures (often the ambient/initial reading, or `-1`), so the FC bean
temperature is read from the telemetry **at the FC event sample**, not from the
summary. Across the set the FC-event bean temp is a clean 169–181 °C, matching
the operator profile (FC ~170–180 °C) — so the derived FC band used as the ETA
target is **171–180 °C (midpoint 175.9 °C)**.

---

## TL;DR verdict table

| Feature | Discriminates / predicts on our 28? | Verdict for #275 |
| --- | --- | --- |
| **FC-ETA** (RoR extrapolation to FC band) | Yes — useful (≤30 s) ETA from ~89 s median lead; |error| 12–20 s in the last ~2 min | **KEEP** as a control input (anticipation trigger), with the lead/error envelope below |
| **RoR crash** (negative curvature at FC) | **No** — 0/28 detectable; signal buried below the curvature noise floor on 5 s telemetry (median SNR 0.43) | **DROP** as a control input; the crash signature is not present in our data at our cadence |
| **RoR flick** (post-FC RoR rebound) | Weak — 16/28, rebound only ~2.2 °C/min | **DISPLAY-ONLY / inconclusive** — too small a signal to gate control on |
| **TP temp → roast outcome** | Raw r strong (−0.84 to total time) **but a charge-temperature artefact** (see confound) | **DROP as a predictor** — it is essentially the charge temp the operator already set |
| **time-to-TP → outcome** | Raw r moderate, **collapses to ~0 once charge temp is controlled** | **DROP as a predictor** |
| **recovery slope → outcome** | Moderate **and survives the charge-temp control** (partial r −0.46 to −0.52) | **KEEP (cautiously, N=28)** — the one TP-family metric carrying genuine independent signal |

---

## Feature 1 — FC-ETA (extrapolate RoR to the FC band)

At each pre-FC tick we take the low-lag bean RoR (and its acceleration), project
bean temp to the FC-band midpoint (linear for the first 5 min after charge,
quadratic with the RoR acceleration thereafter — Artisan's `updateProjection`
shape), and compare the predicted FC time with the actual `first_crack_detected`
event. "Useful" = predicted FC within **±30 s** of actual (chosen to bracket the
12–21 s audio-detector lag the ETA is meant to anticipate / absorb).

**Error by lead time** (how far before FC the tick is):

| Lead before FC | median \|error\| | p90 \|error\| | median signed err (+ = late) | n roasts |
| --- | --- | --- | --- | --- |
| 180 s | 27.6 s | 60.7 s | +10.7 s | 25 |
| 120 s | 20.5 s | 50.9 s | +1.5 s | 28 |
| 90 s | 17.5 s | 42.7 s | −1.1 s | 28 |
| 60 s | 15.7 s | 34.5 s | −6.5 s | 28 |
| 30 s | 12.5 s | 28.9 s | −8.4 s | 28 |

**Earliest contiguous-to-FC useful lead:** median **89 s** before FC — i.e. on
the median roast the ETA settles to within ±30 s and stays there from ~1.5 min
out. (Cropster's commercial FC window opens ~3 min out; our naive extrapolation
is usefully tighter later, looser earlier, as expected from a pure-RoR projection
with no learned model.)

**Reading the numbers:** error shrinks monotonically as FC approaches
(27.6 → 12.5 s median), the natural behaviour of an extrapolation. There is a
small **late bias far out** (+10.7 s at 180 s) flipping to a small **early bias
close in** (−8.4 s at 30 s): far from FC the RoR is still high so the projection
overshoots the time; close in the RoR is already rolling off (beans approaching
FC), so a linear/quadratic projection lands slightly early. Neither bias is large
relative to the detector lag.

**Verdict: KEEP.** The ETA is genuinely informative as the **anticipation
trigger** — within roughly half the detector-lag window by ~90 s out, tighter
thereafter. For #275: feed it as a scalar with an explicit confidence/lead
caveat (treat ETAs at >2 min lead as soft, ≤90 s as firm), and remember the ETA
is a *trigger to start anticipating*, **not** a fan/heat move on its own (the
deterministic pre-FC floor still owns the levers — cf. the 16 Jun negative case,
`pre-fc-fan-into-crack.md`).

---

## Feature 2 — RoR curvature (crash / flick)

Curvature = the derivative of bean RoR (RoR-of-RoR, °C/min/min). The **crash**
folklore signature is a sharp negative curvature dip around FC; the **flick** is
an RoR rebound in early development. Detection thresholds are set relative to each
roast's *own* pre-FC noise (curvature std over the near-linear maillard window,
charge+120 s … FC−120 s), so the test is scale-free.

| Metric | Value |
| --- | --- |
| Crash detected (curvature dip < −3× the roast's own noise floor) | **0 / 28** |
| Flick detected (RoR rebound > 2 °C/min, FC → FC+150 s) | 16 / 28 |
| Median most-negative curvature in FC ±60 s | −2.9 °C/min/min |
| Median post-FC RoR rebound | 2.16 °C/min |
| Curvature noise floor (1 s view / 5 s view) | 6.78 / 7.16 °C/min/min |
| Median signal-to-noise of the FC-window curvature (5 s) | **0.43** |

**Reading the numbers:** the crash is **not detectable** in our data at our
sampling. The most-negative curvature near FC (median −2.9) is roughly *half* the
curvature noise floor (~7), giving a median SNR of 0.43 — the "crash" is smaller
than the sample-to-sample jitter of the second derivative. The noise floor is
essentially the same at 1 s and 5 s (6.78 vs 7.16), so this is **not** a
decimation artefact we could fix by sampling faster — the second derivative of a
±1 °C-quantised thermocouple is just noise-dominated on a Hottop. Looking at the
raw RoR through FC confirms it: bean RoR *gently rolls off* (e.g. 12 → 10 → 7
°C/min across FC ±60 s), it does not crash.

The flick is *weakly* present (16/28) but the rebound is small (~2.2 °C/min,
barely above the 2 °C/min detection bar), so even where flagged it is marginal.

**Verdict: crash = DROP** (not a usable control input on our rig/cadence — the
research note's Tier-2 "trust the signal not the folklore" caveat bites here: the
signal itself is absent). **Flick = DISPLAY-ONLY / inconclusive** — too small and
too inconsistent (16/28) to gate any control move on; revisit only if a heavily
smoothed RoR on a longer record changes the picture. This matches the research
note's warning that crash/flick are *RoR-shape events to detect when present*,
and on our data the crash simply is not present above noise.

---

## Feature 3 — turning point + recovery

TP = post-charge bean-temp minimum (temp + time-to-TP); recovery slope = bean RoR
over the 60 s after TP. We correlate each against three downstream outcomes
(charge→FC time, total roast time, drop bean temp), Pearson + Spearman.

**Ranges across the set:** TP temp 56–91 °C, time-to-TP 47–65 s, recovery slope
10.8–17.0 °C/min.

| Predictor | Outcome | Pearson r | Spearman ρ | ~p | n |
| --- | --- | --- | --- | --- | --- |
| TP temp | time-to-FC | −0.696 | −0.711 | <0.001 | 28 |
| TP temp | total roast time | **−0.842** | −0.804 | <0.001 | 28 |
| TP temp | drop temp | −0.303 | −0.409 | 0.104 | 28 |
| time-to-TP | time-to-FC | −0.409 | −0.361 | 0.022 | 28 |
| time-to-TP | total roast time | −0.480 | −0.454 | 0.005 | 28 |
| time-to-TP | drop temp | −0.195 | −0.298 | 0.310 | 28 |
| recovery slope | time-to-FC | −0.605 | −0.554 | <0.001 | 28 |
| recovery slope | total roast time | −0.630 | −0.620 | <0.001 | 28 |
| recovery slope | drop temp | −0.046 | −0.034 | 0.815 | 28 |

At face value TP temp looks like a strong whole-roast predictor (r = −0.84 to
total time). **It is not — it is a charge-temperature artefact.** The confound
check (run separately, reproducible from the loader):

- **corr(charge bean temp, TP temp) = 0.979.** The TP temp is essentially the
  charge temperature minus a near-constant dip — it carries almost no information
  beyond what the operator already set when charging.
- **corr(charge temp, total roast time) = −0.832** on its own — the charge temp
  *is* the predictor.
- The charge temps are **bimodal** in this set: ~19 roasts charged at ~100–136 °C
  and ~9 at ~173–192 °C. That split alone drives much of the raw correlation.
- **Partial correlations, controlling for charge temp:**
  - TP temp → total time: **−0.842 collapses to −0.246**.
  - time-to-TP → total time: −0.480 collapses to **+0.12**.
  - recovery slope → total time: −0.630 **holds at −0.522**; → time-to-FC:
    **−0.461**.

So once you remove the charge-temperature the operator already knows, **TP temp
and time-to-TP carry essentially no independent predictive signal**, while the
**recovery slope retains a real, moderate, charge-independent relationship** with
both FC timing and total roast time (faster recovery → shorter roast — physically
sensible: a hotter/faster drum gets to FC sooner).

**Verdicts:**

- **TP temp as a predictor → DROP.** It is a proxy for charge temp (r = 0.979);
  feeding it adds folklore-grade input with no information the controller doesn't
  already have from the charge reading. This directly answers the open Tier-3
  "validate TP before trusting it" question: **on our data it does not earn its
  place as a control predictor.**
- **time-to-TP as a predictor → DROP.** Collapses to ~0 once charge temp is
  controlled.
- **recovery slope → KEEP, cautiously.** It is the one TP-family metric with a
  genuine charge-independent correlation (partial r ≈ −0.46 to −0.52 vs FC timing
  / total time). Worth feeding #275 as an *early whole-roast pace* scalar — but
  see the N caveat.

---

## The N = 28 caveat — read this before trusting any "KEEP"

**28 roasts is small, and the set is not a clean experiment.** Specific limits:

- **Bimodal charge temps** (a ~100–136 °C cluster and a ~173–192 °C cluster) mean
  several correlations are partly *between-cluster* effects, not smooth
  within-roast relationships. A two-cluster dataset can manufacture a high r from
  what is really a two-point comparison.
- The **partial-correlation** confound control is the right tool but is itself
  noisy at N=28; the recovery-slope partial r (≈−0.5) is "moderate and probably
  real" not "established". The indicative p-values in the table use a normal-tail
  approximation with no multiple-comparison correction — treat them as a sanity
  flag, not a test. With 9 correlations examined, expect ~0.5 false positives at
  p<0.05 by chance.
- These are **Artisan replays of human roasts**, so heat/fan reflect the
  operator's manual control, not the deterministic D35 floor — recovery slope and
  FC timing are entangled with how *this operator* drove each roast. The
  relationships may not transfer to autonomous control.
- The **FC mark is the audio detector's** (12–21 s lag), so the ETA error
  distribution already folds in detector lag; the ETA is being validated against a
  lagged ground truth, which is the right target for *control* (it must hit the
  detector's mark) but not the true acoustic FC.

**Net:** FC-ETA and recovery-slope are the two features that survive scrutiny on
our data; crash, flick, TP temp, and time-to-TP do not. Even the survivors are
**N=28, single-operator, human-driven, bimodal** — promote them to control input
behind a confidence caveat, keep validating as roasts accumulate, and do **not**
treat any of these as established beyond this set.

---

## Handoff to #275 (context builder)

- **Feed as control input:** FC-ETA (with lead/confidence envelope above) and
  recovery slope (as an early pace scalar, caveated).
- **Display-only:** flick (marginal), and the live TP temp / time-to-TP (useful
  to *show*, but not as predictors — they are charge-temp proxies).
- **Do not compute as a control signal:** RoR crash — absent above noise on our
  rig at 5 s.
- Unchanged from the research note: the **control-signal stability / entropy**
  feature (#229 item 5) is the anti-thrash metric and is validated on the
  *command* series, not the temperature curve; it is out of scope for this
  curve-feature pass and is not re-litigated here.

## Reproduce

```
.venv/bin/python scripts/curve_feature_eval.py --fixtures-dir .artisan-fixtures        # report
.venv/bin/python scripts/curve_feature_eval.py --fixtures-dir .artisan-fixtures --json # machine-readable
```

Confound partials (charge-temp control) are computed inline in the analysis
session; the loader (`curve_feature_eval.load_all`) exposes everything needed to
re-derive them.
