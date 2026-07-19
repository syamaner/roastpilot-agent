# Bean-sourcing extraction model bake-off — result (19 Jul 2026)

**Screening verdict: the cheap tier wins. Default to `openai/gpt-5-mini`
(best quality-per-dollar), `openai/gpt-5-nano` as the budget option. The expensive
ceiling (`gpt-4o`) has no extraction edge and should not be used. Re-confirm after
#590 (preprocessing) lands — this is a SCREENING on the current pipeline, not a
certification.**

> **Numbers below are the corrected re-run (harness fixes, #600 review).** The
> corpus, extraction pipeline, and per-model list prices are unchanged; only the
> scoring harness was hardened: the product `name` is now scored, hallucinated
> token padding no longer earns full COR credit on text/variety matches, a
> process/lot-only `description` no longer registers as a spurious tasting-notes
> claim, a field the model never attempts counts F1 `0` in macro-F1 (not
> excluded), and the two RANGE-altitude pages are disclosed as a scoring
> limitation below rather than silently capped. Every pairwise CombinedScore gap
> among the top five models is well inside its bootstrap CI (see Findings) —
> the verdict is unchanged, decided on cost + reliability exactly as the
> caveat below prescribes when quality is statistically tied.

Harness: `scripts/bakeoff_bean_sourcing.py`. Corpus: 9 hand-labelled real vendor
product pages under `tests/fixtures/bean-sourcing/` (62 gold-present / 28 gold-absent
field cells across 4 vendors, 10 scored fields per page including `name`;
single-origins + 3 blends; green + roasted; single-value and range altitudes). The
full, unchanged `draft_bean_profile_from_url` pipeline runs over the CAPTURED page
bytes via the extractor's injected-`http_client` seam (zero network). Scoring + stats
per `docs/research/bean-sourcing/README.md` §5. 1 pass per model; ~$0.23 total.

Reproduce (needs a valid `OPENROUTER_API_KEY` — note the shadowing gotcha below):

```
OPENROUTER_API_KEY="$(grep -E '^OPENROUTER_API_KEY=' .env | cut -d= -f2-)" \
  .venv/bin/python scripts/bakeoff_bean_sourcing.py --max-spend 3 --no-resume \
  --out /tmp/bo.json --report-md /tmp/bo.md
```

## Leaderboard

Ranked by `CombinedScore` (+1 COR / +0.5 PAR / +0.5 ABS-COR / 0 MIS / −0.5 INC /
−1 SPU) — the axis that makes an honest abstainer beat a confabulator. macro-F1 is the
model-choice headline (every field counts equally).

| Model | Combined | macro F1 | Recall | Faithful | Abstain | ERR | in$/out$ per 1M | ~$/run |
|---|--:|--:|--:|--:|--:|--:|---|--:|
| `x-ai/grok-4.3` | 0.629 | **0.773** | 0.742 | 0.868 | 0.913 | 5 | 0.20 / 0.50 | ~0.0072 |
| `openai/gpt-4o` | 0.606 | 0.755 | 0.726 | **0.833** | 0.870 | 5 | 2.50 / 10.00 | ~0.0979 |
| `openai/gpt-5-mini` | 0.600 | 0.771 | **0.782** | 0.795 | 0.821 | 0 | 0.25 / 2.00 | ~0.0118 |
| `openai/gpt-5.6-luna` | 0.571 | 0.708 | 0.677 | 0.808 | **0.913** | 5 | 1.00 / 6.00 | ~0.0431 |
| `openai/gpt-5-nano` | 0.561 | 0.723 | 0.726 | 0.776 | 0.857 | 0 | 0.05 / 0.40 | **~0.0024** |
| `openai/gpt-4.1-mini` | 0.529 | 0.741 | 0.726 | 0.763 | 0.696 | 5 | 0.40 / 1.60 | ~0.0157 |
| `google/gemini-3.1-flash-lite` | 0.475 | 0.662 | 0.653 | 0.736 | 0.588 | 11 | 0.25 / 1.00 | ~0.0098 |
| `anthropic/claude-haiku-4.5` | 0.471 | 0.721 | 0.726 | 0.726 | 0.565 | 5 | 1.00 / 5.00 | ~0.0411 |

## Findings

1. **Nothing in the top five is statistically distinguishable at N=9 — so cost and
   reliability decide, per the harness's own rule.** Every pairwise CombinedScore
   gap among `grok-4.3` / `gpt-4o` / `gpt-5-mini` / `gpt-5.6-luna` / `gpt-5-nano`
   has a page-clustered bootstrap CI that crosses zero (e.g. `grok-4.3` vs `gpt-4o`
   +0.024 [-0.071, +0.129]; `gpt-5-mini` vs `gpt-4o` -0.006 [-0.117, +0.117];
   `gpt-5-mini` vs `gpt-5-nano` +0.039 [-0.067, +0.133]). The Honest-caveats rule
   below says exactly this case falls back to cost/latency, not the raw
   leaderboard order.
2. **The expensive ceiling still has no PROVEN edge, at 8–14× the cost.** `gpt-4o`
   ($0.098/run) nominally edges `gpt-5-mini` on CombinedScore (0.606 vs 0.600) but
   loses on macro-F1 — the stated model-choice headline (0.755 vs 0.771) — and the
   Combined gap itself is noise (CI above). `grok-4.3` ($0.0072/run, ~14× cheaper)
   beats `gpt-4o` on both axes. Don't pay for `gpt-4o`.
3. **The cheap tier wins on value + reliability.** `gpt-5-mini` posts the best
   macro-F1 (0.771) and recall (0.782) of any model with **zero page errors**, at
   ~1/8 the price of `gpt-4o`. `gpt-5-nano` is ~5× cheaper again ($0.0024/run,
   ~40× cheaper than `gpt-4o`), also zero page errors, and its macro-F1 gap versus
   `gpt-5-mini` (0.723 vs 0.771) is itself inside the bootstrap CI — a credible
   budget pick, not just a discount one.
4. **`grok-4.3` and `gpt-5.6-luna` tie as the best *abstainers*** (0.913
   abstention-correctness each) — matching the research's "Haiku/Luna know when
   not to emit" note. `gpt-5.6-luna` is ~3.7× the price of `gpt-5-mini`
   ($0.0431 vs $0.0118) and ~2.3× *cheaper* than `gpt-4o`, not the double-digit
   multiples an earlier draft of this table mis-stated.

## Operational findings (fixes applied to the harness / for the extractor)

- **The 10s extraction deadline is too tight.** The extractor's `agent.run()` timeout
  (#587) inherits `AdvisorConfig.timeout_seconds` = 10s, the *per-tick roast-advice*
  budget. On the first pass this timed out **every** call for the reasoning models
  `gpt-5-nano` and `gpt-5-mini` (0/81 scored). A one-shot bean draft is not a per-tick
  advice call — the operator pastes a URL and can wait ~30s. The bake-off uses a 45s
  budget; **the extractor config should decouple the extraction deadline from the advice
  deadline and lengthen it (→ #590).** Without that, the two best cheap models are
  unusable in production.
- **`x-ai/grok-4-fast` is a dead slug** (404, "deprecated — switch to Grok 4.3"). The
  roster now pins `x-ai/grok-4.3`. Slugs drift; verify at run time.
- **The Onyx Shopify pages fail on the 20k-char text cap** — their product specs sit
  past the cap, so models see mostly nav text and can't extract an identity (the
  `could not determine a bean name` errors concentrate on the truncated blend pages).
  This is the exact preprocessing weakness `#590` (extruct JSON-LD-first + trafilatura)
  fixes; it **depresses every model's score here**, so the absolute numbers will rise
  after #590 and the tail may reorder.

## Honest caveats (screening, not certification)

- **Latency was NOT captured for the committed result above.** The evaluation plan
  tie-breaks a statistical dead heat (Findings #1 — most top-model pairs cross zero) on
  cost PLUS latency, but this run predates per-call latency capture in the harness (added
  #600 round-2). The pick above stands on macro-F1 + cost alone; a latency column lands
  on the next re-run (the harness now records it), and the pick should be re-checked
  against it then.
- **N ≈ 9 pages.** Per the research §5.2, this reliably surfaces a model that hallucinates
  or mis-formats badly, but a 2–3 point CombinedScore gap between two good models sits
  inside overlapping CIs and should NOT decide the pick — fall back to cost + latency
  there (exactly the situation in Findings #1 above). The page-clustered bootstrap is the
  primary test; McNemar/Wilson treat field decisions as independent and overstate
  certainty (indicative only).
- **RANGE-altitude `COR` is currently unreachable against the real, unmodified
  extractor — and its effect on the RANKING is NOT guaranteed uniform.** The extraction
  prompt tells the model never to compute a midpoint for a stated altitude range, and a
  scalar altitude is always tagged `"on_page"`, never `"origin_estimated"` — so a page
  whose gold altitude is a RANGE can only ever score `MIS` (weight 0, a compliant
  abstention) or `INC` (weight −0.5, a leaked scalar) on that cell, never `COR`,
  regardless of model quality. Two of the nine pages (`cbc-costa-rica-laminita-tarrazu`,
  `counterculture-concepcion-huista`) hit this. **In the committed leaderboard above, all
  eight models happened to abstain on both cells (`MIS`, weight 0)**, so this run's
  deflation was uniform across the roster and did not by itself reorder anything — but
  that uniformity is a property of THIS run, not a guarantee: a model that leaks an
  in-range scalar instead of abstaining scores `INC` there, an asymmetric penalty a
  compliant abstainer does not pay, which CAN shift both CombinedScore and macro-F1
  ordering on a different pass. Given the top-five pairwise gaps are already inside
  bootstrap noise (Findings #1), treat any close ranking as provisional until the RANGE
  contract is resolved (or these two cells are excluded from scoring). See the harness's
  module docstring for the full mechanism; aligning the RANGE contract with a real
  midpoint/`origin_estimated` extractor feature is deferred to #590.
- **Run-to-run sampling variance is real even though the extractor pins
  `temperature=0.0`** for literal, deterministic extraction — provider-side
  nondeterminism (routing, batching, sampler implementation details) is not fully
  eliminated by a temperature setting alone: a model's ERR count and a few field
  outcomes shift between passes at the SAME temperature. A perfect small-set score is a
  *warning* (over-easy fixture or mislabel), not a verdict.
- **This is the *pre-#590* pipeline.** Because the Onyx failures depress all scores,
  the FINAL model choice should be re-confirmed on a post-#590 re-run (and ideally a
  larger corpus). Treat `gpt-5-mini` as the current best-evidence default, not a locked
  decision.

## Ops gotcha (cost the first failed run elsewhere)

The advisor reads `OPENROUTER_API_KEY` straight from `os.environ`
(`advisor.py:_build_model`); nothing loads `.env` for a script. A stale
`OPENROUTER_API_KEY` exported by the shell profile SHADOWS the valid `.env` value →
`401 "User not found"`. Pass the `.env` value explicitly (see the reproduce command).
