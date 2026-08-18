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

Under D163, `run-native-claude` is the separate parent-only instrumentation path for
the committed roles `engineer-be`, `engineer-fe`, `mcp-contract-checker`,
`planning-architect`, `pr-triage`, `product-auditor`, `qa`, `safety-reviewer`,
`security-reviewer`, `sim-roast-runner`, and `story-planner`. It derives READ_ONLY or
WRITE capability from the committed tools, launches with the committed effort, and
accepts only Claude 2.1.233 metadata. It reads only the generated parent transcript,
deduplicates numeric usage, rejects any subagent tree, and writes no transcript content
or host path to the sink. `ui-reviewer` remains excluded because its Playwright MCP
conflicts with empty-MCP capture; `repair` is excluded. The generic `run` path remains
measurement/validation-only, has no routing authority, and retains its
no-session-persistence boundary.

Under D166, every READ_ONLY role returns one bounded, transcript-validated final
assistant response on stdout to the launching parent only — never to the sink, git,
GitHub, fixtures, or any other durable file. `qa`, `mcp-contract-checker`, and
`sim-roast-runner` additionally require a parent-provisioned external
`--validation-root` for their Python/pytest gates, while the attested worktree must
stay byte-clean throughout.
