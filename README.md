# RoastPilot Agent

[![codecov](https://codecov.io/gh/syamaner/roastpilot-agent/graph/badge.svg?token=5MW1K2A3RE)](https://codecov.io/gh/syamaner/roastpilot-agent)

Deterministic agent harness for autonomous coffee roasting with a Hottop
KN-8828B-2K+ — **the controller owns the loop, the LLM advises**.

> **Status: in active development.** First milestone (mock-safe vertical
> slice, no hardware required) is in progress; supervised hardware validation
> follows. Not production-ready.

## What this is

A local Python service that drives a roast through
[`coffee-roaster-mcp`](https://github.com/syamaner/coffee-roaster-mcp):

- **Deterministic controller** — typed state machine, 1.0 s control tick
  (set by the K-type thermocouples' response time, not by the LLM), SQLite
  persistence, explicit recovery states. Restart never auto-resumes heat or
  fan control.
- **Hard safety policy** — deterministic code, not prompt text. Every
  command is validated before it reaches the roaster; verdicts are typed:
  `ALLOW / CLAMP / REJECT / RECOVERY / FAULT / EMERGENCY_STOP`.
- **Advisory-only LLM** — returns a typed recommendation
  (heat/fan targets, drop suggestion, rationale). It never calls tools,
  never owns the loop, and its output is validated, clamped, or rejected by
  the safety layer before any hardware write.
- **Operator authority** — manual bean loading, first-crack override, drop,
  and emergency stop always available; a bundled web UI streams live state
  over SSE.

First-crack detection comes from an audio model
([dataset, model, and live demo on Hugging Face](https://huggingface.co/syamaner/coffee-first-crack-detection))
running inside the MCP server on a Raspberry Pi 5.

## How the advisor model is chosen (the eval)

The advisor model and prompt are not picked by vibes — they are chosen by a
**replay bake-off** that scores candidate models against real roasts:

- **Test set:** known-good Hottop roasts replayed tick-by-tick. The current
  set is the operator's annotated Artisan `.alog` history, quality-filtered to
  drop below the operator's bitterness ceiling (< 198 °C) and converted to the
  replay format by [`scripts/alog_to_fixture.py`](scripts/alog_to_fixture.py)
  (it decodes the BT/ET curve, the charge/first-crack/drop marks, and the
  manual heat/fan control track). The roast logs themselves are personal and
  are **not** committed; only the anonymized scorecard is.
- **What's scored** (`scripts/bakeoff_replay.py`): the **drop decision** as a
  binary classification over ticks — precision / recall / **F1** + the
  drop-timing error in seconds and °C; **heat & fan** as MAE + directional
  agreement (did the model move the lever the way the human did, especially the
  anticipatory pre-first-crack cut); and **latency** per phase against the tick
  budget (tightest at first crack).
- **The honest framing — read it before trusting a number.** Ground truth is a
  *known-good* roast, not a provably optimal one. Every metric measures
  **agreement with what the human did**, NOT absolute correctness: F1 = 1.0
  means *matched this roast's drop*, not *correct*. The scores are a
  quantitative **aid** to the operator's judgement (advice samples + the
  latency gate + the controller's own ≤ 196 °C ceiling), never a replacement.

Run it (needs an OpenRouter key; the replay/scoring layer is fully testable
without one via a fake advisor):

```bash
OPENROUTER_API_KEY=sk-or-... python scripts/advisor_bakeoff.py \
  --prompt-version v2 --out /tmp/bakeoff.json --report-md /tmp/bakeoff.md
```

Latest outcome (28-roast Artisan re-run, 14 Jun 2026): the cheap, fast
`google/gemini-3.1-flash-lite` + prompt `v2` was the only model that reliably
makes the drop call; the frontier and slow models over-hold (never drop). See
[`docs/advisor/bakeoff-artisan-summary.md`](docs/advisor/bakeoff-artisan-summary.md).

## Development setup

Requires Python 3.11+.

```bash
git clone https://github.com/syamaner/roastpilot-agent.git
cd roastpilot-agent
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . --group dev
```

Quality gates (all must pass; CI runs the same four):

```bash
python -m ruff check .
python -m ruff format --check .
python -m pyright
python -m pytest
```

All tests run hardware-free — no roaster, microphone, or model download
needed. Project conventions and architecture invariants live in
[AGENTS.md](AGENTS.md); epic specs and current status live under
[docs/epics/](docs/epics/) with the active-epic pointer in
[docs/state/registry.md](docs/state/registry.md).

## Related repositories

- [`coffee-roaster-mcp`](https://github.com/syamaner/coffee-roaster-mcp) —
  the single MCP server owning the machine/session boundary (PyPI + MCP
  Registry)
- [`coffee-first-crack-detection`](https://github.com/syamaner/coffee-first-crack-detection)
  — ML pipeline for the first-crack audio model

## License

MIT (to be confirmed at first release).
