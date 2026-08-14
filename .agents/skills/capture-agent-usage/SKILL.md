---
name: capture-agent-usage
description: Opt-in, parent-only local capture of metadata-only Codex and Claude task usage.
---

Use only from the top-level Codex parent; leaves must not invoke it or cross the
Codex/Claude family boundary. The skill records local metadata only, never prompts,
responses, tool output, environment values, or CLI raw output.
Never run it during an active roast or on a host currently controlling roaster hardware.

For a failed harness with recognizable usage, capture exits 0 after appending the record
while the record itself has `success=false`; this indicates capture succeeded, not the task.

Use `scripts/capture_usage_cli.py snapshot-capacity` only for a qualitative capacity
observation joined to its required task and slice identifiers. Use `parse-codex` or `parse-claude` with sanitized streams to verify the
frozen parser grammar; `parse-claude` validates Claude init grammar but makes no
launch-authority claim. The live `run` path additionally attests empty tools,
empty MCP servers, and plan permission before it records usage. Use `annotate-outcome` after final gates to record closed finding
counts and rework metadata joined to the required task and slice identifiers. The default `.agent-usage/usage.jsonl` is local-only and
gitignored.

`run` is measurement and validation only: never use it for implementation or repair.
Its maintainer-selected `--prompt-file` is sent only to the selected provider on stdin,
while normalized metadata and usage totals are recorded. The `role` field is
caller-supplied attribution metadata, never agent selection; native named-agent dispatch
remains required for implementation slices.
