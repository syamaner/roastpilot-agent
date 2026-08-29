"""Structural governance for the CodeQL docs-only fast path (D180 §2.8/§3.7)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO / ".github" / "workflows" / "codeql.yml"


def _workflow() -> dict[str, object]:
    """Load the CodeQL workflow as a structural mapping, normalising the YAML 1.1

    ``on`` boolean-key quirk PyYAML's safe loader applies to a bare ``on:`` key.
    """
    loaded: object = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    raw = cast(dict[object, object], loaded)
    return {"on" if key is True else cast(str, key): value for key, value in raw.items()}


@pytest.mark.docs_ci
def test_analyze_needs_classify_and_carries_the_exact_docs_only_condition() -> None:
    """`analyze` skips only an admitted docs-only pull request."""
    workflow = _workflow()
    jobs = cast(dict[str, object], workflow["jobs"])
    analyze = cast(dict[str, object], jobs["analyze"])
    needs = analyze.get("needs", [])
    needs_values = {needs} if isinstance(needs, str) else set(cast(list[str], needs))
    assert needs_values == {"classify"}
    assert analyze.get("if") == "needs.classify.outputs.mode != 'docs-only'"


@pytest.mark.docs_ci
def test_classify_job_pins_checkout_by_sha_and_narrows_permissions() -> None:
    """The new classify job follows this file's SHA-pinning convention and narrows away
    the workflow-level `security-events: write` it would otherwise inherit."""
    workflow = _workflow()
    jobs = cast(dict[str, object], workflow["jobs"])
    classify = cast(dict[str, object], jobs["classify"])
    assert classify["permissions"] == {"contents": "read"}
    steps = cast(list[dict[str, object]], classify["steps"])
    for step in steps:
        uses = step.get("uses")
        if isinstance(uses, str) and uses.startswith("actions/checkout@"):
            assert uses.startswith("actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"), (
                "classify job's checkout must use this file's existing SHA pin"
            )
    assert classify.get("outputs") == {"mode": "${{ steps.classify.outputs.mode }}"}


def test_matrix_and_triggers_are_neither_widened_nor_narrowed() -> None:
    """The three-language matrix and the four triggers are byte-preserved."""
    workflow = _workflow()
    jobs = cast(dict[str, object], workflow["jobs"])
    analyze = cast(dict[str, object], jobs["analyze"])
    strategy = cast(dict[str, object], analyze["strategy"])
    assert strategy["fail-fast"] is False
    matrix = cast(dict[str, object], strategy["matrix"])
    assert matrix["language"] == ["actions", "javascript-typescript", "python"]

    on_block = workflow.get("on")
    assert isinstance(on_block, dict)
    triggers = cast(dict[str, object], on_block)
    assert set(triggers) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    assert cast(dict[str, object], triggers["push"])["branches"] == ["main"]
    assert cast(dict[str, object], triggers["pull_request"])["branches"] == ["main"]


def test_workflow_level_permissions_unchanged() -> None:
    """The workflow-level `security-events: write` stays for `analyze`; only `classify` narrows."""
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read", "security-events": "write"}
