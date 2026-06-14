# Advisor bake-off — real-roast replay scorecard (#172/#173, D20)

> **Read first — what these numbers mean.** The ground truth is a known-GOOD roast, *not* a provably optimal one. Every metric below measures **agreement with a known-good roast**, NOT absolute correctness: a capable model may legitimately differ from what the human did and still roast well, and high agreement is not proof of quality. Drop F1 = 1.0 means *matched this one good roast*, not *correct*. Use these as a quantitative aid to the operator's judgement (the advice samples + the latency gate), never a replacement for it.

Test set (known-good 7-Jun Hottop roasts): live-roast-2026-06-07/session-1, live-roast-2026-06-07/session-2
Drop = should_drop agreement over ticks (F1/precision/recall) + first-drop timing error (s and °C vs the real drop). Heat/Fan = MAE (percentage points) + directional agreement (did the model move the lever the way the human did). Latency = median per phase, FC tightest. NO auto-pick.

Confusion matrices below are derived purely from the per-tick replay data (no extra calls). The 2×2 drop matrix is consistent with the F1/P/R above but is heavily class-imbalanced — almost every tick is no-drop, so TN dominates; read it WITH the drop-timing error, never alone. The 3×3 heat-direction matrix (cut/hold/raise) is the more informative view of control behaviour and anticipatory-cut agreement.

## prompt_version = v2

### google/gemini-3.1-flash-lite (ultra-flash)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=1.0 P=1.0 R=1.0 timing=-0.4s/+0.0°C; heat MAE=10 dir=0.875; fan MAE=5.24 dir=0.725; latency pre=0.95s preFC=1.0s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=40     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    32 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.875 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.4s/+0.0°C; heat MAE=7.89 dir=0.892; fan MAE=7.89 dir=0.649; latency pre=0.88s preFC=0.98s FC=1.06s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=37     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    30 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.892 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast has experienced a severe, unexplained crash in bean temperature (from 193C to 148C in ~15 seconds), likely due to a sensor error or a massive environmental disturbance. Given the current bean temperature of 148C and the high environmental temperature, I am maintaining maximum heat to recover the momentum. Fan is kept low to prioritize conductive heat recovery.'
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'RoR is healthy (9.6 C/min) and the bean temperature is approaching the transition to first crack. Maintaining current heat and fan settings to keep momentum steady as we approach the end of the drying/Maillard phase. No changes needed yet.'
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'First crack occurred at ~541s. Development is very early (15s). RoR is healthy at ~10C/min. To reach a 15% development ratio (approx 85-90s total development time), I need to maintain a steady, gentle glide. Reducing heat to 70% and increasing fan to 50% will shift toward convective heat, preventing scorching while allowing for a controlled, extended development phase.'
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=40% fan=80% drop=False conf=0.9 — 'RoR is currently ~11°C/min, which is healthy for development. Total roast time is ~630s, and development time is ~45s (7% of total). To reach a 15% development ratio, I need ~95s total development time (50s more). I am reducing heat to 40% and increasing fan to 80% to manage the momentum and ensure a gentle, convective finish without stalling the roast. Drop is not yet indicated.'
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=0% fan=100% drop=False conf=0.9 — 'The roaster is currently in the preheating phase, preparing to charge the beans. The current environment temperature is 221°C, which is slightly above the recommended charge range (180-200°C). I am setting the heat to 0 and fan to 100 to cool the drum down to the target charge range before the beans are added.'
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With an RoR of ~11 C/min and bean temperature at 141 C, we are in a good position. I am maintaining full heat to ensure momentum through the drying/Maillard transition, but increasing the fan slightly to 20% to begin clearing moisture and preparing for the increased airflow needs as we approach first crack.'
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack has just begun. The RoR is healthy (9.28 C/min). To manage the development phase and ensure a smooth, controlled finish toward the 16% development target, I am reducing heat to prevent a spike and increasing fan to shift toward convective heat transfer, which will help maintain a steady, gentle development.'
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=20% fan=70% drop=False conf=0.95 — "Development is at ~10.7% (64s/599s). To reach the 16.2% target, I need about 33 more seconds of development. RoR is stable at 8.7°C/min. I will maintain low heat and moderate fan to gently coast toward the drop, ensuring the roast doesn't stall while avoiding a spike in RoR."

