# Advisor evaluation — experiment design, evolution & results

How RoastPilot's LLM advisor is chosen and tuned, end to end: the design, every
run from the first to the current one, the data, the people and agents that fed
it, and where it landed. The advisor is **advisory-only** (a deterministic safety
policy validates/clamps/rejects every recommendation; the controller owns the
loop) — so "choosing the advisor" means choosing the **model** and the **prompt**
that give the best advice, measured against real roasts.

> **Read first — what the numbers mean.** Ground truth is a *known-good* roast,
> NOT a provably optimal one. Every metric measures **agreement with what the
> human did**, not absolute correctness. Drop F1 = 1.0 means *matched this roast's
> drop*, not *correct*. The scores are a quantitative **aid** to the operator's
> judgement (advice samples + the latency gate + the controller's ≤196 °C
> ceiling), never a replacement.

## Where it landed (current state)

- **Model — `google/gemini-3.1-flash-lite`** (D33, merged). On 28 real roasts it
  is the only model that reliably calls the flavor-critical drop; the frontier
  and slow models over-hold (never drop = past the bitter ceiling).
- **Prompt — `v4`** (profile-anchored drop), recommended (D34, pending operator
  go). Closes a drop-recall gap in `v2`: recall 0.68 → 1.0, F1 0.66 → 0.88,
  precision up, anticipatory heat cut held. Generalizes on held-out roasts
  (19/19) and, given a correct target, lands over-dark roasts back in the
  [193, 196] band (Phase 5). Re-pin awaiting operator go.

---

## 1. The design

Three principles, fixed from the first run:

1. **Ground the eval in real roasts, not vibes.** Each candidate is replayed over
   *actual recorded roasts* tick-by-tick — never synthetic prompts or a generic
   benchmark. The roast you already trust is the benchmark.
2. **Filter on the hard constraint before the soft one.** Latency is a wall (the
   controller acts on a 1 s tick; advice that arrives in 15 s is useless). Quality
   is a judgment. Wall first, judgment second.
3. **The human judges the domain; the harness measures the rest.** A roaster reads
   the advice; the harness measures latency, cost, and the scored metrics. The
   operator's taste is the final authority the numbers serve.

**What's scored** (`scripts/bakeoff_replay.py`), per (model, prompt, roast):

- **Drop decision** — binary classification over ticks vs the real drop:
  precision / recall / **F1** + drop-timing error (s and °C). The flavor-critical
  call.
- **Heat / fan** — MAE + **directional agreement** (did the model move the lever
  the way the human did — especially the anticipatory pre-first-crack cut).
- **Latency** — median per phase against the tick budget (tightest at FC).
- **Cost** — $ per advisor call; a roast-economics check and, as Phase 0 found, a
  proxy for the latency gate (slow models spend reasoning tokens). See *Cost* below.

## 2. Who and what fed it

**The domain expert (operator).** A roaster of this exact machine supplies the
ground truth the eval can't derive: the empirical profile (first crack ~178 °C,
drop low-190s, DTR ~15 %), the **≤196 °C indicated bitter ceiling** (phenylindane
onset, given the ~20 °C probe offset), the rule that **DTR is an indicator not a
dial**, and the **success criteria** (e.g. for the held-out over-dark roasts: a
recommended drop is a win if it lands *lower than the actual drop and ≥ 193 °C*).
He also makes the calls the data can't: which model to pin, when to re-pin.

**The research agents.** The prompt's domain content was grounded by a
**four-source triangulation** that cross-validated: (1) cross-repo retrieval over
the plan + roaster + FC-detector repos; (2) the real 7-Jun Warp roast logs; (3)
web research on roast chemistry (chlorogenic-acid → phenylindane, two-sided
bitterness, thermal/detector lag); (4) the operator's own numbers. The prompts
reason from **live context + the per-roast profile**, never hardcoded textbook
temperatures, because the probe offset is roaster-specific.

**The data.**

| set | source | size | role |
|---|---|---|---|
| 7-Jun Warp roasts | live Hottop captures (`tests/fixtures/live-roast-2026-06-07`) | 2 | first replay set; profile validation |
| Artisan good | operator's `.alog` history, drop < 198 °C | 28 | model + prompt bake-off |
| Artisan over-dark | operator's `.alog` history, drop ≥ 198 °C | 19 | **held-out** (unseen by the prompt work) |

The Artisan logs are converted by [`scripts/alog_to_fixture.py`](../../scripts/alog_to_fixture.py)
(decodes the BT/ET curve, the charge/FC/drop marks, and the manual heat/fan
control track). The roast logs are personal and are **not** committed — only the
anonymized scorecards are.

## 3. The evolution (every run)

### Phase 0 — the original bake-off (E8-S4, 7–8 Jun; D20/D21/D22)

