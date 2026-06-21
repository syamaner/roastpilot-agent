# Advisor bake-off — repeatability runbook

How to re-run the post-FC control-advisor bake-off (#277 / D20) end to end, and
how to extend it. The harness is `scripts/advisor_bakeoff.py`; the most recent
results are `docs/advisor/bakeoff-results-2026-06-21.md`.

> **This spends real OpenRouter credits.** Only the real-candidate run needs a
> key; the replay / metrics / report / checkpoint / cost-guard machinery is
> testable without one via a canned recommender (see `tests/`).

## The API key

The harness reads **`OPENROUTER_API_KEY`** from the environment at run time. The
key never enters config or the repo. Supply it either inline on the command, or
from a gitignored `.env` (the repo's `.gitignore` excludes `.env` and `.env.*`):

```bash
# option A — inline (one-off):
OPENROUTER_API_KEY=sk-or-... python scripts/advisor_bakeoff.py ...

# option B — from a gitignored .env you source yourself:
set -a; source .env; set +a
python scripts/advisor_bakeoff.py ...
```

## The three passes

Run from the repo root, in order. Each pass runs the availability sweep first
(dropping any slug that does not resolve on OpenRouter) and resumes from its
checkpoint sidecar on re-run, so a kill / cap / crash loses at most the in-flight
cell — just re-run the same command to continue.

```bash
# 1) AVAILABILITY SWEEP — implicit: every replay run probes the roster first and
#    prints the availability table (reachable / 404 / timed-out, with attempts).
#    To inspect availability alone, run the screen (below); the sweep is its
#    first stage and the JSON's `availability` block records the verdicts.

# 2) SCREEN — all roster models, single seed, the ~6 representative known-good
#    mediums, the as-built c1 control prompt (the default):
OPENROUTER_API_KEY=sk-or-... \
python scripts/advisor_bakeoff.py --roster screen --test-set screen --seeds 1 \
    --trajectory --max-spend 25 \
    --out /tmp/bakeoff-screen.json --report-md /tmp/bakeoff-screen.md

# 3) FINALISTS — the carried models, 2 seeds, the FULL 17 known-good mediums:
OPENROUTER_API_KEY=sk-or-... \
python scripts/advisor_bakeoff.py --roster finalists --test-set full --seeds 2 \
    --trajectory --max-spend 25 \
    --out /tmp/bakeoff-finalists.json --report-md /tmp/bakeoff-finalists.md
```

A **recovery pass** is just a re-run after editing the roster (e.g. swapping a
deprecated slug for its successor): the resume logic skips cells already on disk
and runs only the new candidate.

## The flags that matter

| flag | what it does |
|------|--------------|
| `--roster {screen,finalists}` | which #277 roster to run. `screen` = all roster models; `finalists` = only those flagged `finalist=True` in `ROSTER`. |
| `--test-set {screen,full}` | the known-good-medium fixture set. `screen` = ~6 representative mediums; `full` = all 17. (Unset = the two legacy 7-Jun roasts.) |
| `--seeds N` | repeat passes per cell. Finalists use 2 (variance), the screen uses 1. Each seed writes a seed-suffixed checkpoint so seeds stay independently resumable. |
| `--prompt-version V [V ...]` | prompt(s) to compare. Default `c1` (the as-built #274 control teaching system prompt). Pass `c1 v4` for a c1-vs-v4 (drop-lens) A/B. |
| `--trajectory` | append the command-signal coherence section (change / reversal counts, momentum cuts) over development. Agreement-free; the JSON always carries it. |
| `--max-spend USD` | optional budget; the run stops GRACEFULLY before a cell would breach it, flushes partials, renders the partial scorecard. Suggested ~$25 covers the screen + finalist passes with headroom. |
| `--out PATH` | the JSON results file. |
| `--report-md PATH` | also write the markdown scorecard here. |
| `--include-pre-fc` | OPT-OUT of dev-only scope (see below). |
| `--no-resume` | ignore + truncate the checkpoint and run every cell from scratch. |
| `--concurrency N` | cells in flight (capped). Run the latency-gate pass at `--concurrency 1` (the default) so the gate numbers are authoritative; a higher value only for the scoring pass. |

## Dev-only scope + the opt-out

Under D35 the controller drives the pre-first-crack roast deterministically
(heat 100 / fan 30 to first crack), so the live advisor is consulted **only in
the post-FC `DEVELOPMENT` phase**. The eval matches that: by default it consults
and scores **post-FC ticks only**. This is the as-built scope, and it is what the
21 Jun results reflect.

`--include-pre-fc` is the opt-out: it ALSO consults + scores the gated-out pre-FC
ticks (preheat + drying / Maillard). It costs ~4× and scores a path that never
runs in production, so use it only for a one-off inspection — never for the
decision run.

## Adding a roast to the eval set

The test set is the operator's annotated known-good Artisan roasts, replayed
tick-by-tick. The fixtures are **operator-personal roast data and are local-only
(gitignored under `.artisan-fixtures/`)** — never committed. What IS committed is
the fixture *names* (the load-bearing, reproducible artifact) and the scorecards.

Each fixture is a directory `.artisan-fixtures/artisan-NN/` containing:

- `roast.jsonl` — `telemetry` rows plus three `event` rows (`beans_added` /
  `first_crack_detected` / `beans_dropped`). Temperatures are Celsius.
- `summary.json` — sibling summary for parity with the live-roast fixtures.

To add a roast:

1. **Generate the fixture** from an Artisan `.alog` with the existing converter
   `scripts/alog_to_fixture.py` — it reads the `.alog` (`timex` / `temp1` ET /
   `temp2` BT / `timeindex` markers / `specialevents` heat+fan track) and emits
   the `roast.jsonl` + `summary.json` pair, all in Celsius, into a gitignored
   `--out-dir`. It can exclude over-dark roasts above a drop-temp ceiling
   (`--max-drop-c`, the operator's bitter ceiling ≈196 °C):

   ```bash
   python scripts/alog_to_fixture.py /path/to/alogs \
       --out-dir .artisan-fixtures --max-drop-c 196 --manifest docs/advisor/artisan-testset-manifest.json
   ```

   (There is **no** committed converter for a *store export* → fixture yet; the
   store-export → `roast.jsonl`+`summary.json` data-pipeline converter is the
   open data-pipeline requirement the PM is filing. For now, fixtures come from
   Artisan `.alog` via `alog_to_fixture.py`.)

2. **Register the name** in `scripts/advisor_bakeoff.py`:
   - add `artisan-NN` (with its drop °C / DTR comment, in drop-temperature
     order) to `FULL_MEDIUM_FIXTURE_NAMES`;
   - if it should also be in the cheap screen, add it to
     `SCREEN_MEDIUM_FIXTURE_NAMES` (keep the screen a representative spread
     across the drop-temp / DTR range, not the whole set).

   `resolve_test_set` fails loudly at run time if a named fixture's `roast.jsonl`
   is absent, so a checkout without the local-only data errors clearly rather
   than scoring a partial set.

3. **Verify nothing private is staged.** Before committing, `git status` must
   show no `.artisan-fixtures/` data and no `*.capture.jsonl`. Only the names,
   the manifest, the converter, and the scorecards are committed.

## Adding / swapping a candidate model

Edit the `ROSTER` tuple in `scripts/advisor_bakeoff.py` (it is data): add a
`Candidate(slug, Tier.…, (RoastPhase.DEVELOPMENT,), …)`. Set `finalist=True` to
carry it to the full set; pin `reasoning="minimal"|"low"` for a reasoning model
so it stays inside the live-latency band (never `high`). If a slug 404s as
deprecated, swap in its successor slug and re-run — the resume logic runs only
the new cell. The roster-shape tests in `tests/test_advisor_bakeoff.py` assert
the count, the baseline / prior-winner tiers, and the finalist set, so update
them in the same change.
