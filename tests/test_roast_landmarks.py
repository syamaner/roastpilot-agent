"""Tests for shared persisted first-crack landmark resolution."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import roast_landmarks as landmarks  # noqa: E402
import store_to_fixture as s2f  # noqa: E402


def _status(value: str) -> str:
    """Build one synthetic raw first-crack status state."""
    return json.dumps({"first_crack_status": {"detected_at_utc": value}})


def test_first_crack_onsets_collapse_equal_instants_and_choose_distinct_earliest() -> None:
    """Equivalent spellings collapse while a truly earlier instant wins."""
    onset, count = landmarks.first_crack_onset_utc(
        [
            _status("2026-01-01T00:01:00Z"),
            _status("2026-01-01T00:01:00+00:00"),
            _status("2026-01-01T00:00:35+00:00"),
        ]
    )

    assert onset == "2026-01-01T00:00:35+00:00"
    assert count == 2


def test_malformed_states_are_skipped_but_unparseable_only_stays_for_exporter_fallback() -> None:
    """A valid later status survives malformed siblings without fabrication."""
    onset, count = landmarks.first_crack_onset_utc(
        [None, "{", "[]", json.dumps({"first_crack_status": {}}), _status("bad-date")]
    )

    assert onset == "bad-date"
    assert count == 1


def test_mcp_provenance_is_exact_and_case_sensitive() -> None:
    """Only the committed lower-case MCP provenance admits a status onset."""
    assert landmarks.is_mcp_first_crack_source("mcp")
    assert not landmarks.is_mcp_first_crack_source("MCP")
    assert not landmarks.is_mcp_first_crack_source("operator")
    assert not landmarks.is_mcp_first_crack_source(None)


def test_shared_parsers_reject_invalid_shapes_and_normalize_naive_utc() -> None:
    """Shared state parsing is strict about JSON shapes and timestamp inputs."""
    assert landmarks.first_crack_event_source(None) is None
    assert landmarks.first_crack_event_source("{") is None
    assert landmarks.first_crack_event_source("[]") is None
    assert landmarks.first_crack_event_source('{"source": 1}') is None
    assert landmarks.parse_utc(None) is None
    assert landmarks.parse_utc("") is None
    assert landmarks.parse_utc("not-a-date") is None
    parsed = landmarks.parse_utc("2026-01-01T00:00:00")
    assert parsed is not None
    assert parsed.isoformat() == "2026-01-01T00:00:00+00:00"


def test_utc_mapping_skips_invalid_anchors_and_rejects_negative_results() -> None:
    """Only finite numeric anchors may map a status onset onto the run clock."""
    target = "2026-01-01T00:00:10+00:00"
    assert (
        landmarks.utc_to_run_seconds(
            target,
            [
                ("bad-clock", 1.0),
                ("2026-01-01T00:00:10+00:00", object()),
                ("2026-01-01T00:00:10+00:00", True),
                ("2026-01-01T00:00:10+00:00", math.inf),
                ("2026-01-01T00:00:10+00:00", "invalid"),
                ("2026-01-01T00:00:08+00:00", 8.0),
                ("2026-01-01T00:00:12+00:00", 12.0),
            ],
        )
        == 10.0
    )
    assert (
        landmarks.utc_to_run_seconds(
            "2026-01-01T00:00:00+00:00",
            [("2026-01-01T00:00:10+00:00", 1.0)],
        )
        is None
    )


def test_exporter_delegates_onset_selection_with_shared_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exporter wrapper calls the shared resolver on the persisted row order."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "create table telemetry_snapshots (id integer primary key, run_id text, tick integer, "
        "raw_state_json text)"
    )
    states = [_status("2026-01-01T00:01:00Z"), _status("2026-01-01T00:00:35+00:00")]
    for index, state in enumerate(states, start=1):
        con.execute(
            "insert into telemetry_snapshots (id, run_id, tick, raw_state_json) "
            "values (?, ?, ?, ?)",
            (index, "run", index, state),
        )
    con.commit()
    expected = landmarks.first_crack_onset_utc(states)
    calls: list[list[object]] = []

    def wrapped(raw_states: Iterable[object]) -> tuple[str | None, int]:
        """Record the exporter input while preserving the shared behavior."""
        materialized = list(raw_states)
        calls.append(materialized)
        return landmarks.first_crack_onset_utc(materialized)

    monkeypatch.setattr(s2f, "first_crack_onset_utc", wrapped)

    assert s2f._first_crack_onset_utc(con, "run") == expected  # pyright: ignore[reportPrivateUsage]
    assert calls == [states]
