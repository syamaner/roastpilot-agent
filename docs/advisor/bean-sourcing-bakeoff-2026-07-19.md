# Bean-sourcing extraction model bake-off — result (19 Jul 2026)

**Screening verdict: the cheap tier wins. Default to `openai/gpt-5-mini`
(best quality-per-dollar), `openai/gpt-5-nano` as the budget option. The expensive
ceiling (`gpt-4o`) has no extraction edge and should not be used. Re-confirm after
#590 (preprocessing) lands — this is a SCREENING on the current pipeline, not a
certification.**

Harness: `scripts/bakeoff_bean_sourcing.py`. Corpus: 9 hand-labelled real vendor
product pages under `tests/fixtures/bean-sourcing/` (53 gold-present / 28 gold-absent
field cells across 4 vendors; single-origins + 3 blends; green + roasted; single-value
and range altitudes). The full, unchanged `draft_bean_profile_from_url` pipeline runs
over the CAPTURED page bytes via the extractor's injected-`http_client` seam (zero
network). Scoring + stats per `docs/research/bean-sourcing/README.md` §5. 1 pass per
model; ~$0.23 total.

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
| `x-ai/grok-4.3` | 0.645 | 0.823 | 0.802 | 0.904 | 0.870 | 5 | 0.20 / 0.50 | ~0.02 |
| `openai/gpt-5-mini` | 0.623 | **0.833** | **0.858** | 0.843 | 0.821 | 0 | 0.25 / 2.00 | ~0.012 |
| `openai/gpt-5.6-luna` | 0.605 | 0.785 | 0.745 | 0.859 | **0.913** | 5 | 1.00 / 6.00 | ~0.043 |
| `openai/gpt-4o` | 0.592 | 0.745 | 0.736 | 0.867 | 0.870 | 5 | 2.50 / 10.00 | ~0.098 |
| `openai/gpt-5-nano` | 0.556 | 0.745 | 0.764 | 0.810 | 0.821 | 0 | 0.05 / 0.40 | **~0.0024** |
| `openai/gpt-4.1-mini` | 0.529 | 0.759 | 0.726 | 0.819 | 0.647 | 11 | 0.40 / 1.60 | ~0.016 |
| `google/gemini-3.1-flash-lite` | 0.507 | 0.735 | 0.726 | 0.802 | 0.588 | 11 | 0.25 / 1.00 | ~0.010 |
| `anthropic/claude-haiku-4.5` | 0.467 | 0.748 | 0.764 | 0.750 | 0.565 | 5 | 1.00 / 5.00 | ~0.041 |

## Findings

1. **The expensive ceiling has no edge.** `gpt-4o` (0.592) is beaten by `grok-4.3`,
   `gpt-5-mini`, and `gpt-5.6-luna` at 5–40× less cost — confirming the research
   prediction that gpt-4o is *dominated* for this task. Don't pay for it.
2. **The cheap tier wins on value.** `gpt-5-mini` ($0.012/run) posts the best macro-F1
   (0.833) and recall (0.858) of the whole field; `gpt-5-nano` ($0.0024, ~40× cheaper
   than gpt-4o) is competitive at 0.556 Combined. `grok-4.3` tops CombinedScore (0.645,
   best faithfulness 0.904) but at higher cost + 5 errors.
3. **`gpt-5.6-luna` is the best *abstainer*** (0.913) — it confabulates least (2 SPU),
   matching the research's "Haiku/Luna know when not to emit" note; but its recall is
   lower and it's ~18× the price of gpt-5-mini.

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

- **N ≈ 9 pages.** Per the research §5.2, this reliably surfaces a model that hallucinates
  or mis-formats badly, but a 2–3 point CombinedScore gap between two good models sits
  inside overlapping CIs and should NOT decide the pick — fall back to cost + latency
  there. The page-clustered bootstrap is the primary test; McNemar/Wilson treat field
  decisions as independent and overstate certainty (indicative only).
- **Run-to-run sampling variance is real** (the extractor runs at nonzero temperature):
  a model's ERR count and a few field outcomes shift between passes. A perfect small-set
  score is a *warning* (over-easy fixture or mislabel), not a verdict.
- **This is the *pre-#590* pipeline.** Because the Onyx failures depress all scores,
  the FINAL model choice should be re-confirmed on a post-#590 re-run (and ideally a
  larger corpus). Treat `gpt-5-mini` as the current best-evidence default, not a locked
  decision.

## Ops gotcha (cost the first failed run elsewhere)

The advisor reads `OPENROUTER_API_KEY` straight from `os.environ`
(`advisor.py:_build_model`); nothing loads `.env` for a script. A stale
`OPENROUTER_API_KEY` exported by the shell profile SHADOWS the valid `.env` value →
`401 "User not found"`. Pass the `.env` value explicitly (see the reproduce command).
