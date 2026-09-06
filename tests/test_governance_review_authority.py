"""Governance regression coverage for the D-ToS-1 reconciliation (#938).

This module guards the reconciliation of the GitHub-Claude review-gate
description in ``AGENTS.md`` and ``docs/state/registry.md`` with the live,
verified 6 Sep 2026 state: consumer-OAuth headless Claude CI is retired under
D-ToS-1 (merged in agent commit ``299fb5a93d09e54f58d370f7a35e5ce15f278150``,
PR #923) and ``main`` currently requires zero approving reviews. The D108-D118
PR-scoped Claude approval bridge mechanism is retained but dormant.

The closed grammar (contract §2.2) splits each governed file's text into two
mutually exclusive classes: HISTORICAL/DORMANT blocks, delimited by literal
``<!-- historical-evidence: begin/end -->`` HTML-comment markers on their own
line, and OPERATIVE text (everything else). A forbidden phrase set (P) may
only appear inside a historical block; a required phrase set (R) must appear
at least once in operative text. The parser in this module fails closed on
any malformed marker structure (unclosed, orphaned, nested, or reversed)
rather than silently reporting zero historical blocks.

T5 (the D-ToS-1 headless-gate mechanism still matches its documented
description) has split witnesses. The ``claude-code-review.yml`` reviewer
job's exact ``if:`` condition (the Dependabot-author guard AND the
`CLAUDE_HEADLESS_ENABLED` conjunct) remains asserted, byte-for-byte, by the existing, unchanged
``tests/test_claude_review_approval.py::test_track_progress_disabled_only_for_unsupported_pull_request_actions``
regression test. This module adds the distinct ``claude.yml`` responder-job
conjunct witness, including in-memory negative controls; it does not duplicate
the reviewer assertion.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENTS_MD_PATH = _REPO_ROOT / "AGENTS.md"

_BEGIN_MARKER = "<!-- historical-evidence: begin -->"
_END_MARKER = "<!-- historical-evidence: end -->"

# Phrase set P (contract §2.2): forbidden anywhere outside a historical
# block, case-insensitive, in AGENTS.md and docs/state/registry.md.
_FORBIDDEN_PHRASES: tuple[str, ...] = (
    "requires one approving review",
    "one approval with stale dismissal",
    "stale-dismissing approval",
    "WAIT for Claude's PR-scoped approval",
    "Activation state — LIVE",
    "D108-D118 are active",
    "is ACTIVE (31 Jul)",
)

# The single canonical operative statement of the current merge bar. T2 is
# scoped exactly to this bullet's body (heading through the historical
# marker that follows it) so that an unrelated prose fragment elsewhere in
# the document can never silently satisfy the retained-requirements guard.
_CANONICAL_LIVE_STATE_HEADING = "**Verified live state — 6 Sep 2026 (D-ToS-1).**"

# The complete retained gate list (contract §2.2 phrase set R, scoped to the
# canonical bullet): Codex review AND wait, independent triage, local Claude
# assurance, CI, CodeQL, conversation resolution, strict/app-pinned
# checks/admin enforcement (including the four exact app-pinned check
# names), and zero approving reviews.
_CANONICAL_LIVE_STATE_REQUIRED_PHRASES: tuple[str, ...] = (
    "CLAUDE_HEADLESS_ENABLED",
    "requires zero approving reviews",
    "strict mode",
    "app-pinned",
    "enforce_admins",
    "required_conversation_resolution",
    "rests on CI",
    "CodeQL handling",
    "Codex's exact-current-head review and wait",
    "independent triage",
    "risk-routed authenticated local Claude assurance",
)

_REQUIRED_CHECK_NAMES: tuple[str, ...] = (
    "Checks",
    "Web (lint + typecheck + unit)",
    "Web (Playwright snapshots)",
    "codecov/patch",
)

_SUPERSESSION_MARKER = "[Superseded 6 Sep 2026 by the D-ToS-1 entry at the top of this file:"
_DTOS1_COMMIT_SHA = "299fb5a93d09e54f58d370f7a35e5ce15f278150"
_RESPONDER_CONDITION = (
    "${{ vars.CLAUDE_HEADLESS_ENABLED == 'true' && ( "
    "(github.event_name == 'issue_comment' && contains(github.event.comment.body, '@claude')) || "
    "(github.event_name == 'pull_request_review_comment' && "
    "contains(github.event.comment.body, '@claude')) || "
    "(github.event_name == 'pull_request_review' && "
    "contains(github.event.review.body, '@claude')) || "
    "(github.event_name == 'issues' && (contains(github.event.issue.body, '@claude') || "
    "contains(github.event.issue.title, '@claude'))) ) }}"
)
_HISTORICAL_LABELS = frozenset(
    {
        "[HISTORICAL — RETAINED EVIDENCE, SUPERSEDED 6 Sep 2026 by D-ToS-1.]",
        "[HISTORICAL — RETAINED EVIDENCE, SUPERSEDED 6 Sep 2026 by D-ToS-1. "
        "This wait does not gate merge today; see the precedence subsection above.]",
        "[HISTORICAL — RETAINED EVIDENCE, SUPERSEDED 6 Sep 2026 by the D-ToS-1 "
        "governance reconciliation at the top of this file.]",
        "[HISTORICAL — RETAINED EVIDENCE, SUPERSEDED 6 Sep 2026 by the D-ToS-1 "
        "governance reconciliation recorded at the top of this file. The approval "
        "requirement this record describes no longer applies.]",
        "[HISTORICAL — SUPERSEDED 6 Sep 2026 by the D-ToS-1 entry at the top of this file.]",
    }
)


def _read_agents_md() -> str:
    """Return the full text of the repository's ``AGENTS.md``."""
    return _AGENTS_MD_PATH.read_text(encoding="utf-8")


