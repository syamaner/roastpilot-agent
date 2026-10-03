"""One cold-characterisation invocation's process policy (#954, D194-D207).

:func:`run_hosted` hosts the read-only cold app (Agent API, the D207 cold SSE
route and the built SPA) on the same event loop as the cold engine, then awaits
exactly one cold run.  The order is fixed and is the whole point of this module:

0. A per-interpreter latch: a second call refuses without invoking the run.
1. SIGINT/SIGTERM loop handlers: the first signal before or during the run cancels
   the main task exactly once; every later signal is recorded only.  Nothing here
   ever forces an exit on a repeated signal.
2. A cold-only uvicorn log boundary: no access log, detail-free records.
3. Private resources: the composition's 0700 directory and a 0700 store directory.
4. A fresh, empty, private store; the hub; the guarded app with the SPA mounted
   after the cold route.
5. Bind, then a positive startup barrier with no timeout: a server that does not
   start means the run is never invoked.
6. One fixed mode line, then the engine is awaited directly in the main task.
7. Synchronously, in the same task step as the raw handback: exact-type
   re-admission through a fresh closed-row constructor.  Only an admitted row whose
   ``provider_check`` is ``PENDING_AT_CHECK`` grants self-termination, through
   ``exit_process`` with a closed code and no output, await or teardown first.
8. Otherwise, ordinary reverse-order teardown after one closed summary.

No wait, grace period, duration or watchdog is added: an HTTP startup or teardown
that never returns is a disclosed progress limitation.  No summary or log line
carries a host, port, path, profile, note, credential, raw argument or exception
text.
"""

import asyncio
import contextlib
import dataclasses
import enum
import functools
import logging
import os
import signal
import socket
import sys
import tempfile
import types
import typing
from collections.abc import Awaitable, Callable, Generator, Mapping
from pathlib import Path

import uvicorn

from roastpilot_agent.cold_app import create_cold_app
from roastpilot_agent.cold_characterisation.engine import (
    ColdEngineClock,
    ColdEngineHost,
    MonotonicEngineClock,
)
from roastpilot_agent.cold_characterisation.two_phase import (
    ColdChildOwnership,
    ColdTwoPhaseOutcome,
    ColdTwoPhaseProviderCheck,
    ColdTwoPhaseResult,
)
from roastpilot_agent.cold_composition import (
    ColdCompositionInputs,
    ColdCompositionRefusal,
    ColdCompositionResources,
    run_cold_characterisation,
)
from roastpilot_agent.cold_observation_stream import ColdObservationHub
from roastpilot_agent.config import AppConfig
from roastpilot_agent.live import mount_spa
from roastpilot_agent.store import RoastStore

MODE_LINE: typing.Final = "roastpilot-agent cold-characterisation: run starting\n"
_STORE_PREFIX: typing.Final = "roastpilot-cold-store-"
_STORE_NAME: typing.Final = "cold.sqlite3"
_UVICORN_LOGGERS: typing.Final = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
_HANDLED_SIGNALS: typing.Final = (signal.SIGINT, signal.SIGTERM)
_SIGNAL_TOKENS: typing.Final[Mapping[int, str]] = types.MappingProxyType(
    {int(signal.SIGINT): "sigint", int(signal.SIGTERM): "sigterm"}
)
#: A cancellation this module did not request (no recorded signal) is outside the
#: contract: the summary still states run invocation and child status honestly.
_UNEXPECTED_EXIT: typing.Final = 1
_CLI_REFUSAL_EXIT: typing.Final = 3
_COMPOSITION_REFUSAL_EXIT: typing.Final = 4
_UNKNOWN_CHILD_EXIT: typing.Final = 8
_PENDING_DEFAULT_EXIT: typing.Final = 83

#: The ordinary exit code of each admitted outcome; ``0`` is never qualification.
OUTCOME_EXIT_CODES: typing.Final[Mapping[ColdTwoPhaseOutcome, int]] = types.MappingProxyType(
    {
        ColdTwoPhaseOutcome.ADVISORY_CONFORMANT: 0,
        ColdTwoPhaseOutcome.REFUSED_BEFORE_EVIDENCE: 5,
        ColdTwoPhaseOutcome.NOT_CONFORMANT: 6,
        ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED: 7,
    }
)

