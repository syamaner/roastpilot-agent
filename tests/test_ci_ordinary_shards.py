"""Behavioural tests for the sharded ordinary CI lane (#1039).

Covers the stdlib helper (partition, universe digest, lifecycle, strict reader,
identity, finalize and verify), the CI-only pytest shim through fakes and a fresh
in-process import, and real two-worker xdist runs of a synthetic project in an
owned temporary directory (never the repository's own pytest configuration).

Golden partition and digest constants were computed independently of both the
helper and Python's hashlib: LibreSSL 3.3.6 ``openssl dgst -sha256`` over
``printf`` bytes, with the index read as the parity of the final hex digit.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import ci_ordinary_shard_plugin as plugin
import ci_ordinary_shards as shards
import coverage
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
HEAD = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
REPOSITORY = "syamaner/roastpilot-agent"

#: Independent OpenSSL-derived shard indices.
GOLDEN_INDICES = {
    "tests/test_a.py::test_one": 1,
    "tests/test_a.py::test_two": 1,
    "tests/test_a.py::test_three": 0,
    "tests/test_b.py::TestK::test_m[1-x]": 0,
    "tests/test_ü.py::test_é[ß]": 0,
    "x": 1,
}
#: Independent OpenSSL-derived universe digests (sorted, 8-byte big-endian prefixed).
GOLDEN_DIGESTS = {
    ("b", "a"): "3c9d591045bc8876f9d0399bbfb05c6a412096e906f73278f98406cd5dca86df",
    ("ab", "c"): "601d5476e2ccfe2c87a2bba7a322659734a05749d5b5aa781f513e4912db0d5f",
    ("a", "bc"): "3fafa1cf2f19a7c1129beb20cf0983f73a489a221fc0dd2f16d1be292d089205",
    (): "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}


def _oracle_index(nodeid: str) -> int:
    """Test-local partition oracle: parity of the digest's final byte."""
    return hashlib.sha256(nodeid.encode("utf-8", "strict")).digest()[-1] & 1


def _oracle_digest(nodeids: Sequence[str]) -> str:
    """Test-local universe digest oracle."""
    digest = hashlib.sha256()
    for value in sorted(nodeid.encode("utf-8") for nodeid in nodeids):
        digest.update(len(value).to_bytes(8, "big") + value)
    return digest.hexdigest()


def _passed(nodeid: str) -> list[object]:
    """Return a clean setup/call/teardown record triple."""
    return [
        {"nodeid": nodeid, "when": "setup", "outcome": "passed"},
        {"nodeid": nodeid, "when": "call", "outcome": "passed"},
        {"nodeid": nodeid, "when": "teardown", "outcome": "passed"},
    ]


def _record(nodeid: str, when: str, outcome: str, wasxfail: object = None) -> dict[str, object]:
    """Return one lifecycle record, optionally carrying ``wasxfail``."""
    record: dict[str, object] = {"nodeid": nodeid, "when": when, "outcome": outcome}
    if wasxfail is not None:
        record["wasxfail"] = wasxfail
    return record


UNIVERSE = [f"tests/test_s.py::test_case_{number}" for number in range(16)]
KEPT = {index: [n for n in UNIVERSE if _oracle_index(n) == index] for index in (0, 1)}


def test_synthetic_universe_populates_both_shards() -> None:
    """The fixed synthetic universe exercises both indices."""
    assert KEPT[0] and KEPT[1]
    assert sorted(KEPT[0] + KEPT[1]) == sorted(UNIVERSE)


# --- partition and digest -------------------------------------------------


@pytest.mark.parametrize(("nodeid", "expected"), sorted(GOLDEN_INDICES.items()))
def test_partition_golden_vectors(nodeid: str, expected: int) -> None:
    """The helper and oracle both match the independent OpenSSL vectors."""
    assert shards.shard_index(nodeid) == expected
    assert _oracle_index(nodeid) == expected


@pytest.mark.parametrize(("nodeids", "expected"), sorted(GOLDEN_DIGESTS.items()))
def test_universe_digest_golden_vectors(nodeids: tuple[str, ...], expected: str) -> None:
    """The canonical digest matches the independent vectors in any input order."""
    assert shards.universe_digest(list(nodeids)) == expected
    assert shards.universe_digest(list(reversed(nodeids))) == expected


def test_universe_digest_is_length_prefixed_and_wide() -> None:
    """Length prefixes separate ``ab|c`` from ``a|bc``; IDs over 65,535 bytes fit."""
    assert shards.universe_digest(["ab", "c"]) != shards.universe_digest(["a", "bc"])
    long_ids = ["t::" + "x" * 65_671, "t::" + "x" * 65_535, "é" * 40_000]
    assert shards.universe_digest(long_ids) == _oracle_digest(long_ids)
    assert [shards.shard_index(n) for n in long_ids] == [_oracle_index(n) for n in long_ids]


def test_partition_rejects_surrogate_ids() -> None:
    """A lone surrogate is never coerced into a partition or digest."""
    with pytest.raises(shards.ShardError, match="partition: node ID is not strict UTF-8"):
        shards.shard_index("tests/t.py::\ud800")
    with pytest.raises(shards.ShardError, match="partition: node ID is not strict UTF-8"):
        shards.universe_digest(["ok", "tests/t.py::\udfff"])


def test_universe_rejects_duplicates() -> None:
    """A repeated node ID cannot enter a universe digest or worker record."""
    with pytest.raises(shards.ShardError, match="universe: duplicate node ID"):
        shards.universe_digest(["a", "b", "a"])
    with pytest.raises(shards.ShardError, match="universe: duplicate node ID"):
        shards.worker_record(["a", "a"], 0)


def test_worker_record_filters_in_original_order() -> None:
    """Kept IDs keep collection order; the record names index, universe and kept."""
    reversed_universe = list(reversed(UNIVERSE))
    kept, record = shards.worker_record(reversed_universe, 1)
    assert kept == [n for n in reversed_universe if _oracle_index(n) == 1]
    assert record == {
        "index": 1,
        "universe_count": len(UNIVERSE),
        "universe_digest": _oracle_digest(UNIVERSE),
        "kept": kept,
    }


# --- option grammar -------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "1"])
def test_index_grammar_accepts_exact_strings(value: str) -> None:
    """Only the exact strings 0 and 1 parse."""
    assert shards.parse_index(value) == int(value)


@pytest.mark.parametrize("value", ["2", " 0", "0 ", "00", "-0", "", "1\n", "０", 0, 1, True, None])
def test_index_grammar_rejects_everything_else(value: object) -> None:
    """Anything outside the closed grammar fails closed."""
    with pytest.raises(shards.ShardError, match="option: shard index"):
        shards.parse_index(value)


@pytest.mark.parametrize("value", [1, 4, shards.WORKER_CAP])
def test_xdist_request_accepts_explicit_integers(value: int) -> None:
    """An explicit positive integer request is the expected worker count."""
    assert shards.expected_workers(value) == value


@pytest.mark.parametrize(
    "value", [None, 0, -1, "auto", "logical", "4", True, 4.0, shards.WORKER_CAP + 1]
)
def test_xdist_request_rejects_absent_or_unresolved_values(value: object) -> None:
    """No xdist, zero, auto/logical, bools and non-ints are usage errors."""
    with pytest.raises(shards.ShardError, match="option: an explicit integer"):
        shards.expected_workers(value)


# --- lifecycle --------------------------------------------------------------

_ID = "tests/test_l.py::test_case"


@pytest.mark.parametrize(
    "records",
    [
        pytest.param(_passed(_ID), id="passed"),
        pytest.param(
            [_record(_ID, "setup", "skipped"), _record(_ID, "teardown", "passed")],
            id="setup-skip",
        ),
        pytest.param(
            [
                _record(_ID, "setup", "passed"),
                _record(_ID, "call", "skipped"),
                _record(_ID, "teardown", "passed"),
            ],
            id="call-skip",
        ),
        pytest.param(
            [
                _record(_ID, "setup", "passed"),
                _record(_ID, "call", "passed"),
                _record(_ID, "teardown", "skipped"),
            ],
            id="teardown-skip",
        ),
        pytest.param(
            [_record(_ID, "setup", "skipped", "why"), _record(_ID, "teardown", "passed")],
            id="setup-xfail",
        ),
        pytest.param(
            [
                _record(_ID, "setup", "passed"),
                _record(_ID, "call", "skipped", ""),
                _record(_ID, "teardown", "passed"),
            ],
            id="call-xfail-empty-reason",
        ),
        pytest.param(
            [
                _record(_ID, "setup", "passed"),
                _record(_ID, "call", "passed"),
                _record(_ID, "teardown", "skipped", "teardown xfail"),
            ],
            id="teardown-xfail",
        ),
        pytest.param(
            [
                _record(_ID, "setup", "passed"),
                _record(_ID, "call", "passed", "non-strict"),
                _record(_ID, "teardown", "passed"),
            ],
            id="non-strict-xpass",
        ),
    ],
)
def test_lifecycle_accepts_legitimate_outcomes(records: list[object]) -> None:
    """Skips, setup/call/teardown xfails and non-strict XPASS are preserved."""
    shards.validate_lifecycle([_ID], records)


def _lifecycle(*phases: tuple[object, ...]) -> list[object]:
    """Build records for ``_ID`` from (when, outcome[, wasxfail]) tuples."""
    return [
        _record(_ID, cast(str, phase[0]), cast(str, phase[1]), phase[2] if len(phase) > 2 else None)
        for phase in phases
    ]