def _historical_spans(text: str) -> list[tuple[int, int]]:
    """Return the character spans of every well-formed historical block.

    A marker line counts when, after stripping surrounding whitespace and at
    most one leading Markdown blockquote marker (``>``), it equals exactly
    ``<!-- historical-evidence: begin -->`` or ``... end -->``. Markers must
    be balanced, non-nested, non-overlapping, and appear begin-before-end in
    file order.

    Args:
        text: The full document text to scan.

    Returns:
        A list of ``(start, end)`` character-offset pairs, one per
        begin/end pair, each spanning from the start of the begin marker's
        line to the end of the end marker's line (inclusive of both marker
        lines).

    Raises:
        ValueError: If a begin marker is never closed, an end marker has no
            matching begin, a begin marker appears while another is already
            open (nesting), or the structure is otherwise malformed. This is
            a deliberate fail-closed design: a malformed document must never
            be silently treated as containing zero historical blocks.
    """
    spans: list[tuple[int, int]] = []
    open_start: int | None = None
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        unquoted = stripped[1:].strip() if stripped.startswith(">") else stripped
        is_begin = stripped == _BEGIN_MARKER or unquoted == _BEGIN_MARKER
        is_end = stripped == _END_MARKER or unquoted == _END_MARKER
        if is_begin:
            if open_start is not None:
                raise ValueError(f"nested historical-evidence begin marker at offset {pos}")
            open_start = pos
        elif is_end:
            if open_start is None:
                raise ValueError(f"orphan historical-evidence end marker at offset {pos}")
            spans.append((open_start, pos + len(line)))
            open_start = None
        pos += len(line)
    if open_start is not None:
        raise ValueError("unclosed historical-evidence begin marker")
    return spans


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Build a case-insensitive regex for ``phrase`` tolerant of Markdown formatting.

    Tokens (split on literal spaces in ``phrase``) are joined by a separator
    that accepts any run of whitespace, a Markdown blockquote marker
    (``>``), or emphasis asterisks (``*``), so a phrase that Markdown has
    soft-wrapped across a line break -- or that has a word wrapped in
    ``**bold**`` -- still matches as one occurrence.
    """
    tokens = phrase.split(" ")
    return re.compile(r"[\s>*]+".join(re.escape(token) for token in tokens), re.IGNORECASE)


def _occurrences(text: str, phrase: str) -> list[re.Match[str]]:
    """Return every match of ``phrase`` in ``text`` (see `_phrase_pattern`)."""
    return list(_phrase_pattern(phrase).finditer(text))


def _within_any_span(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    """Return whether the half-open range ``[start, end)`` sits inside one span."""
    return any(span_start <= start and end <= span_end for span_start, span_end in spans)


def _present_operatively(text: str, phrase: str, spans: list[tuple[int, int]]) -> bool:
    """Return whether ``phrase`` occurs at least once outside every span in ``spans``."""
    return any(not _within_any_span(m.start(), m.end(), spans) for m in _occurrences(text, phrase))


def _exact_markdown_code_name_pattern(name: str) -> re.Pattern[str]:
    """Return a case-sensitive pattern for an exact, code-formatted check name.

    Whitespace inside an inline-code status name may be soft-wrapped by
    Markdown, but every non-whitespace character, including punctuation, is
    literal. Requiring the code delimiters prevents generic prose such as
    ``required checks`` from standing in for the check actually named
    ``Checks``.
    """
    parts = re.split(r"(\s+)", name)
    pattern = "".join(r"\s+" if part.isspace() else re.escape(part) for part in parts)
    return re.compile(rf"`{pattern}`")


def _canonical_bullet_bounds(text: str) -> tuple[int, int]:
    """Return the canonical live-state bullet's operative character range.

    The first following historical marker is a boundary only after proving it
    lies after the heading. Callers also verify the entire returned range does
    not overlap any historical block, so wrapping the canonical bullet itself
    cannot turn a historical statement into an accepted live-state policy.
    """
    start = text.index(_CANONICAL_LIVE_STATE_HEADING)
    marker_index = text.index(_BEGIN_MARKER, start)
    end = text.rfind("\n", 0, marker_index) + 1
    if end <= start:
        raise ValueError("canonical live-state bullet has no following boundary")
    return start, end


def _canonical_live_state_bullet(text: str) -> str:
    """Return the body of AGENTS.md's "Verified live state — 6 Sep 2026" bullet.

    The body runs from the bullet's bold heading through (but not including)
    the historical marker that immediately follows it, so the returned text
    is entirely operative.

    Raises:
        ValueError: If the heading or a following historical-evidence begin
            marker cannot be found -- this must fail loudly, never silently
            check an empty or wrong substring.
    """
    start, end = _canonical_bullet_bounds(text)
    return text[start:end]


def _is_affirmative_enablement_instruction(text: str) -> bool:
    """Return whether text affirmatively tells a reader to enable headless CI.

    This deliberately small grammar permits explicit prohibitions (``Do not``
    and ``Never``) while rejecting affirmative ``set``, ``export``, and bare
    assignment forms that assign this exact variable to true. It normalizes
    emphasis, code delimiters, blockquotes, and line wrapping before applying
    the grammar; it is not a broad natural-language classifier.
    """
    normalized = re.sub(r"[`*>]", "", text)
    normalized = re.sub(r"\s+", " ", normalized)
    assignment = re.compile(
        r"(?P<prohibition>\b(?:do not|never)\s+)?"
        r"(?:(?:set|export)\s+)?\bCLAUDE_HEADLESS_ENABLED\b\s*"
        r"(?:=\s*|to\s+)['\"]?true['\"]?\b",
        re.IGNORECASE,
    )
    return any(match.group("prohibition") is None for match in assignment.finditer(normalized))


def _assert_agents_historical_evidence(text: str) -> None:
    """Assert AGENTS forbidden prose and retained historical anchors are contained."""
    spans = _historical_spans(text)
    _assert_historical_labels(text, spans)
    for phrase in _FORBIDDEN_PHRASES:
        for match in _occurrences(text, phrase):
            assert _within_any_span(match.start(), match.end(), spans), (
                f"{phrase!r} appears operatively in AGENTS.md at {match.start()}"
            )
    historical_text = "".join(text[start:end] for start, end in spans)
    for anchor in (
        "PR-scoped Claude restoration (#663 / D108-D118",
        "Activation state — LIVE 31 Jul 2026 (#663 / D108-D118)",
        "WAIT for Claude's PR-scoped approval before merging",
        "D108-D118 are active as of 31 Jul 2026",
    ):
        assert _occurrences(historical_text, anchor), f"missing AGENTS history: {anchor!r}"


def _assert_canonical_live_policy(text: str) -> None:
    """Assert the unique canonical AGENTS policy is entirely operative and complete."""
    spans = _historical_spans(text)
    assert text.count(_CANONICAL_LIVE_STATE_HEADING) == 1
    start, end = _canonical_bullet_bounds(text)
    assert not any(span_start < end and start < span_end for span_start, span_end in spans)
    bullet = _canonical_live_state_bullet(text)
    assert "CLAUDE_HEADLESS_ENABLED" in bullet, "missing canonical headless token"
    normalized_bullet = _normalized_visible(bullet)
    assert (
        "Consumer-OAuth headless Claude CI is retired (CLAUDE_HEADLESS_ENABLED unset; "
        "a skipped headless job is expected, never a failure, and never a reason "
        "to set the variable)." in normalized_bullet
    )
    assert (
        "codecov/patch; strict mode, required_conversation_resolution, and "
        "enforce_admins remain enabled; "
        "force-push/deletion off." in normalized_bullet
    ), "missing enabled canonical protection clauses"
    assert (
        "The merge bar instead rests on CI, codecov/patch, CodeQL handling, GitHub Codex's "
        "exact-current-head review and wait, required_conversation_resolution, independent triage, "
        "and risk-routed authenticated local Claude assurance (below)." in normalized_bullet
    )
    assert normalized_bullet == _CANONICAL_POLICY_SNAPSHOT
    assert not _is_affirmative_enablement_instruction(_operative_text(text, spans))
    for phrase in _CANONICAL_LIVE_STATE_REQUIRED_PHRASES:
        assert _occurrences(bullet, phrase), f"missing canonical policy phrase: {phrase!r}"
    for check_name in _REQUIRED_CHECK_NAMES:
        assert _exact_markdown_code_name_pattern(check_name).search(bullet), (
            f"missing canonical check name: {check_name!r}"
        )


def _assert_precedence_policy(text: str) -> None:
    """Assert the entire operative precedence bullet names both authorities."""
    spans = _historical_spans(text)
    start = text.index("- **Precedence for the GitHub-Claude required-approval description under")
    end = text.index("\n- Mechanism retained", start)
    assert not any(span_start < end and start < span_end for span_start, span_end in spans)
    precedence = text[start:end]
    assert _occurrences(precedence, "sole operative authorities")
    for filename in ("AGENTS.md", "docs/state/registry.md"):
        assert filename in precedence, f"missing precedence authority: {filename!r}"


def _assert_registry_policy(text: str) -> None:
    """Assert the first registry reconciliation entry is operative and complete."""
    spans = _historical_spans(text)
    header_index = text.index("## Active Epic")
    entry_heading = "**6 Sep 2026 — D-ToS-1 governance reconciliation (#938).**"
    entry_start = text.index(entry_heading, header_index)
    assert not re.search(r"\n\*\*[^\n]+", text[header_index:entry_start])
    entry_end = text.index("\n**1 Sep 2026", entry_start)
    assert not any(
        span_start < entry_end and entry_start < span_end for span_start, span_end in spans
    )
    entry = text[entry_start:entry_end]
    normalized_entry = _normalized_visible(entry)
    assert (
        "CLAUDE_HEADLESS_ENABLED is unset: consumer-OAuth headless Claude CI "
        "(the hosted Claude Code Review job and the @claude responder) is retired under D-ToS-1"
        in normalized_entry
    )
    assert "A skipped headless job is expected, never a failure." in normalized_entry
    assert (
        "required_approving_review_count=0; strict mode; enforce_admins=true; "
        "required_conversation_resolution=true; "
        "force-push/ deletion false;" in normalized_entry
    )
    assert (
        "The merge bar remains CI, codecov/patch, CodeQL handling, GitHub Codex's "
        "exact-current-head review and wait, required_conversation_resolution, independent triage, "
        "and risk-routed authenticated local Claude assurance." in normalized_entry
    )
    assert normalized_entry == _REGISTRY_POLICY_SNAPSHOT
    assert not _is_affirmative_enablement_instruction(_operative_text(text, spans))
    for phrase in (
        _DTOS1_COMMIT_SHA,
        "PR #923",
        "requires zero approving reviews",
        "strict mode",
        "enforce_admins=true",
        "required_conversation_resolution=true",
        "skipped headless job is expected",
        "D108-D118 PR-scoped Claude approval bridge",
        "operator-owned branch-protection decision",
        "exact-current-head review",
        "and wait",
        "CodeQL handling",
        "independent triage",
        "risk-routed authenticated local Claude assurance",
    ):
        assert _occurrences(entry, phrase), f"missing top registry phrase: {phrase!r}"
    for check_name in _REQUIRED_CHECK_NAMES:
        assert _exact_markdown_code_name_pattern(check_name).search(entry), (
            f"missing top registry check name: {check_name!r}"
        )


def _assert_registry_historical_evidence(text: str) -> None:
    """Assert registry forbidden prose and retained legacy anchors stay historical."""
    spans = _historical_spans(text)
    _assert_historical_labels(text, spans)
    for phrase in _FORBIDDEN_PHRASES:
        for match in _occurrences(text, phrase):
            assert _within_any_span(match.start(), match.end(), spans), (
                f"{phrase!r} appears operatively in docs/state/registry.md at {match.start()}"
            )
    assert text.count(_SUPERSESSION_MARKER) >= 2
    historical_text = "".join(text[start:end] for start, end in spans)
    for anchor in (
        "#663 / D108-D118 is ACTIVE (31 Jul)",
        "31 Jul 2026 — CLAUDE PR-SCOPED APPROVAL RESTORED (#663 / D108-D118)",
        "arm the `review-gate` required check (#159 / D58)",
        "mark `review-gate` a REQUIRED status check on `main` to arm it",
        "operator activates the `review-gate` required check (#159 / D58)",
    ):
        assert _occurrences(historical_text, anchor), (
            f"historical evidence anchor vanished: {anchor!r}"
        )


def _remove_first_occurrence(text: str, phrase: str) -> str:
    """Remove the first formatting-tolerant occurrence of ``phrase`` from text."""
    match = _occurrences(text, phrase)[0]
    return text[: match.start()] + text[match.end() :]


def _normalized_visible(text: str) -> str:
    """Normalize finite Markdown presentation differences for scoped clauses."""
    return re.sub(r"\s+", " ", re.sub(r"[`*>]", "", text)).strip()


_CANONICAL_POLICY_SNAPSHOT = _normalized_visible(
    """**Verified live state — 6 Sep 2026 (D-ToS-1).** `main` currently requires
    **zero** approving reviews. Required, app-pinned: `Checks`, `Web (lint +
    typecheck + unit)`, `Web (Playwright snapshots)`, and `codecov/patch`;
    strict mode, `required_conversation_resolution`, and `enforce_admins`
    remain enabled; force-push/deletion off. Consumer-OAuth headless Claude CI
    is retired (`CLAUDE_HEADLESS_ENABLED` unset; a skipped headless job is
    expected, never a failure, and never a reason to set the variable). The
    merge bar instead rests on CI, `codecov/patch`, CodeQL handling, GitHub
    Codex's exact-current-head review and wait, `required_conversation_resolution`,
    independent triage, and risk-routed authenticated local Claude assurance
    (below)."""
)
_REGISTRY_POLICY_SNAPSHOT = _normalized_visible(
    """**6 Sep 2026 — D-ToS-1 governance reconciliation (#938).** Verified live
    `main` branch protection: `required_approving_review_count=0`; strict mode;
    `enforce_admins=true`; `required_conversation_resolution=true`; force-push/
    deletion `false`; app-pinned required checks `Checks`, `Web (lint + typecheck
    + unit)`, `Web (Playwright snapshots)`, and `codecov/patch`.
    `CLAUDE_HEADLESS_ENABLED` is unset: consumer-OAuth headless Claude CI (the
    hosted `Claude Code Review` job and the `@claude` responder) is retired under
    D-ToS-1, mirroring roastpilot-cloud #411, merged in agent commit
    `299fb5a93d09e54f58d370f7a35e5ce15f278150` (PR #923). A skipped headless job
    is expected, never a failure. The D108-D118 PR-scoped Claude approval bridge
    mechanism is retained in the codebase but **dormant**: it gates nothing while
    `main` requires zero approving reviews. Restoring it is an operator-owned
    branch-protection decision, not automatic. The merge bar remains CI,
    `codecov/patch`, CodeQL handling, GitHub Codex's exact-current-head review
    and wait, `required_conversation_resolution`, independent triage, and
    risk-routed authenticated local Claude assurance. This entry supersedes the
    operative approval wording in the "31 Jul 2026 — CLAUDE PR-SCOPED APPROVAL
    RESTORED (#663 / D108-D118)" record, the earlier 31-Jul "#663 / D108-D118"
    status summary, and the residual June instructions to arm or require the
    SHA-scoped `review-gate`, all now marked historical. D108-D118 already retired
    that SHA-scoped mechanism; D-ToS-1 does not first retire it and it must not be
    restored."""
)


def _operative_text(text: str, spans: list[tuple[int, int]]) -> str:
    """Return the non-historical portions of a well-formed governed document."""
    parts: list[str] = []
    previous_end = 0
    for start, end in spans:
        parts.append(text[previous_end:start])
        previous_end = end
    parts.append(text[previous_end:])
    return "".join(parts)


def _assert_historical_labels(text: str, spans: list[tuple[int, int]]) -> None:
    """Assert every historical span starts with a visible dated D-ToS-1 label."""
    for start, end in _historical_label_ranges(text, spans):
        label = _normalized_visible(text[start:end]).replace("**", "")
        assert label in _HISTORICAL_LABELS, (
            "historical span lacks a complete visible dated D-ToS-1 label"
        )


def _historical_label_ranges(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return each complete first visible historical-label range for mutations."""
    ranges: list[tuple[int, int]] = []
    for start, end in spans:
        cursor = text.index("\n", start) + 1
        while cursor < end:
            line_end = text.find("\n", cursor, end)
            if line_end == -1:
                line_end = end
            if text[cursor:line_end].strip():
                first_visible = text[cursor:line_end].strip().lstrip(">").strip()
                assert first_visible.startswith("**["), (
                    "historical span lacks a complete visible dated D-ToS-1 label"
                )
                closing = text.find("]**", cursor, end)
                assert closing != -1, "historical span lacks a complete visible dated D-ToS-1 label"
                label_end = closing + len("]**")
                ranges.append((cursor, label_end))
                break
            cursor = line_end + 1
    return ranges


def _assert_responder_headless_gate(workflow: object) -> None:
    """Assert parsed responder workflow data retains the true-only headless gate."""
    assert isinstance(workflow, dict)
    workflow_map = cast(dict[str, object], workflow)
    jobs = workflow_map.get("jobs")
    assert isinstance(jobs, dict)
    jobs_map = cast(dict[str, object], jobs)
    claude_job = jobs_map.get("claude")
    assert isinstance(claude_job, dict)
    claude_map = cast(dict[str, object], claude_job)
    condition = claude_map.get("if")
    assert isinstance(condition, str)
    assert re.sub(r"\s+", " ", condition) == _RESPONDER_CONDITION


def test_agents_md_forbidden_phrases_are_contained_in_historical_blocks() -> None:
    """T1 (AC1, AC2): every phrase-set-P occurrence in AGENTS.md is historical.

    A phrase in `_FORBIDDEN_PHRASES` asserts, in the present tense, that a
    GitHub Claude approval currently gates merge. Under the verified 6 Sep
    2026 state that is false, so every occurrence must sit strictly inside a
    ``historical-evidence`` block, never in operative text.
    """
    text = _read_agents_md()
    _assert_agents_historical_evidence(text)


def test_agents_md_canonical_live_state_bullet_retains_every_gate() -> None:
    """T2 (AC3): the canonical live-state bullet still requires every retained gate.

    This is the G3 fail-open guard, scoped precisely to the one operative
    statement of the current merge bar (the "Verified live state — 6 Sep
    2026 (D-ToS-1)" bullet, up to its historical-evidence begin marker): CI,
    the four exact app-pinned check names (`Checks`,
    `Web (lint + typecheck + unit)`, `Web (Playwright snapshots)`,
    `codecov/patch`), CodeQL, Codex's exact-current-head review AND wait,
    `required_conversation_resolution`, independent triage, risk-routed
    authenticated local Claude assurance, strict mode, admin enforcement,
    and zero approving reviews must all remain stated there as current
    policy. Scoping to this one bullet (rather than the whole document)
    means an unrelated mention elsewhere can never mask a real regression in
    the canonical statement itself.
    """
    text = _read_agents_md()
    _assert_canonical_live_policy(text)


def test_agents_md_headless_skip_is_documented_and_never_instructed_on() -> None:
    """T3 (AC2, AC3): the headless skip is explained; the var is never told to be set.

    Also verifies the E8 precedence subsection names both governed files as
    the operative authorities for the GitHub-Claude required-approval
    description under D-ToS-1.
    """
    text = _read_agents_md()
    spans = _historical_spans(text)

    assert _present_operatively(text, "skipped headless job is expected", spans)

    operative_parts: list[str] = []
    previous_end = 0
    for span_start, span_end in spans:
        operative_parts.append(text[previous_end:span_start])
        previous_end = span_end
    operative_parts.append(text[previous_end:])
    operative = "".join(operative_parts)
    assert not _is_affirmative_enablement_instruction(operative)

    _assert_precedence_policy(text)


@pytest.mark.docs
def test_registry_top_entry_and_legacy_blocks_are_reconciled() -> None:
    """T4 (AC1, AC4): the registry's top entry and legacy blocks are reconciled.

    The first dated ``## Active Epic`` entry must be the 6 Sep 2026 D-ToS-1
    record citing the exact merged commit; both superseded legacy blocks must
    carry a dated supersession marker; and every phrase-set-P occurrence in
    the file must sit inside a historical block, exactly as for AGENTS.md.
    """
    registry_path = _REPO_ROOT / "docs" / "state" / "registry.md"
    text = registry_path.read_text(encoding="utf-8")
    _assert_registry_policy(text)
    _assert_registry_historical_evidence(text)


def test_historical_span_parser_fails_closed_on_malformed_markers() -> None:
    """T6 (AC1, AC4): the delimiter parser rejects every malformed synthetic input.

    Each case must raise, never silently degrade to "no historical blocks
    found" -- that would let a forbidden phrase read as historical while
    actually remaining unguarded.
    """
    unclosed_begin = "before\n<!-- historical-evidence: begin -->\nmiddle\n"
    with pytest.raises(ValueError, match="unclosed"):
        _historical_spans(unclosed_begin)

    orphan_end = "before\n<!-- historical-evidence: end -->\nafter\n"
    with pytest.raises(ValueError, match="orphan"):
        _historical_spans(orphan_end)

    nested_begin = (
        "<!-- historical-evidence: begin -->\n"
        "<!-- historical-evidence: begin -->\n"
        "<!-- historical-evidence: end -->\n"
        "<!-- historical-evidence: end -->\n"
    )
    with pytest.raises(ValueError, match="nested"):
        _historical_spans(nested_begin)

    reversed_order = "<!-- historical-evidence: end -->\n<!-- historical-evidence: begin -->\n"
    with pytest.raises(ValueError, match="orphan"):
        _historical_spans(reversed_order)

    well_formed = (
        "a\n<!-- historical-evidence: begin -->\nb\n<!-- historical-evidence: end -->\nc\n"
    )
    assert len(_historical_spans(well_formed)) == 1


def test_enablement_detector_distinguishes_prohibitions_from_instructions() -> None:
    """T3 regression: explicit prohibitions stay allowed; enablement does not."""
    assert not _is_affirmative_enablement_instruction(
        "Do not set `CLAUDE_HEADLESS_ENABLED` to true."
    )
    assert not _is_affirmative_enablement_instruction(
        "Never set **CLAUDE_HEADLESS_ENABLED**\n> to **true**."
    )
    assert _is_affirmative_enablement_instruction("Set **CLAUDE_HEADLESS_ENABLED**\n> to `true`.")
    assert _is_affirmative_enablement_instruction("CLAUDE_HEADLESS_ENABLED=true")
    assert _is_affirmative_enablement_instruction("export CLAUDE_HEADLESS_ENABLED='true'")
    assert _is_affirmative_enablement_instruction('Set CLAUDE_HEADLESS_ENABLED = "true"')
    assert not _is_affirmative_enablement_instruction("Do not export CLAUDE_HEADLESS_ENABLED=true")
    assert not _is_affirmative_enablement_instruction("Never set CLAUDE_HEADLESS_ENABLED = 'true'")
    assert not _is_affirmative_enablement_instruction("CLAUDE_HEADLESS_ENABLED=false")
    assert not _is_affirmative_enablement_instruction("OTHER_HEADLESS_ENABLED=true")


def test_responder_workflow_requires_the_true_only_headless_conjunct() -> None:
    """T5 responder witness: parsed data and in-memory mutations share one guard."""
    responder_path = _REPO_ROOT / ".github" / "workflows" / "claude.yml"
    workflow = yaml.safe_load(responder_path.read_text(encoding="utf-8"))
    _assert_responder_headless_gate(workflow)

    assert isinstance(workflow, dict)
    workflow_map = cast(dict[str, object], workflow)
    jobs = workflow_map["jobs"]
    assert isinstance(jobs, dict)
    jobs_map = cast(dict[str, object], jobs)
    claude_job = jobs_map["claude"]
    assert isinstance(claude_job, dict)
    claude_map = cast(dict[str, object], claude_job)
    condition = claude_map["if"]
    assert isinstance(condition, str)
    for mutated_condition in (
        condition.replace("== 'true'", "!= 'true'", 1),
        condition.replace("vars.CLAUDE_HEADLESS_ENABLED == 'true' && ", "", 1),
        condition.replace("== 'true' &&", "== 'true' ||", 1),
        condition.replace(" }}", " || true }}", 1),
    ):
        with pytest.raises(AssertionError):
            _assert_responder_headless_gate({"jobs": {"claude": {"if": mutated_condition}}})


@pytest.mark.docs
def test_synthetic_regressions_fail_closed_for_the_other_governance_guards() -> None:
    """Exercise focused synthetic failure modes without mutating governed files.

    These cases cover a generic lower-case check mention, an historical-wrapped
    canonical bullet, each retained registry clause, missing precedence names,
    a missing AGENTS historical anchor, and a June directive outside a span.
    """
    agents = _read_agents_md()
    registry = (_REPO_ROOT / "docs" / "state" / "registry.md").read_text(encoding="utf-8")
    start, end = _canonical_bullet_bounds(agents)
    generic_checks = (
        agents[:start] + agents[start:end].replace("`Checks`", "required checks", 1) + agents[end:]
    )
    with pytest.raises(AssertionError):
        _assert_canonical_live_policy(generic_checks)

    for original, replacement in (
        ("`Checks`", "`Checks` (optional)"),
        ("app-pinned", "not app-pinned"),
        ("**zero** approving reviews", "**not zero** approving reviews"),
    ):
        with pytest.raises(AssertionError):
            _assert_canonical_live_policy(
                agents[:start] + agents[start:end].replace(original, replacement, 1) + agents[end:]
            )

    missing_headless_token = (
        agents[:start]
        + _remove_first_occurrence(agents[start:end], "CLAUDE_HEADLESS_ENABLED")
        + agents[end:]
    )
    with pytest.raises(AssertionError, match="missing canonical headless token"):
        _assert_canonical_live_policy(missing_headless_token)

    lower_case_headless_token = (
        agents[:start]
        + agents[start:end].replace("CLAUDE_HEADLESS_ENABLED", "claude_headless_enabled", 1)
        + agents[end:]
    )
    with pytest.raises(AssertionError, match="missing canonical headless token"):
        _assert_canonical_live_policy(lower_case_headless_token)

    bullet_line_start = agents.rfind("\n", 0, start) + 1
    wrapped = (
        agents[:bullet_line_start]
        + _BEGIN_MARKER
        + "\n"
        + agents[bullet_line_start:end]
        + _END_MARKER
        + "\n"
        + agents[end:]
    )
    _historical_spans(wrapped)
    with pytest.raises(AssertionError):
        _assert_canonical_live_policy(wrapped)

    for original, replacement in (
        ("strict mode,", "non-strict mode,"),
        ("strict mode,", "not strict mode,"),
        (
            "`required_conversation_resolution`",
            "`required_conversation_resolution` remain disabled;",
        ),
        ("`required_conversation_resolution`", "`required_conversation_resolution`=false"),
        ("`enforce_admins`\n  remain enabled", "`enforce_admins` remain disabled"),
        ("`enforce_admins`\n  remain enabled", "`enforce_admins`=false"),
    ):
        weakened = (
            agents[:start] + agents[start:end].replace(original, replacement, 1) + agents[end:]
        )
        with pytest.raises(AssertionError):
            _assert_canonical_live_policy(weakened)

    for replacement in ("set", "is active"):
        weakened = (
            agents[:start]
            + agents[start:end].replace(
                "unset" if replacement == "set" else "is retired", replacement, 1
            )
            + agents[end:]
        )
        with pytest.raises(AssertionError):
            _assert_canonical_live_policy(weakened)
    for instruction in (
        "Set CLAUDE_HEADLESS_ENABLED=true",
        "export CLAUDE_HEADLESS_ENABLED='true'",
        "CLAUDE_HEADLESS_ENABLED=true",
    ):
        with pytest.raises(AssertionError):
            _assert_canonical_live_policy(agents + "\n" + instruction)
    with pytest.raises(AssertionError):
        _assert_canonical_live_policy(
            agents[:start]
            + agents[start:end].replace("independent triage", "not independent triage", 1)
            + agents[end:]
        )

    registry_entry_start = registry.index(
        "**6 Sep 2026 — D-ToS-1 governance reconciliation (#938).**"
    )
    registry_entry_end = registry.index("\n**1 Sep 2026", registry_entry_start)
    for original, replacement in (
        ("`Checks`", "`Checks` (optional)"),
        ("app-pinned", "not app-pinned"),
        ("zero approving reviews", "not zero approving reviews"),
        ("**dormant**", "**active**"),
    ):
        with pytest.raises(AssertionError):
            _assert_registry_policy(
                registry[:registry_entry_start]
                + registry[registry_entry_start:registry_entry_end].replace(
                    original, replacement, 1
                )
                + registry[registry_entry_end:]
            )
    for clause in (
        "requires zero approving reviews",
        "skipped headless job is expected",
        "strict mode",
        "enforce_admins=true",
        "required_conversation_resolution=true",
        "CodeQL handling",
        "exact-current-head review",
        "and wait",
        "independent triage",
        "risk-routed authenticated local Claude assurance",
    ):
        missing_clause = (
            registry[:registry_entry_start]
            + _remove_first_occurrence(registry[registry_entry_start:registry_entry_end], clause)
            + registry[registry_entry_end:]
        )
        with pytest.raises(AssertionError):
            _assert_registry_policy(missing_clause)

    for original, replacement in (
        ("strict mode;", "non-strict mode;"),
        ("strict mode;", "not strict mode;"),
        ("enforce_admins=true", "enforce_admins=false"),
        ("enforce_admins=true", "not enforce_admins=true"),
        ("required_conversation_resolution=true", "required_conversation_resolution=false"),
        ("required_conversation_resolution=true", "not required_conversation_resolution=true"),
        ("is unset", "is set"),
        ("is retired", "is active"),
    ):
        weakened = (
            registry[:registry_entry_start]
            + registry[registry_entry_start:registry_entry_end].replace(original, replacement, 1)
            + registry[registry_entry_end:]
        )
        with pytest.raises(AssertionError):
            _assert_registry_policy(weakened)
    for instruction in (
        "Set CLAUDE_HEADLESS_ENABLED=true",
        "export CLAUDE_HEADLESS_ENABLED='true'",
        "CLAUDE_HEADLESS_ENABLED=true",
    ):
        with pytest.raises(AssertionError):
            _assert_registry_policy(registry + "\n" + instruction)
    with pytest.raises(AssertionError):
        _assert_registry_policy(
            registry[:registry_entry_start]
            + registry[registry_entry_start:registry_entry_end].replace(
                "independent triage", "not independent triage", 1
            )
            + registry[registry_entry_end:]
        )

    precedence_start = agents.index(
        "- **Precedence for the GitHub-Claude required-approval description under"
    )
    precedence_end = agents.index("\n- Mechanism retained", precedence_start)
    for filename in ("AGENTS.md", "docs/state/registry.md"):
        missing_authority = (
            agents[:precedence_start]
            + agents[precedence_start:precedence_end].replace(filename, "", 1)
            + agents[precedence_end:]
        )
        with pytest.raises(AssertionError, match="missing precedence authority"):
            _assert_precedence_policy(missing_authority)

    for anchor in (
        "PR-scoped Claude restoration (#663 / D108-D118",
        "Activation state — LIVE 31 Jul 2026 (#663 / D108-D118)",
        "WAIT for Claude's PR-scoped approval before merging",
        "D108-D118 are active as of 31 Jul 2026",
    ):
        with pytest.raises(AssertionError, match="missing AGENTS history"):
            _assert_agents_historical_evidence(agents.replace(anchor, "", 1))

    for directive in (
        "arm the `review-gate` required check (#159 / D58)",
        "mark `review-gate` a REQUIRED status check on `main` to arm it",
        "operator activates the `review-gate` required check (#159 / D58)",
    ):
        with pytest.raises(AssertionError, match="historical evidence anchor vanished"):
            _assert_registry_historical_evidence(
                _remove_first_occurrence(registry, directive) + "\n" + directive
            )

    for document, assertion in (
        (agents, _assert_agents_historical_evidence),
        (registry, _assert_registry_historical_evidence),
    ):
        for label_start, label_end in _historical_label_ranges(
            document, _historical_spans(document)
        ):
            with pytest.raises(AssertionError, match="complete visible dated D-ToS-1 label"):
                assertion(document[:label_start] + document[label_end:])
            hidden_label = "<!-- " + document[label_start:label_end] + " -->"
            with pytest.raises(AssertionError, match="complete visible dated D-ToS-1 label"):
                assertion(document[:label_start] + hidden_label + document[label_end:])
            with pytest.raises(AssertionError, match="complete visible dated D-ToS-1 label"):
                assertion(document[: label_end - len("]**")] + document[label_end:])
            with pytest.raises(AssertionError, match="complete visible dated D-ToS-1 label"):
                assertion(
                    document[:label_start]
                    + document[label_start:label_end].replace("6 Sep 2026", "6 September", 1)
                    + document[label_end:]
                )
            with pytest.raises(AssertionError, match="complete visible dated D-ToS-1 label"):
                assertion(
                    document[:label_start]
                    + "```text\n"
                    + document[label_start:label_end]
                    + "\n```"
                    + document[label_end:]
                )
            with pytest.raises(AssertionError, match="complete visible dated D-ToS-1 label"):
                assertion(
                    document[:label_start]
                    + "```\n"
                    + document[label_start:label_end]
                    + "\n```"
                    + document[label_end:]
                )
            with pytest.raises(AssertionError, match="complete visible dated D-ToS-1 label"):
                bold_start = document.index("**", label_start, label_end)
                assertion(document[:bold_start] + document[bold_start + 2 :])
