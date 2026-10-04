"""Sanitised, allow-listed report over one interpreted cold-characterisation run.

The builder takes only a retained run, calls ``interpret_retained_run`` exactly
once, and projects the returned interpretation and its rebound capability into a
closed report schema.  Every string leaf is an enum member, a literal, or a
lowercase hexadecimal digest under an anchored pattern; the run and session
identifiers appear only as tagged SHA-256 digests.  Each report field is built
from one typed, allow-listed source: evidence is never dumped and then redacted.
The renderer builds that report once and emits canonical JSON bytes, re-parsed to
prove every object's key set, and a Markdown summary whose fixed templates walk
only the closed report model.

The module is pure.  It performs no file, network or process I/O and never calls
the retained-run reader, the evidence writer or the tree verifier.  It computes no
run verdict and compares no measurement with a limit: the locked D191 and
production limits are rendered, labelled not compared, and per-check results are
rendered as each check defines them.  "Not compared" means not compared by this
report projection: the G17 inference-duration check already compares against the
fixed seven-second hop, and no rendered limit is compared against the recorded
profile.  A per-check pass is not evidence of sustained inference, run duration or
run qualification.

Honest limit: the manifest digest is carried from the supplied run and is not
re-verified here, and nothing here establishes the run's completeness, provenance
or independent storage.  MCP-reported finalisation fields are recorded values, not
evaluated here.  Temperatures are Celsius only.

Named privacy residuals, disclosed rather than waived: when identity qualification
passes, the caller-asserted hexadecimal commitments (the 40-character source
revision and the 64-character artefact and profile-source digests) are rendered.
They are accepted by shape alone, so any of them could be a hex-shaped secret, and
nothing here proves that they are real commits or digests.  The run and session
identifiers are rendered only as tagged SHA-256 digests, but those digests are
deterministic: equal identifiers remain linkable across reports, and a predictable
identifier can be guessed by hashing candidates, so the digests do not keep
identifiers secret.
"""

import enum
import hashlib
import json
import math
import typing

import pydantic

from roastpilot_agent.cold_characterisation.acceptance import (
    D191_N_LIMIT,
    D191_X_LIMIT_MS,
    EFFECTIVE_HOP_SECONDS,
    PRODUCTION_FATAL_STREAK,
    ColdCheck,
    ColdCheckFailure,
    ColdCheckOutcome,
    ColdD191Metrics,
    ColdIdentityFacts,
    ColdInterpretation,
    ColdInterpretationError,
    ColdPhaseInterpretation,
    ColdReboundPhase,
    interpret_retained_run,
)
from roastpilot_agent.cold_characterisation.evidence_reader import (
    ABORT_REASON_BY_DOMAIN,
    ColdRetainedRun,
)
from roastpilot_agent.cold_characterisation.evidence_schema import (
    ColdAbortDomain,
    ColdAdvisorFailureKind,
    ColdCapabilityBranch,
    ColdEngineAbortReason,
    ColdEvidenceFailure,
    ColdFinalisationStatus,
    ColdHostAbortReason,
    ColdIdentityAbortReason,
    ColdMcpAbortReason,
    ColdOperatorAbortReason,
    ColdPhaseKind,
)
from roastpilot_agent.cold_characterisation.evidence_store import canonical_json

__all__ = (
    "ColdReportAbort",
    "ColdReportAdvisorFailureCount",
    "ColdReportCheck",
    "ColdReportCounters",
    "ColdReportError",
    "ColdReportFailure",
    "ColdReportHostExtremes",
    "ColdReportLockedLimits",
    "ColdReportPhase",
    "ColdReportRecordingArtefact",
    "ColdSanitisedReport",
    "build_sanitised_report",
    "render_sanitised_report",
)

