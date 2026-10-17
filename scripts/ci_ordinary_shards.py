"""Partition, receipt, and bundle rules for the sharded ordinary CI lane (#1039).

This module is deliberately standard-library only: the coverage job that verifies
the shard bundles installs nothing but ``coverage``. It never imports pytest,
xdist, or ``roastpilot_agent``. The pytest hook shim
(``scripts/ci_ordinary_shard_plugin.py``) observes a sharded run and delegates
every rule here; the ``finalize`` and ``verify`` subcommands are the CI steps
that publish and check the two per-index bundles.

Every check fails closed. Diagnostics name the failed check and, at most, a
position or count; they never echo node IDs, paths, or other payload strings,
so a hostile string cannot inject a GitHub Actions workflow command.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

RECEIPT_SCHEMA = "roastpilot-ordinary-shard-receipt-v1"
MANIFEST_SCHEMA = "roastpilot-ordinary-shard-v1"
PARTITION = "sha256-be-mod2-v1"
SHARD_COUNT = 2
MIB = 1024 * 1024
MANIFEST_CAP = 32 * MIB
NODEID_CAP = MIB
WASXFAIL_CAP = MIB
COUNT_CAP = 100_000
RECORD_CAP = 3 * COUNT_CAP
COVERAGE_CAP = 64 * MIB
MAX_DEPTH = 6
WORKER_CAP = 1024
WORKER_RECORD_KEY = "ordinary_shard_v1"
BUNDLE_FILES = frozenset({"manifest.json", "coverage.data"})
SHARD_DIRECTORIES = ("ordinary-shard-0", "ordinary-shard-1")
ORDINARY_COVERAGE = ".coverage.ordinary"
IDENTITY_MODES = ("normal", "benchmark")
_WHEN = ("setup", "call", "teardown")
_OUTCOMES = frozenset({"passed", "skipped", "failed"})
_SHA1_HEX = re.compile(r"[0-9a-f]{40}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_DECIMAL = re.compile(r"[1-9][0-9]{0,18}")
_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_GIT_TIMEOUT_SECONDS = 30


class ShardError(Exception):
    """A fail-closed shard check failed; the message is bounded and payload-free."""


def _fail(reason: str) -> NoReturn:
    """Raise the one bounded diagnostic for a failed check."""
    raise ShardError(reason)


def encode_nodeid(nodeid: str) -> bytes:
    """Return the strict UTF-8 bytes of one node ID.

    Args:
        nodeid: A pytest node ID.

    Returns:
        The node ID's strict UTF-8 encoding.

    Raises:
        ShardError: The node ID contains a lone surrogate.
    """
    try:
        return nodeid.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("partition: node ID is not strict UTF-8")


def shard_index(nodeid: str) -> int:
    """Return the v1 shard index of one node ID.

    Args:
        nodeid: A pytest node ID.

    Returns:
        SHA-256 of the strict UTF-8 node ID, read as an unsigned big-endian
        integer, modulo two.
    """
    digest = hashlib.sha256(encode_nodeid(nodeid)).digest()
    return int.from_bytes(digest, "big") % SHARD_COUNT


def universe_digest(nodeids: Sequence[str]) -> str:
    """Return the canonical, order-independent digest of a collection universe.

    Node IDs are sorted by their strict UTF-8 bytes; each contributes an 8-byte
    big-endian length prefix followed by its bytes.

    Args:
        nodeids: The collected node IDs, in any order.

    Returns:
        The lowercase hexadecimal SHA-256 digest.

    Raises:
        ShardError: A node ID repeats or is not strict UTF-8.
    """
    encoded = sorted(encode_nodeid(nodeid) for nodeid in nodeids)
    digest = hashlib.sha256()
    previous: bytes | None = None
    for value in encoded:
        if value == previous:
            _fail("universe: duplicate node ID")
        previous = value
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def parse_index(value: object) -> int:
    """Parse the closed ``--ordinary-shard-index`` grammar.

    Args:
        value: The raw option value.

    Returns:
        The shard index, 0 or 1.

    Raises:
        ShardError: The value is not exactly the string ``"0"`` or ``"1"``.
    """
    if type(value) is not str or value not in {"0", "1"}:
        _fail("option: shard index must be exactly 0 or 1")
    return int(value)


def expected_workers(value: object) -> int:
    """Validate the active xdist worker request.

    Args:
        value: The parsed, unresolved ``numprocesses`` option.

    Returns:
        The exact expected worker count.

    Raises:
        ShardError: The request is absent, not an exact int, or outside 1..cap.
    """
    if type(value) is not int or not 1 <= value <= WORKER_CAP:
        _fail("option: an explicit integer xdist worker count is required")
    return value


def worker_record(universe: Sequence[str], index: int) -> tuple[list[str], dict[str, object]]:
    """Filter one worker's collected universe into its shard.

    Args:
        universe: The worker's node IDs after marker/path selection, in order.
        index: This run's shard index.

    Returns:
        The kept node IDs in original order, and the gw0 record payload.

    Raises:
        ShardError: A node ID repeats or is not strict UTF-8.
    """
    digest = universe_digest(universe)
    kept = [nodeid for nodeid in universe if shard_index(nodeid) == index]
    record: dict[str, object] = {
        "index": index,
        "universe_count": len(universe),
        "universe_digest": digest,
        "kept": list(kept),
    }
    return kept, record


def _exact_keys(
    value: object,
    required: frozenset[str],
    where: str,
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Narrow a value to a mapping with exactly the required (and optional) keys."""
    if type(value) is not dict:
        _fail(f"schema: {where} must be an object")
    mapping: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if type(key) is not str:
            _fail(f"schema: {where} has a non-string key")
        mapping[key] = item
    keys = set(mapping)
    if not required <= keys or not keys <= required | optional:
        _fail(f"schema: {where} keys are not exact")
    return mapping