_S, _C, _T = ("setup", "passed"), ("call", "passed"), ("teardown", "passed")
LIFECYCLE_RULES: dict[str, tuple[list[str], list[object], str]] = {
    "no-reports": ([_ID], [], "has no reports"),
    "setup-not-first": ([_ID], _lifecycle(_C, _S, _T), "leading setup"),
    "duplicate-setup": ([_ID], _lifecycle(_S, _S, _C, _T), "leading setup"),
    "missing-teardown": ([_ID], _lifecycle(_S, _C), "trailing teardown"),
    "teardown-before-call": ([_ID], _lifecycle(_S, _T, _C), "trailing teardown"),
    "duplicate-teardown": ([_ID], _lifecycle(_S, _C, _T, _T), "trailing teardown"),
    "passed-setup-no-call": ([_ID], _lifecycle(_S, _T), "without exactly one call"),
    "passed-setup-two-calls": ([_ID], _lifecycle(_S, _C, _C, _T), "without exactly one call"),
    "skipped-setup-with-call": (
        [_ID],
        _lifecycle(("setup", "skipped"), _C, _T),
        "skipped setup with a call",
    ),
    "failed-setup": ([_ID], _lifecycle(("setup", "failed"), _T), "setup failed"),
    "failed-call-or-strict-xpass": ([_ID], _lifecycle(_S, ("call", "failed"), _T), "call failed"),
    "failed-teardown": ([_ID], _lifecycle(_S, _C, ("teardown", "failed")), "teardown failed"),
    "crash-phase": ([_ID], _lifecycle(_S, ("???", "failed"), _T), "unknown phase"),
    "unknown-outcome": ([_ID], _lifecycle(_S, ("call", "error"), _T), "unknown outcome"),
    "xfail-on-passed-setup": ([_ID], _lifecycle(("setup", "passed", "r"), _C, _T), "ineligible"),
    "xfail-on-passed-teardown": (
        [_ID],
        _lifecycle(_S, _C, ("teardown", "passed", "r")),
        "ineligible",
    ),
    "non-string-xfail": ([_ID], _lifecycle(_S, ("call", "skipped", 1), _T), "must be a string"),
    "outside-record": (
        [_ID],
        [*_lifecycle(_S, _C, _T), *_passed("tests/test_l.py::other")],
        "outside the shard",
    ),
    "empty-kept": ([], [], "no kept test"),
    "duplicate-kept": ([_ID, _ID], _lifecycle(_S, _C, _T), "kept repeats"),
    "extra-key": ([_ID], [{**_record(_ID, *_S), "longrepr": "x"}, *_lifecycle(_C, _T)], "keys"),
    "missing-key": ([_ID], [{"nodeid": _ID, "when": "setup"}, *_lifecycle(_C, _T)], "keys"),
    "non-object-record": ([_ID], ["setup", *_lifecycle(_C, _T)], "must be an object"),
    "non-string-nodeid": ([_ID], [{**_record(_ID, *_S), "nodeid": 7}], "must be a string"),
    "empty-nodeid": ([_ID], [{**_record(_ID, *_S), "nodeid": ""}], "length is out of range"),
    "surrogate-xfail": ([_ID], _lifecycle(("setup", "skipped", "\ud800"), _T), "not strict"),
}


@pytest.mark.parametrize("rule", sorted(LIFECYCLE_RULES))
def test_lifecycle_rule_matrix(rule: str) -> None:
    """Every closed lifecycle rule rejects its violation with a named check (G7)."""
    kept, records, message = LIFECYCLE_RULES[rule]
    with pytest.raises(shards.ShardError, match=message):
        shards.validate_lifecycle(kept, records)


def test_lifecycle_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record count, node ID and xfail reason caps fail closed; real 65,671 bytes fit."""
    long_id = "tests/t.py::" + "x" * (65_671 - len("tests/t.py::"))
    assert len(long_id.encode()) == 65_671
    shards.validate_lifecycle([long_id], _passed(long_id))
    at_cap = "x" * shards.NODEID_CAP
    shards.validate_lifecycle([at_cap], _passed(at_cap))
    over_cap = "x" * (shards.NODEID_CAP + 1)
    with pytest.raises(shards.ShardError, match="caps: receipt.0. node ID length"):
        shards.validate_lifecycle([_ID], _passed(over_cap))
    reason = "r" * shards.WASXFAIL_CAP
    shards.validate_lifecycle([_ID], _lifecycle(_S, ("call", "skipped", reason), _T))
    with pytest.raises(shards.ShardError, match="caps: receipt.1. xfail reason"):
        shards.validate_lifecycle([_ID], _lifecycle(_S, ("call", "skipped", reason + "r"), _T))
    monkeypatch.setattr(shards, "RECORD_CAP", 2)
    with pytest.raises(shards.ShardError, match="caps: too many report records"):
        shards.validate_lifecycle([_ID], _passed(_ID))


# --- controller receipt -----------------------------------------------------


def _observation(index: int = 0, workers: int = 2) -> dict[str, object]:
    """Return keyword arguments for a valid controller observation."""
    kept, record = shards.worker_record(UNIVERSE, index)
    return {
        "index": index,
        "exitstatus": 0,
        "workers": workers,
        "collections": [(f"gw{number}", list(kept)) for number in range(workers)],
        "gw0_records": [("gw0", record)],
        "records": [r for nodeid in kept for r in _passed(nodeid)],
        "collect_failures": 0,
        "internal_errors": 0,
        "node_errors": 0,
    }


def _build(**changes: object) -> dict[str, object]:
    """Build a receipt from a valid observation with keyword overrides."""
    observation = _observation(cast(int, changes.pop("index", 0)))
    observation.update(changes)
    return shards.build_receipt(**observation)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("index", [0, 1])
def test_build_receipt_accepts_a_complete_observation(index: int) -> None:
    """A complete observation yields the closed receipt."""
    receipt = _build(index=index)
    assert receipt == {
        "schema": shards.RECEIPT_SCHEMA,
        "partition": shards.PARTITION,
        "shard_count": 2,
        "index": index,
        "exitstatus": 0,
        "workers": {"expected": 2},
        "universe": {"count": len(UNIVERSE), "digest": _oracle_digest(UNIVERSE)},
        "kept": KEPT[index],
        "receipt": [r for nodeid in KEPT[index] for r in _passed(nodeid)],
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"exitstatus": 1}, "exit status was not zero"),
        ({"exitstatus": 5}, "exit status was not zero"),
        ({"collect_failures": 1}, "collection report failed"),
        ({"internal_errors": 1}, "internal error"),
        ({"node_errors": 1}, "went down with an error"),
    ],
)
def test_controller_rejects_nonzero_exit_and_errors(
    changes: dict[str, object], message: str
) -> None:
    """Exit status, collection, internal and node-down errors block the receipt (G8)."""
    with pytest.raises(shards.ShardError, match=message):
        _build(**changes)


def test_controller_rejects_disagreeing_or_unexpected_workers() -> None:
    """Workers must be exactly gw0..gwN-1 once each and agree with gw0 (G5)."""
    kept = KEPT[0]
    for collections in (
        [("gw0", kept)],
        [("gw0", kept), ("gw1", kept), ("gw2", kept)],
        [("gw0", kept), ("gw2", kept)],
        [("gw0", kept), ("gw0", kept)],
        [("gw0", kept), ("?", kept)],
    ):
        with pytest.raises(shards.ShardError, match="exactly gw0..gwN-1"):
            _build(collections=collections)
    for disagreeing in (kept[:-1], list(reversed(kept)), [*kept, KEPT[1][0]]):
        with pytest.raises(shards.ShardError, match="collection 1 disagrees"):
            _build(collections=[("gw0", kept), ("gw1", disagreeing)])


def test_controller_requires_single_gw0_record_matching_observed() -> None:
    """Exactly one well-formed gw0 record, for this index, equal to observed (G6)."""
    _, record = shards.worker_record(UNIVERSE, 0)
    for gw0_records in ([], [("gw0", record), ("gw0", record)], [("gw1", record)]):
        with pytest.raises(shards.ShardError, match="exactly one gw0 shard record"):
            _build(gw0_records=gw0_records)
    _, other_index = shards.worker_record(UNIVERSE, 1)
    with pytest.raises(shards.ShardError, match="gw0 record index differs"):
        _build(gw0_records=[("gw0", other_index)])
    with pytest.raises(shards.ShardError, match="keys are not exact"):
        _build(gw0_records=[("gw0", {**record, "extra": 1})])
    with pytest.raises(shards.ShardError, match="worker record has a non-string key"):
        _build(gw0_records=[("gw0", {**record, 1: "x"})])
    with pytest.raises(shards.ShardError, match="worker record kept repeats a node ID"):
        _build(gw0_records=[("gw0", {**record, "kept": [KEPT[0][0], KEPT[0][0]]})])
    with pytest.raises(shards.ShardError, match="kept exceeds the universe count"):
        _build(gw0_records=[("gw0", {**record, "universe_count": 1})])
    with pytest.raises(shards.ShardError, match="collection 0 disagrees"):
        _build(gw0_records=[("gw0", {**record, "kept": KEPT[0][:-1]})])
    with pytest.raises(shards.ShardError, match="caps: worker record kept"):
        _build(
            workers=1,
            collections=[("gw0", list[str]())],
            gw0_records=[("gw0", {**record, "kept": [str(n) for n in range(100_001)]})],
        )


def test_controller_rejects_kept_member_of_other_shard() -> None:
    """A kept ID with the other index is rejected even if workers agree."""
    kept = [*KEPT[0], KEPT[1][0]]
    _, record = shards.worker_record(UNIVERSE, 0)
    with pytest.raises(shards.ShardError, match=r"partition: kept\[\d+\] belongs"):
        _build(
            collections=[("gw0", kept), ("gw1", kept)],
            gw0_records=[("gw0", {**record, "kept": kept})],
            records=[r for nodeid in kept for r in _passed(nodeid)],
        )


def test_controller_runs_the_lifecycle_validator() -> None:
    """Missing execution despite agreement is rejected (G7 at the controller)."""
    with pytest.raises(shards.ShardError, match="has no reports"):
        _build(records=[])


# --- strict reader ----------------------------------------------------------

STRICT_READER_CASES: dict[str, tuple[bytes, str]] = {
    "duplicate-key": (b'{"a": 1, "a": 2}', "duplicate object key"),
    "nan": (b'{"a": NaN}', "non-finite constant"),
    "infinity": (b"[Infinity]", "non-finite constant"),
    "negative-infinity": (b"[-Infinity]", "non-finite constant"),
    "float": (b'{"a": 1.0}', "floats are not permitted"),
    "exponent": (b"[1e3]", "floats are not permitted"),
    "escaped-surrogate-value": (b'["\\ud800"]', "not strict UTF-8"),
    "escaped-surrogate-key": (b'{"\\udfff": 1}', "not strict UTF-8"),
    "invalid-utf8": (b'["\xff"]', "payload is not strict UTF-8"),
    "bom": (b'\xef\xbb\xbf{"a": 1}', "byte-order mark"),
    "depth-7": (b"[[[[[[[1]]]]]]]", "nesting is too deep"),
    "recursion": (b"[" * 100_000 + b"]" * 100_000, "nesting is too deep"),
    "invalid-json": (b'{"a": }', "not valid JSON"),
    "huge-integer": (b"[" + b"9" * 5_000 + b"]", "not valid JSON"),
}


@pytest.mark.parametrize("case", sorted(STRICT_READER_CASES))
def test_strict_reader_rejects_unsafe_payloads(case: str) -> None:
    """Each strict-reader hook fails closed with a named check (G16)."""
    data, message = STRICT_READER_CASES[case]
    with pytest.raises(shards.ShardError, match=message):
        shards.parse_strict_json(data)


def test_strict_reader_accepts_the_depth_limit_and_unicode() -> None:
    """Depth six and strict non-ASCII text are accepted unchanged."""
    assert shards.parse_strict_json(b"[[[[[[1]]]]]]") == [[[[[[1]]]]]]
    assert shards.parse_strict_json('{"é": "ß"}'.encode()) == {"é": "ß"}


def test_read_regular_bounded_requires_a_real_bounded_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing, symlinked, non-regular, empty and over-cap inputs fail closed."""
    real = tmp_path / "real"
    real.write_bytes(b"12345")
    assert shards.read_regular_bounded(real, 5, "probe") == b"12345"
    with pytest.raises(shards.ShardError, match="caps: probe exceeds its cap"):
        shards.read_regular_bounded(real, 4, "probe")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(shards.ShardError, match="probe: file is missing or a symlink"):
        shards.read_regular_bounded(link, 5, "probe")
    with pytest.raises(shards.ShardError, match="probe: file is missing or a symlink"):
        shards.read_regular_bounded(tmp_path / "absent", 5, "probe")
    with pytest.raises(shards.ShardError, match="probe: not a regular file"):
        shards.read_regular_bounded(tmp_path, 5, "probe")
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    with pytest.raises(shards.ShardError, match="probe: file is empty"):
        shards.read_regular_bounded(empty, 5, "probe")


