"""Hardware-free tests for the hosted cold-run process policy (#954 U4).

Every external port of :func:`cold_runner.run_hosted` is injected: the cold run is
a fake coroutine, ``exit_process`` raises :class:`ExitCalled`, signals are captured
through a fake port (or the real loop port with no signal ever sent), the server is
a fake or a real uvicorn server on an ephemeral loopback port, and every private
temporary directory lands under ``tmp_path``.  No serial port, MCP child, provider,
``/proc`` read or signal to the test runner is involved.
"""

# pyright: reportPrivateUsage=false

import ast
import asyncio
import contextlib
import enum
import gc
import inspect
import logging
import os
import signal
import socket
import stat
import tempfile
import types
import typing
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import uvicorn
from starlette.types import Message

from roastpilot_agent import api, cold_runner
from roastpilot_agent.cold_app import COLD_OBSERVATION_EVENTS_PATH
from roastpilot_agent.cold_characterisation import advisory_conformance
from roastpilot_agent.cold_characterisation.advisory_sampler import ColdAdvisorySpec
from roastpilot_agent.cold_characterisation.engine import MonotonicEngineClock
from roastpilot_agent.cold_characterisation.evidence_lifecycle import ColdRunTerminationReason
from roastpilot_agent.cold_characterisation.host import HostBoundSample
from roastpilot_agent.cold_characterisation.identity import (
    BOOT_ID_PATH,
    AgentBuildProvenance,
    ColdArtefactKind,
)
from roastpilot_agent.cold_characterisation.two_phase import (
    ColdChildOwnership,
    ColdRunStartRefusal,
    ColdTwoPhaseAdvisoryPath,
    ColdTwoPhaseOutcome,
    ColdTwoPhaseProviderCheck,
    ColdTwoPhaseResult,
)
from roastpilot_agent.cold_composition import (
    ColdCompositionInputs,
    ColdCompositionRefusal,
    ColdCompositionResources,
    ColdHostFacts,
)
from roastpilot_agent.cold_observation_stream import ColdObservationHub
from roastpilot_agent.config import AppConfig, MCPDeviceConfig
from roastpilot_agent.store import RoastStore
from tests.test_cold_cli import SUMMARY_KEYS, parse_summary

Outcome = ColdTwoPhaseOutcome
Own = ColdChildOwnership
Path_ = ColdTwoPhaseAdvisoryPath
Check = ColdTwoPhaseProviderCheck
MARKER = "PLANTEDMARKER9d1e"
DIGEST = "c" * 64
RUNNER_SOURCE = Path(cold_runner.__file__)
CONFORMANT = advisory_conformance.ColdAdvisoryConformanceResult(
    policy_version=2,
    outcome=advisory_conformance.ColdAdvisoryConformanceOutcome.ADVISORY_CONFORMANT,
    findings=(),
    pre_advisory_findings=(),
)
EIGHT_FIELDS = {
    "outcome",
    "start_refusal",
    "termination_reason",
    "child_ownership",
    "manifest_sha256",
    "conformance",
    "advisory_path",
    "provider_check",
}


def fields(
    outcome: Outcome,
    owner: Own = Own.OWNED_STOP_CONFIRMED,
    *,
    refusal: ColdRunStartRefusal | None = None,
    reason: ColdRunTerminationReason | None = None,
    digest: str | None = DIGEST,
    conformance: object = None,
    path: Path_ = Path_.NOT_APPLICABLE,
    check: Check = Check.NOT_CHECKED,
) -> dict[str, typing.Any]:
    return {
        "outcome": outcome,
        "start_refusal": refusal,
        "termination_reason": reason,
        "child_ownership": owner,
        "manifest_sha256": digest,
        "conformance": conformance,
        "advisory_path": path,
        "provider_check": check,
    }


def od5(outcome: Outcome, owner: Own, check: Check, **extra: typing.Any) -> dict[str, typing.Any]:
    """An OD5-path row; NOT_CONFORMANT keeps its digest, EVIDENCE_NOT_SEALED has none."""
    return (
        fields(
            outcome,
            owner,
            reason=ColdRunTerminationReason.PHASE_ABORTED,
            digest=DIGEST if outcome is Outcome.NOT_CONFORMANT else None,
            path=Path_.FAILED_RUN_TERMINAL,
            check=check,
        )
        | extra
    )


def row(values: dict[str, typing.Any]) -> ColdTwoPhaseResult:
    return ColdTwoPhaseResult(**values)


ADVISORY_CONFORMANT = row(fields(Outcome.ADVISORY_CONFORMANT, conformance=CONFORMANT))
ORDINARY_NOT_CONFORMANT = row(fields(Outcome.NOT_CONFORMANT))
REFUSED = row(
    fields(
        Outcome.REFUSED_BEFORE_EVIDENCE,
        Own.NOT_OWNED,
        refusal=ColdRunStartRefusal.CHILD_START_FAILED,
        digest=None,
    )
)
NOT_SEALED = row(fields(Outcome.EVIDENCE_NOT_SEALED, digest=None))


