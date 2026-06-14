# Advisor bake-off — Artisan-expanded re-run summary (14 Jun 2026)

> **Read first — what these numbers mean.** Ground truth is a *known-good*
> roast, **not** a provably optimal one. Every metric measures **agreement with
> what the human did**, NOT absolute correctness. Drop F1 = 1.0 means *matched
> this roast's drop*, not *correct*. A model that wanted a slightly later drop
> than the human scores a "miss" here even if its roast would have been fine.
> These are a quantitative **aid** to the operator's judgement (advice samples +
> the latency gate + the controller's own ≤196 °C ceiling), never a replacement.

## Why this run

The first run (`bakeoff-results-2026-06-14.md`) scored only the two 7-Jun live
roasts — **N=2**, one drop instance each, so the drop-F1 was coarse and read a
misleading **1.0** for the winner. This run replays the operator's **28
quality-filtered Artisan roasts** (drop < 198 °C — the operator's bitterness
ceiling; built by `scripts/alog_to_fixture.py` from the annotated `.alog`
history), **v2 prompt only** (v3 lost to v2 in the first run), at 30 s cadence.
Roster A = the ≤3 s FC-viable cheap set; Opus on a 3-roast DTR-spanning subset.

**This run cost ~$2.29.** Fixtures are anonymized (`artisan-NN`, no dates) and
gitignored; this summary + the raw scorecard are committed.

## Headline (mean across 28 roasts, v2)

| model | drop F1 | called drop on | dropΔ (s/°C) | heat-dir | heat MAE | fan-dir | latency | verdict |
|---|---|---|---|---|---|---|---|---|
| **google/gemini-3.1-flash-lite** | **0.63** | **18/28** (17 clean +1 false) | **−0.1 / 0.0** | **0.88** | 8.6 | 0.52 | **1.21 s** | **only viable model** |
| openai/gpt-4.1-mini | 0.07 | 2/28 | 0 / 0 | 0.55 | 13.7 | 0.23 | 1.83 s | over-holds |
| meta-llama/llama-3.3-70b | 0.07 | 2/28 | 0 / 0 | 0.75 | 17.4 | 0.77 | 1.9 s | over-holds |
| openai/gpt-5.4-nano | 0.00 | 0/28 | — | 0.34 | 19.6 | 0.18 | 1.46 s | never drops; bad levers |
| anthropic/claude-opus-4.8 *(spot, 3)* | 0.00 | 0/3 | — | 0.35 | 18.8 | 0.31 | 5.88 s | over-holds (confirms 1st run) |

## Findings

1. **`gemini-3.1-flash-lite` + v2 is still the clear pick — and now the *only*
   model that makes the flavor-critical call at all.** It calls the drop on
   18/28 roasts (17 spot-on, exactly at the human's drop tick, 1 false alarm one
   tick early), with the best heat-direction agreement (0.88, including the
   anticipatory pre-FC cut) and the fastest latency (1.21 s — viable for every
   phase, including the tight FC gate). Highest precision of any model (1 false
   positive across 28 roasts).

2. **The honest robust drop-F1 is 0.63, not the N=2 mirage of 1.0.** The
   expansion was worth it: it corrected an over-optimistic score *and* surfaced
   a real limitation — gemini **misses the drop on ~10/28 roasts** (recall 0:
   it never suggested dropping within the scored window). The misses skew toward
   the **cooler / shorter-development human drops** (drop 191–197 °C, DTR
   12–17 %): on those roasts gemini's instinct was to develop a little longer
   than the operator actually did. Per the framing, a late-leaning miss is *not*
   necessarily wrong — but it is a known gap to watch and a prompt-tuning target.

3. **"Frontiers over-hold" generalizes hard — and it's the dangerous
   direction.** On 28 diverse roasts, **Opus, gpt-4.1-mini, llama-3.3-70b, and
   gpt-5.4-nano essentially never call the drop** (0–2/28). Failing to drop
   sails the roast *past* the ≤196 °C bitter ceiling — the exact failure the
   controller's own ceiling + the operator's drop button exist to catch. The
   cheap flash model is the one that recognizes the moment; the capable/slow
   models do not. (Opus also confirmed at 5.88 s — far over the FC latency gate.)

## Recommendation

**Pin `google/gemini-3.1-flash-lite` + prompt `v2` as the advisor default for
all phases** (no per-phase override — it is fast enough everywhere). It is the
only model that reliably makes the drop call, is the fastest, and the cheapest.
There is **no useful LLM fallback** — every alternative over-holds, so a
fallback model is a *worse* safety net than the deterministic controller hold +
operator. The safety net for gemini's recall gap is the controller's ≤196 °C
ceiling + the operator's drop button (the advisor is advisory by invariant).

**Follow-up (not blocking the pin):** a prompt iteration targeting the
drop-recall gap — anchor the drop suggestion nearer the operator's empirical
drop band (≈193–197 °C / DTR ≈13–16 %) rather than holding for a higher
target — then re-run this harness to see if recall improves without losing the
0.88 heat-direction and high precision.

## Caveats

- **Agreement ≠ correctness.** F1 measures match-to-this-human-roast. Gemini's
  "misses" are mostly later-leaning drops that may roast fine.
- **Coarse drop label.** 30 s cadence, one drop-positive tick per roast → F1 is
  timing-sensitive; read it WITH the (excellent) timing error and the recall.
- **Opus = 3-roast spot-check**, not the full 28 (cost) — but 0/3 is consistent
  with the first run's 0/2 and the whole-roster over-hold pattern.
- `development_elapsed` is now computed in the controller (was hardcoded
  `None`); this harness reconstructs it from the FC timestamp identically.