def _exact_int(value: object, where: str, low: int, high: int) -> int:
    """Narrow a value to an exact int (never a bool) inside a closed range."""
    if type(value) is not int or not low <= value <= high:
        _fail(f"schema: {where} must be an integer in range")
    return value


def _exact_str(value: object, where: str) -> str:
    """Narrow a value to an exact str."""
    if type(value) is not str:
        _fail(f"schema: {where} must be a string")
    return value


def _exact_list(value: object, where: str, cap: int) -> list[object]:
    """Narrow a value to an exact list of at most ``cap`` items."""
    if type(value) is not list:
        _fail(f"schema: {where} must be an array")
    items = list(cast(list[object], value))
    if len(items) > cap:
        _fail(f"caps: {where} has too many items")
    return items


def _hex(value: object, where: str, pattern: re.Pattern[str]) -> str:
    """Narrow a value to a lowercase hexadecimal digest string."""
    text = _exact_str(value, where)
    if pattern.fullmatch(text) is None:
        _fail(f"schema: {where} is not a lowercase hex digest")
    return text


def _nodeid(value: object, where: str) -> str:
    """Narrow a value to a node ID within the 1 byte .. 1 MiB UTF-8 cap."""
    text = _exact_str(value, where)
    if not 1 <= len(encode_nodeid(text)) <= NODEID_CAP:
        _fail(f"caps: {where} node ID length is out of range")
    return text


def _nodeid_list(value: object, where: str) -> list[str]:
    """Narrow a value to a duplicate-free, non-empty list of capped node IDs."""
    items = _exact_list(value, where, COUNT_CAP)
    nodeids = [_nodeid(item, f"{where}[{position}]") for position, item in enumerate(items)]
    if not nodeids:
        _fail(f"execution: {where} is empty")
    if len(set(nodeids)) != len(nodeids):
        _fail(f"partition: {where} repeats a node ID")
    return nodeids


def _record_fields(record: object, position: int) -> tuple[str, str, str, bool]:
    """Validate one lifecycle record's exact shape."""
    where = f"receipt[{position}]"
    mapping = _exact_keys(
        record,
        frozenset({"nodeid", "when", "outcome"}),
        where,
        optional=frozenset({"wasxfail"}),
    )
    nodeid = _nodeid(mapping["nodeid"], where)
    when = _exact_str(mapping["when"], where)
    outcome = _exact_str(mapping["outcome"], where)
    if when not in _WHEN:
        _fail(f"lifecycle: {where} has an unknown phase")
    if outcome not in _OUTCOMES:
        _fail(f"lifecycle: {where} has an unknown outcome")
    has_wasxfail = "wasxfail" in mapping
    if has_wasxfail:
        reason = _exact_str(mapping["wasxfail"], where)
        try:
            size = len(reason.encode("utf-8", "strict"))
        except UnicodeEncodeError:
            _fail(f"schema: {where} xfail reason is not strict UTF-8")
        if size > WASXFAIL_CAP:
            _fail(f"caps: {where} xfail reason is too long")
    return nodeid, when, outcome, has_wasxfail


