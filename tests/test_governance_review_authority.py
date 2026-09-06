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
description) is deliberately NOT duplicated here: the mechanism was shipped
by commit ``299fb5a93d09e54f58d370f7a35e5ce15f278150`` and the
``claude-code-review.yml`` reviewer job's exact ``if:`` condition (the
Dependabot-author guard AND the `CLAUDE_HEADLESS_ENABLED` conjunct) is
already asserted, byte-for-byte, by the existing, unchanged
``tests/test_claude_review_approval.py::test_track_progress_disabled_only_for_unsupported_pull_request_actions``
regression test. Re-running that unchanged test is this slice's T5 witness;
adding a second, overlapping assertion of the same guard would be an
unintended duplicate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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
    and ``Never``) while rejecting an affirmative ``Set ... to true``. It
    normalizes emphasis, code delimiters, blockquotes, and line wrapping before
    applying the grammar; it is not a broad natural-language classifier.
    """
    normalized = re.sub(r"[`*>]", "", text)
    normalized = re.sub(r"\s+", " ", normalized)
    instruction = re.compile(
        r"(?P<prohibition>do not|never)?\s*set\s+CLAUDE_HEADLESS_ENABLED\s+to\s+['\"]?true['\"]?",
        re.IGNORECASE,
    )
    return any(match.group("prohibition") is None for match in instruction.finditer(normalized))


def _require_phrase(text: str, phrase: str) -> None:
    """Raise when a formatting-tolerant required phrase is absent."""
    assert _occurrences(text, phrase), f"missing required phrase: {phrase!r}"


def _require_exact_check_name(text: str, check_name: str) -> None:
    """Raise when an exact, code-formatted required check name is absent."""
    assert _exact_markdown_code_name_pattern(check_name).search(text), (
        f"missing exact check name: {check_name!r}"
    )


def _require_range_operative(start: int, end: int, spans: list[tuple[int, int]]) -> None:
    """Raise when a required live-policy range overlaps historical evidence."""
    assert not any(span_start < end and start < span_end for span_start, span_end in spans)


def test_agents_md_forbidden_phrases_are_contained_in_historical_blocks() -> None:
    """T1 (AC1, AC2): every phrase-set-P occurrence in AGENTS.md is historical.

    A phrase in `_FORBIDDEN_PHRASES` asserts, in the present tense, that a
    GitHub Claude approval currently gates merge. Under the verified 6 Sep
    2026 state that is false, so every occurrence must sit strictly inside a
    ``historical-evidence`` block, never in operative text.
    """
    text = _read_agents_md()
    spans = _historical_spans(text)
    for phrase in _FORBIDDEN_PHRASES:
        for match in _occurrences(text, phrase):
            assert _within_any_span(match.start(), match.end(), spans), (
                f"{phrase!r} appears operatively (outside a historical-evidence "
                f"block) in AGENTS.md at character offset {match.start()}"
            )
    historical_text = "".join(text[start:end] for start, end in spans)
    for anchor in (
        "PR-scoped Claude restoration (#663 / D108-D118",
        "Activation state — LIVE 31 Jul 2026 (#663 / D108-D118)",
        "WAIT for Claude's PR-scoped approval before merging",
    ):
        assert _occurrences(historical_text, anchor), (
            f"AGENTS.md historical evidence anchor vanished: {anchor!r}"
        )


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
    spans = _historical_spans(text)
    assert text.count(_CANONICAL_LIVE_STATE_HEADING) == 1
    start, end = _canonical_bullet_bounds(text)
    assert not any(span_start < end and start < span_end for span_start, span_end in spans), (
        "AGENTS.md's canonical live-state bullet must be entirely operative"
    )
    bullet = _canonical_live_state_bullet(text)
    for phrase in _CANONICAL_LIVE_STATE_REQUIRED_PHRASES:
        assert _occurrences(bullet, phrase), (
            f"{phrase!r} is missing from AGENTS.md's canonical "
            f"'Verified live state — 6 Sep 2026 (D-ToS-1)' bullet"
        )
    for check_name in _REQUIRED_CHECK_NAMES:
        assert _exact_markdown_code_name_pattern(check_name).search(bullet), (
            f"exact app-pinned status name {check_name!r} is missing from the "
            "canonical live-state bullet"
        )


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

    precedence_start = text.index(
        "- **Precedence for the GitHub-Claude required-approval description under"
    )
    precedence_end = text.index("\n- Mechanism retained", precedence_start)
    assert not any(
        span_start < precedence_end and precedence_start < span_end
        for span_start, span_end in spans
    ), "the precedence subsection must be entirely operative"
    precedence = text[precedence_start:precedence_end]
    assert "sole\n  operative authorities" in precedence
    assert "AGENTS.md" in precedence
    assert "docs/state/registry.md" in precedence


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
    spans = _historical_spans(text)

    header_index = text.index("## Active Epic")
    entry_heading = "**6 Sep 2026 — D-ToS-1 governance reconciliation (#938).**"
    entry_start = text.index(entry_heading, header_index)
    assert not re.search(r"\n\*\*[^\n]+", text[header_index:entry_start])
    entry_end = text.index("\n**1 Sep 2026", entry_start)
    top_entry = text[entry_start:entry_end]
    overlaps_historical = any(
        span_start < entry_end and entry_start < span_end for span_start, span_end in spans
    )
    assert not overlaps_historical, (
        "the first dated reconciliation entry must be entirely operative"
    )
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
        assert _occurrences(top_entry, phrase), f"top reconciliation entry is missing {phrase!r}"
    for check_name in _REQUIRED_CHECK_NAMES:
        assert _exact_markdown_code_name_pattern(check_name).search(top_entry), (
            f"top reconciliation entry is missing exact check name {check_name!r}"
        )

    assert text.count(_SUPERSESSION_MARKER) >= 2

    for phrase in _FORBIDDEN_PHRASES:
        for match in _occurrences(text, phrase):
            assert _within_any_span(match.start(), match.end(), spans), (
                f"{phrase!r} appears operatively (outside a historical-evidence "
                f"block) in docs/state/registry.md at character offset {match.start()}"
            )

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


@pytest.mark.docs
def test_synthetic_regressions_fail_closed_for_the_other_governance_guards() -> None:
    """Exercise focused synthetic failure modes without mutating governed files.

    These cases cover a generic lower-case check mention, an historical-wrapped
    canonical bullet, each retained registry clause, missing precedence names,
    a missing AGENTS historical anchor, and a June directive outside a span.
    """
    agents = _read_agents_md()
    bullet = _canonical_live_state_bullet(agents)
    generic_checks = bullet.replace("`Checks`", "required checks")
    with pytest.raises(AssertionError, match="missing exact check name"):
        _require_exact_check_name(generic_checks, "Checks")

    wrapped = _BEGIN_MARKER + "\n" + bullet + _END_MARKER + "\n"
    wrapped_spans = _historical_spans(wrapped)
    wrapped_start = wrapped.index(_CANONICAL_LIVE_STATE_HEADING)
    wrapped_end = wrapped.index(_END_MARKER, wrapped_start)
    with pytest.raises(AssertionError):
        _require_range_operative(wrapped_start, wrapped_end, wrapped_spans)

    registry = (_REPO_ROOT / "docs" / "state" / "registry.md").read_text(encoding="utf-8")
    entry_end = registry.index("\n**1 Sep 2026")
    top_entry = registry[:entry_end]
    for clause in (
        "strict mode",
        "enforce_admins=true",
        "required_conversation_resolution=true",
        "CodeQL handling",
        "exact-current-head review",
        "and wait",
    ):
        mutated = top_entry.replace(clause, "", 1)
        with pytest.raises(AssertionError, match="missing required phrase"):
            _require_phrase(mutated, clause)

    precedence_start = agents.index(
        "- **Precedence for the GitHub-Claude required-approval description under"
    )
    precedence_end = agents.index("\n- Mechanism retained", precedence_start)
    precedence = agents[precedence_start:precedence_end]
    for filename in ("AGENTS.md", "docs/state/registry.md"):
        with pytest.raises(AssertionError, match="missing required phrase"):
            _require_phrase(precedence.replace(filename, "", 1), filename)

    agent_spans = _historical_spans(agents)
    agent_history = "".join(agents[a:b] for a, b in agent_spans)
    with pytest.raises(AssertionError, match="missing required phrase"):
        _require_phrase(
            agent_history.replace("WAIT for Claude's PR-scoped approval before merging", "", 1),
            "WAIT for Claude's PR-scoped approval before merging",
        )

    june_directive = "arm the `review-gate` required check (#159 / D58)"
    with pytest.raises(AssertionError):
        assert _within_any_span(0, len(june_directive), _historical_spans(june_directive))
