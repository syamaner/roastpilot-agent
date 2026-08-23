"""Synthetic store-corpus tests for the plant-model first-crack anchor."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import plant_model_arx_study as study  # noqa: E402

_START = datetime(2026, 1, 1, tzinfo=UTC)
_FALLBACK_FC_SECONDS = 70


def _wall(seconds: int) -> str:
    """Return a synthetic UTC wall clock aligned to charge elapsed time."""
    return (_START + timedelta(seconds=seconds)).isoformat()


def _status(seconds: int) -> str:
    """Build a synthetic persisted MCP first-crack status."""
    return json.dumps({"first_crack_status": {"detected_at_utc": _wall(seconds)}})


def _write_store(
    path: Path,
    *,
    event_source: str | None = "mcp",
    raw_states: list[str] | None = None,
    with_clock_anchors: bool = True,
) -> None:
    """Create a minimal, synthetic completed store run for the offline study."""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            create table roast_runs (
                id text primary key, outcome text, started_at_utc text, excluded integer
            );
            create table telemetry_snapshots (
                id integer primary key, run_id text, tick integer,
                charge_elapsed_seconds real, bean_temp_c real, env_temp_c real,
                heat_level_percent real, fan_level_percent real, agent_phase text,
                recorded_at_utc text, raw_state_json text
            );
            create table roast_events (
                id integer primary key, run_id text, kind text, monotonic_seconds real,
                recorded_at_utc text, payload_json text
            );
            """
        )
        payload: dict[str, object] = {"bean_temp_c": 170.0}
        if event_source is not None:
            payload["source"] = event_source
        con.execute(
            "insert into roast_runs values (?, 'completed', ?, 0)",
            ("synthetic-run", _wall(0)),
        )
        states = raw_states or []
        for second in range(101):
            con.execute(
                "insert into telemetry_snapshots values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    second + 1,
                    "synthetic-run",
                    second,
                    float(second),
                    100.0 + float(second),
                    150.0 + float(second),
                    80.0,
                    30.0,
                    "development" if second >= _FALLBACK_FC_SECONDS else "roasting_pre_first_crack",
                    _wall(second) if with_clock_anchors else None,
                    states[second] if second < len(states) else None,
                ),
            )
        con.execute(
            "insert into roast_events values (1, ?, 'first_crack', ?, ?, ?)",
            (
                "synthetic-run",
                70.0,
                _wall(_FALLBACK_FC_SECONDS),
                json.dumps(payload),
            ),
        )
        con.commit()
    finally:
        con.close()


def _one_store_roast(path: Path) -> study.Roast:
    """Load the sole synthetic store roast."""
    roasts = study.load_store(path)
    assert len(roasts) == 1
    return roasts[0]


def test_store_uses_earlier_mcp_onset_for_coupled_fc_anchors(tmp_path: Path) -> None:
    """A 25-second earlier onset moves FC time and BT together by interpolation."""
    db_path = tmp_path / "earlier.sqlite3"
    states = ["{"] + [_status(45)]
    _write_store(db_path, raw_states=states)

    roast = _one_store_roast(db_path)

    assert roast.fc_t == 45.0
    assert roast.landmarks["fc_bt"] == 145.0
    assert roast.landmarks["fc_bt"] < 170.0
    assert roast.fc_anchor == "fc_status_utc"