def _check_test_lifecycle(phases: list[tuple[str, str, bool]], position: int) -> None:
    """Check one kept test's ordered phase reports against the closed v1 rules."""
    where = f"kept[{position}]"
    if not phases:
        _fail(f"lifecycle: {where} has no reports")
    whens = [when for when, _, _ in phases]
    if whens.count("setup") != 1 or whens[0] != "setup":
        _fail(f"lifecycle: {where} lacks exactly one leading setup")
    if whens.count("teardown") != 1 or whens[-1] != "teardown":
        _fail(f"lifecycle: {where} lacks exactly one trailing teardown")
    setup_outcome = phases[0][1]
    calls = whens.count("call")
    if setup_outcome == "passed" and calls != 1:
        _fail(f"lifecycle: {where} passed setup without exactly one call")
    if setup_outcome == "skipped" and calls != 0:
        _fail(f"lifecycle: {where} skipped setup with a call")
    for when, outcome, has_wasxfail in phases:
        if outcome == "failed":
            _fail(f"lifecycle: {where} {when} failed")
        if has_wasxfail and not (outcome == "skipped" or (when, outcome) == ("call", "passed")):
            _fail(f"lifecycle: {where} carries an xfail reason on an ineligible phase")


def validate_lifecycle(kept: Sequence[str], records: Sequence[object]) -> None:
    """Validate the controller-observed report lifecycle of every kept test.

    Each kept test needs exactly one setup and one teardown in order, with
    exactly one call after a passed setup and none after a skipped one. Skips,
    xfails (setup, call, or teardown) and non-strict XPASS are accepted; any
    failure, unknown phase or outcome, outside or duplicate record, or missing
    test is rejected.

    Args:
        kept: The shard's kept node IDs.
        records: Report records with keys ``nodeid``, ``when``, ``outcome`` and
            optional ``wasxfail``.

    Raises:
        ShardError: Any lifecycle rule is violated.
    """
    if not kept:
        _fail("execution: no kept test")
    if len(records) > RECORD_CAP:
        _fail("caps: too many report records")
    phases: dict[str, list[tuple[str, str, bool]]] = {nodeid: [] for nodeid in kept}
    if len(phases) != len(kept):
        _fail("partition: kept repeats a node ID")
    for position, record in enumerate(records):
        nodeid, when, outcome, has_wasxfail = _record_fields(record, position)
        test_phases = phases.get(nodeid)
        if test_phases is None:
            _fail(f"lifecycle: receipt[{position}] names a test outside the shard")
        test_phases.append((when, outcome, has_wasxfail))
    for position, nodeid in enumerate(kept):
        _check_test_lifecycle(phases[nodeid], position)


def _worker_record_fields(record: object, index: int) -> tuple[int, str, list[str]]:
    """Validate the gw0 worker record and return its universe and kept IDs."""
    mapping = _exact_keys(
        record,
        frozenset({"index", "universe_count", "universe_digest", "kept"}),
        "worker record",
    )
    if _exact_int(mapping["index"], "worker record index", 0, 1) != index:
        _fail("worker: gw0 record index differs from the option")
    count = _exact_int(mapping["universe_count"], "worker record count", 1, COUNT_CAP)
    digest = _hex(mapping["universe_digest"], "worker record digest", _SHA256_HEX)
    kept = _nodeid_list(mapping["kept"], "worker record kept")
    if len(kept) > count:
        _fail("universe: kept exceeds the universe count")
    return count, digest, kept


