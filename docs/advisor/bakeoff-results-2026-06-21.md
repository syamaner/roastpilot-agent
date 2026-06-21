# Advisor bake-off — post-FC control results (#277, 21 Jun 2026)

> **Read first — what these numbers mean.** The ground truth is a known-GOOD
> roast, *not* a provably optimal one. Every metric here measures **agreement
> with a known-good roast**, NOT absolute correctness: a capable model may
> legitimately differ from what the human did and still roast well, and high
> agreement is not proof of quality. `drop F1 = 1.0` means *matched this one
> good roast*, not *correct*. Treat the whole scorecard as a quantitative aid to
> the operator's judgement (the advice samples + the latency gate), never a
> replacement for it.

This is the #277 evaluation that selects the model for the **live post-FC
control advisory loop**. Under D35 the controller drives the pre-first-crack
roast deterministically (heat 100 / fan 30 to first crack); the advisor's actual
job — and the only scope evaluated here — is the **post-FC `DEVELOPMENT` phase**:
trim heat / fan through development and call the **drop**. That scope decision is
the headline method change versus the earlier (14 / 16 Jun) bake-offs, which
scored all phases.

Raw data:

- `docs/advisor/bakeoff-results-2026-06-21.json` — the finalists pass
  (2 seeds × 17 known-good mediums, c1 prompt).
- `docs/advisor/bakeoff-screen-2026-06-21.json` — the screen pass
  (9 models × 6 representative mediums, single seed).

(`capture_path` + `interesting_cells` are stripped from the committed JSON: the
raw per-call capture — full prompts / responses / reasoning traces — is
gitignored as `*.capture.jsonl`, mirroring the `.artisan-fixtures` convention.
The aggregate `cells`, the per-tick `samples`, and `availability` are retained.)

---

## (a) Decision — PIN `openai/gpt-4o` + prompt c1

**The live post-FC control model is `openai/gpt-4o`, run with the as-built c1
control teaching prompt.** Rationale:

- **Closest to the operator's real heat moves.** gpt-4o's heat MAE is ≈7.5
  percentage points against what the human actually did, versus ≈22 for
  gemini-3.1-flash-lite and ≈31 for gemini-3-flash-preview. Heat magnitude is
  where a development-phase advisor earns its keep (the drop is the easier,
  later call), and gpt-4o is the only finalist that tracks the human's heat
  trajectory tightly. It also has the best heat-direction agreement (≈0.78).
- **It is the proven baseline.** gpt-4o is the model in the operator's working
  n8n autonomous-roaster (D40.4). Pinning it de-risks the first supervised
  hardware roast: we are matching a control behaviour that has already roasted
  real coffee, not betting on an unproven model.
- **Reliable drop + live-viable latency.** Drop F1 ≈0.86 (clean, no never-drop
  failures) and ≈2.0 s median FC-slot latency, comfortably inside the 10 s gate.

**Runner-up: `google/gemini-3.1-flash-lite`** — the speed / cost choice (≈1.0 s,
the fastest, and roughly 5× cheaper than gpt-4o), with a clean drop (F1 ≈0.87).
The gap that keeps it second is **heat-magnitude fidelity** (MAE ≈22): it gets
the drop and the broad direction right but does not match the operator's heat
levels as closely. It is the obvious candidate to revisit once the cloud
feedback loop (D29 / D42) can learn heat trims from our own labelled corpus.

**Rejected: `google/gemini-3-flash-preview`** — despite the *best* drop F1
(≈0.92, the cleanest drop in the set), it **steers heat the wrong way**: worst
heat MAE (≈31) and worst heat-direction agreement (≈0.48, barely better than a
coin flip). A model that calls the drop well but moves the heat lever wrongly
through development is not a control advisor we want driving the roast.

`gpt-4o` stays **config-overridable** (D5 — the model slug is config); this PIN
sets the default the live controller uses, not a hard-coded model.

---

## (b) Finalist scorecard

Finalists carried to the FULL set: 2 seeds × 17 known-good mediums, c1 prompt.
Of the six finalists flagged in the roster, **three produced usable full data**;
the other three were unreachable on this OpenRouter access (see disposition
below). Headline aggregates:

| model | drop F1 | heat MAE (pp) | heat-dir | thrash (mean abs Δ trend) | latency FC (s) | relative cost | verdict |
|-------|---------|---------------|----------|----------------------------|----------------|---------------|---------|
| **openai/gpt-4o** (baseline-n8n) | ≈0.86 | **≈7.5** (best) | **≈0.78** (best) | ≈0.42 | ≈2.0 | 1× (baseline) | **PIN** |
| google/gemini-3.1-flash-lite (prior-winner) | ≈0.87 | ≈22 | ≈0.69 | ≈0.32 | **≈1.0** (fastest) | **~0.2×** (~5× cheapest) | runner-up (cost / speed) |
| google/gemini-3-flash-preview (control-cand.) | **≈0.92** (best drop) | ≈31 (worst) | ≈0.48 (worst) | **≈0.25** (least) | ≈1.8 | mid | rejected (steers heat wrong) |

