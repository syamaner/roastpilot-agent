# Agent-usage parser fixtures

These fixtures reproduce the event grammar observed from the installed CLIs on
18 August 2026 while replacing identifiers, messages, model names, timings,
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
- `claude-2.1.233.jsonl`, `claude-2.1.233-native.jsonl`, and the
  `claude-2.1.233-transcript/` family are the sole admitted frozen grammar for
  generic and native capture. They are copied unchanged from the lead-supplied
  sanitized fixture root; the 2.1.228 and 2.1.231 files remain rejection evidence.
- `claude-2.1.231-native.jsonl` is a synthetic-conforming native-profile fixture
  with non-empty tools, `permissionMode: auto`, and a model field. It validates
  parser and launch-boundary logic only; it is not observed provenance and proves
  neither native role resolution nor whole-tree coverage.
- `claude-2.1.231-transcript/parent.jsonl` is derived from three bounded persisted
  `--agent engineer-be --session-id` probes: tools-disabled, read-only, and an
  isolated write-capable probe that exercised the `Write` tool. The write probe
  produced the same closed row-type inventory as the first two probes; that
  comparison is parent-owned out-of-band observation, not something the synthetic
  fixture bytes independently prove.
  All content, paths, request/message/session IDs, timestamps, and token counts are
  synthetic. The observed D161 leaf grammar is: the project directory encodes each
  non-alphanumeric path character as `-`; `agent-setting` binds the exact native
  role; allowed row types are `agent-setting`, `ai-title`, `assistant`,
  `attachment`, `last-prompt`, `queue-operation`, and `user`; assistant rows carry
  `sessionId`, version `2.1.231`, model, `effort`, and usage. Usage always includes
  `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, and
  `output_tokens`, with the observed optional keys `output_tokens_details`,
  `server_tool_use`, `service_tier`, `cache_creation`, `inference_geo`,
  `iterations`, and `speed`. Repeated tool-use assistant rows carried the same
  message ID and byte-identical usage, establishing the required deduplication
  case; the synthetic duplicate uses distinct outer transcript UUIDs so a parser
  cannot substitute outer-row deduplication. A second assistant row deliberately
  omits every optional usage key. Native implementation roles are leaves: no probe
  created a subagent directory or exposed an agent ID, so any subagent transcript
  is an authority violation rather than valid descendant consumption.
  `engineer-fe.jsonl` is a synthetic-conforming role-binding variant with the
  observed `auto` permission-mode shape; it ensures both eligible native roles are
  exercised without claiming a separate frontend probe.

No prompt, response, credential, environment value, repository path, or live
session identifier is retained.