_SHA256_RUST_PATTERN: typing.Final = r"\A[0-9a-f]{64}\z"
_RUN_ID_TAG: typing.Final = b"rp954-cold-run-id-v1\x00"
_SESSION_ID_TAG: typing.Final = b"rp954-cold-session-id-v1\x00"
#: Rendered once, labelled not compared; the pin below is their only comparison.
_LOCKED_LIMIT_VALUES: typing.Final = (
    D191_N_LIMIT,
    D191_X_LIMIT_MS,
    PRODUCTION_FATAL_STREAK,
    EFFECTIVE_HOP_SECONDS,
)
#: ``ValueError`` includes pydantic's ``ValidationError`` and Unicode codec errors.
_VALUE_ERRORS: typing.Final = (ValueError, TypeError, ArithmeticError)
_NULL: typing.Final = "`null`"
_UNAVAILABLE: typing.Final = "`null` (unavailable)"
#: Protocol intent only: true of every rendered report, including partial or aborted runs.
_PROTOCOL_INTENT: typing.Final = (
    (
        "Cold, empty-roaster load characterisation summary; not detector-accuracy, "
        "deployment-acoustics, Pi-readiness or live-roast evidence."
    ),
    (
        "The mode is designed to issue no actuator or control command and to permit "
        "only D187 all-zero protocol frames; this report does not evaluate command-state, "
        "safe-zero, serial-write or physical-response evidence and makes no finding that "
        "actuation did or did not occur."
    ),
    (
        "Per-tick software observation checks commanded heat, main fan, roast fan and cooling. "
        "Drum and solenoid/drop appear only in eligible D195 six-dimension finalisation "
        "evidence. These are commanded software values, not physical sensing or proof of "
        "physical response."
    ),
    (
        "This report renders per-check results and states no run verdict; D191 limits are "
        "shown, not compared; the recorded identity is committed by digest and is not a "
        "complete D192 identity."
    ),
    (
        "The manifest digest is carried from the supplied run and is not re-verified here; "
        "this report does not establish completeness, provenance or independent storage, "
        "and MCP-reported fields are recorded values, not evaluated here."
    ),
)
#: Markdown section headings for nested report fields; every other field is a table row.
_SECTION_NOTES: typing.Final = (
    (
        "locked_limits",
        "shown, not compared by the report projection; G17 already compares inference "
        "duration against the fixed seven-second hop; these recorded limits are not "
        "compared against the recorded profile",
    ),
    (
        "checks",
        "per-check results, not a run verdict; a per-check pass is not evidence of "
        "sustained inference, run duration or run qualification",
    ),
    ("d191", "derived, not compared"),
    ("counters", "final snapshot, plus the series maximum inference duration; null if unavailable"),
    ("aborts", "closed classifications"),
    ("advisor_failure_counts", "closed classifications"),
    ("host_extremes", "recorded, not compared"),
    ("recording_artefacts", "stat only"),
    ("identity_facts", "shown only when identity check v1 has no failure"),
)

_Digest: typing.TypeAlias = typing.Annotated[str, pydantic.Field(pattern=_SHA256_RUST_PATTERN)]
_Count: typing.TypeAlias = typing.Annotated[int, pydantic.Field(ge=0)]
_Measure: typing.TypeAlias = typing.Annotated[float, pydantic.Field(ge=0)]
_T = typing.TypeVar("_T")


class ColdReportFailure(enum.Enum):
    """Closed failures for building or rendering a sanitised report."""

    REBIND_FAILED = "rebind_failed"
    VALUE_NOT_ADMITTED = "value_not_admitted"
    EGRESS_KEYSET_MISMATCH = "egress_keyset_mismatch"


class ColdReportError(RuntimeError):
    """Closed report error carrying only its failure member and a fixed message."""

    failure: ColdReportFailure

    def __init__(self, failure: ColdReportFailure) -> None:
        """Create a content-free report failure.

        Args:
            failure: Closed refusal reason.
        """
        super().__init__("Cold report failed.")
        self.failure = failure