Latency + cost + reasoning, judged on advice quality. The prompt evolved
`v0` (generic) → `v1` (electric drum: thermal lag, act early) → `v2` (fan as a
coupled heat-transfer lever, development = *duration* not a temperature).
Findings: the **latency gate is also a cost filter** (the over-gate models are the
reasoning-token spenders); "best ≠ priciest"; reasoning knobs aren't portable
(disabling it 400s on some endpoints). **Pinned: `claude-opus-4.8` + `v2`** — the
only frontier model passing the latency gate on advice quality. Raw:
[`../advisor-bakeoff-2026-06-08.md`](../advisor-bakeoff-2026-06-08.md).
*At this point the conclusion was "the prompt is the variable; the model never
changed."* That held only while the test set was tiny.

### Phase 1 — the replay harness, first run (2 roasts) — the mirage

The replay harness added the F1/timing/MAE/directional metrics and ran 6 models ×
v2/v3 over the **2** 7-Jun roasts. `gemini-3.1-flash-lite` read a perfect drop
**F1 = 1.0**; every other model 0.0. v3 did not beat v2.

| model (v2, N=2) | drop F1 | recall |
|---|---|---|
| google/gemini-3.1-flash-lite | 1.00 | 1.00 |
| every other candidate | 0.00 | 0.00 |

A perfect score on two roasts is a **warning, not a win** — one drop instance each
makes F1 coarse. Raw: [`bakeoff-results-2026-06-14.{md,json}`](bakeoff-results-2026-06-14.md).

### Phase 2 — model selection on 28 real roasts (D33)

Rebuilt the test set from the operator's `.alog` history (drop < 198 °C → 28
roasts) and re-ran the model bake-off on v2; opus on a 3-roast spot-check.

| model | drop F1 | precision | recall | drop called | timing (s) | heat-dir | latency |
|---|---|---|---|---|---|---|---|
| **google/gemini-3.1-flash-lite** | **0.63** | 0.62 | 0.64 | **18/28** | −0.1 | **0.88** | **1.2 s** |
| openai/gpt-4.1-mini | 0.07 | 0.07 | 0.07 | 2/28 | 0.0 | 0.55 | 1.8 s |
| meta-llama/llama-3.3-70b | 0.07 | 0.07 | 0.07 | 2/28 | 0.0 | 0.75 | 2.0 s |
| openai/gpt-5.4-nano | 0.00 | 0.00 | 0.00 | 0/28 | — | 0.34 | 1.4 s |
| anthropic/claude-opus-4.8 *(3-roast spot)* | 0.00 | 0.00 | 0.00 | 0/3 | — | 0.35 | 5.8 s |

The mirage corrected: gemini's F1 fell 1.0 → **0.63**, and **the model flipped** —
gemini was the *only* model that reliably called the drop; opus and every
frontier/slow candidate **over-hold** (never drop = past the bitter ceiling, the
dangerous direction). The cheap flash model also costs a fraction and is 4× faster.
**Pinned: `gemini-3.1-flash-lite` + v2 (D33).** Raw:
[`bakeoff-artisan-summary.md`](bakeoff-artisan-summary.md). Cost ~$2.29.

### Phase 3 — prompt optimization (#194)

gemini's 0.63 came from missing the drop on ~10/28 roasts, always *later* than the
operator — because v2 told it "`target_drop_temp_c` is a guide, develop modestly
past it, don't rush the drop." Five new variants (v4–v8), each a distinct
drop-decision strategy grounded in §2's research. Pinned model, same 28 roasts.

| prompt | drop F1 | precision | recall | drop called | timing (s/°C) | DTR @ drop | heat-dir |
|---|---|---|---|---|---|---|---|
| v2 (baseline) | 0.655 | 0.643 | 0.68 | 19/28 | −0.5 / −0.07 | 18.8 % | 0.887 |
| **v4 — profile-anchor** ✅ | **0.881** | **0.821** | **1.00** | **28/28** | −2.4 / −0.27 | 17.3 % | 0.851 |
| v5 — DTR-target | 0.881 | 0.821 | 1.00 | 28/28 | −2.8 / −0.32 | 17.2 % | 0.826 |
| v8 — concise synthesis | 0.786 | 0.679 | 1.00 | 28/28 | −7.4 / −1.1 | 16.7 % | 0.911 |
| v6 — two-sided window | 0.744 | 0.631 | 1.00 | 28/28 | −13.5 / −2.1 | 15.9 % | 0.902 |
| v7 — lag-anticipation | 0.620 | 0.467 | 1.00 | 28/28 | −33.0 / −5.2 | 13.2 % | 0.818 |

