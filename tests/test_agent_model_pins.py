"""Consistency guard for the agent-topology model/effort pins.

The topology (``docs/agent-topology.md``) pins each named subagent to a full
model ID with an explicit effort, and §12 requires a consistency mechanism when
that fact is duplicated across the ten frontmatters, ``AGENTS.md``, and the §4
table. This test is that mechanism: a single authoritative mapping that fails if
any agent frontmatter drifts (an alias creeps back, a model/effort changes
without updating the map, or a role is added/removed), plus a repo-side guard
against committed settings that could silently defeat the pins.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_AGENTS_DIR = _REPO / ".claude" / "agents"

# The single authoritative (model, effort) mapping for every named subagent.
# Any change to an agent's frontmatter must be reflected here, and vice versa.
_EXPECTED: dict[str, tuple[str, str]] = {
    "engineer-be": ("claude-sonnet-5", "high"),
    "engineer-fe": ("claude-sonnet-5", "high"),
    "mcp-contract-checker": ("claude-sonnet-5", "medium"),
    "sim-roast-runner": ("claude-sonnet-5", "medium"),
    "pr-triage": ("claude-sonnet-5", "high"),
    "product-pm": ("claude-sonnet-5", "high"),
    "qa": ("claude-sonnet-5", "high"),
    "security-reviewer": ("claude-sonnet-5", "high"),
    "ui-reviewer": ("claude-sonnet-5", "high"),
    "safety-reviewer": ("claude-opus-5", "xhigh"),
    "planning-architect": ("claude-fable-5", "high"),
}
_ALIASES = {"sonnet", "opus", "fable", "haiku", "best", "default"}


def _frontmatter(path: Path) -> dict[str, str]:
    """Return the simple ``key: value`` frontmatter fields of an agent file."""
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, f"{path.name}: no YAML frontmatter"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def _agent_files() -> list[Path]:
    return sorted(_AGENTS_DIR.glob("*.md"))


def test_every_expected_agent_exists() -> None:
    on_disk = {p.stem for p in _agent_files()}
    assert on_disk == set(_EXPECTED), (
        f"agent set drifted from the map: only-on-disk={on_disk - set(_EXPECTED)}, "
        f"only-in-map={set(_EXPECTED) - on_disk}"
    )


@pytest.mark.parametrize("path", _agent_files(), ids=lambda p: p.stem)
def test_agent_model_and_effort_match_the_map(path: Path) -> None:
    fm = _frontmatter(path)
    name = fm.get("name")
    assert name == path.stem, (
        f"{path.name}: frontmatter name {name!r} must equal the filename stem "
        f"{path.stem!r} — workflows resolve agents by name, so a mismatch or a "
        f"renamed role would silently pass the file-set check"
    )
    assert name in _EXPECTED, f"{name} is not in the authoritative pin map"
    expected_model, expected_effort = _EXPECTED[name]
    assert fm.get("model") == expected_model, (
        f"{name}: model is {fm.get('model')!r}, expected {expected_model!r}"
    )
    assert fm.get("effort") == expected_effort, (
        f"{name}: effort is {fm.get('effort')!r}, expected {expected_effort!r}"
    )


@pytest.mark.parametrize("path", _agent_files(), ids=lambda p: p.stem)
def test_no_agent_uses_a_floating_alias(path: Path) -> None:
    model = _frontmatter(path).get("model", "")
    assert model not in _ALIASES, f"{path.stem}: model {model!r} is a floating alias; pin a full ID"
    assert model.startswith("claude-"), f"{path.stem}: model {model!r} is not a full ID"


def test_agents_md_prose_names_the_full_ids() -> None:
    """AGENTS.md prose must name the pinned IDs and the two special role associations."""
    agents_md = (_REPO / "AGENTS.md").read_text()
    for full_id in {m for m, _ in _EXPECTED.values()}:
        assert full_id in agents_md, f"AGENTS.md does not mention the pinned id {full_id}"
    # Bind the two roles whose model/effort differ from the Sonnet-high default, so
    # the prose can't drift from the pins for them.
    assert re.search(r"claude-opus-5.{0,24}xhigh", agents_md, re.S), (
        "AGENTS.md must document safety-reviewer as claude-opus-5 at xhigh effort"
    )
    assert "claude-fable-5" in agents_md and "planning-architect" in agents_md, (
        "AGENTS.md must document the planning-architect claude-fable-5 pin"
    )


def test_workflow_review_model_is_a_full_id() -> None:
    """The review-branch workflow's inline-stage pin must be a full ID, not an alias."""
    mjs = (_REPO / ".claude" / "workflows" / "review-branch.mjs").read_text()
    match = re.search(r"const REVIEW_MODEL = '([^']+)'", mjs)
    assert match, "REVIEW_MODEL constant not found in review-branch.mjs"
    value = match.group(1)
    assert value not in _ALIASES, f"REVIEW_MODEL {value!r} is a floating alias; pin a full ID"
    assert value == "claude-sonnet-5", f"REVIEW_MODEL {value!r} != claude-sonnet-5"


def test_committed_settings_do_not_defeat_the_pins() -> None:
    """Repo-side of the §5 override check, on GIT-TRACKED settings only.

    A committed ``availableModels`` that excludes a pinned model, or a committed
    ``CLAUDE_CODE_SUBAGENT_MODEL`` env override, would defeat the frontmatter pins.
    Only tracked files are inspected: a developer's local (gitignored) override is
    their own environment, not repository config, and must not fail the suite. The
    live-environment and organisation-policy side stays an operator verification.
    """
    import json
    import subprocess

    tracked = set(
        subprocess.run(
            ["git", "ls-files", ".claude"],
            cwd=_REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )
    pinned_ids = {m for m, _ in _EXPECTED.values()}
    for rel in (".claude/settings.json", ".claude/settings.local.json"):
        if rel not in tracked:
            continue
        data = json.loads((_REPO / rel).read_text())
        env = data.get("env", {})
        assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env, (
            f"{rel} commits a CLAUDE_CODE_SUBAGENT_MODEL override that defeats every pin"
        )
        allowed = data.get("availableModels")
        if not allowed:
            continue
        for pinned in pinned_ids:
            # family alias (sonnet/opus/fable), exact id, or a prefix the id extends
            family = pinned.rsplit("-", 1)[0].rsplit("-", 1)[-1]
            permitted = family in allowed or any(
                pinned == e or pinned.startswith(e + "-") for e in allowed
            )
            assert permitted, f"{rel} availableModels {allowed} does not permit pinned id {pinned}"