@pytest.mark.parametrize(
    ("cap_name", "what"), [("COVERAGE_CAP", "coverage"), ("MANIFEST_CAP", "m")]
)
def test_real_caps_reject_one_byte_over_with_a_sparse_probe(
    tmp_path: Path, cap_name: str, what: str
) -> None:
    """The frozen 64 MiB / 32 MiB caps reject cap + 1 without writing that data."""
    cap = cast(int, getattr(shards, cap_name))
    assert cap == {"COVERAGE_CAP": 64, "MANIFEST_CAP": 32}[cap_name] * 1024 * 1024
    sparse = tmp_path / "sparse"
    with sparse.open("wb") as handle:
        handle.truncate(cap + 1)
    with pytest.raises(shards.ShardError, match=f"caps: {what} exceeds its cap"):
        shards.read_regular_bounded(sparse, cap, what)


# --- identity ---------------------------------------------------------------


def _env(**changes: str) -> dict[str, str]:
    """Return a valid normal-mode Actions environment with overrides."""
    env = {
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_WORKFLOW_SHA": HEAD,
        "GITHUB_SHA": HEAD,
    }
    env.update(changes)
    return env


def test_identity_normal_mode_requires_equal_shas() -> None:
    """Normal mode binds workflow, HEAD and GITHUB_SHA to one commit (G14)."""
    context = shards.identity_context(_env(), "normal", None, HEAD)
    assert context == shards.IdentityContext(REPOSITORY, 123456, 2, HEAD, HEAD, "normal")
    for env, head in (
        (_env(GITHUB_SHA=OTHER_SHA), HEAD),
        (_env(GITHUB_WORKFLOW_SHA=OTHER_SHA, GITHUB_SHA=OTHER_SHA), HEAD),
        (_env(), OTHER_SHA),
    ):
        with pytest.raises(shards.ShardError, match="normal mode requires"):
            shards.identity_context(env, "normal", None, head)
    with pytest.raises(shards.ShardError, match="normal mode forbids"):
        shards.identity_context(_env(), "normal", HEAD, HEAD)


def test_identity_benchmark_mode_records_distinct_shas_without_github_sha() -> None:
    """Benchmark mode needs the literal target at HEAD and a distinct workflow SHA."""
    env = _env(GITHUB_WORKFLOW_SHA=OTHER_SHA, GITHUB_SHA="not-a-sha")
    context = shards.identity_context(env, "benchmark", HEAD, HEAD)
    assert (context.workflow_sha, context.target_sha, context.mode) == (
        OTHER_SHA,
        HEAD,
        "benchmark",
    )
    for expected, message in ((None, "requires a 40-hex"), ("ABC", "requires a 40-hex")):
        with pytest.raises(shards.ShardError, match=message):
            shards.identity_context(env, "benchmark", expected, HEAD)
    with pytest.raises(shards.ShardError, match="expected target differs from HEAD"):
        shards.identity_context(env, "benchmark", OTHER_SHA, HEAD)
    with pytest.raises(shards.ShardError, match="must differ from the target"):
        shards.identity_context(_env(), "benchmark", HEAD, HEAD)
    with pytest.raises(shards.ShardError, match="unknown identity mode"):
        shards.identity_context(_env(), "other", None, HEAD)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"GITHUB_REPOSITORY": ""}, "GITHUB_REPOSITORY"),
        ({"GITHUB_REPOSITORY": "owner"}, "GITHUB_REPOSITORY"),
        ({"GITHUB_REPOSITORY": "owner/name/x"}, "GITHUB_REPOSITORY"),
        ({"GITHUB_REPOSITORY": "owner/.."}, "GITHUB_REPOSITORY"),
        ({"GITHUB_REPOSITORY": "-owner/name"}, "GITHUB_REPOSITORY"),
        ({"GITHUB_RUN_ID": "0"}, "GITHUB_RUN_ID"),
        ({"GITHUB_RUN_ID": "012"}, "GITHUB_RUN_ID"),
        ({"GITHUB_RUN_ID": "+12"}, "GITHUB_RUN_ID"),
        ({"GITHUB_RUN_ID": "1.0"}, "GITHUB_RUN_ID"),
        ({"GITHUB_RUN_ATTEMPT": "0"}, "GITHUB_RUN_ATTEMPT"),
        ({"GITHUB_RUN_ATTEMPT": ""}, "GITHUB_RUN_ATTEMPT"),
        ({"GITHUB_WORKFLOW_SHA": HEAD.upper()}, "GITHUB_WORKFLOW_SHA"),
        ({"GITHUB_WORKFLOW_SHA": HEAD[:-1]}, "GITHUB_WORKFLOW_SHA"),
    ],
)
def test_identity_grammar_fails_closed(changes: dict[str, str], message: str) -> None:
    """Every identity field has a closed grammar."""
    with pytest.raises(shards.ShardError, match=message):
        shards.identity_context(_env(**changes), "normal", None, HEAD)
    with pytest.raises(shards.ShardError, match="HEAD is not 40-hex"):
        shards.identity_context(_env(), "normal", None, "HEAD")


def _completed(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
    """Return a fake completed git process."""
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr="")


def test_read_head_sha_requires_one_exact_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """git must succeed and print exactly one 40-hex line."""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake(stdout: str, returncode: int = 0) -> Callable[..., object]:
        def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((argv, kwargs))
            return _completed(returncode, stdout)

        return run

    monkeypatch.setattr(subprocess, "run", fake(HEAD + "\n"))
    assert shards.read_head_sha() == HEAD
    assert calls[0][0] == ["git", "rev-parse", "--verify", "HEAD"]
    assert calls[0][1]["timeout"] == 30 and "shell" not in calls[0][1]
    for stdout, returncode in ((HEAD + "\n", 1), ("x" * 40, 0), (HEAD + "\n\n", 0), ("", 0)):
        monkeypatch.setattr(subprocess, "run", fake(stdout, returncode))
        with pytest.raises(shards.ShardError, match="git HEAD is not a 40-hex commit"):
            shards.read_head_sha()
    for error in (OSError("no git"), subprocess.TimeoutExpired(["git"], 30)):

        def raising(*_args: object, _error: BaseException = error, **_kwargs: object) -> None:
            raise _error

        monkeypatch.setattr(subprocess, "run", raising)
        with pytest.raises(shards.ShardError, match="git rev-parse could not run"):
            shards.read_head_sha()


# --- receipt, finalize, verify ------------------------------------------------


def _context(attempt: int = 1, **changes: object) -> shards.IdentityContext:
    """Return a normal-mode identity context."""
    context = shards.IdentityContext(REPOSITORY, 123456, attempt, HEAD, HEAD, "normal")
    return replace(context, **changes)  # pyright: ignore[reportArgumentType]


def _write_receipt(directory: Path, shard: int, **changes: object) -> Path:
    """Write a valid receipt for ``shard`` with optional top-level overrides."""
    receipt = {**_build(index=shard), **changes}
    path = directory / f"receipt-{shard}.json"
    shards.write_receipt(path, receipt)
    return path


def _make_bundles(
    root: Path, contexts: tuple[shards.IdentityContext, shards.IdentityContext] | None = None
) -> Path:
    """Finalize two real bundles under ``root/ordinary-shards``."""
    bundle_root = root / "ordinary-shards"
    bundle_root.mkdir(parents=True)
    for index in (0, 1):
        coverage_file = root / f".coverage.ordinary-shard-{index}"
        coverage_file.write_bytes(f"coverage-bytes-{index}".encode())
        shards.finalize(
            index=index,
            receipt_path=_write_receipt(root, index),
            coverage_path=coverage_file,
            bundle=bundle_root / f"ordinary-shard-{index}",
            context=(contexts or (_context(), _context()))[index],
        )
    return bundle_root


