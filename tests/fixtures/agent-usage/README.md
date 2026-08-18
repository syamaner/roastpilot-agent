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
- `claude-2.1.233.jsonl` is the admitted frozen generic strict-launch grammar.
  `claude-2.1.233-native.jsonl` is frozen observed native-stream/rejection
  evidence for generic strict authority, and `claude-2.1.233-transcript/` is the
  admitted native-capture grammar. These files are copied unchanged from the
  lead-supplied sanitized fixture root; the 2.1.228 and 2.1.231 files remain
  rejection evidence.
  The observed native stream parses in lax structural mode, but generic strict
  launch authority rejects it because `hook_started`/`hook_response` precede `init`.
  `security-reviewer.jsonl` retains its exact-shape, metadata-only `ai-title` row
  (carried forward unchanged from the prior probe evidence, §D166 note below); it
  retains no provider content or paths.
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
  Its natural distinct-outer-UUID duplicate documents the historical case; the
  admitted 2.1.233 synthetic test independently proves the live dedup invariant.

## D166 round-7 repair: regenerated READ_ONLY `dontAsk` fixtures (18 Aug 2026)

`claude-2.1.233-transcript/story-planner.jsonl`, `safety-reviewer.jsonl`, and
`security-reviewer.jsonl` are regenerated, and `qa.jsonl` is new, to carry the
`dontAsk` permission-mode grammar and a terminal assistant turn with a
`thinking` block followed by a `text` block. The `permissionMode: "dontAsk"`
user-row field, the `promptSource: "sdk"` field, the `{"type":"mode","mode":
"normal"}` row, the terminal `stop_reason: "end_turn"`, and the exact
`thinking`/`text` content-block key sets are lead-supplied, bounded,
parent-owned probe evidence from a Claude Code 2.1.233 Opus/high
`story-planner` run and a Bash-capable Sonnet/high `pr-triage` run on an idle
non-roasting host. Every identifier, timestamp, and token count is synthetic.
**Every `text` block's content is a synthetic sanitised placeholder authored by
the lead — never a real model response** (A4); `qa.jsonl` is test data for the
parser and launcher stub only and does not claim a live `--validation-root`
run (that path is proved by the local mutation/behavioural tests plus a
separate parent-owned post-implementation live proof, never by this fixture).
`security-reviewer.jsonl`'s pre-existing `ai-title` row is carried forward
unchanged from the fixture it replaces (its shape and grammar are outside this
round's changes) with only its `sessionId` binding updated to match the
regenerated session. `parent.jsonl` and `engineer-fe.jsonl` (WRITE roles
already on the frozen `auto` mode) are unchanged by this round.

No prompt, response, credential, environment value, repository path, or live
session identifier is retained.
