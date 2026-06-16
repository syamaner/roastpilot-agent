# Negative case — pre-FC fan-into-the-crack + heat cut (16 Jun 2026)

**Source:** the 16 Jun model-roster bake-off (`docs/advisor/bakeoff-run-2026-06-16.py`),
roast `.artisan-fixtures/artisan-02`, prompt **`v4`**, model **`openai/gpt-5-mini`**
(reasoning effort `low`), a single **pre-first-crack** tick (roast_elapsed 534 s,
bean 168 °C). Captured live during the run.

**Label: NEGATIVE — the #218 bake behaviour, reproduced.** A capable *reasoning* model,
correctly naming the phase, still made the dangerous move — because `v4` hands it the
heat/fan levers in **every** phase with no phase *rule*. This is a **prompt gap, not a
model indictment**: per the eval method (#277), gpt-5-mini is **not** culled for this; the
same model with the D39 teaching prompt + the D35 phase discipline would not be asked to,
and likely would not, do this.

## Context sent to the model

```json
{"phase":"roasting_pre_first_crack","roast_elapsed_seconds":534.0,"development_elapsed_seconds":null,"current_bean_temp_c":168.0,"current_env_temp_c":219.0,"bean_ror_c_per_min":10.7,"env_ror_c_per_min":7.0,"target_drop_temp_c":190.0,"target_development_percent":17.9,"charge_guidance_min_c":null,"charge_guidance_max_c":null,"profile_name":".artisan-fixtures/artisan-02","recent_telemetry_samples":[{"monotonic_seconds":537.698,"bean_temp_c":167.0,"env_temp_c":218.0,"heat_level_percent":100,"fan_level_percent":30},{"monotonic_seconds":538.698,"bean_temp_c":167.3,"env_temp_c":218.3,"heat_level_percent":100,"fan_level_percent":30},{"monotonic_seconds":539.698,"bean_temp_c":167.7,"env_temp_c":218.7,"heat_level_percent":100,"fan_level_percent":30},{"monotonic_seconds":540.698,"bean_temp_c":168.0,"env_temp_c":219.0,"heat_level_percent":100,"fan_level_percent":30},{"monotonic_seconds":541.698,"bean_temp_c":168.0,"env_temp_c":219.0,"heat_level_percent":100,"fan_level_percent":30},{"monotonic_seconds":542.698,"bean_temp_c":168.0,"env_temp_c":219.0,"heat_level_percent":100,"fan_level_percent":30}],"first_crack_detected":false,"first_crack_timestamp_seconds":null,"seconds_since_charge":null}
```

## Model response

```json
{"target_heat":60,"target_fan":50,"should_drop":false,"confidence":0.8,"rationale":"Pre-first-crack with high bean RoR (~10.7 °C/min) and element at 100%: act early and decisively to prevent overshoot. Substantially reduce indicated heat to 60% to bring RoR down (thermal lag means effect will appear later) and raise fan to 50% to shift toward convective transfer and evacuate chaff/smoke. Do not drop — first crack not detected and current temp (168 °C) is well below profile drop (190 °C). Monitor RoR; if RoR falls too quickly (<4 °C/min) lower fan or raise heat modestly to avoid stalling."}
```

## Why it's wrong (operator method + D35)

- **Fan 30 → 50 approaching FC** at bean 168 / RoR 10.7. The textbook #218 error —
  *raising fan into the crack crashes RoR through first crack*. The operator's proven method
  holds fan low to FC; opening it here is the known-bad move.
- **Heat 100 → 60 at bean 168**, still climbing toward the ~170–180 °C FC band. "Prevent
  overshoot" sounds prudent, but cutting that hard *before* FC risks **stalling/delaying** the
  crack — the opposite of "drive to FC".
- The human in `artisan-02` held **100 / 30** through here (see `recent_telemetry_samples`):
  a clean directional **disagreement**, in the dangerous direction.
- It correctly names the phase ("Pre-first-crack…") — **recognition is not the failure;
  *acting* pre-FC is.** Even the model's own hedge ("if RoR falls too quickly … avoid
  stalling") shows it sensed the risk it was creating.

## Correct behaviour

- **D35:** pre-FC is deterministic (heat 100 / fan low, drive to FC); the LLM is **not
  consulted pre-FC** — this recommendation would never be solicited live.
- **D36 (#228) pre-FC advisory layer** (post-first-roast): at most a *moderate* anticipatory
  heat trim (~60–70 %), **never a fan raise** into FC, over the always-on deterministic floor,
  failing closed to it.
- **D39 (#274) teaching prompt:** encodes the phase model + objective — "pre-FC: smooth into
  FC, **never stall**; do not open fan early" — which makes *acting* here wrong, not just
  names the phase.

## Use

- **#274** — a worked negative example for designing/validating the control teaching system
  prompt: the prompt must make "act pre-FC" wrong, not merely surface the phase label.
- **#224** — a labelled negative-regression case for the corpus / replay harness: the
  contexted + gated loop must **not** reproduce this (no pre-FC fan raise, no momentum-killing
  heat cut before FC).

Cross-refs: #218 (the live bake this mirrors), D35 / D36 / D39, #228 / #274 / #224,
memory `operator-hottop-roast-profile` (fan low to FC), `operator-working-n8n-roast-system`
(pre-FC heat 100 / fan 30).