def build_receipt(
    *,
    index: int,
    exitstatus: int,
    workers: int,
    collections: Sequence[tuple[str, Sequence[str]]],
    gw0_records: Sequence[tuple[str, object]],
    records: Sequence[object],
    collect_failures: int,
    internal_errors: int,
    node_errors: int,
) -> dict[str, object]:
    """Check everything the controller observed and build the receipt.

    Args:
        index: This run's shard index.
        exitstatus: The pytest session exit status.
        workers: The expected xdist worker count.
        collections: ``(worker id, node IDs)`` per observed worker collection.
        gw0_records: ``(worker id, record)`` per harvested shard record.
        records: Controller-observed report records.
        collect_failures: Count of failed collect reports.
        internal_errors: Count of internal errors.
        node_errors: Count of worker node-down errors.

    Returns:
        The receipt mapping.

    Raises:
        ShardError: Any controller, worker, partition, or lifecycle check fails.
    """
    if exitstatus != 0:
        _fail("pytest: exit status was not zero")
    if collect_failures:
        _fail("pytest: a collection report failed")
    if internal_errors:
        _fail("pytest: an internal error occurred")
    if node_errors:
        _fail("worker: a worker went down with an error")
    expected_ids = Counter(f"gw{number}" for number in range(workers))
    if Counter(worker for worker, _ in collections) != expected_ids:
        _fail("worker: observed workers are not exactly gw0..gwN-1, once each")
    if len(gw0_records) != 1 or gw0_records[0][0] != "gw0":
        _fail("worker: exactly one gw0 shard record is required")
    count, digest, kept = _worker_record_fields(gw0_records[0][1], index)
    for position, (_, ids) in enumerate(collections):
        if list(ids) != kept:
            _fail(f"worker: collection {position} disagrees with the gw0 record")
    for position, nodeid in enumerate(kept):
        if shard_index(nodeid) != index:
            _fail(f"partition: kept[{position}] belongs to the other shard")
    validate_lifecycle(kept, records)
    return {
        "schema": RECEIPT_SCHEMA,
        "partition": PARTITION,
        "shard_count": SHARD_COUNT,
        "index": index,
        "exitstatus": 0,
        "workers": {"expected": workers},
        "universe": {"count": count, "digest": digest},
        "kept": kept,
        "receipt": list(records),
    }


def _canonical_json(value: object) -> bytes:
    """Serialize a validated payload deterministically as strict UTF-8 JSON."""
    text = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    data = text.encode("utf-8", "strict")
    if len(data) > MANIFEST_CAP:
        _fail("caps: serialized payload exceeds the manifest cap")
    return data


def _write_new_file(path: Path, data: bytes) -> None:
    """Create a new regular file exclusively and write all bytes durably."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags, 0o644)
    except OSError:
        _fail("write: output file could not be created exclusively")
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(descriptor)
    except OSError:
        _fail("write: output file could not be written")
    finally:
        _close(descriptor, "write")


def _close(descriptor: int, what: str) -> None:
    """Close a descriptor this module opened, mapping failure to a bounded refusal."""
    try:
        os.close(descriptor)
    except OSError:
        _fail(f"{what}: file could not be closed")


def write_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    """Write the receipt atomically; refuse an existing target.

    Args:
        path: The receipt path.
        receipt: The receipt mapping from :func:`build_receipt`.

    Raises:
        ShardError: The target exists or the receipt cannot be written.
    """
    data = _canonical_json(dict(receipt))
    if _lstat_mode(path) is not None:
        _fail("receipt: target already exists")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    _write_new_file(temporary, data)
    try:
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        _fail("receipt: could not be published")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, rejecting any duplicate key."""
    mapping: dict[str, object] = {}
    for key, value in pairs:
        if key in mapping:
            _fail("json: duplicate object key")
        mapping[key] = value
    return mapping


def _reject_constant(_constant: str) -> NoReturn:
    """Reject ``NaN``, ``Infinity`` and ``-Infinity``."""
    _fail("json: non-finite constant")


def _reject_float(_text: str) -> NoReturn:
    """Reject every JSON float; the schemas are integer-only."""
    _fail("json: floats are not permitted")


def _check_tree(value: object, depth: int) -> None:
    """Bound container depth and require every string to be strict UTF-8.

    ``depth`` is the container depth of ``value`` if it is a list or object; the
    top-level container is depth 1 and scalars add no depth.
    """
    kind = type(value)
    if kind is str:
        try:
            cast(str, value).encode("utf-8", "strict")
        except UnicodeEncodeError:
            _fail("json: string is not strict UTF-8")
        return
    if kind is not list and kind is not dict:
        return
    if depth > MAX_DEPTH:
        _fail("json: nesting is too deep")
    if kind is list:
        for item in cast(list[object], value):
            _check_tree(item, depth + 1)
    else:
        for key, item in cast(dict[object, object], value).items():
            _check_tree(key, depth + 1)
            _check_tree(item, depth + 1)


