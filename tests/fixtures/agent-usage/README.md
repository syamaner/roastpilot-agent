# Agent-usage parser fixtures

These fixtures reproduce the event grammar observed from the installed CLIs on
13 August 2026 while replacing identifiers, messages, model names, timings,
costs, and token counts with synthetic values.

- `codex-0.147.0.jsonl` is derived from a parent-run `codex exec --json` capture,
  including opaque tool-lifecycle events.
- `claude-2.1.228.jsonl` is derived from a parent-run
  `claude -p --output-format stream-json` capture, including opaque `user`
  events and the exact installed terminal usage schema. Its duplicate assistant
  ID and second model are synthetic adversarial cases for whole-tree accounting.

No prompt, response, credential, environment value, repository path, or live
session identifier is retained.
