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
  `v2`.** It won the first bake-off on quality + latency, and across three
  runs (`v0` → `v1` → `v2`) it is the only frontier model that stays under
  the 10 s gate as the prompt gained roast craft. Latency: ~4.4 s on `v0`,
  ~4.7–5.2 s on `v1`, ~6.2 s on `v2`.
- **The prompt evolved with the operator's domain input, run over run:**
  `v0` (generic "small adjustments") → `v1` (electric roaster: thermal lag,
  decisive early heat) → `v2` (fan as a coupled heat-transfer-mode lever +
  development *duration* as the objective). `v2` is the shipped default.
- Set in `AdvisorConfig`: `model_slug="anthropic/claude-opus-4.8"`,
  `prompt_version="v2"` (provider/base_url already OpenRouter). Native
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

## Second run — the whole slate under `v1` (the shipped prompt)

The first run ranked models under `v0`; the default ships `v1`. To keep the
default evidence-based on the *actual* prompt, the full slate was re-run under
`v1` (same context, moments, N=3):

```bash
python scripts/advisor_bakeoff.py --iterations 3 --prompt-version v1 --out /tmp/bakeoff_v1.json
```

**Finding 1 — the prompt moved every model.** Early-moment heat cut from 100 %:

| Model | v0 (timid) | v1 (decisive) |
|---|---|---|
| qwen3.6-35b-a3b (local) | 100 (*"maintain"*) | **30** |
| google/gemini-3.5-flash | 75 | **40** |
| openai/gpt-5.5 | 85 | **30** |
| anthropic/claude-opus-4.8 | 85 | **55** |
| anthropic/claude-sonnet-4.6 | 80 | **55** |
| anthropic/claude-haiku-4.5 | 85 | **65** |
| openai/gpt-5-mini | 85 | 20 (over budget) |

Under `v0` every model looked timid and similar; `v1` made them all act —
strong evidence that **the prompt, not the model, drove the behavior**.

**Finding 2 — a decisiveness spectrum `v0` hid.** With every model now acting,
they split three ways:

- **Under-follows** — haiku (only 100 → 65): weakest instruction-following on
  "cut decisively."
- **Calibrated-decisive** — opus & sonnet (100 → 55). Sonnet even names a
  *target*: "flatten RoR toward ~4–5 °C/min" — it cuts to hit a sane
  development RoR, not just to cut hard.
- **Aggressive** — gemini (40), gpt-5.5 (30), qwen (30), gpt-5-mini (20): slam
  heat down with no stated RoR target. On a laggy electric element, 100 → 30
  risks crashing RoR / stalling (a "baked" flick-back). Operator judgment
  favoured the **calibrated** band: decisive enough to stretch development,
  not so much it stalls.

**Finding 3 — opus holds, now confirmed on the shipped prompt.** opus and
sonnet give near-identical `v1` advice (heat 55/35/38 vs 55/35/40) with
comparable lag reasoning, but opus is faster.

`v1` latency vs the 10 s gate (median per moment):

| Candidate | early / mid / late | Gate |
|---|---|---|
| qwen3.6-35b-a3b (local) | 2.4 / 2.1 / 2.1 s | ✅ |
| anthropic/claude-haiku-4.5 | 4.4 / 3.8 / 3.6 s | ✅ |
| **anthropic/claude-opus-4.8** | **5.2 / 4.7 / 4.8 s** | ✅ |
| google/gemini-3.5-flash | 4.9 / 6.9 / 6.2 s | ✅ |
| openai/gpt-5.5 | 7.9 / 7.4 / 8.5 s | ⚠️ pass (one iter 10.9 s) |
| anthropic/claude-sonnet-4.6 | 8.0 / 7.6 / 7.5 s | ⚠️ pass (thin margin) |
| openai/gpt-5-mini | 15.9 / 17.5 / 15.0 s | ❌ over budget |

Honest correction to the first run's hypothesis: the longer `v1` prompt was
expected to push sonnet over the gate — it did **not** (sonnet held ~7.5–8.0 s,
within noise of its `v0` 8.4 s). opus still wins on margin, but not because
sonnet busted. `gpt-5-mini` is *worse* under `v1` (longer prompt + default
reasoning) and stays disqualified.

**Decision: opus + v1 stands.** The re-run validated the shipped default on
the shipped prompt and refined the rationale (calibrated-decisive sweet spot).
Two operational notes:

- **Latency margin is also self-correcting.** opus at ~5 s leaves room, and
  the E8-S3 change-based call-frequency policy re-consults the advisor on the
  next meaningful change (≥1 °C bean / ≥2 °C·min RoR / phase / 15 s heartbeat),
  so a slow or skipped call is corrected on the following tick — the controller
  is never blocked waiting.
- **The runners-up are captured and config-swappable** (D18): the candidate
  slate lives in `scripts/advisor_bakeoff.py`; sonnet-4.6 (deeper rationale,
  thinner margin), haiku-4.5 (fast/cheap, under-follows v1), and gemini-3.5-
  flash (cheap, aggressive) are a `MODEL_SLUG` swap away if priorities change.
  Note the latency risk on one runner-up: **gpt-5.5 crossed the gate on a
  single iteration under v1 (10.9 s)** — its medians pass (7.4–8.5 s) but the
  longer prompt leaves it without margin, so a swap-in carries real
  over-budget risk.

## Third run — `v2` (fan + duration), and the new default

