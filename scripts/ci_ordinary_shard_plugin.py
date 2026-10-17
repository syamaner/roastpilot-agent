"""CI-only pytest hook shim for the sharded ordinary lane (#1039).

Loaded only by the ordinary CI job's explicit ``-p scripts.ci_ordinary_shard_plugin``;
a bare local ``python -m pytest`` never loads it. Each xdist worker keeps only the
items whose v1 shard index matches ``--ordinary-shard-index`` (after marker and
path selection, in original order) and gw0 publishes its collection record. The
controller observes worker collections and every report, then writes the receipt
only if every check in ``ci_ordinary_shards`` passes; otherwise it writes nothing
and forces a non-zero exit.

The helper is imported lazily inside hook bodies so its module-level lines stay
measurable by coverage. A receipt records observed reports only: it is not proof
against a plugin that fabricates reports.
"""

from __future__ import annotations

import sys
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import cast

import pytest

_CONTROLLER_NAME = "ordinary-shard-controller"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the two shard options.

    Args:
        parser: The pytest option parser.
    """
    group = parser.getgroup("ordinary-shard", "ordinary CI shard (#1039)")
    group.addoption(
        "--ordinary-shard-index",
        dest="ordinary_shard_index",
        default=None,
        help="v1 shard index to keep: exactly 0 or 1.",
    )
    group.addoption(
        "--ordinary-shard-receipt",
        dest="ordinary_shard_receipt",
        default=None,
        help="path the controller writes the verified receipt to.",
    )


@pytest.hookimpl(wrapper=True)
def pytest_cmdline_main(config: pytest.Config) -> Generator[None, object, object]:
    """Validate the shard options before xdist resolves its worker request.

    Workers only re-validate the index. The controller additionally requires a
    receipt path and an explicit integer ``-n`` (not ``auto``/``logical``/0),
    then registers the observing controller plugin.

    Args:
        config: The pytest configuration.

    Returns:
        The wrapped hook result.

    Raises:
        pytest.UsageError: An option is missing or outside its closed grammar.
    """
    import ci_ordinary_shards as shards

    try:
        index = shards.parse_index(config.getoption("ordinary_shard_index"))
        if not hasattr(config, "workerinput"):
            receipt = config.getoption("ordinary_shard_receipt")
            if type(receipt) is not str or not receipt:
                raise shards.ShardError("option: a receipt path is required")
            workers = shards.expected_workers(getattr(config.option, "numprocesses", None))
            config.pluginmanager.register(
                _Controller(index, config.invocation_params.dir / receipt, workers),
                _CONTROLLER_NAME,
            )
    except shards.ShardError as error:
        raise pytest.UsageError(f"ordinary-shard: {error}") from None
    return (yield)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep this shard's items on a worker after all other selection.

    Args:
        config: The pytest configuration.
        items: The selected items, filtered in place.
    """
    workerinput = getattr(config, "workerinput", None)
    if workerinput is None:
        return
    import ci_ordinary_shards as shards

    index = shards.parse_index(config.getoption("ordinary_shard_index"))
    kept_ids, record = shards.worker_record([item.nodeid for item in items], index)
    kept_set = set(kept_ids)
    removed = [item for item in items if item.nodeid not in kept_set]
    items[:] = [item for item in items if item.nodeid in kept_set]
    if removed:
        config.hook.pytest_deselected(items=removed)
    if workerinput.get("workerid") == "gw0":
        outputs = cast(dict[str, object], getattr(config, "workeroutput"))  # noqa: B009
        outputs[shards.WORKER_RECORD_KEY] = record


def _worker_id(node: object) -> str:
    """Return an xdist node's gateway ID, or ``?`` when it is unavailable."""
    worker = getattr(getattr(node, "gateway", None), "id", None)
    return worker if type(worker) is str else "?"


class _Controller:
    """Controller-side observer that builds and writes the receipt."""

    def __init__(self, index: int, receipt_path: Path, workers: int) -> None:
        """Start observing one sharded controller session.

        Args:
            index: The shard index.
            receipt_path: Where to write the receipt.
            workers: The expected xdist worker count.
        """
        self.index = index
        self.receipt_path = receipt_path
        self.workers = workers
        self.collections: list[tuple[str, list[str]]] = []
        self.gw0_records: list[tuple[str, object]] = []
        self.records: list[object] = []
        self.collect_failures = 0
        self.internal_errors = 0
        self.node_errors = 0

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: object, ids: Sequence[str]) -> None:
        """Record one worker's final collection.

        Args:
            node: The xdist worker controller.
            ids: The worker's collected node IDs, in order.
        """
        self.collections.append((_worker_id(node), list(ids)))

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Record one observed phase report.

        Args:
            report: The test phase report.
        """
        record: dict[str, object] = {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
        }
        if hasattr(report, "wasxfail"):
            record["wasxfail"] = getattr(report, "wasxfail")  # noqa: B009
        self.records.append(record)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """Count failed collect reports forwarded to the controller.

        Args:
            report: The collect report.
        """
        if report.failed:
            self.collect_failures += 1

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: object, error: object) -> None:
        """Record a node-down error and harvest any shard record.

        Args:
            node: The xdist worker controller.
            error: The node-down error, or None for a clean finish.
        """
        import ci_ordinary_shards as shards

        if error is not None:
            self.node_errors += 1
        workeroutput = getattr(node, "workeroutput", None)
        if type(workeroutput) is dict and shards.WORKER_RECORD_KEY in workeroutput:
            outputs = cast(dict[str, object], workeroutput)
            self.gw0_records.append((_worker_id(node), outputs[shards.WORKER_RECORD_KEY]))

    def pytest_internalerror(self) -> None:
        """Count an internal error."""
        self.internal_errors += 1

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        """Write the receipt, or write nothing and force a non-zero exit.

        Args:
            session: The pytest session.
            exitstatus: The session exit status.
        """
        import ci_ordinary_shards as shards

        try:
            receipt = shards.build_receipt(
                index=self.index,
                exitstatus=int(exitstatus),
                workers=self.workers,
                collections=self.collections,
                gw0_records=self.gw0_records,
                records=self.records,
                collect_failures=self.collect_failures,
                internal_errors=self.internal_errors,
                node_errors=self.node_errors,
            )
            shards.write_receipt(self.receipt_path, receipt)
        except shards.ShardError as error:
            sys.stderr.write(f"ordinary-shard: receipt refused: {error}\n")
            if session.exitstatus == 0:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED
