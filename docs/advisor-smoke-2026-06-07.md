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
| timeout_seconds | `10.0` (controller budget) |

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

**At the production budget (`timeout_seconds=10`): TimeoutError.** The
advisory call does not return within 10 s, so in a live roast the controller
would REJECT it and hold current targets — correct fail-safe behavior, but
no advice delivered. Root cause is thinking mode (below), not the network or
the model's competence.

**With headroom (`timeout_seconds=180`, characterization only — exceeds the
controller budget):**

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
   which is the anticipated real finding. To use this model for live advice,
   disable thinking at the **LM Studio model-load config** (or choose a
   non-reasoning model / a faster-reasoning one). This is an operator/model
   choice, not an advisor-code change.

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
- **Reasoning models need their thinking disabled (or a much larger budget)
  to fit the 10 s tick-aligned advisory window.** Latency, not advice
  quality, is the gating factor for this candidate. Capture latency and
  reasoning-token cost alongside advice quality when comparing.
- A cloud non-reasoning model (or Qwen with thinking off) is the apples-to-
  apples comparison point; record each candidate's latency at the production
  budget.
