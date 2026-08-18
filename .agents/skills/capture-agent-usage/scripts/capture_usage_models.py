"""Closed, metadata-only models for the opt-in agent-usage capture pilot."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from enum import Enum
from typing import Annotated, BinaryIO, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

AGENT_USAGE_SCHEMA_VERSION = 1
"""The append-only on-disk record schema version."""

NATIVE_WORKER_USAGE_SCHEMA_VERSION = 3
"""D161 native-worker record schema, distinct from generic capture records."""

SKILL_VERSION = "0.1.0"
"""The version of the capture skill's normalized record grammar."""

MAX_EVENT_BYTES = 65_536
"""Largest accepted JSONL event, limiting one allocation to 64 KiB."""
MAX_EVENT_COUNT = 10_000
"""Largest accepted event count, far above normal task streams."""
MAX_STREAM_BYTES = 1_048_576
"""Largest accepted total stream size, limiting hostile or endless output."""


class BoundedStreamError(ValueError):
    """Raised when a binary JSONL stream exceeds the closed ingestion grammar."""


def bounded_jsonl_lines(
    stream: BinaryIO,
    *,
    max_event_bytes: int = MAX_EVENT_BYTES,
    max_event_count: int = MAX_EVENT_COUNT,
    max_stream_bytes: int = MAX_STREAM_BYTES,
) -> Iterator[str]:
    """Yield complete UTF-8 JSONL lines under fixed byte and count limits.

    Args:
        stream: A binary file-like stream, including a fixed subprocess stdout pipe.

    Raises:
        BoundedStreamError: If a line, stream, or encoding violates the fixed limits.
    """
    event_count = 0
    total_bytes = 0
    while True:
        raw_line = stream.readline(max_event_bytes + 1)
        if raw_line == b"":
            return
        if len(raw_line) > max_event_bytes:
            raise BoundedStreamError("usage stream event exceeds size limit")
        if not raw_line.endswith(b"\n"):
            raise BoundedStreamError("usage stream contains a partial event")
        event_count += 1
        if event_count > max_event_count:
            raise BoundedStreamError("usage stream exceeds event count limit")
        total_bytes += len(raw_line)
        if total_bytes > max_stream_bytes:
            raise BoundedStreamError("usage stream exceeds total byte limit")
        invalid_utf8 = False
        text = ""
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            invalid_utf8 = True
        if invalid_utf8:
            raise BoundedStreamError("usage stream contains invalid UTF-8") from None
        yield text


class HarnessFamily(Enum):
    """Supported harnesses with fixed executable and argument grammars."""

    CODEX = "CODEX"
    CLAUDE = "CLAUDE"


class NativeClaudeRole(Enum):
    """Registered Claude implementation roles that native capture can attest."""

    ENGINEER_BE = "engineer-be"
    ENGINEER_FE = "engineer-fe"
    MCP_CONTRACT_CHECKER = "mcp-contract-checker"
    PLANNING_ARCHITECT = "planning-architect"
    PR_TRIAGE = "pr-triage"
    PRODUCT_AUDITOR = "product-auditor"
    QA = "qa"
    SAFETY_REVIEWER = "safety-reviewer"
    SECURITY_REVIEWER = "security-reviewer"
    SIM_ROAST_RUNNER = "sim-roast-runner"
    STORY_PLANNER = "story-planner"


NATIVE_ROLE_EXCLUSIONS: dict[str, str] = {
    "ui-reviewer": (
        "its Playwright MCP conflicts with the empty-MCP, empty-tools native"
        " capture launch boundary"
    ),
}
"""Committed `.claude/agents/*.md` roles deliberately excluded from native capture.

Every excluded role is documented with its exact reason rather than silently
absent; :data:`NativeClaudeRole` values union this mapping's keys must equal the
committed agent file stems exactly (tests/test_capture_agent_usage.py).
"""


class RoleCapability(Enum):
    """Closed capability inferred from committed native-role tools."""

    WRITE = "WRITE"
    READ_ONLY = "READ_ONLY"


