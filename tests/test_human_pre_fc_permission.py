"""Pure receipt and canonical-identity guards for the private mock permit."""

import ast
import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from roastpilot_agent import human_pre_fc_permission as permission_module
from roastpilot_agent.human_pre_fc_permission import (
    MarkReceipt,
    PermitState,
    PreFcPermission,
    PrivateBinding,
    canonical_mark_matches,
    decode_private_receipt,
    pre_mark_evidence,
)
from roastpilot_agent.timing_evidence import (
    EventEvidence,
    EventSource,
    TimeBasis,
    TimingEvidence,
    ValidChannel,
)

SESSION = "a" * 32
EPOCH = "0" * 32


def binding() -> PrivateBinding:
    """Create opaque synthetic owner objects without an SDK."""
    return PrivateBinding(object(), object(), object(), object(), SESSION, 1, 0, 0)


def control_receipt() -> dict[str, Any]:
    """Return the exact protected control-result shape."""
    return dict(
        session_id=SESSION,
        phase="roasting",
        heat_level_percent=65,
        fan_level_percent=30,
        cooling_on=False,
    )


def mark_receipt() -> dict[str, Any]:
    """Return the exact protected event-result shape."""
    return dict(
        session_id=SESSION,
        phase="development",
        event_count=2,
        event=dict(
            kind="first_crack_detected",
            recorded_at_utc="2026-11-21T22:00:00Z",
            monotonic_seconds=2.0,
            payload={"source": "manual"},
        ),
    )


def decode(
    name: str, raw: Any, *, error: object = False, text: str | None = None
) -> dict[str, object] | None:
    """Decode the independent text and structured representations."""
    return decode_private_receipt(
        name,
        error,
        [{"type": "text", "text": json.dumps(raw) if text is None else text}],
        raw,
        SESSION,
        65 if name == "set_heat" else 30,
    )


def timing(*, post: bool = False) -> TimingEvidence:
    """Supply atomic synthetic timing with a canonical charge and optional FC."""
    charge = EventEvidence(
        "beans_added", 0, 0.0, EPOCH, EventSource.AUTO_T0, TimeBasis.PRODUCER_CANONICAL_RECORD
    )
    fc = EventEvidence(
        "first_crack_detected",
        1,
        2.0,
        EPOCH,
        EventSource.MANUAL_MARK,
        TimeBasis.PRODUCER_CANONICAL_RECORD,
    )
    return TimingEvidence(
        1,
        SESSION,
        1,
        EPOCH,
        2 if post else 1,
        "development" if post else "roasting",
        charge,
        fc if post else None,
        None,
        ValidChannel(180.0, 3.0, 3.0),
        ValidChannel(200.0, 3.0, 3.0),
    )


def test_one_way_grant_and_claim() -> None:
    permit = PreFcPermission(binding())
    assert permit.state is PermitState.ACTIVE
    claim = permit.reserve_mark()
    assert claim is not None and claim.binding is permit.binding
    assert permit.reserve_mark() is None
    permit.end()
    assert permit.state is PermitState.ENDED and permit.reserve_mark() is None


