"""Closed synthetic producer rows and malformed exact Drop receipts."""

import json
from dataclasses import replace
from typing import Any

import pytest

from roastpilot_agent.exact_drop_reply import (
    ExactDropDisposition as D,
)
from roastpilot_agent.exact_drop_reply import (
    ExactDropIdentity,
    ExactDropReason,
    ExactDropReceipt,
    decode_exact_drop_reply,
)

IDENTITY = ExactDropIdentity("schema-session", "0" * 32, 2**53 - 1, 1)
KEYS = [
    "outcome",
    "reason",
    "request_dispatch",
    "this_request",
    "history",
    "fence",
    "prior_transition",
    "observer_failure",
    "cleanup_failure",
]
SETTLED = ("transitioned", "reservation_owned", "saved", "released", "not_applicable")
NON_ENTRY = ("not_invoked", "no_reservation", "not_applicable", "not_applicable", "not_applicable")
# Source-derived closed tuples from the ratified protected producer contract.
ROWS = [
    (D.COMPLETED, ("success", None, *SETTLED, False, False)),
    *[
        (D.COMPLETED, ("success", reason, *SETTLED, True, cleanup))
        for reason in ("observation_resolution_failed", "observation_freeze_failed")
        for cleanup in (False, True)
    ],
    (D.COMPLETED, ("success", "post_drop_cleanup_failed", *SETTLED, False, True)),
    *[
        (D.REFUSED, (outcome, reason, *NON_ENTRY, False, False))
        for outcome, reason in (
            ("refused_capability", "manual_mock_normal_drop_required"),
            ("refused_state", "exact_baseline_unavailable"),
            ("refused_precondition", "human_first_crack_unavailable"),
            ("refused_state", "exact_live_owner_unavailable"),
            ("refused_precondition", "human_first_crack_missing"),
            ("refused_state", "dispatch_reservation_unavailable"),
            ("refused", "timing_evidence_superseded"),
        )
    ],
    (
        D.PRIOR_RECORDED,
        ("already_dropped", "endpoint_origin_not_established", *NON_ENTRY, False, False),
    ),
    (D.PRIOR_UNRESOLVED, ("in_progress", "normal_drop_in_progress", *NON_ENTRY, False, False)),
    (
        D.PRIOR_UNRESOLVED,
        (
            "ambiguous",
            "normal_drop_unresolved",
            "not_invoked",
            "no_reservation",
            "unknown",
            "retained",
            "unknown",
            False,
            False,
        ),
    ),
]


def payload(row: tuple[object, ...] = ROWS[0][1]) -> dict[str, Any]:
    """Build a source-derived receipt, without claiming a live wire capture."""
    return {
        **IDENTITY.arguments(),
        **dict(zip(KEYS, row, strict=True)),
        "retry": "forbidden",
        "physical_effect": "not_established",
    }


def decode(raw: dict[str, Any], text: str | None = None) -> ExactDropReceipt:
    """Decode an exact scalar mirror or deliberately replaced text."""
    return decode_exact_drop_reply(
        False,
        [{"type": "text", "text": json.dumps(raw) if text is None else text}],
        raw,
        IDENTITY,
    )


@pytest.mark.parametrize(("expected", "row"), ROWS)
def test_complete_closed_rows(expected: D, row: tuple[object, ...]) -> None:
    raw = payload(row)
    result = decode(raw)
    assert result.disposition is expected
    assert result.identity == IDENTITY
    assert result.reason == (None if raw["reason"] is None else ExactDropReason(raw["reason"]))
    assert result.observer_failure is raw["observer_failure"]
    assert result.cleanup_failure is raw["cleanup_failure"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("session_id", "foreign"),
        ("clock_epoch_id", "b" * 32),
        ("ownership_sequence", True),
        ("timing_evidence_sequence", 2),
        ("ownership_sequence", float(2**53 - 1)),
        ("observer_failure", 0),
        ("cleanup_failure", 1),
        ("outcome", "ambiguous"),
        ("reason", "normal_drop_unresolved"),
        ("reason", "observation_freeze_failed"),
        ("history", "unknown"),
        ("fence", "retained"),
        ("prior_transition", "unknown"),
        ("request_dispatch", "not_invoked"),
        ("this_request", "no_reservation"),
        ("retry", "allowed"),
        ("physical_effect", "established"),
    ],
)
def test_outside_grammar_or_wrong_identity(key: str, value: object) -> None:
    raw = payload()
    raw[key] = value
    assert decode(raw) == ExactDropReceipt(D.UNKNOWN)


