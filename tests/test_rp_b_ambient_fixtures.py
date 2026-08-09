"""Tests for the RP-B (#709) ambient-eval anonymised fixture registry (D129).

Deliberately does NOT import ``advisor_bakeoff`` (heavy pydantic-ai / provider
deps) — this module is stdlib-only by design, and its tests stay that way too.
No real roast data anywhere here: every fixture path used is a synthetic
``tmp_path`` file, never the operator's actual store output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rp_b_ambient_fixtures as fixtures  # noqa: E402


def test_eval_set_is_the_ratified_four_point_spread() -> None:
    """D129: exactly the four operator-ratified anonymised names, in the
    below/below/above/above boundary order the module docstring records."""
    assert fixtures.AMBIENT_EVAL_FIXTURE_NAMES == ("eval-02", "eval-06", "eval-07", "eval-10")
    assert len(set(fixtures.AMBIENT_EVAL_FIXTURE_NAMES)) == 4


def test_fixture_path_for_points_under_the_gitignored_dir() -> None:
    """Every fixture path resolves under AMBIENT_FIXTURES_DIR/<name>/roast.jsonl —
    the same shape advisor_bakeoff.fixture_path_for uses for .artisan-fixtures."""
    path = fixtures.fixture_path_for("eval-02")
    assert path == fixtures.AMBIENT_FIXTURES_DIR / "eval-02" / "roast.jsonl"


def test_resolve_ambient_eval_set_errors_clearly_on_missing_fixtures(tmp_path: Path) -> None:
    """A checkout with no local fixtures fails loudly, naming every missing one —
    never silently scores a partial set (mirrors
    test_resolve_test_set_errors_clearly_on_missing_fixture in
    tests/test_advisor_bakeoff.py for the sibling .artisan-fixtures resolver)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fixtures, "AMBIENT_FIXTURES_DIR", tmp_path)
        with pytest.raises(
            FileNotFoundError, match="missing local-only RP-B ambient-eval fixtures"
        ) as exc_info:
            fixtures.resolve_ambient_eval_set(("eval-02", "eval-06"))
        # Both missing names are listed, not just the first — an implementer
        # regenerating fixtures needs the full gap, not one at a time.
        assert "eval-02" in str(exc_info.value)
        assert "eval-06" in str(exc_info.value)


def test_resolve_ambient_eval_set_errors_naming_only_the_absent_subset(tmp_path: Path) -> None:
    """A partially-regenerated local set names only what's actually missing."""
    present_dir = tmp_path / "eval-02"
    present_dir.mkdir()
    (present_dir / "roast.jsonl").write_text('{"type": "telemetry"}\n', encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fixtures, "AMBIENT_FIXTURES_DIR", tmp_path)
        with pytest.raises(FileNotFoundError) as exc_info:
            fixtures.resolve_ambient_eval_set(("eval-02", "eval-06"))
        assert "eval-02" not in str(exc_info.value)
        assert "eval-06" in str(exc_info.value)


def test_resolve_ambient_eval_set_returns_paths_in_order_when_all_present(
    tmp_path: Path,
) -> None:
    """A fully-regenerated local set resolves cleanly, preserving name order."""
    for name in ("eval-02", "eval-06", "eval-07", "eval-10"):
        fixture_dir = tmp_path / name
        fixture_dir.mkdir()
        (fixture_dir / "roast.jsonl").write_text('{"type": "telemetry"}\n', encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fixtures, "AMBIENT_FIXTURES_DIR", tmp_path)
        resolved = fixtures.resolve_ambient_eval_set()
    assert resolved == tuple(
        tmp_path / name / "roast.jsonl" for name in fixtures.AMBIENT_EVAL_FIXTURE_NAMES
    )


def test_resolve_ambient_eval_set_defaults_to_the_full_ratified_set(tmp_path: Path) -> None:
    """Calling with no arguments resolves the full D129 set, not a caller-chosen
    subset — the default matters because a future harness may call this with no
    arguments expecting the ratified four."""
    for name in fixtures.AMBIENT_EVAL_FIXTURE_NAMES:
        fixture_dir = tmp_path / name
        fixture_dir.mkdir()
        (fixture_dir / "roast.jsonl").write_text('{"type": "telemetry"}\n', encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fixtures, "AMBIENT_FIXTURES_DIR", tmp_path)
        resolved = fixtures.resolve_ambient_eval_set()
    assert len(resolved) == len(fixtures.AMBIENT_EVAL_FIXTURE_NAMES)
