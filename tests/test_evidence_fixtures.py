"""Integrity checks for compact evidence fixtures derived from hardware traces."""

import json
from pathlib import Path
from typing import Any

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "evidence" / "roast-3-authority-conflict.json"


def test_roast_three_authority_conflict_fixture_pins_recorded_sequence() -> None:
    """Keep the public Roast 3 evidence aligned with the source trace."""
    payload: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    source = payload["source"]
    samples = payload["samples"]

    assert payload["schema_version"] == 1
    assert source["run_id"] == "3fbfd8882d144965b1a2de4de8721d87"
    assert source["first_crack_bean_temp_c"] == 188.0
    assert source["profile_target_drop_temp_c"] == 195.0
    assert [sample["tick"] for sample in samples] == [918, 928, 936, 944, 952, 959, 966, 974, 981]
    assert [sample["seconds_since_first_crack"] for sample in samples] == [
        7.6,
        15.1,
        23.6,
        31.6,
        38.4,
        45.5,
        53.9,
        60.5,
        67.5,
    ]
    assert [sample["advisor_heat_target_percent"] for sample in samples] == [
        50,
        30,
        10,
        0,
        0,
        0,
        0,
        0,
        0,
    ]

    drop_requests = [sample for sample in samples if sample["advisor_should_drop"]]
    assert drop_requests[0]["bean_temp_c"] == 195.0
    assert [sample["drop_verdict"] for sample in drop_requests] == [
        "reject",
        "reject",
        "reject",
        "reject",
        "reject",
        "allow",
    ]
    assert all(sample["drop_rule"] == "advisor_drop_coherence" for sample in drop_requests[:5])
    assert drop_requests[-1]["drop_rule"] == "drop_eligibility"
    assert drop_requests[-1]["bean_temp_c"] == 203.0
    assert all(sample["telemetry_verdict"] == "allow" for sample in samples)
    assert all(sample["command_verdict"] == "allow" for sample in samples)