@pytest.mark.parametrize(("expected", "row"), ROWS)
def test_every_row_rejects_contradictory_settlement(expected: D, row: tuple[object, ...]) -> None:
    del expected
    raw = payload(row)
    raw["history"] = "contradictory"
    assert decode(raw).disposition is D.UNKNOWN
    raw = payload(row)
    raw["observer_failure"] = not raw["observer_failure"]
    # Flipping into another complete permitted auxiliary row remains valid.
    candidate = tuple(raw[k] for k in KEYS)
    admitted = any(candidate == valid for _, valid in ROWS)
    assert (decode(raw).disposition is not D.UNKNOWN) is admitted


@pytest.mark.parametrize("key", list(payload()))
def test_missing_field(key: str) -> None:
    raw = payload()
    del raw[key]
    assert decode(raw).disposition is D.UNKNOWN


@pytest.mark.parametrize(
    "text",
    [
        '{"outcome":"success","outcome":"success"}',
        json.dumps(payload()).replace('"reason": null', '"reason": NaN'),
        json.dumps(payload()).replace('"reason": null', '"reason": Infinity'),
        json.dumps(payload()).replace('"reason": null', '"reason": 1e999'),
        "{",
        " " * 16_385,
        json.dumps({**payload(), "ownership_sequence": True}),
    ],
)
def test_bad_text(text: str) -> None:
    assert decode(payload(), text).disposition is D.UNKNOWN


def test_duplicate_full_mirror_and_cap_boundary() -> None:
    text = json.dumps(payload())
    assert decode(payload(), text[:-1] + ', "reason":null}').disposition is D.UNKNOWN
    assert decode(payload(), text + " " * (16_384 - len(text))).disposition is D.COMPLETED
    assert decode(payload(), text + " " * (16_385 - len(text))).disposition is D.UNKNOWN


@pytest.mark.parametrize(
    ("error", "content", "raw"),
    [
        (True, [], payload()),
        (0, [], payload()),
        (False, [], payload()),
        (False, None, payload()),
        (False, [{"type": "text", "text": json.dumps(payload()), "extra": 1}], payload()),
        (False, [{"type": "image", "text": json.dumps(payload())}], payload()),
        (False, [{"type": "text", "text": 123}], payload()),
        (False, [{"type": "text", "text": json.dumps(payload())}] * 2, payload()),
        (False, [{"type": "text", "text": json.dumps(payload())}], {**payload(), "extra": None}),
        (False, [{"type": "text", "text": json.dumps(payload())}], None),
    ],
)
def test_envelope(error: object, content: object, raw: object) -> None:
    assert decode_exact_drop_reply(error, content, raw, IDENTITY).disposition is D.UNKNOWN


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", ""),
        ("session_id", "x" * 129),
        ("session_id", True),
        ("clock_epoch_id", "A" * 32),
        ("clock_epoch_id", "a" * 32 + "\n"),
        ("ownership_sequence", 0),
        ("ownership_sequence", True),
        ("timing_evidence_sequence", 2**53),
        ("timing_evidence_sequence", 1.0),
    ],
)
def test_identity_bounds(field: str, value: Any) -> None:
    identity = replace(IDENTITY, **{field: value})
    assert not identity.is_valid()
    assert decode_exact_drop_reply(False, [], payload(), identity).disposition is D.UNKNOWN
