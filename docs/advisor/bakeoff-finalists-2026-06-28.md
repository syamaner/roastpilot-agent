# Post-FC Bakeoff Report (#277) — 28 Jun 2026

**Scope:** Post-FC development phase only (D35: pre-FC is deterministic).
**Corpus:** 17 known-good medium roasts (Artisan `.alog` exports, drop ≤ 196°C,
operator validated). See `bakeoff-screen-2026-06-28.json` (screen pass) and
`bakeoff-finalists-2026-06-28.json` (finalists, 17 roasts × 1–2 seeds; see §1) for raw data.
**Prompt version:** c3 throughout (live control prompt as of this run date).
**c1 comparison:** targeted 6-roast screen re-run on gpt-4o only; see §4.

---

## 1. Roster and method

### Screen pass (all candidates, 6 roasts, 1 seed)

All 7 candidates in `ROSTER` were run over the 6-roast screen subset.
Latency disqualification gate: median FC response ≤ 2.5 s.

| Model                        | Tier               | Drop F1 | Heat MAE | Heat DA | Fan MAE | FC Lat (med) |
|------------------------------|--------------------|---------|----------|---------|---------|--------------|
| google/gemini-3.1-flash-lite | prior-winner       | **1.000** | 61.7 pp | 0.45 | 12.0 pp | 1.15 s |
| google/gemini-3-flash-preview | control-candidate | 0.945   | 49.8 pp  | 0.45    | 15.5 pp | 2.38 s |
| openai/gpt-4o                | baseline-n8n       | 0.611   | 52.8 pp  | 0.45    | 12.6 pp | 1.80 s |
| x-ai/grok-4.3                | control-candidate  | 0.889   | 36.4 pp  | 0.55    | 8.3 pp  | 6.12 s ⚠ |
| deepseek/deepseek-v4-flash   | control-candidate  | 0.889   | 74.2 pp  | 0.45    | 13.4 pp | 6.57 s ⚠ |
| google/gemini-3.5-flash      | frontier-ceiling   | 0.500   | 47.3 pp  | 0.48    | 13.6 pp | 3.72 s ⚠ |
| anthropic/claude-haiku-4.5   | control-candidate  | 0.278   | 29.0 pp  | 0.45    | 9.3 pp  | 4.83 s ⚠ |

⚠ = latency-disqualified (median > 2.5 s FC gate).

Availability: gpt-5-nano and gpt-5-mini timed out on reachability probe (both seeds).
grok-4.3 also produced `confidence > 1.0` on 3 ticks (AdvisorUnsafeOutputError),
making it structurally unreliable independent of latency.

### Finalists (3 models, 17 roasts, 2 seeds)

After the screen, the 3 latency-passing models with highest drop F1 were carried
to the full 17-roast medium set with 2 independent seeds:

| Model                        | Drop F1 (n pairs)     | Heat MAE | Heat DA | Fan MAE | FC Lat |
|------------------------------|----------------------|----------|---------|---------|--------|
| google/gemini-3.1-flash-lite | **0.931** (n=34)     | 59.8 pp  | 0.44    | 11.1 pp | 1.21 s |
| google/gemini-3-flash-preview | 0.902 (n=17) †      | 48.5 pp  | 0.47    | 15.3 pp | 2.47 s |
| openai/gpt-4o                | 0.765 (n=34)         | 46.9 pp  | **0.48**| 13.3 pp | 1.70 s |

† gemini-3-flash-preview completed seed 1 only (17 pairs): unavailable on the
final availability sweep but its 17 checkpoint cells are valid. Score is from a
single seed, not a 2-seed average; use with that caveat for re-pin decisions.

Per-seed breakdown for gpt-4o (seed 1 = 0.804, seed 2 = 0.726) and
gemini-3.1-flash-lite (seed 1 = 0.941, seed 2 = 0.922) show stable ranking.

Note on Heat MAE: absolute lever-position deviation in percentage points.
High values reflect a known c3-prompt confound (see §4 below) — models reason
correctly about direction but calibrate to different lever magnitudes than
the Artisan operator traces. Heat directional agreement (Heat DA) is the more
reliable direction metric. Fan MAE is not confounded in the same way.

---

## 2. The gpt-4o drop-F1 gap — investigation

### 2a. The 2 never-drop roasts (artisan-01, artisan-12 on c3)

On c3, gpt-4o produced `should_drop=False` on every tick for artisan-01 and
artisan-12 (fn=1 each), giving F1=0 on those roasts.

**artisan-01** (drop_temp=189°C, DTR=20.5%):
- Final tick (bean=189°C, dev_elapsed=127s): gpt-4o output `should_drop=False`,
  heat=0, fan=70. The model had already cut heat to 0 but refused to call the
  explicit drop at 189°C. The target was a light roast at the low end of the
  medium window.

**artisan-12** (drop_temp=194°C, DTR=14.4%):
- Final tick (bean=194°C, dev_elapsed=80s): gpt-4o output `should_drop=False`,
  heat=0, fan=70. At 80s into development (14.4% DTR already met), this is a
  genuine miss — the model held when it should have dropped.

### 2b. Reconciliation with roast 6 (hardware, 27 Jun)

