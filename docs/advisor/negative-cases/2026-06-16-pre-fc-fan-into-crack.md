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

## Second instance — confirms it's a `v4` pattern, not a one-off (`artisan-04`)

Same model (`gpt-5-mini`), another pre-FC tick on a different roast (`artisan-04`, bean 165 °C,
RoR 12, human holding **100 / 10**). Same dangerous direction, milder magnitude: **heat 100 → 75,
fan 10 → 25.** So this is a **systematic `v4` pattern across roasts**, not one bad sample.

```json
{"phase":"roasting_pre_first_crack","roast_elapsed_seconds":382.0,"current_bean_temp_c":165.0,"current_env_temp_c":212.7,"bean_ror_c_per_min":12.0,"target_drop_temp_c":191.3,"target_development_percent":15.7,"profile_name":".artisan-fixtures/artisan-04","first_crack_detected":false}
```
```json
{"target_heat":75,"target_fan":25,"should_drop":false,"confidence":0.8,"rationale":"Pre-first-crack with a high bean RoR (~12 C/min) and max heat at 100% — act early to avoid overshoot. Reduce element to 75% ... increase fan to 25% ... if RoR remains >10 C/min after ~20–30 s, cut heat further and/or raise fan toward 30–35%. Not ready to drop."}
```

**New finding — lever-unit confusion (a #274 input).** The model's visible reasoning said
*"reduce the heat to around 70-80**°C**—I'll choose 75°C"* — but heat is a **0–100 % duty lever,
not a temperature**. The output (75) and the rationale ("75%") are right, so it self-corrected,
but the mental model is muddled. **#274's teaching prompt must state the lever semantics
explicitly** (heat / fan are 0–100 % duty, their effect + thermal lag — not °C setpoints).

## Third instance — the most damning: ~35 °C below FC (`artisan-08`)

Same model, pre-FC at **bean 137 °C** (RoR 12, human holding **100 / 20**) → **heat 100 → 70,
fan 20 → 35.** Bean 137 is **early drying / Maillard, ~35 °C below the FC band** — RoR 12 there is
*healthy*, not an overshoot risk. Cutting heat this early is the textbook setup for a **stall /
long bake**, nowhere near FC. The model again flags the hole it is digging in its own rationale:
*"if RoR collapses below ~3 C/min, recover heat by +10–15% to avoid long bake."*

```json
{"phase":"roasting_pre_first_crack","roast_elapsed_seconds":260.0,"current_bean_temp_c":137.0,"current_env_temp_c":198.0,"bean_ror_c_per_min":12.0,"target_drop_temp_c":193.0,"target_development_percent":16.6,"profile_name":".artisan-fixtures/artisan-08","first_crack_detected":false}
```
```json
{"target_heat":70,"target_fan":35,"should_drop":false,"confidence":0.85,"rationale":"Pre-first-crack with a high bean RoR (~12 C/min) and max heat currently applied: act early to blunt the RoR ... Reduce element to 70% ... raise fan moderately (35%) ... if RoR collapses below ~3 C/min, recover heat by +10–15% to avoid long bake."}
```

**The three instances span 137 / 165 / 168 °C** — the whole pre-FC range. So this is **not**
over-eager FC anticipation; it is a blanket **"high RoR → cut heat + open fan" reflex with no
phase awareness** — exactly the v4 gap. (A complete tally across all models/roasts will come from
the run's scorecard, via the heat-direction-disagreement metric on pre-FC ticks.)

## Use

- **#274** — a worked negative example for designing/validating the control teaching system
  prompt: the prompt must make "act pre-FC" wrong, not merely surface the phase label, **and**
  state the lever units explicitly (the unit confusion above).
- **#224** — a labelled negative-regression case for the corpus / replay harness: the
  contexted + gated loop must **not** reproduce this (no pre-FC fan raise, no momentum-killing
  heat cut before FC).

Cross-refs: #218 (the live bake this mirrors), D35 / D36 / D39, #228 / #274 / #224,
memory `operator-hottop-roast-profile` (fan low to FC), `operator-working-n8n-roast-system`
(pre-FC heat 100 / fan 30).
