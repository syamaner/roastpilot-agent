# Advisor bake-off — 2026-06-08 (E8-S4, plan §11.1 → D20)

Resolves the last E8 open item: which provider/model is the advisor default.
Operator-judged comparison of candidate models against the same grounded
roast context, using the smoke harness as the runner. **Manual / local
only** — live network calls; never in CI. `FakeAdvisor` stays the CI
default; the LLM is advisory-only (a deterministic safety policy validates,
clamps, or rejects every recommendation before any hardware write).

Builds on the single-model smoke run in
[`advisor-smoke-2026-06-07.md`](advisor-smoke-2026-06-07.md).

## Outcome

- **Winner / default: `anthropic/claude-opus-4.8` via OpenRouter, prompt
  `v1`.** Best advice quality with comfortable latency headroom under the
  10 s budget (~4.4 s on the bake-off prompt; ~5.7 s on v1).
- **New electric-roaster prompt `v1`** is now the default
  (`AdvisorConfig.prompt_version`). The original `v0` ("small, conservative
  adjustments") was miscalibrated for an electric roaster — see below.
- Set in `AdvisorConfig`: `model_slug="anthropic/claude-opus-4.8"`,
  `prompt_version="v1"` (provider/base_url already OpenRouter). Native
  Anthropic (no OpenRouter hop/markup, D18) is a config swap:
  `provider=anthropic` + `api_key_env=ANTHROPIC_API_KEY`.

## Methodology (reproducible)

- **Runner:** `scripts/advisor_bakeoff.py` (loops the slate over the real
  `PydanticAIAdvisor`, reusing `advisor_smoke.build_context`). Candidates
  differ **only by config** — no advisor code changes (D18).
- **Grounded context:** the 7 Jun 2026 live-roast fixture
  (`tests/fixtures/live-roast-2026-06-07/session-1`), sampled at **3
  development moments** via `--offset-seconds`: early (10 s post-first-crack,
  bean ~183 °C), mid (45 s, 189 °C, RoR ~11 °C/min, 8 °C below the 197 °C
  drop target), late (80 s, 194 °C, near drop). `temperature=0.0`.
- **N = 3** iterations per cell (7 candidates × 3 moments = 21 cells).
- **The hard gate is latency.** The advisory call is bounded by the
  controller's 10 s tick-aligned budget; advice that arrives late is useless
  on a roaster. A cell **passes** iff its median latency ≤ 10 s. Over-budget
  candidates are kept here labelled, not dropped.
- **Quality is operator-judged** (the story's output is this document + the
  default, not an automated metric).
- Cloud candidates ran via OpenRouter (`OPENROUTER_API_KEY` exported in the
  operator's shell — never in config or the repo); the local baseline ran
  against LM Studio.

## Latency vs the 10 s gate

| Candidate | Tier | Median (per moment) | Gate | Cost |
|---|---|---|---|---|
| qwen3.6-35b-a3b (local, reasoning-off) | baseline (free) | 1.6 s | ✅ | free |
| anthropic/claude-haiku-4.5 | cheap cloud | 2.9–3.3 s | ✅ | cheap |
| **anthropic/claude-opus-4.8** | frontier | **4.4–4.5 s** | ✅ | $5/1M |
| google/gemini-3.5-flash | cheap cloud | 4.3–7.5 s | ✅ | $1.5/1M |
| openai/gpt-5.5 | frontier | 5.3–6.8 s | ✅ | $5.5/1M |
| anthropic/claude-sonnet-4.6 | frontier | 7.5–8.4 s | ⚠️ pass (thin margin) | $4/1M |
| openai/gpt-5-mini | cheap cloud | 12.0–15.9 s | ❌ over budget | cheap |

Notes:
- **`gpt-5-mini` is the only failure** — it reasons by default via OpenRouter
  (12–16 s); the run warned `temperature` was ignored because reasoning was
  on. (So its near-deterministic assumption also breaks.)
- **Surprise: `opus-4.8` (4.4 s) is far faster than `sonnet-4.6` (8.4 s)** —
  opus runs without extended thinking and lands with comfortable margin,
  while sonnet is the slowest *passing* model (8.4 s leaves little room for a
  slow tick).

## Advice quality (operator-judged)

Every candidate got the fundamentals right at all 3 moments: never drops
early (bean below target throughout), eases heat / raises fan toward target,
reads the context correctly. Depth differed:

- **qwen local** — correct but least adaptive: early it says "maintain
  100/30" (doesn't taper from full heat); holds 60/60 mid/late. Safe, shallow.
- **gemini-flash** — concrete, progressive tapering (75/45 → 50/65 → 45/70).
- **haiku-4.5** — consistent, conservative, well-reasoned (conf 0.78).
- **sonnet-4.6** — richest roast-craft: dev ratio, "~18–20 s to target",
  env-RoR, "the safety controller will catch the drop." Slowest passing.
- **opus-4.8** — frontier reasoning *and* fast: scorch/baked risk, declining
  RoR, prep-to-drop late.
- **gpt-5.5** — terse but correct; eases appropriately.
- **gpt-5-mini** — good reasoning, disqualified by latency.

### Why opus-4.8 wins for an *electric* roaster

The operator's roaster is electric (heating element has real thermal lag),
first crack lands ~180–183 °C and drop ~190–194 °C — a narrow ~10 °C window
where **maximizing development time** is the goal and decisions must happen
**early and decisively** to beat the lag. Cost is not a constraint. Against
those criteria:

- **Latency is decisive.** On a laggy roaster you must react ahead of the
  curve; opus's 4.4 s leaves headroom, sonnet's 8.4 s eats most of the budget
  before the controller even acts.
- **Reasoning depth** about RoR / time-to-target / dev-time tradeoff is
  frontier-tier in opus, matching sonnet.
- Cost ($5/1M) is irrelevant per the operator.

## The prompt was the bigger lever: v0 → v1

Under the original **`v0`** prompt ("prefer small, conservative
adjustments"), *every* model gave only gentle early trims (heat 100 → 75/80/
85 %) — none made the drastic early cut an electric roaster needs. That is a
**prompt** miscalibration, not a model failure. **`v1`** encodes the
hardware reality: electric element → thermal lag → act early and decisively;
primary goal is to maximize development time in the narrow first-crack→drop
window; large heat cuts when post-crack RoR is high.

Opus, same grounded moments, `v0` vs `v1`:

| Moment | v0 (timid) | **v1 (decisive)** |
|---|---|---|
| early (10 s post-FC) | heat 100 → **85** | heat 100 → **55** |
| mid (45 s) | 60 → **55** | 60 → **30** |
| late (80 s) | 60 → **52** | 60 → **38** |

v1 makes large, anticipatory cuts and reasons explicitly about lag — e.g.
*"a timid trim won't catch it — cut heat decisively from 100% to 55% now to
flatten the curve and stretch development time… anticipating thermal lag."*
Confidence rose (0.80–0.83 vs 0.78). The default config (`opus` + `v1`)
re-confirmed under the production 10 s budget: 3/3 pass, mean **5.7 s** (v1's
longer prompt adds ~1.3 s over bare opus, still comfortable).

## Reproduce

```bash
# Full slate (set OPENROUTER_API_KEY in your shell first; never commit it):
OPENROUTER_API_KEY=... LMSTUDIO_API_KEY=lm-studio \
  python scripts/advisor_bakeoff.py --iterations 3 --out /tmp/bakeoff.json

# Single candidate / prompt / roast moment via the smoke harness:
OPENROUTER_API_KEY=... \
ROASTPILOT_ADVISOR__MODEL_SLUG=anthropic/claude-opus-4.8 \
ROASTPILOT_ADVISOR__PROMPT_VERSION=v1 \
  python scripts/advisor_smoke.py --iterations 3 --offset-seconds 45
```

## Follow-ups (not blocking the default)

- **Ambient (room) temperature in `AdvisorContext`.** The operator noted
  outdoor/ambient temp affects an electric roast; the context currently
  carries chamber `env_temp_c` but not room ambient. A candidate context
  enrichment for a later prompt/profile iteration.
- **Native-Anthropic default** (D18) once an `ANTHROPIC_API_KEY` is in play —
  avoids the OpenRouter hop/markup; a pure config swap.
- Re-measure with N ≥ 3 if the slate or prompt changes materially.