Roast 6 (Colombia Washed, MCP auto-FC, gpt-4o live on c3 control prompt) produced
a clean drop at ~190°C. This is not contradicted by the bakeoff: the live run
operates within the full controller loop including the dev-guard
(`drop_dev_margin_percent=3%`), operator confirmation path, and real heat/fan
feedback. The eval simulates the advisor in isolation against Artisan lever traces.
A "never-drop" in the eval does NOT mean gpt-4o would never drop live — it means
the model did not emit `should_drop=True` within the scored development window on
those 2 specific fixtures.

### 2c. The c1 vs c3 prompt confound

The 21 Jun pin was established using gpt-4o + c1 prompt. This run uses c3.
To isolate the prompt change, gpt-4o was re-run on the same 6-roast screen set
using c1:

| Roast        | gpt-4o c3 F1 | gpt-4o c1 F1 | delta |
|--------------|-------------|-------------|-------|
| artisan-01   | 0.00 (never-drop) | **1.00** | +1.00 |
| artisan-06   | 0.67 (early) | 0.00 (late fp) | -0.67 |
| artisan-09   | 1.00         | 1.00         | 0.00 |
| artisan-12   | 0.00 (never-drop) | **1.00** | +1.00 |
| artisan-16   | 1.00         | 1.00         | 0.00 |
| artisan-22   | 1.00         | 1.00         | 0.00 |
| **Average**  | **0.611**   | **0.833**   | **+0.222** |

Both previously failing roasts (artisan-01, artisan-12) recovered to F1=1.0 on
c1. The average drop-F1 improved from 0.611 (c3) to 0.833 (c1).

**Conclusion on drop-F1 gap:** The c1→c3 prompt change is a likely contributor to
gpt-4o's lower drop F1 on the c3 screen. The 2 never-drop roasts are a c3 prompt
artifact, not a fundamental model failure. The finding cannot be attributed to the
model without first isolating the prompt.

---

## 3. Scorecard on the deciding axis

The 21 Jun pin was decided primarily on heat-magnitude MAE and heat-direction
agreement, not drop F1 (gpt-4o's heat MAE ~7.5 pp vs gemini ~22 pp on c1).

| Model                        | Drop F1 | Heat MAE | Heat DA |
|------------------------------|---------|----------|---------|
| gemini-3.1-flash-lite (c3)   | 0.931   | 59.8 pp  | 0.44    |
| gemini-3-flash-preview (c3)  | 0.902   | 48.5 pp  | 0.47    |
| gpt-4o (c3)                  | 0.765   | 46.9 pp  | 0.48    |
| gpt-4o (c1, screen only)     | 0.833   | (not scored in this run) | — |

Heat DA for all finalists is tightly clustered (0.44–0.48). Heat MAE is high
for all models on c3 — this is believed to be a prompt confound (models reason
about direction correctly but calibrate lever percentages to the c3 teaching, not
the Artisan absolute positions). The c1 vs c3 heat MAE comparison requires a
dedicated run with full heat scoring on both prompts; not done in this session.

---

## 4. Key findings and operator decisions surfaced

1. **Drop-F1 gap for gpt-4o under c3 is a prompt confound, not a model verdict.**
   The c1→c3 prompt change raises the 2 artisan-01/artisan-12 never-drops; the
   same roasts both dropped correctly on c1. Before any re-pin decision, a
   comparative run of all finalists on both c1 AND c3 is needed.

2. **grok-4.3 removed from finalists** (28 Jun, this session). Reason: 6.12 s
   median FC latency (gate = 2.5 s) AND confidence > 1.0 on 3 ticks
   (AdvisorUnsafeOutputError, structurally unreliable). Set `finalist=False` in
   ROSTER; kept for screen coverage only. Tests updated accordingly.

3. **gemini-3.1-flash-lite continues to lead on drop accuracy and latency.**
   Drop F1=0.931, FC lat=1.21 s across the full 17-roast, 2-seed run. This
   is consistent with the 21 Jun screen result (F1=1.0 on 6 roasts, 1 seed).

4. **Dev-guard (#313/#325) blocks correct drops live.** The store corpus traces
   (roast 3, Ethiopia Natural) show gpt-4o correctly calling `should_drop=True`
   at 195°C on ticks 4-9, but the controller's `drop_dev_margin_percent=3%`
   guard blocked all 5 calls while bean temp climbed to 203°C. This is a
   controller issue, not a model issue, and is tracked in #313/#325.

5. **No re-pin recommended on this run.** The c3 drop-F1 gap for gpt-4o is
   confounded. The pin decision belongs to the operator after a dedicated
   c1-vs-c3 head-to-head on the full 17-roast set (both prompts, same session,
   same key). Refs #277.

---

## 5. Artefacts

| File | Description |
|------|-------------|
| `docs/advisor/bakeoff-screen-2026-06-28.json` | Screen pass raw data (7 models, 6 roasts, 1 seed, c3) |
| `docs/advisor/bakeoff-finalists-2026-06-28.md` | This report |
| `docs/advisor/store-roast-corpus-manifest.json` | Labelled store-corpus manifest (#224) |
| `/tmp/bakeoff-finalists-2026-06-28.json` | Finalists raw data (3 models, 17 roasts, 2 seeds, c3) — gitignored |
| `/tmp/bakeoff-gpt4o-c1-screen-2026-06-28.json.capture.jsonl` | c1 comparison capture — gitignored |

The finalists JSON and c1 capture are in `/tmp` (not committed — too large for the
repo and partially gitignored per AGENTS.md). The screen JSON scorecard
(`bakeoff-screen-2026-06-28.json`) IS committed as a small analysis artefact.

---

*Generated: 28 Jun 2026. Refs #277.*