def _manifest(bundle_root: Path, index: int) -> dict[str, object]:
    """Load one bundle manifest."""
    path = bundle_root / f"ordinary-shard-{index}" / "manifest.json"
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _rewrite(bundle_root: Path, index: int, edit: Callable[[dict[str, object]], object]) -> None:
    """Rewrite one manifest in place through ``edit``."""
    manifest = _manifest(bundle_root, index)
    edit(manifest)
    path = bundle_root / f"ordinary-shard-{index}" / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _verify(bundle_root: Path, context: shards.IdentityContext | None = None) -> None:
    """Verify the bundles against a fresh combine-target path."""
    shards.verify(bundle_root, context or _context(), bundle_root.parent / ".coverage.ordinary")


def test_finalize_writes_an_exact_two_file_bundle(tmp_path: Path) -> None:
    """finalize binds the receipt and exact coverage bytes into a closed manifest."""
    bundle_root = _make_bundles(tmp_path)
    for index in (0, 1):
        directory = bundle_root / f"ordinary-shard-{index}"
        assert sorted(os.listdir(directory)) == ["coverage.data", "manifest.json"]
        data = f"coverage-bytes-{index}".encode()
        assert (directory / "coverage.data").read_bytes() == data
        manifest = _manifest(bundle_root, index)
        assert manifest == {
            "schema": shards.MANIFEST_SCHEMA,
            "partition": shards.PARTITION,
            "shard_count": 2,
            "index": index,
            "identity": {
                "repository": REPOSITORY,
                "run_id": 123456,
                "run_attempt": 1,
                "workflow_sha": HEAD,
                "target_sha": HEAD,
                "mode": "normal",
            },
            "workers": {"expected": 2},
            "universe": {"count": len(UNIVERSE), "digest": _oracle_digest(UNIVERSE)},
            "kept": KEPT[index],
            "receipt": [r for nodeid in KEPT[index] for r in _passed(nodeid)],
            "coverage": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        }
    _verify(bundle_root)


def test_finalize_hashes_exactly_the_bytes_it_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupting the source after the read cannot change the bundle (G12)."""
    coverage_file = tmp_path / "coverage"
    coverage_file.write_bytes(b"original-bytes")
    original = shards.read_regular_bounded

    def read_then_corrupt(path: Path, cap: int, what: str) -> bytes:
        data = original(path, cap, what)
        if what == "coverage":
            path.write_bytes(b"corrupted-after-read")
        return data

    monkeypatch.setattr(shards, "read_regular_bounded", read_then_corrupt)
    bundle = tmp_path / "bundle"
    shards.finalize(
        index=0,
        receipt_path=_write_receipt(tmp_path, 0),
        coverage_path=coverage_file,
        bundle=bundle,
        context=_context(),
    )
    assert (bundle / "coverage.data").read_bytes() == b"original-bytes"
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["coverage"] == {
        "size": len(b"original-bytes"),
        "sha256": hashlib.sha256(b"original-bytes").hexdigest(),
    }


@pytest.mark.parametrize(
    ("receipt_changes", "index", "message"),
    [
        ({"index": 1}, 0, "receipt: index differs"),
        ({"exitstatus": 1}, 0, "receipt exitstatus"),
        ({"schema": "v2"}, 0, "schema or partition is not v1"),
        ({"partition": "other"}, 0, "schema or partition is not v1"),
        ({"shard_count": 3}, 0, "receipt shard_count"),
        ({"extra": True}, 0, "receipt keys are not exact"),
        ({"workers": {"expected": 0}}, 0, "receipt workers"),
        ({"universe": {"count": 1, "digest": "0" * 64}}, 0, "kept exceeds the universe"),
        ({"universe": {"count": 16, "digest": "A" * 64}}, 0, "not a lowercase hex digest"),
        ({"kept": [*KEPT[0], KEPT[1][0]]}, 0, "belongs to the other shard"),
        ({"receipt": []}, 0, "has no reports"),
        ({"kept": []}, 0, "receipt kept is empty"),
    ],
)
def test_finalize_rejects_an_invalid_receipt(
    tmp_path: Path, receipt_changes: dict[str, object], index: int, message: str
) -> None:
    """finalize re-validates the receipt and publishes nothing on failure (G10)."""
    receipt = _write_receipt(tmp_path, index, **receipt_changes)
    coverage_file = tmp_path / "coverage"
    coverage_file.write_bytes(b"data")
    with pytest.raises(shards.ShardError, match=message):
        shards.finalize(
            index=0,
            receipt_path=receipt,
            coverage_path=coverage_file,
            bundle=tmp_path / "bundle",
            context=_context(),
        )
    assert not (tmp_path / "bundle").exists()


def test_finalize_rejects_a_bad_coverage_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing, symlinked, empty or over-cap coverage publishes nothing (G11)."""
    receipt = _write_receipt(tmp_path, 0)
    real = tmp_path / "real"
    real.write_bytes(b"12345")
    link = tmp_path / "link"
    link.symlink_to(real)
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    monkeypatch.setattr(shards, "COVERAGE_CAP", 4)
    for coverage_file, message in (
        (tmp_path / "absent", "missing or a symlink"),
        (link, "missing or a symlink"),
        (empty, "file is empty"),
        (real, "caps: coverage exceeds its cap"),
    ):
        with pytest.raises(shards.ShardError, match=message):
            shards.finalize(
                index=0,
                receipt_path=receipt,
                coverage_path=coverage_file,
                bundle=tmp_path / "bundle",
                context=_context(),
            )
        assert not (tmp_path / "bundle").exists()


def test_finalize_requires_a_fresh_bundle_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing bundle path, or any extra file appearing, fails closed (G13)."""
    receipt = _write_receipt(tmp_path, 0)
    coverage_file = tmp_path / "coverage"
    coverage_file.write_bytes(b"data")
    existing = tmp_path / "existing"
    existing.mkdir()
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    for bundle in (existing, dangling):
        with pytest.raises(shards.ShardError, match="bundle: directory already exists"):
            shards.finalize(
                index=0,
                receipt_path=receipt,
                coverage_path=coverage_file,
                bundle=bundle,
                context=_context(),
            )
    assert os.listdir(existing) == []
    original = shards._write_new_file  # pyright: ignore[reportPrivateUsage]

    def write_with_intruder(path: Path, data: bytes) -> None:
        original(path, data)
        if path.name == "manifest.json":
            (path.parent / "intruder").write_bytes(b"x")

    monkeypatch.setattr(shards, "_write_new_file", write_with_intruder)
    with pytest.raises(shards.ShardError, match="files are not exactly"):
        shards.finalize(
            index=0,
            receipt_path=receipt,
            coverage_path=coverage_file,
            bundle=tmp_path / "fresh",
            context=_context(),
        )


def test_finalize_and_receipt_serialization_respect_the_manifest_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest that would exceed the cap is never written."""
    receipt = _write_receipt(tmp_path, 0)
    coverage_file = tmp_path / "coverage"
    coverage_file.write_bytes(b"data")
    monkeypatch.setattr(shards, "MANIFEST_CAP", receipt.stat().st_size + 8)
    with pytest.raises(shards.ShardError, match="caps: serialized payload exceeds"):
        shards.finalize(
            index=0,
            receipt_path=receipt,
            coverage_path=coverage_file,
            bundle=tmp_path / "bundle",
            context=_context(),
        )
    assert not (tmp_path / "bundle").exists()


def test_write_receipt_is_atomic_and_refuses_an_existing_target(tmp_path: Path) -> None:
    """The receipt is written canonically, once, with no temporary left behind."""
    receipt = _build(index=1)
    target = tmp_path / "receipt.json"
    shards.write_receipt(target, receipt)
    assert sorted(os.listdir(tmp_path)) == ["receipt.json"]
    assert shards.parse_strict_json(target.read_bytes()) == receipt
    with pytest.raises(shards.ShardError, match="receipt: target already exists"):
        shards.write_receipt(target, receipt)
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "nowhere")
    with pytest.raises(shards.ShardError, match="receipt: target already exists"):
        shards.write_receipt(dangling, receipt)
    blocked = tmp_path / "blocked.json"
    (tmp_path / f".blocked.json.{os.getpid()}.tmp").write_bytes(b"x")
    with pytest.raises(shards.ShardError, match="could not be created exclusively"):
        shards.write_receipt(blocked, receipt)
    assert not blocked.exists()


def test_verify_accepts_bundles_from_earlier_attempts(tmp_path: Path) -> None:
    """A retried shard may pair attempt 1 with attempt 2 under current attempt 2."""
    bundle_root = _make_bundles(tmp_path, (_context(attempt=1), _context(attempt=2)))
    _verify(bundle_root, _context(attempt=2))


def test_verify_rejects_future_attempt(tmp_path: Path) -> None:
    """A bundle from a later attempt than the current one fails closed (G24)."""
    bundle_root = _make_bundles(tmp_path, (_context(attempt=3), _context(attempt=2)))
    with pytest.raises(shards.ShardError, match="bundle is from a future attempt"):
        _verify(bundle_root, _context(attempt=2))


@pytest.mark.parametrize(
    ("context_changes", "message"),
    [
        ({"repository": "other/repo"}, "repository differs"),
        ({"run_id": 654321}, "run ID differs"),
        ({"workflow_sha": OTHER_SHA}, "workflow SHA differs"),
        ({"target_sha": OTHER_SHA}, "target SHA differs from HEAD"),
        ({"mode": "benchmark"}, "mode differs"),
    ],
)
def test_verify_rejects_wrong_identity(
    tmp_path: Path, context_changes: dict[str, object], message: str
) -> None:
    """Repository, run, workflow SHA, target SHA and mode must match (G24)."""
    bundle_root = _make_bundles(tmp_path)
    with pytest.raises(shards.ShardError, match=message):
        _verify(bundle_root, replace(_context(), **context_changes))  # pyright: ignore[reportArgumentType]