class _ReportModel(pydantic.BaseModel):
    """Frozen, closed, strict and finite: the configuration of every report model."""

    model_config = pydantic.ConfigDict(
        frozen=True, extra="forbid", strict=True, allow_inf_nan=False
    )


class ColdReportLockedLimits(_ReportModel):
    """The locked D191 and production limits, pinned to the acceptance constants.

    They are shown beside the derived values and are never compared with them by
    this report projection; the label "not compared" means exactly that.  The G17
    inference-duration check already compares against the fixed seven-second hop,
    and these recorded limits are not compared against the recorded profile.
    """

    label: typing.Literal["not compared"]
    n: int
    x_ms: float
    fatal_streak: int
    hop_seconds: float

    @pydantic.model_validator(mode="after")
    def _require_acceptance_constants(self) -> typing.Self:
        """Pin every rendered limit to its acceptance constant."""
        if (self.n, self.x_ms, self.fatal_streak, self.hop_seconds) != _LOCKED_LIMIT_VALUES:
            raise ValueError("locked limits differ from the acceptance constants")
        return self


class ColdReportCheck(_ReportModel):
    """One per-check result, as its software check defines it; not a run verdict."""

    check: ColdCheck
    outcome: ColdCheckOutcome
    failures: tuple[ColdCheckFailure, ...]

    @pydantic.model_validator(mode="after")
    def _require_consistent_failures(self) -> typing.Self:
        """Require unique failures in declaration order and ``PASS`` exactly without one."""
        ordered = tuple(member for member in ColdCheckFailure if member in self.failures)
        outcome_is_pass = self.outcome is ColdCheckOutcome.PASS
        if self.failures != ordered or outcome_is_pass == bool(self.failures):
            raise ValueError("check failures and outcome disagree")
        return self


class ColdReportCounters(_ReportModel):
    """Final-snapshot counters plus the G17 maximum; each ``None`` when unavailable."""

    emitted: _Count | None
    processed: _Count | None
    dropped: _Count | None
    inference_overruns: _Count | None
    total_overflows: _Count | None
    max_inference_duration_ms: _Measure | None


class ColdReportAbort(_ReportModel):
    """One abort classification: a closed domain and its paired closed reason."""

    domain: ColdAbortDomain
    reason: (
        ColdHostAbortReason
        | ColdIdentityAbortReason
        | ColdEvidenceFailure
        | ColdMcpAbortReason
        | ColdAdvisorFailureKind
        | ColdOperatorAbortReason
        | ColdEngineAbortReason
    )

    @pydantic.model_validator(mode="after")
    def _require_paired_reason(self) -> typing.Self:
        """Require the reason's enum to be the one the retained-run reader pairs with its domain."""
        if type(self.reason) is not ABORT_REASON_BY_DOMAIN.get(self.domain):
            raise ValueError("abort reason does not belong to its domain")
        return self


class ColdReportAdvisorFailureCount(_ReportModel):
    """How many advisory records carry one advisor-failure kind."""

    kind: ColdAdvisorFailureKind
    count: _Count


class ColdReportHostExtremes(_ReportModel):
    """Host extremes across a phase's host records; rendered, never compared."""

    max_soc_temp_c: float
    min_mem_available_bytes: _Count
    min_free_bytes: _Count


class ColdReportRecordingArtefact(_ReportModel):
    """One recording artefact's role and size; its path and filename are never read."""

    role: typing.Literal[
        "primary_wav", "recording_sidecar", "annotation_session_sidecar", "additional_wav"
    ]
    size_bytes: _Count | None


