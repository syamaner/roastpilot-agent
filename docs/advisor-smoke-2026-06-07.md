# Advisor smoke run — 2026-06-07

First real-advice data point for the advisory layer (E8-S2), and the seed
for the E8-S4 bake-off. Run with `scripts/advisor_smoke.py` against a local
LM Studio. **Manual / local only** — never in CI (the hardware-free /
no-network guardrail stays; CI keeps using `FakeAdvisor`).

## Configuration

| Setting | Value |
|---|---|
| provider | `openai_compatible` |
| base_url | `http://127.0.0.1:1234/v1` (LM Studio) |
| model_slug | `qwen/qwen3.6-35b-a3b` (Qwen 3.6 35B-A3B) |
| api_key_env | `LMSTUDIO_API_KEY` (`lm-studio`) |
| prompt_version | `v0` |
| temperature | `0.0` |
| timeout_seconds | `10.0` (`AdvisorConfig.timeout_seconds`; same default as the controller's `ControllerConfig.advisory_timeout_seconds`) |

Command:

```bash
ROASTPILOT_ADVISOR__PROVIDER=openai_compatible \
ROASTPILOT_ADVISOR__PROVIDER_BASE_URL=http://127.0.0.1:1234/v1 \
ROASTPILOT_ADVISOR__API_KEY_ENV=LMSTUDIO_API_KEY \
ROASTPILOT_ADVISOR__MODEL_SLUG=qwen/qwen3.6-35b-a3b \
LMSTUDIO_API_KEY=lm-studio \
python scripts/advisor_smoke.py --iterations 1
```

## Source telemetry row

Grounded in a real development-phase row from the coffee-roaster-mcp
live-roast export `tests/fixtures/live-roast-2026-06-07/session-1/roast.jsonl`
(the 7 Jun 2026 supervised Hottop roast), ~45 s after first crack:

- monotonic 1225.6 s, `recorded_at_utc` 2026-06-07T12:18:56Z
- bean 189 °C, env 238 °C, heat 60 %, fan 60 %
- first crack at +541.5 s (roast start = beans added); drop happened at
  bean 197 °C

Derived `AdvisorContext` (RoR computed from the raw temperatures over the
prior ~60 s; target = the roast's actual achieved drop temp):

```json
{
  "phase": "development",
  "roast_elapsed_seconds": 586.677,
  "development_elapsed_seconds": 45.157,
  "current_bean_temp_c": 189.0,
  "current_env_temp_c": 238.0,
  "bean_ror_c_per_min": 10.971,
  "env_ror_c_per_min": 9.974,
  "target_drop_temp_c": 197.0,
  "profile_name": "live-roast-2026-06-07/session-1",
  "first_crack_detected": true,
  "first_crack_timestamp_seconds": 541.52
}
```

(plus the last 6 telemetry rows as `recent_telemetry_samples`)

## Result

**At the production budget (`AdvisorConfig.timeout_seconds=10`, the harness
default): TimeoutError.** The advisory call does not return within 10 s, so
in a live roast the controller — which applies its own
`ControllerConfig.advisory_timeout_seconds` (also 10 s) — would REJECT it and
hold current targets, correct fail-safe behavior but no advice delivered.
Root cause is thinking mode (below), not the network or the model's
competence.

**With headroom (`ROASTPILOT_ADVISOR__TIMEOUT_SECONDS=180`, characterization
only — extends the harness budget, not the controller's live budget):**

- latency **21.55 s**
- raw model output: a clean `final_result` tool call (no JSON parse issues),
  `finish_reason=tool_calls`
- usage: 954 prompt + 2254 completion tokens, of which **2103 are reasoning
  tokens** — the thinking-mode cost that drives the latency

Parsed `RoastDecision`:

```
heat=60%  fan=60%  should_drop=false  confidence=0.9
rationale: "Bean is 8°C from target (197°C) with a high ROR (~11°C/min).
Maintaining current settings (60/60) will likely reach target in ~45s,
completing the current development window."
```

(A request-shape replay returned the same decision with confidence 0.85 and
an equivalent rationale — stable across runs at temperature 0.)

**With thinking disabled (the fix — at the production budget):** after turning
reasoning off in the LM Studio model-load config (the only place it could be
disabled; see Finding 1), the same model at the default 10 s budget:

- 3/3 iterations succeed, latency **~2.2 s mean (1.72 – 2.93 s)** — well
  inside budget
- clean `final_result` tool call, same sane decision, stable across
  iterations: `heat=60% fan=60% should_drop=false confidence=0.85`,
  rationale "Bean is in development phase, 7.8 °C below target drop temp. ROR
  is healthy (10.97 °C/min). Maintain current heat/fan settings…"

This closes the loop: with reasoning off, the convenience local setup
delivers usable, sane, structured advice inside the tick-aligned advisory
window. Latency — not the harness, the advisor, or advice quality — was the
only gate.

### Is the advice sane for this roast moment?

Yes. Bean is 189 °C, 8 °C below the 197 °C drop target, climbing at
~11 °C/min in development. The model:

- **holds** heat/fan at the roast's actual 60/60 rather than spiking — right
  call this close to target;
- **does not drop** (bean still below target) — the drop-eligibility the
  controller would also enforce, reached here on merit;
- reads the context correctly — it recovered the 8 °C-to-target gap and the
  ~11 °C/min RoR from the JSON, and even estimated ~45 s to target.

This is a credible first real-advice data point.

## Findings

1. **Thinking mode blows the timeout (real finding — not worked around).**
   Qwen 3.6 35B-A3B in LM Studio reasons on every call. Even a trivial
   "reply with one word" prompt spent ~200 reasoning tokens; the full
   advisory prompt spent ~2100, taking ~21 s — over the 10 s controller
   budget. Per the guardrail the production timeout was **not** widened to
   mask this.

   Request-level switches did **not** disable it:
   - `chat_template_kwargs={"enable_thinking": false}` → still ~199 reasoning
     tokens (ignored by this build);
   - `/no_think` in system + user message → still ~233 reasoning tokens
     (ignored).

   The advisor's fixed `v0` system prompt therefore cannot turn thinking off,
   which is the anticipated real finding. Root cause is confirmed in the
   Qwen chat template: it gates on `enable_thinking`, but **LM Studio's
   OpenAI-compatible endpoint does not forward `chat_template_kwargs`** to the
   template, so the request-level switch never reaches it.

   **Resolution (verified):** disabling reasoning in the **LM Studio
   model-load config** drops latency from ~21 s to **~2.2 s** at the default
   10 s budget, with the same sane structured advice (see "With thinking
   disabled" above). This is an operator/model choice, not an advisor-code
   change. Alternatively, choose a genuinely non-reasoning instruct model.

2. **Structured output works out of the box — no code change needed.**
   PydanticAI's default tool-calling structured-output mode works against LM
   Studio: the OpenAI-compatible endpoint returned a proper `tool_calls`
   response (`final_result` with valid arguments) and PydanticAI parsed it
   cleanly. The anticipated JSON-schema / `response_format` fallback was
   **not** required for this server, so no config-guarded advisor change was
   made. (Should a future local server reject tool-calling, that fallback
   becomes a separate, config-guarded follow-up — it is not needed today.)

3. **Advice quality:** sane and well-grounded for the sampled moment (see
   above). First data point toward the E8-S4 comparison.

## Implications for E8-S4 (bake-off)

- `scripts/advisor_smoke.py --iterations N` is the bake-off runner: swap
  `ROASTPILOT_ADVISOR__PROVIDER` / `MODEL_SLUG` (and base_url/api_key_env)
  to compare candidates against the same grounded context — no code change
  (D18).
- **Reasoning models need their thinking disabled to fit the 10 s
  tick-aligned advisory window.** Latency, not advice quality, is the gating
  factor. With reasoning off, Qwen 3.6 35B-A3B comes in at ~2.2 s with the
  same sane advice. Capture latency and reasoning-token cost alongside advice
  quality when comparing.
- Candidate latencies at the production budget so far:

  | Model | Reasoning | Latency | Fits 10 s? | Advice |
  |---|---|---|---|---|
  | Qwen 3.6 35B-A3B | on (default) | ~21.5 s | ✗ | hold 60/60, no drop — sane |
  | Gemma 4 e4b | on (default) | ~13.8 s | ✗ | hold 60/60, no drop — sane |
  | Qwen 3.6 35B-A3B | **off** | **~2.2 s** | ✓ | hold 60/60, no drop — sane |

  (Both LM Studio models reason by default; turning it off is what makes them
  viable. A cloud non-reasoning model is the apples-to-apples comparison
  point — record each candidate's latency at the production budget.)