On the good roasts v4 drops at **DTR 17.3 %** *and* at the right temp (−0.27 °C vs
the human) — the dev target met at a low drop, the good-roast signature. The DTR @
drop column also exposes the over-correction directly: v7's −33 s early drops land
at **13.2 % DTR** — under-developed, not just imprecise.

**v4 wins**, stated precisely (paired tests over the 28 roasts):
- **Recall win is statistically robust** — v4 calls the drop on 9 roasts v2 missed
  and misses none v2 caught → exact **McNemar p = 0.0039**.
- **F1 gain is real but modest** — per-roast better on 9, worse on 3, tied on 16
  (mean Δ +0.23). The 3 regressions are one-tick-early FPs on roasts v2 already
  nailed. Magnitude-weighted (Wilcoxon, W=6) significant; sign test borderline
  (p=0.15). Not a clean sweep.
- **Mean precision rises** (0.64 → 0.82) because v4 converts v2's *misses* into
  catches; the anticipatory heat cut holds (0.85).

Net: v4 trades **+9 caught drops** (the safety-relevant direction) for **3
one-tick near-matches**. The variants that pushed *earlier* (v6, v7) over-corrected
into chronic early drops (v7 −33 s, precision 0.47, *worse than v2*) — the failure
mode the precision column was built to catch. Raw:
[`bakeoff-results-prompts-2026-06-14.{md,json}`](bakeoff-results-prompts-2026-06-14.md). Cost ~$1.86.

### Phase 4 — held-out validation (the train-on-test mitigation)

v4–v8 were authored from the operator's profile (the aggregate of the same 28
roasts) — a population-level train-on-test risk. Validation on the **19 UNSEEN
over-dark roasts** (drop ≥ 198 °C) the prompt work never touched.

| prompt | recall (drop called) | wins [193, < actual] | mean rec. drop | DTR @ drop | ≤196 °C |
|---|---|---|---|---|---|
| v2 | 11/19 | 2/12 | 199.7 °C | 14.3 % | 0 |
| **v4** | **19/19** | **11/19** | 197.8 °C | 13.5 % | 4 |
| v5 | 19/19 | 9/19 | 198.3 °C | 13.8 % | 2 |

> **DTR @ drop is the coupling check** (operator: dev-time % only helps if you
> *slowed the roast enough* to hit it while the drop temp is still low — hitting
> DTR at a high temp is a bad roast). Here v4 drops cooler than the human (good)
> but at **13.5 % DTR vs the human's ~14 %+** — slightly *under*-developed. On a
> roast that ran hot there is no drop tick that hits both a low temp *and* a full
> DTR: these over-dark roasts were a **heat-management failure** (should have been
> slowed earlier), not a drop-timing one, and a replay can't slow the trajectory.
> The advisor's drop logic does the honest best on a fixed bad curve.

- **Generalization: clean.** v4 calls the drop on **19/19** unseen roasts; v2 stays
  at 11/19 (the over-hold gap persists on unseen data). v4's recall fix is **not** a
  train-on-test artifact.
- **Caught the over-roast (operator criterion): partial, far better than v2.** v4
  pulled the drop *lower than the over-dark actual, ≥ 193 °C* on **11/19** (v2:
  2/12). But fed the *actual ~200 °C target*, it only pulls to a mean **197.8 °C**
  (4/19 ≤196) — right direction, doesn't fully enforce the ceiling under a too-high
  target. → the addendum. Raw: [`bakeoff-holdout-2026-06-14.{md,json}`](bakeoff-holdout-2026-06-14.md).

### Phase 5 — target-sensitivity addendum (a "what-if", complete)

A counterfactual (vary one input, hold model + prompt fixed — *not* an ablation):
replay the same 19 over-dark roasts feeding the operator's **intended** target
(195 °C / 15 % DTR) instead of the actual over-dark drop, isolating how much the
recommended drop follows the target vs the prompt's own ≤196 ceiling. Runner:
[`bakeoff-holdout-addendum.py`](bakeoff-holdout-addendum.py); scorecard
`bakeoff-holdout-addendum-2026-06-14.json`. Cost ~$0.69.

| prompt | rec. drop @ 200 | rec. drop @ 195 | DTR @ drop (195) | in [193, 196] | recall |
|---|---|---|---|---|---|
| v2 | 199.7 °C | 197.4 °C | 16.6 % (n=6) | 1/19 | **5/19** |
| **v4** | 197.8 °C | **196.1 °C** | 12.3 % | **11/19** | 19/19 |
| v5 | 198.3 °C | 195.8 °C | 12.2 % | 12/19 | 19/19 |

Note the DTR @ drop falls to ~12 % with the 195 target — dropping cooler on a
hot-running roast buys a lower temp at the cost of development, confirming the
coupling: the only way to get *both* on these roasts is to slow the roast earlier
(a heat-management fix upstream of the drop, which the replay holds fixed).