class EstimateBasis(Enum):
    """The provenance of a normalized estimated USD amount."""

    CLIENT_SIDE_ESTIMATE = "CLIENT_SIDE_ESTIMATE"
    NOT_EXPOSED = "NOT_EXPOSED"


class CapacityStatus(Enum):
    """Qualitative subscription capacity states only."""

    HEALTHY = "HEALTHY"
    CONSTRAINED = "CONSTRAINED"
    RESERVE_ONLY = "RESERVE_ONLY"


class CapacitySource(Enum):
    """Allowed sources for a qualitative capacity judgement."""

    CLI_STATUS = "CLI_STATUS"
    CLI_USAGE = "CLI_USAGE"
    OPERATOR = "OPERATOR"


class FindingSeverity(Enum):
    """Closed severity vocabulary used by pilot outcome metadata."""

    BLOCKER = "BLOCKER"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class FindingLens(Enum):
    """Closed review-lens vocabulary used by pilot outcome metadata."""

    SECURITY = "SECURITY"
    SAFETY = "SAFETY"
    QA = "QA"
    UI = "UI"
    MCP_CONTRACT = "MCP_CONTRACT"
    CODEX = "CODEX"
    CLAUDE = "CLAUDE"
    HUMAN = "HUMAN"


class CaptureModel(BaseModel):
    """Base model that rejects unrecognized persisted fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


SafeIdentifier = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
]
GitReference = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}$"),
]
GitSha = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{7,64}$")]
RepositoryName = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"),
]
TokenCount = Annotated[int, Field(ge=0)]


class ClaudeModelUsage(CaptureModel):
    """Whole-tree terminal usage for one Claude model, never message deltas."""

    model: SafeIdentifier
    input_tokens: TokenCount
    cached_input_tokens: TokenCount
    cache_creation_input_tokens: TokenCount
    output_tokens: TokenCount
    estimated_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ParsedUsage(CaptureModel):
    """Normalized parser result kept in memory until a validated record is made."""

    input_tokens: TokenCount | None = None
    cached_input_tokens: TokenCount | None = None
    cache_creation_input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    reasoning_output_tokens: TokenCount | None = None
    claude_model_usage: tuple[ClaudeModelUsage, ...] | None = None
    claude_terminal_success: bool | None = None
    claude_init_model: SafeIdentifier | None = Field(default=None, exclude=True)
    claude_model_canonical_names: tuple[str, ...] | None = Field(default=None, exclude=True)
    estimated_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    estimate_basis: EstimateBasis = EstimateBasis.NOT_EXPOSED

    @model_validator(mode="after")
    def validate_estimate_basis(self) -> ParsedUsage:
        """Require cost metadata to be explicitly labelled as an estimate."""
        if self.estimated_usd is None and self.estimate_basis is not EstimateBasis.NOT_EXPOSED:
            raise ValueError("an absent estimated_usd requires NOT_EXPOSED")
        if (
            self.estimated_usd is not None
            and self.estimate_basis is not EstimateBasis.CLIENT_SIDE_ESTIMATE
        ):
            raise ValueError("estimated_usd must be labelled CLIENT_SIDE_ESTIMATE")
        return self


class TaskUsageRecord(CaptureModel):
    """A metadata-only per-task harness result appended to the local sink."""

    record_type: Literal["TASK_USAGE"] = "TASK_USAGE"
    schema_version: Literal[1] = AGENT_USAGE_SCHEMA_VERSION
    tool_version: SafeIdentifier = SKILL_VERSION
    captured_at: datetime
    task_id: SafeIdentifier
    slice_id: SafeIdentifier
    harness: HarnessFamily
    role: SafeIdentifier
    model: SafeIdentifier
    effort: SafeIdentifier | None = None
    repository: RepositoryName
    branch: GitReference
    base_sha: GitSha
    head_sha: GitSha
    started_at: datetime
    completed_at: datetime
    elapsed_ms: Annotated[int, Field(ge=0)]
    exit_code: int
    success: bool
    harness_version: SafeIdentifier
    input_tokens: TokenCount | None = None
    cached_input_tokens: TokenCount | None = None
    cache_creation_input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    reasoning_output_tokens: TokenCount | None = None
    claude_model_usage: tuple[ClaudeModelUsage, ...] | None = None
    estimated_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    estimate_basis: EstimateBasis = EstimateBasis.NOT_EXPOSED
    whole_tree_verified: bool = False
    usage_complete: bool
    parent_task_id: SafeIdentifier | None = None

    @model_validator(mode="after")
    def validate_usage(self) -> TaskUsageRecord:
        """Protect normalized usage and client-estimate provenance invariants."""
        if self.success != (self.exit_code == 0):
            raise ValueError("success must match the harness exit code")
        if self.estimated_usd is None and self.estimate_basis is not EstimateBasis.NOT_EXPOSED:
            raise ValueError("an absent estimated_usd requires NOT_EXPOSED")
        if (
            self.estimated_usd is not None
            and self.estimate_basis is not EstimateBasis.CLIENT_SIDE_ESTIMATE
        ):
            raise ValueError("estimated_usd must be labelled CLIENT_SIDE_ESTIMATE")
        if not self.usage_complete and any(
            value is not None
            for value in (
                self.input_tokens,
                self.cached_input_tokens,
                self.cache_creation_input_tokens,
                self.output_tokens,
                self.reasoning_output_tokens,
                self.claude_model_usage,
                self.estimated_usd,
            )
        ):
            raise ValueError("incomplete usage must not invent partial token or cost totals")
        if not self.usage_complete and self.whole_tree_verified:
            raise ValueError("incomplete usage cannot claim whole-tree verification")
        if self.harness is HarnessFamily.CODEX and self.claude_model_usage is not None:
            raise ValueError("Codex records must not contain Claude model usage")
        if self.harness is HarnessFamily.CLAUDE and self.reasoning_output_tokens is not None:
            raise ValueError("Claude records must not contain Codex reasoning usage")
        return self


class NativeWorkerUsageRecord(CaptureModel):
    """A complete Claude-native implementation-worker usage capture.

    The role attests only that this recorder built and executed the exact
    ``claude --agent <role>`` argv for the process whose usage this record totals;
    not an in-stream role echo and not routing authority (D161).
    """

    record_type: Literal["NATIVE_WORKER_USAGE"] = "NATIVE_WORKER_USAGE"
    schema_version: Literal[3] = NATIVE_WORKER_USAGE_SCHEMA_VERSION
    tool_version: SafeIdentifier = SKILL_VERSION
    captured_at: datetime
    task_id: SafeIdentifier
    slice_id: SafeIdentifier
    harness: Literal[HarnessFamily.CLAUDE] = HarnessFamily.CLAUDE
    native_role: NativeClaudeRole
    role_capability: RoleCapability
    model: SafeIdentifier
    effort: SafeIdentifier
    repository: RepositoryName
    branch: GitReference
    base_sha: GitSha
    final_head_sha: GitSha
    parent_task_id: SafeIdentifier
    session_id: SafeIdentifier
    subagent_count: Literal[0]
    usage_message_count: Annotated[int, Field(ge=1)]
    started_at: datetime
    completed_at: datetime
    elapsed_ms: Annotated[int, Field(ge=0)]
    exit_code: int
    success: bool
    harness_version: SafeIdentifier
    input_tokens: TokenCount
    cached_input_tokens: TokenCount
    cache_creation_input_tokens: TokenCount
    output_tokens: TokenCount
    claude_model_usage: tuple[ClaudeModelUsage, ...]
    estimated_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    estimate_basis: EstimateBasis = EstimateBasis.NOT_EXPOSED
    usage_complete: Literal[True] = True
    whole_tree_verified: Literal[True] = True

    @field_validator("harness", mode="before")
    @classmethod
    def normalize_serialized_claude_literal(cls, value: object) -> object:
        """Round-trip the sole JSON enum value without widening the literal type."""
        return HarnessFamily.CLAUDE if value == HarnessFamily.CLAUDE.value else value

    @model_validator(mode="after")
    def validate_usage(self) -> NativeWorkerUsageRecord:
        """Protect complete usage, exit, and estimate provenance invariants."""
        if self.success != (self.exit_code == 0):
            raise ValueError("success must match the harness exit code")
        if self.estimated_usd is None and self.estimate_basis is not EstimateBasis.NOT_EXPOSED:
            raise ValueError("an absent estimated_usd requires NOT_EXPOSED")
        if (
            self.estimated_usd is not None
            and self.estimate_basis is not EstimateBasis.CLIENT_SIDE_ESTIMATE
        ):
            raise ValueError("estimated_usd must be labelled CLIENT_SIDE_ESTIMATE")
        if len(self.claude_model_usage) != 1 or self.claude_model_usage[0].model != self.model:
            raise ValueError("native usage must contain the sole parent model")
        model_usage = self.claude_model_usage[0]
        if (
            model_usage.input_tokens != self.input_tokens
            or model_usage.cached_input_tokens != self.cached_input_tokens
            or model_usage.cache_creation_input_tokens != self.cache_creation_input_tokens
            or model_usage.output_tokens != self.output_tokens
        ):
            raise ValueError("native totals must equal parent model usage")
        if (
            self.role_capability is RoleCapability.READ_ONLY
            and self.final_head_sha != self.base_sha
        ):
            raise ValueError("read-only native workers must retain the base head")
        if self.role_capability is RoleCapability.WRITE and self.final_head_sha == self.base_sha:
            raise ValueError("write native workers must create a descendant head")
        return self


class CapacitySnapshotRecord(CaptureModel):
    """A qualitative capacity observation without raw readings or percentages."""

    record_type: Literal["CAPACITY_SNAPSHOT"] = "CAPACITY_SNAPSHOT"
    schema_version: Literal[1] = AGENT_USAGE_SCHEMA_VERSION
    tool_version: SafeIdentifier = SKILL_VERSION
    captured_at: datetime
    task_id: SafeIdentifier
    slice_id: SafeIdentifier
    family: HarnessFamily
    status: CapacityStatus
    source: CapacitySource

    @model_validator(mode="after")
    def validate_source_family(self) -> CapacitySnapshotRecord:
        """Bind each direct CLI source to the only harness family it can observe."""
        if self.source is CapacitySource.CLI_STATUS and self.family is not HarnessFamily.CODEX:
            raise ValueError("CLI_STATUS capacity snapshots require CODEX")
        if self.source is CapacitySource.CLI_USAGE and self.family is not HarnessFamily.CLAUDE:
            raise ValueError("CLI_USAGE capacity snapshots require CLAUDE")
        return self


class OutcomeRecord(CaptureModel):
    """Closed pilot-quality metadata appended after a completed slice."""

    record_type: Literal["OUTCOME"] = "OUTCOME"
    schema_version: Literal[1] = AGENT_USAGE_SCHEMA_VERSION
    tool_version: SafeIdentifier = SKILL_VERSION
    captured_at: datetime
    task_id: SafeIdentifier
    slice_id: SafeIdentifier
    finding_counts: dict[FindingLens, dict[FindingSeverity, TokenCount]]
    repair_commit_count: TokenCount
    final_gate_passed: bool

    @field_validator("finding_counts")
    @classmethod
    def reject_empty_finding_counts(
        cls, value: dict[FindingLens, dict[FindingSeverity, TokenCount]]
    ) -> dict[FindingLens, dict[FindingSeverity, TokenCount]]:
        """Require at least one explicit lens count, including zero findings."""
        if not value:
            raise ValueError("finding_counts requires at least one closed lens")
        if any(not counts for counts in value.values()):
            raise ValueError("each finding lens requires at least one severity count")
        return value


UsageRecord: TypeAlias = (
    TaskUsageRecord | NativeWorkerUsageRecord | CapacitySnapshotRecord | OutcomeRecord
)
"""The closed append-only record union."""

USAGE_RECORD_ADAPTER = TypeAdapter(Annotated[UsageRecord, Field(discriminator="record_type")])
"""Validator for records received at the sink boundary."""