#: The pending self-exit code by (outcome, ownership): 80/81 mean the engine's
#: seal returned a digest, 82/83 that it did not; 81/83 mean an unconfirmed stop.
#: Any other pair is unreachable after closed-row admission and maps to 83.
_PENDING_CODES: Mapping[tuple[ColdTwoPhaseOutcome, ColdChildOwnership], int] = (
    types.MappingProxyType(
        {
            (ColdTwoPhaseOutcome.NOT_CONFORMANT, ColdChildOwnership.OWNED_STOP_CONFIRMED): 80,
            (ColdTwoPhaseOutcome.NOT_CONFORMANT, ColdChildOwnership.OWNED_STOP_UNCONFIRMED): 81,
            (ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED, ColdChildOwnership.OWNED_STOP_CONFIRMED): 82,
            (
                ColdTwoPhaseOutcome.EVIDENCE_NOT_SEALED,
                ColdChildOwnership.OWNED_STOP_UNCONFIRMED,
            ): 83,
        }
    )
)

#: The per-interpreter latch; set by the first :func:`run_hosted` and never reset.
_CONSUMED = False
#: The exit code of the last summary :func:`run_hosted` reported, if any.
_REPORTED_EXIT: int | None = None


class CliRefusal(enum.Enum):
    """Closed CLI refusals; each means this invocation did not invoke the run."""

    INPUT_NOT_ADMITTED = "input_not_admitted"
    CONFIG_NOT_LOADED = "config_not_loaded"
    HOST_FACTS_NOT_READ = "host_facts_not_read"
    HOST_READER_UNAVAILABLE = "host_reader_unavailable"
    SPA_NOT_FOUND = "spa_not_found"
    STORE_NOT_INITIALISED = "store_not_initialised"
    BIND_FAILED = "bind_failed"
    HTTP_START_FAILED = "http_start_failed"
    ALREADY_RUN = "already_run"


class SummaryResult(enum.Enum):
    """The closed ``result=`` token of the summary."""

    ADMITTED = "admitted"
    COMPOSITION_REFUSED = "composition_refused"
    UNADMITTED = "unadmitted"
    PROPAGATED = "propagated"
    CANCELLED = "cancelled"
    CANCELLED_BEFORE_RUN = "cancelled_before_run"
    CLI_REFUSED = "cli_refused"


@dataclasses.dataclass(frozen=True)
class ColdRunSummary:
    """One closed summary; every rendered value is a member value or a fixed token.

    Attributes:
        run_invoked: Whether this invocation invoked the cold run.
        result: The closed result token.
        exit_code: The process exit code.
        cli_refusal: The CLI refusal, if any.
        composition_refusal: The admitted composition refusal, if any.
        row: The freshly re-admitted run result, if any.
        signal_number: The first recorded SIGINT/SIGTERM, if any.
    """

    run_invoked: bool
    result: SummaryResult
    exit_code: int
    cli_refusal: CliRefusal | None = None
    composition_refusal: ColdCompositionRefusal | None = None
    row: ColdTwoPhaseResult | None = None
    signal_number: int | None = None


def _token(member: enum.Enum | None) -> str:
    """The closed rendering of one enum member: its value, or ``none``."""
    if member is None:
        return "none"
    return str(member.value)


def render_summary(summary: ColdRunSummary) -> str:
    """Render the closed 15-key summary.

    ``child_ownership=none`` means the run was not invoked; ``unknown`` means it
    was invoked without an admitted row.  Neither, nor any member value, proves
    that no child exists.

    Args:
        summary: The summary to render.

    Returns:
        The summary text, one ``key=value`` line per key.
    """
    row = summary.row
    if row is not None:
        ownership = _token(row.child_ownership)
    elif summary.run_invoked:
        ownership = "unknown"
    else:
        ownership = "none"
    conformance = None if row is None or row.conformance is None else row.conformance.outcome
    digest = None if row is None else row.manifest_sha256
    signal_token = (
        "none"
        if summary.signal_number is None
        else _SIGNAL_TOKENS.get(summary.signal_number, "none")
    )
    lines = (
        ("mode", "cold_characterisation"),
        ("run_invoked", "true" if summary.run_invoked else "false"),
        ("result", _token(summary.result)),
        ("cli_refusal", _token(summary.cli_refusal)),
        ("composition_refusal", _token(summary.composition_refusal)),
        ("outcome", _token(None if row is None else row.outcome)),
        ("start_refusal", _token(None if row is None else row.start_refusal)),
        ("termination_reason", _token(None if row is None else row.termination_reason)),
        ("child_ownership", ownership),
        ("advisory_path", _token(None if row is None else row.advisory_path)),
        ("provider_check", _token(None if row is None else row.provider_check)),
        ("conformance_outcome", _token(conformance)),
        ("manifest_sha256", "none" if digest is None else digest),
        ("signal", signal_token),
        ("exit_code", str(summary.exit_code)),
    )
    return "".join(f"{key}={value}\n" for key, value in lines)