### openai/gpt-5.4-nano (ultra-flash)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=19.68 dir=0.175; fan MAE=13.63 dir=0.125; latency pre=1.4s preFC=1.52s FC=1.61s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=40     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    31 |     4 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.175 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=23.34 dir=0.081; fan MAE=15.61 dir=0.108; latency pre=1.37s preFC=1.48s FC=1.75s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=37     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    34 |     0 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.081 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=72% fan=65% drop=False conf=0.62 — 'Pre–first crack: current bean temp ~148 C with strongly negative RoR indicates active cooling/instability after an earlier overshoot; use decisive heat reduction to avoid further overshoot, while raising fan toward convective mode to stabilize bean temp rise and clear smoke/chaff. Not dropping since first crack not reached and development ratio target applies later.'
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=85% fan=35% drop=False conf=0.62 — 'Pre–first crack: bean temp is still ~146°C and RoR is rising but not near development; keep heat moderately high to maintain momentum, while increasing fan slightly for more even convective heating and smoke/chaff clearance. Do not drop since first crack not detected and duration target is for post-crack.'
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=55% fan=70% drop=False conf=0.74 — 'Post–first crack development is only ~15% (556.6s elapsed with 15.1s dev), near the 10–20% target, so don’t drop yet. Bean temp ~184°C is still below the 197°C guide and RoR is still high (~10°C/min), so cut heat substantially but increase fan toward more convective transfer to extend duration smoothly and avoid overshooting into dark/baked flavor.'
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=35% fan=75% drop=False conf=0.68 — 'Post-crack development target ~15%: you’re at ~45.2s/586.7s ≈7.7%, so duration is short and should not drop yet. Bean temp ~189°C with RoR ~11°C/min is running a bit hot; reduce heat decisively (accounting for thermal lag) while raising fan to more convective transfer to slow overshoot and even out heat. Fan+lower heat balance should stretch development toward the target without chasing the 197°C guide.'
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=85% fan=35% drop=False conf=0.66 — 'Preheating/approach to charge: bean is ~186°C with high RoR (~19°C/min) and no first crack yet. Reduce heat from max to avoid overshooting due to thermal lag, while raising fan to increase convective/evacuative transfer and prevent uneven, baked flavor as you move toward the 180–200°C guidance band. Keep running toward charge; do not drop (development ratio target not applicable yet).'
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=85% fan=25% drop=False conf=0.63 — 'Pre–first crack: bean temp is rising slowly (RoR ~11 C/min) and far from the 193 C guide; keep heat fairly high but begin coordinating airflow upward to reduce lag and keep heat transfer even as you approach the turning point. Fan increased modestly (more convective transfer) without starving drum heat.'
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=55% fan=70% drop=False conf=0.74 — 'Post–first crack with high bean RoR (~9.3 C/min). Bean is still below drop guide (178→) and you’re only ~16.2% into development, so prioritize duration: cut heat substantially to tame RoR (thermal lag), and raise fan to favor convective/evacuate smoke for even development rather than rushing to a temperature-based drop.'
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=35% fan=80% drop=False conf=0.74 — 'Post–first crack and development target ~16%: at ~600s elapsed with ~64s into development, you’re near the duration goal. Bean RoR remains high (~8.8°C/min), so reduce drum energy now (anticipating thermal lag) to prevent baking past the ~193°C guide, while raising fan to increase convective heat transfer and smoke/chaff evacuation for a controlled, even finish. Drop is deferred since development ratio is not clearly overshot and temp ceiling (~195°C+ bean-dependent) can be managed with the heat reduction.'

### openai/gpt-4.1-mini (ultra-flash)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10 dir=0.6; fan MAE=7.68 dir=0.325; latency pre=1.79s preFC=2.06s FC=1.9s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=40     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    14 |    21 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.6 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=5.92 dir=0.811; fan MAE=6.11 dir=0.432; latency pre=1.84s preFC=1.87s FC=2.03s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=37     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     7 |    27 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.811 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=100% fan=15% drop=False conf=0.9 — 'In the pre-first crack phase with a dropping bean RoR and current bean temperature at 148°C, maintain full heat (100%) to drive temperature rise. Slightly increase fan from 10% to 15% to improve convective heat transfer and evacuate chaff/smoke without cooling excessively. No drop now; wait for first crack detection or development phase initiation.'
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=95% fan=35% drop=False conf=0.9 — 'Pre-first crack phase with a bean RoR of 9.64 C/min is moderately high but stable. Current bean temperature is 146 C, climbing steadily. Slightly reducing heat from max to 95% will anticipate thermal lag and prevent overshooting while increasing fan slightly to 35% improves convective heat transfer and smoke evacuation. These coordinated adjustments support controlled temperature rise without risking baked flavor. Not ready to drop: no first crack detected, target development not yet relevant.'
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=60% fan=70% drop=False conf=0.9 — 'RoR is high at ~10 C/min during development with a short elapsed development time of 15s and development ratio ~2.7%. To extend development toward the target 15% ratio, reduce heat significantly and increase fan substantially to shift heat mode toward convective and lower RoR without stalling progress or risking baked flavor. Temperature at 184 C is below the target drop temp of 197 C, so no drop yet.'
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=50% fan=70% drop=False conf=0.9 — 'In development phase with development ratio around 7.7% (45.157/586.677), target ratio is 15%. Bean temp is 189C, below target_drop 197C, but RoR is relatively high (10.97 C/min). Reduce heat to 50% to slow rate, raise fan to 70% to increase convective cooling and avoid scorching, stretching development. Do not drop yet to reach target development ratio.'
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=90% fan=15% drop=False conf=0.9 — "In preheating phase with bean temp at 186°C and rising, heat is slightly reduced from max to avoid overshoot due to thermal lag, while fan is gently raised to 15 to enhance convective heat transfer and protect flavor. No drop is recommended as first crack hasn't occurred and development hasn't started."
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — "Pre-first crack, bean temperature is rising steadily with a healthy RoR of 11.1 C/min. To maintain an efficient rise and avoid stalling, maintain full heat and moderately increase fan from 10 to 25 to improve heat transfer and smoke/chaff evacuation. No drop yet as development hasn't started and temperature is well below target drop 193 C."
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=85% fan=45% drop=False conf=0.85 — 'Development phase just started (2.48s) with bean temp rising steadily and RoR high but not excessive. To extend development time toward the target 16.2% ratio, reduce heat moderately from full and increase fan to enhance convective heat transfer and avoid baking. No drop yet as development ratio is far from target and temperature is below 193C guide.'
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=15% fan=85% drop=False conf=0.9 — 'Development is about 10.7% of total roast time, near the target of 16.2%. Bean temperature is below the 193°C drop target but RoR is somewhat high (8.75°C/min). To stretch development and avoid overshooting temperature, lower heat decisively and increase fan to favor convective heat transfer and slow RoR while preserving flavor. Not yet time to drop.'

