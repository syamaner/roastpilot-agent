# Advisor bake-off — model-roster screen (16 Jun 2026, D40.4)

**What this is:** a model comparison on the operator's 28 quality-filtered Artisan
roasts, **prompt held at `v4`** (the D34 drop pin) to isolate the model, scored on the
drop decision + heat/fan direction + per-phase latency, **plus** the new
control-trajectory scorer (#277). It is a **SCREEN, not a model pin** — see the framing
below. Runner: `docs/advisor/bakeoff-run-2026-06-16.py`. The full per-roast report
(`bakeoff-results-2026-06-16.{json,md}`, ~2.4 MB) is regenerable and gitignored; this is
the aggregate + the read.

## Aggregate (28 roasts; FC latency = median / max, gate ≈ 5 s)

| model | drop F1 | recall | heat-dir | FC latency | gate | traj-sanity | entropy | mom-cuts |
|---|---|---|---|---|---|---|---|---|
| google/gemini-3.1-flash-lite | 0.87 | 1.0 | **0.84** | **1.21 / 1.44** | ✅ | 0.336 | 0.263 | 19 |
| openai/gpt-4o-mini | 0.90 | 1.0 | 0.82 | 2.06 / 2.39 | ✅ | 0.480 | 0.409 | 19 |
| openai/gpt-4o | 0.88 | 1.0 | 0.74 | 2.41 / 3.73 | ✅ | 0.398 | 0.302 | 26 |
| anthropic/claude-haiku-4.5 | 0.75 | 1.0 | 0.76 | 4.10 / 4.59 | ✅ (tight) | 0.309 | 0.223 | 23 |
| openai/gpt-5-mini (reasoning=low) | 0.94 | 0.96 | **0.26** | 5.23 / 6.07 | ❌ | **0.524** | **0.479** | 12 |
| anthropic/claude-opus-4.8 | 0.76 | 1.0 | 0.40 | 6.08 / 6.68 | ❌ | 0.221 | 0.127 | 26 |
| openai/gpt-5.5 | **0.92** | 1.0 | 0.41 | 7.17 / 8.58 | ❌ | 0.264 | 0.183 | 22 |
| anthropic/claude-sonnet-4.6 | 0.89 | 1.0 | 0.40 | 8.30 / 9.23 | ❌ | 0.342 | 0.271 | 19 |

(traj-sanity / entropy: lower = less thrash, development phase only. mom-cuts: total
heat cuts on an already-declining RoR across the 28 roasts, development phase.)

## The read

1. **The FC latency gate is decisive and is the one near-prompt-invariant screen.** Only
   **gpt-4o, gpt-4o-mini, and gemini-3.1-flash-lite** clear ~5 s comfortably (haiku-4.5 is
   tight at 4.1 s). **sonnet-4.6, gpt-5.5, opus-4.8, and even gpt-5-mini at `reasoning=low`
   all bust it** — despite gpt-5-mini / gpt-5.5 topping the drop-F1 column. The reasoning
   and frontier models think their way past the wall; same lesson as the original bake-off.
2. **Drop skill barely separates the survivors.** Recall is **1.0 across the whole roster**
   (v4's recall fix holds), and F1 is a 0.87–0.90 near-tie among the latency survivors. Drop
   F1 is not the discriminator here; latency and control-behaviour are.
3. **Heat-direction agreement is the quantitative shadow of the pre-FC bake finding.**
   gpt-5-mini — the model caught cutting heat + opening fan pre-FC (see
   `negative-cases/2026-06-16-pre-fc-fan-into-crack.md`) — has the **worst heat-dir of the
   roster (0.26)**; the frontier trio (opus / sonnet / gpt-5.5) cluster at ~0.40, the
   "intervene when the human held" signature. The latency survivors that move the levers
   most like the roaster are **gemini-flash-lite (0.84)** and **gpt-4o-mini (0.82)**. The
   development-phase trajectory scorer agrees: gpt-5-mini is the most thrashy (0.52 / 0.48),
   opus the most coherent (0.22).

## Framing — this is a SCREEN, not a pin (D40 method)

- **No model is pinned or culled on this run.** It was scored on `v4`, which is drop-tuned
  and says nothing about phase discipline, so the low heat-dir on the frontier models and
  gpt-5-mini reflects the **prompt gap** as much as the model.
- **The precise per-model pre-FC-intervention tally cannot be reconstructed from this run** —
  the trajectory scorer is development-phase only, heat-dir is aggregated over all phases,
  and this run predates the reasoning capture (#284/#285, merged after). Heat-dir is the
  proxy; the exact tally needs the **capture-enabled re-run with the teaching prompt**.
- **The real model pin comes from that re-run** (#274 teaching prompt + #284 capture),
  reporting **paired before→after per model** (D40.4). Meanwhile the incumbent pin
  (`gemini-3.1-flash-lite`, D33) stands — and this screen reaffirms it: fastest by 2×, top
  heat-dir, drop-F1 in the pack. gpt-4o (the operator's n8n-proven control model) is
  confirmed latency-viable for the first time; gpt-4o-mini is a close third.

## Regenerate

```bash
python scripts/alog_to_fixture.py "<roasting-logs-dir>" --out-dir .artisan-fixtures
OPENROUTER_API_KEY=sk-or-... python docs/advisor/bakeoff-run-2026-06-16.py
```

Cost note: this premium roster over the full 28-roast set ran ~$31 (≈10× a cheap-slate
run); budget eval cost from roster × test set, and see #280/#281/#284 for the harness
hardening (checkpoint / cost guard / concurrency / capture) the run motivated.