class ExitCalled(BaseException):
    """Raised by the injected ``exit_process``; never caught by product code."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class FakeHost:
    """A host port that must never be called by these tests."""

    def check_start_bounds(self, evidence_root: Path) -> None:
        raise AssertionError("host port reached")

    def sample(self, evidence_root: Path) -> HostBoundSample:
        raise AssertionError("host port reached")


LOG: list[str] = []
TEMP_DIRS: list[tempfile.TemporaryDirectory[str]] = []
_REAL_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory


class TrackedTemporaryDirectory(_REAL_TEMPORARY_DIRECTORY[str]):
    """Records each private directory and logs its cleanup by kind."""

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        TEMP_DIRS.append(self)

    def cleanup(self) -> None:
        store = os.path.basename(self.name).startswith("roastpilot-cold-store-")
        LOG.append("td:store" if store else "td:resources")
        super().cleanup()


class FakeSignals:
    """Captures the loop callbacks; never touches process signal dispositions."""

    def __init__(self) -> None:
        self.callbacks: dict[int, Callable[[], None]] = {}

    def install(self, signum: int, callback: Callable[[], None]) -> None:
        self.callbacks[signum] = callback

    def restore(self) -> None:
        LOG.append("signals.restore")

    def fire(self, signum: int, times: int = 1) -> None:
        for _ in range(times):
            self.callbacks[signum]()


class LoggingStore(RoastStore):
    async def close(self) -> None:
        LOG.append("store.close")
        await super().close()


class LoggingHub(ColdObservationHub):
    def close(self) -> None:
        LOG.append("hub.close")
        super().close()


class Ports:
    """The injected ports of one hosted run plus their observations."""

    def __init__(self, spa_dir: Path) -> None:
        self.spa_dir = spa_dir
        self.signals = FakeSignals()
        self.servers: list[FakeServer] = []
        self.stores: list[RoastStore] = []
        self.configs: list[uvicorn.Config] = []
        self.sockets: list[socket.socket] = []
        self.server_mode = "ok"
        self.serve_release = asyncio.Event()
        self.serve_release.set()
        self.on_server_exit: Callable[[], None] = lambda: None
        self.store_class: type[RoastStore] = LoggingStore

    def signal_port(self, loop: asyncio.AbstractEventLoop) -> FakeSignals:
        return self.signals

    def real_config(self, app: typing.Any, **kwargs: typing.Any) -> uvicorn.Config:
        """A real uvicorn config with its real ``bind_socket``; access state is mutated."""
        config = uvicorn.Config(app, **kwargs)
        self.configs.append(config)
        return config

    def exit_process(self, code: int) -> typing.NoReturn:
        LOG.append(f"exit:{code}")
        raise ExitCalled(code)

    def store_factory(self, path: Path) -> RoastStore:
        store = self.store_class(path)
        self.stores.append(store)
        return store

    def config_factory(self, app: typing.Any, **kwargs: typing.Any) -> uvicorn.Config:
        config = uvicorn.Config(app, **kwargs)
        self.configs.append(config)

        def unbound() -> socket.socket:
            sock = socket.socket()
            self.sockets.append(sock)
            return sock

        config.bind_socket = unbound
        return config

    def server_factory(
        self, config: uvicorn.Config, started: "asyncio.Future[bool]"
    ) -> "FakeServer":
        server = FakeServer(self, started)
        self.servers.append(server)
        return server

    def app(self) -> typing.Any:
        return self.configs[-1].app

    async def cleanup(self) -> None:
        """Release what a pending exit (or a failed assertion) left behind."""
        self.serve_release.set()
        for server in self.servers:
            server.should_exit = True
        for server in self.servers:
            await server.finished.wait()
        for store in self.stores:
            with contextlib.suppress(Exception):
                await store.close()
        for sock in self.sockets:
            sock.close()


class FakeServer:
    """A server that resolves the barrier (or not) and serves until ``should_exit``."""

    def __init__(self, ports: Ports, started: "asyncio.Future[bool]") -> None:
        self._ports = ports
        self._started = started
        self._exit = asyncio.Event()
        self.finished = asyncio.Event()

    @property
    def should_exit(self) -> bool:
        return self._exit.is_set()

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        if value and not self._exit.is_set():
            LOG.append("server.exit")
            self._exit.set()
            self._ports.on_server_exit()

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            mode = self._ports.server_mode
            if mode == "raise":
                raise RuntimeError(MARKER)
            if mode == "sysexit":
                raise SystemExit(3)
            if mode == "refuse":
                self._started.set_result(False)
            elif mode != "stall":
                self._started.set_result(True)
            await self._exit.wait()
            await self._ports.serve_release.wait()
        finally:
            self.finished.set()


def make_inputs(**overrides: str) -> ColdCompositionInputs:
    values: dict[str, typing.Any] = {
        "pi_evidence_root": "/srv/primary",
        "laptop_evidence_root": "/srv/secondary",
        "audio_device_identity": "usb-mic",
        "serial_port_path": "/dev/ttyUSB-test",
        "stimulus_block": "stimulus",
        "operator_host_notes": "host",
        "operator_psu_notes": "psu",
        "operator_cooling_notes": "cooling",
    }
    profile = overrides.pop("profile_name", "cold-profile")
    values.update(overrides)
    return ColdCompositionInputs(
        spec=ColdAdvisorySpec(
            profile_name=profile,
            target_drop_temp_c=200.0,
            charge_guidance_min_c=None,
            charge_guidance_max_c=None,
        ),
        build_provenance=AgentBuildProvenance(
            source_revision="0" * 40,
            source_tree_dirty=False,
            artefact_kind=ColdArtefactKind.EDITABLE_SOURCE,
            artefact_sha256=None,
        ),
        host_facts=ColdHostFacts(
            coffee_roaster_mcp_version="0.2.2",
            python_version="3.11.9",
            platform="Linux-test",
            machine="aarch64",
            operating_system="Linux",
            kernel="6.6",
            pi_model="Raspberry Pi 5",
            pi_revision="c04170",
            boot_id_path=BOOT_ID_PATH,
        ),
        device_config=MCPDeviceConfig(),
        protected_roots=(),
        **values,
    )


RunFn = Callable[..., Awaitable[object]]


def returning(raw: object, *, before: Callable[[], Awaitable[None]] | None = None) -> RunFn:
    """A fake cold run that optionally awaits ``before`` and returns ``raw``."""

    async def fake(
        config: AppConfig,
        inputs: ColdCompositionInputs,
        *,
        resources: ColdCompositionResources,
        clock: object,
        host: object,
        tick_observer: Callable[[object], None],
    ) -> object:
        LOG.append("run")
        if before is not None:
            await before()
        return raw

    return fake


async def hosted(ports: Ports, run: RunFn, **overrides: typing.Any) -> int:
    kwargs: dict[str, typing.Any] = {
        "host_reader": FakeHost(),
        "spa_dir": ports.spa_dir,
        "bind_host": "127.0.0.1",
        "bind_port": 0,
        "run": run,
        "exit_process": ports.exit_process,
        "server_factory": ports.server_factory,
        "config_factory": ports.config_factory,
        "store_factory": ports.store_factory,
        "signals": ports.signal_port,
    }
    kwargs.update(overrides)
    inputs = kwargs.pop("inputs", None) or make_inputs()
    return await cold_runner.run_hosted(AppConfig(), inputs, **kwargs)


def uvicorn_logger_state() -> list[tuple[list[logging.Filter], list[logging.Handler], bool]]:
    state: list[tuple[list[logging.Filter], list[logging.Handler], bool]] = []
    for name in cold_runner._UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        filters = typing.cast(list[logging.Filter], list(logger.filters))
        state.append((filters, list(logger.handlers), logger.propagate))
    return state


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Strip env, reset the latch, route temp dirs under tmp_path, restore loggers."""
    for name in list(os.environ):
        if name.upper().startswith(("ROASTPILOT_", "COFFEE_")) or name == "OPENROUTER_API_KEY":
            monkeypatch.delenv(name)

    def forbidden() -> typing.NoReturn:
        raise AssertionError("host enumeration reached")

    monkeypatch.setattr(api, "_enumerate_serial", forbidden)
    monkeypatch.setattr(api, "_enumerate_audio_inputs", forbidden)
    monkeypatch.setattr(cold_runner, "_CONSUMED", False)
    monkeypatch.setattr(cold_runner, "_REPORTED_EXIT", None)
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    monkeypatch.setattr(tempfile, "TemporaryDirectory", TrackedTemporaryDirectory)
    LOG.clear()
    TEMP_DIRS.clear()
    saved = uvicorn_logger_state()
    yield temp_root
    for directory in TEMP_DIRS:
        directory.cleanup()
    for name, (filters, handlers, propagate) in zip(
        cold_runner._UVICORN_LOGGERS, saved, strict=True
    ):
        logger = logging.getLogger(name)
        logger.filters = list(filters)
        logger.handlers = handlers
        logger.propagate = propagate


