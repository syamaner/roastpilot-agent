# Advisor bake-off — real-roast replay scorecard (#172/#173, D20)

> **Read first — what these numbers mean.** The ground truth is a known-GOOD roast, *not* a provably optimal one. Every metric below measures **agreement with a known-good roast**, NOT absolute correctness: a capable model may legitimately differ from what the human did and still roast well, and high agreement is not proof of quality. Drop F1 = 1.0 means *matched this one good roast*, not *correct*. Use these as a quantitative aid to the operator's judgement (the advice samples + the latency gate), never a replacement for it.

Test set (known-good 7-Jun Hottop roasts): .artisan-holdout/artisan-29, .artisan-holdout/artisan-30, .artisan-holdout/artisan-31, .artisan-holdout/artisan-32, .artisan-holdout/artisan-33, .artisan-holdout/artisan-34, .artisan-holdout/artisan-35, .artisan-holdout/artisan-36, .artisan-holdout/artisan-37, .artisan-holdout/artisan-38, .artisan-holdout/artisan-39, .artisan-holdout/artisan-40, .artisan-holdout/artisan-41, .artisan-holdout/artisan-42, .artisan-holdout/artisan-43, .artisan-holdout/artisan-44, .artisan-holdout/artisan-45, .artisan-holdout/artisan-46, .artisan-holdout/artisan-47
Drop = should_drop agreement over ticks (F1/precision/recall) + first-drop timing error (s and °C vs the real drop). Heat/Fan = MAE (percentage points) + directional agreement (did the model move the lever the way the human did). Latency = median per phase, FC tightest. NO auto-pick.

Confusion matrices below are derived purely from the per-tick replay data (no extra calls). The 2×2 drop matrix is consistent with the F1/P/R above but is heavily class-imbalanced — almost every tick is no-drop, so TN dominates; read it WITH the drop-timing error, never alone. The 3×3 heat-direction matrix (cut/hold/raise) is the more informative view of control behaviour and anticipatory-cut agreement.

## prompt_version = v2