def parse_strict_json(data: bytes) -> object:
    """Parse bounded payload bytes as strict JSON.

    Args:
        data: The raw payload bytes.

    Returns:
        The parsed JSON value.

    Raises:
        ShardError: The bytes carry a BOM, are not strict UTF-8 or valid JSON,
            repeat a key, contain a float or non-finite constant, nest too deeply,
            or contain a string that is not strict UTF-8.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        _fail("json: byte-order mark is not permitted")
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _fail("json: payload is not strict UTF-8")
    try:
        value: object = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
        )
    except RecursionError:
        _fail("json: nesting is too deep")
    except ValueError:
        _fail("json: payload is not valid JSON")
    _check_tree(value, 1)
    return value


def read_regular_bounded(path: Path, cap: int, what: str) -> bytes:
    """Read a non-empty regular file without following a final symlink.

    Args:
        path: The file path.
        cap: The inclusive byte cap.
        what: The check name used in diagnostics.

    Returns:
        The exact bytes read.

    Raises:
        ShardError: The file is missing, a symlink, not regular, unreadable, empty,
            or over cap.
    """
    # O_NONBLOCK: opening a FIFO (or other special file) must never wait for a
    # writer; it is rejected below as non-regular. Regular-file reads are unaffected.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail(f"{what}: file is missing or a symlink")
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _fail(f"{what}: not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(cap + 1)
    except OSError:
        _fail(f"{what}: file could not be read")
    finally:
        _close(descriptor, what)
    if len(data) > cap:
        _fail(f"caps: {what} exceeds its cap")
    if not data:
        _fail(f"{what}: file is empty")
    return data


@dataclass(frozen=True)
class IdentityContext:
    """The current run identity a bundle is produced in or verified against."""

    repository: str
    run_id: int
    run_attempt: int
    workflow_sha: str
    target_sha: str
    mode: str


def read_head_sha() -> str:
    """Return the checked-out ``HEAD`` commit SHA from git.

    Returns:
        The 40-character lowercase hexadecimal SHA.

    Raises:
        ShardError: git fails, times out, or prints anything else.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        # Only stdout is identity input: stderr (e.g. a localized warning) is never
        # decoded, so it can neither widen nor narrow acceptance.
        head = result.stdout.decode("ascii", "strict").removesuffix("\n")
    except UnicodeError:
        _fail("identity: git HEAD is not a 40-hex commit")
    except (OSError, subprocess.TimeoutExpired):
        _fail("identity: git rev-parse could not run")
    if result.returncode != 0 or _SHA1_HEX.fullmatch(head) is None:
        _fail("identity: git HEAD is not a 40-hex commit")
    return head


def identity_context(
    env: Mapping[str, str], mode: str, expected_target_sha: str | None, head: str
) -> IdentityContext:
    """Derive and check the current identity from the Actions environment.

    Normal mode requires ``GITHUB_WORKFLOW_SHA == HEAD == GITHUB_SHA`` and
    forbids an expected target. Benchmark mode requires the expected target to
    equal ``HEAD`` and differ from the workflow SHA; it never reads
    ``GITHUB_SHA``.

    Args:
        env: The process environment.
        mode: ``normal`` or ``benchmark``.
        expected_target_sha: The benchmark's literal tested-source SHA.
        head: The checked-out ``HEAD`` SHA.

    Returns:
        The validated identity context.

    Raises:
        ShardError: Any identity grammar or mode rule is violated.
    """
    repository = env.get("GITHUB_REPOSITORY", "")
    if _REPOSITORY.fullmatch(repository) is None or repository.endswith(("/.", "/..")):
        _fail("identity: GITHUB_REPOSITORY is not owner/name")
    run_id = env.get("GITHUB_RUN_ID", "")
    run_attempt = env.get("GITHUB_RUN_ATTEMPT", "")
    if _DECIMAL.fullmatch(run_id) is None:
        _fail("identity: GITHUB_RUN_ID is not a positive decimal")
    if _DECIMAL.fullmatch(run_attempt) is None:
        _fail("identity: GITHUB_RUN_ATTEMPT is not a positive decimal")
    workflow_sha = env.get("GITHUB_WORKFLOW_SHA", "")
    if _SHA1_HEX.fullmatch(workflow_sha) is None:
        _fail("identity: GITHUB_WORKFLOW_SHA is not 40-hex")
    if _SHA1_HEX.fullmatch(head) is None:
        _fail("identity: HEAD is not 40-hex")
    if mode == "normal":
        if expected_target_sha is not None:
            _fail("identity: normal mode forbids an expected target SHA")
        if not workflow_sha == head == env.get("GITHUB_SHA", ""):
            _fail("identity: normal mode requires workflow, HEAD and GITHUB_SHA to match")
    elif mode == "benchmark":
        if expected_target_sha is None or _SHA1_HEX.fullmatch(expected_target_sha) is None:
            _fail("identity: benchmark mode requires a 40-hex expected target SHA")
        if expected_target_sha != head:
            _fail("identity: benchmark expected target differs from HEAD")
        if workflow_sha == head:
            _fail("identity: benchmark workflow SHA must differ from the target")
    else:
        _fail("identity: unknown identity mode")
    return IdentityContext(repository, int(run_id), int(run_attempt), workflow_sha, head, mode)


