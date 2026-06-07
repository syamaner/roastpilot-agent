# RoastPilot Agent

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
  `ALLOW / CLAMP / REJECT / FAULT / EMERGENCY_STOP`.
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

## Related repositories

- [`coffee-roaster-mcp`](https://github.com/syamaner/coffee-roaster-mcp) —
  the single MCP server owning the machine/session boundary (PyPI + MCP
  Registry)
- [`coffee-first-crack-detection`](https://github.com/syamaner/coffee-first-crack-detection)
  — ML pipeline for the first-crack audio model

## License

MIT (to be confirmed at first release).
