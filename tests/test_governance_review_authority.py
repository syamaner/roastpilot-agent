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

import hashlib
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
_HISTORICAL_SPAN_HASHES = {
    "AGENTS.md": (
        "9c42dbe8b2d6545184500c6df2311c4ed2e2c29baf45c1213f670065eeee161f",
        "77036173b179ade3a6d6d13be44ae9124621393168f096a3e036e1045e6f51b4",
        "e9f3e355d73996180c21a363fe6c3bf2f0e97309352155103dc6a21d10c3a734",
        "2068d33a30d8cf20a6489f65464f653a965fdeccc8ad3251d1f170df3bf61c11",
    ),
    "docs/state/registry.md": (
        "563be152854b3ebcdce0b142fef714c963522680538ccc769407aa050fa1405e",
        "56c29c9419f0a820986b589c491a5b284a547472f62e53df861a93c3f63ba1a0",
        "4ac05ceba7ebf886296e5de878ce86a97f9a82d8a424e2114728e8787ac9764b",
        "116690f3909c7ed2e2bb1d8b0f3d22b7e5ffe5eebf58af25eb743225e71d584c",
        "e077d2f7a0751235a0fa0b3566f5209f9d95f1acc6a5492698a905daa76969da",
        "54b4487109564e2063ed9f275a69064943cb2042dba95dafec92ba42d331b7f2",
        "479c64d91b64ea090b7d9d8669906c0f093b7eaf544ee7025d910ec821186043",
        "20c123164558d99a78a5be81fc3ba31a4ee77c0f9a7fdb2b0837bd5c33629256",
    ),
}


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
    assignment forms that assign this exact variable to true. Its finite
    covered producer set is those plain assignment/export forms and literal
    ``gh variable set CLAUDE_HEADLESS_ENABLED`` commands; it is not a general
    shell or natural-language parser.
    """
    markdown_clean = re.sub(r"[`*>]", "", text)
    normalized = re.sub(r"\s+", " ", markdown_clean)
    assignment = re.compile(
        r"(?P<prohibition>\b(?:do not|never)\s+)?"
        r"(?:(?:set|export)\s+)?\bCLAUDE_HEADLESS_ENABLED\b\s*"
        r"(?:=\s*|to\s+)['\"]?true['\"]?\b",
        re.IGNORECASE,
    )
    gh_variable_set = re.compile(
        r"(?P<prohibition>\b(?:do not|never)\s+)?gh\s+variable\s+set\b",
        re.IGNORECASE,
    )
    explicit_false_body = re.compile(
        r"^\s+(?:-b|--body)(?:\s+|=)['\"]?false['\"]?[.!?]?\s*$",
        re.IGNORECASE,
    )
    has_assignment = any(
        match.group("prohibition") is None for match in assignment.finditer(normalized)
    )
    gh_scan = re.sub(r"\\[ \t]*\r?\n[ \t]*", " ", markdown_clean)
    gh_matches = list(gh_variable_set.finditer(gh_scan))
    has_gh_enablement = False
    for index, match in enumerate(gh_matches):
        line_end = gh_scan.find("\n", match.end())
        if line_end == -1:
            line_end = len(gh_scan)
        command_separator = re.search(r";|&&|\|\|", gh_scan[match.end() : line_end])
        if command_separator is not None:
            line_end = match.end() + command_separator.start()
        next_command_start = (
            gh_matches[index + 1].start() if index + 1 < len(gh_matches) else line_end
        )
        command_segment = gh_scan[match.end() : min(line_end, next_command_start)]
        target = re.search(r"\bCLAUDE_HEADLESS_ENABLED\b", command_segment)
        if target is None:
            continue
        tail = command_segment[target.end() :]
        if match.group("prohibition") is None and explicit_false_body.fullmatch(tail) is None:
            has_gh_enablement = True
            break
    return has_assignment or has_gh_enablement


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
    _assert_historical_span_hashes(text, "AGENTS.md")


def _assert_canonical_live_policy(text: str) -> None:
    """Assert the unique canonical AGENTS policy is entirely operative and complete."""
    spans = _historical_spans(text)
    _assert_no_nonzero_approval_requirement(text, spans)
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
    for filename in ("AGENTS.md", "docs/state/registry.md"):
        assert filename in precedence, f"missing precedence authority: {filename!r}"
    assert _normalized_visible(precedence) == _PRECEDENCE_POLICY_SNAPSHOT


def _assert_no_nonzero_approval_requirement(text: str, spans: list[tuple[int, int]]) -> None:
    """Reject approving-review count requirements except the explicit zero/no forms."""
    operative = re.sub(r"\s+", " ", re.sub(r"\*", "", _operative_text(text, spans)))
    nonzero_requirement = re.compile(
        r"(?<!no longer )(?<!not )\brequire(?:s|d|ing)?\s+"
        r"(?!(?:zero|no)\s+approving\s+reviews?\b)"
        r"(?:at\s+least\s+)?[a-z0-9-]+\s+approving\s+reviews?\b",
        re.IGNORECASE,
    )
    machine_nonzero_requirement = re.compile(
        r"\brequired_approving_review_count\b\s*(?:=|:)\s*[1-9][0-9]*\b",
        re.IGNORECASE,
    )
    assert not nonzero_requirement.search(operative)
    assert not machine_nonzero_requirement.search(operative.replace("`", ""))


def _assert_branch_protection_policy(text: str) -> None:
    """Assert the one earlier current branch-protection bullet is byte-scoped in meaning."""
    start = text.index("- **`main` is branch-protected")
    end = text.index("\n- **Precedence for the GitHub-Claude", start)
    assert text.count("- **`main` is branch-protected") == 1
    assert _normalized_visible(text[start:end]) == _BRANCH_PROTECTION_SNAPSHOT


def _assert_review_roster_policy(text: str) -> None:
    """Assert the complete operative Code Review Rubric roster remains live."""
    start = text.index("**The PR review roster (verified live 6 Sep 2026, D-ToS-1)")
    end = text.index("\n<!-- historical-evidence: begin -->", start)
    assert text.count("**The PR review roster (verified live 6 Sep 2026, D-ToS-1)") == 1
    assert _normalized_visible(text[start:end]) == _REVIEW_ROSTER_SNAPSHOT


def _assert_minimum_sufficient_review_policy(text: str) -> None:
    """Assert the operative local-review gate list excludes retired Claude approval."""
    start = text.index("### Minimum sufficient local review")
    paragraph_end = text.index("\n\n- Ordinary slice:", start)
    paragraph = text[start:paragraph_end]
    assert _normalized_visible(paragraph) == _MINIMUM_SUFFICIENT_REVIEW_SNAPSHOT
    assert not _occurrences(paragraph, "GitHub Claude exact-head approval")
    assert not _occurrences(paragraph, "Claude exact-head approval")


def _assert_draft_phase_policy(text: str) -> None:
    """Assert draft opening triggers a skipped headless job, not findings to fold."""
    start = text.index("**Draft phase vs ready phase")
    end = text.index("\n\n**WAIT for Codex's verdict before merging", start)
    paragraph = text[start:end]
    assert _normalized_visible(paragraph) == _DRAFT_PHASE_SNAPSHOT
    assert not _occurrences(paragraph, "findings there are real and worth folding")


def _assert_retained_mechanism_policy(text: str) -> None:
    """Assert the operative retained-mechanism bullet remains dormant and operator-owned."""
    spans = _historical_spans(text)
    start = text.index("- Mechanism retained but")
    end = text.index("\n  <!-- historical-evidence: begin -->", start)
    assert not any(span_start < end and start < span_end for span_start, span_end in spans)
    mechanism = text[start:end]
    assert _occurrences(mechanism, "dormant")
    assert _occurrences(mechanism, "gates nothing today")
    assert _occurrences(mechanism, "operator-owned branch-protection decision")
    assert not _occurrences(mechanism, "not dormant")
    assert not _occurrences(mechanism, "not operator-owned branch-protection decision")


def _assert_preserved_wait_policy(text: str) -> None:
    """Assert the operative preservation sentence keeps the wait non-gating."""
    spans = _historical_spans(text)
    heading = "Preserved as the D108-D118 design/evidence record:"
    assert text.count(heading) == 1
    start = text.index(heading)
    end = text.index("\n\n<!-- historical-evidence: begin -->", start)
    assert not any(span_start < end and start < span_end for span_start, span_end in spans)
    preserved = text[start:end]
    assert _occurrences(preserved, "this wait does not gate merge")
    assert _occurrences(preserved, "current verified state")


def _assert_claude_review_note_policy(text: str) -> None:
    """Assert the operative Claude-review note documents both headless skips."""
    spans = _historical_spans(text)
    heading = "> Note: `claude-review` is intentionally"
    assert text.count(heading) == 1
    start = text.index(heading)
    end = text.index("\n\n## Codex-Led Delivery Topology", start)
    assert not any(span_start < end and start < span_end for span_start, span_end in spans)
    note = text[start:end]
    normalized_note = _normalized_visible(note)
    assert "not a required status check" in normalized_note
    assert (
        "The real findings-gate is GitHub Codex's inline threads + "
        "required_conversation_resolution" in normalized_note
    )
    for required in (
        ".github/workflows/claude-code-review.yml:27-41",
        ".github/workflows/claude.yml:14-26",
        "a skipped headless job is expected, never a failure, and never a reason "
        "to set the variable or re-trigger",
    ):
        assert _occurrences(note, required), f"missing operative Claude-review note: {required!r}"


def _assert_registry_735_clarification(text: str) -> None:
    """Assert the #735 clarification is operative and preserves D-ToS-1 skip semantics."""
    spans = _historical_spans(text)
    _span_start, span_end = next(
        (start, end)
        for start, end in spans
        if "#735 story-closing implementation complete" in text[start:end]
    )
    clarification_end = text.index("\n\n**11 Aug 2026", span_end)
    clarification = text[span_end:clarification_end]
    assert _normalized_visible(clarification) == _REGISTRY_735_CLARIFICATION_SNAPSHOT
    assert "the review itself still runs unskipped" not in clarification