@pytest_asyncio.fixture
async def ports(tmp_path: Path) -> typing.AsyncIterator[Ports]:
    """The ports; teardown releases anything a pending exit (or a failed test) left."""
    spa = tmp_path / "spa"
    spa.mkdir()
    (spa / "index.html").write_text("<html>cold-spa</html>", encoding="utf-8")
    state = Ports(spa)
    yield state
    await state.cleanup()


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Log mode/summary emission, hub close and log-filter restoration."""
    real_emit = cold_runner.emit
    real_restore = cold_runner._LogBoundary.restore

    def emit(text: str) -> None:
        LOG.append("mode" if text == cold_runner.MODE_LINE else "summary")
        real_emit(text)

    def restore(self: cold_runner._LogBoundary) -> None:
        LOG.append("filters.restore")
        real_restore(self)

    monkeypatch.setattr(cold_runner, "emit", emit)
    monkeypatch.setattr(cold_runner._LogBoundary, "restore", restore)
    monkeypatch.setattr(cold_runner, "ColdObservationHub", LoggingHub)


def cold_filter_installed() -> bool:
    return any(
        isinstance(item, cold_runner._ColdLogRedactor)
        for item in logging.getLogger("uvicorn.error").filters
    )


# --- 5. re-admission and the pending exit ----------------------------------------------


@pytest.mark.parametrize(
    ("values", "code"),
    [
        (od5(Outcome.NOT_CONFORMANT, Own.OWNED_STOP_CONFIRMED, Check.PENDING_AT_CHECK), 80),
        (od5(Outcome.NOT_CONFORMANT, Own.OWNED_STOP_UNCONFIRMED, Check.PENDING_AT_CHECK), 81),
        (od5(Outcome.EVIDENCE_NOT_SEALED, Own.OWNED_STOP_CONFIRMED, Check.PENDING_AT_CHECK), 82),
        (
            od5(Outcome.EVIDENCE_NOT_SEALED, Own.OWNED_STOP_UNCONFIRMED, Check.PENDING_AT_CHECK),
            83,
        ),
    ],
    ids=["80", "81", "82", "83"],
)
@pytest.mark.asyncio
async def test_admitted_pending_row_exits_directly_before_any_other_action(
    ports: Ports,
    logged: None,
    capsys: pytest.CaptureFixture[str],
    values: dict[str, typing.Any],
    code: int,
) -> None:
    """C6/C7: synchronous admission; nothing runs, yields or writes before the exit."""
    raw = row(values)

    async def fake(*_args: object, **_kwargs: object) -> object:
        LOG.append("run")
        asyncio.get_running_loop().call_soon(LOG.append, "sentinel")
        return raw

    with pytest.raises(ExitCalled) as exc:
        await hosted(ports, fake)
    assert exc.value.code == code
    assert ["mode", "run", f"exit:{code}"] == LOG
    assert capsys.readouterr().out == cold_runner.MODE_LINE
    # The pending path left everything in place, by design.
    assert len(TEMP_DIRS) == 2
    assert all(Path(directory.name).is_dir() for directory in TEMP_DIRS)
    assert ports.stores[0]._connection is not None
    assert not ports.servers[0].should_exit
    assert cold_filter_installed()
    await ports.cleanup()


@pytest.mark.asyncio
async def test_pending_default_is_the_most_uncertain_code(ports: Ports) -> None:
    """C19 (defensive): with an empty table the pending default is 83, never 0."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            cold_runner, "_PENDING_CODES", types.MappingProxyType[tuple[Outcome, Own], int]({})
        )
        raw = row(od5(Outcome.NOT_CONFORMANT, Own.OWNED_STOP_CONFIRMED, Check.PENDING_AT_CHECK))
        with pytest.raises(ExitCalled) as exc:
            await hosted(ports, returning(raw))
    assert exc.value.code == 83
    await ports.cleanup()


class SubResult(ColdTwoPhaseResult):
    """A subclass carrying an otherwise valid pending row."""


class LookAlikeCheck(enum.Enum):
    PENDING_AT_CHECK = "pending_at_check"


class LookAlikeRefusal(enum.Enum):
    CREDENTIAL_ABSENT = "credential_absent"


def _merged(base: dict[str, typing.Any], **changes: typing.Any) -> dict[str, typing.Any]:
    merged = dict(base)
    merged.update(changes)
    return merged


def _pending_values() -> dict[str, typing.Any]:
    return od5(Outcome.NOT_CONFORMANT, Own.OWNED_STOP_CONFIRMED, Check.PENDING_AT_CHECK)


NO_EXIT_CASES: list[tuple[str, Callable[[], object], int, str]] = [
    ("subclass", lambda: SubResult(**_pending_values()), 8, "unadmitted"),
    (
        "construct-pending-not-applicable",
        lambda: ColdTwoPhaseResult.model_construct(
            **fields(Outcome.NOT_CONFORMANT, check=Check.PENDING_AT_CHECK)
        ),
        8,
        "unadmitted",
    ),
    (
        "construct-conformant-pending",
        lambda: ColdTwoPhaseResult.model_construct(
            **od5(
                Outcome.ADVISORY_CONFORMANT,
                Own.OWNED_STOP_CONFIRMED,
                Check.PENDING_AT_CHECK,
                conformance=CONFORMANT,
                digest=DIGEST,
            )
        ),
        8,
        "unadmitted",
    ),
    (
        "construct-missing-field",
        lambda: ColdTwoPhaseResult.model_construct(outcome=Outcome.NOT_CONFORMANT),
        8,
        "unadmitted",
    ),
    ("duck", lambda: types.SimpleNamespace(**_pending_values()), 8, "unadmitted"),
    ("string", lambda: "pending_at_check", 8, "unadmitted"),
    ("member", lambda: Check.PENDING_AT_CHECK, 8, "unadmitted"),
    (
        "look-alike-check",
        lambda: ColdTwoPhaseResult.model_construct(
            **_merged(_pending_values(), provider_check=LookAlikeCheck.PENDING_AT_CHECK)
        ),
        8,
        "unadmitted",
    ),
    (
        "carrier-not-admitted",
        lambda: ColdTwoPhaseResult.model_construct(
            **_merged(
                _pending_values(),
                advisory_path=Path_.PROVIDER_OUTSTANDING_FAILED,
                conformance=types.SimpleNamespace(outcome=CONFORMANT.outcome),
            )
        ),
        8,
        "unadmitted",
    ),
    (
        "construct-bad-digest",
        lambda: ColdTwoPhaseResult.model_construct(
            **_merged(_pending_values(), manifest_sha256="NOT-A-DIGEST")
        ),
        8,
        "unadmitted",
    ),
    (
        "construct-row-consistent-unadmitted-carrier",
        lambda: ColdTwoPhaseResult.model_construct(
            **_merged(
                _pending_values(),
                advisory_path=Path_.PROVIDER_OUTSTANDING_FAILED,
                conformance=types.SimpleNamespace(
                    outcome=advisory_conformance.ColdAdvisoryConformanceOutcome.NOT_CONFORMANT
                ),
            )
        ),
        8,
        "unadmitted",
    ),
    ("look-alike-refusal", lambda: LookAlikeRefusal.CREDENTIAL_ABSENT, 8, "unadmitted"),
    ("none", lambda: None, 8, "unadmitted"),
    ("not-checked", lambda: ADVISORY_CONFORMANT, 0, "admitted"),
    (
        "not-pending",
        lambda: row(
            od5(Outcome.NOT_CONFORMANT, Own.OWNED_STOP_CONFIRMED, Check.NOT_PENDING_AT_CHECK)
        ),
        6,
        "admitted",
    ),
    (
        "not-observable",
        lambda: row(
            od5(
                Outcome.EVIDENCE_NOT_SEALED,
                Own.OWNED_STOP_UNCONFIRMED,
                Check.NOT_OBSERVABLE_AT_CHECK,
            )
        ),
        7,
        "admitted",
    ),
    ("refused-before-evidence", lambda: REFUSED, 5, "admitted"),
    ("not-sealed", lambda: NOT_SEALED, 7, "admitted"),
]