Reading the table: gpt-4o wins on the two metrics that decide a development-phase
control advisor — **heat MAE** and **heat-direction agreement** — while staying
reliable on the drop and inside the latency gate. flash-lite trades a little heat
fidelity for ≈5× lower cost and ≈half the latency. flash-preview's clean drop is
real but is undermined by it pushing heat the wrong way.

Per-roast numbers (all 17 mediums × 2 seeds), per-tick advice samples, and the
heat-direction / drop confusion matrices are in
`docs/advisor/bakeoff-results-2026-06-21.json`.

---

## (c) Screen disposition (the other six)

The screen ran all 9 roster models once over 6 representative known-good
mediums; the availability sweep ran first. Disposition of the candidates that
did **not** become usable finalists:

**Unreachable on this OpenRouter access** (consistent >5 s reachability-probe
timeouts, 2 attempts each — `availability` in the JSON records each):

- `openai/gpt-5-nano` — probe timed out (2 attempts).
- `openai/gpt-5-mini` — probe timed out (2 attempts).
- `x-ai/grok-4-fast` — **404, deprecated** (`xAI recommends switching to Grok
  4.3`); replaced in the roster by the recovery slug below.
- `x-ai/grok-4.3` — the recovery slug for the deprecated grok-4-fast; also
  probe-timed-out (2 attempts), so no control datapoint from xAI this run.

**Behaviourally rejected in the screen:**

- `anthropic/claude-haiku-4.5` — latency >4 s (close to / breaching the live
  band) **and** never-drops on 3 of 6 screen roasts.
- `google/gemini-3.5-flash` — over-holds: never-drops on 5 of 6 **plus** tick
  failures.
- `deepseek/deepseek-v4-flash` — borderline latency **and** never-drops on 2 of
  6.

The dominant rejection reason across the screen was **"never-drop"** (the model
holds and never calls the drop). On a real roast a never-drop advisor would
over-roast the batch — a fail-dangerous behaviour, so it is disqualifying for the
control loop regardless of the other metrics.

---

## (d) Methodology + the surprises

**Tiered, screen-before-spend method.** Availability sweep → screen (9 models ×
6 roasts, single seed) → finalists (the carried set × 17 mediums × 2 seeds) →
recovery pass (the grok-4.3 swap after grok-4-fast 404'd). Total spend ≈$20. The
ground truth is the operator's 17 annotated known-good Hottop **medium** roasts
(drop ≤196 °C, the bitter ceiling), in drop-temperature order; the screen subset
spans the low-drop / high-DTR end, the mid band, and the high-drop / low-DTR end.

**Scope: dev-only consult.** Per D35 the live advisor is consulted only in the
post-FC `DEVELOPMENT` phase, so that is the only phase scored. The eval defaults
to dev-only; `--include-pre-fc` is the opt-out for a one-off inspection of the
gated-out pre-FC ticks (it costs ~4× and scores a path that never runs in
production).

**Metrics.** Drop = `should_drop` agreement over ticks (F1 / precision / recall)
plus first-drop timing error (s and °C). Heat / fan = MAE in percentage points
plus directional agreement (did the model move the lever the way the human did).
Trajectory = the model's *own* command-signal coherence over development
(change / reversal counts, momentum cuts) — agreement-free. Latency = median per
phase, FC-slot tightest, against the 10 s gate.

**Surprises worth recording:**

1. **Screen-before-spend caught a 4× scope bug.** The screen pass surfaced that
   the harness was still scoring the deterministic pre-FC ticks — work the live
   advisor never does under D35. Catching it on the cheap screen, before the
   expensive finalist run, avoided paying ~4× for a path that does not ship.
2. **The availability sweep caught dead / deprecated slugs before the spend.**
   `x-ai/grok-4-fast` was 404 / deprecated and two gpt-5 slugs were unreachable;
   the sweep flagged them up front rather than burning finalist budget on calls
   that would never resolve.
3. **"Never-drop" (over-hold) was the dominant failure mode.** Most rejected
   models did not steer badly so much as refuse to call the drop — the
   fail-dangerous behaviour (over-roast) for a control loop.
4. **Cheapest is not always the winner.** The proven gpt-4o won on the
   decision-critical heat-fidelity metrics even though flash-lite is ~5× cheaper
   and twice as fast. We pinned the model that matches the operator's real
   control moves, not the cheapest one that passes.
5. **Honest-metric caveat (restated).** Every score is **agreement with one
   known-good roast**, not absolute correctness. A high score means "behaved
   like this good roast"; it is decision-support for the operator's first
   supervised hardware roast, not proof the model roasts correctly.

---

## Re-running this eval

See `docs/advisor/bakeoff-runbook.md` for the exact commands, flags, key
handling, dev-only scope / opt-out, and how to add a roast to the test set.