def test_verify_wrong_partition_member(tmp_path: Path) -> None:
    """An ID moved into the wrong shard is rejected even though the union holds (G20)."""
    bundle_root = _make_bundles(tmp_path)
    moved = KEPT[1][0]

    def add(manifest: dict[str, object]) -> None:
        manifest["kept"] = [*cast(list[str], manifest["kept"]), moved]
        manifest["receipt"] = [*cast(list[object], manifest["receipt"]), *_passed(moved)]

    def remove(manifest: dict[str, object]) -> None:
        manifest["kept"] = [n for n in cast(list[str], manifest["kept"]) if n != moved]
        manifest["receipt"] = [
            r for r in cast(list[dict[str, object]], manifest["receipt"]) if r["nodeid"] != moved
        ]

    _rewrite(bundle_root, 0, add)
    _rewrite(bundle_root, 1, remove)
    with pytest.raises(shards.ShardError, match=r"partition: shard 0 kept\[\d+\] belongs"):
        _verify(bundle_root)


def test_verify_missing_member(tmp_path: Path) -> None:
    """An ID dropped from its shard breaks the universe union (G22)."""
    bundle_root = _make_bundles(tmp_path)
    dropped = KEPT[0][0]

    def drop(manifest: dict[str, object]) -> None:
        manifest["kept"] = [n for n in cast(list[str], manifest["kept"]) if n != dropped]
        manifest["receipt"] = [
            r for r in cast(list[dict[str, object]], manifest["receipt"]) if r["nodeid"] != dropped
        ]

    _rewrite(bundle_root, 0, drop)
    with pytest.raises(shards.ShardError, match="union does not equal the collected universe"):
        _verify(bundle_root)


def test_verify_overlap(tmp_path: Path) -> None:
    """An ID present in both shards is rejected (G21, defence in depth)."""
    bundle_root = _make_bundles(tmp_path)
    copied = KEPT[1][0]

    def add(manifest: dict[str, object]) -> None:
        manifest["kept"] = [*cast(list[str], manifest["kept"]), copied]
        manifest["receipt"] = [*cast(list[object], manifest["receipt"]), *_passed(copied)]

    _rewrite(bundle_root, 0, add)
    with pytest.raises(shards.ShardError, match="partition"):
        _verify(bundle_root)


def test_verify_universe_mismatch(tmp_path: Path) -> None:
    """Shards must record one universe whose digest and count the union reproduces."""
    bundle_root = _make_bundles(tmp_path)

    def other_digest(manifest: dict[str, object]) -> None:
        manifest["universe"] = {"count": len(UNIVERSE), "digest": "0" * 64}

    _rewrite(bundle_root, 0, other_digest)
    with pytest.raises(shards.ShardError, match="record different universes"):
        _verify(bundle_root)
    _rewrite(bundle_root, 1, other_digest)
    with pytest.raises(shards.ShardError, match="union does not equal"):
        _verify(bundle_root)

    def larger_count(manifest: dict[str, object]) -> None:
        manifest["universe"] = {"count": len(UNIVERSE) + 1, "digest": _oracle_digest(UNIVERSE)}

    _rewrite(bundle_root, 0, larger_count)
    _rewrite(bundle_root, 1, larger_count)
    with pytest.raises(shards.ShardError, match="union does not equal"):
        _verify(bundle_root)


def test_verify_coverage_manifest_mismatch(tmp_path: Path) -> None:
    """Coverage bytes must match the manifest size and SHA-256 exactly (G25)."""
    bundle_root = _make_bundles(tmp_path)
    data = bundle_root / "ordinary-shard-1" / "coverage.data"
    original = data.read_bytes()
    data.write_bytes(original[:-1] + b"X")
    with pytest.raises(shards.ShardError, match="coverage: shard 1 bytes differ"):
        _verify(bundle_root)
    data.write_bytes(original + b"X")
    with pytest.raises(shards.ShardError, match="coverage: shard 1 bytes differ"):
        _verify(bundle_root)


def _layout_extra_file(root: Path) -> None:
    (root / "ordinary-shard-0" / "extra").write_bytes(b"x")


def _layout_missing_manifest(root: Path) -> None:
    (root / "ordinary-shard-1" / "manifest.json").unlink()


def _layout_symlinked_manifest(root: Path) -> None:
    manifest = root / "ordinary-shard-0" / "manifest.json"
    target = root.parent / "manifest-target.json"
    manifest.rename(target)
    manifest.symlink_to(target)


def _layout_coverage_directory(root: Path) -> None:
    data = root / "ordinary-shard-0" / "coverage.data"
    data.unlink()
    data.mkdir()


def _layout_symlinked_shard(root: Path) -> None:
    shard = root / "ordinary-shard-1"
    target = root.parent / "shard-target"
    shard.rename(target)
    shard.symlink_to(target)


def _layout_symlinked_root(root: Path) -> None:
    target = root.parent / "root-target"
    root.rename(target)
    root.symlink_to(target)


def _layout_extra_shard(root: Path) -> None:
    (root / "ordinary-shard-2").mkdir()


def _layout_missing_shard(root: Path) -> None:
    shutil.rmtree(root / "ordinary-shard-1")


def _layout_swapped_shards(root: Path) -> None:
    (root / "ordinary-shard-0").rename(root / "swap")
    (root / "ordinary-shard-1").rename(root / "ordinary-shard-0")
    (root / "swap").rename(root / "ordinary-shard-1")


LAYOUT_CASES: dict[str, tuple[Callable[[Path], None], str]] = {
    "extra-file": (_layout_extra_file, "files are not exactly"),
    "missing-manifest": (_layout_missing_manifest, "files are not exactly"),
    "symlinked-manifest": (_layout_symlinked_manifest, "not a regular file"),
    "coverage-directory": (_layout_coverage_directory, "not a regular file"),
    "symlinked-shard": (_layout_symlinked_shard, "shard 1 is not a real directory"),
    "symlinked-root": (_layout_symlinked_root, "bundle root is not a real directory"),
    "extra-shard": (_layout_extra_shard, "exactly the two shards"),
    "missing-shard": (_layout_missing_shard, "exactly the two shards"),
    "swapped-shards": (_layout_swapped_shards, "manifest index differs from its directory"),
}


@pytest.mark.parametrize("case", sorted(LAYOUT_CASES))
def test_verify_bundle_layout_exact(tmp_path: Path, case: str) -> None:
    """Exact directories and regular files only; no symlink anywhere (G26)."""
    bundle_root = _make_bundles(tmp_path)
    mutate, message = LAYOUT_CASES[case]
    mutate(bundle_root)
    with pytest.raises(shards.ShardError, match=message):
        _verify(bundle_root)


def test_verify_rejects_preexisting_ordinary_coverage(tmp_path: Path) -> None:
    """No stale or placeholder combine target may exist, even as a symlink (G27)."""
    bundle_root = _make_bundles(tmp_path)
    target = tmp_path / ".coverage.ordinary"
    for create in (
        lambda: target.write_bytes(b"stale"),
        lambda: target.symlink_to(tmp_path / "nowhere"),
    ):
        create()
        with pytest.raises(shards.ShardError, match="combine target already exists"):
            shards.verify(bundle_root, _context(), target)
        target.unlink()
    shards.verify(bundle_root, _context(), target)


SCHEMA_EDITS: dict[str, tuple[Callable[[dict[str, object]], object], str]] = {
    "extra-key": (lambda m: m.update({"extra": 1}), "manifest keys are not exact"),
    "missing-key": (lambda m: m.pop("workers"), "manifest keys are not exact"),
    "schema-v2": (lambda m: m.update({"schema": "v2"}), "schema or partition is not v1"),
    "partition": (lambda m: m.update({"partition": "x"}), "schema or partition is not v1"),
    "shard-count": (lambda m: m.update({"shard_count": 3}), "manifest shard_count"),
    "bool-index": (lambda m: m.update({"index": False}), "manifest index"),
    "string-index": (lambda m: m.update({"index": "0"}), "manifest index"),
    "bool-run-id": (
        lambda m: cast(dict[str, object], m["identity"]).update({"run_id": True}),
        "identity run_id",
    ),
    "identity-extra": (
        lambda m: cast(dict[str, object], m["identity"]).update({"path": "x"}),
        "identity keys are not exact",
    ),
    "repository-type": (
        lambda m: cast(dict[str, object], m["identity"]).update({"repository": 1}),
        "identity repository",
    ),
    "workflow-sha-case": (
        lambda m: cast(dict[str, object], m["identity"]).update({"workflow_sha": HEAD.upper()}),
        "identity workflow_sha",
    ),
    "workers-zero": (lambda m: m.update({"workers": {"expected": 0}}), "manifest workers"),
    "workers-bool": (lambda m: m.update({"workers": {"expected": True}}), "manifest workers"),
    "universe-over-count-cap": (
        lambda m: m.update({"universe": {"count": 100_001, "digest": "0" * 64}}),
        "manifest universe count",
    ),
    "kept-not-list": (lambda m: m.update({"kept": "x"}), "manifest kept must be an array"),
    "coverage-extra": (
        lambda m: cast(dict[str, object], m["coverage"]).update({"path": "x"}),
        "manifest coverage keys",
    ),
    "coverage-size-bool": (
        lambda m: cast(dict[str, object], m["coverage"]).update({"size": True}),
        "manifest coverage size",
    ),
    "coverage-sha-short": (
        lambda m: cast(dict[str, object], m["coverage"]).update({"sha256": "ab"}),
        "manifest coverage sha256",
    ),
    "not-an-object": (lambda m: m.clear(), "manifest keys are not exact"),
}


@pytest.mark.parametrize("case", sorted(SCHEMA_EDITS))
def test_manifest_schema_exact(tmp_path: Path, case: str) -> None:
    """The v1 manifest is closed: exact keys, exact int/str types, no bools (G17)."""
    bundle_root = _make_bundles(tmp_path)
    edit, message = SCHEMA_EDITS[case]
    _rewrite(bundle_root, 0, edit)
    with pytest.raises(shards.ShardError, match=message):
        _verify(bundle_root)