def _validate_receipt(value: object, index: int) -> dict[str, object]:
    """Validate a receipt's closed schema, partition and lifecycle."""
    receipt = _exact_keys(
        value,
        frozenset(
            {
                "schema",
                "partition",
                "shard_count",
                "index",
                "exitstatus",
                "workers",
                "universe",
                "kept",
                "receipt",
            }
        ),
        "receipt",
    )
    if receipt["schema"] != RECEIPT_SCHEMA or receipt["partition"] != PARTITION:
        _fail("schema: receipt schema or partition is not v1")
    _exact_int(receipt["shard_count"], "receipt shard_count", SHARD_COUNT, SHARD_COUNT)
    if _exact_int(receipt["index"], "receipt index", 0, 1) != index:
        _fail("receipt: index differs from --index")
    _exact_int(receipt["exitstatus"], "receipt exitstatus", 0, 0)
    workers = _exact_keys(receipt["workers"], frozenset({"expected"}), "receipt workers")
    worker_count = _exact_int(workers["expected"], "receipt workers", 1, WORKER_CAP)
    universe = _exact_keys(receipt["universe"], frozenset({"count", "digest"}), "receipt universe")
    count = _exact_int(universe["count"], "receipt universe count", 1, COUNT_CAP)
    digest = _hex(universe["digest"], "receipt universe digest", _SHA256_HEX)
    kept = _nodeid_list(receipt["kept"], "receipt kept")
    if len(kept) > count:
        _fail("universe: kept exceeds the universe count")
    for position, nodeid in enumerate(kept):
        if shard_index(nodeid) != index:
            _fail(f"partition: kept[{position}] belongs to the other shard")
    records = _exact_list(receipt["receipt"], "receipt records", RECORD_CAP)
    validate_lifecycle(kept, records)
    return {
        "workers": {"expected": worker_count},
        "universe": {"count": count, "digest": digest},
        "kept": kept,
        "receipt": records,
    }


def finalize(
    *,
    index: int,
    receipt_path: Path,
    coverage_path: Path,
    bundle: Path,
    context: IdentityContext,
) -> None:
    """Publish one fresh two-file shard bundle from a verified receipt.

    Args:
        index: The shard index.
        receipt_path: The receipt written by the pytest shim.
        coverage_path: The closed shard coverage database.
        bundle: The bundle directory to create; it must not exist.
        context: The current, validated run identity.

    Raises:
        ShardError: Any receipt, coverage, identity, or bundle check fails.
    """
    receipt = _validate_receipt(
        parse_strict_json(read_regular_bounded(receipt_path, MANIFEST_CAP, "receipt")), index
    )
    coverage = read_regular_bounded(coverage_path, COVERAGE_CAP, "coverage")
    manifest: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "partition": PARTITION,
        "shard_count": SHARD_COUNT,
        "index": index,
        "identity": {
            "repository": context.repository,
            "run_id": context.run_id,
            "run_attempt": context.run_attempt,
            "workflow_sha": context.workflow_sha,
            "target_sha": context.target_sha,
            "mode": context.mode,
        },
        **receipt,
        "coverage": {"size": len(coverage), "sha256": hashlib.sha256(coverage).hexdigest()},
    }
    manifest_bytes = _canonical_json(manifest)
    try:
        os.mkdir(bundle)
    except OSError:
        _fail("bundle: directory already exists or cannot be created")
    _write_new_file(bundle / "coverage.data", coverage)
    _write_new_file(bundle / "manifest.json", manifest_bytes)
    _check_bundle_files(bundle)


