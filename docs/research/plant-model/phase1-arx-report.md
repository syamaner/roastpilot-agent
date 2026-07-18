# Phase 1 -- Plant-model feasibility study: linear ARX for bean-RoR projection

Offline, deterministic, no network, no paid APIs. Two corpora (same Hottop, same
room) unified to a common 1 Hz charge-referenced schema. Question: does a
low-order **linear ARX** predict bean RoR at control-relevant horizons
(t+20 / t+30 / t+40 s, past the ~25-35 s thermocouple lag) well enough to
justify building a predictive controller?

Ambient/room temperature is **excluded entirely** (Artisan lacks it) -- a later phase.

---

## 1. Probe-calibration alignment (the load-bearing check)

Do Artisan-era BT readings and current-MCP BT readings live on the same scale?
If not, the corpora cannot be pooled. Compared characteristic BT-landmark
distributions between corpora:

| Landmark | Artisan mean ± sd | Store mean ± sd | Offset (store − artisan) |
|---|---|---|---|
| turnaround_bt | 69.4 ± 9.8 (n=47) | 91.3 ± 3.8 (n=14) | +21.9 |
| dry_end_bt | 150.5 ± 5.0 (n=47) | 150.1 ± 0.3 (n=13) | -0.4 |
| fc_bt | 177.6 ± 3.9 (n=47) | 184.2 ± 2.3 (n=12) | +6.6 |
| drop_bt | 196.4 ± 3.3 (n=47) | 190.9 ± 3.6 (n=14) | -5.5 |

**Finding: no clean evidence of a probe-calibration offset that would block
pooling.** The apparent landmark offsets are all explained by
detection-method / policy differences, not a probe-scale shift:

- **FC BT ~+6.6 C (store higher).** Store FC is MCP audio detection, which lags
  the true crack ~12-21 s; BT keeps climbing during that lag, so the flagged BT
  reads higher. Artisan FC is operator-marked at the crack. This is detector lag,
  not calibration.
- **Drop BT ~-5.5 C (store lower).** The agent drops beans ~5 C cooler by policy
  (a deliberately conservative bitter-ceiling drop), not because the probe reads
  low.
- **Turnaround ~+21.9 C.** Confounded by charge conditions (batch mass / charge
  temp differ across the multi-year Artisan set) **and** the store sampling
  caveat below -- store telemetry is sparse (~5-6 s) and phase-gated, so the
  interpolated turnaround minimum is shallow. Not a reliable comparator.
- **Dry-end ~150 C in BOTH (offset ~0 C).** This is the one directly comparable
  region. Store fires `drying_end` at a 150 C threshold (pinned by construction),
  but Artisan operators *independently marked* dry-end at ~150 C on average --
  i.e. the two BT scales agree to within ~0.5 C where we can check them.

**Decision: pooled the two corpora directly, with no offset subtraction.** The
model target is RoR (dBT/dt), which is invariant to a constant BT offset anyway;
BT enters the model only as a coarse regime feature, where a small offset is
harmless. Data volume is not the blocker (see the verdict).

## 2. Corpus statistics

- Artisan roasts used: **47**
- Store roasts used (completed, usable telemetry): **14**
- Pooled roasts: **61**
- Modelled ticks (all features + all-horizon targets present): **36429**
- Pre-FC ticks: 35047 · Post-FC ticks: 6262

RoR derived as a **trailing 30 s linear-fit slope** of BT (deg C/min), causal at
every tick (uses only samples up to and including t). Applied identically to both
corpora for fairness.

> **Store sampling caveat.** Store telemetry is recorded roughly every 5-6 s
> (~100-130 rows/roast), not at 1 Hz. It is linearly interpolated onto the 1 Hz
> grid, so store RoR is smoother than the underlying reality. Artisan logs are
> genuinely ~1 Hz.

## 3. Multi-horizon RMSE -- ARX vs naive baselines (leave-one-roast-out CV)

Never train and test on the same roast. RMSE in deg C/min. The **no-heat ARX**
(all heat/fan columns dropped) is the honest control ablation: if the full model
does not beat it, the ARX is only autoregressive trend-following.

**All ticks (n=36429, charge->drop):**

| Model | t+20 | t+30 | t+40 |
|---|---|---|---|
| **ARX (linear, full)** | 1.51 | 1.61 | 1.63 |
| ARX, no heat/fan (ablation) | 1.56 | 1.70 | 1.75 |
| persistence (RoR[t+h]=RoR[t]) | 8.11 | 10.02 | 11.27 |
| linear RoR extrapolation | 8.57 | 14.32 | 20.79 |
| **ARX gain vs best naive** | +6.60 (+81%) | +8.40 (+84%) | +9.64 (+86%) |

(Positive gain = ARX beats the best naive baseline by that many deg C/min.)