### anthropic/claude-opus-4.8-fast (ultra-flash)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=14.88 dir=0.625; fan MAE=5.41 dir=0.625; latency pre=2.39s preFC=2.96s FC=3.29s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=40     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    13 |    22 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.625 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10.39 dir=0.622; fan MAE=5.39 dir=0.649; latency pre=2.46s preFC=3.09s FC=2.83s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=37     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    14 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.622 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=80% fan=15% drop=False conf=0.3 — "The telemetry is inconsistent: roast_elapsed is ~0.2s (just charged) yet the recent samples show ~640s of history with bean temp peaking at 193C then falling sharply to 148C. This pattern — env temp steady ~223C while bean temp drops from 193 to 148 over the last ~25s — looks like the beans were just charged into the drum (the dramatic negative RoR of -29 C/min is the classic post-charge turnaround dip), OR a sensor/probe disturbance. Treating this as a fresh charge approaching the turning point: I'm keeping heat firm to drive recovery toward first crack but not full 100% (the prior full-blast appears to have caused an unstable spike). Fan stays low to retain energy for the climb. Do not drop — we are pre-first-crack with no development underway. Low confidence given the contradictory timestamps; please verify the probe and elapsed-time signal."
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=85% fan=40% drop=False conf=0.78 — "Pre-first-crack at 146C, ~5:20 in, with bean RoR ~9.6 C/min — a healthy, slightly declining ramp. Heat has been pinned at 100% with a low 30% fan, which risks a scorched/baked edge and traps chaff/smoke as we approach first crack (~195-205C env, bean ~196). I'm easing heat to 85% to begin a gentle RoR decline into the crack (anticipating thermal lag) and raising fan to 40% to shift toward more even convective transfer and clear smoke. Not near drop — keep developing."
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=50% fan=50% drop=False conf=0.8 — 'Just 15s into development at 184C with heat pinned at 100% and fan only 30% — bean RoR ~10C/min is too hot for a controlled development and will race past the temperature guide before reaching the duration target. At 556s total, a 15% development ratio needs roughly 100s of development, so we have plenty of time to stretch. Cut heat decisively from 100 to ~50 to tame RoR while keeping some momentum, and raise fan from 30 to ~50 to shift toward convective transfer, evening out heat and protecting flavor (avoiding scorched/baked notes). This coordinated move slows the climb so we hit the duration target around 195C rather than overshooting. Not dropping — only ~15s of development so far.'
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=40% fan=75% drop=False conf=0.78 — "Development is only 45s (~7.7% of total) — too early to drop; target is 15%, roughly 88s of development. Bean temp 189C is below the 197C guide. RoR is still ~11 C/min, which would carry us past 195C too fast and risk a dark/baked result. The prior heat cut to 60 already helped, but to stretch development I'm cutting heat decisively to 40 and raising fan to 75 to shift toward convective transfer, evening out the roast and evacuating smoke while easing RoR. Watch that RoR doesn't crash — reassess in the next 20-30s as we approach ~12-15% development."
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — "Preheating, ~10s before charge. Bean temp 186 C sits comfortably within the 180-200 C charge window, so the drum is ready. Keep heat at 100 and fan low (10) to bank energy for the cold-bean charge, when the thermal mass will drop bean temp sharply and we'll want maximum stored heat to drive the turnaround. No development consideration yet; do not drop."
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=85% fan=25% drop=False conf=0.78 — "Mid-roast at 141C, ~5min in, pre-first-crack with a healthy ~11 C/min bean RoR — momentum is good and we're approaching the Maillard-to-crack transition. Heat has been pinned at 100% with fan very low (10), which risks scorching the bean surface and uneven/baked development as we near first crack. I'm easing heat off slightly (to 85) ahead of crack to avoid a RoR spike and surface scorch — acting early for thermal lag — while raising fan to 25 to shift toward convective transfer, even out the heat, and start evacuating smoke/chaff. Still well below the drop guide (193C) with no crack yet, so do not drop."
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=60% fan=45% drop=False conf=0.82 — "First crack just hit (~2.5s into development) at 178C with heat pinned at 100% and a still-brisk bean RoR of ~9.3 C/min. To reach a ~16% development ratio we need roughly 90-100s of development from a ~535s roast, so I want a controlled, gentle climb — not a spike that races past the 193C guide and darkens the beans. Cut heat decisively from 100% to ~60% to bleed off the thermal-lag-driven surge that's still in the pipeline, and raise fan from 30% to ~45% to shift toward convective heat for even development and to clear smoke. This balance trims RoR without crashing it (avoiding a stall/bake). Hold for the drop until development time stretches into the target window."
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=25% fan=75% drop=False conf=0.78 — "Development is at 64s / ~10.7% of total roast — short of the 16.2% target, so keep developing. Bean temp 187C with RoR already eased to ~8.8 C/min after the earlier heat cuts (100→60→30); that's a healthy gentle glide. I'm trimming heat slightly more (30→25) and nudging fan up (70→75) to keep convective transfer dominant, evacuate smoke, and slow the climb so we stretch development toward ~14-16% before reaching the ~193C guide. Hold for now; expect to drop in roughly another 30-40s once the ratio lands in target and bean temp is near 190-193C. Don't rush the drop on temperature alone."

### meta-llama/llama-3.3-70b-instruct (speed-and-power)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=12.93 dir=0.85; fan MAE=5.12 dir=0.875; latency pre=2.19s preFC=2.62s FC=2.35s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=40     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    31 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.85 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=6.84 dir=0.865; fan MAE=3.68 dir=0.892; latency pre=2.55s preFC=2.26s FC=2.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=37     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     5 |    29 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.865 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.8 — 'Pre-first crack, high heat and low fan to promote radiant and conductive heating.'
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.8 — 'Maintain current heat and fan settings to continue roasting pre-first crack.'
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio target not yet reached, continue roasting with balanced heat and fan'
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=40% fan=80% drop=False conf=0.8 — 'Cut heat substantially and raise fan toward convective transfer to stretch development when post-crack RoR is high.'
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=80% fan=50% drop=False conf=0.8 — 'Preheating phase, increase heat and fan to reach target temperature'
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.8 — 'Pre-first crack, maintain high heat and low fan to continue temperature increase.'
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=50% fan=80% drop=False conf=0.8 — 'development ratio target not yet reached, continue roasting with balanced heat and fan'
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=20% fan=80% drop=False conf=0.8 — 'development ratio target'

