"""Closed, metadata-only models for the opt-in agent-usage capture pilot."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, BinaryIO, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

AGENT_USAGE_SCHEMA_VERSION = 1
"""The append-only on-disk record schema version."""

NATIVE_WORKER_USAGE_SCHEMA_VERSION = 3
"""D161 native-worker record schema, distinct from generic capture records."""

NATIVE_CODEX_USAGE_SCHEMA_VERSION = 1
"""Parent-to-registered-Codex-leaf record schema version."""

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
    SIM_ROAST_RUNNER = "sim-roast-runner"
    STORY_PLANNER = "story-planner"


class NativeCodexRole(Enum):
    """The only registered Codex leaves admitted to native capture."""

    ENGINEER_BE = "engineer-be"
    ENGINEER_FE = "engineer-fe"
    REPAIR = "repair"


class NativeCodexTaskStatus(Enum):
    """Closed parent-observed terminal task status."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


NATIVE_ROLE_EXCLUSIONS: dict[str, str] = {
    "safety-reviewer": (
        "its mandatory whole-diff safety review requires ordinary-role Bash/git access"
    ),
    "security-reviewer": (
        "its mandatory whole-diff security review requires ordinary-role Bash/git access"
    ),
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


VALIDATION_ENVIRONMENT_ROLES = frozenset(
    {
        NativeClaudeRole.QA,
        NativeClaudeRole.MCP_CONTRACT_CHECKER,
        NativeClaudeRole.SIM_ROAST_RUNNER,
    }
)
"""Roles requiring a parent-provisioned external validation environment (D166).

These three READ_ONLY roles execute Python/pytest to do their jobs; a
worktree-local ``.venv`` would fail the read-only pre-launch and post-exit
attestation, so their gates run against an external per-run root instead
(§2.4). ``--validation-root`` is required for exactly these roles and
rejected for every other role.
"""


class ValidationCommandKind(Enum):
    """Closed command-matching kind for one committed validation-role rule.

    ``EXACT`` renders a provider allow-rule that matches only the byte-exact
    command; ``PREFIX`` renders a rule matching that exact command followed
    by any arguments (D168, §2.2). Plain ``Enum``, never string-compared.
    """

    EXACT = "EXACT"
    PREFIX = "PREFIX"


@dataclass(frozen=True)
class ValidationCommand:
    """One committed validation-role gate command template.

    ``template`` uses exactly two placeholders, ``{python}`` and ``{tmp}``,
    substituted only from the one already-validated resolved root every
    other validation-environment consumer uses (D168, §2.2).
    """

    kind: ValidationCommandKind
    template: str


VALIDATION_ROLE_COMMANDS: dict[NativeClaudeRole, tuple[ValidationCommand, ...]] = {
    NativeClaudeRole.QA: (
        ValidationCommand(ValidationCommandKind.PREFIX, "{python} -m pytest"),
        ValidationCommand(ValidationCommandKind.EXACT, "{python} -m pyright --pythonpath {python}"),
        ValidationCommand(ValidationCommandKind.EXACT, "{python} -m ruff check ."),
        ValidationCommand(ValidationCommandKind.EXACT, "{python} -m ruff format --check ."),
    ),
    NativeClaudeRole.MCP_CONTRACT_CHECKER: (
        ValidationCommand(ValidationCommandKind.EXACT, "{python} -m pip show coffee-roaster-mcp"),
        ValidationCommand(
            ValidationCommandKind.EXACT,
            "{python} -m pytest tests/test_mcp_client.py -q --basetemp {tmp}/pytest",
        ),
    ),
    NativeClaudeRole.SIM_ROAST_RUNNER: (
        ValidationCommand(
            ValidationCommandKind.EXACT,
            "{python} -m pytest tests/test_milestone1.py "
            "tests/test_milestone1_real_mcp.py -q --basetemp {tmp}/pytest",
        ),
    ),
}
"""The single source of truth for validation-role gate commands (D168, §2.2).

Keys equal :data:`VALIDATION_ENVIRONMENT_ROLES` exactly; no other role is
present. Rendered two ways from this one table, never independently: as the
exact per-run commands ``print-validation-commands`` prints into the
lead-authored role brief, and as the ``--allowedTools`` rules bound to the
native launch argv (§2.2, §2.6)."""


def render_validation_commands(role: NativeClaudeRole, root: str) -> tuple[str, ...]:
    """Render one role's exact gate commands against one validated root.

    Performs no filesystem access itself; ``root`` must already be the
    canonical resolved return value of the sole
    :func:`~capture_usage_cli._validate_validation_root` call for this run.

    Args:
        role: The candidate native role.
        root: The canonical resolved validation root.

    Returns:
        The ordered, rendered command strings for ``role``, or an empty
        tuple when ``role`` is not a validation role.
    """
    commands = VALIDATION_ROLE_COMMANDS.get(role, ())
    python = os.path.join(root, "venv", "bin", "python")
    tmp = os.path.join(root, "tmp")
    return tuple(command.template.format(python=python, tmp=tmp) for command in commands)


def render_allowed_tools(role: NativeClaudeRole, root: str) -> tuple[str, ...]:
    """Render one role's committed ``--allowedTools`` rules from the same table.

    Never performs an independent substitution or validation; wraps exactly
    the strings :func:`render_validation_commands` returns.

    Args:
        role: The candidate native role.
        root: The canonical resolved validation root.

    Returns:
        The ordered ``Bash(...)`` rule strings for ``role`` — an ``EXACT``
        entry renders ``Bash(<command>)`` and a ``PREFIX`` entry renders
        ``Bash(<command>:*)`` — or an empty tuple when ``role`` is not a
        validation role.
    """
    commands = VALIDATION_ROLE_COMMANDS.get(role, ())
    rendered = render_validation_commands(role, root)
    return tuple(
        f"Bash({text}:*)" if command.kind is ValidationCommandKind.PREFIX else f"Bash({text})"
        for command, text in zip(commands, rendered, strict=True)
    )


class BoundRootKind(Enum):
    """Closed kind of one parent-provisioned bound root for a native launch (D169, §2.2).

    Plain ``Enum``, never string-compared: at most one kind is ever active for
    a single native launch because the three policies' admitted role sets are
    pairwise disjoint (proven once by a closure test, never re-checked at
    runtime).
    """

    VALIDATION = "VALIDATION"
    PLAN = "PLAN"
    EVIDENCE = "EVIDENCE"


VALIDATION_ENVIRONMENT_KEYS = frozenset(
    {
        "ROASTPILOT_VALIDATION_ROOT",
        "ROASTPILOT_VALIDATION_PYTHON",
        "ROASTPILOT_VALIDATION_TMP",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "PYTHONPYCACHEPREFIX",
        "PYTHONDONTWRITEBYTECODE",
        "RUFF_CACHE_DIR",
        "COVERAGE_FILE",
        "COFFEE_ROAST_LOG_DIR",
        "PIP_CACHE_DIR",
        "PYTEST_ADDOPTS",
    }
)
"""Closed environment-variable names bound only for a validation-role launch.

Moved here from ``capture_usage_cli`` (D169, §2.2) so :data:`BOUND_ROOT_POLICIES`
can reference it directly. Stripped from every native launch's inherited
environment first, then reinstated with exactly these values only when the
role is a member of :data:`VALIDATION_ENVIRONMENT_ROLES` (D166 §2.4)."""

PLAN_ROOT_ENVIRONMENT_KEY = "ROASTPILOT_PLAN_ROOT"
"""The one environment key bound only for a PLAN-kind native launch (D169, §2.3)."""

EVIDENCE_ROOT_ENVIRONMENT_KEY = "ROASTPILOT_EVIDENCE_ROOT"
"""The one environment key bound only for an EVIDENCE-kind native launch (D169, §2.4)."""

PLAN_ENVIRONMENT_KEYS = frozenset({PLAN_ROOT_ENVIRONMENT_KEY})
EVIDENCE_ENVIRONMENT_KEYS = frozenset({EVIDENCE_ROOT_ENVIRONMENT_KEY})

ALL_BOUND_ROOT_ENVIRONMENT_KEYS = (
    VALIDATION_ENVIRONMENT_KEYS | PLAN_ENVIRONMENT_KEYS | EVIDENCE_ENVIRONMENT_KEYS
)
"""The closed union of every bound-root environment key (thirteen total, D169, §2.2).

Stripped from every native launch's inherited environment first; exactly one
kind's keys are then reinstated, and only when that kind is the launch's
active bound root."""


@dataclass(frozen=True)
class BoundRootPolicy:
    """One closed bound-root kind's option grammar and role admission (D169, §2.2)."""

    kind: BoundRootKind
    root_option: str
    companion_option: str | None
    required_roles: frozenset[NativeClaudeRole]
    optional_roles: frozenset[NativeClaudeRole]
    environment_keys: frozenset[str]


BOUND_ROOT_POLICIES: dict[BoundRootKind, BoundRootPolicy] = {
    BoundRootKind.VALIDATION: BoundRootPolicy(
        kind=BoundRootKind.VALIDATION,
        root_option="--validation-root",
        companion_option=None,
        required_roles=VALIDATION_ENVIRONMENT_ROLES,
        optional_roles=frozenset(),
        environment_keys=VALIDATION_ENVIRONMENT_KEYS,
    ),
    BoundRootKind.PLAN: BoundRootPolicy(
        kind=BoundRootKind.PLAN,
        root_option="--plan-root",
        companion_option="--plan-sha",
        required_roles=frozenset(
            {
                NativeClaudeRole.PLANNING_ARCHITECT,
                NativeClaudeRole.PRODUCT_AUDITOR,
                NativeClaudeRole.STORY_PLANNER,
            }
        ),
        optional_roles=frozenset(),
        environment_keys=PLAN_ENVIRONMENT_KEYS,
    ),
    BoundRootKind.EVIDENCE: BoundRootPolicy(
        kind=BoundRootKind.EVIDENCE,
        root_option="--evidence-root",
        companion_option="--evidence-pr",
        required_roles=frozenset({NativeClaudeRole.PR_TRIAGE}),
        optional_roles=frozenset(),
        environment_keys=EVIDENCE_ENVIRONMENT_KEYS,
    ),
}
"""The exactly-three closed bound-root policies (D169, §2.2).

Every admitted role set (``required_roles | optional_roles``) is pairwise
disjoint across the three policies, so at most one policy's root can ever be
active for a single native launch — proven once by a closure test in
``tests/test_capture_agent_usage.py``, never re-checked at runtime. This
table's :data:`BoundRootKind.VALIDATION` entry's ``required_roles`` **is**
:data:`VALIDATION_ENVIRONMENT_ROLES` (the identical object), so the two never
drift."""


@dataclass(frozen=True)
class BoundRoot:
    """The one validated bound root for a native launch, if any (D169, §2.2).

    ``reattest`` is ``None`` for :data:`BoundRootKind.VALIDATION` (no D169
    post-exit re-check is defined for that pre-existing kind) and a bound,
    zero-argument closure for :data:`BoundRootKind.PLAN` and
    :data:`BoundRootKind.EVIDENCE`, capturing exactly the state needed to
    re-verify identity and integrity after the native child exits.
    """

    kind: BoundRootKind
    path: str
    reattest: Callable[[], None] | None = None
    descriptor: int | None = None


EVIDENCE_SCHEMA_VERSION = 1
"""The closed PR-evidence-bundle manifest schema version (D169, §2.4)."""

EVIDENCE_MANIFEST_NAME = "manifest.json"
"""The one bundle manifest file name."""

EVIDENCE_PAYLOAD_FILES: tuple[str, ...] = (
    "pr.json",
    "diff.patch",
    "checks.json",
    "reviews.json",
    "review-comments.json",
    "issue-comments.json",
    "authors.json",
    "review-threads.json",
)
"""The closed, ordered set of the eight parent-fetched PR evidence payload files."""

EVIDENCE_BUNDLE_FILES: frozenset[str] = frozenset({EVIDENCE_MANIFEST_NAME, *EVIDENCE_PAYLOAD_FILES})
"""The exact nine-entry closed bundle listing (D169, §2.4): no subdirectories, no extras."""

EVIDENCE_MAX_MANIFEST_BYTES = 65_536
"""64 KiB manifest cap (D169, §2.4)."""

EVIDENCE_MAX_FILE_BYTES = 4 * 1024 * 1024
"""4 MiB per-payload-file cap (D169, §2.4)."""

EVIDENCE_MAX_TOTAL_BYTES = 16 * 1024 * 1024
"""16 MiB aggregate payload cap (D169, §2.4)."""

EVIDENCE_CHUNK_BYTES = 65_536
"""Bounded streaming chunk size for payload hashing (D169, §2.4)."""


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


class NativeCodexUsageRecord(CaptureModel):
    """One metadata-only result from a registered Codex leaf lifecycle."""

    record_type: Literal["NATIVE_CODEX_USAGE"] = "NATIVE_CODEX_USAGE"
    schema_version: Literal[1] = NATIVE_CODEX_USAGE_SCHEMA_VERSION
    tool_version: SafeIdentifier = SKILL_VERSION
    captured_at: datetime
    task_id: SafeIdentifier
    slice_id: SafeIdentifier
    parent_task_id: SafeIdentifier
    task_name: SafeIdentifier
    native_role: NativeCodexRole
    role_capability: Literal[RoleCapability.WRITE] = RoleCapability.WRITE
    model: Literal["gpt-5.6-terra"] = "gpt-5.6-terra"
    effort: SafeIdentifier
    config_sha256: str
    role_sha256: str
    repository: RepositoryName
    branch: GitReference
    base_sha: GitSha
    launch_head_sha: GitSha
    final_head_sha: GitSha
    parent_thread_id: SafeIdentifier
    leaf_session_id: SafeIdentifier
    topology_depth: Literal[1] = 1
    harness_version: Literal["0.147.0"] = "0.147.0"
    # Native collaboration reports a task terminal state, not a shell exit code.
    exit_code: None = None
    task_status: NativeCodexTaskStatus
    success: bool
    started_at: datetime
    completed_at: datetime
    elapsed_ms: TokenCount
    input_tokens: TokenCount
    cached_input_tokens: TokenCount
    cache_write_input_tokens: TokenCount
    output_tokens: TokenCount
    reasoning_output_tokens: TokenCount
    total_tokens: TokenCount
    # True proves every newly-created rollout was scanned and none named this
    # leaf as parent; zero ``subagent_count`` is the corresponding evidence.
    whole_tree_verified: bool
    subagent_count: int

    @field_validator("role_capability", mode="before")
    @classmethod
    def normalize_serialized_write_capability(cls, value: object) -> object:
        """Round-trip the one persisted Enum value without widening capability."""
        return RoleCapability.WRITE if value == RoleCapability.WRITE.value else value

    @model_validator(mode="after")
    def validate_native_codex_usage(self) -> NativeCodexUsageRecord:
        """Require Git and task outcomes to agree with the closed lifecycle."""
        if self.success:
            if self.task_status is not NativeCodexTaskStatus.SUCCESS:
                raise ValueError("successful native Codex record has contradictory task status")
            if self.final_head_sha == self.base_sha:
                raise ValueError("successful native Codex record requires a descendant head")
        elif self.task_status is NativeCodexTaskStatus.SUCCESS:
            raise ValueError("failed native Codex record has contradictory task status")
        if not all(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
            for value in (self.config_sha256, self.role_sha256)
        ):
            raise ValueError("native Codex role hashes must be SHA-256")
        if self.subagent_count < 0 or (self.whole_tree_verified and self.subagent_count != 0):
            raise ValueError("native Codex topology proof is contradictory")
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
    TaskUsageRecord
    | NativeWorkerUsageRecord
    | NativeCodexUsageRecord
    | CapacitySnapshotRecord
    | OutcomeRecord
)
"""The closed append-only record union."""

USAGE_RECORD_ADAPTER = TypeAdapter(Annotated[UsageRecord, Field(discriminator="record_type")])
"""Validator for records received at the sink boundary."""
