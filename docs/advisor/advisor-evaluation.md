# Advisor evaluation — how the model and prompt are chosen

This is the technical record of how RoastPilot's LLM advisor is selected and
tuned: the method, the data, and the results. It is not vibes — every choice is
scored by replaying **real Hottop roasts** tick-by-tick and measuring the advice
against what the roast actually did.

> **Read first — what these numbers mean.** Ground truth is a *known-good* roast,
> NOT a provably optimal one. Every metric measures **agreement with what the
> human did**, not absolute correctness. Drop F1 = 1.0 means *matched this
> roast's drop*, not *correct*. A model that wanted a slightly different drop than
> the human can still roast well. These scores are a quantitative **aid** to the
> operator's judgement (advice samples + the latency gate + the controller's own
> ≤196 °C ceiling), never a replacement.

## Method

- **Test set.** Known-good Hottop roasts replayed at the 30 s advisory cadence.
  The current set is the operator's annotated Artisan `.alog` history, converted
  by [`scripts/alog_to_fixture.py`](../../scripts/alog_to_fixture.py) (decodes
  the BT/ET curve, the charge/first-crack/drop marks, and the manual heat/fan
  control track) and quality-filtered to drop **< 198 °C** (the operator's
  bitterness ceiling) → **28 roasts**. The roast logs are personal and are **not**
  committed; only the anonymized scorecards are.
- **What's scored** (`scripts/bakeoff_replay.py`):
  - **Drop decision** — binary classification over ticks vs the real drop:
    precision / recall / **F1**, plus the drop-timing error in seconds and °C.
    This is the flavor-critical call.
  - **Heat / fan** — MAE + **directional agreement** (did the model move the
    lever the way the human did, especially the anticipatory pre-first-crack cut).
  - **Latency** — median per phase against the tick budget (tightest at FC).
- **Two phases.** First fix the *model* (latency is a hard wall; quality is the
  judgment), then tune the *prompt* on the pinned model. A third, held-out run
  validates the winner on roasts the tuning never saw.

## Phase 1 — Model selection (D33, prompt v2)

Roster of latency-viable candidates (the ≤3 s FC-viable cheap set) over all 28
roasts; the prior incumbent `claude-opus-4.8` on a 3-roast spot-check. Full
scorecard: [`bakeoff-artisan-summary.md`](bakeoff-artisan-summary.md) (+ raw
`bakeoff-results-artisan-2026-06-14.{md,json}`).

| model | drop F1 | precision | recall | drop called | timing (s) | heat-dir | latency |
|---|---|---|---|---|---|---|---|
| **google/gemini-3.1-flash-lite** | **0.63** | 0.62 | 0.64 | **18/28** | −0.1 | **0.88** | **1.2 s** |
| openai/gpt-4.1-mini | 0.07 | 0.07 | 0.07 | 2/28 | 0.0 | 0.55 | 1.8 s |
| meta-llama/llama-3.3-70b | 0.07 | 0.07 | 0.07 | 2/28 | 0.0 | 0.75 | 2.0 s |
| openai/gpt-5.4-nano | 0.00 | 0.00 | 0.00 | 0/28 | — | 0.34 | 1.4 s |
| anthropic/claude-opus-4.8 *(3-roast spot)* | 0.00 | 0.00 | 0.00 | 0/3 | — | 0.35 | 5.8 s |

**Conclusion:** `gemini-3.1-flash-lite` is the **only model that reliably makes
the drop call** at all. Every other candidate — including the frontier
`opus-4.8` — **over-holds** (never recommends the drop = sails past the ≤196 °C
bitter ceiling, the dangerous, irreversible direction). The cheap flash model is
also the fastest. An earlier 2-roast run had read a misleading drop F1 = 1.0 for
gemini; the 28-roast set corrected that to a robust **0.63** and surfaced a real
**recall gap** — gemini misses the drop on ~10/28, leaning *later* than the
operator. → Phase 2. **Pinned: `gemini-3.1-flash-lite` + v2 (D33).**

## Phase 2 — Prompt tuning (#194, pinned model gemini-3.1-flash-lite)