### anthropic/claude-opus-4.8 (incumbent)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=15.85 dir=0.575; fan MAE=5.41 dir=0.575; latency pre=4.38s preFC=6.07s FC=6.76s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=40     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    15 |    20 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.575 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=11.53 dir=0.568; fan MAE=5.92 dir=0.622; latency pre=4.44s preFC=6.19s FC=6.12s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=37     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    16 |    18 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.568 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=80% fan=15% drop=False conf=0.4 — "The telemetry is internally inconsistent: roast_elapsed is ~0.2s yet recent samples show ~640s of history with bean temp peaking near 193C then dropping sharply to 148C. The most likely reading is that we are well into the roast and the bean probe just registered a sudden plunge (probe artifact, charge of a second batch, or a real heat-transfer disruption) — the -28.9 C/min RoR is almost certainly a sensor glitch rather than physical bean behavior. I'm treating this as pre-first-crack with no FC detected, so do NOT drop. Given the ambiguity, I'm pulling heat back from 100 to a moderate 80 to avoid scorching if the probe recovers and the real bean temp is actually near the high-180s/190s, while keeping enough energy to drive momentum if temps truly dropped. Fan nudged to 15 for slightly more even convective transfer and smoke/chaff evacuation. Low confidence — recommend verifying the bean probe seating before trusting the next recommendation."
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=80% fan=40% drop=False conf=0.78 — "Pre-first-crack at 146C, ~5:20 in, bean RoR ~9.6 C/min — healthy momentum. Heat has been pinned at 100% with fan only 30, so anticipating thermal lag I'm trimming heat to 80 now to avoid charging into first crack with too steep a RoR (which would compress development). I'm simultaneously raising fan to 40 to shift toward convective transfer for more even roasting and to start evacuating chaff/smoke as we approach the crack. Roughly 50C still to go to the ~197C guide, so plenty of runway — hold, don't drop. Reassess RoR after the heat cut shows up."
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=60% fan=45% drop=False conf=0.82 — 'Just 15s into development (~2.7% ratio) at 184C with bean RoR ~10 C/min and heat pinned at 100% — too hot, this will blow past the temperature guide before reaching a 10-20% development ratio. To hit ~15% I need roughly 80-100s of development, which means slowing the climb decisively now. Cutting heat from 100 to 60 anticipates thermal lag and tames RoR, while raising fan from 30 to 45 shifts toward convective transfer for even, smoke-free development — coordinated so RoR eases without crashing/baking. Re-evaluate as RoR settles; expect to drop around 193-197C once the duration target is in range.'
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=40% fan=75% drop=False conf=0.78 — 'Development is only 45s in (~7.7% ratio) — well short of the ~15% target (~104s, i.e. ~88s more). Bean temp 189C with RoR still ~11 C/min is too hot; at this pace it would race past the 197C guide and risk baking/over-darkening before hitting the duration target. The prior cut to 60/60 helped but RoR is still high, so cut heat decisively to 40 and raise fan to ~75 to shift toward convective transfer — this stretches development and evacuates smoke without crashing RoR. Hold; reassess as we approach ~10-15% development and ~195C.'
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=80% fan=20% drop=False conf=0.8 — "Preheating, charge imminent at ~186 C bean temp, within the 180-200 C charge window. Beans are not yet in. I'd ease heat from a full 100% toward 80% so that after charge the element isn't slamming energy into a cold mass with too much thermal lag, and bring fan up slightly to 20% for early chaff/smoke evacuation and more even convective transfer once beans hit the drum. Not dropping — the roast hasn't started. Reassess RoR immediately after charge when the temperature crashes and recovers."
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=85% fan=25% drop=False conf=0.74 — "Pre-first-crack at 141 C, ~5:12 in, bean RoR ~11 C/min and climbing steadily — healthy momentum toward first crack (~196 C env / ~150-160 C bean range coming). Heat has been pinned at 100% with fan at only 10%, a heat-heavy, low-convection setup that risks scorching and uneven roasting as we approach the crack. I'm trimming heat to 85% to begin anticipating thermal lag and avoid an RoR spike into first crack, while raising fan to 25% to shift toward convective transfer and start evacuating smoke/chaff for cleaner flavor. This keeps RoR robust (no stall) but more controlled. Hold for now — nowhere near drop."
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=70% fan=45% drop=False conf=0.8 — "First crack just hit (2.5s into development, bean 178C). We're at the very start of development and need to build duration toward ~16% of total roast time — at ~538s elapsed, a 16% ratio means roughly 90-100s of development, so no drop. Heat is pinned at 100% with fan only at 30% — that's too much radiant drive into the early development window and risks racing past the temp guide and baking. Cut heat decisively to 70% to ease RoR (currently ~9C/min) so development stretches, and raise fan to 45% to shift toward convective transfer for even heat, smoke/chaff evacuation, and flavor protection — keeping the heat:fan balance from crashing RoR. Reassess as we approach the 193C guide and the duration target."
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=25% fan=75% drop=False conf=0.78 — "Development is at ~64s of ~600s total (~10.7%), below the 16.2% target, so keep developing. Bean RoR has come down nicely to ~8.7 C/min and heat was already cut to 30%; I'm trimming heat slightly to 25% to ease RoR further and stretch development while nudging fan to 75 to favor convective, even heat transfer and clear smoke. At 187 C with the 193 C guide nearby, there's headroom to develop toward ~16% (roughly another 30-40s) without pushing dangerously dark. Hold the drop for now and reassess as the ratio climbs."