**Conclusion: Phase 4's "only pulls to 197.8 °C" was the over-dark target's doing.**
Given a *correct* 195 °C target, v4 lands the over-dark roasts right in the band —
mean **196.1 °C**, **11/19** fully in [193, 196] — while still calling the drop on
19/19. So v4 both *follows a sane target* and *enforces its ceiling* against a
too-high one (Phase 4). Two corroborating notes: v5 edges v4 here (12/19 in band)
— a near-tie that doesn't overturn v4's win on the training-set discriminators
(heat-direction, timing); and **v2's recall collapses to 5/19** with the lower
target — it over-holds *worse* when told to aim cooler, the exact pathology v4 was
written to fix.

### Cost

Cost is an eval dimension, not an afterthought — in Phase 0 the latency gate
turned out to *also* be a cost filter (the slow models were the reasoning-token
spenders). The pinned model is the cheap one, which is part of why it wins.

**Per-call (realized averages):**

| model | $ / advisor call | source |
|---|---|---|
| **google/gemini-3.1-flash-lite** | **$0.00045** | measured — Phase 3: $1.86 ÷ 4,098 calls |
| anthropic/claude-opus-4.8 | ~$0.019 (≈ **42×**) | measured — D22 cost pass |

At the eval's 30 s cadence a full roast is ~25 advisor calls ⇒ **≈ $0.011 per
roast** with the pinned model; in production it is less, because the advisor is
not called every tick (D32 cadence: preheat off, drying change-based). A whole
real roast costs about a cent in advice.

**Per run (OpenRouter spend):**

| phase | run | calls | cost |
|---|---|---|---|
| 2 | model bake-off (5 models × 28, incl. opus) | 2,801 | **$2.29** (measured) |
| 3 | prompt sweep (gemini × 6 prompts × 28) | 4,098 | **$1.86** (measured) |
| 4 | held-out (gemini × 3 × 19) | 1,545 | ≈ $0.70 (calls × per-call) |
| 5 | addendum (gemini × 3 × 19) | 1,545 | ≈ $0.69 (measured) |

Phase 2 is the priciest despite fewer calls than Phase 3 — opus's 84 spot-check
calls cost more than the other 2,717 combined. The Artisan campaign (Phases 2–5)
cost **≈ $5.5** total.

## 4. What we learned

- **Ground the eval in real runs**, score each output with its own metric, **gate
  on the wall (latency) before the judgment (quality)**, keep the human on the
  domain call.
- **A perfect score on a tiny set is a warning, not a win** — N=2 read F1 = 1.0;
  N=28 read 0.63 and *flipped the model* (opus → gemini).
- **When you fix one metric, instrument the opposite.** The recall fix's danger was
  precision; the eval was built to see it (v6/v7 over-corrected).
- **Prefer paired/statistical comparison to eyeballing means** — the recall win is
  significant (McNemar); the F1 win is real but modest.
- **Name your train-on-test, then pay it down with held-out data.** The clean
  future fix is to hold out the operator's *next* real roasts as a true test set.

## 5. Honest caveats / limitations

- **Agreement ≠ correctness.** F1 measures match-to-this-human-roast.
- **Coarse drop label** — 30 s cadence, one drop-positive tick per roast.
- **Single trial at temperature 0** — LLM nondeterminism unmeasured (a few repeats
  would bound it).
- **Held-out is over-dark only** — no unseen *good* roasts exist (all 28 informed
  the prompts), so Phase 4 validates generalization + ceiling logic, not good-drop
  agreement on unseen good data.

## 6. Decisions

- **D20/D21/D22** — original pin: `opus-4.8` + `v2`; latency/cost/reasoning findings.
- **D33** — model re-pinned to `gemini-3.1-flash-lite` on the 28-roast Artisan run. *Merged.*
- **D34** — recommended prompt re-pin to **v4**. *Pending operator go + Phase 5.*

## 7. Reproduce

```bash
python scripts/alog_to_fixture.py "<roasting-logs-dir>" --out-dir .artisan-fixtures   # drop<198 = 28-roast set
OPENROUTER_API_KEY=sk-or-... python docs/advisor/bakeoff-run-artisan.py     # model selection (Phase 2)
OPENROUTER_API_KEY=sk-or-... python docs/advisor/bakeoff-run-prompts.py     # prompt sweep   (Phase 3)
OPENROUTER_API_KEY=sk-or-... python docs/advisor/bakeoff-holdout-prompts.py # held-out       (Phase 4)
```

The replay + scoring + report layer is fully testable without a key via a fake
advisor; only the real-candidate runs need `OPENROUTER_API_KEY`.