def emit(text: str) -> None:
    """Write fixed or closed text to stdout and flush; an output failure is contained.

    Args:
        text: Fixed or closed-grammar text.
    """
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.write(text)
        sys.stdout.flush()


def reported_exit_code() -> int | None:
    """The exit code of the summary :func:`run_hosted` already reported, if any."""
    return _REPORTED_EXIT


def _report(summary: ColdRunSummary) -> int:
    """Emit the summary, remember its code, and return the code."""
    global _REPORTED_EXIT
    _REPORTED_EXIT = summary.exit_code  # pyright: ignore[reportConstantRedefinition]
    emit(render_summary(summary))
    return summary.exit_code


class _Admission(typing.NamedTuple):
    """A re-admitted raw handback: a fresh row, an exact refusal member, or neither."""

    result: ColdTwoPhaseResult | None
    refusal: ColdCompositionRefusal | None


_UNADMITTED: typing.Final = _Admission(None, None)


def _readmit(raw: object) -> _Admission:
    """Re-admit the raw handback synchronously; pure and never raises.

    A result must be exactly :class:`ColdTwoPhaseResult` and is rebuilt through
    its constructor from exactly eight attribute reads, re-running every strict
    validator and the closed-row check.  A refusal must be exactly a
    :class:`ColdCompositionRefusal` member by identity.  Anything else, or any
    failure, is unadmitted.
    """
    if type(raw) is ColdTwoPhaseResult:
        try:
            fresh = ColdTwoPhaseResult(
                outcome=raw.outcome,
                start_refusal=raw.start_refusal,
                termination_reason=raw.termination_reason,
                child_ownership=raw.child_ownership,
                manifest_sha256=raw.manifest_sha256,
                conformance=raw.conformance,
                advisory_path=raw.advisory_path,
                provider_check=raw.provider_check,
            )
        except Exception:
            return _UNADMITTED
        return _Admission(fresh, None)
    if type(raw) is ColdCompositionRefusal and any(raw is m for m in ColdCompositionRefusal):
        return _Admission(None, raw)
    return _UNADMITTED


class _Phase(enum.Enum):
    """Where the invocation is relative to the engine await."""

    PRE_ENGINE = "pre_engine"
    ENGINE = "engine"
    POST_ENGINE = "post_engine"


class _SignalState:
    """The first-signal record and the single cancellation it may request."""

    def __init__(self, task: asyncio.Task[typing.Any]) -> None:
        self.task = task
        self.phase = _Phase.PRE_ENGINE
        self.signal_number: int | None = None
        self.cancel_requested = False

    def on_signal(self, signum: int) -> None:
        """Record the first signal; cancel the main task at most once, never exit."""
        if self.signal_number is None:
            self.signal_number = signum
        if not self.cancel_requested and self.phase is not _Phase.POST_ENGINE:
            self.cancel_requested = True
            self.task.cancel()


class SignalPort(typing.Protocol):
    """Installs loop signal callbacks and restores the prior dispositions."""

    def install(self, signum: int, callback: Callable[[], None]) -> None:
        """Install ``callback`` for ``signum``."""
        ...

    def restore(self) -> None:
        """Remove every installed callback and restore each prior disposition."""
        ...