@pytest.mark.parametrize(
    ("raw_factory", "code", "result"),
    [case[1:] for case in NO_EXIT_CASES],
    ids=[case[0] for case in NO_EXIT_CASES],
)
@pytest.mark.asyncio
async def test_no_exit_authority_without_an_admitted_pending_row(
    ports: Ports,
    capsys: pytest.CaptureFixture[str],
    raw_factory: Callable[[], object],
    code: int,
    result: str,
) -> None:
    """C3/C4/C5: laundered, look-alike, unadmitted and non-pending values never exit."""
    granted: int | None = None
    observed: int | None = None
    try:
        observed = await hosted(ports, returning(raw_factory()))
    except ExitCalled as exc:
        granted = exc.code
        await ports.cleanup()
    assert granted is None, f"exit authority granted without an admitted pending row: {granted}"
    assert observed == code
    assert not any(entry.startswith("exit:") for entry in LOG)
    out = capsys.readouterr().out
    assert out.startswith(cold_runner.MODE_LINE)
    summary = parse_summary(out[len(cold_runner.MODE_LINE) :])
    assert summary["result"] == result
    assert summary["exit_code"] == str(code)
    assert summary["run_invoked"] == "true"
    if result == "unadmitted":
        assert summary["child_ownership"] == "unknown"
        assert summary["provider_check"] == "none"


@pytest.mark.parametrize("refusal", list(ColdCompositionRefusal))
@pytest.mark.asyncio
async def test_each_composition_refusal_is_exit_4_without_absence_claim(
    ports: Ports, capsys: pytest.CaptureFixture[str], refusal: ColdCompositionRefusal
) -> None:
    assert await hosted(ports, returning(refusal)) == 4
    out = capsys.readouterr().out
    summary = parse_summary(out[len(cold_runner.MODE_LINE) :])
    assert summary["result"] == "composition_refused"
    assert summary["composition_refusal"] == refusal.value
    assert summary["child_ownership"] == "unknown"