## prompt_version = v3

### google/gemini-3.1-flash-lite (ultra-flash)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=1.0 P=1.0 R=1.0 timing=-0.4s/+0.0°C; heat MAE=18.66 dir=0.625; fan MAE=8.66 dir=0.625; latency pre=1.05s preFC=1.09s FC=1.21s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=40     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    13 |    22 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.625 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=0.667 P=0.5 R=1.0 timing=-9.6s/-2.0°C; heat MAE=12.11 dir=0.73; fan MAE=8.68 dir=0.622; latency pre=1.07s preFC=1.18s FC=1.21s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=36     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    10 |    24 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.73 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The bean temperature has plummeted from 193C to 148C in under 20 seconds, indicating a massive sensor error or a catastrophic event (e.g., beans ejected or probe failure). Given the current telemetry, the roast is effectively stalled or the data is invalid. I am maintaining maximum heat to attempt recovery, but this roast is likely compromised.'
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=80% fan=50% drop=False conf=0.9 — 'RoR is healthy at 9.6 C/min as we approach first crack. Maintaining 100% heat is no longer appropriate as we approach the end of Maillard; reducing heat to 80% and increasing fan to 50% will help manage the momentum and transition smoothly into first crack without stalling.'
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'Development is at 15.1s out of a total roast time of 556.6s (2.7%). To reach a 15% development ratio, we need ~83s of development time. We are currently at 184C with a healthy RoR. Maintaining 100% heat will likely push the temperature too high before the development target is met. Reducing heat to 60% and increasing fan to 60% will manage the RoR and transition to convective heat to ensure even development.'
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=60% fan=60% drop=False conf=0.95 — 'Current development ratio is ~7.7% (45s / 586s). To reach the 15% target, we need approximately 43 more seconds of development. RoR is currently 11C/min, which is appropriate for a controlled finish. Maintaining current heat/fan settings to allow a steady, gentle climb toward the target development time. Will monitor for the 15% mark to drop.'
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=0% fan=50% drop=False conf=1.0 — 'The drum is currently at 221°C, which is above the charge guidance range (180-200°C). I am reducing heat to 0% and increasing fan to 50% to accelerate cooling toward the target charge range. Ready to charge once the environment temperature stabilizes within the 180-200°C band.'
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'RoR is healthy at 11C/min, but we are approaching the Maillard phase. Increasing fan to 30% will shift heat transfer toward convection to ensure even development and prepare for the upcoming crack, while maintaining 100% heat to keep the momentum steady before the inevitable pre-crack heat reduction.'
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack just started. Development is at 0.5%. Need to reduce heat and increase fan to manage the RoR and ensure a controlled development phase. Target development time is ~87s (16.2% of 538s). Reducing heat to 60% and increasing fan to 60% to shift to convective heat and prevent a spike.'
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=0% fan=70% drop=False conf=0.95 — 'Current development ratio is ~10.7%. To reach the 16.2% target, we need ~32 more seconds of development. RoR is stable at 8.7C/min. Reducing heat to 0% and maintaining fan at 70% will allow a gentle, controlled finish to the target ratio without overshooting the temperature. Ready to drop in ~30-35 seconds.'

### openai/gpt-5.4-nano (ultra-flash)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=0.5 P=0.333 R=1.0 timing=-50.4s/-8.0°C; heat MAE=22.44 dir=0.075; fan MAE=16.71 dir=0.1; latency pre=1.59s preFC=1.6s FC=1.62s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=2      |  TN=38     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    35 |     0 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.075 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=0.0 P=0.0 R=0.0 timing=-101.3s/-15.0°C; heat MAE=21.5 dir=0.135; fan MAE=14.03 dir=0.135; latency pre=1.58s preFC=1.49s FC=1.61s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=1      |  TN=36     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    32 |     2 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.135 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=15% fan=70% drop=False conf=0.62 — 'Pre-first-crack: bean temp has plunged from ~190–193°C down to ~148°C after charging (thermal lag/charge dunk). With RoR currently strongly negative (~-28.9°C/min), this indicates the beans are still recovering and you need early, decisive steering rather than timid heat trims. Cut heat substantially (from prior 100%) to avoid overshooting once lag catches up, and raise fan to ~70% to increase convective/evaporative transfer and stabilize the climb smoothly toward ~first-crack range. Hold/not drop in preheat-stage behavior; continue until bean temp stabilizes in the approach band.'
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=88% fan=45% drop=False conf=0.66 — 'Pre–first crack: beans at 146°C and still climbing; RoR is relatively high and not yet eased. Because of thermal lag, cut heat decisively from 100% and shift toward more convective transfer to smooth the RoR decline. Keep should_drop false in this phase and aim to hold a steady climb toward the ~197°C region without a stall.'
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=35% fan=70% drop=False conf=0.67 — 'Post–first-crack development is on track (15.12s / 556.64s ≈ 2.7% from charge, well below the 15% target), and bean temp is still rising at ~10.0°C/min. Cut heat substantially to avoid overshooting while raising fan toward convective transfer to shape RoR and broaden development without baking; drop only once the development-ratio target is met, so not yet.'
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=45% fan=72% drop=True conf=0.72 — 'Post–first crack development: elapsed since charge ~45s of ~586s total (development ratio ≈7.7% so you’re a bit ahead of the ~10–20% sweet spot but already at the target zone for exiting). Bean temp ~189C with high RoR (~11C/min) suggests heat is still driving fast rise; cut heat substantially and raise fan to keep it from baking/overextending while finishing the last fraction. Dropping now is decisive to match the development-duration goal rather than chasing the 197C guide.'
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=78% fan=18% drop=False conf=0.74 — 'Preheat goal is to reach/hold the 180–200\u202f°C charge band once the drum stabilizes. Beans are already at ~186\u202f°C (within band) while drum/env is ~221\u202f°C and still climbing with high RoR, so reduce heat decisively to prevent overshoot from thermal lag. Keep fan modest to maintain even convective transfer and chaff/smoke evacuation without overcooling. Hold (no drop) until charge is clearly stable in-band.'
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=92% fan=16% drop=False conf=0.62 — 'Pre–1st-crack: beans are ~141°C and still climbing with fairly high RoR (~11.1°C/min); keep heat strong but begin a controlled, slightly earlier reduction to avoid later RoR being too high once thermal lag catches up. Increase fan modestly to support steadier convective transfer and smoke/chaff evacuation while not over-driving airflow (to avoid RoR stalling). Hold should_drop=false during this stage.'
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=35% fan=75% drop=True conf=0.66 — 'Post–first crack with very small development timer (only ~2.5s into development; development ratio far below target ~16%). RoR is still high (~9.3°C/min), so anticipate thermal lag by cutting heat substantially and shifting to convective airflow (higher fan) to control RoR and prevent overshoot/baking. Drop decisively because ratio target is not met yet would be incorrect; however development_elapsed is tiny—likely just after FC detection—so defer drop and keep steering. Recommend should_drop=false.'
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=30% fan=80% drop=False conf=0.64 — 'Post–first-crack development: development ratio = 64.17s / 599.74s ≈ 10.7%, below target 16.2%, so don’t drop yet. Bean temp (187°C) is below the 193°C guide and RoR is fairly high (8.75°C/min), indicating heat still feeding fast enough; cut heat substantially but shift to higher fan for controlled convective transfer to stretch development without driving too hot. Reassess for drop once development ratio approaches ~16%.'