def _lstat_mode(path: Path) -> int | None:
    """Return a path's own mode without following symlinks, or None if absent."""
    try:
        return os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    except OSError:
        _fail("filesystem: a path could not be inspected")


def _listdir(path: Path) -> list[str]:
    """List a directory's entries, mapping failure to a bounded refusal."""
    try:
        return os.listdir(path)
    except OSError:
        _fail("filesystem: a directory could not be listed")


def _check_bundle_files(bundle: Path) -> None:
    """Require exactly ``manifest.json`` and ``coverage.data`` as regular files."""
    if frozenset(_listdir(bundle)) != BUNDLE_FILES:
        _fail("bundle: files are not exactly manifest.json and coverage.data")
    for name in sorted(BUNDLE_FILES):
        mode = _lstat_mode(bundle / name)
        if mode is None or not stat.S_ISREG(mode):
            _fail("bundle: a bundle file is not a regular file")


@dataclass(frozen=True)
class _Shard:
    """One verified manifest's fields needed for cross-bundle checks."""

    count: int
    digest: str
    kept: list[str]


def _check_manifest_identity(value: object, context: IdentityContext) -> None:
    """Check one manifest identity against the current run."""
    identity = _exact_keys(
        value,
        frozenset({"repository", "run_id", "run_attempt", "workflow_sha", "target_sha", "mode"}),
        "identity",
    )
    if _exact_str(identity["repository"], "identity repository") != context.repository:
        _fail("identity: repository differs")
    if _exact_int(identity["run_id"], "identity run_id", 1, 10**19) != context.run_id:
        _fail("identity: run ID differs")
    attempt = _exact_int(identity["run_attempt"], "identity run_attempt", 1, 10**19)
    if attempt > context.run_attempt:
        _fail("identity: bundle is from a future attempt")
    workflow_sha = _hex(identity["workflow_sha"], "identity workflow_sha", _SHA1_HEX)
    if workflow_sha != context.workflow_sha:
        _fail("identity: workflow SHA differs")
    target_sha = _hex(identity["target_sha"], "identity target_sha", _SHA1_HEX)
    if target_sha != context.target_sha:
        _fail("identity: target SHA differs from HEAD")
    if identity["mode"] != context.mode:
        _fail("identity: mode differs")


def _verify_shard(directory: Path, index: int, context: IdentityContext) -> _Shard:
    """Verify one shard directory's layout, manifest, receipt and coverage."""
    mode = _lstat_mode(directory)
    if mode is None or not stat.S_ISDIR(mode):
        _fail(f"bundle: shard {index} is not a real directory")
    _check_bundle_files(directory)
    manifest = _exact_keys(
        parse_strict_json(
            read_regular_bounded(directory / "manifest.json", MANIFEST_CAP, "manifest")
        ),
        frozenset(
            {
                "schema",
                "partition",
                "shard_count",
                "index",
                "identity",
                "workers",
                "universe",
                "kept",
                "receipt",
                "coverage",
            }
        ),
        "manifest",
    )
    if manifest["schema"] != MANIFEST_SCHEMA or manifest["partition"] != PARTITION:
        _fail(f"schema: shard {index} schema or partition is not v1")
    _exact_int(manifest["shard_count"], "manifest shard_count", SHARD_COUNT, SHARD_COUNT)
    if _exact_int(manifest["index"], "manifest index", 0, 1) != index:
        _fail(f"bundle: shard {index} manifest index differs from its directory")
    _check_manifest_identity(manifest["identity"], context)
    workers = _exact_keys(manifest["workers"], frozenset({"expected"}), "manifest workers")
    _exact_int(workers["expected"], "manifest workers", 1, WORKER_CAP)
    universe = _exact_keys(
        manifest["universe"], frozenset({"count", "digest"}), "manifest universe"
    )
    count = _exact_int(universe["count"], "manifest universe count", 1, COUNT_CAP)
    digest = _hex(universe["digest"], "manifest universe digest", _SHA256_HEX)
    kept = _nodeid_list(manifest["kept"], "manifest kept")
    for position, nodeid in enumerate(kept):
        if shard_index(nodeid) != index:
            _fail(f"partition: shard {index} kept[{position}] belongs to the other shard")
    validate_lifecycle(kept, _exact_list(manifest["receipt"], "manifest receipt", RECORD_CAP))
    coverage_fields = _exact_keys(
        manifest["coverage"], frozenset({"size", "sha256"}), "manifest coverage"
    )
    size = _exact_int(coverage_fields["size"], "manifest coverage size", 1, COVERAGE_CAP)
    sha256 = _hex(coverage_fields["sha256"], "manifest coverage sha256", _SHA256_HEX)
    coverage = read_regular_bounded(directory / "coverage.data", COVERAGE_CAP, "coverage")
    if len(coverage) != size or hashlib.sha256(coverage).hexdigest() != sha256:
        _fail(f"coverage: shard {index} bytes differ from the manifest")
    return _Shard(count, digest, kept)


