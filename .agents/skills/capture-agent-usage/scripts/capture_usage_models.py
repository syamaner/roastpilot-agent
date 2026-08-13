"""Closed, metadata-only models for the opt-in agent-usage capture pilot."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

AGENT_USAGE_SCHEMA_VERSION = 1
"""The append-only on-disk record schema version."""

SKILL_VERSION = "0.1.0"
"""The version of the capture skill's normalized record grammar."""


class HarnessFamily(Enum):
    """Supported harnesses with fixed executable and argument grammars."""

    CODEX = "CODEX"
    CLAUDE = "CLAUDE"


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
        return self


class CapacitySnapshotRecord(CaptureModel):
    """A qualitative capacity observation without raw readings or percentages."""

    record_type: Literal["CAPACITY_SNAPSHOT"] = "CAPACITY_SNAPSHOT"
    schema_version: Literal[1] = AGENT_USAGE_SCHEMA_VERSION
    tool_version: SafeIdentifier = SKILL_VERSION
    captured_at: datetime
    family: HarnessFamily
    status: CapacityStatus
    source: CapacitySource


class OutcomeRecord(CaptureModel):
    """Closed pilot-quality metadata appended after a completed slice."""

    record_type: Literal["OUTCOME"] = "OUTCOME"
    schema_version: Literal[1] = AGENT_USAGE_SCHEMA_VERSION
    tool_version: SafeIdentifier = SKILL_VERSION
    captured_at: datetime
    task_id: SafeIdentifier
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


UsageRecord: TypeAlias = TaskUsageRecord | CapacitySnapshotRecord | OutcomeRecord
"""The closed append-only record union."""

USAGE_RECORD_ADAPTER = TypeAdapter(Annotated[UsageRecord, Field(discriminator="record_type")])
"""Validator for records received at the sink boundary."""
