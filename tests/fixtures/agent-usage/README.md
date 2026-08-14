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
- `claude-2.1.231.jsonl` is derived from a parent-run capture with the same fixed
  no-tools, no-MCP, plan-permission boundary. Its sanitized full init inventory
  attests empty `tools`, empty `mcp_servers`, and `permissionMode: plan`; it
  proves the closed event and terminal usage grammars remain unchanged while
  using wholly synthetic content.
- `claude-2.1.231-native.jsonl` is a synthetic-conforming native-profile fixture
  with non-empty tools, `permissionMode: auto`, and a model field. It validates
  parser and launch-boundary logic only; it is not observed provenance and proves
  neither native role resolution nor whole-tree coverage.

No prompt, response, credential, environment value, repository path, or live
session identifier is retained.