class _LoopSignals:
    """The production :class:`SignalPort` over ``loop.add_signal_handler``."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._prior: list[tuple[int, typing.Any]] = []

    def install(self, signum: int, callback: Callable[[], None]) -> None:
        """Record the prior disposition, then install the loop callback."""
        prior = signal.getsignal(signum)
        self._loop.add_signal_handler(signum, callback)
        self._prior.append((signum, prior))

    def restore(self) -> None:
        """Remove each loop callback and restore its recorded prior disposition."""
        while self._prior:
            signum, prior = self._prior.pop()
            with contextlib.suppress(Exception):
                self._loop.remove_signal_handler(signum)
            if prior is not None:
                with contextlib.suppress(Exception):
                    signal.signal(signum, prior)


class _ColdLogRedactor(logging.Filter):
    """Replace every uvicorn record with a detail-free ``cold-http LEVEL`` record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Strip message arguments, exception and stack detail; keep the record."""
        record.msg = "cold-http " + record.levelname
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        record.__dict__.pop("color_message", None)
        return True


class _LogBoundary:
    """The cold-only uvicorn logging boundary and its restoration."""

    def __init__(self) -> None:
        self._filter = _ColdLogRedactor()
        self._saved: list[tuple[logging.Logger, list[logging.Handler], bool]] = []

    def install(self) -> None:
        """Snapshot each uvicorn logger's handlers and propagation, then filter it."""
        for name in _UVICORN_LOGGERS:
            logger = logging.getLogger(name)
            self._saved.append((logger, list(logger.handlers), logger.propagate))
            logger.addFilter(self._filter)

    def restore(self) -> None:
        """Remove the filter and restore each snapshot (uvicorn mutates ``access``)."""
        while self._saved:
            logger, handlers, propagate = self._saved.pop()
            logger.removeFilter(self._filter)
            logger.handlers = handlers
            logger.propagate = propagate


class _ServerPort(typing.Protocol):
    """The hosted HTTP server surface :func:`run_hosted` drives."""

    should_exit: bool

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        """Serve on ``sockets`` until ``should_exit``."""
        ...


class _ColdServer(uvicorn.Server):
    """Uvicorn server that never captures signals and reports a positive start."""

    def __init__(self, config: uvicorn.Config, started: asyncio.Future[bool]) -> None:
        super().__init__(config)
        self._cold_started = started

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        """Leave the process signal dispositions to :func:`run_hosted`."""
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """Start, then resolve the barrier to whether the server is still running."""
        await super().startup(sockets=sockets)
        if not self._cold_started.done():
            self._cold_started.set_result(not self.should_exit)


ServerFactory = Callable[[uvicorn.Config, "asyncio.Future[bool]"], _ServerPort]
ConfigFactory = Callable[..., uvicorn.Config]


async def _settle(task: "asyncio.Future[typing.Any]") -> None:
    """Await ``task`` to completion despite cancellation, with no timer; contain errors."""
    while not task.done():
        # ``shield`` re-raises the inner task's own exception, so contain it here
        # as well as an outer cancellation; the loop ends only when the task is done.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(task)
    if not task.cancelled():
        task.exception()


def _contain(step: Callable[[], object]) -> None:
    """Run one synchronous teardown step; its exception is contained."""
    with contextlib.suppress(Exception):
        step()