### openai/gpt-4.1-mini (ultra-flash)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=1.0 P=1.0 R=1.0 timing=-0.4s/+0.0°C; heat MAE=24.39 dir=0.275; fan MAE=13.54 dir=0.25; latency pre=1.91s preFC=2.04s FC=1.86s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=40     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    27 |     8 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.275 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=0.667 P=0.5 R=1.0 timing=-9.6s/-2.0°C; heat MAE=24.47 dir=0.243; fan MAE=14.87 dir=0.243; latency pre=2.01s preFC=2.34s FC=2.54s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=36     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    28 |     6 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.243 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=70% fan=30% drop=False conf=0.9 — 'Bean temperature dropped significantly at charge with a steep negative RoR indicating thermal lag. To assist early climb and smooth RoR decline, reduce heat from max to 70% to prevent overshoot and raise fan to 30% to promote convective heat transfer, preventing stall or baked flavor pre-first crack.'
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=70% fan=40% drop=False conf=0.9 — 'In drying/maillard phase with bean RoR declining but still quite high at 9.64°C/min. Current heat at 100% is risking overshoot and possible stall later. Recommended cutting heat decisively to 70% to avoid flattening RoR, and increasing fan moderately to 40% to improve convective heat transfer and chaff removal. No drop yet as first crack not detected and roast still developing.'
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=65% fan=50% drop=False conf=0.9 — 'In development phase with 15% development ratio target, current ratio is just about 2.7%. Bean temperature near 184°C, RoR is still moderate. To meet development time goal, reduce heat substantially and raise fan to shift heat mode for gentle, even convection and slow down temp rise. Too early to drop; focus on controlling development duration and preventing overshoot on temp.'
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=25% fan=80% drop=False conf=0.9 — 'Development phase with 7.7% development ratio, high RoR (~11 C/min) and bean temp (189 C), approaching target drop temp (197 C). Cut heat substantially and increase fan to shift to convective heat and slow development to stretch development duration to meet 15% target. Not ready to drop yet.'
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=90% fan=10% drop=False conf=0.9 — 'Bean temp is stable within the charge band (180-200°C) and rising steadily; maintain heat slightly reduced from max to hold temp steady. Fan at 10% ensures stable convection without rapid cooling. Ready to charge once temp is stable in band.'
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=80% fan=30% drop=False conf=0.85 — 'Bean temperature is climbing steadily but the rate of rise (RoR) is still relatively high at 11 C/min. To smoothly decline RoR toward first crack without stalling or baking, reduce heat decisively from full 100% to about 80% to prevent overshoot. Increase fan from 10% to 30% to shift toward more convective heat transfer and support even drying. No drop recommended; focus on managing RoR decline with early coordinated heat and fan adjustment.'
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=40% fan=70% drop=False conf=0.9 — 'This is very early in development with a high bean RoR (~9.3 C/min) and temperature (178C rising). To extend development time and avoid pushing bean temp too fast past the target (~193C), cut heat substantially and raise fan to promote convective cooling and slow roasting. Not yet ready to drop: development time is only 2.48s and development ratio is far below target (~0.46%).'
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=15% fan=80% drop=False conf=0.9 — 'Beans are at 187C, below target drop 193C, development time is 64s with a target development percentage of 16.2%. Rate-of-rise is still high (8.7C/min), so reduce heat significantly to slow temperature rise and increase fan to shift toward convective heat, extending development time and avoiding overshooting temperature. No drop yet; focus on hitting development ratio target.'