The recall gap traced to the v2 prompt itself: it told the model "`target_drop_temp_c`
is a guide, develop modestly *past* it, don't rush the drop" — exactly what makes
it hold past the operator's cooler/shorter drops. Five new variants (v4–v8), each
a distinct drop-decision strategy grounded in the domain research (reason from the
**per-roast profile + live indicated temp**, not hardcoded numbers; ≤196 °C
indicated bitter ceiling; FC-detector lag makes the development clock a *lower
bound*; keep v2's anticipatory heat cut). Baseline v2 + v4–v8 over all 28 roasts.
Full scorecard: [`bakeoff-results-prompts-2026-06-14.{md,json}`](bakeoff-results-prompts-2026-06-14.md).

| prompt | drop F1 | precision | recall | drop called | timing (s/°C) | heat-dir |
|---|---|---|---|---|---|---|
| v2 (baseline) | 0.655 | 0.643 | 0.68 | 19/28 | −0.5 / −0.07 | 0.887 |
| **v4 ✅ winner** | **0.881** | **0.821** | **1.00** | **28/28** | −2.4 / −0.27 | 0.851 |
| v5 (runner-up) | 0.881 | 0.821 | 1.00 | 28/28 | −2.8 / −0.32 | 0.826 |
| v8 | 0.786 | 0.679 | 1.00 | 28/28 | −7.4 / −1.1 | 0.911 |
| v6 | 0.744 | 0.631 | 1.00 | 28/28 | −13.5 / −2.1 | 0.902 |
| v7 | 0.620 | 0.467 | 1.00 | 28/28 | −33.0 / −5.2 | 0.818 |

Strategies: **v4** anchors the drop to the profile's `target_drop_temp_c` + a
development floor; **v5** uses the profile's `target_development_percent` as the
indicator; **v6** is a full two-sided window (floor / ≤196 ceiling / post-FC flick
guard), floor-biased; **v7** is lag-aware (the clock under-reports development);
**v8** is a concise synthesis.

**Conclusion:** **v4 wins and cleanly beats v2** — recall 0.68 → **1.0** (zero
misses across 28), F1 0.655 → **0.881**, precision *up* 0.643 → 0.821, with the
anticipatory heat cut essentially held (0.851 vs 0.887). v4's only cost is calling
the drop ~2.4 s early on 10/28 roasts — all **exactly one tick** (worst −25 s /
−2 °C), near-matches, never under-developed. The variants that pushed *earlier*
(**v6, v7**) over-corrected into chronic early drops (v7 −33 s, precision 0.47,
*worse than v2*) — confirming the failure mode the eval was watching for.
**Recommended pin: v4 (D34 — pending operator go + held-out validation below).**

## Phase 3 — Held-out validation (running)

> **Status: in progress.** Conclusion pending — this section is filled when the
> run completes.

The v4–v8 prompts were authored from the operator's profile, which is the
aggregate of the same 28 roasts — a population-level train-on-test risk. This run
validates the winner (v4) and runner-up (v5), with v2 as baseline, against the
**19 UNSEEN roasts** the prompt work never touched: the over-dark logs the
operator excluded (drop **≥ 198 °C**). Runner:
[`bakeoff-holdout-prompts.py`](bakeoff-holdout-prompts.py); scorecard
`bakeoff-holdout-2026-06-14.{md,json}` (written on completion).

Read on two axes:
- **Generalization (clean):** does the prompt reliably *recognize the drop window*
  on roasts it never informed (recall > 0)?
- **Ceiling behavior:** on roasts the operator dropped at 198–202 °C, does the
  prompt recommend dropping *earlier* (≤ ~196 °C indicated) — i.e. would it have
  *caught* the over-roast? **Caveat:** the harness feeds the advisor
  `target_drop_temp_c = the actual (over-dark) drop`, so this measures whether
  v4's ≤196 ceiling overrides an over-dark profile target. Agreement-with-the-human
  is **not** correctness here (the human over-roasted) — a lower-agreement
  *earlier* drop is the better outcome; read recall + the drop-temperature
  distribution, not F1 alone.

**Honest limitation:** there are no unseen *good* roasts left (all 28 informed the
prompt design), so this validates generalization + ceiling logic on the over-dark
population only — not "matches good drops on unseen good data." The clean future
fix is to hold out the operator's *next* real roasts as a true test set.

## How to run

```bash
# Build the fixtures from the .alog history (drop < 198 = the 28-roast set):
python scripts/alog_to_fixture.py "<roasting-logs-dir>" --out-dir .artisan-fixtures

# Model bake-off (needs an OpenRouter key; the replay/scoring layer is testable
# without one via a fake advisor):
OPENROUTER_API_KEY=sk-or-... python docs/advisor/bakeoff-run-artisan.py
# Prompt sweep on the pinned model:
OPENROUTER_API_KEY=sk-or-... python docs/advisor/bakeoff-run-prompts.py
```

## Caveats (carry these with every number)

- **Agreement ≠ correctness.** F1 measures match-to-this-human-roast.
- **Coarse drop label.** 30 s cadence, one drop-positive tick per roast → F1 is
  timing-sensitive; read it WITH the timing error and recall.
- **Train-on-test.** The prompts are hand-authored against the same 28-roast
  distribution; the *ranking* (v4/v5 > v8 > v6 > v7, all > v2 on recall) is the
  load-bearing result, not the exact 0.881. Phase 3 is the mitigation.
- **Held-out = over-dark only.** Phase 3 tests generalization + the ceiling, not
  good-drop agreement on unseen good roasts.

## Decisions

- **D33** — pinned `google/gemini-3.1-flash-lite` + v2 (model selection). *Merged.*
- **D34** — recommended pin of prompt **v4** (drop-recall fix). *Pending operator
  go + Phase 3.*
