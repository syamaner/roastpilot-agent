# Roast 9/10 A/B Trace Analysis — #405 Post-FC Control Rework

Source: `~/roasts/roastpilot.sqlite3`, WAL-safe `.backup` copy analyzed at
`/private/tmp/claude-501/-Users-sertanyamaner-git-roastpilot-agent/53ae8da9-ca9c-4cf1-93a3-4761fcfa16b2/scratchpad/trace.sqlite3`.
Cross-checked against `roast_events` (`command_executed`, absolute UTC clock),
`telemetry_snapshots` (absolute UTC `recorded_at_utc` + its own `raw_state_json`),
`advisor_decisions.decision_json`, and `safety_evaluations`.

**Correlation method used:** both `telemetry_snapshots` and `roast_events` carry an
absolute `recorded_at_utc` column, so joins were done on that directly — no monotonic
rebase was needed (verified: the FC `roast_events` row lines up with the telemetry
row that flips `agent_phase` to `development` at the same bean_temp_c, tick 739 in
Roast 1, tick 948 in Roast 2).

**Run IDs**
- Roast 1 (baseline, #405 OFF): `bf85c77a5436406285571a75df017512`
- Roast 2 (#405 ON): `a4299aea124b43d289bd425d4dc850c4`
- `be9f2036967b4ccda9e17ebb7cf4a434`: faulted, emergency-stopped in `preheating` — excluded, not relevant beyond that.

Both target roasts are `outcome=completed`, same bean profile (Guatemala El
Durazno White Honey, 250 g in), same ambient window (~31.5 °C / ~38.6% / ~1007.6 hPa).

---

## 1. Roast 2 divergence evidence (the smoking gun)

Config confirmed via `roast_runs.config_json`:
- Roast 1: `post_first_crack_control.enabled: false` (pure advisor).
- Roast 2: `post_first_crack_control.enabled: true`, `target_ror_c_per_min: 8.0`,
  `heat_ceiling_percent: 100`, `heat_floor_percent: 25`, `fan_percent: 40` (fixed),
  `kp=3.0`, `ki=0.1`, `ror_deadband=1.0`, `control_interval_seconds: 5.0`.
  Also `target_development_percent` raised 13.0 → 16.0 for this run.

The cleanest source turned out to be `roast_events(kind='command_executed')`,
which carries `{fan_percent, heat_percent}` directly with a `source` tag
(`controller` = the deterministic PID loop; `advisor` = advisor-issued
commands/drop). This obviated cross-referencing `command_log` (which, oddly,
only logged the final `stop_cooling` for both roasts — the per-tick `set_heat`/
`set_targets` writes are NOT mirrored into `command_log`, only into
`roast_events`/`safety_evaluations`; worth a follow-up ticket, not fixed here).

**Full command stream, FC (19:37:26.62) → drop (19:38:49.96):**

| UTC | source | heat % | fan % |
|---|---|---|---|
| 19:37:26.623 | controller (PID) | **72** | 40 |
| 19:37:29.798 | controller (PID) | 71 | 40 |
| 19:37:35.799 | controller (PID) | 73 | 40 |
| 19:37:42.048 | controller (PID) | 74 | 40 |
| 19:37:49.944 | controller (PID) | 76 | 40 |
| 19:37:52.802 | controller (PID) | 77 | 40 |
| 19:37:58.135 | controller (PID) | 79 | 40 |
| 19:38:05.908 | controller (PID) | 80 | 40 |
| 19:38:09.799 | controller (PID) | 83 | 40 |
| 19:38:16.530 | controller (PID) | 85 | 40 |
| 19:38:24.444 | controller (PID) | 87 | 40 |
| 19:38:32.782 | controller (PID) | 89 | 40 |
| 19:38:42.795 | controller (PID) | **91 (max)** | 40 |
| 19:38:49.961 | advisor | `drop_beans` | — |

Interleaved, on the same ticks, the advisor kept advising through
`advisor_decisions.decision_json` (safety-evaluated, `safety_evaluation_id`-linked,
verdict always `allow`, but its heat/fan numbers were **never actuated** — the
deterministic loop owned the write path once `enabled: true`):

| UTC | advisor target heat/fan | rationale gist |
|---|---|---|
| 19:37:26.622 | 0 / 60 | "development at 4.27%, well below 16% target ... drop heat to 0%" |
| 19:37:34.267 | 50 / 45 | (a one-off higher figure, still far below actuated 71-73) |
| 19:37:41.980 | 0 / 60 | |
| 19:37:49.942 | 0 / 60 | |
| 19:37:58.028 | 0 / 60 | |
| 19:38:05.906 | 0 / 60 | |
| 19:38:16.461 | 0 / 60 | |
| 19:38:24.441 | 0 / 70 | |
| 19:38:32.710 | 0 / 70 | |
| 19:38:41.332 | 0 / 80 | "heat already at minimum ... raise fan to 80%" |
| 19:38:48.718 | 0 / 80, **should_drop=true** | DTR 16.11%, bean 194°C — drop fires here |

**First divergence tick:** the very first post-FC PID command, 19:37:26.623 UTC
(same instant as the FC transition), already actuates **heat=72%** while the
advisor (a few ms earlier/same tick) recommended **heat=0%**. So there is no
gradual drift — the two control signals are opposed from tick zero of the
`development` phase.

**Max actuated heat:** **91%** at 19:38:42.795 UTC (tick 1027, bean 193°C, ~7s
before the drop fired). This is inside the configured `heat_ceiling_percent: 100`,
so it never hit a hard bound — it was still climbing when the advisor's `should_drop`
finally landed.

**Safety verdicts on the loop's own commands:** every `all_clear` evaluation on the
climbing heat commands (72→91%) came back **ALLOW** — `"command within bounds and
rate limit"`. There is **no non-ALLOW verdict anywhere in Roast 2** (checked all
4 rules: `all_clear`, `command_phase_validity`, `drop_eligibility`,
`event_source_validity` — all ALLOW). The runaway-heat behavior is not a safety-policy
failure; it's a **control-law design gap** — the fixed 8 °C/min RoR-band target,
chasing a RoR that was already ~6-7°C/min and gently rising, has nothing telling it
to prefer *trimming down* over *ramping up* as bean temp approaches the ceiling. It
is fully legal by policy and still produces the wrong shape.

---

## 2. Roast 1 reference taper (the design input)

FC event: 19:03:44.717 (bean 185°C). First post-FC telemetry tick: 19:03:46.146.
Drop (`command_executed` drop_beans): 19:04:44.544 (bean 189°C).

**Full FC→drop trajectory** (advisor-driven, `post_first_crack_control.enabled=false`):

| UTC | bean °C | RoR °C/min | heat % | fan % | dev % |
|---|---|---|---|---|---|
| 19:03:46.146 | 185 | 6.102 | 0 | 50 | 5.24 |
| 19:03:59.461 | 185 | 6.101 | 0 | 60 | 7.28 |
| 19:04:07.283 | 187 | 6.000 | 0 | 70 | 8.44 |
| 19:04:15.140 | 188 | 6.060 | 0 | 80 | 9.57 |
| 19:04:22.253 | 188 | 5.000 | 0 | 90 | 10.57 |
| 19:04:35.812 | 189 | 6.087 | 0 | 100 | 12.42 |
| 19:04:44.545 | 189 | 4.068 | 0 | 100 | **13.57 (drop)** |

**Duration FC→drop:** ~58.4 s (739→794 telemetry ticks).
**Bean temp FC→drop:** 185 → 189 °C (+4 °C).
**Heat command:** pinned at **0%** for the entire window — heat was never the lever;
**fan** was the only lever, ramped monotonically 50→60→70→80→90→100% roughly every
~7-14s in step with each advisor tick.
**RoR@FC (first post-FC tick):** 6.10 °C/min.
**RoR@drop:** nominally 4.07 °C/min at the very last tick, but it held essentially flat
around 6.0-6.1 °C/min for the first ~50s of the window and only eased in the final
~9s once fan saturated at 100% — so the "shape" is **flat-then-late-taper**, not a
smooth exponential decay across the whole window. Call it approximately **linear-late**:
RoR is roughly constant until fan hits its ceiling, then eases.

**Drop trigger:** advisor `should_drop=true` fired exactly when DTR reached 13.1%
(target was 13.0%) and bean temp (189°C) was closing on the 195°C ceiling — i.e. the
advisor used the *dev% target*, not a temperature ceiling, as the actual drop gate
here, with the ceiling only as a backstop consideration in its rationale.

### Proposed taper parameters for the #405 rework (D88 input)

- **taper start (RoR at engagement) ≈ 6.1 °C/min** — measured, RoR at the first
  post-FC tick in the clean baseline run. (Roast 2's post-FC RoR was similar,
  ~6.1-7.0 °C/min at FC, so this is a reasonably stable engagement point across
  both runs, not a one-off.)
- **taper end (RoR at a good drop) ≈ 4.0 °C/min** — measured, RoR at the Roast 1
  drop tick. Treat this as a soft floor; the observed shape suggests the true
  "good drop" RoR may sit anywhere in the 4-6 °C/min band depending on how late
  the fan-ceiling saturation kicks in — recommend validating against a 3rd/4th
  clean advisor-driven run before locking this to a single number.
  **Roast 2's target_ror_c_per_min of 8.0 °C/min is itself part of the problem**:
  it is *above* the measured RoR-at-engagement (6.1), so the loop reads "RoR too
  low vs. target" from the first tick and adds heat — exactly backwards from
  what the reference trace wants. Any taper-band control law should target
  *below* the measured engagement RoR, not above it.
- **taper duration ≈ 58 s** — measured, FC→drop window in the clean baseline.
  Roast 2's FC→drop window was 83s at a higher DTR target (16% vs 13%), so
  duration should scale with the configured `target_development_percent`, not
  be a fixed constant; ~58s @ 13% dev and ~83s @ 16% dev are both single data
  points, so a linear duration-vs-target-DTR relationship is **estimated, not
  fitted** from 2 points — treat as a starting default, re-validate once more
  clean runs land.
- **Heat/fan through the reference window:** heat 0% flat, fan ramped 50→100%
  in ~6 discrete +10% steps roughly every advisor tick (~7-14s cadence, tied to
  `advisory_min_interval_seconds`/near-FC interval config, not a fixed clock).
  This is the "fan as sole post-FC brake, heat pinned to floor" pattern the D88
  taper law should reproduce deterministically rather than leaving to advisor
  judgment.

---

## 3. Both-roast summary stats

| | Roast 1 (baseline) | Roast 2 (#405 ON) |
|---|---|---|
| run id | bf85c77a… | a4299aea… |
| started (UTC) | 18:51:27.934 | 19:21:35.690 |
| completed (UTC) | 19:10:11.164 | 19:42:18.817 |
| weight in / out | 250 g / **220 g** | 250 g / **216 g** |
| ambient (T/RH/P) | 31.61 °C / 38.4% / 1007.58 hPa | 31.49 °C / 38.8% / 1007.67 hPa |
| T0 (charge) | 18:53:40.082 (MCP `beans_added`, bean 188°C charge temp, confirmed 18:53:48.081) | (equivalent MCP charge event; agent `t0_detected` 19:27:47.790, bean 164°C detected) |
| agent `t0_detected` | 18:53:50.142, bean_temp_c=159 (post-debounce reading) | 19:27:47.790, bean_temp_c=164 (post-debounce reading) |
| drying_end | 18:59:36.390, bean 150°C | 19:33:06.794, bean 151°C |
| FC (agent-confirmed, source=mcp) | 19:03:44.717, bean **185°C** | 19:37:26.623, bean **186°C** |
| FC (raw MCP detector timestamp, from `raw_state_json.first_crack_at_utc`) | 19:03:23.165 (~21.5s before agent-confirmed FC — consistent with known FC-detector confirmation-window lag) | not independently re-extracted, but same confirmation-window mechanism applies |
| drop | 19:04:44.544, bean **189°C**, DTR **13.57%**, source=**advisor** (`command_executed` `drop_beans`) | 19:38:49.961, bean **194°C**, DTR **16.51%**, source=**advisor** (`command_executed` `drop_beans`) |
| dev time (FC→drop) | ~58.4 s | ~83.3 s |
| DTR (telemetry `development_percent`, authoritative — matches controller's own drop logic) | **13.57%** | **16.51%** |
| operator actions | only `stop_cooling` at end (accepted) | only `stop_cooling` at end (accepted) |
| safety verdicts | all ALLOW (1133 all_clear + a few phase/drop/source checks) | all ALLOW (1269 all_clear + a few phase/drop/source checks) — **no CLAMP/REJECT anywhere in either run** |

**Correction to the working assumption going into this analysis:** Roast 2's drop
was **not an operator hand-drop**. `roast_events` shows `command_executed` with
`source=advisor, command=drop_beans` at 19:38:49.960, immediately preceding the
`phase_changed→cooling` event — the advisor's own `should_drop=true` fired (DTR
16.11% ≥ the raised 16% target) and the controller executed it, same mechanism as
Roast 1. The only genuinely operator-sourced action in either run is the final
`stop_cooling`. So the reported "~14.4% DTR, operator hand-dropped ~193°C" in the
original brief undersells it slightly: the achieved figures were **194°C / 16.51%
DTR**, and the drop was algorithmic (advisor), not manual. The advisor was still
doing its job correctly throughout — recommending heat-down/fan-up right until the
end — it just never got to actuate it.

---

## 4. Roast 2 counterfactual: would the ceiling have been the binding constraint?

Deterministic drop condition being designed: `bean_temp_c >= 195 AND dev% >= 16`.

Walking the telemetry from FC to actual drop and slightly beyond:

| tick | UTC | bean °C | dev % |
|---|---|---|---|
| 1031 (actual drop) | 19:38:49.961 | 194 | **16.51** ← dev% condition already satisfied here |
| 1040 | 19:38:55.792 | **195** | 16.51 ← bean-temp leg of the AND only satisfies now, +5.8s later |
| 1046 | 19:39:01.795 | 196 (= bitter/safety ceiling) | 16.51 |

**Answer: dev% (16%) crossed first, bean temp (195°C) crossed ~5.8s later.** On
this specific run, the deterministic `bean≥195 AND dev≥16%` condition would have
fired at essentially the same moment the advisor's own drop fired (the advisor was
slightly ahead, on the dev% leg alone) — the bean-temp leg was not the constraint
that would have delayed the drop. This does **not** mean the ceiling guard is
unneeded, though: the post-drop telemetry (heat_level_percent still reporting 91%
for several ticks into `cooling` — a stale-readout artifact worth flagging
separately, see below) shows bean temp continuing to **coast up to 206°C** over the
next ~2.5 minutes purely on residual drum heat, with RoR spiking to 11-12°C/min
before the physical cooling caught up. That coast is *after* drop/cooling was
already commanded, so it doesn't bear on the drop-timing counterfactual, but it is
a vivid illustration of exactly the kind of overshoot risk the 196°C
`bitter_ceiling_temp_c` / 198°C `emergency_drop_temp_c` guards exist for, and why a
hard temperature ceiling as an OR-condition (not just AND) alongside the dev%
target is the safer shape for D88: on a run where the loop keeps chasing heat even
harder (e.g. colder ambient, a stickier RoR read), dev% could lag while bean temp
alone crosses 195/196 first — the ceiling must be able to force the drop
independently of the dev% leg.

---

## 5. Surprises

1. **No safety-policy failure at all in Roast 2.** Every single verdict across
   both runs was ALLOW. The runaway climb to 91% heat post-FC was 100%
   policy-legal (within `heat_ceiling_percent: 100`, `min_seconds_between_commands`
   respected, valid phase). This is purely a control-law design gap, not a gap in
   the safety layer — useful for scoping D88 correctly: fix the control law, don't
   look for a missing safety rule.

2. **`command_log` did not capture the per-tick `set_heat`/PID writes for either
   run** — only the final operator `stop_cooling` landed there for both roasts.
   The per-tick commands are fully visible in `roast_events(command_executed)`
   and `safety_evaluations`, so the analysis wasn't blocked, but this looks like
   a `command_log` mirroring gap worth its own small ticket (it undermines
   `command_log` as a complete audit trail for anyone querying it in isolation).

3. **Stale `heat_level_percent` readout after drop.** In Roast 2, telemetry
   continues to report `heat_level_percent=91` for multiple ticks after
   `agent_phase` has already flipped to `cooling` (e.g. tick 1040/1046, ~6-12s
   post-drop) — this is very likely a last-known-value carried in the MCP state
   mirror rather than heat actually staying commanded at 91% during cooling
   (`cooling_on=1` is correctly set at the same ticks). Didn't chase this further
   since it's outside the FC→drop divergence story, but flagging in case it
   confuses a future trace read.

4. **The agent-confirmed FC event lags the MCP's raw internal FC timestamp by
   ~21.5s** in Roast 1 (`raw_state_json.first_crack_at_utc` = 19:03:23.165 vs.
   the emitted `first_crack` roast_event at 19:03:44.717) — consistent with the
   known FC audio-detector confirmation-window behavior (min 5 positive windows
   over a 20s confirmation window per the detector config visible in the same
   blob). Not new, but a concrete number for the record: true detection can be
   pinned earlier than the agent's confirmed FC by roughly the size of one
   confirmation window.

5. **`development_percent` in the top-level `telemetry_snapshots` column does not
   equal a naive `(now - agent-FC-event) / (now - agent-T0-event)` computation**
   — several candidate bases were tried (MCP's raw `beans_added_at_utc`/
   `first_crack_at_utc` vs. the agent's own `t0_detected`/`first_crack` events)
   and none reproduced the stored `development_percent` exactly. The stored
   column is trusted here as authoritative (it's what the controller's own
   `should_drop`/`drop_dev_margin_percent` logic actually consumes, and it
   matches the operator's known-good 13.6% for Roast 1 almost exactly), but the
   precise DTR formula wasn't reverse-engineered — worth reading `controller.py`
   directly if an exact formula is needed rather than inferring it from traces.

6. **Roast 2's achieved DTR (16.51%) landed almost exactly on its raised 16%
   target**, despite the wrong control mechanism (heat climbing instead of
   tapering). The loop's fixed 8°C/min RoR-band target happened to produce a
   RoR trajectory that reached the dev% target at almost the same bean
   temperature the advisor alone would have found — the failure mode here is
   about the *path* (actuated heat directly opposing advisor judgment, and the
   post-drop 206°C coast this analysis surfaced) rather than the final number.
   Don't let the "close-enough" final DTR understate how wrong the path was.