def test_verify_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Manifest and coverage caps are enforced on read (G15/G18)."""
    bundle_root = _make_bundles(tmp_path)
    manifest_size = (bundle_root / "ordinary-shard-0" / "manifest.json").stat().st_size
    with monkeypatch.context() as patch:
        patch.setattr(shards, "MANIFEST_CAP", manifest_size - 1)
        with pytest.raises(shards.ShardError, match="caps: manifest exceeds its cap"):
            _verify(bundle_root)
    with monkeypatch.context() as patch:
        patch.setattr(shards, "COVERAGE_CAP", len(b"coverage-bytes-0") - 1)
        with pytest.raises(shards.ShardError, match="manifest coverage size|caps: coverage"):
            _verify(bundle_root)
    _verify(bundle_root)


def test_diagnostics_never_echo_payload_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A workflow-command-shaped ID never reaches the CLI diagnostic."""
    hostile = "tests/x.py::test[\n::error::pwned]"
    bundle_root = _make_bundles(tmp_path)

    def add(manifest: dict[str, object]) -> None:
        manifest["kept"] = [*cast(list[str], manifest["kept"]), hostile]
        manifest["receipt"] = [*cast(list[object], manifest["receipt"]), *_passed(hostile)]

    _rewrite(bundle_root, _oracle_index(hostile) ^ 1, add)
    for name, value in _env(GITHUB_RUN_ATTEMPT="1").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(shards, "read_head_sha", lambda: HEAD)
    monkeypatch.chdir(tmp_path)
    assert shards.main(["verify", "--bundle-root", "ordinary-shards", "--identity-mode", "normal"])
    stderr = capsys.readouterr().err
    assert stderr.startswith("ordinary-shard: partition: shard")
    assert stderr.count("\n") == 1
    assert "pwned" not in stderr and "tests/" not in stderr and str(tmp_path) not in stderr


def test_main_round_trip_and_bounded_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI finalizes both shards, verifies them, and fails with one bounded line."""
    for name, value in _env(GITHUB_RUN_ATTEMPT="1").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(shards, "read_head_sha", lambda: HEAD)
    monkeypatch.chdir(tmp_path)
    Path("ordinary-shards").mkdir()
    for index in (0, 1):
        _write_receipt(tmp_path, index)
        Path(f".coverage.ordinary-shard-{index}").write_bytes(b"cov")
        argv = [
            "finalize",
            "--index",
            str(index),
            "--receipt",
            f"receipt-{index}.json",
            "--coverage",
            f".coverage.ordinary-shard-{index}",
            "--bundle",
            f"ordinary-shards/ordinary-shard-{index}",
            "--identity-mode",
            "normal",
        ]
        assert shards.main(argv) == 0
    verify_argv = ["verify", "--bundle-root", "ordinary-shards", "--identity-mode", "normal"]
    assert shards.main(verify_argv) == 0
    assert capsys.readouterr().err.splitlines() == [
        "ordinary-shard: finalize passed",
        "ordinary-shard: finalize passed",
        "ordinary-shard: verify passed",
    ]
    Path(".coverage.ordinary").write_bytes(b"stale")
    assert shards.main(verify_argv) == 1
    assert capsys.readouterr().err == (
        "ordinary-shard: coverage: the ordinary combine target already exists\n"
    )
    monkeypatch.setenv("GITHUB_SHA", OTHER_SHA)
    assert shards.main(verify_argv) == 1
    assert "normal mode requires" in capsys.readouterr().err
    with pytest.raises(SystemExit) as usage:
        shards.main(["finalize", "--index", "2"])
    assert usage.value.code == 2


# --- shim (fakes and fresh import) -----------------------------------------


@dataclass
class _FakeOption:
    """Option namespace for a fake pytest config."""

    ordinary_shard_index: object = "0"
    ordinary_shard_receipt: object = "receipt.json"
    numprocesses: object = 2


@dataclass
class _FakePluginManager:
    """Records registered plugins."""

    registered: list[tuple[object, str]] = field(default_factory=list[tuple[object, str]])

    def register(self, plugin_object: object, name: str) -> None:
        """Record one registration."""
        self.registered.append((plugin_object, name))


@dataclass
class _FakeHook:
    """Records deselected items."""

    deselected: list[object] = field(default_factory=list[object])

    def pytest_deselected(self, items: list[object]) -> None:
        """Record one deselection batch."""
        self.deselected.extend(items)


class _FakeConfig:
    """Minimal stand-in for ``pytest.Config``."""

    def __init__(self, directory: Path, option: _FakeOption, worker: str | None = None) -> None:
        self.option = option
        self.pluginmanager = _FakePluginManager()
        self.hook = _FakeHook()
        self.invocation_params = SimpleNamespace(dir=directory)
        if worker is not None:
            self.workerinput = {"workerid": worker}
            self.workeroutput: dict[str, object] = {}

    def getoption(self, name: str) -> object:
        """Return one option value."""
        return getattr(self.option, name)


def _drive_cmdline(config: _FakeConfig) -> object:
    """Run the shim's ``pytest_cmdline_main`` wrapper around a fake result."""
    wrapper = plugin.pytest_cmdline_main(cast(pytest.Config, config))
    next(wrapper)
    with pytest.raises(StopIteration) as finished:
        wrapper.send(0)
    return cast(object, finished.value.value)


def _controller(config: _FakeConfig) -> plugin._Controller:  # pyright: ignore[reportPrivateUsage]
    """Return the controller plugin the shim registered."""
    ((registered, name),) = config.pluginmanager.registered
    assert name == "ordinary-shard-controller"
    return cast(plugin._Controller, registered)  # pyright: ignore[reportPrivateUsage]


def test_shim_registers_a_controller_with_the_explicit_worker_count(tmp_path: Path) -> None:
    """The controller gets index, invocation-relative receipt and exact workers."""
    config = _FakeConfig(tmp_path, _FakeOption(ordinary_shard_index="1", numprocesses=4))
    assert _drive_cmdline(config) == 0
    controller = _controller(config)
    assert (controller.index, controller.receipt_path, controller.workers) == (
        1,
        tmp_path / "receipt.json",
        4,
    )
    worker = _FakeConfig(tmp_path, _FakeOption(numprocesses=None), worker="gw1")
    assert _drive_cmdline(worker) == 0
    assert worker.pluginmanager.registered == []


@pytest.mark.parametrize("value", ["2", " 0", None, 0])
def test_shim_index_option_closed_grammar(tmp_path: Path, value: object) -> None:
    """Controller and workers reject an index outside the closed grammar (G2)."""
    for worker in (None, "gw0"):
        config = _FakeConfig(tmp_path, _FakeOption(ordinary_shard_index=value), worker=worker)
        with pytest.raises(pytest.UsageError, match="ordinary-shard: option: shard index"):
            _drive_cmdline(config)


@pytest.mark.parametrize("value", [None, 0, "auto", "logical", True])
def test_shim_rejects_non_integer_or_absent_xdist_request(tmp_path: Path, value: object) -> None:
    """Only an explicit integer ``-n`` is accepted on the controller (G1)."""
    config = _FakeConfig(tmp_path, _FakeOption(numprocesses=value))
    with pytest.raises(pytest.UsageError, match="explicit integer xdist worker count"):
        _drive_cmdline(config)
    assert config.pluginmanager.registered == []


@pytest.mark.parametrize("value", [None, ""])
def test_shim_requires_a_receipt_path(tmp_path: Path, value: object) -> None:
    """The controller refuses to run without a receipt path."""
    config = _FakeConfig(tmp_path, _FakeOption(ordinary_shard_receipt=value))
    with pytest.raises(pytest.UsageError, match="receipt path is required"):
        _drive_cmdline(config)


@dataclass
class _FakeGroup:
    """Records options added to one option group."""

    options: list[tuple[str, dict[str, object]]] = field(
        default_factory=list[tuple[str, dict[str, object]]]
    )

    def addoption(self, name: str, **kwargs: object) -> None:
        """Record one option."""
        self.options.append((name, kwargs))


def test_shim_registers_exactly_two_options_without_defaults() -> None:
    """The shim adds only the index and receipt options, both defaulting to None."""
    group = _FakeGroup()
    groups: list[tuple[str, str]] = []

    def getgroup(name: str, description: str) -> _FakeGroup:
        groups.append((name, description))
        return group

    plugin.pytest_addoption(cast(pytest.Parser, SimpleNamespace(getgroup=getgroup)))
    assert groups == [("ordinary-shard", "ordinary CI shard (#1039)")]
    assert [(name, kwargs["dest"], kwargs["default"]) for name, kwargs in group.options] == [
        ("--ordinary-shard-index", "ordinary_shard_index", None),
        ("--ordinary-shard-receipt", "ordinary_shard_receipt", None),
    ]


def _items(nodeids: Sequence[str]) -> list[pytest.Item]:
    """Return fake items carrying only node IDs."""
    return [cast(pytest.Item, SimpleNamespace(nodeid=nodeid)) for nodeid in nodeids]


def test_shim_worker_filters_in_place_and_only_gw0_records(tmp_path: Path) -> None:
    """Workers keep this index in order, report the rest, and gw0 alone records."""
    for worker in ("gw0", "gw1"):
        config = _FakeConfig(tmp_path, _FakeOption(ordinary_shard_index="1"), worker=worker)
        items = _items(UNIVERSE)
        plugin.pytest_collection_modifyitems(cast(pytest.Config, config), items)
        assert [item.nodeid for item in items] == KEPT[1]
        assert [cast(pytest.Item, i).nodeid for i in config.hook.deselected] == KEPT[0]
        expected = (
            {} if worker == "gw1" else {"ordinary_shard_v1": shards.worker_record(UNIVERSE, 1)[1]}
        )
        assert config.workeroutput == expected
    only_kept = _FakeConfig(tmp_path, _FakeOption(ordinary_shard_index="1"), worker="gw1")
    items = _items(KEPT[1])
    plugin.pytest_collection_modifyitems(cast(pytest.Config, only_kept), items)
    assert only_kept.hook.deselected == []
    controller = _FakeConfig(tmp_path, _FakeOption())
    items = _items(UNIVERSE)
    plugin.pytest_collection_modifyitems(cast(pytest.Config, controller), items)
    assert [item.nodeid for item in items] == UNIVERSE


def _node(worker: str, workeroutput: object = None) -> object:
    """Return a fake xdist node."""
    node = SimpleNamespace(gateway=SimpleNamespace(id=worker))
    if workeroutput is not None:
        node.workeroutput = workeroutput
    return node