class ColdReportPhase(_ReportModel):
    """One present phase: its five per-check results and allow-listed derived values."""

    phase: ColdPhaseKind
    identity_sha256: _Digest
    checks: tuple[ColdReportCheck, ...]
    d191: ColdD191Metrics | None
    tick_count: _Count
    observed_tick_span_seconds: _Measure | None
    counters: ColdReportCounters
    session_id_sha256: _Digest | None
    mcp_reported_finalisation_status: ColdFinalisationStatus | None
    mcp_reported_clean: bool | None
    observed_command_streaming_required: bool | None
    applied_branch: ColdCapabilityBranch | None
    aborts: tuple[ColdReportAbort, ...]
    advisory_record_count: _Count
    advisor_failure_counts: tuple[ColdReportAdvisorFailureCount, ...]
    host_extremes: ColdReportHostExtremes | None
    recording_artefacts: tuple[ColdReportRecordingArtefact, ...] | None
    identity_facts: ColdIdentityFacts | None

    @pydantic.model_validator(mode="after")
    def _require_closed_phase(self) -> typing.Self:
        """Require the five checks in order, facts with qualification, one count per kind."""
        if tuple(item.check for item in self.checks) != tuple(ColdCheck):
            raise ValueError("phase checks are not the closed check set")
        if (self.checks[0].outcome is ColdCheckOutcome.PASS) != (self.identity_facts is not None):
            raise ValueError("identity facts disagree with the qualification result")
        kinds = tuple(item.kind for item in self.advisor_failure_counts)
        if kinds != tuple(ColdAdvisorFailureKind):
            raise ValueError("advisor failure counts are not one per kind")
        return self


class ColdSanitisedReport(_ReportModel):
    """The closed public report schema v1 over one interpreted retained run."""

    report_schema_version: typing.Literal[1]
    run_id_sha256: _Digest
    manifest_sha256: _Digest
    locked_limits: ColdReportLockedLimits
    phases: tuple[ColdReportPhase, ...]
    phases_absent: tuple[ColdPhaseKind, ...]

    @pydantic.model_validator(mode="after")
    def _require_partitioned_phases(self) -> typing.Self:
        """Require present phases in phase order and ``phases_absent`` as their complement."""
        present = tuple(item.phase for item in self.phases)
        if not present or present != tuple(kind for kind in ColdPhaseKind if kind in present):
            raise ValueError("phases are empty, repeated or out of phase order")
        if self.phases_absent != tuple(kind for kind in ColdPhaseKind if kind not in present):
            raise ValueError("absent phases are not the complement of the present phases")
        return self


# ------------------------------------------------------------------ projection


def _tagged_sha256(tag: bytes, value: str) -> str:
    """Return the SHA-256 of a domain tag followed by one UTF-8 identifier."""
    return hashlib.sha256(tag + value.encode("utf-8")).hexdigest()


def _count(value: object) -> int | None:
    """An exact non-negative ``int``; an absent, malformed or negative value is unavailable."""
    return value if type(value) is int and value >= 0 else None


def _reading(value: object) -> float | None:
    """An exact finite non-negative ``float``; anything else is unavailable."""
    return value if type(value) is float and math.isfinite(value) and value >= 0 else None


def _tick_span(rebound: ColdReboundPhase) -> float | None:
    """The last tick's monotonic seconds minus the first's; ``None`` below two or if negative."""
    ticks = rebound.ticks
    if len(ticks) < 2:
        return None
    span = ticks[-1].monotonic_seconds - ticks[0].monotonic_seconds
    return span if span >= 0 else None


def _g17_maximum(rebound: ColdReboundPhase) -> float | None:
    """The largest ``max_inference_duration_ms`` over ``S``, or ``None`` when unavailable.

    ``S`` is every tick sample, then the pre-finalisation and final snapshots.  The
    maximum is unavailable (never zero) unless every element is present and every
    reading is an exact finite non-negative float.
    """
    result = rebound.finalisation
    pre = None if result is None else result.pre_finalisation_first_crack_status
    runtime = None if result is None else result.first_crack_runtime
    if not rebound.ticks or pre is None or runtime is None:
        return None
    samples = (*(tick.audio for tick in rebound.ticks), pre, runtime.final_status)
    readings = [_reading(sample.max_inference_duration_ms) for sample in samples]
    admitted = [value for value in readings if value is not None]
    return max(admitted) if len(admitted) == len(readings) else None