def verify(bundle_root: Path, context: IdentityContext, ordinary_coverage: Path) -> None:
    """Verify both shard bundles before their coverage is combined.

    Args:
        bundle_root: The directory holding ``ordinary-shard-0`` and ``-1``.
        context: The current, validated run identity.
        ordinary_coverage: The combine target, which must not exist yet.

    Raises:
        ShardError: Any layout, schema, identity, partition, universe, receipt,
            or coverage check fails.
    """
    if _lstat_mode(ordinary_coverage) is not None:
        _fail("coverage: the ordinary combine target already exists")
    mode = _lstat_mode(bundle_root)
    if mode is None or not stat.S_ISDIR(mode):
        _fail("bundle: the bundle root is not a real directory")
    if sorted(_listdir(bundle_root)) != list(SHARD_DIRECTORIES):
        _fail("bundle: the bundle root does not hold exactly the two shards")
    shards = [
        _verify_shard(bundle_root / name, index, context)
        for index, name in enumerate(SHARD_DIRECTORIES)
    ]
    first, second = shards
    # Defence in depth (contract §2.9 step 5): unreachable while each shard's own
    # partition check holds, since one ID cannot match both indices.
    if not set(first.kept).isdisjoint(second.kept):  # pragma: no cover - see above
        _fail("partition: the two shards overlap")
    if (first.count, first.digest) != (second.count, second.digest):
        _fail("universe: the two shards record different universes")
    union = [*first.kept, *second.kept]
    if len(union) != first.count or universe_digest(union) != first.digest:
        _fail("universe: the shard union does not equal the collected universe")


def _parser() -> argparse.ArgumentParser:
    """Build the closed ``finalize``/``verify`` command-line grammar."""
    parser = argparse.ArgumentParser(prog="ci_ordinary_shards.py")
    commands = parser.add_subparsers(dest="command", required=True)
    finalize_parser = commands.add_parser("finalize")
    finalize_parser.add_argument("--index", required=True, choices=("0", "1"))
    finalize_parser.add_argument("--receipt", required=True, type=Path)
    finalize_parser.add_argument("--coverage", required=True, type=Path)
    finalize_parser.add_argument("--bundle", required=True, type=Path)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--bundle-root", required=True, type=Path)
    for subparser in (finalize_parser, verify_parser):
        subparser.add_argument("--identity-mode", required=True, choices=IDENTITY_MODES)
        subparser.add_argument("--expected-target-sha")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``finalize`` or ``verify`` subcommand.

    Args:
        argv: Command-line arguments, excluding the program name.

    Returns:
        0 when every check passes, otherwise 1 (argparse exits 2 on bad usage).
    """
    arguments = _parser().parse_args(argv)
    try:
        context = identity_context(
            os.environ,
            arguments.identity_mode,
            arguments.expected_target_sha,
            read_head_sha(),
        )
        if arguments.command == "finalize":
            finalize(
                index=parse_index(arguments.index),
                receipt_path=arguments.receipt,
                coverage_path=arguments.coverage,
                bundle=arguments.bundle,
                context=context,
            )
        else:
            verify(arguments.bundle_root, context, Path(ORDINARY_COVERAGE))
    except ShardError as error:
        print(f"ordinary-shard: {error}", file=sys.stderr)
        return 1
    print(f"ordinary-shard: {arguments.command} passed", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a CI subprocess entry point
    raise SystemExit(main())