**Mid/late roast only (BT >= 150 C -- where drop/RoR control actually happens,
n=14542):**

| Model | t+20 | t+30 | t+40 |
|---|---|---|---|
| **ARX (linear, full)** | 1.51 | 1.65 | 1.69 |
| ARX, no heat/fan (ablation) | 1.64 | 1.85 | 1.98 |
| persistence | 1.58 | 1.69 | 1.78 |
| linear RoR extrapolation | 2.60 | 3.32 | 4.03 |

## 4. Heat-step counterfactual (the control-relevant test)

Can the model predict the RoR **response to a heat change**? Isolated ticks
within 5-40 s **after** a heat setpoint step of
>= 10 % and scored there specifically. The decisive contrast is
**full ARX vs no-heat ARX**: only the heat columns can explain the RoR bend a
step induces.

- Heat steps found -- Artisan: **374**, Store: **40**
- Step-response ticks scored: **2875**

| Model | t+20 | t+30 | t+40 |
|---|---|---|---|
| **ARX full (on step-response)** | 1.80 | 1.91 | 1.93 |
| ARX no-heat (on step-response) | 1.90 | 2.11 | 2.25 |
| persistence (on step-response) | 15.18 | 18.54 | 20.90 |

RMSE in deg C/min. If full ARX does not beat the no-heat ablation **here**, it
has not learned the heat->RoR dynamics -- only smooth coasting, useless for
control.

## 5. Verdict -- NEEDS MORE DATA (conditional no-go on building the controller yet)

A low-order linear ARX is the **right model class** and is **numerically
accurate** at t+20-40 (overall RMSE ~1.5-1.7 C/min, crushing the naive
baselines). But Phase 1 does **not** yet justify building a predictive
controller, for two reasons that the headline table hides:

**(a) The overall ARX-vs-naive win is inflated by the drying phase, not the
control regime.** Persistence looks terrible overall (~8-11 C/min) only because
early-roast RoR falls steeply from turnaround through dry-end -- an easy,
monotonic trend any autoregressive model nails. Restrict to the regime where
drop and RoR decisions actually live (**BT >= 150 C**, section 3, second table)
and persistence collapses to near-parity with the full ARX (the gap is on the
order of ~0.05-0.15 C/min at t+20-40). In the control-relevant window RoR is
slowly varying, so "RoR in 30 s ~= RoR now" is already a strong controller-grade
predictor. The ARX barely improves on it there.

**(b) The heat->RoR signal -- the entire reason to prefer a *predictive*
controller over a reactive one -- is real but small, and the current operating
regime barely excites it.** The no-heat ablation (drop all heat/fan columns) is
almost as good as the full model overall, and on the isolated heat-step-response
ticks the full model beats the no-heat model by only ~0.1-0.3 C/min (the edge
grows with horizon, as expected for a dead-time system). Worse, the **store
corpus (current MCP regime) supplies almost no excitation**: heat is pinned near
65 % through development (the advisor moves fan, not heat), giving only a few
dozen heat steps, mostly clustered at charge. Nearly all identifiable heat
dynamics come from the operator-driven Artisan logs. You cannot robustly
identify a plant gain + dead-time from data that never moves the input.

**What this means:**

- The corpora ARE poolable (section 1): the apparent landmark offsets are
  explained by detection-method and policy differences, not a probe-scale shift,
  and RoR is invariant to a constant BT offset regardless. Data volume is not the
  blocker.
- More *passive* roasts will not fix this -- they add more coasting, not more
  heat-response information. The blocker is **excitation**, not sample count.

**Recommended before any GO:**

1. **Designed excitation.** Run a handful of roasts with deliberate heat steps
   (a staircase or PRBS on the burner, within safe bounds) so the heat->RoR gain
   and dead-time are actually identifiable. This is the single highest-value next
   step.
2. **Prefer a grey-box FOPDT** (first-order-plus-dead-time) fit to those step
   responses over the pooled black-box ARX. It has 3 physically meaningful
   parameters (gain, time-constant, dead-time), extrapolates to unseen inputs far
   better than a regression that never saw input variation, and drops straight
   into a Smith-predictor / IMC controller.
3. **Re-evaluate the ARX against persistence *in the BT >= 150 regime only*** as
   the acceptance gate -- overall RMSE is the wrong yardstick here.

**Bottom line:** promising model class, adequate numerics, but the control-
relevant marginal value over trivial persistence is currently within noise and
the plant's heat channel is under-excited. **NEEDS MORE DATA -- specifically
designed heat-step excitation -- before committing to a predictive controller.**

---

*Artifacts alongside this report:* `model_summary.json` (all numbers),
`landmarks.csv` (per-roast landmarks), `loro_rmse.csv` (the RMSE table).
`step_response_traces.csv` (raw per-tick predicted-vs-actual) is regenerable but
not committed (raw roast data, per `AGENTS.md`).