def _assert_registry_policy(text: str) -> None:
    """Assert the first registry reconciliation entry is operative and complete."""
    spans = _historical_spans(text)
    _assert_no_nonzero_approval_requirement(text, spans)
    _assert_registry_735_clarification(text)
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
        "the review itself still runs unskipped",
        "PR review roster = Claude Code Review (+ human reviewers)",
        "`claude-review` posts **blocking inline** findings (`--comment`)",
    ):
        assert _occurrences(historical_text, anchor), (
            f"historical evidence anchor vanished: {anchor!r}"
        )
    _assert_historical_span_hashes(text, "docs/state/registry.md")


def _remove_first_occurrence(text: str, phrase: str) -> str:
    """Remove the first formatting-tolerant occurrence of ``phrase`` from text."""
    match = _occurrences(text, phrase)[0]
    return text[: match.start()] + text[match.end() :]


def _normalized_visible(text: str) -> str:
    """Normalize finite Markdown presentation differences for scoped clauses."""
    without_blockquotes = re.sub(r"(?m)^[ \t]*>[ \t]?", "", text)
    return re.sub(r"\s+", " ", re.sub(r"[`*]", "", without_blockquotes)).strip()


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
    D-ToS-1, merged in agent commit
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
    SHA-scoped `review-gate`, plus the 17-Aug #735 statement that the headless
    review itself still runs unskipped, all now marked historical. D108-D118
    already retired that SHA-scoped mechanism; D-ToS-1 is unrelated to that
    retirement; it must not be restored."""
)
_PRECEDENCE_POLICY_SNAPSHOT = _normalized_visible(
    """- **Precedence for the GitHub-Claude required-approval description under
    D-ToS-1 only.** `AGENTS.md` plus `docs/state/registry.md` are the sole
    operative authorities for whether a GitHub-hosted Claude review currently
    gates a required approving review on `main`. Where another file's text
    about that one specific fact conflicts with the verified live state
    recorded here (for example `.claude/skills/pr-preflight/SKILL.md` or
    `docs/agent-topology.md`), that other file's conflicting sentence is
    superseded and historical; those files are not edited by this
    reconciliation. This narrow precedence does not extend to any other
    skill, plan, or review rule, and does not override an explicit operator
    instruction."""
)
_BRANCH_PROTECTION_SNAPSHOT = _normalized_visible(
    """- **`main` is branch-protected (13 Jun, enforces this policy at the platform).**
    Required, verified live 6 Sep 2026: app-pinned `Checks`, `Web (lint +
    typecheck + unit)`, `Web (Playwright snapshots)`, and `codecov/patch`;
    strict mode; `required_conversation_resolution` (every review thread
    resolved); and `enforce_admins` (no bypass for owner or agents);
    force-push/deletion off; repo auto-merge on. **`main` currently requires
    zero approving reviews** — consumer-OAuth headless Claude CI is retired
    under D-ToS-1 (`CLAUDE_HEADLESS_ENABLED` unset); see the precedence
    subsection below for the operative merge-bar description and the
    retained-but-dormant D108-D118 mechanism. **`claude-review` is
    intentionally NOT a required check** — it fails by design on PRs that edit a
    workflow file (the App's workflow-validation guard) and on Dependabot PRs (no
    secrets), and it passes-on-findings; so the findings gate is GitHub Codex's
    inline comments + conversation-resolution, not the check itself. Don't
    re-add it as required (it would deadlock workflow PRs). Green CI alone never
    means mergeable."""
)
_REVIEW_ROSTER_SNAPSHOT = _normalized_visible(
    """**The PR review roster (verified live 6 Sep 2026, D-ToS-1): GitHub `Codex`
    (exact-current-head review + wait, advisory-but-triaged), risk-routed
    authenticated local Claude assurance (`safety-reviewer`, `security-reviewer`,
    `qa`, etc., per the routing table below), independent triage, and any human
    reviewer.** The hosted headless **Claude Code Review**
    (`.github/workflows/claude-code-review.yml`, running `/code-review --comment`)
    job is retired/dormant: it SKIPS while `CLAUDE_HEADLESS_ENABLED` is unset, so
    it is no longer a live roster member. **CodeRabbit stays disabled (15 Jun) and
    the Augment Code trial ENDED (28 Jun).**"""
)
_MINIMUM_SUFFICIENT_REVIEW_SNAPSHOT = _normalized_visible(
    """### Minimum sufficient local review

    This routing changes only additional local/pre-open model review. Ready-head
    Codex review and wait, branch protection, conversation resolution, CI, CodeQL
    handling, and `codecov/patch` rules above remain unchanged."""
)
_DRAFT_PHASE_SNAPSHOT = _normalized_visible(
    """**Draft phase vs ready phase (the shift-left reconciliation, D103-adjacent; corrected 27
    Jul 2026).** The once-on-final-commit rule above governs the **post-ready** phase: a
    marked-ready PR heading to merge, where re-triggering across pushes is re-litigation
    churn. Codex itself does not review a PR while it sits in **draft**: GitHub's Codex
    connector fires automatically only at the ready transition (opened ready, or a draft
    marked ready), confirmed against roastpilot-cloud PR #150 (27 Jul 2026), where Codex
    produced no review during the draft phase and then reviewed automatically the moment the
    PR was marked ready. Under the D158 pilot, the pre-ready fold is the
    minimum-sufficient independent review selected by the Codex parent and run locally under
    `pr-preflight`; it is not the former fixed local-Codex step. A GitHub-side `@codex review`
    comment left on a draft is a different thing again, and is NOT simply inert: D105 observed
    on 19 Jul 2026 that it does run and does post findings-reviews, but never completes the
    clean-verdict flow on a draft, so a draft waiting on a clean signal waits forever. Both facts
    hold, because they describe different mechanisms: the automatic trigger does not fire until
    ready, while an explicit comment runs but cannot produce a clean verdict there. Neither makes
    the draft a place to converge to clean. Draft = fold locally and let the runner gates
    (`ci.yml`, tests, `ruff`, `pyright`) run; ready = the automatic Codex review fires on that
    head, then once-on-final-commit applies to any later push. Opening a draft still triggers the
    `claude-review` workflow's `on: opened` event, but under D-ToS-1 the job itself SKIPS while
    `CLAUDE_HEADLESS_ENABLED` is unset — it is not a required check, and a skipped run posts no
    findings to fold. Unlike Codex, this job is not draft-suppressed by GitHub; it is gated off by
    the headless retirement instead."""
)
_REGISTRY_735_CLARIFICATION_SNAPSHOT = _normalized_visible(
    """The trigger remains configured, but under D-ToS-1 its headless job skips while
    `CLAUDE_HEADLESS_ENABLED` is unset; the retained `track_progress` implementation
    above is not undone."""
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


def _assert_historical_span_hashes(text: str, filename: str) -> None:
    """Assert every retained historical span remains byte-exact and ordered."""
    spans = _historical_spans(text)
    observed = tuple(hashlib.sha256(text[start:end].encode()).hexdigest() for start, end in spans)
    assert observed == _HISTORICAL_SPAN_HASHES[filename]


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

    _assert_branch_protection_policy(text)
    _assert_review_roster_policy(text)
    _assert_minimum_sufficient_review_policy(text)
    _assert_draft_phase_policy(text)
    _assert_retained_mechanism_policy(text)
    _assert_preserved_wait_policy(text)
    _assert_claude_review_note_policy(text)
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
    assert _is_affirmative_enablement_instruction(
        "gh variable set CLAUDE_HEADLESS_ENABLED --body true"
    )
    assert _is_affirmative_enablement_instruction(
        "gh variable set " + "\\\n" + "CLAUDE_HEADLESS_ENABLED --body true"
    )
    assert _is_affirmative_enablement_instruction(
        'gh variable set CLAUDE_HEADLESS_ENABLED --body="true"'
    )
    assert _is_affirmative_enablement_instruction(
        "gh variable set CLAUDE_HEADLESS_ENABLED -b='true'"
    )
    assert _is_affirmative_enablement_instruction("gh variable set CLAUDE_HEADLESS_ENABLED -b true")
    assert _is_affirmative_enablement_instruction("gh variable set CLAUDE_HEADLESS_ENABLED -b=true")
    assert _is_affirmative_enablement_instruction(
        "gh variable set --repo owner/repo CLAUDE_HEADLESS_ENABLED --body true"
    )
    assert _is_affirmative_enablement_instruction(
        "gh variable set -R=owner/repo CLAUDE_HEADLESS_ENABLED -b='true'"
    )
    assert _is_affirmative_enablement_instruction(
        "gh variable set --repo='owner/repo' CLAUDE_HEADLESS_ENABLED --body=true"
    )
    assert _is_affirmative_enablement_instruction(
        'gh variable set -R "owner/repo" CLAUDE_HEADLESS_ENABLED -b true'
    )
    assert _is_affirmative_enablement_instruction("gh variable set CLAUDE_HEADLESS_ENABLED")
    assert _is_affirmative_enablement_instruction(
        "printf true | gh variable set CLAUDE_HEADLESS_ENABLED"
    )
    assert _is_affirmative_enablement_instruction(
        "echo true | gh variable set CLAUDE_HEADLESS_ENABLED"
    )
    assert _is_affirmative_enablement_instruction(
        "gh variable set CLAUDE_HEADLESS_ENABLED < enabled.txt"
    )
    assert _is_affirmative_enablement_instruction("gh variable set CLAUDE_HEADLESS_ENABLED <<<true")
    assert _is_affirmative_enablement_instruction(
        "gh variable set CLAUDE_HEADLESS_ENABLED --body $VALUE"
    )
    assert _is_affirmative_enablement_instruction(
        "gh variable set CLAUDE_HEADLESS_ENABLED -b truth"
    )
    assert not _is_affirmative_enablement_instruction("Do not export CLAUDE_HEADLESS_ENABLED=true")
    assert not _is_affirmative_enablement_instruction("Never set CLAUDE_HEADLESS_ENABLED = 'true'")
    assert not _is_affirmative_enablement_instruction("CLAUDE_HEADLESS_ENABLED=false")
    assert not _is_affirmative_enablement_instruction("OTHER_HEADLESS_ENABLED=true")
    assert not _is_affirmative_enablement_instruction(
        "Do not gh variable set CLAUDE_HEADLESS_ENABLED --body true"
    )
    for false_body in (
        "gh variable set CLAUDE_HEADLESS_ENABLED --body false",
        "gh variable set CLAUDE_HEADLESS_ENABLED --body=false",
        'gh variable set CLAUDE_HEADLESS_ENABLED --body "false"',
        "gh variable set CLAUDE_HEADLESS_ENABLED -b='false'",
        "gh variable set CLAUDE_HEADLESS_ENABLED --body false.",
        "gh variable set --repo=owner/repo CLAUDE_HEADLESS_ENABLED --body 'false'",
        "gh variable set -R owner/repo CLAUDE_HEADLESS_ENABLED -b=false",
    ):
        assert not _is_affirmative_enablement_instruction(false_body)
    assert not _is_affirmative_enablement_instruction(
        "gh variable set OTHER_HEADLESS_ENABLED --body true"
    )
    assert not _is_affirmative_enablement_instruction(
        "gh variable set --repo owner/repo OTHER_HEADLESS_ENABLED --body true"
    )
    assert not _is_affirmative_enablement_instruction(
        "gh variable set\nunconnected prose\nCLAUDE_HEADLESS_ENABLED --body true"
    )
    assert _is_affirmative_enablement_instruction(
        "gh variable set CLAUDE_HEADLESS_ENABLED --body false; "
        "gh variable set CLAUDE_HEADLESS_ENABLED --body true"
    )
    assert _is_affirmative_enablement_instruction(
        "gh variable set CLAUDE_HEADLESS_ENABLED --body false. "
        "gh variable set CLAUDE_HEADLESS_ENABLED --body true"
    )
    assert _is_affirmative_enablement_instruction(
        "Do not gh variable set CLAUDE_HEADLESS_ENABLED --body true; "
        "gh variable set CLAUDE_HEADLESS_ENABLED --body true"
    )
    assert not _is_affirmative_enablement_instruction(
        "Never gh variable set --repo owner/repo CLAUDE_HEADLESS_ENABLED --body true"
    )


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
        "requires two approving reviews",
        "requires **two** approving reviews",
        "requires ten approving reviews",
        "requires 10 approving reviews",
        "requires at least two approving reviews",
        "required_approving_review_count=1",
        "required_approving_review_count = 10",
        "required_approving_review_count: 2",
        "`required_approving_review_count`: 1",
        "gh variable set CLAUDE_HEADLESS_ENABLED --body true",
        "gh variable set " + "\\\n" + "CLAUDE_HEADLESS_ENABLED --body true",
        'gh variable set CLAUDE_HEADLESS_ENABLED --body="true"',
        "gh variable set CLAUDE_HEADLESS_ENABLED -b='true'",
        "gh variable set CLAUDE_HEADLESS_ENABLED -b=true",
        "gh variable set CLAUDE_HEADLESS_ENABLED",
        "printf true | gh variable set CLAUDE_HEADLESS_ENABLED",
        "gh variable set CLAUDE_HEADLESS_ENABLED < enabled.txt",
        "gh variable set CLAUDE_HEADLESS_ENABLED <<<true",
        "gh variable set CLAUDE_HEADLESS_ENABLED --body $VALUE",
        "gh variable set CLAUDE_HEADLESS_ENABLED --body false; "
        "gh variable set CLAUDE_HEADLESS_ENABLED --body true",
        "Do not gh variable set CLAUDE_HEADLESS_ENABLED --body true; "
        "gh variable set CLAUDE_HEADLESS_ENABLED --body true",
        "gh variable set --repo owner/repo CLAUDE_HEADLESS_ENABLED --body true",
        "gh variable set -R=owner/repo CLAUDE_HEADLESS_ENABLED -b='true'",
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
        ("required_approving_review_count=0", "required_approving_review_count>=0"),
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
        "requires two approving reviews",
        "requires **two** approving reviews",
        "requires ten approving reviews",
        "requires 10 approving reviews",
        "requires at least two approving reviews",
        "required_approving_review_count=1",
        "required_approving_review_count = 10",
        "required_approving_review_count: 2",
        "`required_approving_review_count`: 1",
        "gh variable set CLAUDE_HEADLESS_ENABLED --body true",
        "gh variable set " + "\\\n" + "CLAUDE_HEADLESS_ENABLED --body true",
        'gh variable set CLAUDE_HEADLESS_ENABLED --body="true"',
        "gh variable set CLAUDE_HEADLESS_ENABLED -b='true'",
        "gh variable set CLAUDE_HEADLESS_ENABLED -b=true",
        "gh variable set CLAUDE_HEADLESS_ENABLED",
        "echo true | gh variable set CLAUDE_HEADLESS_ENABLED",
        "gh variable set CLAUDE_HEADLESS_ENABLED < enabled.txt",
        "gh variable set CLAUDE_HEADLESS_ENABLED <<<true",
        "gh variable set CLAUDE_HEADLESS_ENABLED --body $VALUE",
        "gh variable set CLAUDE_HEADLESS_ENABLED --body false; "
        "gh variable set CLAUDE_HEADLESS_ENABLED --body true",
        "Do not gh variable set CLAUDE_HEADLESS_ENABLED --body true; "
        "gh variable set CLAUDE_HEADLESS_ENABLED --body true",
        "gh variable set --repo owner/repo CLAUDE_HEADLESS_ENABLED --body true",
        "gh variable set -R=owner/repo CLAUDE_HEADLESS_ENABLED -b='true'",
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

    for document, assertion in (
        (agents, _assert_canonical_live_policy),
        (registry, _assert_registry_policy),
    ):
        for allowed_instruction in (
            "requires no approving reviews",
            "requires **no** approving reviews",
            "required_approving_review_count=0",
            "required_approving_review_count = 0",
            "required_approving_review_count: 0",
            "gh variable set --repo=owner/repo CLAUDE_HEADLESS_ENABLED --body 'false'",
            "Do not gh variable set -R owner/repo CLAUDE_HEADLESS_ENABLED --body true",
            "gh variable set --repo owner/repo OTHER_HEADLESS_ENABLED --body true",
        ):
            assertion(document + "\n" + allowed_instruction)

    historical_nonzero = (
        agents + "\n" + _BEGIN_MARKER + "\nrequires ten approving reviews\n" + _END_MARKER
    )
    _assert_no_nonzero_approval_requirement(
        historical_nonzero, _historical_spans(historical_nonzero)
    )
    for historical_machine_assignment in (
        "required_approving_review_count=1",
        "required_approving_review_count = 1",
        "required_approving_review_count: 1",
    ):
        historical_machine_nonzero = (
            agents
            + "\n"
            + _BEGIN_MARKER
            + "\n"
            + historical_machine_assignment
            + "\n"
            + _END_MARKER
        )
        _assert_no_nonzero_approval_requirement(
            historical_machine_nonzero, _historical_spans(historical_machine_nonzero)
        )
    bold_nonzero = agents + "\nrequires **two** approving reviews"
    with pytest.raises(AssertionError):
        _assert_no_nonzero_approval_requirement(bold_nonzero, _historical_spans(bold_nonzero))
    for machine_nonzero in (
        "required_approving_review_count=1",
        "required_approving_review_count = 10",
        "required_approving_review_count: 2",
        "`required_approving_review_count`: 1",
    ):
        with pytest.raises(AssertionError):
            _assert_no_nonzero_approval_requirement(
                agents + "\n" + machine_nonzero,
                _historical_spans(agents + "\n" + machine_nonzero),
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
    with pytest.raises(AssertionError):
        _assert_precedence_policy(
            agents[:precedence_start]
            + agents[precedence_start:precedence_end].replace(
                "are the sole\n  operative authorities",
                "are not the sole\n  operative authorities",
                1,
            )
            + agents[precedence_end:]
        )

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

    branch_start = agents.index("- **`main` is branch-protected")
    branch_end = agents.index("\n- **Precedence for the GitHub-Claude", branch_start)
    for original, replacement in (
        ("strict mode;", "strict mode disabled;"),
        ("`Checks`", "`Checks` (optional)"),
        ("zero approving reviews", "two approving reviews"),
    ):
        with pytest.raises(AssertionError):
            _assert_branch_protection_policy(
                agents[:branch_start]
                + agents[branch_start:branch_end].replace(original, replacement, 1)
                + agents[branch_end:]
            )
    with pytest.raises(AssertionError):
        _assert_branch_protection_policy(
            agents[:branch_end] + agents[branch_start:branch_end] + agents[branch_end:]
        )

    roster_start = agents.index("**The PR review roster (verified live 6 Sep 2026, D-ToS-1)")
    roster_end = agents.index("\n<!-- historical-evidence: begin -->", roster_start)
    for original, replacement in (
        ("retired/dormant", "live"),
        ("no longer a live roster member", "a live roster member"),
        ("GitHub `Codex`", ""),
        ("authenticated local Claude assurance", ""),
        ("independent triage", ""),
    ):
        with pytest.raises(AssertionError):
            _assert_review_roster_policy(
                agents[:roster_start]
                + agents[roster_start:roster_end].replace(original, replacement, 1)
                + agents[roster_end:]
            )

    review_start = agents.index("### Minimum sufficient local review")
    review_end = agents.index("\n\n- Ordinary slice:", review_start)
    with pytest.raises(AssertionError):
        _assert_minimum_sufficient_review_policy(
            agents[:review_start]
            + agents[review_start:review_end].replace(
                "Ready-head\nCodex review and wait", "GitHub Claude exact-head approval", 1
            )
            + agents[review_end:]
        )

    draft_start = agents.index("**Draft phase vs ready phase")
    draft_end = agents.index("\n\n**WAIT for Codex's verdict before merging", draft_start)
    with pytest.raises(AssertionError):
        _assert_draft_phase_policy(
            agents[:draft_start]
            + agents[draft_start:draft_end].replace(
                "a\nskipped run posts no findings to fold",
                "findings there are real and worth folding",
                1,
            )
            + agents[draft_end:]
        )

    with pytest.raises(AssertionError):
        _assert_registry_735_clarification(
            registry.replace("The trigger remains configured", "", 1)
        )
    with pytest.raises(AssertionError):
        _assert_registry_735_clarification(
            registry.replace(
                "its headless job skips while\n`CLAUDE_HEADLESS_ENABLED` is unset",
                "the review itself still runs unskipped",
                1,
            )
        )

    mechanism_start = agents.index("- Mechanism retained but")
    mechanism_end = agents.index("\n  <!-- historical-evidence: begin -->", mechanism_start)
    for original, replacement in (
        ("dormant", ""),
        ("dormant", "not dormant"),
        ("operator-owned branch-protection decision", ""),
        ("operator-owned branch-protection decision", "not operator-owned"),
        ("gates nothing\n  today", "gates merge today"),
    ):
        with pytest.raises(AssertionError):
            _assert_retained_mechanism_policy(
                agents[:mechanism_start]
                + agents[mechanism_start:mechanism_end].replace(original, replacement, 1)
                + agents[mechanism_end:]
            )

    preserved_start = agents.index("Preserved as the D108-D118 design/evidence record:")
    preserved_end = agents.index("\n\n<!-- historical-evidence: begin -->", preserved_start)
    with pytest.raises(AssertionError):
        _assert_preserved_wait_policy(agents[:preserved_start] + agents[preserved_end:])

    note_start = agents.index("> Note: `claude-review` is intentionally")
    note_end = agents.index("\n\n## Codex-Led Delivery Topology", note_start)
    with pytest.raises(AssertionError):
        _assert_claude_review_note_policy(agents[:note_start] + agents[note_end:])
    for citation in (
        ".github/workflows/claude-code-review.yml:27-41",
        ".github/workflows/claude.yml:14-26",
    ):
        with pytest.raises(AssertionError):
            _assert_claude_review_note_policy(agents.replace(citation, "", 1))
    with pytest.raises(AssertionError):
        _assert_claude_review_note_policy(
            agents.replace("**not** a required status check", "a required status check", 1)
        )

    for document, assertion in (
        (agents, _assert_agents_historical_evidence),
        (registry, _assert_registry_historical_evidence),
    ):
        for _span_start, span_end in _historical_spans(document):
            closing_line_start = document.rfind("\n", 0, span_end - 1) + 1
            with pytest.raises(AssertionError):
                assertion(document[:closing_line_start] + " \n" + document[closing_line_start:])

    wait_start, wait_end = _historical_spans(agents)[-1]
    wait_span = agents[wait_start:wait_end]
    early_close_at = wait_span.index("Opening a normal PR starts Claude")
    early_close_line_start = wait_span.rfind("\n", 0, early_close_at) + 1
    wait_without_original_end = wait_span[: wait_span.rfind(_END_MARKER)]
    shortened_wait = (
        wait_without_original_end[:early_close_line_start]
        + _END_MARKER
        + "\n"
        + wait_without_original_end[early_close_line_start:]
    )
    with pytest.raises(AssertionError):
        _assert_agents_historical_evidence(agents[:wait_start] + shortened_wait + agents[wait_end:])

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