def _counters(rebound: ColdReboundPhase) -> ColdReportCounters:
    """Read counters from the frozen final snapshot, plus the G17 maximum over ``S``."""
    result = rebound.finalisation
    runtime = None if result is None else result.first_crack_runtime
    if runtime is None:
        return ColdReportCounters.model_validate(dict.fromkeys(ColdReportCounters.model_fields))
    final = runtime.final_status
    return ColdReportCounters(
        emitted=_count(final.emitted_window_count),
        processed=_count(final.processed_window_count),
        dropped=_count(final.dropped_window_count),
        inference_overruns=_count(final.inference_overrun_count),
        total_overflows=_count(final.total_overflow_count),
        max_inference_duration_ms=_g17_maximum(rebound),
    )


def _host_extremes(rebound: ColdReboundPhase) -> ColdReportHostExtremes | None:
    """Host extremes, or ``None`` without a host record or with any unadmitted byte count."""
    samples = tuple(record.sample for record in rebound.hosts)
    memory = [_count(sample.mem_available_bytes) for sample in samples]
    free = [_count(sample.free_bytes) for sample in samples]
    if not samples or None in memory or None in free:
        return None
    return ColdReportHostExtremes(
        max_soc_temp_c=max(sample.soc_temp_c for sample in samples),
        min_mem_available_bytes=min(typing.cast(list[int], memory)),
        min_free_bytes=min(typing.cast(list[int], free)),
    )


def _recording_artefacts(
    rebound: ColdReboundPhase,
) -> tuple[ColdReportRecordingArtefact, ...] | None:
    """The selected recording's artefact roles and sizes, or ``None`` without that evidence."""
    result = rebound.finalisation
    recording = None if result is None else result.recording
    if recording is None:
        return None
    return tuple(
        ColdReportRecordingArtefact(role=artefact.role, size_bytes=_count(artefact.size_bytes))
        for artefact in recording.artifacts
    )


def _projected_phase(item: ColdPhaseInterpretation, rebound: ColdReboundPhase) -> ColdReportPhase:
    """Project one phase's interpretation and bound snapshots into the report schema.

    MCP-reported fields and the session digest come from the last bound finalisation
    record only when a finalisation was selected (unambiguous); otherwise ``None``.
    """
    last = rebound.finalisations[-1] if rebound.finalisation is not None else None
    return ColdReportPhase(
        phase=item.phase,
        identity_sha256=item.identity_sha256,
        checks=tuple(
            ColdReportCheck(check=result.check, outcome=result.outcome, failures=result.failures)
            for result in item.results
        ),
        d191=item.d191,
        tick_count=len(rebound.ticks),
        observed_tick_span_seconds=_tick_span(rebound),
        counters=_counters(rebound),
        session_id_sha256=(
            None if last is None else _tagged_sha256(_SESSION_ID_TAG, last.session_id)
        ),
        mcp_reported_finalisation_status=None if last is None else last.status,
        mcp_reported_clean=None if last is None else last.clean,
        observed_command_streaming_required=(
            None if last is None else last.observed_command_streaming_required
        ),
        applied_branch=None if last is None else last.applied_branch,
        aborts=tuple(
            ColdReportAbort(domain=record.domain, reason=record.reason) for record in rebound.aborts
        ),
        advisory_record_count=len(rebound.advisories),
        advisor_failure_counts=tuple(
            ColdReportAdvisorFailureCount(
                kind=kind, count=sum(record.failure is kind for record in rebound.advisories)
            )
            for kind in ColdAdvisorFailureKind
        ),
        host_extremes=_host_extremes(rebound),
        recording_artefacts=_recording_artefacts(rebound),
        identity_facts=item.identity_facts,
    )