@pytest.mark.asyncio
async def test_a_propagated_exception_is_exit_8_with_child_unknown(
    ports: Ports, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing(*_args: object, **_kwargs: object) -> object:
        raise ValueError(MARKER)

    assert await hosted(ports, failing) == 8
    out = capsys.readouterr().out
    assert MARKER not in out
    summary = parse_summary(out[len(cold_runner.MODE_LINE) :])
    assert (summary["result"], summary["child_ownership"]) == ("propagated", "unknown")


def test_the_result_model_has_exactly_the_eight_readmitted_fields() -> None:
    assert set(ColdTwoPhaseResult.model_fields) == EIGHT_FIELDS


def test_readmit_returns_a_fresh_equal_row_and_identity_refusal() -> None:
    admitted = cold_runner._readmit(ORDINARY_NOT_CONFORMANT)
    assert admitted.result == ORDINARY_NOT_CONFORMANT
    assert admitted.result is not ORDINARY_NOT_CONFORMANT
    refused = cold_runner._readmit(ColdCompositionRefusal.ROOT_NOT_ADMITTED)
    assert refused.refusal is ColdCompositionRefusal.ROOT_NOT_ADMITTED
    assert refused.result is None


def test_exit_code_tables_are_total_and_closed() -> None:
    assert set(cold_runner.OUTCOME_EXIT_CODES) == set(Outcome)
    assert dict(cold_runner.OUTCOME_EXIT_CODES) == {
        Outcome.ADVISORY_CONFORMANT: 0,
        Outcome.REFUSED_BEFORE_EVIDENCE: 5,
        Outcome.NOT_CONFORMANT: 6,
        Outcome.EVIDENCE_NOT_SEALED: 7,
    }
    assert sorted(cold_runner._PENDING_CODES.values()) == [80, 81, 82, 83]


# --- 6. ordinary teardown and output hygiene -------------------------------------------


@pytest.mark.asyncio
async def test_ordinary_teardown_order_and_run_wiring(
    ports: Ports, logged: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """C16/C20: summary, hub, server, store, store dir, resources, handlers, filters."""
    seen: dict[str, object] = {}

    async def fake(
        config: AppConfig,
        inputs: ColdCompositionInputs,
        *,
        resources: ColdCompositionResources,
        clock: object,
        host: object,
        tick_observer: object,
    ) -> object:
        LOG.append("run")
        seen.update(
            directory=resources.directory,
            clock=clock,
            host=host,
            observer=tick_observer,
            hub=ports.app().state.cold_observation_hub,
            filter=cold_filter_installed(),
        )
        return ORDINARY_NOT_CONFORMANT

    assert await hosted(ports, fake) == 6
    assert LOG == [
        "mode",
        "run",
        "summary",
        "hub.close",
        "server.exit",
        "store.close",
        "td:store",
        "td:resources",
        "signals.restore",
        "filters.restore",
    ]
    assert isinstance(seen["directory"], Path)
    assert isinstance(seen["clock"], MonotonicEngineClock)
    assert isinstance(seen["host"], FakeHost)
    hub = typing.cast(ColdObservationHub, seen["hub"])
    assert seen["observer"] == hub.publish
    assert hub.closed
    assert seen["filter"] is True
    assert not cold_filter_installed()
    assert all(not Path(directory.name).exists() for directory in TEMP_DIRS)
    assert ports.sockets[0].fileno() == -1
    out = capsys.readouterr().out
    summary = parse_summary(out[len(cold_runner.MODE_LINE) :])
    assert summary == {
        "mode": "cold_characterisation",
        "run_invoked": "true",
        "result": "admitted",
        "cli_refusal": "none",
        "composition_refusal": "none",
        "outcome": "not_conformant",
        "start_refusal": "none",
        "termination_reason": "none",
        "child_ownership": "owned_stop_confirmed",
        "advisory_path": "not_applicable",
        "provider_check": "not_checked",
        "conformance_outcome": "none",
        "manifest_sha256": DIGEST,
        "signal": "none",
        "exit_code": "6",
    }


@pytest.mark.asyncio
async def test_injected_clock_is_passed_through(ports: Ports) -> None:
    clock = MonotonicEngineClock()
    seen: list[object] = []

    async def fake(*_args: object, clock: object, **_kwargs: object) -> object:
        seen.append(clock)
        return ADVISORY_CONFORMANT

    assert await hosted(ports, fake, clock=clock) == 0
    assert seen == [clock]


def _value_sets() -> dict[str, set[str]]:
    from roastpilot_agent.cold_characterisation.evidence_lifecycle import (
        ColdRunTerminationReason as Reason,
    )

    def tokens(kind: type[enum.Enum]) -> set[str]:
        return {str(member.value) for member in kind} | {"none"}

    return {
        "mode": {"cold_characterisation"},
        "run_invoked": {"true", "false"},
        "result": {member.value for member in cold_runner.SummaryResult},
        "cli_refusal": tokens(cold_runner.CliRefusal),
        "composition_refusal": tokens(ColdCompositionRefusal),
        "outcome": tokens(Outcome),
        "start_refusal": tokens(ColdRunStartRefusal),
        "termination_reason": tokens(Reason),
        "child_ownership": tokens(Own) | {"unknown"},
        "advisory_path": tokens(Path_),
        "provider_check": tokens(Check),
        "conformance_outcome": tokens(advisory_conformance.ColdAdvisoryConformanceOutcome),
        "signal": {"sigint", "sigterm", "none"},
    }


@pytest.mark.parametrize(
    "raw",
    [ADVISORY_CONFORMANT, REFUSED, NOT_SEALED, ColdCompositionRefusal.PROFILE_NOT_ADMITTED, None],
    ids=["conformant", "refused", "not-sealed", "composition", "none"],
)
@pytest.mark.asyncio
async def test_summary_has_fifteen_closed_keys_and_closed_values(
    ports: Ports, capsys: pytest.CaptureFixture[str], raw: object
) -> None:
    code = await hosted(ports, returning(raw))
    text = capsys.readouterr().out[len(cold_runner.MODE_LINE) :]
    summary = parse_summary(text)
    assert tuple(summary) == SUMMARY_KEYS
    assert len(SUMMARY_KEYS) == 15
    allowed = _value_sets()
    for key, value in summary.items():
        if key == "manifest_sha256":
            assert value == "none" or (len(value) == 64 and set(value) <= set("0123456789abcdef"))
        elif key == "exit_code":
            assert value == str(code)
        else:
            assert value in allowed[key], key


@pytest.mark.asyncio
async def test_planted_markers_never_reach_output_or_logs(
    ports: Ports,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No profile, note, path, host, credential or run-identity text is ever output."""
    monkeypatch.setenv("OPENROUTER_API_KEY", f"sk-or-{MARKER}")
    inputs = make_inputs(
        profile_name=f"profile-{MARKER}",
        pi_evidence_root=f"/srv/{MARKER}",
        laptop_evidence_root=f"/srv/2-{MARKER}",
        serial_port_path=f"/dev/{MARKER}",
        operator_host_notes=MARKER,
        operator_psu_notes=MARKER,
        operator_cooling_notes=MARKER,
        stimulus_block=MARKER,
        audio_device_identity=MARKER,
    )

    async def log_run_identity() -> None:
        logging.getLogger("uvicorn.error").error("run-id %s", f"run-{MARKER}")

    caplog.set_level(logging.INFO)
    code = await hosted(
        ports,
        returning(ADVISORY_CONFORMANT, before=log_run_identity),
        inputs=inputs,
        bind_host=f"host-{MARKER}",
    )
    assert code == 0
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err
    assert MARKER not in caplog.text
    assert "cold-http ERROR" in caplog.text


@pytest.mark.asyncio
async def test_real_signal_port_and_log_state_are_restored(ports: Ports) -> None:
    """C20 with the production loop port (no signal is ever sent)."""
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    access = logging.getLogger("uvicorn.access")
    access_before = (list(access.handlers), access.propagate)
    during: list[object] = []

    async def observe() -> None:
        during.append(signal.getsignal(signal.SIGINT))

    code = await hosted(
        ports,
        returning(ADVISORY_CONFORMANT, before=observe),
        signals=cold_runner._LoopSignals,
        config_factory=ports.real_config,
    )
    assert code == 0
    assert during[0] is not before[0]
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before
    assert (list(access.handlers), access.propagate) == access_before
    assert not cold_filter_installed()


def test_loop_signal_port_restores_only_a_recorded_python_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``None`` prior (non-Python handler) is not reinstalled; removal errors are contained."""
    restored: list[int] = []

    class Loop:
        def add_signal_handler(self, signum: int, callback: Callable[[], None]) -> None:
            pass

        def remove_signal_handler(self, signum: int) -> bool:
            raise RuntimeError("closed")

    def getsignal(signum: int) -> None:
        return None

    def set_signal(signum: int, handler: object) -> None:
        restored.append(signum)

    monkeypatch.setattr(signal, "getsignal", getsignal)
    monkeypatch.setattr(signal, "signal", set_signal)
    port = cold_runner._LoopSignals(typing.cast(asyncio.AbstractEventLoop, Loop()))
    port.install(signal.SIGINT, lambda: None)
    port.restore()
    assert restored == []


def test_emit_contains_output_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def write(self, _text: str) -> int:
            raise OSError("closed")

        def flush(self) -> None:
            raise AssertionError("unreachable")

    monkeypatch.setattr("sys.stdout", Broken())
    cold_runner.emit("x\n")


def test_unknown_signal_number_renders_none() -> None:
    summary = cold_runner.ColdRunSummary(
        run_invoked=False,
        result=cold_runner.SummaryResult.CANCELLED_BEFORE_RUN,
        exit_code=1,
        signal_number=1,
    )
    assert "signal=none\n" in cold_runner.render_summary(summary)


def test_conformance_outcome_is_rendered_from_the_admitted_row() -> None:
    summary = cold_runner.ColdRunSummary(
        run_invoked=True,
        result=cold_runner.SummaryResult.ADMITTED,
        exit_code=0,
        row=ADVISORY_CONFORMANT,
    )
    assert "conformance_outcome=advisory_conformant\n" in cold_runner.render_summary(summary)


# --- 7. latch ----------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["admitted", "refused", "exception"])
@pytest.mark.asyncio
async def test_latch_is_never_reset(
    ports: Ports, capsys: pytest.CaptureFixture[str], first: str
) -> None:
    """C2: a second call refuses with already_run and never invokes the run."""
    calls: list[str] = []

    async def failing(*_args: object, **_kwargs: object) -> object:
        raise ValueError("boom")

    if first == "refused":
        ports.server_mode = "refuse"
        assert await hosted(ports, returning(ADVISORY_CONFORMANT)) == 3
    elif first == "exception":
        assert await hosted(ports, failing) == 8
    else:
        assert await hosted(ports, returning(ADVISORY_CONFORMANT)) == 0
    capsys.readouterr()

    async def second(*_args: object, **_kwargs: object) -> object:
        calls.append("run")
        return ADVISORY_CONFORMANT

    assert await hosted(ports, second) == 3
    assert calls == []
    summary = parse_summary(capsys.readouterr().out)
    assert (summary["cli_refusal"], summary["run_invoked"]) == ("already_run", "false")
    assert summary["child_ownership"] == "none"
    assert cold_runner.reported_exit_code() == 3


# --- 8. signals --------------------------------------------------------------------------


class BlockingStore(LoggingStore):
    entered: asyncio.Event
    release: asyncio.Event

    async def initialize(self) -> None:
        type(self).entered.set()
        await type(self).release.wait()
        await super().initialize()


@pytest.mark.parametrize(("signum", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
@pytest.mark.asyncio
async def test_signal_during_store_initialisation_prevents_the_run(
    ports: Ports, capsys: pytest.CaptureFixture[str], signum: int, code: int
) -> None:
    """C9/C8: the first signal cancels once; repeats only record; the run is never invoked."""
    BlockingStore.entered = asyncio.Event()
    BlockingStore.release = asyncio.Event()
    ports.store_class = BlockingStore
    task = asyncio.create_task(hosted(ports, returning(ADVISORY_CONFORMANT)))
    await BlockingStore.entered.wait()
    ports.signals.fire(signum)
    ports.signals.fire(signal.SIGINT, times=3)
    assert task.cancelling() == 1
    assert await task == code
    assert task.cancelling() == 1
    assert "run" not in LOG
    assert not any(entry.startswith("exit:") for entry in LOG)
    summary = parse_summary(capsys.readouterr().out)
    assert summary["result"] == "cancelled_before_run"
    assert summary["run_invoked"] == "false"
    assert summary["child_ownership"] == "none"
    assert summary["signal"] == ("sigint" if signum == signal.SIGINT else "sigterm")
    assert ports.servers == []
    assert "store.close" in LOG
    assert "td:store" in LOG


@pytest.mark.asyncio
async def test_signal_while_the_startup_barrier_is_pending_prevents_the_run(
    ports: Ports, capsys: pytest.CaptureFixture[str]
) -> None:
    ports.server_mode = "stall"
    task = asyncio.create_task(hosted(ports, returning(ADVISORY_CONFORMANT)))
    while not ports.servers:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    ports.signals.fire(signal.SIGINT, times=4)
    assert await task == 130
    assert task.cancelling() == 1
    assert "run" not in LOG
    summary = parse_summary(capsys.readouterr().out)
    assert summary["result"] == "cancelled_before_run"
    assert ports.servers[0].finished.is_set()


@pytest.mark.parametrize(("signum", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
@pytest.mark.asyncio
async def test_signal_during_the_run_cancels_once_and_reaches_engine_cleanup(
    ports: Ports, capsys: pytest.CaptureFixture[str], signum: int, code: int
) -> None:
    """The engine's cleanup runs; repeated signals neither exit nor re-cancel."""
    entered = asyncio.Event()
    cleanup = asyncio.Event()
    in_cleanup = asyncio.Event()
    cancelling: list[int] = []

    async def engine(*_args: object, **_kwargs: object) -> object:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            in_cleanup.set()
            await cleanup.wait()
            current = asyncio.current_task()
            assert current is not None
            cancelling.append(current.cancelling())
            raise
        raise AssertionError("unreachable")

    task = asyncio.create_task(hosted(ports, engine))
    await entered.wait()
    ports.signals.fire(signum)
    await in_cleanup.wait()
    ports.signals.fire(signal.SIGINT, times=2)
    ports.signals.fire(signal.SIGTERM)
    cleanup.set()
    assert await task == code
    assert cancelling == [1]
    assert not any(entry.startswith("exit:") for entry in LOG)
    out = capsys.readouterr().out
    summary = parse_summary(out[len(cold_runner.MODE_LINE) :])
    assert (summary["result"], summary["run_invoked"]) == ("cancelled", "true")
    assert summary["child_ownership"] == "unknown"
    assert summary["exit_code"] == str(code)


@pytest.mark.asyncio
async def test_signals_after_the_engine_returned_only_record(
    ports: Ports, capsys: pytest.CaptureFixture[str]
) -> None:
    """A teardown-time signal never cancels and never changes the outcome code."""
    current: list[asyncio.Task[typing.Any]] = []

    def fire() -> None:
        ports.signals.fire(signal.SIGTERM)
        ports.signals.fire(signal.SIGINT, times=2)

    async def fake(*_args: object, **_kwargs: object) -> object:
        task = asyncio.current_task()
        assert task is not None
        current.append(task)
        return ORDINARY_NOT_CONFORMANT

    ports.on_server_exit = fire
    assert await hosted(ports, fake) == 6
    assert current[0].cancelling() == 0
    summary = parse_summary(capsys.readouterr().out[len(cold_runner.MODE_LINE) :])
    assert summary["result"] == "admitted"


@pytest.mark.asyncio
async def test_engine_that_returns_after_a_signal_keeps_its_outcome_and_reports_it(
    ports: Ports, capsys: pytest.CaptureFixture[str]
) -> None:
    entered = asyncio.Event()

    async def engine(*_args: object, **_kwargs: object) -> object:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return ORDINARY_NOT_CONFORMANT
        raise AssertionError("unreachable")

    task = asyncio.create_task(hosted(ports, engine))
    await entered.wait()
    ports.signals.fire(signal.SIGINT)
    assert await task == 6
    summary = parse_summary(capsys.readouterr().out[len(cold_runner.MODE_LINE) :])
    assert (summary["result"], summary["signal"]) == ("admitted", "sigint")


def test_signal_state_records_without_cancelling_after_the_engine() -> None:
    cancelled: list[bool] = []

    class Task:
        def cancel(self) -> bool:
            cancelled.append(True)
            return True

    state = cold_runner._SignalState(typing.cast(asyncio.Task[typing.Any], Task()))
    state.phase = cold_runner._Phase.POST_ENGINE
    state.on_signal(signal.SIGTERM)
    state.on_signal(signal.SIGINT)
    assert (state.signal_number, cancelled) == (signal.SIGTERM, [])


@pytest.mark.parametrize("phase", ["pre", "engine"])
@pytest.mark.asyncio
async def test_a_cancellation_this_module_did_not_request_is_exit_1(
    ports: Ports, capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    """Outside the contract: no recorded signal means exit 1, never 130/143."""

    class CancellingStore(LoggingStore):
        async def initialize(self) -> None:
            raise asyncio.CancelledError

    async def engine(*_args: object, **_kwargs: object) -> object:
        raise asyncio.CancelledError

    if phase == "pre":
        ports.store_class = CancellingStore
    assert await hosted(ports, engine) == 1
    out = capsys.readouterr().out
    if phase == "pre":
        summary = parse_summary(out)
        assert (summary["result"], summary["run_invoked"]) == ("cancelled_before_run", "false")
    else:
        summary = parse_summary(out[len(cold_runner.MODE_LINE) :])
        assert (summary["result"], summary["child_ownership"]) == ("cancelled", "unknown")
    assert summary["signal"] == "none"


# --- 9. startup barrier and bind ---------------------------------------------------------


@pytest.mark.parametrize("mode", ["refuse", "raise", "sysexit"])
@pytest.mark.asyncio
async def test_a_server_that_does_not_start_means_the_run_is_never_invoked(
    ports: Ports,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    """C10: a negative or failed barrier refuses with http_start_failed."""
    ports.server_mode = mode
    assert await hosted(ports, returning(ADVISORY_CONFORMANT)) == 3
    assert "run" not in LOG
    out = capsys.readouterr().out
    assert MARKER not in out
    summary = parse_summary(out)
    assert (summary["cli_refusal"], summary["run_invoked"]) == ("http_start_failed", "false")
    assert ports.servers[0].finished.is_set()
    assert all(not Path(directory.name).exists() for directory in TEMP_DIRS)
    gc.collect()
    await asyncio.sleep(0)
    assert "never retrieved" not in caplog.text


@pytest.mark.asyncio
async def test_real_server_with_should_exit_preset_resolves_the_barrier_negative(
    ports: Ports, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real ``_ColdServer.startup`` reports ``not should_exit`` as the barrier."""

    def exiting(config: uvicorn.Config, started: "asyncio.Future[bool]") -> object:
        server = cold_runner._ColdServer(config, started)
        server.should_exit = True
        return server

    code = await hosted(
        ports,
        returning(ADVISORY_CONFORMANT),
        server_factory=exiting,
        config_factory=ports.real_config,
    )
    assert code == 3
    assert "run" not in LOG
    assert parse_summary(capsys.readouterr().out)["cli_refusal"] == "http_start_failed"


@pytest.mark.parametrize("error", [OSError(MARKER), SystemExit(3)], ids=["oserror", "sysexit"])
@pytest.mark.asyncio
async def test_bind_failure_means_the_run_is_never_invoked(
    ports: Ports, capsys: pytest.CaptureFixture[str], error: BaseException
) -> None:
    """C11: bind happens before the run; a failure refuses with bind_failed."""

    def config_factory(app: typing.Any, **kwargs: typing.Any) -> uvicorn.Config:
        config = uvicorn.Config(app, **kwargs)

        def failing() -> socket.socket:
            raise error

        config.bind_socket = failing
        return config

    assert await hosted(ports, returning(ADVISORY_CONFORMANT), config_factory=config_factory) == 3
    assert "run" not in LOG
    assert ports.servers == []
    out = capsys.readouterr().out
    assert MARKER not in out
    assert parse_summary(out)["cli_refusal"] == "bind_failed"


def _raiser(*_args: object, **_kwargs: object) -> typing.NoReturn:
    raise RuntimeError(MARKER)


@pytest.mark.parametrize(
    ("override", "refusal"),
    [
        ("resources_factory", "store_not_initialised"),
        ("store_init", "store_not_initialised"),
        ("create_cold_app", "http_start_failed"),
        ("config_factory", "http_start_failed"),
        ("server_factory", "http_start_failed"),
    ],
)
@pytest.mark.asyncio
async def test_each_partial_setup_failure_refuses_and_tears_down(
    ports: Ports,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    override: str,
    refusal: str,
) -> None:
    class FailingStore(LoggingStore):
        async def initialize(self) -> None:
            raise RuntimeError(MARKER)

    extra: dict[str, typing.Any] = {}
    if override == "store_init":
        ports.store_class = FailingStore
    elif override == "create_cold_app":
        monkeypatch.setattr(cold_runner, "create_cold_app", _raiser)
    else:
        extra[override] = _raiser
    assert await hosted(ports, returning(ADVISORY_CONFORMANT), **extra) == 3
    assert "run" not in LOG
    out = capsys.readouterr().out
    assert MARKER not in out
    assert parse_summary(out)["cli_refusal"] == refusal
    assert "signals.restore" in LOG
    assert all(not Path(directory.name).exists() for directory in TEMP_DIRS)


@pytest.mark.asyncio
async def test_teardown_steps_are_contained_and_retried_through_cancellation(
    ports: Ports, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failing teardown steps are contained; an outside cancel cannot cut teardown short."""

    class FailingCloseStore(LoggingStore):
        async def close(self) -> None:
            await super().close()
            raise RuntimeError(MARKER)

    class FailingResources(ColdCompositionResources):
        def __exit__(self, *args: typing.Any) -> None:
            super().__exit__(*args)
            raise RuntimeError(MARKER)

    ports.store_class = FailingCloseStore
    ports.serve_release = asyncio.Event()
    task = asyncio.create_task(
        hosted(ports, returning(ADVISORY_CONFORMANT), resources_factory=FailingResources)
    )
    while not (ports.servers and ports.servers[0].should_exit):
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    ports.serve_release.set()
    assert await task == 0
    assert MARKER not in capsys.readouterr().out
    assert all(not Path(directory.name).exists() for directory in TEMP_DIRS)


@pytest.mark.asyncio
async def test_a_cancelled_teardown_task_is_settled_without_raising() -> None:
    async def cancelled() -> None:
        raise asyncio.CancelledError

    task = asyncio.ensure_future(cancelled())
    await cold_runner._settle(task)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_cold_server_startup_never_resolves_an_already_resolved_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A barrier already resolved (negative) by the serve wrapper is never overwritten."""

    async def started_ok(self: uvicorn.Server, sockets: list[socket.socket] | None = None) -> None:
        return None

    monkeypatch.setattr(uvicorn.Server, "startup", started_ok)
    started: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    started.set_result(False)
    server = cold_runner._ColdServer(uvicorn.Config(_noop_app), started)
    await server.startup(sockets=None)
    assert started.result() is False
    fresh: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    await cold_runner._ColdServer(uvicorn.Config(_noop_app), fresh).startup(sockets=None)
    assert fresh.result() is True


async def _noop_app(scope: typing.Any, receive: typing.Any, send: typing.Any) -> None:
    return None


# --- 10. ASGI: store, guard, cold route, SPA ----------------------------------------------


def http_scope(method: str, path: str) -> dict[str, typing.Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }


@pytest.mark.asyncio
async def test_hosted_app_store_guard_cold_route_and_spa(
    ports: Ports, isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """C12/C13: empty private store; 409 before receive; cold route not shadowed; SPA."""
    checks: dict[str, object] = {}

    async def probe() -> None:
        store = ports.stores[0]
        async with store.connection.execute("SELECT COUNT(*) FROM roast_runs") as cursor:
            count = await cursor.fetchone()
        assert count is not None
        checks["rows"] = count[0]
        checks["store_dir"] = store.db_path.parent
        app = ports.app()
        received: list[int] = []
        sent: list[Message] = []

        async def receive() -> Message:
            received.append(1)
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        for method, path in (
            ("POST", "/"),
            ("POST", "/index.html"),
            ("PUT", "/api/config"),
            ("DELETE", "/api/roasts"),
        ):
            received.clear()
            sent.clear()
            await app(http_scope(method, path), receive, send)
            assert received == [], (method, path)
            assert sent[0]["status"] == 409, (method, path)
        released = asyncio.Event()
        stream: list[Message] = []

        requested: list[int] = []

        async def stream_receive() -> Message:
            if not requested:
                requested.append(1)
                return {"type": "http.request", "body": b"", "more_body": False}
            await released.wait()
            return {"type": "http.disconnect"}

        async def stream_send(message: Message) -> None:
            stream.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                app.state.cold_observation_hub.close()

        await app(http_scope("GET", COLD_OBSERVATION_EVENTS_PATH), stream_receive, stream_send)
        released.set()
        start = stream[0]
        headers = dict(start["headers"])
        checks["events_status"] = start["status"]
        checks["events_type"] = headers[b"content-type"].decode()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            index = await client.get("/")
            checks["index"] = (index.status_code, index.text)

    assert await hosted(ports, returning(ADVISORY_CONFORMANT, before=probe)) == 0
    assert checks["rows"] == 0
    store_dir = typing.cast(Path, checks["store_dir"])
    assert store_dir.parent == isolated
    assert store_dir.name.startswith("roastpilot-cold-store-")
    assert checks["events_status"] == 200
    assert str(checks["events_type"]).startswith("text/event-stream")
    assert checks["index"] == (200, "<html>cold-spa</html>")
    capsys.readouterr()


@pytest.mark.asyncio
async def test_store_directory_is_private(ports: Ports) -> None:
    modes: list[int] = []

    async def probe() -> None:
        modes.append(stat.S_IMODE(ports.stores[0].db_path.parent.stat().st_mode))

    assert await hosted(ports, returning(ADVISORY_CONFORMANT, before=probe)) == 0
    assert modes == [0o700]


@pytest.mark.asyncio
async def test_absent_spa_refuses_before_the_run(
    ports: Ports, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await hosted(ports, returning(ADVISORY_CONFORMANT), spa_dir=tmp_path / "missing")
    assert code == 3
    assert "run" not in LOG
    assert parse_summary(capsys.readouterr().out)["cli_refusal"] == "spa_not_found"


@pytest.mark.asyncio
async def test_real_uvicorn_hosts_the_guarded_app_on_the_same_loop(
    ports: Ports, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real loopback server: SPA, guard and an open stream that teardown ends."""
    observed: dict[str, object] = {}
    chunks: list[str] = []
    first = asyncio.Event()
    stream_done: list[asyncio.Task[None]] = []

    async def probe() -> None:
        port = ports.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        async def consume() -> None:
            async with (
                httpx.AsyncClient() as client,
                client.stream("GET", base + COLD_OBSERVATION_EVENTS_PATH) as response,
            ):
                observed["type"] = response.headers["content-type"]
                async for text in response.aiter_text():
                    chunks.append(text)
                    first.set()

        stream_done.append(asyncio.create_task(consume()))
        await first.wait()
        async with httpx.AsyncClient() as client:
            index = await client.get(base + "/")
            blocked = await client.post(base + "/", content=b"body")
            observed["index"] = (index.status_code, index.text)
            observed["blocked"] = blocked.status_code

    def real_config(app: typing.Any, **kwargs: typing.Any) -> uvicorn.Config:
        config = uvicorn.Config(app, **kwargs)
        ports.configs.append(config)
        real_bind = config.bind_socket

        def bind() -> socket.socket:
            sock = real_bind()
            ports.sockets.append(sock)
            return sock

        config.bind_socket = bind
        return config

    async with asyncio.timeout(60):
        code = await hosted(
            ports,
            returning(ADVISORY_CONFORMANT, before=probe),
            server_factory=cold_runner._ColdServer,
            config_factory=real_config,
        )
        await stream_done[0]
    assert code == 0
    assert str(observed["type"]).startswith("text/event-stream")
    assert observed["index"] == (200, "<html>cold-spa</html>")
    assert observed["blocked"] == 409
    assert chunks and chunks[0].startswith(": connected")
    assert ports.configs[0].access_log is False
    assert ports.configs[0].log_config is None
    capsys.readouterr()


# --- 11. log boundary ---------------------------------------------------------------------


def _marked_record() -> None:
    try:
        raise RuntimeError(MARKER)
    except RuntimeError as error:
        logging.getLogger("uvicorn.error").error("boom %s", MARKER, exc_info=error)


@pytest.mark.asyncio
async def test_cold_log_boundary_strips_detail_and_the_control_shows_it_is_load_bearing(
    ports: Ports, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C14: access log off, no log config, detail-free records only while hosted."""
    monkeypatch.setenv("ROASTPILOT_HTTP_ACCESS_LOG", "full")
    caplog.set_level(logging.INFO)

    async def log_inside() -> None:
        _marked_record()

    assert await hosted(ports, returning(ADVISORY_CONFORMANT, before=log_inside)) == 0
    config = ports.configs[0]
    assert config.access_log is False
    assert config.log_config is None
    inside = [record for record in caplog.records if record.name == "uvicorn.error"]
    assert [record.getMessage() for record in inside] == ["cold-http ERROR"]
    assert inside[0].exc_info is None
    assert MARKER not in caplog.text
    caplog.clear()
    _marked_record()
    assert MARKER in caplog.text


def test_redactor_clears_every_detail_field() -> None:
    record = logging.LogRecord(
        "uvicorn.error", logging.WARNING, __file__, 1, "x %s", (MARKER,), None
    )
    record.stack_info = MARKER
    record.exc_text = MARKER
    record.__dict__["color_message"] = MARKER
    assert cold_runner._ColdLogRedactor().filter(record) is True
    assert record.getMessage() == "cold-http WARNING"
    assert (record.exc_info, record.exc_text, record.stack_info) == (None, None, None)
    assert "color_message" not in record.__dict__


# --- static contracts ---------------------------------------------------------------------


def _pending_compare_violations(source: str) -> list[str]:
    """Static C5b: exactly one ``.provider_check is ...PENDING_AT_CHECK`` and nothing else.

    The provider check may be compared only by identity, exactly once, and never via
    ``.value``, ``==``, truthiness or a ``"pending_at_check"`` literal.  The generic
    closed-token renderer's ``member.value`` read is not a provider-check comparison.
    """
    tree = ast.parse(source)
    problems: list[str] = []
    identity: list[ast.Compare] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "pending_at_check":
            problems.append("literal")
        if isinstance(node, ast.Attribute) and node.attr == "value":
            text = ast.unparse(node.value)
            if "provider_check" in text or "ColdTwoPhaseProviderCheck" in text:
                problems.append("value-read")
        if isinstance(node, ast.Compare):
            text = ast.unparse(node)
            if "PENDING_AT_CHECK" in text:
                if (
                    len(node.ops) == 1
                    and isinstance(node.ops[0], ast.Is)
                    and isinstance(node.left, ast.Attribute)
                    and node.left.attr == "provider_check"
                    and ast.unparse(node.comparators[0])
                    == "ColdTwoPhaseProviderCheck.PENDING_AT_CHECK"
                ):
                    identity.append(node)
                else:
                    problems.append("non-identity-compare")
    mentions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "PENDING_AT_CHECK"
    ]
    if len(identity) != 1 or len(mentions) != 1:
        problems.append("identity-count")
    return problems


def test_runner_compares_the_provider_check_exactly_once_by_identity() -> None:
    assert _pending_compare_violations(RUNNER_SOURCE.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "if r.provider_check == ColdTwoPhaseProviderCheck.PENDING_AT_CHECK:\n    pass\n",
        "if r.provider_check.value == 'pending_at_check':\n    pass\n",
        "if r.provider_check:\n    pass\n",
        "if r.provider_check is not ColdTwoPhaseProviderCheck.NOT_CHECKED:\n    pass\n",
        (
            "if r.provider_check is ColdTwoPhaseProviderCheck.PENDING_AT_CHECK:\n    pass\n"
            "if q.provider_check is ColdTwoPhaseProviderCheck.PENDING_AT_CHECK:\n    pass\n"
        ),
    ],
    ids=["eq", "value-eq", "truthy", "not-not-checked", "twice"],
)
def test_static_pending_contract_flags_negative_controls(snippet: str) -> None:
    assert _pending_compare_violations(snippet) != []


def test_static_pending_contract_accepts_the_single_identity_form() -> None:
    snippet = "if r.provider_check is ColdTwoPhaseProviderCheck.PENDING_AT_CHECK:\n    pass\n"
    assert _pending_compare_violations(snippet) == []


def test_pre_run_signal_check_precedes_the_engine_phase_and_await() -> None:
    """C9b (static, defensive): the pre-run signal check sits before the engine await."""
    source = inspect.getsource(cold_runner._HostedRun.drive)
    assert "if self.state.signal_number is not None:" in source
    check = source.index("if self.state.signal_number is not None:")
    phase = source.index("self.state.phase = _Phase.ENGINE")
    engine = source.index("raw: object = await self.run(")
    mode = source.index("emit(MODE_LINE)")
    barrier = source.index("ok = await asyncio.shield(started)")
    assert barrier < mode < check < phase < engine


def test_step_seven_has_no_await_between_handback_and_exit() -> None:
    """Static companion of C6: no await/yield between the engine await and the exit call."""
    source = inspect.getsource(cold_runner._HostedRun.drive)
    window = source[
        source.index("raw: object = await self.run(") : source.index("self.exit_process(")
    ]
    window = window[window.index("admitted = _readmit(raw)") :]
    assert "await" not in window
    assert "yield" not in window
    assert "emit(" not in window


def test_new_modules_avoid_normal_mode_wiring() -> None:
    """C21 (static): no env forwarding or live-service wiring in the cold modules."""
    for path in (RUNNER_SOURCE, RUNNER_SOURCE.with_name("cold_cli.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        }
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        forbidden = {
            "forward_coffee_env",
            "build_live_service",
            "_LiveSignalGuard",
            "_LiveExitGuard",
            "_SignalManagedServer",
            "_configure_access_log",
        }
        assert not (names | imported) & forbidden, path.name