def _report(nodeid: str, when: str, outcome: str, **extra: object) -> pytest.TestReport:
    """Return a fake test report."""
    return cast(
        pytest.TestReport, SimpleNamespace(nodeid=nodeid, when=when, outcome=outcome, **extra)
    )


def _observe(config: _FakeConfig, index: int, *, crash: bool = False) -> plugin._Controller:  # pyright: ignore[reportPrivateUsage]
    """Drive a fake two-worker controller session through every observing hook."""
    _drive_cmdline(config)
    controller = _controller(config)
    kept, record = shards.worker_record(UNIVERSE, index)
    for worker in ("gw0", "gw1"):
        controller.pytest_xdist_node_collection_finished(_node(worker), kept)
    for nodeid in kept:
        controller.pytest_runtest_logreport(_report(nodeid, "setup", "passed"))
        controller.pytest_runtest_logreport(_report(nodeid, "call", "passed", wasxfail="nonstrict"))
        controller.pytest_runtest_logreport(_report(nodeid, "teardown", "passed"))
    controller.pytest_collectreport(cast(pytest.CollectReport, SimpleNamespace(failed=False)))
    controller.pytest_testnodedown(_node("gw0", {"ordinary_shard_v1": record}), None)
    controller.pytest_testnodedown(_node("gw1", {"exitstatus": 0}), "crashed" if crash else None)
    controller.pytest_testnodedown(SimpleNamespace(), None)
    return controller


def _session(exitstatus: int) -> pytest.Session:
    """Return a fake session carrying a mutable exit status."""
    return cast(pytest.Session, SimpleNamespace(exitstatus=exitstatus))


def test_shim_controller_writes_the_receipt_after_a_clean_session(tmp_path: Path) -> None:
    """Observed collections, reports and the gw0 record become the receipt."""
    config = _FakeConfig(tmp_path, _FakeOption(ordinary_shard_index="1"))
    controller = _observe(config, 1)
    session = _session(0)
    controller.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0
    receipt = shards.parse_strict_json((tmp_path / "receipt.json").read_bytes())
    assert cast(dict[str, object], receipt)["kept"] == KEPT[1]
    assert cast(dict[str, object], receipt)["receipt"] == [
        _record(nodeid, when, "passed", "nonstrict" if when == "call" else None)
        for nodeid in KEPT[1]
        for when in ("setup", "call", "teardown")
    ]


@pytest.mark.parametrize(
    ("break_session", "message"),
    [
        ("crash", "went down with an error"),
        ("collect", "collection report failed"),
        ("internal", "internal error"),
        ("extra-report", "outside the shard"),
        ("replacement", "exactly gw0..gwN-1"),
    ],
)
def test_shim_controller_refuses_and_forces_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], break_session: str, message: str
) -> None:
    """A violation writes nothing and turns a zero exit into a failure (G9)."""
    config = _FakeConfig(tmp_path, _FakeOption())
    controller = _observe(config, 0, crash=break_session == "crash")
    if break_session == "collect":
        controller.pytest_collectreport(cast(pytest.CollectReport, SimpleNamespace(failed=True)))
    elif break_session == "internal":
        controller.pytest_internalerror()
    elif break_session == "extra-report":
        controller.pytest_runtest_logreport(_report("::error::x", "setup", "passed"))
    elif break_session == "replacement":
        controller.pytest_xdist_node_collection_finished(_node("gw2"), KEPT[0])
    session = _session(0)
    controller.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert not (tmp_path / "receipt.json").exists()
    stderr = capsys.readouterr().err
    assert stderr.startswith("ordinary-shard: receipt refused: ")
    assert message in stderr and "::error" not in stderr and stderr.count("\n") == 1


def test_shim_controller_keeps_an_existing_failure_code(tmp_path: Path) -> None:
    """A non-zero exit is preserved, never rewritten, and no receipt is written."""
    controller = _observe(_FakeConfig(tmp_path, _FakeOption()), 0)
    session = _session(pytest.ExitCode.INTERRUPTED)
    controller.pytest_sessionfinish(session, pytest.ExitCode.INTERRUPTED)
    assert session.exitstatus == pytest.ExitCode.INTERRUPTED
    assert not (tmp_path / "receipt.json").exists()


def test_shim_and_helper_fresh_imports_execute_module_lines() -> None:
    """Late fresh in-process imports execute both modules' top-level lines (D-e)."""
    for path in (SCRIPTS / "ci_ordinary_shard_plugin.py", SCRIPTS / "ci_ordinary_shards.py"):
        name = f"_fresh_{path.stem}_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            del sys.modules[name]
        assert module.__file__ == str(path)
        assert callable(getattr(module, "pytest_addoption", None) or module.main)
    controller = plugin._Controller  # pyright: ignore[reportPrivateUsage]
    for hook, option in (
        (plugin.pytest_cmdline_main, "wrapper"),
        (plugin.pytest_collection_modifyitems, "trylast"),
        (controller.pytest_xdist_node_collection_finished, "optionalhook"),
        (controller.pytest_testnodedown, "optionalhook"),
        (controller.pytest_sessionfinish, "trylast"),
    ):
        options = cast(dict[str, object], getattr(hook, "pytest_impl"))  # noqa: B009
        assert options[option] is True


# --- real subprocess integration --------------------------------------------

_PYPROJECT = """\
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["scripts"]
markers = ["serial: deselected by the synthetic ordinary selection"]
"""

_BODY = """\
import hashlib
import os
import pathlib

import pytest


def _ran(request):
    nodeid = request.node.nodeid
    name = hashlib.sha256(nodeid.encode()).hexdigest()
    (pathlib.Path(os.environ["SHARD_BODY_DIR"]) / name).write_text(nodeid, encoding="utf-8")
"""

_CASES = (
    _BODY
    + """

@pytest.fixture
def skip_in_teardown():
    yield
    pytest.skip("teardown skip")


@pytest.fixture
def xfail_in_setup():
    pytest.xfail("setup xfail")


@pytest.fixture
def xfail_in_teardown():
    yield
    pytest.xfail("teardown xfail")


@pytest.mark.parametrize("number", range(8))
def test_pass(request, number):
    _ran(request)


@pytest.mark.skip(reason="setup skip")
def test_skip_setup(request):
    _ran(request)


def test_skip_call(request):
    _ran(request)
    pytest.skip("call skip")


def test_skip_teardown(request, skip_in_teardown):
    _ran(request)


def test_xfail_setup(request, xfail_in_setup):
    _ran(request)


@pytest.mark.xfail(reason="call xfail")
def test_xfail_call(request):
    _ran(request)
    assert False


def test_xfail_teardown(request, xfail_in_teardown):
    _ran(request)


@pytest.mark.xfail(reason="non-strict")
def test_xpass_nonstrict(request):
    _ran(request)


@pytest.mark.serial
def test_deselected_by_marker(request):
    _ran(request)
"""
)

_WITNESS = """\
import json
import os

_config = None


def pytest_configure(config):
    global _config
    _config = config


def _write(event):
    if not hasattr(_config, "workerinput"):
        with open(os.environ["SHARD_WITNESS"], "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\\n")


def pytest_xdist_node_collection_finished(node, ids):
    _write(["collection", node.gateway.id, list(ids)])


def pytest_runtest_logreport(report):
    reason = getattr(report, "wasxfail", None)
    _write(["report", report.nodeid, report.when, report.outcome, reason])
"""

_SKIP_PROTOCOL = "def pytest_runtest_protocol(item, nextitem):\n    return True\n"

_DUPLICATE_REPORTS = """\
_config = None


def pytest_configure(config):
    global _config
    _config = config


def pytest_runtest_logreport(report):
    if hasattr(_config, "workerinput") and report.when == "call" and not hasattr(report, "dup"):
        report.dup = True
        _config.hook.pytest_runtest_logreport(report=report)
"""


def _scrubbed_env(**extra: str) -> dict[str, str]:
    """Return the parent environment minus pytest, coverage and project controls."""
    prefixes = ("PYTEST_", "COV_CORE_", "COVERAGE_", "ROASTPILOT_", "FAKE_", "GIT_")
    removed = {"PYTHONPATH", "PYTHONSTARTUP", "BASH_ENV", "ENV", "OPENROUTER_API_KEY"}
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(prefixes) and name not in removed
    }
    env.update(PYTHONDONTWRITEBYTECODE="1", **extra)
    return env