def _report_of(interpretation: ColdInterpretation) -> ColdSanitisedReport:
    """Project one interpretation and its rebound capability; nothing else is read."""
    rebound = interpretation.rebound
    pairs = tuple(zip(interpretation.phases, rebound.phases, strict=True))
    if any(item.phase is not phase.phase for item, phase in pairs):
        raise ValueError("interpreted and rebound phases disagree")
    present = frozenset(item.phase for item, _ in pairs)
    n, x_ms, fatal_streak, hop_seconds = _LOCKED_LIMIT_VALUES
    return ColdSanitisedReport(
        report_schema_version=1,
        run_id_sha256=_tagged_sha256(_RUN_ID_TAG, rebound.run_id),
        manifest_sha256=rebound.manifest_sha256,
        locked_limits=ColdReportLockedLimits(
            label="not compared", n=n, x_ms=x_ms, fatal_streak=fatal_streak, hop_seconds=hop_seconds
        ),
        phases=tuple(_projected_phase(item, phase) for item, phase in pairs),
        phases_absent=tuple(kind for kind in ColdPhaseKind if kind not in present),
    )


def _admitted(call: typing.Callable[[], _T]) -> _T:
    """Run one projection or egress step; a value error becomes ``VALUE_NOT_ADMITTED``.

    The error is constructed inside the handler and raised outside it, so it carries
    neither the original error's text nor its chain.
    """
    try:
        value = call()
    except _VALUE_ERRORS:
        error = ColdReportError(ColdReportFailure.VALUE_NOT_ADMITTED)
    else:
        return value
    raise error


# ------------------------------------------------------------------ egress


def _keys_match(model: pydantic.BaseModel, document: object) -> bool:
    """Whether a re-parsed object carries exactly its model's field keys, recursively."""
    fields = tuple(type(model).model_fields)
    if type(document) is not dict:
        return False
    parsed = typing.cast(dict[str, object], document)
    return set(parsed) == set(fields) and all(
        _value_keys_match(getattr(model, name), parsed[name]) for name in fields
    )


def _value_keys_match(value: object, document: object) -> bool:
    """Whether one re-parsed value has its report value's object and array shape."""
    if isinstance(value, pydantic.BaseModel):
        return _keys_match(value, document)
    if isinstance(value, tuple):
        items = typing.cast(tuple[object, ...], value)
        if type(document) is not list:
            return False
        children = typing.cast(list[object], document)
        return len(children) == len(items) and all(
            _value_keys_match(item, child) for item, child in zip(items, children, strict=True)
        )
    return type(document) is not dict and type(document) is not list


def _egress(report: ColdSanitisedReport) -> bytes | None:
    """Canonical UTF-8 JSON bytes, or ``None`` when a re-parsed key set differs."""
    document = canonical_json(report.model_dump(mode="json")).encode("utf-8")
    return document if _keys_match(report, json.loads(document)) else None


def _cell(value: object) -> str:
    """Render one report scalar, or a tuple of them, with a fixed spelling.

    Raises:
        TypeError: If the value is not a report scalar; nothing is rendered by ``repr``.
    """
    if value is None:
        return _NULL
    if isinstance(value, tuple):
        items = typing.cast(tuple[object, ...], value)
        return ", ".join(_cell(item) for item in items) or "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, enum.Enum):
        return f"`{value.value}`"
    if isinstance(value, str):
        return f"`{value}`"
    if type(value) is int or type(value) is float:
        return str(value)
    raise TypeError("report value is not a renderable scalar")


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    """Render one table whose header and body cells are already fixed or rendered text."""
    return [
        f"| {' | '.join(headers)} |",
        f"|{'---|' * len(headers)}",
        *(f"| {' | '.join(row)} |" for row in rows),
        "",
    ]