@dataclasses.dataclass(eq=False)
class _HostedRun:
    """The ports, resources and ordered steps of one hosted invocation."""

    config: AppConfig
    inputs: ColdCompositionInputs
    host_reader: ColdEngineHost
    spa_dir: Path
    bind_host: str
    bind_port: int
    run: Callable[..., Awaitable[object]]
    exit_process: Callable[[int], typing.NoReturn]
    server_factory: ServerFactory
    config_factory: ConfigFactory
    store_factory: Callable[[Path], RoastStore]
    resources_factory: Callable[[], ColdCompositionResources]
    clock: ColdEngineClock | None
    state: _SignalState
    signals: SignalPort
    log_boundary: _LogBoundary
    resources: ColdCompositionResources | None = None
    store_dir: tempfile.TemporaryDirectory[str] | None = None
    store: RoastStore | None = None
    hub: ColdObservationHub | None = None
    sock: socket.socket | None = None
    server: _ServerPort | None = None
    serve_task: asyncio.Task[None] | None = None
    serve_failed: bool = False

    def _refused(self, refusal: CliRefusal) -> ColdRunSummary:
        return ColdRunSummary(
            run_invoked=False,
            result=SummaryResult.CLI_REFUSED,
            exit_code=_CLI_REFUSAL_EXIT,
            cli_refusal=refusal,
        )

    def _cancelled(self, *, run_invoked: bool) -> ColdRunSummary:
        signum = self.state.signal_number
        return ColdRunSummary(
            run_invoked=run_invoked,
            result=SummaryResult.CANCELLED if run_invoked else SummaryResult.CANCELLED_BEFORE_RUN,
            exit_code=_UNEXPECTED_EXIT if signum is None else 128 + signum,
        )

    async def _serve(
        self, server: _ServerPort, sock: socket.socket, started: "asyncio.Future[bool]"
    ) -> None:
        """Serve; any failure becomes a closed flag and resolves the barrier negative."""
        try:
            await server.serve(sockets=[sock])
        except (SystemExit, Exception):
            self.serve_failed = True
        finally:
            if not started.done():
                started.set_result(False)

    async def drive(self) -> ColdRunSummary:
        """Steps 3-7: resources, store, app, bind, barrier, engine, admission."""
        try:
            self.resources = self.resources_factory()
            self.resources.__enter__()
            self.store_dir = tempfile.TemporaryDirectory(prefix=_STORE_PREFIX)
            self.store = self.store_factory(Path(self.store_dir.name) / _STORE_NAME)
        except Exception:
            return self._refused(CliRefusal.STORE_NOT_INITIALISED)
        try:
            await self.store.initialize()
        except asyncio.CancelledError:
            return self._cancelled(run_invoked=False)
        except Exception:
            return self._refused(CliRefusal.STORE_NOT_INITIALISED)
        try:
            self.hub = ColdObservationHub()
            app = create_cold_app(self.store, self.config, self.hub)
        except Exception:
            return self._refused(CliRefusal.HTTP_START_FAILED)
        try:
            mount_spa(app, self.spa_dir)
        except Exception:
            return self._refused(CliRefusal.SPA_NOT_FOUND)
        try:
            uv = self.config_factory(
                app,
                host=self.bind_host,
                port=self.bind_port,
                log_config=None,
                access_log=False,
            )
        except Exception:
            return self._refused(CliRefusal.HTTP_START_FAILED)
        try:
            self.sock = uv.bind_socket()
        except (OSError, SystemExit):
            return self._refused(CliRefusal.BIND_FAILED)
        started: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        try:
            self.server = self.server_factory(uv, started)
        except Exception:
            return self._refused(CliRefusal.HTTP_START_FAILED)
        self.serve_task = asyncio.create_task(self._serve(self.server, self.sock, started))
        try:
            ok = await asyncio.shield(started)
        except asyncio.CancelledError:
            return self._cancelled(run_invoked=False)
        if not ok:
            return self._refused(CliRefusal.HTTP_START_FAILED)
        emit(MODE_LINE)
        if self.state.signal_number is not None:  # pragma: no cover - defensive (C9b)
            # A recorded pre-engine signal always cancels, which surfaces at an
            # await above; this check only keeps the run uninvoked if it did not.
            return self._cancelled(run_invoked=False)
        self.state.phase = _Phase.ENGINE
        try:
            raw: object = await self.run(
                self.config,
                self.inputs,
                resources=self.resources,
                clock=MonotonicEngineClock() if self.clock is None else self.clock,
                host=self.host_reader,
                tick_observer=self.hub.publish,
            )
        except asyncio.CancelledError:
            self.state.phase = _Phase.POST_ENGINE
            return self._cancelled(run_invoked=True)
        except Exception:
            self.state.phase = _Phase.POST_ENGINE
            return ColdRunSummary(
                run_invoked=True, result=SummaryResult.PROPAGATED, exit_code=_UNKNOWN_CHILD_EXIT
            )
        # Step 7: synchronous; nothing may await, schedule, call out or write
        # between the raw handback and exit_process.
        admitted = _readmit(raw)
        if (
            admitted.result is not None
            and admitted.result.provider_check is ColdTwoPhaseProviderCheck.PENDING_AT_CHECK
        ):
            self.exit_process(
                _PENDING_CODES.get(
                    (admitted.result.outcome, admitted.result.child_ownership),
                    _PENDING_DEFAULT_EXIT,
                )
            )
        self.state.phase = _Phase.POST_ENGINE
        if admitted.result is not None:
            return ColdRunSummary(
                run_invoked=True,
                result=SummaryResult.ADMITTED,
                exit_code=OUTCOME_EXIT_CODES[admitted.result.outcome],
                row=admitted.result,
            )
        if admitted.refusal is not None:
            return ColdRunSummary(
                run_invoked=True,
                result=SummaryResult.COMPOSITION_REFUSED,
                exit_code=_COMPOSITION_REFUSAL_EXIT,
                composition_refusal=admitted.refusal,
            )
        return ColdRunSummary(
            run_invoked=True, result=SummaryResult.UNADMITTED, exit_code=_UNKNOWN_CHILD_EXIT
        )

    async def teardown(self, summary: ColdRunSummary) -> int:
        """Step 8: summary, then reverse-order teardown; every step is contained."""
        self.state.phase = _Phase.POST_ENGINE
        code = _report(dataclasses.replace(summary, signal_number=self.state.signal_number))
        hub, server, task, sock = self.hub, self.server, self.serve_task, self.sock
        if hub is not None:
            _contain(hub.close)
        if server is not None and task is not None:
            server.should_exit = True
            await _settle(task)
        if sock is not None:
            _contain(sock.close)
        store = self.store
        if store is not None:
            await _settle(asyncio.ensure_future(store.close()))
        store_dir = self.store_dir
        if store_dir is not None:
            _contain(store_dir.cleanup)
        resources = self.resources
        if resources is not None:
            _contain(functools.partial(resources.__exit__, None, None, None))
        _contain(self.signals.restore)
        _contain(self.log_boundary.restore)
        return code