### anthropic/claude-opus-4.8-fast (ultra-flash)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=21.34 dir=0.45; fan MAE=7.07 dir=0.4; latency pre=2.26s preFC=2.79s FC=2.47s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=40     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    20 |    15 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.45 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=18.29 dir=0.432; fan MAE=6.84 dir=0.405; latency pre=2.33s preFC=2.82s FC=2.66s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=37     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    21 |    13 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.432 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=100% fan=15% drop=False conf=0.82 — "This is the post-charge dunk: bean temp has dropped from ~193 to 148 C as cool beans hit the drum, hence the steeply negative bean RoR. We're at the very start of drying with environment temp ~223 C. Hold heat at full to drive recovery and re-establish a climbing bean temp through the turning point; keep fan low (15) to favor conductive/radiant energy into the charge while still evacuating early chaff and smoke. Once the bean temp bottoms out and RoR turns positive, I'll begin the early, decisive heat cuts to shape a smoothly declining RoR toward first crack. No drop in drying."
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=70% fan=40% drop=False conf=0.8 — "We're ~5:20 into drying/Maillard at 146C bean, climbing toward first crack with heat pinned at 100% and fan low at 30. Bean RoR is ~9.6 C/min — reasonable but heat at full power will keep pushing it and risks a flick/spike into FC. Because of thermal lag, cut heat now and decisively to 70% to set up a smooth RoR decline into first crack rather than reacting late. Bump fan to 40 to shift toward convective transfer for even heating and to start clearing chaff/smoke as we approach the crack. Hold this and watch for FC; don't let RoR flatten into a stall."
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=60% fan=45% drop=False conf=0.82 — "Just 15s into development (ratio ~2.7%, target 15%). Bean at 184C with RoR ~10C/min on full heat — that's too hot to stretch development and will run to drop temp far too fast. Cut heat decisively from 100 to ~60 and raise fan 30->45 to shift toward convective transfer, easing RoR so development can extend toward the 15% ratio (~85s+ to go). Far too early to drop — hold and let the duration build."
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=45% fan=70% drop=False conf=0.8 — "Development is only ~7.7% (45s of 587s) — short of the 15% target. Post-FC RoR is still ~11 C/min, too hot to stretch cleanly. Cut heat to 45 and raise fan to 70 to shift toward convective transfer, ease the RoR, and protect flavor while extending development. At 189 C there's room before darkening; hold and re-check as the ratio climbs toward target."
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=60% fan=15% drop=False conf=0.82 — 'Drum is at 186 C, already inside the 180-200 C charge band, but heat is pinned at 100% and the bean temp is still climbing ~19 C/min — it will overshoot the band if held. Cut heat decisively to ~60% now (thermal lag means the rise will continue briefly) to arrest the climb and let it settle near mid-band. Nudge fan to 15 to even out drum heat and stabilize. Hold and let it stabilize for ~30-60s; ready to charge once steady in band (~185-190 C). Do not charge while still ramping.'
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=70% fan=25% drop=False conf=0.78 — "At ~5:12, BT 141C and still on 100% heat / 10% fan. Bean RoR is a healthy ~11 C/min but you're approaching the Maillard-to-FC transition, and with thermal lag full heat now will overshoot and spike the RoR right into first crack. Cut heat decisively to ~70% to begin the smooth RoR decline ahead of the lag, and raise fan to ~25% to shift toward convective transfer, even out the heat, and start clearing smoke/chaff. Do not drop — pre-FC. Watch for a flick-up; if RoR climbs further trim heat again rather than wait."
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=70% fan=45% drop=False conf=0.84 — 'First crack just began (~2s in, ratio <1%). Heat is pinned at 100% with low fan 30% and bean RoR ~9 C/min — too hot for a controlled development. Cut heat decisively to 70% and lift fan to 45% to shift toward convective transfer, ease the RoR, and evacuate smoke. Target ratio ~16% means developing roughly 80-90s; far too early to drop. Bean temp 178 C with room before the 193 C guide. Hold and let development stretch.'
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=25% fan=75% drop=False conf=0.8 — 'Development is ~64s into a ~600s roast = ~10.7% ratio, short of the 16.2% target. Bean temp 187C with a controlled ~8.8 C/min RoR — good, healthy decline. Hold a low heat (25) and keep fan high (75) for convective, even development without spiking RoR. Continue developing toward the ratio target; expect to drop in roughly another 30-40s as the ratio approaches ~15-16% (around 192-195C). Not yet.'

### meta-llama/llama-3.3-70b-instruct (speed-and-power)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=1.0 P=1.0 R=1.0 timing=-0.4s/+0.0°C; heat MAE=26.1 dir=0.225; fan MAE=10.98 dir=0.275; latency pre=2.37s preFC=2.3s FC=2.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=40     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    29 |     6 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.225 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=0.667 P=0.5 R=1.0 timing=-39.6s/-6.0°C; heat MAE=23.16 dir=0.27; fan MAE=8.82 dir=0.324; latency pre=2.91s preFC=2.33s FC=2.21s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=36     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    27 |     7 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.27 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=100% fan=50% drop=False conf=0.8 — 'Early in the roast, high heat and moderate fan to balance convective and radiant heat transfer.'
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=80% fan=40% drop=False conf=0.8 — 'reduce heat to manage RoR decline and increase fan for convective transfer'
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=80% fan=50% drop=False conf=0.8 — 'Development phase, adjusting heat and fan to maintain a steady rate of rise and reach the target development ratio.'
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=50% fan=80% drop=False conf=0.8 — 'development phase, reduce heat and increase fan to stretch development time'
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=80% fan=20% drop=False conf=0.8 — 'preheat: reduce heat to stabilize drum temperature within charge guidance band'
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=80% fan=20% drop=False conf=0.8 — 'reduce heat to manage RoR decline and increase fan for convective transfer'
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=50% fan=50% drop=False conf=0.8 — 'Reduce heat and increase fan to stretch development time and prevent over-roasting.'
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=0% fan=100% drop=True conf=0.9 — 'Development ratio target met, dropping beans to prevent over-development.'

