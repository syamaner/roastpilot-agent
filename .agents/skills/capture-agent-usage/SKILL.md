---
name: capture-agent-usage
description: Opt-in, parent-only local capture of metadata-only Codex and Claude task usage.
---

Use only from the top-level Codex parent; leaves must not invoke it or cross the
Codex/Claude family boundary. The skill records local metadata only, never prompts,
responses, tool output, environment values, or CLI raw output.

Use `scripts/capture_usage_cli.py snapshot-capacity` only for a qualitative capacity
observation. Use `parse-codex` or `parse-claude` with sanitized streams to verify the
frozen parser grammar, and `annotate-outcome` after final gates to record closed finding
counts and rework metadata. The default `.agent-usage/usage.jsonl` is local-only and
gitignored.