def test_private_capability_has_only_the_ratified_callers() -> None:
    references: dict[str, list[tuple[str, str]]] = {
        "_grant_private_pre_fc": [],
        "_private_pre_fc_call": [],
    }
    for path in sorted(Path(permission_module.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text())
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in references:
                owner = parents.get(node)
                functions: list[str] = []
                while owner is not None:
                    if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
                        functions.append(owner.name)
                    owner = parents.get(owner)
                references[node.attr].append((path.name, functions[-1]))
    for values in references.values():
        values.sort()
    assert references == {
        "_grant_private_pre_fc": [("api.py", "_start_mock_human_roast")],
        "_private_pre_fc_call": [
            ("controller.py", "_execute_private_pre_fc_targets"),
            ("controller.py", "_operator_private_first_crack"),
        ],
    }


@pytest.mark.parametrize("name", ["set_heat", "set_fan", "mark_first_crack"])
def test_receipt_positive_and_envelope_guards(name: str) -> None:
    raw = mark_receipt() if name == "mark_first_crack" else control_receipt()
    assert decode(name, raw) == raw
    for flag in (True, 0, 1, None, "false"):
        assert decode(name, raw, error=flag) is None
    assert decode(name, raw, text=json.dumps(raw).replace(SESSION, "b" * 32)) is None
    assert decode(name, raw, text='{"session_id":"duplicate",' + json.dumps(raw)[1:]) is None
    assert decode(name, raw, text=" " * 16385 + json.dumps(raw)) is None
    assert decode_private_receipt(name, False, [], raw, SESSION, 65) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("session_id", 1),
        ("session_id", "wrong"),
        ("phase", "development"),
        ("phase", 1),
        ("heat_level_percent", True),
        ("heat_level_percent", 65.0),
        ("heat_level_percent", 64),
        ("fan_level_percent", "30"),
        ("fan_level_percent", -1),
        ("cooling_on", 0),
        ("extra", 1),
    ],
)
def test_control_raw_authority(key: str, value: object) -> None:
    raw = control_receipt()
    raw[key] = value
    assert decode("set_heat", raw) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("session_id", 1),
        ("session_id", "wrong"),
        ("phase", "roasting"),
        ("event_count", True),
        ("event_count", 2.0),
        ("event_count", 0),
        ("event_count", 2**53),
        ("extra", 1),
        ("event", []),
    ],
)
def test_mark_raw_authority(key: str, value: object) -> None:
    raw = mark_receipt()
    raw[key] = value
    assert decode("mark_first_crack", raw) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("kind", "beans_added"),
        ("kind", 1),
        ("monotonic_seconds", 2),
        ("monotonic_seconds", True),
        ("monotonic_seconds", -1.0),
        ("monotonic_seconds", math.inf),
        ("monotonic_seconds", math.nan),
        ("recorded_at_utc", 1),
        ("payload", []),
        ("extra", 1),
    ],
)
def test_mark_event_authority(key: str, value: object) -> None:
    raw = mark_receipt()
    raw["event"][key] = value
    assert decode("mark_first_crack", raw) is None


def test_binary_float_and_representation_types() -> None:
    raw = mark_receipt()
    raw["event"]["payload"] = {"zero": 0.0}
    assert (
        decode("mark_first_crack", raw, text=json.dumps(raw).replace('"zero": 0.0', '"zero": -0.0'))
        is None
    )
    raw["event"]["monotonic_seconds"] = 0.0
    assert (
        decode(
            "mark_first_crack",
            raw,
            text=json.dumps(raw).replace('"monotonic_seconds": 0.0', '"monotonic_seconds": -0.0'),
        )
        is None
    )


@pytest.mark.parametrize(
    "fault",
    [
        "session",
        "child",
        "epoch",
        "ownership",
        "sequence",
        "phase",
        "charge",
        "fc",
        "drop",
        "source",
        "basis",
        "fc_epoch",
        "time",
        "index",
        "receipt_session",
        "receipt_count",
        "receipt_time",
        "pre_existing",
        "pre_missing_charge",
    ],
)
def test_canonical_mark_identity(fault: str) -> None:
    before, after = timing(), timing(post=True)
    receipt = MarkReceipt(SESSION, 2.0, 2)
    assert canonical_mark_matches(before, after, receipt, binding())
    changes: dict[str, Any] = {
        "session": {"session_id": "wrong"},
        "child": {"child_epoch": 2},
        "epoch": {"epoch_id": "f" * 32},
        "ownership": {"session_ownership_sequence": 2},
        "sequence": {"evidence_sequence": 1},
        "phase": {"phase": "roasting"},
        "charge": {"charge": None},
        "fc": {"first_crack": None},
        "drop": {"drop": after.charge},
    }
    if fault in changes:
        after = replace(after, **changes[fault])
    elif fault in ("source", "basis", "fc_epoch", "time", "index"):
        assert after.first_crack is not None
        fc_changes = {
            "source": {"source": EventSource.MODEL_DETECTION},
            "basis": {"time_basis": TimeBasis.DETECTOR_CLOCK_REBASED},
            "fc_epoch": {"clock_epoch_id": "f" * 32},
            "time": {"canonical_seconds": math.nextafter(2.0, math.inf)},
            "index": {"event_index": 2},
        }
        after = replace(after, first_crack=replace(after.first_crack, **fc_changes[fault]))
    elif fault == "receipt_session":
        receipt = replace(receipt, session_id="wrong")
    elif fault == "receipt_count":
        receipt = replace(receipt, event_count=3)
    elif fault == "receipt_time":
        receipt = replace(receipt, monotonic_seconds=math.nextafter(2.0, math.inf))
    elif fault == "pre_existing":
        before = replace(before, first_crack=after.first_crack)
    else:
        before = replace(before, charge=None)
    assert not canonical_mark_matches(before, after, receipt, binding())
    if fault.startswith("pre_"):
        assert not pre_mark_evidence(before, binding())