@pytest.mark.parametrize(
    ("event_source", "raw_states", "with_clock_anchors", "expected_anchor"),
    [
        ("operator", [_status(45)], True, "phase_transition"),
        (None, [_status(45)], True, "phase_transition"),
        ("mcp", [], True, "phase_transition"),
        (
            "mcp",
            [json.dumps({"first_crack_status": {"detected_at_utc": "bad-date"}})],
            True,
            "phase_transition",
        ),
        ("mcp", ["{"], True, "phase_transition"),
        ("mcp", ["{", _status(45)], True, "fc_status_utc"),
        ("mcp", [_status(45)], False, "phase_transition"),
        ("mcp", [_status(125)], True, "phase_transition"),
    ],
    ids=(
        "operator",
        "missing-provenance",
        "no-raw-state",
        "unparseable-state",
        "malformed-state",
        "malformed-then-valid",
        "unmappable",
        "out-of-grid",
    ),
)
def test_store_fc_anchor_fails_closed_or_accepts_valid_status(
    tmp_path: Path,
    event_source: str | None,
    raw_states: list[str],
    with_clock_anchors: bool,
    expected_anchor: str,
) -> None:
    """Provenance and mapping failures retain the historical FC pair together."""
    db_path = tmp_path / f"{expected_anchor}-{event_source}-{with_clock_anchors}.sqlite3"
    _write_store(
        db_path,
        event_source=event_source,
        raw_states=raw_states,
        with_clock_anchors=with_clock_anchors,
    )

    roast = _one_store_roast(db_path)

    assert roast.fc_anchor == expected_anchor
    if expected_anchor == "fc_status_utc":
        assert roast.fc_t == 45.0
        assert roast.landmarks["fc_bt"] == 145.0
    else:
        assert roast.fc_t == float(_FALLBACK_FC_SECONDS)
        assert roast.landmarks["fc_bt"] == 170.0


def test_artisan_loading_keeps_its_existing_first_crack_marks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Artisan first-crack timing and temperature remain independent of store state."""
    alog = tmp_path / "synthetic.alog"
    alog.touch()
    profile = {
        "timex": list(range(141)),
        "temp1": [150.0 + value for value in range(141)],
        "temp2": [80.0 + value for value in range(141)],
        "specialevents": [],
        "specialeventstype": [],
        "specialeventsvalue": [],
        "timeindex": [10, 0, 70, 0, 0, 0, 140],
    }

    def fake_load_alog(_: Path) -> dict[str, list[int] | list[float]]:
        """Return the synthetic Artisan profile."""
        return profile

    monkeypatch.setattr(study, "load_alog", fake_load_alog)

    roasts = study.load_artisan(tmp_path)

    assert len(roasts) == 1
    assert roasts[0].fc_t == 60.0
    assert roasts[0].landmarks["fc_bt"] == 150.0
    assert roasts[0].fc_anchor is None


def test_landmarks_csv_records_closed_fc_anchor_vocabulary(tmp_path: Path) -> None:
    """Machine-readable landmark output carries only the defined store labels."""
    arrays = np.array([0.0, 1.0], dtype=np.float64)
    roasts = [
        study.Roast(
            rid="store:status",
            source_id="status",
            corpus="store",
            t=arrays,
            bt=arrays,
            et=arrays,
            heat=arrays,
            fan=arrays,
            fc_t=1.0,
            fc_anchor="fc_status_utc",
            drop_t=1.0,
            landmarks={"turnaround_bt": 1.0, "dry_end_bt": 2.0, "fc_bt": 3.0, "drop_bt": 4.0},
        ),
        study.Roast(
            rid="store:phase",
            source_id="phase",
            corpus="store",
            t=arrays,
            bt=arrays,
            et=arrays,
            heat=arrays,
            fan=arrays,
            fc_t=1.0,
            fc_anchor="phase_transition",
            drop_t=1.0,
            landmarks={"turnaround_bt": 1.0, "dry_end_bt": 2.0, "fc_bt": 3.0, "drop_bt": 4.0},
        ),
    ]

    study._write_landmarks_csv(roasts, tmp_path)  # pyright: ignore[reportPrivateUsage]

    rows = (tmp_path / "landmarks.csv").read_text().splitlines()
    assert rows[0].endswith("fc_t,fc_anchor,drop_t")
    assert rows[1].split(",")[-2] == "fc_status_utc"
    assert rows[2].split(",")[-2] == "phase_transition"


@pytest.mark.parametrize(
    "payload",
    [None, "{", "[]", '{"bean_temp_c": {}}', '{"bean_temp_c": "not-a-number"}'],
)
def test_historical_event_temperature_is_malformed_payload_safe(payload: object) -> None:
    """Malformed historical fallback payloads cannot abort a store-corpus load."""
    assert np.isnan(study._event_bean_temp(payload))  # pyright: ignore[reportPrivateUsage]