### anthropic/claude-opus-4.8 (incumbent)
- live-roast-2026-06-07/session-1 (truth DTR 15.0%, 41/41 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=21.59 dir=0.425; fan MAE=7.07 dir=0.4; latency pre=4.37s preFC=5.61s FC=4.79s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=40     |
    (total ticks=41; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    21 |    14 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=40; diagonal agreement=0.425 — the more informative control-behaviour view)
- live-roast-2026-06-07/session-2 (truth DTR 16.2%, 38/38 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=19.34 dir=0.378; fan MAE=6.97 dir=0.432; latency pre=4.38s preFC=5.65s FC=4.63s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=37     |
    (total ticks=38; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |    23 |    11 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=37; diagonal agreement=0.378 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - live-roast-2026-06-07/session-1 charge @ 639s bean=148.0°C ror=-28.898 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.8 — "Fresh charge just detected (elapsed ~0s): the bean probe shows the expected charge dunk — falling from ~193C toward 148C as cold beans hit the drum. We're at the very start of drying, so keep heat full at 100 to drive recovery decisively through the thermal lag and establish a strong initial RoR. Nudge fan from 10 to 20 to improve convective evenness and evacuate early chaff/smoke without crashing the rebuild of bean temp. Watch for the turning point, then we'll begin the early, drastic heat cuts well before RoR misbehaves as we approach Maillard/first crack. No drop in drying."
    - live-roast-2026-06-07/session-1 maillard @ 960s bean=146.0°C ror=9.64 real(heat/fan)=100/30: model heat=75% fan=40% drop=False conf=0.78 — 'Pre-FC at 146C, ~5:20 in, running heat pinned at 100% with low fan 30%. Bean RoR is only ~9.6 C/min and easing — but with full heat still applied and thermal lag, energy is banked and we risk a late RoR spike or scorched/baked flavor from too little convection. Cut heat decisively to 75% now (ahead of the lag) and lift fan to 40% to shift toward convective transfer, even out the bean pile, and evacuate smoke/chaff. This sets up a smooth RoR decline into first crack rather than a flatten or flick-up. Hold for the next minute and reassess as we approach FC.'
    - live-roast-2026-06-07/session-1 first-crack @ 1196s bean=184.0°C ror=9.951 real(heat/fan)=100/30: model heat=75% fan=45% drop=False conf=0.84 — 'Just 15s into development (~2.7% ratio); need to reach ~15% near 83s of development. Bean is 184C with RoR ~10C/min, still on full heat at 100% — that will run away past target before development matures. Cut heat decisively to 75 and lift fan to 45 to shift toward convective transfer, evacuate smoke, and ease RoR so development stretches without darkening too fast. Hold; reassess as ratio builds toward target.'
    - live-roast-2026-06-07/session-1 development @ 1226s bean=189.0°C ror=10.971 real(heat/fan)=60/60: model heat=45% fan=70% drop=False conf=0.8 — 'Development is only ~45s into a ~587s roast, a ratio of ~7.7% — well short of the 15% target, so keep going. Post-crack bean RoR is still ~11 C/min, too hot to stretch development cleanly. Cut heat to 45 and raise fan to 70 to shift toward convective transfer, ease the RoR, and protect flavor. At 189 C you have room to develop toward and modestly past the 197 C guide while building duration. Reassess for drop as the ratio approaches ~12-15%.'
    - live-roast-2026-06-07/session-2 charge @ 486s bean=186.0°C ror=18.981 real(heat/fan)=100/10: model heat=60% fan=15% drop=False conf=0.82 — "Drum is at 186 C, already inside the 180-200 C charge band, but heat is pinned at 100% and bean temp is still climbing ~19 C/min — left alone it will overshoot the band. Cut heat decisively to ~60% now (thermal lag means the current rise will continue for a bit) to arrest the climb and let it settle near the band midpoint. Nudge fan to 15 for a touch more convective evenness and to evacuate any chaff. Not ready to charge this second — hold ~30-60s for the temperature to stabilize, then charge once it's steady around 190-195 C."
    - live-roast-2026-06-07/session-2 maillard @ 809s bean=141.0°C ror=11.076 real(heat/fan)=100/10: model heat=75% fan=25% drop=False conf=0.78 — "At ~5:12, bean temp 141C climbing toward first crack with heat pinned at 100% and fan at only 10%. That's a lot of stored energy in the element with minimal convective transfer — classic setup for an RoR spike into FC and scorched/baked flavor. Cut heat decisively now to anticipate thermal lag (a trim won't bite in time), and raise fan to shift toward convective heat for even roasting and to start evacuating chaff/smoke. RoR is currently a healthy ~11 C/min and easing; this keeps the decline smooth into first crack without stalling. Not dropping — pre-FC."
    - live-roast-2026-06-07/session-2 first-crack @ 1034s bean=178.0°C ror=9.283 real(heat/fan)=100/30: model heat=70% fan=45% drop=False conf=0.82 — 'First crack just hit (2.5s in, ratio ~0.5%) — far from the 16.2% target. Heat is pinned at 100% with post-crack RoR still ~9 C/min, which would race through development. Cut heat decisively to 70% to tame the rise and raise fan to 45 for convective evenness and smoke/chaff evacuation, stretching development. Hold; reassess as bean temp approaches the 193 C guide and the ratio builds toward target.'
    - live-roast-2026-06-07/session-2 development @ 1096s bean=187.0°C ror=8.754 real(heat/fan)=30/70: model heat=35% fan=70% drop=False conf=0.8 — 'Development is only ~64s (10.7% ratio) vs 16.2% target, so keep developing. Bean RoR has eased nicely to ~8.8 C/min at 187 C — already cut hard to 30%. Hold heat low (35%) with fan at 70% to sustain convective transfer and a gentle climb without spiking RoR. Aim to drop around 90-100s development (~16%), likely near 191-193 C. Not yet.'