### google/gemini-3.1-flash-lite (ultra-flash)
- .artisan-holdout/artisan-29 (truth DTR 14.1%, 24/24 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=9.17 dir=0.957; fan MAE=7.71 dir=0.652; latency pre=1.23s preFC=1.21s FC=1.21s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     0 |    17 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.957 — the more informative control-behaviour view)
- .artisan-holdout/artisan-30 (truth DTR 16.5%, 34/34 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10.59 dir=0.848; fan MAE=11.47 dir=0.485; latency pre=0.91s preFC=1.1s FC=1.36s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=33     |
    (total ticks=34; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     5 |    26 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=33; diagonal agreement=0.848 — the more informative control-behaviour view)
- .artisan-holdout/artisan-31 (truth DTR 14.9%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=-2.0s/-0.7°C; heat MAE=2.5 dir=0.96; fan MAE=11.15 dir=0.52; latency pre=1.4s preFC=1.26s FC=1.17s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.96 — the more informative control-behaviour view)
- .artisan-holdout/artisan-32 (truth DTR 16.9%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.31 dir=0.88; fan MAE=11.54 dir=0.6; latency pre=1.04s preFC=1.19s FC=1.41s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    19 |     0 |
    |        raise |     2 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-holdout/artisan-33 (truth DTR 18.3%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.42 dir=0.957; fan MAE=8.54 dir=0.522; latency pre=1.29s preFC=1.07s FC=1.07s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    17 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=23; diagonal agreement=0.957 — the more informative control-behaviour view)
- .artisan-holdout/artisan-34 (truth DTR 20.9%, 30/30 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=14.67 dir=0.828; fan MAE=7.67 dir=0.621; latency pre=1.0s preFC=1.13s FC=1.19s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=29     |
    (total ticks=30; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     3 |    20 |     2 |
    |        raise |     0 |     0 |     1 |
    (n=29; diagonal agreement=0.828 — the more informative control-behaviour view)
- .artisan-holdout/artisan-35 (truth DTR 13.7%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.8 dir=0.958; fan MAE=13.6 dir=0.375; latency pre=1.33s preFC=1.18s FC=1.22s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    19 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.958 — the more informative control-behaviour view)
- .artisan-holdout/artisan-36 (truth DTR 17.6%, 27/27 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=3.7 dir=0.923; fan MAE=13.89 dir=0.423; latency pre=0.93s preFC=1.16s FC=1.11s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=26     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     1 |    19 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=26; diagonal agreement=0.923 — the more informative control-behaviour view)
- .artisan-holdout/artisan-37 (truth DTR 14.0%, 25/25 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=10.4 dir=0.917; fan MAE=8.8 dir=0.5; latency pre=1.07s preFC=1.14s FC=1.32s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    18 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.917 — the more informative control-behaviour view)
- .artisan-holdout/artisan-38 (truth DTR 13.3%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=3.85 dir=0.92; fan MAE=14.81 dir=0.44; latency pre=1.33s preFC=1.12s FC=1.2s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    20 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.92 — the more informative control-behaviour view)
- .artisan-holdout/artisan-39 (truth DTR 12.4%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=3.85 dir=0.92; fan MAE=10.19 dir=0.6; latency pre=1.32s preFC=1.15s FC=1.27s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.92 — the more informative control-behaviour view)
- .artisan-holdout/artisan-40 (truth DTR 14.2%, 26/26 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=7.31 dir=0.96; fan MAE=12.88 dir=0.44; latency pre=0.97s preFC=1.13s FC=1.29s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     0 |    19 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.96 — the more informative control-behaviour view)
- .artisan-holdout/artisan-41 (truth DTR 13.5%, 29/29 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=1.72 dir=0.929; fan MAE=10.86 dir=0.536; latency pre=1.03s preFC=1.15s FC=1.5s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=28     |
    (total ticks=29; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    23 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=28; diagonal agreement=0.929 — the more informative control-behaviour view)
- .artisan-holdout/artisan-42 (truth DTR 12.7%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.54 dir=0.92; fan MAE=4.04 dir=0.84; latency pre=1.05s preFC=1.19s FC=1.23s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.92 — the more informative control-behaviour view)
- .artisan-holdout/artisan-43 (truth DTR 10.5%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=2.4 dir=0.958; fan MAE=8 dir=0.5; latency pre=1.36s preFC=1.24s FC=1.21s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.958 — the more informative control-behaviour view)
- .artisan-holdout/artisan-44 (truth DTR 13.2%, 28/28 ok): drop F1=0.0 P=0.0 R=0.0 timing=never-dropped; heat MAE=1.79 dir=0.963; fan MAE=13.04 dir=0.444; latency pre=1.0s preFC=1.18s FC=1.22s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=0      |  FN=1      |
    | real: hold |  FP=0      |  TN=27     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     1 |    20 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=27; diagonal agreement=0.963 — the more informative control-behaviour view)
- .artisan-holdout/artisan-45 (truth DTR 12.3%, 29/29 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.86 dir=0.893; fan MAE=10.86 dir=0.536; latency pre=0.89s preFC=1.15s FC=1.12s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=28     |
    (total ticks=29; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    22 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=28; diagonal agreement=0.893 — the more informative control-behaviour view)
- .artisan-holdout/artisan-46 (truth DTR 13.6%, 33/33 ok): drop F1=0.667 P=0.5 R=1.0 timing=-4.0s/-0.6°C; heat MAE=9.09 dir=0.875; fan MAE=12.73 dir=0.469; latency pre=1.05s preFC=1.14s FC=1.14s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=31     |
    (total ticks=33; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    25 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=32; diagonal agreement=0.875 — the more informative control-behaviour view)
- .artisan-holdout/artisan-47 (truth DTR 12.8%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.92 dir=0.96; fan MAE=14.42 dir=0.4; latency pre=1.28s preFC=1.17s FC=1.18s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    20 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=25; diagonal agreement=0.96 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-holdout/artisan-29 charge @ 60s bean=110.0°C ror=-1.0 real(heat/fan)=100/40: model heat=100% fan=40% drop=False conf=0.9 — 'The bean RoR has crashed to negative despite 100% heat. This indicates a stall or a measurement error, but given the environment temperature is rising, the bean probe is likely experiencing a lag or a temporary dip. I will maintain maximum heat to recover momentum and keep the fan at 40% to ensure sufficient heat transfer without cooling the drum prematurely.'
    - .artisan-holdout/artisan-29 maillard @ 391s bean=136.0°C ror=11.7 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With a bean RoR of 11.7 C/min and current temperature of 136 C, we are in a good position. I am maintaining full heat to ensure momentum through the end of the drying/Maillard phase, while slightly increasing fan to 20% to begin preparing for better airflow management as we approach first crack.'
    - .artisan-holdout/artisan-29 first-crack @ 601s bean=179.7°C ror=13.7 real(heat/fan)=80/0: model heat=60% fan=40% drop=False conf=0.9 — 'First crack has just occurred. RoR is healthy (13.7 C/min). To manage the development phase and avoid a runaway RoR, I am reducing heat to 60% and increasing fan to 40% to shift toward convective heat and ensure steady, controlled development toward the 14% target.'
    - .artisan-holdout/artisan-29 development @ 661s bean=193.0°C ror=13.3 real(heat/fan)=10/90: model heat=10% fan=90% drop=False conf=0.95 — 'Development time is 62s out of 611s total (10.1%). To reach the 14.1% target, I need ~24 more seconds of development. RoR is 13.3 C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop around 197-198C in about 25-30 seconds.'
    - .artisan-holdout/artisan-30 charge @ 212s bean=124.5°C ror=25.5 real(heat/fan)=0/10: model heat=0% fan=10% drop=False conf=0.9 — 'The roaster is currently in the preheating phase, waiting to charge. The environment temperature is approaching the target charge range (180-200C). I am setting heat to 0 and fan to 10 to maintain stability while waiting for the drum to reach the optimal charge temperature.'
    - .artisan-holdout/artisan-30 maillard @ 603s bean=137.0°C ror=10.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "RoR is slowing down (10.3 C/min) as we approach first crack. The environment temperature is stable, but we need to prepare for the exothermic reaction. I am increasing the fan to 25% to begin transitioning to convective heat and clearing moisture, while maintaining full heat to ensure we don't stall before first crack."
    - .artisan-holdout/artisan-30 first-crack @ 873s bean=180.0°C ror=8.0 real(heat/fan)=100/30: model heat=60% fan=50% drop=False conf=0.9 — 'First crack occurred at 645s. Development is very early (13s). Current RoR is 8C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 60% to moderate the momentum and increasing fan to 50% to shift toward convective heat, ensuring even development and smoke evacuation. I will aim for a total roast time of ~780-800s to hit the 16.5% development target.'
    - .artisan-holdout/artisan-30 development @ 933s bean=187.3°C ror=7.3 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'Development is at 73s out of 718s total (approx 10.2%). To reach the 16.5% target, I need about 143s total development time, meaning ~70s more. RoR is 7.3 C/min, which is healthy. I am cutting heat to 60% and increasing fan to 60% to transition to convective heat, ensuring a smooth, controlled finish without stalling, while aiming to drop near the 198C target in about 60-70 seconds.'
    - .artisan-holdout/artisan-31 charge @ 0s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 154°C, which is below the target charge range of 180°C-200°C. I am maintaining heat and fan at 0 to allow the roaster to continue climbing toward the target charge temperature.'
    - .artisan-holdout/artisan-31 maillard @ 362s bean=134.7°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily at 134.7°C with a healthy RoR of 12°C/min. I am maintaining full heat to ensure sufficient momentum approaching first crack. I am keeping the fan at 0% to maximize conductive heat transfer and build energy in the drum, but I will prepare to introduce airflow once the bean temperature approaches 150-160°C to manage the transition into the Maillard phase.'
    - .artisan-holdout/artisan-31 first-crack @ 632s bean=181.0°C ror=9.0 real(heat/fan)=100/60: model heat=100% fan=60% drop=False conf=0.9 — 'First crack occurred at 610s. Current development time is 15s (2.4% of total time). To reach a 14.9% development ratio, I need a total roast time of ~715s, meaning ~105s of development. RoR is currently 9C/min, which is healthy. I am maintaining heat at 100% and fan at 60% to ensure steady development without stalling. I will hold these settings to allow the roast to progress toward the target development time.'
    - .artisan-holdout/artisan-31 development @ 692s bean=192.0°C ror=11.0 real(heat/fan)=60/80: model heat=40% fan=80% drop=False conf=0.9 — 'Development time is 75s out of 685s total (10.9%). To reach the 14.9% target, I need about 25-30 more seconds of development. RoR is 11 C/min, which is healthy. I will reduce heat further to gently coast toward the finish and maintain fan at 80% to ensure clean convective heat and smoke evacuation. Drop will be triggered once the development ratio is met, likely in ~30 seconds.'
    - .artisan-holdout/artisan-32 charge @ 61s bean=96.5°C ror=-16.5 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR has crashed significantly (-16.5 C/min) despite 100% heat. This indicates a stall or a measurement error, but given the current temperature (96.5 C), the roast is in danger of stalling. I am maintaining 100% heat to recover momentum and keeping the fan at 0% to maximize conductive heat transfer and prevent further cooling.'
    - .artisan-holdout/artisan-32 maillard @ 392s bean=136.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'RoR is healthy at 12C/min. Maintaining full heat to drive through the drying/Maillard transition. Fan remains at 0 to build momentum for the approach to first crack. No changes needed yet.'
    - .artisan-holdout/artisan-32 first-crack @ 633s bean=179.3°C ror=10.3 real(heat/fan)=100/40: model heat=70% fan=60% drop=False conf=0.9 — 'First crack has just begun (5s in). The RoR is healthy at 10.3 C/min. To manage the development phase and prevent an overly aggressive finish, I am reducing heat to 70% to moderate the momentum and increasing fan to 60% to shift toward convective heat and ensure smoke evacuation. This balance will help achieve the target development ratio without stalling the roast.'
    - .artisan-holdout/artisan-32 development @ 693s bean=188.0°C ror=8.7 real(heat/fan)=100/50: model heat=60% fan=75% drop=False conf=0.95 — 'The roast is currently at 65 seconds of development (approx 10% of total time). The RoR is healthy at 8.7 C/min. To reach the target development ratio of ~17% (approx 130 seconds total development), I need to extend the roast by about 65 more seconds. I am reducing heat to 60% to prevent a spike and increasing fan to 75% to shift toward convective heat, ensuring a smooth, controlled finish without stalling. Drop is not yet indicated.'
    - .artisan-holdout/artisan-33 charge @ 1s bean=127.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 153°C and rising. I am waiting for the environment temperature to reach the target charge range (180°C - 200°C) before initiating the charge. No heat or fan is required at this stage.'
    - .artisan-holdout/artisan-33 maillard @ 332s bean=135.0°C ror=12.0 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The RoR has flattened significantly at 135C, suggesting the roast is stalling or losing momentum prematurely. I need to maintain heat to push through the drying/early Maillard phase toward first crack. Keeping heat at 100% and slightly increasing fan to 20% to improve airflow and heat distribution without stalling the momentum.'
    - .artisan-holdout/artisan-33 first-crack @ 572s bean=179.0°C ror=11.3 real(heat/fan)=90/40: model heat=70% fan=60% drop=False conf=0.9 — 'First crack occurred 24 seconds ago. The RoR is currently 11.3 C/min, which is healthy but needs to be managed to ensure a smooth development phase. I am reducing heat to 70% and increasing fan to 60% to transition toward convective heat transfer, preventing a spike in RoR while ensuring even development. We are at 12 seconds of development; aiming for a total roast time of ~650-680 seconds to hit the 18% development target.'
    - .artisan-holdout/artisan-33 development @ 632s bean=189.7°C ror=10.7 real(heat/fan)=50/50: model heat=30% fan=70% drop=False conf=0.95 — 'Development is at 11.5% (72s / 624s). To reach the 18.3% target, I need ~42 more seconds of development. The RoR is 10.7 C/min, which is healthy but needs to be managed to avoid overshooting the target temperature too quickly. Reducing heat to 30% and increasing fan to 70% will shift the roast toward convection, gently slowing the RoR to ensure a smooth, controlled finish over the next 40-50 seconds.'
    - .artisan-holdout/artisan-34 charge @ 151s bean=111.3°C ror=25.0 real(heat/fan)=0/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roaster is currently in the preheating phase and has not yet been charged. The bean temperature is rising steadily. I am setting the heat to 100% to prepare for the charge, as the current environment temperature is still below the target charge range of 180-200C. Fan is kept low to allow the drum to build thermal energy.'
    - .artisan-holdout/artisan-34 maillard @ 481s bean=124.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily but the RoR has slowed significantly (12.3 C/min). To maintain momentum toward first crack without stalling, I am maintaining full heat. I am keeping the fan low to ensure sufficient conductive heat transfer to build energy in the beans before the crack.'
    - .artisan-holdout/artisan-34 first-crack @ 722s bean=169.7°C ror=11.4 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just begun (7 seconds ago). The RoR is currently healthy at 11.4 C/min. To manage the development phase and prevent a runaway RoR, I am reducing heat significantly and increasing airflow to transition to convective heat transfer, ensuring a controlled development period toward the 20% target.'
    - .artisan-holdout/artisan-34 development @ 812s bean=188.0°C ror=13.0 real(heat/fan)=90/50: model heat=60% fan=70% drop=False conf=0.9 — 'Development is at 14.7% (97s / 658s). To reach the 20.9% target, I need ~40 more seconds of development. RoR is 13C/min, which is healthy but needs to be managed to avoid overshooting the drop temp too quickly. Reducing heat to 60% and increasing fan to 70% will shift to convective heat, smoothing the finish and allowing for a controlled, steady development.'
    - .artisan-holdout/artisan-35 charge @ 0s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 123°C, which is below the target charge range of 180°C-200°C. I am setting heat to 100% to reach the charge temperature efficiently. Fan is kept at 0% to allow the drum to heat up effectively.'
    - .artisan-holdout/artisan-35 maillard @ 361s bean=133.0°C ror=11.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily but the RoR is beginning to flatten out. With the bean temperature at 133C, we are approaching the Maillard phase. I am maintaining full heat to ensure sufficient momentum into first crack, while introducing a small amount of fan (20%) to begin clearing moisture and preparing for the transition to convective heat transfer.'
    - .artisan-holdout/artisan-35 first-crack @ 632s bean=182.0°C ror=11.3 real(heat/fan)=80/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack occurred at 607s. Development is only 15s in. RoR is 11.3 C/min, which is quite high for this stage. To achieve a 13.7% development ratio (approx 85-90s total development time), I need to slow the momentum significantly to avoid a crash or an overly dark roast. Reducing heat to 40% and increasing fan to 60% will shift the roast toward convection and gently taper the RoR.'
    - .artisan-holdout/artisan-35 development @ 662s bean=189.3°C ror=13.0 real(heat/fan)=50/90: model heat=50% fan=90% drop=False conf=0.9 — 'Development is at 45s (approx 7% of total time). To reach the 13.7% target, I need about 45-50 more seconds of development. The RoR is stable at 13C/min. I will maintain current settings to ensure a steady, controlled finish without stalling, aiming to drop around 196-197C in about 45 seconds.'
    - .artisan-holdout/artisan-36 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 166°C and rising toward the target charge range (180-200°C). Keeping heat and fan at 0% is appropriate until the target charge temperature is reached.'
    - .artisan-holdout/artisan-36 maillard @ 392s bean=136.7°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 136.7°C with 0% fan. To prepare for the upcoming Maillard phase and first crack, I need to introduce airflow to manage moisture and prepare for convective heat transfer. Increasing fan to 25% while maintaining full heat will help stabilize the RoR and prevent potential stalling or uneven development as we approach the drying/Maillard transition.'
    - .artisan-holdout/artisan-36 first-crack @ 632s bean=176.0°C ror=9.0 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack has just begun. To manage the development phase and ensure a controlled RoR, I am reducing heat to prevent a spike and increasing fan to shift toward convective heat transfer, which will help maintain a steady development trajectory toward the 17.6% target.'
    - .artisan-holdout/artisan-36 development @ 722s bean=190.7°C ror=9.7 real(heat/fan)=100/30: model heat=80% fan=50% drop=False conf=0.9 — 'The roast is currently at 12.9% development (92s / 713s). To reach the 17.6% target, I need approximately 60 more seconds of development. The RoR is healthy at 9.7 C/min. I am increasing fan to 50% to shift toward convective heat and manage the final development phase, while keeping heat at 80% to maintain a steady, controlled finish without stalling. Drop is not yet indicated.'
    - .artisan-holdout/artisan-37 charge @ 91s bean=98.0°C ror=-7.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR has crashed significantly (-7.0 C/min) despite 100% heat. This indicates a stall or a measurement error, but given the current low bean temperature (98 C), the roast is in danger of stalling. I am maintaining 100% heat to recover momentum and keeping the fan at 0% to maximize conductive heat transfer and prevent further heat loss.'
    - .artisan-holdout/artisan-37 maillard @ 392s bean=137.3°C ror=13.0 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — "The roast is progressing steadily at 137.3°C. With a RoR of 13°C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure we don't stall, but increasing the fan slightly to 20% to begin preparing for better convective heat transfer and to clear moisture/chaff as we approach the crack."
    - .artisan-holdout/artisan-37 first-crack @ 602s bean=179.0°C ror=12.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR of 12C/min. Maintaining full heat to ensure momentum into the crack, but increasing fan to 30% to prepare for the exothermic reaction and ensure clean airflow as smoke begins to develop.'
    - .artisan-holdout/artisan-37 development @ 662s bean=192.7°C ror=13.7 real(heat/fan)=60/80: model heat=60% fan=80% drop=False conf=0.9 — 'Development time is 55s out of 585s total (9.4%). To reach the 14% target, I need ~27s more development. RoR is 13.7 C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop around 197-198C in about 30 seconds.'
    - .artisan-holdout/artisan-38 charge @ 31s bean=88.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (drying/yellowing). The bean temperature drop suggests a potential sensor anomaly or a very aggressive charge, but given the current 88C reading and 100% heat, I will maintain maximum heat to ensure momentum through the drying phase. Fan remains at 0% to build initial energy.'
    - .artisan-holdout/artisan-38 maillard @ 392s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at a low bean temperature (134.7C) with a moderate RoR. Maintaining full heat is appropriate to continue the momentum toward first crack. I am keeping the fan at 0% to maximize conductive heat transfer and ensure the bean mass gains sufficient energy before the drying/Maillard transition.'
    - .artisan-holdout/artisan-38 first-crack @ 662s bean=185.0°C ror=12.3 real(heat/fan)=100/0: model heat=40% fan=50% drop=False conf=0.9 — 'The roast is in early development (14s in). RoR is 12.3 C/min, which is healthy but needs to be managed to avoid a runaway finish. I am cutting heat significantly to 40% to prevent the RoR from spiking, and increasing fan to 50% to transition to convective heat and clear smoke, ensuring a controlled development phase toward the 13.3% target.'
    - .artisan-holdout/artisan-38 development @ 692s bean=190.3°C ror=12.3 real(heat/fan)=60/70: model heat=40% fan=80% drop=False conf=0.9 — 'Development is at 44s (6.5% of total time). To reach the 13.3% target (approx. 95s total dev time), I need about 50 more seconds of development. RoR is healthy at 12.3 C/min. I am reducing heat to 40% to gently manage the momentum and increasing fan to 80% to shift toward convective heat, ensuring a clean finish without stalling. I will monitor for the drop in the next 45-50 seconds.'
    - .artisan-holdout/artisan-39 charge @ 1s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 112°C, which is below the recommended charge range (180°C-200°C). I am maintaining zero heat and fan to allow the drum to continue preheating toward the target charge temperature.'
    - .artisan-holdout/artisan-39 maillard @ 393s bean=136.0°C ror=10.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 136°C with 0% fan. To prepare for the upcoming first crack and ensure a clean, convective environment, I am increasing the fan to 25% to begin clearing moisture and chaff. Heat remains at 100% to maintain momentum toward first crack.'
    - .artisan-holdout/artisan-39 first-crack @ 663s bean=184.0°C ror=10.0 real(heat/fan)=100/50: model heat=70% fan=70% drop=False conf=0.9 — 'First crack occurred at 649s. Development is just beginning (6s in). RoR is healthy at 10C/min. To manage the development phase and aim for a ~12% development ratio (approx 80-90s total development time), I need to maintain momentum without spiking the RoR. I will reduce heat to 70% to prevent a runaway RoR and increase fan to 70% to shift toward convective heat and ensure clean development.'
    - .artisan-holdout/artisan-39 development @ 723s bean=194.7°C ror=10.7 real(heat/fan)=50/80: model heat=50% fan=80% drop=False conf=0.9 — 'Current development time is 66s out of 715s total (9.2%). To reach the 12.4% target, I need approximately 23 more seconds of development. The RoR is stable at 10.7 C/min. I will maintain current heat and fan settings to allow a gentle, controlled finish without stalling, aiming to drop around 197-198 C in about 20-25 seconds.'
    - .artisan-holdout/artisan-40 charge @ 61s bean=121.0°C ror=23.0 real(heat/fan)=0/0: model heat=60% fan=20% drop=False conf=0.9 — 'The roast is currently at 121C with heat and fan at 0%, which is causing the bean RoR to drop significantly. To maintain momentum toward first crack, I need to introduce heat. I am setting heat to 60% and fan to 20% to begin building energy while maintaining enough airflow to clear moisture.'
    - .artisan-holdout/artisan-40 maillard @ 392s bean=131.0°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 131°C with no fan usage. To prepare for the approach to first crack and ensure even heat distribution, I am introducing 25% fan. Heat remains at 100% to maintain momentum toward the Maillard phase.'
    - .artisan-holdout/artisan-40 first-crack @ 633s bean=179.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR. Maintaining full heat to ensure momentum through the crack, but increasing fan to 30% to prepare for the transition to convective heat transfer and to begin clearing smoke/chaff as we enter the development phase.'
    - .artisan-holdout/artisan-40 development @ 693s bean=192.7°C ror=13.7 real(heat/fan)=30/60: model heat=20% fan=70% drop=False conf=0.9 — 'Total roast time is 634s, development time is 54s (8.5%). To reach a 14.2% development ratio, I need a total roast time of ~380s (which is impossible) or to extend development to ~105s. Current RoR is 13.7 C/min, which is healthy. I will reduce heat further to gently coast toward the drop, maintaining fan to ensure convective airflow and smoke evacuation. Drop target is ~200C, but I will prioritize the development duration.'
    - .artisan-holdout/artisan-41 charge @ 121s bean=113.0°C ror=23.0 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has not yet been charged. The environment temperature is approaching the target charge range (180-200C). I am setting heat to 0 and fan to 0 to allow the roaster to continue stabilizing at the target charge temperature.'
    - .artisan-holdout/artisan-41 maillard @ 482s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'RoR is healthy at 12C/min and the bean temperature is rising steadily. I am increasing the fan to 25% to begin introducing convective heat transfer and preparing for the moisture release phase, while maintaining 100% heat to ensure momentum is maintained toward first crack.'
    - .artisan-holdout/artisan-41 first-crack @ 722s bean=178.0°C ror=12.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — "Approaching first crack with a healthy RoR. I am increasing the fan to 30% to prepare for the exothermic reaction and improve convective heat transfer, while maintaining 100% heat to ensure we don't stall before crack. I will monitor for the first crack and then reduce heat significantly to manage the development phase."
    - .artisan-holdout/artisan-41 development @ 782s bean=192.0°C ror=14.0 real(heat/fan)=30/50: model heat=30% fan=50% drop=False conf=0.95 — 'Development is at 51s (7.8% of total time). To reach a 13.5% development ratio, I need ~94s total development time (43s more). RoR is 14C/min, which is healthy. I will maintain current settings to allow a steady, controlled development without stalling, aiming to drop in about 40-45 seconds.'
    - .artisan-holdout/artisan-42 charge @ 30s bean=85.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (18 seconds in). The telemetry shows a significant drop in bean temperature (from 94.5C to 85C) while heat is at 100%. This suggests the bean probe is reacting to the initial charge or a sensor anomaly. I will maintain maximum heat to ensure sufficient energy is entering the drum for the drying phase. Fan remains at 0% to build momentum.'
    - .artisan-holdout/artisan-42 maillard @ 391s bean=137.0°C ror=11.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily toward first crack. With an RoR of 11.3 C/min and bean temperature at 137 C, I am maintaining full heat to ensure sufficient momentum for the upcoming endothermic phase, while keeping fan low to maximize conductive heat transfer through the drum. No changes needed yet.'
    - .artisan-holdout/artisan-42 first-crack @ 661s bean=184.0°C ror=10.3 real(heat/fan)=100/30: model heat=60% fan=50% drop=False conf=0.9 — 'First crack has just started (5s in). RoR is healthy at 10.3 C/min. To manage the development phase and hit the 12.7% development ratio target, I need to reduce heat to prevent a spike and increase fan to shift toward convective heat for a clean finish. Dropping heat to 60% and fan to 50% will stabilize the momentum.'
    - .artisan-holdout/artisan-42 development @ 721s bean=194.3°C ror=10.3 real(heat/fan)=80/100: model heat=60% fan=100% drop=False conf=0.9 — 'Development time is 65s out of 709s total (9.1%). To reach the 12.7% target, I need ~25s more development. RoR is 10.3 C/min, which is healthy. I am reducing heat to 60% to manage the momentum as we approach the drop, while keeping fan at 100% to ensure convective heat and clean airflow. Drop is not yet indicated.'
    - .artisan-holdout/artisan-43 charge @ 1s bean=106.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The environment temperature is currently 136°C, which is well below the target charge range of 180°C-200°C. Maintaining zero heat and zero fan to allow the drum to continue preheating toward the target charge temperature.'
    - .artisan-holdout/artisan-43 maillard @ 393s bean=135.0°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily but slowly. At 135C and 383 seconds, we are behind the typical curve for a standard roast. I am maintaining full heat to build momentum toward first crack while slightly increasing the fan to 20% to ensure better airflow and prepare for the upcoming exothermic phase.'
    - .artisan-holdout/artisan-43 first-crack @ 633s bean=182.0°C ror=12.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR. Maintaining full heat to ensure momentum into the crack, but increasing fan to 30% to prepare for better convective heat transfer and smoke management as we enter the development phase.'
    - .artisan-holdout/artisan-43 development @ 693s bean=195.7°C ror=13.7 real(heat/fan)=20/100: model heat=20% fan=100% drop=False conf=0.9 — 'Development time is 50s out of 683s total (7.3%). To reach the 10.5% target, I need ~22 more seconds of development. RoR is 13.7 C/min, which is healthy. I will maintain current settings to allow a steady, controlled finish without stalling, aiming to drop around 200-201C in about 20-25 seconds.'
    - .artisan-holdout/artisan-44 charge @ 120s bean=127.0°C ror=2.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR has crashed significantly (from 141C to 127C in 6 seconds). The heat is at 100% but the bean temperature is dropping rapidly, suggesting a potential sensor issue or a severe stall. I am maintaining maximum heat to recover momentum and keeping the fan at 0% to maximize conductive heat transfer and prevent further cooling.'
    - .artisan-holdout/artisan-44 maillard @ 481s bean=140.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'The RoR has stalled at 140C despite 100% heat. The environment temperature is also stagnant. I need to increase airflow to pull more heat through the drum and encourage the bean temperature to climb toward first crack. Increasing fan to 30% will improve convective heat transfer and help break the stall.'
    - .artisan-holdout/artisan-44 first-crack @ 721s bean=184.0°C ror=12.0 real(heat/fan)=70/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just started. To manage the development phase and prevent a runaway RoR, I am cutting heat significantly and increasing fan to shift toward convective heat transfer. This will help maintain a controlled, steady development duration toward the 13.2% target.'
    - .artisan-holdout/artisan-44 development @ 781s bean=196.0°C ror=12.0 real(heat/fan)=50/100: model heat=50% fan=100% drop=False conf=0.9 — 'The roast is at 64 seconds of development with a total time of 671 seconds (approx 9.5% development ratio). The RoR is 12 C/min, which is healthy but needs to be managed to reach the 13.2% target (approx 89 seconds total development). I am maintaining heat at 50% and fan at 100% to ensure convective heat transfer and prevent stalling while gently approaching the drop temperature. I will drop in about 25 seconds.'
    - .artisan-holdout/artisan-45 charge @ 151s bean=104.0°C ror=-10.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR has crashed significantly (-10 C/min) despite 100% heat. This indicates a stall or a measurement error, but given the current state, I must maintain maximum heat to recover momentum. Fan is kept at 0% to maximize conductive heat transfer and prevent further cooling.'
    - .artisan-holdout/artisan-45 maillard @ 512s bean=137.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 137.7°C with a healthy RoR of 11.7°C/min. I am maintaining full heat to ensure momentum toward first crack, but I am introducing 25% fan now to begin transitioning toward convective heat transfer and to prepare for the smoke that will accompany first crack. This helps prevent the drum from becoming overly radiant/conductive as we approach the development phase.'
    - .artisan-holdout/artisan-45 first-crack @ 752s bean=182.0°C ror=11.7 real(heat/fan)=100/30: model heat=60% fan=50% drop=False conf=0.9 — 'First crack has just begun (6s ago). The RoR is healthy at 11.7 C/min. To ensure a controlled development phase and hit the 12.3% development ratio target (approx. 75-80s total development time), I am reducing heat to prevent a spike and increasing fan to 50% to transition to convective heat, which will help manage the roast trajectory and clear smoke.'
    - .artisan-holdout/artisan-45 development @ 782s bean=189.0°C ror=13.0 real(heat/fan)=80/90: model heat=60% fan=90% drop=False conf=0.9 — 'Development is at 36s (approx 5.6% of total time). To reach a 12.3% development ratio, I need about 45-50 more seconds of development. RoR is 13C/min, which is healthy but needs to be managed to avoid overshooting the target temperature too quickly. Reducing heat to 60% and maintaining fan at 90% will gently taper the RoR to ensure a smooth, controlled finish.'
    - .artisan-holdout/artisan-46 charge @ 243s bean=88.0°C ror=-13.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR has crashed significantly (-13 C/min) despite maximum heat. This indicates a potential sensor error or a severe stall. Given the current bean temperature (88 C) is very low for the elapsed time, I am maintaining maximum heat to recover momentum. Fan remains at 0 to maximize conductive heat transfer and prevent further cooling.'
    - .artisan-holdout/artisan-46 maillard @ 603s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 134.7°C. With heat at 100% and fan at 0%, the RoR is healthy (11.7°C/min). I am maintaining full heat to ensure momentum toward first crack, but I will introduce a small amount of fan (20%) now to begin transitioning toward convective heat transfer and to prepare for the upcoming moisture release, ensuring the environment remains clean.'
    - .artisan-holdout/artisan-46 first-crack @ 843s bean=180.0°C ror=12.0 real(heat/fan)=100/0: model heat=40% fan=50% drop=False conf=0.95 — 'First crack has just begun. Heat is currently at 100% with no fan, which is too aggressive for the development phase and will likely cause a spike in RoR and potential scorching. I am cutting heat significantly and introducing airflow to shift toward convective heat transfer, ensuring a controlled development phase to reach the 13.6% development target.'
    - .artisan-holdout/artisan-46 development @ 903s bean=194.7°C ror=14.7 real(heat/fan)=10/60: model heat=10% fan=70% drop=False conf=0.95 — 'Development is at 63s out of 677s total (9.3%). To reach the 13.6% target, I need ~92s of development time, meaning ~29s more. RoR is 14.7 C/min, which is healthy but needs to be managed to avoid overshooting the target temp too quickly. Heat is already at 10%; I will maintain this to keep the momentum steady while increasing fan to 70% to shift toward convective heat and ensure clean development as we approach the drop.'
    - .artisan-holdout/artisan-47 charge @ 1s bean=132.0°C ror=None real(heat/fan)=0/0: model heat=100% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. I am setting the heat to 100% to reach the target charge temperature (180-200°C) efficiently. Fan is kept at 0% to allow the drum to heat up effectively.'
    - .artisan-holdout/artisan-47 maillard @ 392s bean=137.3°C ror=11.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 137C with 100% heat and 0% fan. To prepare for the approach to first crack and ensure a clean, convective environment, I am introducing 25% fan now. I will maintain 100% heat for a moment longer to keep the RoR from dropping too sharply, but will look to reduce heat in the next adjustment as the bean temperature climbs toward 150C.'
    - .artisan-holdout/artisan-47 first-crack @ 662s bean=186.0°C ror=12.7 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack occurred at 638s. Development is only 16s into a total roast of 654s (2.4% ratio). RoR is 12.7 C/min, which is healthy but needs to be managed to avoid overshooting the target development time. I am reducing heat to 60% and increasing fan to 60% to transition to convective heat and gently taper the RoR for a controlled 12-15% development ratio. Drop is not yet indicated.'
    - .artisan-holdout/artisan-47 development @ 692s bean=191.7°C ror=12.0 real(heat/fan)=70/50: model heat=50% fan=70% drop=False conf=0.9 — 'Development time is 46s out of 684s total (6.7%). To reach the 12.8% target, I need ~42s more development. RoR is 12C/min, which is healthy but needs to be managed to avoid overshooting the drop temp too quickly. Reducing heat to 50% and increasing fan to 70% will shift to convective heat, smoothing the finish and ensuring we reach the development duration without stalling.'

## prompt_version = v4

### google/gemini-3.1-flash-lite (ultra-flash)
- .artisan-holdout/artisan-29 (truth DTR 14.1%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=2.92 dir=1.0; fan MAE=11.04 dir=0.435; latency pre=0.89s preFC=1.04s FC=1.34s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     0 |    18 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=1.0 — the more informative control-behaviour view)
- .artisan-holdout/artisan-30 (truth DTR 16.5%, 34/34 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=14.26 dir=0.788; fan MAE=11.32 dir=0.545; latency pre=1.19s preFC=1.22s FC=1.33s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=33     |
    (total ticks=34; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     6 |    24 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=33; diagonal agreement=0.788 — the more informative control-behaviour view)
- .artisan-holdout/artisan-31 (truth DTR 14.9%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-2.0s/-0.7°C; heat MAE=4.23 dir=0.96; fan MAE=15 dir=0.44; latency pre=1.04s preFC=1.07s FC=1.17s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.96 — the more informative control-behaviour view)
- .artisan-holdout/artisan-32 (truth DTR 16.9%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=3.85 dir=0.96; fan MAE=10.77 dir=0.6; latency pre=1.39s preFC=1.18s FC=1.11s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     0 |    20 |     0 |
    |        raise |     1 |     0 |     2 |
    (n=25; diagonal agreement=0.96 — the more informative control-behaviour view)
- .artisan-holdout/artisan-33 (truth DTR 18.3%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.08 dir=0.913; fan MAE=11.04 dir=0.478; latency pre=0.92s preFC=1.11s FC=1.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    17 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=23; diagonal agreement=0.913 — the more informative control-behaviour view)
- .artisan-holdout/artisan-34 (truth DTR 20.9%, 30/30 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.33 dir=0.931; fan MAE=7 dir=0.655; latency pre=1.05s preFC=1.14s FC=1.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=29     |
    (total ticks=30; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    23 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=29; diagonal agreement=0.931 — the more informative control-behaviour view)
- .artisan-holdout/artisan-35 (truth DTR 13.7%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=4 dir=0.958; fan MAE=13.6 dir=0.417; latency pre=0.82s preFC=1.11s FC=1.21s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    19 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.958 — the more informative control-behaviour view)
- .artisan-holdout/artisan-36 (truth DTR 17.6%, 27/27 ok): drop F1=0.667 P=0.5 R=1.0 timing=-11.0s/-2.4°C; heat MAE=8.52 dir=0.923; fan MAE=15 dir=0.385; latency pre=1.05s preFC=1.19s FC=1.22s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=25     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     1 |    19 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=26; diagonal agreement=0.923 — the more informative control-behaviour view)
- .artisan-holdout/artisan-37 (truth DTR 14.0%, 25/25 ok): drop F1=0.667 P=0.5 R=1.0 timing=-1.0s/-0.3°C; heat MAE=9 dir=0.792; fan MAE=11 dir=0.458; latency pre=1.04s preFC=1.21s FC=1.19s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=23     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     5 |    15 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.792 — the more informative control-behaviour view)
- .artisan-holdout/artisan-38 (truth DTR 13.3%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-23.0s/-4.0°C; heat MAE=6.15 dir=0.92; fan MAE=15.38 dir=0.44; latency pre=0.84s preFC=1.11s FC=1.2s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    20 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.92 — the more informative control-behaviour view)
- .artisan-holdout/artisan-39 (truth DTR 12.4%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.77 dir=0.88; fan MAE=12.88 dir=0.52; latency pre=0.92s preFC=1.13s FC=1.12s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    19 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-holdout/artisan-40 (truth DTR 14.2%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-12.0s/-2.0°C; heat MAE=6.54 dir=0.92; fan MAE=13.27 dir=0.44; latency pre=1.12s preFC=1.12s FC=1.34s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     1 |    18 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.92 — the more informative control-behaviour view)
- .artisan-holdout/artisan-41 (truth DTR 13.5%, 29/29 ok): drop F1=0.667 P=0.5 R=1.0 timing=-13.0s/-2.7°C; heat MAE=6.21 dir=0.893; fan MAE=12.41 dir=0.5; latency pre=1.07s preFC=1.1s FC=1.17s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=27     |
    (total ticks=29; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     1 |    22 |     1 |
    |        raise |     0 |     1 |     1 |
    (n=28; diagonal agreement=0.893 — the more informative control-behaviour view)
- .artisan-holdout/artisan-42 (truth DTR 12.7%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.85 dir=0.88; fan MAE=10.96 dir=0.4; latency pre=0.84s preFC=1.13s FC=1.11s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    19 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-holdout/artisan-43 (truth DTR 10.5%, 25/25 ok): drop F1=0.667 P=0.5 R=1.0 timing=-24.0s/-5.3°C; heat MAE=4.4 dir=0.917; fan MAE=10.8 dir=0.417; latency pre=0.88s preFC=1.15s FC=1.07s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=23     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    19 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.917 — the more informative control-behaviour view)
- .artisan-holdout/artisan-44 (truth DTR 13.2%, 28/28 ok): drop F1=0.667 P=0.5 R=1.0 timing=-28.0s/-5.0°C; heat MAE=5.89 dir=0.889; fan MAE=12.86 dir=0.407; latency pre=1.07s preFC=1.14s FC=1.29s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=26     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     3 |    18 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=27; diagonal agreement=0.889 — the more informative control-behaviour view)
- .artisan-holdout/artisan-45 (truth DTR 12.3%, 29/29 ok): drop F1=0.667 P=0.5 R=1.0 timing=-19.0s/-5.0°C; heat MAE=2.76 dir=0.929; fan MAE=12.93 dir=0.5; latency pre=1.21s preFC=1.23s FC=1.38s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=27     |
    (total ticks=29; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    23 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=28; diagonal agreement=0.929 — the more informative control-behaviour view)
- .artisan-holdout/artisan-46 (truth DTR 13.6%, 33/33 ok): drop F1=0.667 P=0.5 R=1.0 timing=-4.0s/-0.6°C; heat MAE=5.45 dir=0.875; fan MAE=11.52 dir=0.531; latency pre=1.08s preFC=1.18s FC=1.08s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=31     |
    (total ticks=33; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    25 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=32; diagonal agreement=0.875 — the more informative control-behaviour view)
- .artisan-holdout/artisan-47 (truth DTR 12.8%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-18.0s/-4.0°C; heat MAE=6.92 dir=0.88; fan MAE=14.81 dir=0.4; latency pre=1.04s preFC=1.08s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    19 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-holdout/artisan-29 charge @ 60s bean=110.0°C ror=-1.0 real(heat/fan)=100/40: model heat=100% fan=40% drop=False conf=0.9 — 'The roast is currently experiencing a significant drop in bean temperature (RoR -1.0 C/min) despite maximum heat. This indicates a potential stall or a measurement anomaly following the charge. I am maintaining maximum heat to recover momentum and keeping the fan at 40% to ensure sufficient heat transfer without cooling the drum prematurely.'
    - .artisan-holdout/artisan-29 maillard @ 391s bean=136.0°C ror=11.7 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 136°C. With a RoR of 11.7°C/min, we are in a good position to maintain momentum toward first crack. I am increasing the fan to 25% to begin transitioning toward convective heat transfer and preparing for the smoke that will accompany the upcoming Maillard phase, while keeping heat at 100% to maintain the current energy trajectory.'
    - .artisan-holdout/artisan-29 first-crack @ 601s bean=179.7°C ror=13.7 real(heat/fan)=80/0: model heat=40% fan=50% drop=False conf=0.9 — 'First crack has just begun (2 seconds ago). The RoR is still quite high (13.7 C/min). I need to reduce heat and increase airflow to manage the momentum and transition to convective heat transfer for the development phase. Dropping heat to 40% and increasing fan to 50% will help control the RoR without stalling the roast.'
    - .artisan-holdout/artisan-29 development @ 661s bean=193.0°C ror=13.3 real(heat/fan)=10/90: model heat=0% fan=90% drop=False conf=0.95 — 'The roast is in the final stages of development. With the bean temperature at 193°C and a RoR of 13.3°C/min, we are approaching the target drop temperature of 198°C. To avoid overshooting or hitting the 196°C+ bitter ceiling, I am cutting heat to 0% to coast into the finish while maintaining high airflow (90%) to ensure clean development. I am recommending a drop at 195°C to respect the safety ceiling while achieving the profile goals.'
    - .artisan-holdout/artisan-30 charge @ 212s bean=124.5°C ror=25.5 real(heat/fan)=0/10: model heat=80% fan=20% drop=False conf=0.9 — 'The roaster is currently in the preheating phase, but the telemetry indicates the beans are already at 124.5C. This suggests the beans were charged into a hot drum. Since the heat is currently off (0%) and the fan is at a low setting (10%), I need to initiate heat to maintain momentum through the drying phase while keeping the fan low to build convective energy. I am setting heat to 80% to establish a steady RoR.'
    - .artisan-holdout/artisan-30 maillard @ 603s bean=137.0°C ror=10.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 137C. With RoR at 10.3C/min and no fan yet, it is time to introduce airflow to manage the transition into the Maillard phase and prevent potential scorching as we approach first crack. I am increasing fan to 25% to shift toward convective heat transfer while maintaining full heat to keep momentum.'
    - .artisan-holdout/artisan-30 first-crack @ 873s bean=180.0°C ror=8.0 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.95 — 'First crack has been reached (13 seconds ago). The RoR is currently 8.0 C/min, which is healthy but needs to be managed to avoid a runaway finish. I am reducing heat to 70% to begin the gentle glide toward the 198 C target and increasing fan to 50% to shift toward convective heat and clear smoke, ensuring a clean development phase. We are well below the 196 C indicated ceiling.'
    - .artisan-holdout/artisan-30 development @ 933s bean=187.3°C ror=7.3 real(heat/fan)=100/30: model heat=50% fan=60% drop=False conf=0.9 — 'The roast is progressing well, but the RoR (7.3 C/min) is still quite high for this stage of development. To ensure a smooth finish and avoid hitting the 196 C ceiling too aggressively, I am cutting heat to 50% and increasing fan to 60% to shift toward convective cooling and stabilize the RoR. We are approaching the target drop temperature, so we need to manage the momentum carefully.'
    - .artisan-holdout/artisan-31 charge @ 0s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current environment temperature is 154°C, which is below the recommended charge range of 180°C-200°C. I am recommending to continue preheating to reach the target charge temperature.'
    - .artisan-holdout/artisan-31 maillard @ 362s bean=134.7°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'RoR is healthy at 12C/min and the bean temperature is still in the early-to-mid Maillard phase. Maintaining full heat is appropriate to keep momentum toward first crack. I am keeping the fan at 0% to maximize conductive heat transfer for now, but will look to introduce airflow as we approach the crack to manage smoke and transition to convective heat.'
    - .artisan-holdout/artisan-31 first-crack @ 632s bean=181.0°C ror=9.0 real(heat/fan)=100/60: model heat=100% fan=80% drop=False conf=0.9 — 'First crack occurred 15 seconds ago. The RoR is currently 9.0 C/min, which is healthy for this stage. I am maintaining high heat to ensure momentum through the development phase while increasing fan to 80% to manage smoke and transition to convective heat transfer as we approach the target drop temperature of 198 C. We are not yet at the drop temperature, so we continue.'
    - .artisan-holdout/artisan-31 development @ 692s bean=192.0°C ror=11.0 real(heat/fan)=60/80: model heat=50% fan=80% drop=False conf=0.9 — 'The roast is progressing well through development (75s in, ~11% development ratio). RoR is steady at 11C/min. With the target drop at 198C and current temp at 192C, we have room to continue. I am reducing heat slightly to 50% to manage the momentum as we approach the finish, while maintaining 80% fan to ensure convective heat and smoke evacuation. We are not yet at the drop threshold.'
    - .artisan-holdout/artisan-32 charge @ 61s bean=96.5°C ror=-16.5 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is experiencing a severe, erratic drop in bean temperature (RoR -16.5 C/min) despite maximum heat. This indicates a sensor error or a massive thermal stall. Given the environment temperature is stable at 160C, the bean probe is likely misreading or the beans are not absorbing heat. I am maintaining maximum heat to attempt to recover the momentum, but this roast is highly unstable.'
    - .artisan-holdout/artisan-32 maillard @ 392s bean=136.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'RoR is healthy at 12C/min and the bean temperature is progressing well through the drying/early Maillard phase. Maintaining full heat is appropriate here to ensure sufficient momentum heading into first crack. I am introducing a small amount of fan (20%) to begin clearing moisture and chaff, shifting slightly toward convective heat transfer without stalling the RoR.'
    - .artisan-holdout/artisan-32 first-crack @ 633s bean=179.3°C ror=10.3 real(heat/fan)=100/40: model heat=100% fan=60% drop=False conf=0.9 — 'First crack has just begun (5 seconds ago). The RoR is healthy at 10.3 C/min. To manage the development phase and prevent an overly aggressive finish, I am increasing the fan to 60% to shift toward convective heat and reduce the risk of tipping/scorching, while maintaining 100% heat for now to ensure momentum is maintained through the initial crack expansion. I will look to reduce heat in the next adjustment as the RoR stabilizes.'
    - .artisan-holdout/artisan-32 development @ 693s bean=188.0°C ror=8.7 real(heat/fan)=100/50: model heat=100% fan=50% drop=False conf=0.9 — 'The roast is in the development phase, 65 seconds post-first crack. The RoR is healthy at 8.7 C/min, and the bean temperature is 188 C. With a target drop of 198 C, we have 10 degrees to go. I am maintaining full heat and moderate fan to ensure steady development without stalling, as we are still well below the 196 C danger zone. I will continue to monitor for the RoR to begin its final decline before dropping.'
    - .artisan-holdout/artisan-33 charge @ 1s bean=127.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 153°C, which is below the target charge range (180°C-200°C). I am maintaining heat and fan at 0 to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-holdout/artisan-33 maillard @ 332s bean=135.0°C ror=12.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The RoR has flattened significantly (12 C/min) and the bean temperature is stalling at 135 C despite 100% heat. The environment temperature is also stagnant. I need to increase airflow to improve convective heat transfer and help push the roast through the Maillard phase, while maintaining maximum heat to overcome the current stall.'
    - .artisan-holdout/artisan-33 first-crack @ 572s bean=179.0°C ror=11.3 real(heat/fan)=90/40: model heat=50% fan=60% drop=False conf=0.9 — 'First crack occurred 12 seconds ago. The RoR is still quite high (11.3 C/min) for this stage. I am reducing heat to 50% and increasing fan to 60% to manage the momentum and transition to convective cooling, preventing a spike while ensuring even development. We are still well below the drop target and development time is minimal, so we continue.'
    - .artisan-holdout/artisan-33 development @ 632s bean=189.7°C ror=10.7 real(heat/fan)=50/50: model heat=30% fan=70% drop=False conf=0.9 — 'The roast is progressing well through development (72s in, ~11.5% development ratio). With a current bean temp of 189.7°C and a target of 198.3°C, we have room to continue. The RoR is healthy at 10.7°C/min. I am reducing heat to 30% to manage the momentum as we approach the target and increasing fan to 70% to shift toward convective heat, ensuring a clean finish without stalling. We are not yet at the drop point.'
    - .artisan-holdout/artisan-34 charge @ 151s bean=111.3°C ror=25.0 real(heat/fan)=0/10: model heat=0% fan=10% drop=False conf=0.9 — 'The roaster is currently in the preheating phase, approaching the charge temperature range (180-200C). Heat is currently off. I will maintain the current settings to allow the drum to continue soaking and reach the target charge temperature.'
    - .artisan-holdout/artisan-34 maillard @ 481s bean=124.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily but the RoR has slowed significantly (12.3 C/min). To maintain momentum through the Maillard phase and avoid stalling before first crack, I am maintaining full heat. I am keeping the fan low (10%) to ensure sufficient conductive heat transfer to build energy in the beans.'
    - .artisan-holdout/artisan-34 first-crack @ 722s bean=169.7°C ror=11.4 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — "First crack has just begun (7 seconds ago). The RoR is healthy at 11.4 C/min. I am maintaining high heat to ensure momentum through the development phase while increasing fan to 50% to manage convective heat transfer and clear smoke as the roast progresses. I will monitor the RoR closely to ensure it doesn't crash or spike as we approach the target drop temperature."
    - .artisan-holdout/artisan-34 development @ 812s bean=188.0°C ror=13.0 real(heat/fan)=90/50: model heat=70% fan=70% drop=False conf=0.95 — 'The roast is in the final stages of development. With a current bean temp of 188C and a target of 198.7C, we are approaching the finish. The RoR is 13C/min, which is healthy but needs to be managed to avoid overshooting the target or stalling. I am reducing heat to 70% to gently taper the RoR and increasing fan to 70% to ensure convective heat transfer and smoke evacuation as we approach the drop. We are not yet at the drop temperature, so should_drop is false.'
    - .artisan-holdout/artisan-35 charge @ 0s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 143°C, which is below the recommended charge range (180°C-200°C). I am recommending to maintain zero heat and fan to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-holdout/artisan-35 maillard @ 361s bean=133.0°C ror=11.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With the bean temperature at 133°C and a stable RoR of 11.3°C/min, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient energy for the upcoming endothermic phase, but I am introducing 25% fan now to begin transitioning toward convective heat transfer and to prepare for the smoke evacuation required during first crack.'
    - .artisan-holdout/artisan-35 first-crack @ 632s bean=182.0°C ror=11.3 real(heat/fan)=80/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack occurred 15 seconds ago. The RoR is still quite high (11.3 C/min) for this stage of development. I am reducing heat to 40% and increasing fan to 60% to manage the momentum and transition to convective heat, preventing a spike while ensuring even development toward the 198.7 C target. We are not yet at the drop temperature or development ratio.'
    - .artisan-holdout/artisan-35 development @ 662s bean=189.3°C ror=13.0 real(heat/fan)=50/90: model heat=50% fan=90% drop=False conf=0.9 — 'The roast is progressing well through development (45s in). The RoR is stable at 13C/min. With the target drop at 198.7C and current temp at 189.3C, we have about 9.4C to go. Maintaining current heat/fan settings is appropriate to ensure a smooth, controlled finish without stalling or rushing the development. I will hold these settings until closer to the target drop.'
    - .artisan-holdout/artisan-36 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 166°C, which is below the recommended charge range (180°C-200°C). I am recommending no heat or fan to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-holdout/artisan-36 maillard @ 392s bean=136.7°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is progressing steadily but is still in the early-to-mid Maillard phase. With a bean RoR of 10.7 C/min and no fan usage yet, I am maintaining full heat to ensure sufficient energy for the upcoming first crack. I will keep the fan at 0% for now to maximize conductive heat transfer, but I will prepare to introduce airflow as we approach first crack to manage smoke and transition to convective heat.'
    - .artisan-holdout/artisan-36 first-crack @ 632s bean=176.0°C ror=9.0 real(heat/fan)=100/30: model heat=70% fan=50% drop=False conf=0.9 — 'First crack has just begun (621s). The RoR is currently 9.0 C/min, which is healthy for the start of development. To manage the momentum and ensure a controlled finish toward the 198.7 C target, I am reducing heat to 70% to prevent a spike and increasing fan to 50% to shift toward convective heat and clear smoke. This balance will help maintain a steady, declining RoR through the development phase.'
    - .artisan-holdout/artisan-36 development @ 722s bean=190.7°C ror=9.7 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'The roast is currently at 190.7°C with a RoR of 9.7°C/min. The target drop is 198.7°C. With 92 seconds of development time, we are approaching the target development ratio. The current heat setting of 100% is too high for this stage of development and will likely cause the RoR to spike, leading to an uneven roast or over-development. I am reducing heat to 40% and increasing fan to 60% to manage the momentum and ensure a controlled finish toward the target drop temperature.'
    - .artisan-holdout/artisan-37 charge @ 91s bean=98.0°C ror=-7.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is currently experiencing a severe RoR crash (-7.0 C/min) despite 100% heat. This indicates the bean probe is likely experiencing a transient reading error or the drum is struggling to recover from the charge. I am maintaining 100% heat and 0% fan to maximize energy input and stabilize the bean temperature trend.'
    - .artisan-holdout/artisan-37 maillard @ 392s bean=137.3°C ror=13.0 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 137C. With a RoR of 13C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure momentum, but increasing the fan to 25% to begin shifting toward convective heat transfer and preparing for the smoke that will accompany the upcoming Maillard phase.'
    - .artisan-holdout/artisan-37 first-crack @ 602s bean=179.0°C ror=12.0 real(heat/fan)=100/10: model heat=75% fan=30% drop=False conf=0.9 — 'Approaching first crack (179C indicated). RoR is healthy at 12C/min. To prepare for the transition, I am reducing heat to manage the momentum and increasing fan to shift toward convective heat transfer, ensuring a clean development phase without stalling.'
    - .artisan-holdout/artisan-37 development @ 662s bean=192.7°C ror=13.7 real(heat/fan)=60/80: model heat=40% fan=90% drop=False conf=0.9 — 'The roast is progressing well through development (55s in). The RoR is 13.7 C/min, which is healthy but needs to be managed as we approach the 199 C target. I am reducing heat to 40% to gently taper the RoR and increasing fan to 90% to maximize convective heat and ensure clean development as we approach the drop. We are not yet at the target drop temperature, so should_drop is false.'
    - .artisan-holdout/artisan-38 charge @ 31s bean=88.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — "The roast is in the very early stages (drying/early yellowing). The bean temperature is dropping rapidly after the charge, likely due to the probe's sensitivity or a very low charge mass. I am maintaining maximum heat (100%) and keeping the fan off (0%) to build momentum and recover the bean temperature as quickly as possible."
    - .artisan-holdout/artisan-38 maillard @ 392s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 134.7°C with a low RoR (11.7°C/min). To ensure sufficient momentum heading into the Maillard phase and first crack, I am maintaining full heat. I am introducing 25% fan now to begin clearing moisture and transitioning toward convective heat transfer, which will help prevent stalling as the bean density increases.'
    - .artisan-holdout/artisan-38 first-crack @ 662s bean=185.0°C ror=12.3 real(heat/fan)=100/0: model heat=70% fan=40% drop=False conf=0.9 — 'First crack occurred at 631s. We are currently at 185C with a RoR of 12.3C/min. The target drop is 199C. With 14 seconds of development, we are very early. Heat is currently at 100% with 0% fan, which is too aggressive for the post-crack phase. I am reducing heat to 70% and increasing fan to 40% to manage the RoR and transition to convective heat transfer, preventing a spike or roast defect as we approach the target.'
    - .artisan-holdout/artisan-38 development @ 692s bean=190.3°C ror=12.3 real(heat/fan)=60/70: model heat=40% fan=70% drop=False conf=0.9 — 'The roast is progressing well through development (44 seconds in). The RoR is healthy at 12.3 C/min, and we are approaching the target drop temperature of 199 C. To ensure a smooth finish without overshooting or stalling, I am reducing heat further to manage the momentum while maintaining airflow to ensure clean convective heat transfer. We are not yet at the drop temperature, so we continue.'
    - .artisan-holdout/artisan-39 charge @ 1s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 144°C, which is below the target charge range (180°C-200°C). I am maintaining heat and fan at 0 to allow the drum to continue heating up toward the target charge temperature.'
    - .artisan-holdout/artisan-39 maillard @ 393s bean=136.0°C ror=10.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily but the RoR is quite low (10.3 C/min) for this stage. With heat at 100% and fan at 0%, I need to maintain energy to ensure we reach first crack without stalling, but I will introduce a small amount of fan (20%) to begin clearing moisture and preparing for the convective phase, while keeping heat at 100% to support the momentum.'
    - .artisan-holdout/artisan-39 first-crack @ 663s bean=184.0°C ror=10.0 real(heat/fan)=100/50: model heat=70% fan=70% drop=False conf=0.9 — 'First crack has just begun (6s ago). RoR is 10C/min, which is healthy for this stage. I am reducing heat to 70% to manage the momentum of the roast and increasing fan to 70% to shift toward convective heat and clear smoke, ensuring a controlled development phase without stalling. We are still well below the drop target.'
    - .artisan-holdout/artisan-39 development @ 723s bean=194.7°C ror=10.7 real(heat/fan)=50/80: model heat=30% fan=80% drop=False conf=0.95 — 'The roast is approaching the 200C target. With a current RoR of 10.7C/min and 66 seconds into development, the bean is progressing well. To avoid overshooting the 196C indicated ceiling and to ensure a controlled finish, I am reducing heat to 30% while maintaining high airflow to manage the convective heat transfer and clear smoke. I will hold for a few more degrees to reach the target drop temperature.'
    - .artisan-holdout/artisan-40 charge @ 61s bean=121.0°C ror=23.0 real(heat/fan)=0/0: model heat=80% fan=20% drop=False conf=0.9 — 'The roast is currently in the early stages (121C) with heat and fan at 0%. The bean RoR is dropping rapidly. I need to apply heat to maintain momentum through the drying/Maillard transition. Setting heat to 80% and fan to 20% to initiate convective airflow while building energy.'
    - .artisan-holdout/artisan-40 maillard @ 392s bean=131.0°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With the bean temperature at 131°C and a healthy RoR of 12.3°C/min, we are in a good position. I am maintaining full heat to ensure momentum, but introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the Maillard phase.'
    - .artisan-holdout/artisan-40 first-crack @ 633s bean=179.0°C ror=12.3 real(heat/fan)=100/10: model heat=70% fan=30% drop=False conf=0.9 — 'Approaching first crack with a healthy RoR. I am reducing heat to 70% to manage the energy transition into the crack and increasing fan to 30% to shift toward convective heat and prepare for smoke evacuation. This prevents a spike in RoR during the crack.'
    - .artisan-holdout/artisan-40 development @ 693s bean=192.7°C ror=13.7 real(heat/fan)=30/60: model heat=20% fan=60% drop=False conf=0.9 — 'The roast is in the development phase with 54 seconds elapsed since first crack. The current bean temperature is 192.7°C, approaching the target drop of 200°C. The RoR is 13.7°C/min, which is healthy but needs to be managed to avoid overshooting the target. I am reducing heat to 20% to gently taper the RoR while maintaining the current fan speed to ensure convective heat transfer and smoke evacuation. We are not yet at the drop temperature or the development ratio target, so we continue.'
    - .artisan-holdout/artisan-41 charge @ 121s bean=113.0°C ror=23.0 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has not yet been charged. The environment temperature is 135.7°C, which is below the recommended charge range (180°C-200°C). I am recommending no heat or fan to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-holdout/artisan-41 maillard @ 482s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily but is still in the early stages of the Maillard phase. With a bean RoR of 12°C/min and no fan usage yet, I am maintaining full heat to ensure sufficient energy for the upcoming first crack. I will introduce a small amount of fan (20%) to begin clearing moisture and preparing for the transition to convective heat transfer, while keeping heat at 100% to maintain momentum.'
    - .artisan-holdout/artisan-41 first-crack @ 722s bean=178.0°C ror=12.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is approaching first crack. The RoR has slowed to 12C/min, which is appropriate for this stage. I am maintaining full heat to ensure enough momentum for the crack, but increasing the fan to 30% to begin transitioning toward convective heat transfer and preparing for the smoke that will accompany first crack.'
    - .artisan-holdout/artisan-41 development @ 782s bean=192.0°C ror=14.0 real(heat/fan)=30/50: model heat=20% fan=60% drop=False conf=0.9 — 'The roast is in the development phase with 51 seconds elapsed since first crack. The current bean temperature is 192°C, approaching the 200°C target. The RoR is 14°C/min, which is healthy but needs to be managed as we approach the drop. I am reducing heat to 20% to gently taper the RoR and increasing fan to 60% to ensure convective heat transfer and smoke evacuation as we approach the finish. We are not yet at the target drop temperature or development percentage, so we continue.'
    - .artisan-holdout/artisan-42 charge @ 30s bean=85.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (18 seconds in). The recent telemetry shows a significant, erratic drop in bean temperature (from 94.5C to 85C), likely due to a probe reading anomaly or sensor stabilization after charging. Heat is already at 100% and fan at 0%. I will maintain maximum heat to ensure momentum is established and keep the fan off to allow the drum to build thermal energy.'
    - .artisan-holdout/artisan-42 maillard @ 391s bean=137.0°C ror=11.3 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is still in the early stages of the Maillard phase. With a bean RoR of 11.3 C/min and an indicated temperature of 137 C, we have plenty of headroom before first crack. Maintaining full heat is appropriate to keep momentum, but I am increasing the fan to 25% to begin shifting toward convective heat transfer and preparing for the smoke that will accompany the upcoming crack.'
    - .artisan-holdout/artisan-42 first-crack @ 661s bean=184.0°C ror=10.3 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just begun (5 seconds ago). The RoR is still quite high (10.3 C/min) for this stage. To ensure a controlled development and avoid a crash or an overly dark roast, I am cutting heat significantly and increasing airflow to transition to convective heat transfer, which will help manage the momentum as we approach the target drop temperature.'
    - .artisan-holdout/artisan-42 development @ 721s bean=194.3°C ror=10.3 real(heat/fan)=80/100: model heat=40% fan=100% drop=False conf=0.9 — 'The roast is at 194.3C with a RoR of 10.3C/min. We are approaching the 200.3C target. Given the thermal lag and the proximity to the 196C indicated ceiling, I am cutting heat significantly to 40% to prevent a spike while maintaining 100% fan to ensure convective development and smoke evacuation. We are not yet at the drop temperature, so we continue.'
    - .artisan-holdout/artisan-43 charge @ 1s bean=106.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current environment temperature (136C) is well below the target charge range (180C-200C). Maintaining zero heat and fan to allow the drum to continue soaking up energy toward the target charge temperature.'
    - .artisan-holdout/artisan-43 maillard @ 393s bean=135.0°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — "The roast is progressing steadily through the drying/early Maillard phase. With a bean RoR of 12.7 C/min and current temp at 135 C, we have sufficient momentum. I am maintaining full heat to ensure we don't stall before first crack, while slightly increasing fan to 20% to begin managing moisture and preparing for the transition to convective heat transfer."
    - .artisan-holdout/artisan-43 first-crack @ 633s bean=182.0°C ror=12.0 real(heat/fan)=100/10: model heat=70% fan=40% drop=False conf=0.9 — 'The RoR is flattening at 182C, indicating we are approaching first crack. To prepare for the development phase and avoid a spike, I am reducing heat to 70% and increasing fan to 40% to shift toward convective heat and manage the upcoming exothermic reaction. We are not at the drop temperature yet.'
    - .artisan-holdout/artisan-43 development @ 693s bean=195.7°C ror=13.7 real(heat/fan)=20/100: model heat=0% fan=100% drop=True conf=0.95 — 'The bean temperature is at 195.7°C, which is very close to the 196°C indicated ceiling. With a RoR of 13.7°C/min and 50 seconds of development, the roast is progressing rapidly. To avoid hitting the bitter/ashy threshold, I am recommending an immediate drop.'
    - .artisan-holdout/artisan-44 charge @ 120s bean=127.0°C ror=2.7 real(heat/fan)=100/0: model heat=100% fan=10% drop=False conf=0.9 — 'The bean RoR has plummeted to 2.7 C/min, indicating a stall or significant drop in energy absorption despite 100% heat. The recent telemetry shows a sharp decline in bean temperature. I need to maintain maximum heat to recover momentum and introduce minimal airflow to ensure heat distribution without cooling the drum too quickly. The roast is currently in danger of stalling.'
    - .artisan-holdout/artisan-44 maillard @ 481s bean=140.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is stalling at 140C with 100% heat and 0% fan. The RoR has dropped to 12C/min and is flattening. I need to introduce airflow to improve convective heat transfer and prevent the roast from stalling or baking, while maintaining high heat to push through the Maillard phase toward first crack. Increasing fan to 25% will help move the heat more effectively.'
    - .artisan-holdout/artisan-44 first-crack @ 721s bean=184.0°C ror=12.0 real(heat/fan)=70/30: model heat=50% fan=50% drop=False conf=0.9 — 'First crack has just begun (4 seconds ago). The RoR is still healthy at 12C/min. I am reducing heat to 50% to manage the momentum and increasing fan to 50% to transition to convective heat and clear smoke, ensuring a controlled development phase without stalling. We are well below the drop temperature and development target.'
    - .artisan-holdout/artisan-44 development @ 781s bean=196.0°C ror=12.0 real(heat/fan)=50/100: model heat=0% fan=100% drop=True conf=0.95 — 'The bean temperature has reached the 196C indicated ceiling, which is the hard limit for this roaster to avoid ashy/bitter results. Despite being slightly below the target drop temperature of 201C, the risk of over-roasting at this indicated temperature is too high. Development time is sufficient, and the RoR is still positive, so dropping now is the safest and most appropriate action to preserve quality.'
    - .artisan-holdout/artisan-45 charge @ 151s bean=104.0°C ror=-10.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (-10 C/min RoR) despite 100% heat, indicating a severe stall or sensor error following the charge. I am maintaining maximum heat to recover momentum. Fan remains at 0% to maximize conductive heat transfer and prevent further cooling.'
    - .artisan-holdout/artisan-45 maillard @ 512s bean=137.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. With the bean RoR at 11.7 C/min and the environment temperature rising, it is time to introduce airflow to transition toward convective heat transfer and prepare for the upcoming Maillard phase. Maintaining 100% heat while introducing moderate fan will help manage the momentum as we approach first crack.'
    - .artisan-holdout/artisan-45 first-crack @ 752s bean=182.0°C ror=11.7 real(heat/fan)=100/30: model heat=40% fan=60% drop=False conf=0.9 — 'First crack has just begun (6s ago). The RoR is still quite high (11.7 C/min) for this stage. I need to reduce heat significantly to prevent a runaway finish and increase fan to transition to convective heat, ensuring even development without scorching. I am targeting a drop at 201C, so I have time to manage the momentum.'
    - .artisan-holdout/artisan-45 development @ 782s bean=189.0°C ror=13.0 real(heat/fan)=80/90: model heat=80% fan=90% drop=False conf=0.9 — 'RoR is currently 13 C/min, which is healthy for this stage of development. We are 36 seconds into development with a target of 201 C. To maintain a controlled finish and avoid overshooting the target or stalling, I am maintaining current heat and fan settings to allow the momentum to carry us toward the drop temperature.'
    - .artisan-holdout/artisan-46 charge @ 243s bean=88.0°C ror=-13.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (17 seconds elapsed), but the bean temperature reading is erratic and dropping sharply (-13 C/min RoR). The current heat is at 100% with no fan. I need to maintain maximum heat to stabilize the drum and recover the momentum lost during the charge/early phase. The fan remains at 0% to ensure maximum conductive heat transfer until the bean temperature stabilizes and begins a consistent upward trend.'
    - .artisan-holdout/artisan-46 maillard @ 603s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily through the drying/early Maillard phase. RoR is healthy at 11.7 C/min. I am maintaining full heat to build momentum, but I am introducing 20% fan now to begin transitioning toward convective heat transfer and to ensure the environment is clear for the upcoming Maillard phase.'
    - .artisan-holdout/artisan-46 first-crack @ 843s bean=180.0°C ror=12.0 real(heat/fan)=100/0: model heat=60% fan=50% drop=False conf=0.9 — 'First crack has just started (3 seconds ago). RoR is still high (12 C/min). I need to reduce heat to manage the momentum and increase fan to transition to convective heat and clear smoke. Dropping heat to 60% and fan to 50% will help control the development phase without stalling the roast.'
    - .artisan-holdout/artisan-46 development @ 903s bean=194.7°C ror=14.7 real(heat/fan)=10/60: model heat=10% fan=60% drop=False conf=0.95 — 'The roast is at 194.7°C with a RoR of 14.7°C/min. While the target drop is 201.3°C, the current RoR is still quite high for this stage of development. I am keeping heat at 10% to allow the RoR to naturally taper as we approach the drop temperature, and maintaining 60% fan to ensure convective heat transfer and smoke evacuation. We are approaching the danger zone (196°C+), so I will monitor closely for the next adjustment.'
    - .artisan-holdout/artisan-47 charge @ 1s bean=132.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 163°C, which is below the recommended charge range (180°C-200°C). I am recommending to keep heat and fan at 0 to allow the roaster to continue preheating toward the target charge temperature.'
    - .artisan-holdout/artisan-47 maillard @ 392s bean=137.3°C ror=11.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily but the RoR is beginning to flatten out at 137C. To maintain momentum through the Maillard phase and prepare for first crack, I am increasing the fan to 25% to introduce convective heat and improve airflow, while maintaining 100% heat to ensure we don't stall before the crack."
    - .artisan-holdout/artisan-47 first-crack @ 662s bean=186.0°C ror=12.7 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack occurred at 638s. We are currently at 186C with a RoR of 12.7C/min. The target drop is 202C. With 16 seconds of development, we are early. Heat is at 100%, which is too high for this stage post-crack; I am reducing heat to 60% and increasing fan to 60% to manage the RoR and transition to convective heat for a controlled finish.'
    - .artisan-holdout/artisan-47 development @ 692s bean=191.7°C ror=12.0 real(heat/fan)=70/50: model heat=50% fan=70% drop=False conf=0.9 — 'The roast is in the development phase with 46 seconds elapsed since first crack. The current bean temperature is 191.7°C with a RoR of 12°C/min. To reach the target drop of 202°C, we need to maintain steady momentum without overshooting or stalling. I am reducing heat to 50% to manage the RoR and increasing fan to 70% to shift toward convective heat and ensure clean development as we approach the finish. We are not yet at the drop temperature.'

## prompt_version = v5

### google/gemini-3.1-flash-lite (ultra-flash)
- .artisan-holdout/artisan-29 (truth DTR 14.1%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.67 dir=0.826; fan MAE=12.71 dir=0.478; latency pre=1.12s preFC=1.15s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     4 |    14 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=23; diagonal agreement=0.826 — the more informative control-behaviour view)
- .artisan-holdout/artisan-30 (truth DTR 16.5%, 34/34 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=11.76 dir=0.848; fan MAE=12.5 dir=0.576; latency pre=0.9s preFC=1.12s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=33     |
    (total ticks=34; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     1 |     0 |     0 |
    |         hold |     5 |    26 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=33; diagonal agreement=0.848 — the more informative control-behaviour view)
- .artisan-holdout/artisan-31 (truth DTR 14.9%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-2.0s/-0.7°C; heat MAE=3.85 dir=0.96; fan MAE=17.88 dir=0.36; latency pre=0.92s preFC=1.07s FC=1.27s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     1 |    20 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.96 — the more informative control-behaviour view)
- .artisan-holdout/artisan-32 (truth DTR 16.9%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.88 dir=0.84; fan MAE=15.96 dir=0.44; latency pre=1.03s preFC=1.15s FC=1.13s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    17 |     0 |
    |        raise |     1 |     0 |     2 |
    (n=25; diagonal agreement=0.84 — the more informative control-behaviour view)
- .artisan-holdout/artisan-33 (truth DTR 18.3%, 24/24 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=9.58 dir=0.783; fan MAE=10.83 dir=0.478; latency pre=0.84s preFC=1.13s FC=1.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=23     |
    (total ticks=24; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     4 |    14 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=23; diagonal agreement=0.783 — the more informative control-behaviour view)
- .artisan-holdout/artisan-34 (truth DTR 20.9%, 30/30 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=5.67 dir=0.931; fan MAE=10 dir=0.517; latency pre=0.9s preFC=1.14s FC=1.17s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=29     |
    (total ticks=30; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    23 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=29; diagonal agreement=0.931 — the more informative control-behaviour view)
- .artisan-holdout/artisan-35 (truth DTR 13.7%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=4.4 dir=0.917; fan MAE=15.2 dir=0.375; latency pre=1.31s preFC=1.25s FC=1.07s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    18 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.917 — the more informative control-behaviour view)
- .artisan-holdout/artisan-36 (truth DTR 17.6%, 27/27 ok): drop F1=0.667 P=0.5 R=1.0 timing=-11.0s/-2.4°C; heat MAE=4.07 dir=1.0; fan MAE=14.26 dir=0.423; latency pre=1.4s preFC=1.15s FC=1.17s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=25     |
    (total ticks=27; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     0 |    20 |     0 |
    |        raise |     0 |     0 |     2 |
    (n=26; diagonal agreement=1.0 — the more informative control-behaviour view)
- .artisan-holdout/artisan-37 (truth DTR 14.0%, 25/25 ok): drop F1=0.667 P=0.5 R=1.0 timing=-1.0s/-0.3°C; heat MAE=10 dir=0.917; fan MAE=10.2 dir=0.458; latency pre=1.02s preFC=1.1s FC=1.24s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=23     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     3 |     0 |     0 |
    |         hold |     2 |    18 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.917 — the more informative control-behaviour view)
- .artisan-holdout/artisan-38 (truth DTR 13.3%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=8.08 dir=0.88; fan MAE=17.12 dir=0.36; latency pre=0.85s preFC=1.19s FC=1.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     2 |    19 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-holdout/artisan-39 (truth DTR 12.4%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=6.92 dir=0.88; fan MAE=17.31 dir=0.36; latency pre=0.79s preFC=1.06s FC=1.1s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    19 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.88 — the more informative control-behaviour view)
- .artisan-holdout/artisan-40 (truth DTR 14.2%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-12.0s/-2.0°C; heat MAE=9.81 dir=0.84; fan MAE=13.46 dir=0.44; latency pre=0.99s preFC=1.22s FC=1.34s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     3 |    16 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.84 — the more informative control-behaviour view)
- .artisan-holdout/artisan-41 (truth DTR 13.5%, 29/29 ok): drop F1=0.667 P=0.5 R=1.0 timing=-13.0s/-2.7°C; heat MAE=7.24 dir=0.857; fan MAE=12.76 dir=0.464; latency pre=1.07s preFC=1.19s FC=1.93s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=27     |
    (total ticks=29; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    21 |     0 |
    |        raise |     0 |     1 |     1 |
    (n=28; diagonal agreement=0.857 — the more informative control-behaviour view)
- .artisan-holdout/artisan-42 (truth DTR 12.7%, 26/26 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=11.15 dir=0.8; fan MAE=9.23 dir=0.52; latency pre=1.3s preFC=1.09s FC=1.26s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=25     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     5 |    17 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=25; diagonal agreement=0.8 — the more informative control-behaviour view)
- .artisan-holdout/artisan-43 (truth DTR 10.5%, 25/25 ok): drop F1=1.0 P=1.0 R=1.0 timing=+0.0s/+0.0°C; heat MAE=7.2 dir=0.875; fan MAE=11.2 dir=0.375; latency pre=1.36s preFC=1.17s FC=1.15s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=0      |  TN=24     |
    (total ticks=25; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    18 |     0 |
    |        raise |     0 |     0 |     1 |
    (n=24; diagonal agreement=0.875 — the more informative control-behaviour view)
- .artisan-holdout/artisan-44 (truth DTR 13.2%, 28/28 ok): drop F1=0.667 P=0.5 R=1.0 timing=-28.0s/-5.0°C; heat MAE=9.46 dir=0.889; fan MAE=13.57 dir=0.444; latency pre=1.29s preFC=1.18s FC=1.14s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=26     |
    (total ticks=28; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     4 |     0 |     0 |
    |         hold |     2 |    19 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=27; diagonal agreement=0.889 — the more informative control-behaviour view)
- .artisan-holdout/artisan-45 (truth DTR 12.3%, 29/29 ok): drop F1=0.667 P=0.5 R=1.0 timing=-19.0s/-5.0°C; heat MAE=6.55 dir=0.857; fan MAE=12.93 dir=0.5; latency pre=1.01s preFC=1.16s FC=1.27s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=27     |
    (total ticks=29; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    21 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=28; diagonal agreement=0.857 — the more informative control-behaviour view)
- .artisan-holdout/artisan-46 (truth DTR 13.6%, 33/33 ok): drop F1=0.667 P=0.5 R=1.0 timing=-4.0s/-0.6°C; heat MAE=8.48 dir=0.844; fan MAE=12.58 dir=0.469; latency pre=1.01s preFC=1.21s FC=1.12s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=31     |
    (total ticks=33; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     4 |    24 |     1 |
    |        raise |     0 |     0 |     1 |
    (n=32; diagonal agreement=0.844 — the more informative control-behaviour view)
- .artisan-holdout/artisan-47 (truth DTR 12.8%, 26/26 ok): drop F1=0.667 P=0.5 R=1.0 timing=-18.0s/-4.0°C; heat MAE=8.08 dir=0.84; fan MAE=19.04 dir=0.28; latency pre=1.29s preFC=1.12s FC=1.45s
    drop confusion (rows = actual drop?, cols = model said drop):
    |            | model: drop | model: hold |
    |------------|-------------|-------------|
    | real: drop |  TP=1      |  FN=0      |
    | real: hold |  FP=1      |  TN=24     |
    (total ticks=26; TN dominates — read WITH drop-timing error)
    heat-direction confusion (rows = real move, cols = model move):
    |  real \ model |   cut |  hold | raise |
    |--------------|-------|-------|-------|
    |          cut |     2 |     0 |     0 |
    |         hold |     3 |    18 |     0 |
    |        raise |     1 |     0 |     1 |
    (n=25; diagonal agreement=0.84 — the more informative control-behaviour view)

  advice samples (operator judges quality — agreement ≠ correct):
    - .artisan-holdout/artisan-29 charge @ 60s bean=110.0°C ror=-1.0 real(heat/fan)=100/40: model heat=100% fan=40% drop=False conf=0.9 — 'The bean RoR has plummeted to -1.0 C/min, indicating a stall or significant measurement error immediately following the charge. With heat at 100% and the bean temperature dropping, the roast is in danger of stalling. I am maintaining maximum heat to recover momentum and keeping the fan at 40% to ensure sufficient airflow without prematurely cooling the drum.'
    - .artisan-holdout/artisan-29 maillard @ 391s bean=136.0°C ror=11.7 real(heat/fan)=100/10: model heat=100% fan=10% drop=False conf=0.9 — 'The roast is progressing steadily at 136C. With a bean RoR of 11.7C/min, we are in a good position to approach first crack. I am maintaining full heat to ensure sufficient momentum through the Maillard phase, while keeping fan low to prioritize conductive heat transfer for body development before the convective shift required post-crack.'
    - .artisan-holdout/artisan-29 first-crack @ 601s bean=179.7°C ror=13.7 real(heat/fan)=80/0: model heat=40% fan=40% drop=False conf=0.9 — 'First crack has just begun (2 seconds in). The RoR is currently 13.7 C/min, which is quite high for this stage. To manage the development phase and prevent an uncontrolled spike toward the 198 C target, I am cutting heat significantly and increasing airflow to transition to convective heat transfer, which will help stabilize the roast and manage the smoke/chaff from the crack.'
    - .artisan-holdout/artisan-29 development @ 661s bean=193.0°C ror=13.3 real(heat/fan)=10/90: model heat=0% fan=90% drop=False conf=0.95 — 'Current development ratio is 10.1% (62s / 611s), approaching the 14.1% target. Bean temperature is 193C with a steady RoR of 13.3C/min. To reach the target drop temp of 198C without overshooting or stalling, I am cutting heat to 0% to coast and maintaining high airflow to ensure clean development. Drop is imminent.'
    - .artisan-holdout/artisan-30 charge @ 212s bean=124.5°C ror=25.5 real(heat/fan)=0/10: model heat=0% fan=10% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and is approaching the target charge temperature range (180-200C). The environment temperature is rising steadily. I am setting heat to 0% to maintain control while waiting for the target charge temperature to be reached.'
    - .artisan-holdout/artisan-30 maillard @ 603s bean=137.0°C ror=10.3 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily but the RoR is beginning to flatten (10.3 C/min). To maintain momentum through the Maillard phase and prepare for first crack, I am introducing moderate airflow to shift toward convective heat transfer while maintaining full heat to prevent a stall. This will help ensure a clean transition into first crack.'
    - .artisan-holdout/artisan-30 first-crack @ 873s bean=180.0°C ror=8.0 real(heat/fan)=100/30: model heat=50% fan=60% drop=False conf=0.95 — 'The roast is in early development (13s post-FC). The current RoR is 8C/min, which is healthy but needs to be managed to avoid overshooting the 198C target. I am cutting heat to 50% to manage the momentum and increasing fan to 60% to shift toward convective heat and clear smoke. The development ratio is currently ~2%, well below the 16.5% target, so we are not ready to drop.'
    - .artisan-holdout/artisan-30 development @ 933s bean=187.3°C ror=7.3 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.95 — 'Current development ratio is ~10.2% (73s / 718s). The target is 16.5%, which equates to ~118s total development time. With 45s remaining, the RoR is 7.3 C/min, which is healthy. I am maintaining high heat to ensure we reach the target drop temp of 198C without stalling, while keeping fan at 30% to maintain convective efficiency without crashing the RoR. Will monitor for the final 45 seconds.'
    - .artisan-holdout/artisan-31 charge @ 0s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 123°C, which is below the target charge range of 180°C-200°C. I am maintaining heat and fan at 0 to allow the drum to continue soaking and reach the target charge temperature.'
    - .artisan-holdout/artisan-31 maillard @ 362s bean=134.7°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 134.7C with 100% heat and 0% fan. To prepare for the approach to first crack and ensure convective heat transfer, I am initiating a fan increase to 25% while maintaining full heat to keep the momentum steady as we enter the Maillard phase.'
    - .artisan-holdout/artisan-31 first-crack @ 632s bean=181.0°C ror=9.0 real(heat/fan)=100/60: model heat=100% fan=60% drop=False conf=0.9 — 'The roast is in early development (15s post-FC). The current development ratio is ~2.4%, well below the 14.9% target. RoR is stable at 9C/min. I am maintaining high heat to ensure momentum through the development phase while keeping fan at 60% to balance convective heat transfer without stalling the RoR. I will continue to monitor for the target development ratio and the 196C ceiling.'
    - .artisan-holdout/artisan-31 development @ 692s bean=192.0°C ror=11.0 real(heat/fan)=60/80: model heat=40% fan=80% drop=False conf=0.95 — 'Current development ratio is 10.9% (75s / 685s), approaching the 14.9% target. Bean temp is 192C with a steady RoR of 11C/min. To reach the target drop temp of 198C without overshooting or stalling, I am reducing heat to 40% to manage the momentum while maintaining 80% fan for convective consistency. We are close to the drop; monitoring for the final 4% development.'
    - .artisan-holdout/artisan-32 charge @ 61s bean=96.5°C ror=-16.5 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is experiencing a severe, unexplained drop in bean temperature (RoR -16.5 C/min) despite maximum heat input. This suggests a sensor error or a major thermal instability. I am maintaining maximum heat to attempt to recover the energy, but the situation is critical. Fan remains at 0 to maximize conductive heat transfer.'
    - .artisan-holdout/artisan-32 maillard @ 392s bean=136.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — "The roast is progressing steadily at 136C. With RoR at 12C/min and no first crack yet, we need to maintain momentum while preparing for the transition to convective heat. Increasing fan to 25% will help manage the environment and prepare for the upcoming Maillard phase, while keeping heat at 100% ensures we don't stall before first crack."
    - .artisan-holdout/artisan-32 first-crack @ 633s bean=179.3°C ror=10.3 real(heat/fan)=100/40: model heat=60% fan=60% drop=False conf=0.95 — 'First crack has just begun (5 seconds in). The bean RoR is still healthy at 10.3 C/min. To manage the development phase and prevent an uncontrolled spike, I am reducing heat to 60% to moderate the energy input while increasing fan to 60% to shift toward convective heat and ensure smoke evacuation. We are far from the target drop temperature and development percentage, so we continue roasting.'
    - .artisan-holdout/artisan-32 development @ 693s bean=188.0°C ror=8.7 real(heat/fan)=100/50: model heat=100% fan=70% drop=False conf=0.95 — 'Current development ratio is 10.0% (65s / 647s), well below the 16.9% target. RoR is 8.7 C/min, which is healthy but needs to be managed to avoid hitting the 198 C drop target too quickly or overshooting. I am maintaining heat to ensure momentum but increasing fan to 70% to shift toward convective heat and manage the environment temperature as we approach the final stages. Drop is not yet indicated.'
    - .artisan-holdout/artisan-33 charge @ 1s bean=127.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current environment temperature (153C) is below the target charge range (180C-200C). Maintaining zero heat and fan to allow the drum to continue soaking and rising toward the target charge temperature.'
    - .artisan-holdout/artisan-33 maillard @ 332s bean=135.0°C ror=12.0 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The RoR has flattened significantly (12 C/min and stalling at 135 C). To prevent a stall and maintain momentum toward first crack, I need to increase convective heat transfer and ensure the environment temperature continues to climb. Increasing fan to 30% will help move the heat, and keeping heat at 100% is necessary to overcome the current stagnation.'
    - .artisan-holdout/artisan-33 first-crack @ 572s bean=179.0°C ror=11.3 real(heat/fan)=90/40: model heat=70% fan=60% drop=False conf=0.9 — 'The roast is in early development (12s post-FC). The RoR is healthy at 11.3 C/min. To manage the approach to the target drop temperature (198.3 C) and avoid an uncontrolled spike, I am reducing heat to 70% and increasing fan to 60% to shift toward convective cooling and ensure a controlled, steady finish. We are well below the development target of 18.3%.'
    - .artisan-holdout/artisan-33 development @ 632s bean=189.7°C ror=10.7 real(heat/fan)=50/50: model heat=50% fan=50% drop=False conf=0.95 — 'Current development ratio is 11.5% (72s / 624s), well below the 18.3% target. Bean temperature is 189.7°C with a manageable RoR of 10.7°C/min. I am maintaining heat and fan to ensure steady development toward the target drop temperature of 198.3°C without stalling.'
    - .artisan-holdout/artisan-34 charge @ 151s bean=111.3°C ror=25.0 real(heat/fan)=0/10: model heat=0% fan=10% drop=False conf=0.9 — 'The roaster is currently in the preheating phase and has not yet reached the charge temperature range (180-200C). I am maintaining heat at 0% and fan at 10% to allow the drum to continue soaking and rising toward the target charge temperature.'
    - .artisan-holdout/artisan-34 maillard @ 481s bean=124.0°C ror=12.3 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — 'The bean RoR has dropped significantly to 12.3 C/min, indicating the roast is losing momentum well before the Maillard phase is fully established. With the heat at 100% and fan at 10%, the current energy input is insufficient to maintain a healthy trajectory. I am increasing heat to maintain momentum and slightly increasing fan to improve convective efficiency as we approach the Maillard transition.'
    - .artisan-holdout/artisan-34 first-crack @ 722s bean=169.7°C ror=11.4 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — 'First crack has just begun (7 seconds ago). The RoR is currently 11.4 C/min, which is healthy for this stage. To manage the development phase and ensure a smooth transition toward the target drop temperature of 198.7 C, I am maintaining high heat to sustain momentum while increasing the fan to 50% to introduce more convective heat and manage the smoke/chaff generated by the crack. This balance will prevent stalling while ensuring even development.'
    - .artisan-holdout/artisan-34 development @ 812s bean=188.0°C ror=13.0 real(heat/fan)=90/50: model heat=60% fan=70% drop=False conf=0.95 — 'Current development ratio is ~14.7% (97s / 658s), well below the 20.9% target. Bean RoR is 13 C/min, which is healthy but needs to be managed to avoid overshooting the 198.7 C drop target. I am cutting heat to 60% to manage the momentum and increasing fan to 70% to shift toward convective heat and ensure clean development as we approach the drop. We are not yet at the drop criteria.'
    - .artisan-holdout/artisan-35 charge @ 0s bean=123.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 123°C, which is below the target charge range of 180°C-200°C. I am maintaining heat and fan at 0 to allow the drum to continue preheating toward the target charge temperature.'
    - .artisan-holdout/artisan-35 maillard @ 361s bean=133.0°C ror=11.3 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 133°C with 100% heat and 0% fan. To prepare for the approach to first crack and ensure convective heat transfer, I am increasing the fan to 30% while maintaining full heat to keep the RoR from dropping prematurely. This transition will help manage the environment temperature and prepare for the upcoming exothermic phase.'
    - .artisan-holdout/artisan-35 first-crack @ 632s bean=182.0°C ror=11.3 real(heat/fan)=80/30: model heat=60% fan=50% drop=False conf=0.9 — 'The roast is in early development (15s post-FC). The RoR is healthy at 11.3 C/min. To manage the momentum toward the 198.7 C target and ensure a smooth finish without stalling, I am reducing heat to 60% and increasing fan to 50% to shift toward convective heat and manage the internal bean energy. Development ratio is currently ~2.4%, well below the 13.7% target.'
    - .artisan-holdout/artisan-35 development @ 662s bean=189.3°C ror=13.0 real(heat/fan)=50/90: model heat=50% fan=90% drop=False conf=0.95 — 'Current development time is 45s out of 652s total (6.9%). The target development is 13.7%, which requires ~89s total development time. With RoR at 13C/min and bean temp at 189.3C, we have room to continue. Maintaining current heat/fan settings to ensure a steady, controlled finish toward the 198.7C target.'
    - .artisan-holdout/artisan-36 charge @ 0s bean=125.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase. The current environment temperature (166C) is below the target charge range (180C-200C). I am maintaining heat and fan at 0 to allow the drum to continue soaking and rising toward the target charge temperature.'
    - .artisan-holdout/artisan-36 maillard @ 392s bean=136.7°C ror=10.7 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 136.7C with 0% fan. To ensure even heat distribution and prepare for the upcoming Maillard phase and first crack, I am introducing convective airflow. Heat remains at 100% to maintain momentum as we approach the target drop temperature.'
    - .artisan-holdout/artisan-36 first-crack @ 632s bean=176.0°C ror=9.0 real(heat/fan)=100/30: model heat=100% fan=50% drop=False conf=0.9 — 'First crack has just begun (2 seconds in). The RoR is currently 9.0 C/min, which is healthy for the start of development. I am maintaining full heat to ensure momentum through the initial crack phase while increasing fan to 50% to improve convective heat transfer and begin clearing smoke/chaff, preparing for the transition toward the drop. The development ratio is currently very low; we have significant time before reaching the 17.6% target.'
    - .artisan-holdout/artisan-36 development @ 722s bean=190.7°C ror=9.7 real(heat/fan)=100/30: model heat=100% fan=30% drop=False conf=0.9 — 'The current development ratio is 12.9% (92s / 713s), which is approaching the 17.6% target. The bean temperature is 190.7°C, and the RoR is 9.7°C/min. With 8°C remaining to the target drop temperature of 198.7°C, and the heat currently at 100%, I need to maintain momentum to reach the target without stalling, but prepare for the drop. I will keep heat high to ensure we reach the target temperature efficiently, while keeping fan at 30% to maintain convective heat transfer without crashing the RoR. We are not yet at the drop point.'
    - .artisan-holdout/artisan-37 charge @ 91s bean=98.0°C ror=-7.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is experiencing a severe crash in bean temperature (RoR -7.0 C/min) immediately after charge. The bean temperature is dropping rapidly despite 100% heat. This indicates the drum was likely under-charged or the thermal mass is insufficient to overcome the initial heat sink. I am maintaining 100% heat and keeping the fan at 0% to maximize conductive heat transfer and attempt to recover the RoR before the roast stalls completely.'
    - .artisan-holdout/artisan-37 maillard @ 392s bean=137.3°C ror=13.0 real(heat/fan)=100/10: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With a bean RoR of 13.0 C/min and bean temp at 137.3 C, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but increasing the fan to 25% to begin shifting toward convective heat transfer and preparing for the smoke/chaff management required as we approach first crack.'
    - .artisan-holdout/artisan-37 first-crack @ 602s bean=179.0°C ror=12.0 real(heat/fan)=100/10: model heat=70% fan=30% drop=False conf=0.9 — 'Approaching first crack (179C indicated). RoR is healthy at 12C/min. To prepare for the exothermic phase and ensure convective heat transfer for an even roast, I am increasing fan to 30% and reducing heat to 70% to manage the momentum before crack begins.'
    - .artisan-holdout/artisan-37 development @ 662s bean=192.7°C ror=13.7 real(heat/fan)=60/80: model heat=40% fan=80% drop=False conf=0.95 — 'The current development ratio is 9.4% (55s / 585s), well below the 14% target. Bean temperature is 192.7°C, approaching the 196°C ceiling. RoR is 13.7°C/min and stable. To reach the target development without hitting the bitter ceiling, I am reducing heat to 40% to slow the momentum while maintaining 80% fan to ensure convective heat transfer and smoke evacuation. We are approximately 25-30 seconds away from the target development time.'
    - .artisan-holdout/artisan-38 charge @ 31s bean=88.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (charge/drying). The bean temperature drop is expected as the beans absorb heat, but the current telemetry shows a rapid decline in bean temperature (98.5C to 88.0C in 5 seconds). Heat is already at 100%. I will maintain maximum heat to recover momentum and keep the fan at 0% to ensure maximum conductive heat transfer during this initial phase.'
    - .artisan-holdout/artisan-38 maillard @ 392s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily at 134.7C. With RoR at 11.7C/min and no fan usage yet, it is time to introduce airflow to manage the transition toward first crack and ensure convective heat transfer. I am maintaining full heat to keep momentum, but introducing 25% fan to begin clearing moisture and preparing for the upcoming exothermic phase.'
    - .artisan-holdout/artisan-38 first-crack @ 662s bean=185.0°C ror=12.3 real(heat/fan)=100/0: model heat=60% fan=40% drop=False conf=0.9 — 'The roast is currently at 185C with a RoR of 12.3C/min. Development time is 14s (2.2% of total time). The target drop is 199C. With 100% heat and 0% fan, the RoR is likely to spike or remain too high, risking a crash or uneven development. I am cutting heat to 60% and increasing fan to 40% to transition to convective heat, manage the RoR, and prepare for a smooth finish toward the 199C target. Drop is not yet indicated.'
    - .artisan-holdout/artisan-38 development @ 692s bean=190.3°C ror=12.3 real(heat/fan)=60/70: model heat=40% fan=80% drop=False conf=0.95 — 'Current development ratio is 6.5% (44s / 675s). The target is 13.3%, which equates to approximately 90 seconds of development time. With a current RoR of 12.3 C/min and bean temp at 190.3 C, we are approaching the ceiling. I am reducing heat to 40% to manage the momentum and increasing fan to 80% to shift toward convective cooling and ensure clean development as we approach the 196 C limit. We are not yet at the drop target.'
    - .artisan-holdout/artisan-39 charge @ 1s bean=112.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The bean temperature is 112°C, which is below the recommended charge range (180°C-200°C). I am maintaining heat and fan at 0 to allow the drum to continue preheating toward the target charge temperature.'
    - .artisan-holdout/artisan-39 maillard @ 393s bean=136.0°C ror=10.3 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but the RoR is low (10.3 C/min) for this stage. With heat at 100% and fan at 0%, I need to introduce airflow to transition toward convective heat transfer and prepare for the upcoming Maillard phase, while maintaining enough energy to keep the RoR from crashing. Increasing fan to 25% while holding heat at 100% will improve heat distribution without stalling the momentum.'
    - .artisan-holdout/artisan-39 first-crack @ 663s bean=184.0°C ror=10.0 real(heat/fan)=100/50: model heat=60% fan=70% drop=False conf=0.95 — 'First crack occurred at 649s. Current development time is 6s. Total roast time is 655s. Target development is 12.4% (approx 81s total dev time). We are very early in development. RoR is 10C/min, which is healthy. Maintaining 100% heat is too aggressive now that we are in the development phase; I am reducing heat to 60% to manage the momentum and increasing fan to 70% to shift toward convective heat and ensure clean development. Drop is not yet indicated.'
    - .artisan-holdout/artisan-39 development @ 723s bean=194.7°C ror=10.7 real(heat/fan)=50/80: model heat=0% fan=80% drop=False conf=0.95 — 'The current development ratio is ~9.2% (66s / 715s), approaching the 12.4% target. With bean temperature at 194.7°C and a steady RoR of 10.7°C/min, we are very close to the 196°C ceiling. To avoid overshooting or hitting the bitter threshold, I am cutting heat to 0% to coast into the drop, while maintaining 80% fan to ensure clean convective airflow during these final seconds. Drop is imminent.'
    - .artisan-holdout/artisan-40 charge @ 61s bean=121.0°C ror=23.0 real(heat/fan)=0/0: model heat=80% fan=0% drop=False conf=0.9 — 'The roast is currently in a stall or significant dip (RoR is dropping, bean temp is falling). Heat is at 0% and fan is at 0%. I need to apply heat immediately to recover momentum and establish a steady drying/early Maillard phase. I am setting heat to 80% to aggressively reverse the temperature drop, while keeping fan at 0% to maximize conductive heat transfer and prevent further stalling.'
    - .artisan-holdout/artisan-40 maillard @ 392s bean=131.0°C ror=12.3 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 131C. With RoR at 12.3C/min and no first crack yet, I am maintaining full heat to ensure sufficient momentum into the Maillard phase. I am introducing 20% fan now to begin clearing moisture and preparing for the transition to convective heat transfer as we approach the crack.'
    - .artisan-holdout/artisan-40 first-crack @ 633s bean=179.0°C ror=12.3 real(heat/fan)=100/10: model heat=70% fan=30% drop=False conf=0.9 — 'The bean temperature is approaching the typical first crack range (180-185C indicated). With RoR at 12.3 C/min and holding steady, I need to prepare for the exothermic reaction of first crack. I am reducing heat to 70% to prevent a spike and increasing fan to 30% to improve convective heat transfer and prepare for smoke evacuation. This coordinated move will help manage the transition into development.'
    - .artisan-holdout/artisan-40 development @ 693s bean=192.7°C ror=13.7 real(heat/fan)=30/60: model heat=0% fan=60% drop=False conf=0.95 — 'Current development ratio is 8.5% (54s / 634s), well below the 14.2% target. Bean temp is 192.7C, approaching the 196C ceiling. RoR is 13.7C/min and stable. I am cutting heat to 0% to coast toward the target drop temp of 200C while maintaining 60% fan to ensure convective heat transfer and smoke evacuation without stalling the roast. Drop will occur once the development ratio hits 14.2% (approx. 90s total development time).'
    - .artisan-holdout/artisan-41 charge @ 121s bean=113.0°C ror=23.0 real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is in the preheating phase and is ready for charging. The current environment temperature (135.7C) is below the recommended charge range (180C-200C). I am recommending 0 heat and 0 fan to continue preheating until the target charge temperature is reached.'
    - .artisan-holdout/artisan-41 maillard @ 482s bean=131.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=25% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 131°C with 0% fan. To prepare for the Maillard phase and ensure even heat distribution, I am introducing 25% fan to shift toward convective heat transfer. I am maintaining 100% heat to keep the RoR from dropping too early, as we are still well below the first crack threshold.'
    - .artisan-holdout/artisan-41 first-crack @ 722s bean=178.0°C ror=12.0 real(heat/fan)=100/10: model heat=60% fan=40% drop=False conf=0.9 — 'The bean temperature is approaching the typical first crack range (180-185C indicated). The RoR has slowed significantly to 12C/min. To prepare for the exothermic reaction of first crack and avoid a spike, I am reducing heat to 60% and increasing fan to 40% to transition toward convective heat transfer and clear smoke. This coordinated move prevents stalling while managing the energy transition.'
    - .artisan-holdout/artisan-41 development @ 782s bean=192.0°C ror=14.0 real(heat/fan)=30/50: model heat=20% fan=60% drop=False conf=0.95 — 'Current development ratio is ~7.8% (51s / 652s). The target is 13.5%, which equates to ~88 seconds of development. With 37 seconds remaining and a steady RoR of 14C/min, we are on track to hit the target drop temp of 200C. Reducing heat slightly to 20% to manage the final approach and increasing fan to 60% to ensure clean convective heat and smoke evacuation as we approach the finish.'
    - .artisan-holdout/artisan-42 charge @ 30s bean=85.0°C ror=None real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The roast is in the very early stages (18 seconds in). The bean temperature is dropping rapidly (from 94.5C to 85C), likely due to the thermal mass of the drum absorbing the initial charge energy. Heat is already at 100% and fan is at 0% to maximize energy input. I will maintain these settings to recover momentum and establish a steady rate of rise.'
    - .artisan-holdout/artisan-42 maillard @ 391s bean=137.0°C ror=11.3 real(heat/fan)=100/10: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily but is still well below first crack. With a bean RoR of 11.3 C/min and a current bean temp of 137 C, we need to maintain momentum to reach the crack without stalling. Keeping heat at 100% is appropriate for now, but I am increasing the fan to 30% to begin transitioning toward convective heat transfer and preparing for the smoke that will accompany the upcoming crack.'
    - .artisan-holdout/artisan-42 first-crack @ 661s bean=184.0°C ror=10.3 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack has just begun (5s in). The RoR is still healthy at 10.3 C/min. To manage the development phase and prevent an uncontrolled spike, I am cutting heat to 60% and increasing fan to 60% to shift toward convective cooling and ensure even development. We are far from the target drop temperature and development percentage, so we continue.'
    - .artisan-holdout/artisan-42 development @ 721s bean=194.3°C ror=10.3 real(heat/fan)=80/100: model heat=0% fan=100% drop=False conf=0.95 — 'The current bean temperature is 194.3°C, approaching the 196°C ceiling. The development ratio is currently ~9.2% (65s / 709s), and the target is 12.7%. With a RoR of 10.3°C/min, the roast is still moving quickly. I am cutting heat to 0% to coast into the drop and maintaining maximum fan to ensure convective cooling and prevent stalling while approaching the target drop temperature of 200.3°C. Drop is imminent as we approach the temperature ceiling and development target.'
    - .artisan-holdout/artisan-43 charge @ 1s bean=106.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase. The environment temperature is 136°C, which is well below the target charge range of 180°C-200°C. I am maintaining heat and fan at 0 to allow the roaster to continue climbing toward the charge temperature.'
    - .artisan-holdout/artisan-43 maillard @ 393s bean=135.0°C ror=12.7 real(heat/fan)=100/10: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily but slowly. At 135C, we are well into the Maillard phase. The RoR is low (12.7 C/min), and the environment temperature is stable. To ensure we maintain momentum toward first crack without stalling, I am maintaining full heat and slightly increasing the fan to 20% to improve convective heat transfer and prepare for the upcoming moisture release.'
    - .artisan-holdout/artisan-43 first-crack @ 633s bean=182.0°C ror=12.0 real(heat/fan)=100/10: model heat=70% fan=30% drop=False conf=0.9 — 'The bean temperature is approaching the critical pre-crack zone (182C). With RoR at 12C/min and heat at 100%, we need to prepare for the exothermic reaction of first crack. I am reducing heat to 70% to manage the momentum and increasing fan to 30% to shift toward convective heat and prepare for smoke evacuation. This prevents a spike during crack.'
    - .artisan-holdout/artisan-43 development @ 693s bean=195.7°C ror=13.7 real(heat/fan)=20/100: model heat=0% fan=100% drop=False conf=0.95 — 'The bean temperature is at 195.7°C, very close to the 196°C ceiling. The development time is 59.8 seconds (692.8 - 633), which is 8.6% of the total roast time (692.8). The target development is 10.5%, which would be reached at approximately 72.7 seconds of development time (total time ~705s). Given the current RoR of 13.7°C/min and the proximity to the hard ceiling, I am cutting heat to 0% to coast to the drop temperature safely without stalling or overshooting. Fan remains at 100% to manage the final development phase. Drop is imminent.'
    - .artisan-holdout/artisan-44 charge @ 120s bean=127.0°C ror=2.7 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR has crashed significantly (from positive to negative) while heat is at 100%. This indicates a potential sensor error or a severe stall. Given the rapid drop in bean temperature (141C to 127C in 5 seconds), I am maintaining maximum heat to attempt to recover the momentum, but the situation is unstable. Fan remains at 0 to maximize conductive heat transfer.'
    - .artisan-holdout/artisan-44 maillard @ 481s bean=140.0°C ror=12.0 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'RoR has stalled at 140C despite 100% heat. The bean mass is struggling to gain momentum. I am increasing fan to 20% to introduce convective heat transfer and help pull the heat through the bean mass, while maintaining 100% heat to push through the current plateau.'
    - .artisan-holdout/artisan-44 first-crack @ 721s bean=184.0°C ror=12.0 real(heat/fan)=70/30: model heat=50% fan=50% drop=False conf=0.9 — 'First crack has just begun (4 seconds in). The RoR is currently 12 C/min, which is appropriate for the start of development. I am reducing heat to 50% to manage the momentum and increasing fan to 50% to shift toward convective heat transfer, ensuring even development and smoke evacuation as we approach the target drop temperature. We are far from the 13.2% development target, so we continue the roast.'
    - .artisan-holdout/artisan-44 development @ 781s bean=196.0°C ror=12.0 real(heat/fan)=50/100: model heat=0% fan=100% drop=True conf=0.95 — 'The bean temperature has reached the 196C ceiling, which is the hard limit for this roaster to avoid ashy/bitter flavors. The development ratio is currently 9.5% (64s / 671s), which is below the 13.2% target, but continuing to develop at this temperature will result in a baked or burnt roast. Given the RoR is still 12C/min and the temperature is at the ceiling, the roast must be dropped immediately to preserve quality.'
    - .artisan-holdout/artisan-45 charge @ 151s bean=104.0°C ror=-10.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean RoR has crashed to -10 C/min despite 100% heat. This indicates a severe stall or a sensor/telemetry error. Given the rapid drop in bean temperature (119C to 104C in 6 seconds), the roast is in danger of stalling completely. I am maintaining 100% heat to attempt to recover momentum and keeping fan at 0% to maximize conductive heat transfer. If the temperature does not recover immediately, the roast may be compromised.'
    - .artisan-holdout/artisan-45 maillard @ 512s bean=137.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=20% drop=False conf=0.9 — 'The roast is progressing steadily at 137.7C. With RoR at 11.7C/min, we are approaching the transition to first crack. I am maintaining full heat to ensure sufficient momentum, but I am initiating a small fan increase to 20% to begin clearing moisture and preparing for the convective phase as we approach the crack.'
    - .artisan-holdout/artisan-45 first-crack @ 752s bean=182.0°C ror=11.7 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'First crack has just begun (6 seconds ago). The RoR is currently 11.7 C/min, which is healthy but needs to be managed to avoid overshooting the target drop temperature of 201 C. I am reducing heat to 60% to manage the momentum and increasing fan to 60% to shift toward convective heat and clear smoke, ensuring a controlled development phase. We are far from the 12.3% development target, so we continue the roast.'
    - .artisan-holdout/artisan-45 development @ 782s bean=189.0°C ror=13.0 real(heat/fan)=80/90: model heat=80% fan=90% drop=False conf=0.95 — 'Current development ratio is ~5.6% (36s / 644s), well below the 12.3% target. RoR is 13 C/min, which is healthy but needs to be managed to avoid hitting the 196 C ceiling too quickly. Maintaining heat at 80% while keeping fan at 90% provides sufficient convective energy to continue development without stalling, while preparing for a smooth finish as we approach the 201 C drop target.'
    - .artisan-holdout/artisan-46 charge @ 243s bean=88.0°C ror=-13.0 real(heat/fan)=100/0: model heat=100% fan=0% drop=False conf=0.9 — 'The bean temperature is dropping rapidly (negative RoR) despite maximum heat input, likely due to a sensor anomaly or a significant environmental disturbance. Since the heat is already at 100%, I must maintain maximum heat to recover the momentum and prevent a stall. Fan remains at 0% to conserve all available thermal energy.'
    - .artisan-holdout/artisan-46 maillard @ 603s bean=134.7°C ror=11.7 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily through the Maillard phase. With bean temperature at 134.7°C and a stable RoR, it is time to introduce airflow to transition toward convective heat transfer and prepare for the upcoming first crack. I am increasing the fan to 30% to manage moisture and smoke, while maintaining full heat to ensure sufficient momentum into the crack.'
    - .artisan-holdout/artisan-46 first-crack @ 843s bean=180.0°C ror=12.0 real(heat/fan)=100/0: model heat=60% fan=40% drop=False conf=0.95 — 'First crack has just begun (3 seconds ago). The RoR is still healthy at 12C/min, but I need to manage the energy to avoid a spike while approaching the target drop temperature. I am cutting heat to 60% to manage the momentum and increasing fan to 40% to introduce convective heat and clear smoke. The development ratio is currently very low; I will continue to monitor for the target development percentage.'
    - .artisan-holdout/artisan-46 development @ 903s bean=194.7°C ror=14.7 real(heat/fan)=10/60: model heat=10% fan=60% drop=False conf=0.95 — 'The current development ratio is 9.3% (63s / 677s), approaching the target of 13.6%. The bean temperature is 194.7°C, very close to the 196°C ceiling. With a RoR of 14.7°C/min, the roast is moving quickly. I am keeping heat at 10% to prevent a spike and maintaining fan at 60% to manage the roast. I will drop as soon as the development ratio hits the target or the temperature approaches the 196°C limit.'
    - .artisan-holdout/artisan-47 charge @ 1s bean=132.0°C ror=None real(heat/fan)=0/0: model heat=0% fan=0% drop=False conf=1.0 — 'The roaster is currently in the preheating phase and has not yet reached the target charge temperature (180-200C). Maintaining zero heat and fan is appropriate until the target charge temperature is reached.'
    - .artisan-holdout/artisan-47 maillard @ 392s bean=137.3°C ror=11.3 real(heat/fan)=100/0: model heat=100% fan=30% drop=False conf=0.9 — 'The roast is progressing steadily but is currently at 137C with 0% fan. To prepare for the upcoming Maillard phase and first crack, I need to introduce convective heat transfer to ensure even development and manage the environment temperature. Increasing fan to 30% while maintaining heat will help stabilize the RoR and prepare the beans for the transition.'
    - .artisan-holdout/artisan-47 first-crack @ 662s bean=186.0°C ror=12.7 real(heat/fan)=100/30: model heat=60% fan=60% drop=False conf=0.9 — 'The roast is in early development (16s post-FC). The current development ratio is ~2.4%, well below the 12.8% target. The RoR is healthy at 12.7 C/min. To manage the momentum toward the 202 C drop target without overshooting or stalling, I am reducing heat to 60% to manage the energy input and increasing fan to 60% to shift toward convective heat and ensure clean development.'
    - .artisan-holdout/artisan-47 development @ 692s bean=191.7°C ror=12.0 real(heat/fan)=70/50: model heat=40% fan=70% drop=False conf=0.95 — 'Current development ratio is ~6.7% (46s/684s), well below the 12.8% target. Bean temperature is 191.7°C, approaching the 196°C ceiling. RoR is 12°C/min, which is healthy but needs to be managed to avoid overshooting the drop temperature or hitting the ceiling too quickly. I am cutting heat to 40% to manage the momentum and increasing fan to 70% to shift to convective cooling and ensure clean development as we approach the target. Drop is not yet indicated.'