async def run_hosted(
    config: AppConfig,
    inputs: ColdCompositionInputs,
    *,
    host_reader: ColdEngineHost,
    spa_dir: Path,
    bind_host: str,
    bind_port: int,
    run: Callable[..., Awaitable[object]] = run_cold_characterisation,
    exit_process: Callable[[int], typing.NoReturn] = os._exit,
    server_factory: ServerFactory = _ColdServer,
    config_factory: ConfigFactory = uvicorn.Config,
    store_factory: Callable[[Path], RoastStore] = RoastStore,
    resources_factory: Callable[[], ColdCompositionResources] = ColdCompositionResources,
    signals: Callable[[asyncio.AbstractEventLoop], SignalPort] = _LoopSignals,
    clock: ColdEngineClock | None = None,
) -> int:
    """Host the read-only cold app and run exactly one cold run in this interpreter.

    The cold child, MCP and provider are reachable only through ``run``.  The
    pending self-exit leaves the hub, server, store, both private temporary
    directories, signal handlers and log filters untouched by design.

    Args:
        config: The loaded application config.
        inputs: The admitted composition inputs.
        host_reader: The host-bound port for the engine.
        spa_dir: The built SPA directory (holds ``index.html``).
        bind_host: The HTTP bind host.
        bind_port: The HTTP bind port.
        run: The cold run; production is :func:`run_cold_characterisation`.
        exit_process: The direct process exit; production is the OS-level exit.
        server_factory: Builds the server from the uvicorn config and barrier.
        config_factory: Builds the uvicorn config.
        store_factory: Builds the run-private store.
        resources_factory: Builds the composition's private resources.
        signals: Builds the signal port for the running loop.
        clock: The engine clock; ``None`` means a fresh :class:`MonotonicEngineClock`.

    Returns:
        The closed exit code (see the runbook); the pending path never returns.
    """
    global _CONSUMED
    if _CONSUMED:
        return _report(
            ColdRunSummary(
                run_invoked=False,
                result=SummaryResult.CLI_REFUSED,
                exit_code=_CLI_REFUSAL_EXIT,
                cli_refusal=CliRefusal.ALREADY_RUN,
            )
        )
    _CONSUMED = True  # pyright: ignore[reportConstantRedefinition]
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:  # pragma: no cover - a coroutine awaited under asyncio has a task
        raise RuntimeError("Cold run requires a running task.")
    state = _SignalState(task)
    port = signals(loop)
    for signum in _HANDLED_SIGNALS:
        port.install(signum, functools.partial(state.on_signal, signum))
    log_boundary = _LogBoundary()
    log_boundary.install()
    hosted = _HostedRun(
        config=config,
        inputs=inputs,
        host_reader=host_reader,
        spa_dir=spa_dir,
        bind_host=bind_host,
        bind_port=bind_port,
        run=run,
        exit_process=exit_process,
        server_factory=server_factory,
        config_factory=config_factory,
        store_factory=store_factory,
        resources_factory=resources_factory,
        clock=clock,
        state=state,
        signals=port,
        log_boundary=log_boundary,
    )
    summary = await hosted.drive()
    return await hosted.teardown(summary)