Reading the v1 results, the operator surfaced more roast craft that v1 (and
the eval) had under-weighted: on a **Hottop the fan is a primary,
flavor-coupled lever** — it sets the heat-transfer *mode* (radiant/conductive
→ convective) and prevents scorched/baked flavor — and the real development
objective is **duration** (a ~10–20 % development ratio, ~10 % can be
excellent), not hitting a drop temperature (which is a guide one may run
modestly past). `v1` was heat-only and treated the drop temp as a hard stop,
so its fan numbers were a reflex, not reasoning. **`v2`** encodes both: two
coupled levers (heat + fan), and `should_drop` judged on the development
ratio.

```bash
python scripts/advisor_bakeoff.py --iterations 3 --prompt-version v2 --out /tmp/bakeoff_v2.json
```

**Finding — the richer prompt is a far harsher latency filter.** `v2`
(~1,800 chars vs v1's ~1,100) added ~1–2 s, and that pushed the borderline
frontier models over the gate. Only three candidates pass:

| Candidate | v2 latency (iter range) | Gate |
|---|---|---|
| qwen3.6 local | 2.3–2.6 s | ✅ |
| anthropic/claude-haiku-4.5 | 4.3–5.5 s | ✅ |
| **anthropic/claude-opus-4.8** | **5.7–6.8 s** | ✅ |
| anthropic/claude-sonnet-4.6 | 8.8–11.0 s | ❌ over |
| google/gemini-3.5-flash | 5.7–11.9 s | ❌ over |
| openai/gpt-5.5 | 7.3–12.9 s | ❌ over |
| openai/gpt-5-mini | 20–23 s | ❌ over |

**Opus is now the only frontier model that passes** — the v1 prediction that
sonnet would bust the gate came true under the heavier, production-
representative prompt. The more roast craft in the prompt, the more opus's
latency margin matters.

**The prompt worked — fan + duration reasoning appears in every decision.**
All models compute the ratio from context (e.g. "45 s dev / 587 s = 7.7 %"),
treat fan as a transfer-mode lever, and hold `should_drop=False` at the temp
guide (duration, not temperature, drives the drop). Opus at mid: *"~7.7 %
ratio — too short; aim to stretch toward 10–15 %… cut heat to 35 and raise
fan to 75 to shift toward convective transfer, evacuate smoke, and flatten
RoR without crashing it… plan to drop once ratio reaches ~12–15 %."* Fans are
now actively coordinated (opus 45 → 75 → 72, vs a passive 40 → 60 → 60 under
v1).

**A surprise — haiku stepped up.** It *under-followed* v1's "cut decisively"
(only 100 → 65) but follows v2's richer, more structured instructions well
(computes ratio, reasons about convective transfer and baked/scorched notes,
even "if RoR drops below ~5 °C/min, nudge heat back up") — at 4.6 s and cheap.
So v2 promoted haiku from also-ran to the viable fast/cheap alternative.

**Decision: `opus` + `v2` is the new default** (was `opus` + `v1`; plan §11.1
→ D20, refined → **D21**). Opus gives the best-rounded fan+duration advice and
is the lone frontier gate-passer. Sonnet has marginally the sharpest rationale
(targets a specific RoR, "drop ~195–198 °C at 10–13 % ratio") but is
**disqualified by the gate** under the production prompt — exactly what gating
on latency is for. Haiku-4.5 (~4.6 s) is the documented fast/cheap fallback.

## Follow-up surfaced by v2 — `target_development_percent` in context

The models inferred the development ratio from elapsed times + the prompt's
"10–20 %" guidance. Making the **per-profile** target development percent an
explicit `AdvisorContext` field (so different beans can override the general
band) is a clean enrichment for a future story — the operator expects to
refine the duration target and the ~195 °C darkness threshold as more roast
data is collected.

## Reproduce

```bash
# Full slate under the shipped prompt (set OPENROUTER_API_KEY first; never
# commit it). --prompt-version defaults to v2; pass v0/v1 to reproduce the
# earlier runs.
OPENROUTER_API_KEY=... LMSTUDIO_API_KEY=lm-studio \
  python scripts/advisor_bakeoff.py --iterations 3 --prompt-version v2 --out /tmp/bakeoff_v2.json

# Single candidate / prompt / roast moment via the smoke harness:
OPENROUTER_API_KEY=... \
ROASTPILOT_ADVISOR__MODEL_SLUG=anthropic/claude-opus-4.8 \
ROASTPILOT_ADVISOR__PROMPT_VERSION=v2 \
  python scripts/advisor_smoke.py --iterations 3 --offset-seconds 45
```

## Follow-ups (not blocking the default)

- **`target_development_percent` in `AdvisorContext`** (surfaced by v2): make
  the per-profile development-ratio target explicit so different beans can
  override the prompt's general 10–20 % band.
- **Ambient (room) temperature in `AdvisorContext`.** The operator noted
  outdoor/ambient temp affects an electric roast; the context currently
  carries chamber `env_temp_c` but not room ambient. A candidate context
  enrichment for a later prompt/profile iteration.
- **Native-Anthropic default** (D18) once an `ANTHROPIC_API_KEY` is in play —
  avoids the OpenRouter hop/markup; a pure config swap.
- Refine the ~195 °C darkness threshold and the duration target as more roast
  data is collected (operator note).
- Re-measure with N ≥ 3 if the slate or prompt changes materially.