def _fields(model: pydantic.BaseModel, names: tuple[str, ...]) -> list[str]:
    """Render a field-name and value table over one report model's named fields."""
    return _table(("Field", "Value"), [(name, _cell(getattr(model, name))) for name in names])


def _section(value: object) -> list[str]:
    """Render one nested report value: a model, a tuple of models, or ``None``."""
    if value is None:
        return [_UNAVAILABLE, ""]
    if isinstance(value, pydantic.BaseModel):
        return _fields(value, tuple(type(value).model_fields))
    items = typing.cast(tuple[pydantic.BaseModel, ...], value)
    if not items:
        return ["none", ""]
    names = tuple(type(items[0]).model_fields)
    return _table(names, [tuple(_cell(getattr(item, name)) for name in names) for item in items])


def _model_markdown(model: pydantic.BaseModel, level: str, names: tuple[str, ...]) -> list[str]:
    """Render named report fields: scalars as table rows, each nested field as a section."""
    notes = dict(_SECTION_NOTES)
    lines = _fields(model, tuple(name for name in names if name not in notes))
    for name in names:
        if name in notes:
            lines += [f"{level} `{name}` ({notes[name]})", "", *_section(getattr(model, name))]
    return lines


def _markdown(report: ColdSanitisedReport) -> str:
    """Render the Markdown summary: the protocol-intent sentences, then the report model only."""
    lines = [f"# Cold characterisation summary (report schema {report.report_schema_version})", ""]
    for sentence in _PROTOCOL_INTENT:
        lines += [sentence, ""]
    run_names = tuple(name for name in type(report).model_fields if name != "phases")
    lines += ["## Run", "", *_model_markdown(report, "##", run_names)]
    for phase in report.phases:
        names = tuple(type(phase).model_fields)
        lines += [f"## Phase {_cell(phase.phase)}", "", *_model_markdown(phase, "###", names)]
    return "\n".join(lines)


# ------------------------------------------------------------------ entry points


def build_sanitised_report(run: ColdRetainedRun) -> ColdSanitisedReport:
    """Build the sanitised report for one retained run.

    It calls ``interpret_retained_run`` exactly once, then reads only the returned
    interpretation and its rebound capability.  It accepts no interpretation,
    result, verdict or text from the caller, never calls the reader, writer or
    verifier, and performs no file I/O.

    Args:
        run: A retained run, as the strict retained-run reader returns it.

    Returns:
        The closed report schema v1 over every present phase.

    Raises:
        ColdReportError: ``REBIND_FAILED`` if the run does not rebind, or
            ``VALUE_NOT_ADMITTED`` if the report schema does not admit a value.
    """
    try:
        interpretation = interpret_retained_run(run)
    except ColdInterpretationError:
        error = ColdReportError(ColdReportFailure.REBIND_FAILED)
    except _VALUE_ERRORS:
        error = ColdReportError(ColdReportFailure.VALUE_NOT_ADMITTED)
    else:
        return _admitted(lambda: _report_of(interpretation))
    raise error


def render_sanitised_report(run: ColdRetainedRun) -> tuple[bytes, str]:
    """Render the sanitised report for one retained run as JSON bytes and Markdown.

    It builds the report once and renders both forms from that report only; no
    renderer accepts a report object.  The JSON is canonical UTF-8, re-parsed so
    every object's key set is proved equal to its model's fields.

    Args:
        run: A retained run, as the strict retained-run reader returns it.

    Returns:
        The canonical JSON bytes and the fixed-template Markdown summary.

    Raises:
        ColdReportError: ``REBIND_FAILED`` or ``VALUE_NOT_ADMITTED`` as for the
            builder, or ``EGRESS_KEYSET_MISMATCH`` if a re-parsed key set differs.
    """
    report = build_sanitised_report(run)
    document = _admitted(lambda: _egress(report))
    if document is None:
        raise ColdReportError(ColdReportFailure.EGRESS_KEYSET_MISMATCH)
    return document, _admitted(lambda: _markdown(report))