@dataclass(frozen=True)
class _Project:
    """An owned synthetic pytest project outside the repository."""

    root: Path

    def run(
        self, *args: str, body_dir: str = "bodies", **env: str
    ) -> subprocess.CompletedProcess[str]:
        """Run pytest in the project with a scrubbed environment."""
        bodies = self.root / body_dir
        bodies.mkdir(exist_ok=True)
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
            cwd=self.root,
            env=_scrubbed_env(SHARD_BODY_DIR=str(bodies), **env),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def shard(self, index: int, *extra: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        """Run one shard exactly as CI does (two workers locally)."""
        receipt = self.root / f"receipt-{index}.json"
        result = self.run(
            "-n",
            "2",
            "--dist",
            "worksteal",
            "-p",
            "scripts.ci_ordinary_shard_plugin",
            "--ordinary-shard-index",
            str(index),
            "--ordinary-shard-receipt",
            receipt.name,
            "-m",
            "not serial",
            "--ignore=tests/test_ignored.py",
            *extra,
            body_dir=f"bodies-{index}",
        )
        return result, receipt


def _project(tmp_path: Path, files: dict[str, str]) -> _Project:
    """Create a synthetic project with the CI scripts copied in."""
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    for name in ("ci_ordinary_shards.py", "ci_ordinary_shard_plugin.py"):
        shutil.copyfile(SCRIPTS / name, root / "scripts" / name)
    for relative, text in files.items():
        (root / relative).write_text(text, encoding="utf-8")
    return _Project(root)


def _witness(path: Path) -> tuple[dict[str, list[str]], list[tuple[str, str, str, object]]]:
    """Read the witness plugin's collection and report events."""
    collections: dict[str, list[str]] = {}
    reports: list[tuple[str, str, str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = cast(list[object], json.loads(line))
        if event[0] == "collection":
            collections[cast(str, event[1])] = cast(list[str], event[2])
        else:
            reports.append(
                (cast(str, event[1]), cast(str, event[2]), cast(str, event[3]), event[4])
            )
    return collections, reports


def _bodies(directory: Path) -> set[str]:
    """Return the SHA-256 names of the node IDs whose bodies actually ran."""
    return {entry.name for entry in directory.iterdir()}


def _hashed(nodeids: set[str]) -> set[str]:
    """Return the body-record names the synthetic tests write for ``nodeids``."""
    return {hashlib.sha256(nodeid.encode()).hexdigest() for nodeid in nodeids}


@pytest.mark.slow
def test_real_shards_filter_after_marker_selection_in_original_order(tmp_path: Path) -> None:
    """Two real two-worker shards partition the unsharded run exactly (AC1/AC3).

    Reports, outcomes and independently recorded test-body executions match the
    unsharded run, including setup/call/teardown skips and xfails, a teardown
    xfail and a non-strict XPASS; marker-deselected and ignored items never enter
    the universe.
    """
    project = _project(
        tmp_path,
        {
            "tests/test_cases.py": _CASES,
            "tests/test_ignored.py": _BODY + "\n\ndef test_ignored(request):\n    _ran(request)\n",
            "witness_record.py": _WITNESS,
        },
    )
    witness = project.root / "witness.jsonl"
    unsharded = project.run(
        "-n",
        "2",
        "-p",
        "witness_record",
        "-m",
        "not serial",
        "--ignore=tests/test_ignored.py",
        body_dir="bodies-unsharded",
        SHARD_WITNESS=str(witness),
    )
    assert unsharded.returncode == 0, unsharded.stdout + unsharded.stderr
    collections, reports = _witness(witness)
    assert set(collections) == {"gw0", "gw1"}
    universe = collections["gw0"]
    assert len(universe) == 15
    assert "tests/test_cases.py::test_deselected_by_marker" not in universe
    assert not any(nodeid.startswith("tests/test_ignored.py") for nodeid in universe)

    kept_by_index: dict[int, list[str]] = {}
    for index in (0, 1):
        result, receipt_path = project.shard(index)
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = cast(dict[str, object], shards.parse_strict_json(receipt_path.read_bytes()))
        kept = cast(list[str], receipt["kept"])
        assert kept == [nodeid for nodeid in universe if _oracle_index(nodeid) == index]
        assert receipt["universe"] == {"count": len(universe), "digest": _oracle_digest(universe)}
        assert receipt["workers"] == {"expected": 2}
        observed = {
            (
                cast(str, r["nodeid"]),
                cast(str, r["when"]),
                cast(str, r["outcome"]),
                r.get("wasxfail"),
            )
            for r in cast(list[dict[str, object]], receipt["receipt"])
        }
        assert observed == {report for report in reports if report[0] in kept}
        setup_passed = {r[0] for r in observed if r[1] == "setup" and r[2] == "passed"}
        assert setup_passed and _bodies(project.root / f"bodies-{index}") == _hashed(setup_passed)
        kept_by_index[index] = kept

    assert kept_by_index[0] and kept_by_index[1]
    assert set(kept_by_index[0]).isdisjoint(kept_by_index[1])
    assert set(kept_by_index[0]) | set(kept_by_index[1]) == set(universe)
    assert _bodies(project.root / "bodies-0") | _bodies(project.root / "bodies-1") == _bodies(
        project.root / "bodies-unsharded"
    )
    assert (
        "tests/test_cases.py::test_xfail_teardown",
        "teardown",
        "skipped",
        "teardown xfail",
    ) in reports
    assert ("tests/test_cases.py::test_xpass_nonstrict", "call", "passed", "non-strict") in reports

    bundle_root = project.root / "ordinary-shards"
    bundle_root.mkdir()
    for index in (0, 1):
        data = project.root / f"coverage-{index}"
        data.write_bytes(b"cov")
        shards.finalize(
            index=index,
            receipt_path=project.root / f"receipt-{index}.json",
            coverage_path=data,
            bundle=bundle_root / f"ordinary-shard-{index}",
            context=_context(),
        )
    shards.verify(bundle_root, _context(), project.root / ".coverage.ordinary")


def _negative(tmp_path: Path, body: str, bad: str, **files: str) -> tuple[_Project, int]:
    """Create a project whose ``bad`` test lands in the returned shard index."""
    source = _BODY + "\n\ndef test_ok_one(request):\n    _ran(request)\n\n\n"
    source += "def test_ok_two(request):\n    _ran(request)\n\n\n" + body
    project = _project(tmp_path, {"tests/test_cases.py": source, **files})
    return project, _oracle_index(f"tests/test_cases.py::{bad}")


def _assert_refused(
    result: subprocess.CompletedProcess[str], receipt: Path, reason: str | None
) -> None:
    """Assert a non-zero exit, no receipt, and a bounded payload-free reason."""
    assert result.returncode != 0, result.stdout + result.stderr
    assert not receipt.exists()
    refusals = [
        line for line in result.stderr.splitlines() if line.startswith("ordinary-shard: receipt")
    ]
    assert len(refusals) == 1, result.stderr
    assert "tests/" not in refusals[0] and "::" not in refusals[0]
    if reason is not None:
        assert reason in refusals[0], refusals[0]


@pytest.mark.slow
def test_real_skipped_execution_despite_exit_zero(tmp_path: Path) -> None:
    """Workers that skip the run protocol exit 0 alone, but the shard refuses (G7/G9)."""
    project, index = _negative(
        tmp_path,
        "def test_target(request):\n    _ran(request)\n",
        "test_target",
        **{"skip_protocol.py": _SKIP_PROTOCOL},
    )
    premise = project.run("-n", "2", "-p", "skip_protocol")
    assert premise.returncode == 0, premise.stdout + premise.stderr
    result, receipt = project.shard(index, "-p", "skip_protocol")
    _assert_refused(result, receipt, "has no reports")
    assert _bodies(project.root / f"bodies-{index}") == set()


@pytest.mark.slow
def test_real_duplicate_reports_rejected(tmp_path: Path) -> None:
    """A re-emitted call report exits 0 alone, but the shard refuses (G7/G9)."""
    project, index = _negative(
        tmp_path,
        "def test_target(request):\n    _ran(request)\n",
        "test_target",
        **{"duplicate_reports.py": _DUPLICATE_REPORTS},
    )
    premise = project.run("-n", "2", "-p", "duplicate_reports")
    assert premise.returncode == 0, premise.stdout + premise.stderr
    result, receipt = project.shard(index, "-p", "duplicate_reports")
    _assert_refused(result, receipt, "without exactly one call")


@pytest.mark.slow
def test_real_worker_crash_leaves_no_receipt(tmp_path: Path) -> None:
    """A crashed worker can never yield a receipt (G5/G9)."""
    project, index = _negative(
        tmp_path, "def test_crash(request):\n    os._exit(3)\n", "test_crash"
    )
    result, receipt = project.shard(index)
    assert "crashed while running" in result.stdout
    _assert_refused(result, receipt, None)


@pytest.mark.slow
@pytest.mark.parametrize(
    "body",
    [
        pytest.param("def test_bad(request):\n    assert False\n", id="failed"),
        pytest.param(
            "@pytest.mark.xfail(strict=True)\ndef test_bad(request):\n    pass\n",
            id="strict-xpass",
        ),
    ],
)
def test_real_failed_outcomes_leave_no_receipt(tmp_path: Path, body: str) -> None:
    """A failed test or strict XPASS fails the shard and publishes nothing."""
    project, index = _negative(tmp_path, body, "test_bad")
    result, receipt = project.shard(index)
    _assert_refused(result, receipt, "exit status was not zero")


@pytest.mark.slow
def test_real_cli_is_stdlib_only_and_combines_shard_coverage(tmp_path: Path) -> None:
    """The verifier runs with no site-packages; coverage combines both shards (AC6).

    ``-I -S`` removes site-packages and the environment from the CLI, so a third-party
    import would fail. Two real coverage databases then combine into
    ``.coverage.ordinary`` with every measured line from both shards.
    """
    work = tmp_path / "work"
    work.mkdir()
    git_env = _scrubbed_env(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    for argv in (
        ["git", "init", "-q"],
        ["git", "commit", "-q", "--allow-empty", "--no-verify", "-m", "probe"],
    ):
        subprocess.run(argv, cwd=work, env=git_env, check=True, capture_output=True, timeout=60)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        env=git_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()

    bundle_root = work / "ordinary-shards"
    bundle_root.mkdir()
    measured = {0: {"/src/a.py": [1, 2]}, 1: {"/src/a.py": [3], "/src/b.py": [7]}}
    context = _context(workflow_sha=head, target_sha=head)
    for index in (0, 1):
        data = coverage.CoverageData(basename=str(tmp_path / f"shard-{index}.cov"))
        data.add_lines(measured[index])
        data.write()
        shards.finalize(
            index=index,
            receipt_path=_write_receipt(tmp_path, index),
            coverage_path=tmp_path / f"shard-{index}.cov",
            bundle=bundle_root / f"ordinary-shard-{index}",
            context=context,
        )
    env = _scrubbed_env(
        GITHUB_REPOSITORY=REPOSITORY,
        GITHUB_RUN_ID="123456",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_WORKFLOW_SHA=head,
        GITHUB_SHA=head,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    verify = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(SCRIPTS / "ci_ordinary_shards.py"),
            "verify",
            "--bundle-root",
            "ordinary-shards",
            "--identity-mode",
            "normal",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr
    assert verify.stderr == "ordinary-shard: verify passed\n"
    combine = subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "combine",
            "--keep",
            "--data-file=.coverage.ordinary",
            "ordinary-shards/ordinary-shard-0/coverage.data",
            "ordinary-shards/ordinary-shard-1/coverage.data",
        ],
        cwd=work,
        env=_scrubbed_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert combine.returncode == 0, combine.stdout + combine.stderr
    combined = coverage.CoverageData(basename=str(work / ".coverage.ordinary"))
    combined.read()
    assert {path: sorted(combined.lines(path) or []) for path in combined.measured_files()} == {
        "/src/a.py": [1, 2, 3],
        "/src/b.py": [7],
    }
    assert (bundle_root / "ordinary-shard-0" / "coverage.data").exists()
