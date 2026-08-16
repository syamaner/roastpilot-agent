"""Console entrypoint for the roastpilot-agent service.

Two run modes plus the scaffold default:

- ``serve`` drives a **live** roast: it spawns the wired ``coffee-roaster-mcp``
  child, assembles the live :class:`~roastpilot_agent.api.RoastService` (with the
  recovery lifespan, so an agent restart enters ``operator_recovery_required``
  and never auto-resumes heat/fan), mounts the built SPA, and serves REST + SSE.
  This is the entrypoint the supervised hardware roast uses.
- ``--replay`` streams a recorded roast export through the real SSE pipeline
  (E10-S1): UI development without hardware, deterministic Playwright snapshots
  (``--step``), and the talk's 1× screen-recording rig (``--speed 1``). It serves
  the same built SPA so the recorded roast renders in the real dashboard.
- No arguments prints help (the E1 scaffold smoke contract).
"""

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import tempfile
import threading
from collections.abc import Awaitable, Callable, Generator, Sequence
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, cast

import uvicorn

from roastpilot_agent import __version__

if TYPE_CHECKING:
    from roastpilot_agent.api import RoastService
    from roastpilot_agent.config import HttpAccessLogMode, LoggingConfig
    from roastpilot_agent.mcp_client import MCPServerProcess, RuntimeConfigSnapshot, ToolCaller
    from roastpilot_agent.models import AdvisorHealth
    from roastpilot_agent.store import RoastStore

_log = logging.getLogger(__name__)

#: Valid access-log modes for the ``--access-log`` CLI flag and the
#: ``ROASTPILOT_HTTP_ACCESS_LOG`` env var (issue #267).
_ACCESS_LOG_MODES = ("quiet", "full", "off")
#: The uvicorn logger that emits the per-request ``GET /… 200 OK`` access lines.
_UVICORN_ACCESS_LOGGER = "uvicorn.access"
_LIVE_EXIT_GRACE_SECONDS = 10.0
_LIVE_EXIT_CODE = 70
_SIGBREAK = cast(int | None, getattr(signal, "SIGBREAK", None))
_LIVE_TERMINATION_SIGNALS: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM) + (
    () if _SIGBREAK is None else (_SIGBREAK,)
)


class _LiveExitGuard:
    """Bound live-process finalization after safety teardown completes.

    The daemon thread is started before ``asyncio.run`` so it remains able to
    terminate the process if asyncio's residual-task or executor shutdown
    blocks. It is deliberately armed only after the live store has closed.
    """

    def __init__(self, *, grace_seconds: float = _LIVE_EXIT_GRACE_SECONDS) -> None:
        """Start an unarmed live-exit watchdog.

        Args:
            grace_seconds: Fixed post-teardown finalization grace period.
        """
        self._grace_seconds = grace_seconds
        self._armed = threading.Event()
        self._disarmed = threading.Event()
        self._residual_labels: tuple[str, ...] = ()
        self._thread = threading.Thread(
            target=self._watch,
            name="roastpilot-live-exit-watchdog",
            daemon=True,
        )
        self._thread.start()

    def arm(self, residual_labels: tuple[str, ...]) -> None:
        """Arm the process bound with safe residual-task labels."""
        self._residual_labels = residual_labels
        self._armed.set()

    def disarm(self) -> None:
        """Disarm the watchdog after ``asyncio.run`` finishes."""
        self._disarmed.set()
        # Also release a never-armed watchdog (for example an MCP startup
        # failure before a live service was established) so its daemon thread
        # does not linger for the rest of an embedding process's lifetime.
        self._armed.set()

    def _watch(self) -> None:
        self._armed.wait()
        if self._disarmed.wait(self._grace_seconds):
            return
        labels = ", ".join(self._residual_labels) or "unidentified residual finalization"
        message = (
            "roastpilot-agent: live teardown completed, but process finalization "
            f"exceeded {self._grace_seconds:g}s; survivors: {labels}\n"
        )
        with contextlib.suppress(OSError):
            os.write(2, message.encode("utf-8", errors="replace"))
        os._exit(_LIVE_EXIT_CODE)


class _LiveSignalGuard:
    """Preserve first-SIGINT teardown and force an explicit second abort."""

    def __init__(self) -> None:
        """Install no handlers until entering the guard."""
        self._sigint_count = 0
        self._received_signal: int | None = None
        self._previous: dict[
            int, signal.Handlers | int | Callable[[int, FrameType | None], None] | None
        ] = {}
        self._graceful_handler: Callable[[int, FrameType | None], None] | None = None
        self._pending_graceful_signal: int | None = None

    def bind_graceful_handler(self, handler: Callable[[int, FrameType | None], None]) -> None:
        """Bind Uvicorn's first-signal graceful-shutdown handler."""
        self._graceful_handler = handler
        pending = self._pending_graceful_signal
        self._pending_graceful_signal = None
        if pending is not None:
            handler(pending, None)

    @property
    def received_signal(self) -> int | None:
        """Return the graceful termination signal received, if any."""
        return self._received_signal

    def __enter__(self) -> "_LiveSignalGuard":
        try:
            for signum in _LIVE_TERMINATION_SIGNALS:
                # Record the prior disposition before exposing _handle. A
                # signal can run between signal.signal() installing a handler
                # and that call returning to Python for an assignment.
                self._previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        for signum, handler in self._previous.items():
            if handler is not None:
                signal.signal(signum, handler)

    def _handle(self, _signum: int, _frame: FrameType | None) -> None:
        if self._received_signal is None:
            self._received_signal = _signum
        if self._graceful_handler is None and self._pending_graceful_signal is None:
            # If installation/startup has not bound its cancellation handler
            # yet, replay the first signal exactly once when that handler is
            # available. The prior disposition still runs below where safe.
            self._pending_graceful_signal = _signum
        if _signum != signal.SIGINT:
            if self._graceful_handler is not None:
                self._graceful_handler(_signum, _frame)
            else:
                previous = self._previous.get(_signum)
                if callable(previous):
                    previous(_signum, _frame)
                else:
                    os._exit(128 + _signum)
            return
        self._sigint_count += 1
        if self._sigint_count < 2:
            # Uvicorn treats any SIGINT received after should_exit is already
            # set as a forced exit, even when SIGTERM/SIGBREAK—not SIGINT—was
            # the first signal. Keep that first non-SIGINT shutdown graceful;
            # only two actual SIGINTs activate our explicit force-exit path.
            if self._received_signal != signal.SIGINT:
                return
            if self._graceful_handler is not None:
                self._graceful_handler(_signum, _frame)
            else:
                previous = self._previous.get(_signum)
                if callable(previous):
                    previous(_signum, _frame)
            return
        message = (
            "roastpilot-agent: second SIGINT forced immediate exit; live teardown "
            "may be incomplete and hardware state is uncertain\n"
        )
        with contextlib.suppress(OSError):
            os.write(2, message.encode("utf-8"))
        os._exit(_LIVE_EXIT_CODE)


class _SignalManagedServer(uvicorn.Server):
    """Uvicorn server whose signals are owned by ``_LiveSignalGuard``."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        """Keep Uvicorn from replacing the process-level live handlers."""
        yield


def _propagate_live_termination(signum: int | None) -> None:
    """Propagate a graceful live termination after ordered teardown."""
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    if signum is not None:
        raise SystemExit(128 + signum)


def _access_path_matches(request_path: str, pattern: str) -> bool:
    """Return whether ``request_path`` matches a quiet-path ``pattern``.

    The chatty paths are per-run (``/api/roasts/{run_id}/telemetry`` and the
    SSE stream), so the run id is part of every real request line and an exact
    literal match would never fire. A ``pattern`` containing the ``{run_id}``
    template segment is therefore matched as **prefix + suffix**: the request
    path must start with the text before ``{run_id}`` and end with the text
    after it, with a non-empty id between (no ``/`` in the id segment, so a
    deeper sub-path such as ``…/telemetry/extra`` does not match). A pattern
    with no template segment is matched exactly. The query string (if any) is
    stripped before matching.

    Args:
        request_path: The request path from the uvicorn access record (may
            carry a ``?query`` suffix).
        pattern: A quiet-path entry — an exact path or a ``{run_id}`` template.

    Returns:
        ``True`` when the request path is one of the quiet paths.
    """
    path = request_path.split("?", 1)[0]
    if "{run_id}" not in pattern:
        return path == pattern
    prefix, suffix = pattern.split("{run_id}", 1)
    if not (path.startswith(prefix) and path.endswith(suffix)):
        return False
    run_id = path[len(prefix) : len(path) - len(suffix)] if suffix else path[len(prefix) :]
    return bool(run_id) and "/" not in run_id


def _access_record_path_and_status(record: logging.LogRecord) -> tuple[str, int] | None:
    """Extract the request path and HTTP status from a uvicorn access record.

    uvicorn's access logger formats with a 5-tuple
    ``(client_addr, method, full_path, http_version, status_code)`` in
    ``record.args``. This reads ``full_path`` (index 2) and ``status_code``
    (index 4). Any record that does not match that shape (a non-access log
    routed through the same logger, a future uvicorn format change) returns
    ``None`` so the filter fails OPEN — it keeps the record rather than risk
    dropping something it cannot classify.

    Args:
        record: A log record emitted on the ``uvicorn.access`` logger.

    Returns:
        ``(path, status)`` when the record is a recognizable access line, else
        ``None``.
    """
    args = record.args
    if not isinstance(args, tuple) or len(args) < 5:
        return None
    path, status = args[2], args[4]
    if not isinstance(path, str) or not isinstance(status, int):
        return None
    return path, status


class _QuietAccessLogFilter(logging.Filter):
    """Drop successful access lines on the chatty paths (issue #267).

    Installed on the ``uvicorn.access`` logger in ``quiet`` mode. It suppresses
    a record only when BOTH hold: the HTTP status is < 400 (success), AND the
    request path is in the configured quiet-path set (the SSE stream, the
    per-tick telemetry series, the health poll — matched per
    :func:`_access_path_matches`, so any run id is caught). Everything else
    passes: every 4xx/5xx (any path), every non-quiet path, and any record this
    filter cannot classify (fails open). It is logging-only — it never touches
    API behaviour, only what the access logger emits.
    """

    def __init__(self, quiet_paths: Sequence[str]) -> None:
        """Initialize the filter with the quiet-path set.

        Args:
            quiet_paths: The route paths (exact or ``{run_id}`` templates)
                whose successful requests are suppressed.
        """
        super().__init__()
        self._quiet_paths = tuple(quiet_paths)

    def filter(self, record: logging.LogRecord) -> bool:
        """Return ``True`` to keep the record, ``False`` to drop it.

        Args:
            record: The candidate ``uvicorn.access`` log record.

        Returns:
            ``False`` only for a successful (status < 400) request on a quiet
            path; ``True`` otherwise (kept).
        """
        parsed = _access_record_path_and_status(record)
        if parsed is None:
            return True  # unclassifiable — fail open, keep it
        path, status = parsed
        if status >= 400:
            return True  # always keep client/server errors
        return not any(_access_path_matches(path, p) for p in self._quiet_paths)


def _resolve_access_log_mode(
    cli_value: "str | None", config_default: "HttpAccessLogMode"
) -> "HttpAccessLogMode":
    """Resolve the access-log mode: CLI flag > env var > config default (#267).

    Mirrors the ``--db`` > ``ROASTPILOT_DB`` > default precedence in
    :func:`_resolve_live_store_path`.

    Args:
        cli_value: The ``--access-log`` flag value, or ``None`` when unset.
        config_default: The ``AppConfig`` default to fall back to when neither
            the flag nor the env var is set.

    Returns:
        The effective mode (``quiet`` / ``full`` / ``off``).
    """
    if cli_value is not None:
        return cast_access_log_mode(cli_value)
    env_value = os.environ.get("ROASTPILOT_HTTP_ACCESS_LOG")
    if env_value:
        normalized = env_value.strip().lower()
        if normalized in _ACCESS_LOG_MODES:
            return cast_access_log_mode(normalized)
        _log.warning(
            "ignoring invalid ROASTPILOT_HTTP_ACCESS_LOG=%r (expected one of %s)",
            env_value,
            ", ".join(_ACCESS_LOG_MODES),
        )
    return config_default


def cast_access_log_mode(value: str) -> "HttpAccessLogMode":
    """Narrow a validated string to the ``HttpAccessLogMode`` literal type.

    The caller has already constrained ``value`` to one of the three modes
    (argparse ``choices`` or an env-var membership check), so this is a typed
    pass-through that satisfies pyright without a runtime cost.

    Args:
        value: One of ``"quiet"``, ``"full"``, ``"off"``.

    Returns:
        The same string, typed as :data:`HttpAccessLogMode`.
    """
    return cast("HttpAccessLogMode", value)


def _resolve_log_level(cli_value: str | None, config_default: str) -> str:
    """Resolve the uvicorn log level: CLI flag > env var > config default (#267).

    Args:
        cli_value: The ``--log-level`` flag value, or ``None`` when unset.
        config_default: The ``AppConfig`` default to fall back to when neither
            the flag nor the env var is set.

    Returns:
        The effective uvicorn log level string (e.g. ``"info"``).
    """
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get("ROASTPILOT_LOG_LEVEL")
    if env_value:
        return env_value.strip().lower()
    return config_default


def _apply_access_log_mode(mode: "HttpAccessLogMode", quiet_paths: Sequence[str]) -> bool:
    """Install/clear the quiet filter and return uvicorn's ``access_log`` flag.

    Logging-only side effect: it manages the :class:`_QuietAccessLogFilter` on
    the ``uvicorn.access`` logger so re-resolution is idempotent (any prior
    instance is removed first), then:

    - ``quiet`` → install the filter (drop 2xx/3xx on the quiet paths), return
      ``True`` (access log stays on for everything it keeps);
    - ``full`` → no filter, return ``True`` (today's behaviour);
    - ``off`` → no filter, return ``False`` (``uvicorn.Config(access_log=False)``
      disables the access log entirely).

    Args:
        mode: The resolved access-log mode.
        quiet_paths: The quiet-path set for the filter (used in ``quiet`` mode).

    Returns:
        The value to pass as ``uvicorn.Config(access_log=...)``.
    """
    access_logger = logging.getLogger(_UVICORN_ACCESS_LOGGER)
    for existing in [f for f in access_logger.filters if isinstance(f, _QuietAccessLogFilter)]:
        access_logger.removeFilter(existing)
    if mode == "quiet":
        access_logger.addFilter(_QuietAccessLogFilter(quiet_paths))
        return True
    # ``full`` keeps the access log on with no filter; ``off`` disables it.
    return mode != "off"


def _configure_access_log(
    args: argparse.Namespace, logging_config: "LoggingConfig"
) -> tuple[str, bool]:
    """Resolve + apply the access-log config for a serve entrypoint (#267).

    Resolves the mode and log level with CLI > env > config precedence, installs
    the quiet filter on ``uvicorn.access`` when appropriate, and returns the
    ``(log_level, access_log)`` pair the caller passes to ``uvicorn.Config``.
    Logging-only: it changes nothing but what the access logger emits.

    Args:
        args: The parsed CLI namespace (``access_log`` / ``log_level`` flags).
        logging_config: The ``AppConfig.logging`` section (config defaults +
            quiet-path set).

    Returns:
        ``(log_level, access_log)`` for ``uvicorn.Config(log_level=...,
        access_log=...)``.
    """
    mode = _resolve_access_log_mode(args.access_log, logging_config.http_access_log_mode)
    log_level = _resolve_log_level(args.log_level, logging_config.log_level)
    access_log = _apply_access_log_mode(mode, logging_config.http_access_log_quiet_paths)
    return log_level, access_log


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roastpilot-agent",
        description="Deterministic agent harness for autonomous coffee roasting.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "action",
        nargs="?",
        choices=["serve"],
        help="'serve' drives a live roast against the wired coffee-roaster-mcp child",
    )
    parser.add_argument(
        "--replay",
        metavar="EXPORT_DIR",
        type=Path,
        help=(
            "replay a recorded roast export directory (with roast.jsonl) through "
            "the real SSE pipeline instead of driving live hardware"
        ),
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help=(
            "replay speed multiplier 1x-60x (1x is the screen-recording rig); "
            "ignored with --step. Free-running replay keeps SERVING the final "
            "frame after the recorded roast ends (it does not exit) so the rig "
            "can screenshot the terminal state — stop it with Ctrl-C"
        ),
    )
    parser.add_argument(
        "--step",
        action="store_true",
        help=(
            "replay paused at tick 0, mounting the gated /api/replay/{step,advance-to} "
            "control routes for deterministic Playwright stepping (replay mode only)"
        ),
    )
    parser.add_argument(
        "--spa-dir",
        metavar="PATH",
        type=Path,
        default=None,
        help=(
            "directory of the built SPA to serve at / (defaults to the bundled "
            "web/dist when present); applies to both 'serve' and --replay"
        ),
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        type=Path,
        default=None,
        help=(
            "SQLite path for the live decision trace ('serve' only); persists "
            "across restart so recovery can read prior run state. Defaults to "
            "$ROASTPILOT_DB, else $XDG_STATE_HOME/roastpilot/roastpilot.sqlite3 "
            "(else ~/.local/state/...). Replay is always ephemeral and ignores this."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default 8000)")
    parser.add_argument(
        "--access-log",
        dest="access_log",
        choices=list(_ACCESS_LOG_MODES),
        default=None,
        help=(
            "HTTP access-log verbosity for 'serve'/--replay: 'quiet' (default) "
            "drops successful (2xx/3xx) logs on the chatty SSE/telemetry/health "
            "paths while keeping all 4xx/5xx and every other path; 'full' logs "
            "all requests; 'off' disables the access log. Precedence: this flag "
            "> $ROASTPILOT_HTTP_ACCESS_LOG > config default (quiet)."
        ),
    )
    parser.add_argument(
        "--log-level",
        dest="log_level",
        default=None,
        help=(
            "uvicorn log level for 'serve'/--replay (default 'info'). "
            "Precedence: this flag > $ROASTPILOT_LOG_LEVEL > config default."
        ),
    )
    return parser


def _resolve_live_store_path(args: argparse.Namespace) -> Path:
    """Resolve the persistent SQLite path for a live ``serve`` (issue #161).

    A **live** roast must persist its agent decision trace — per-tick
    telemetry, every CLAMP/REJECT :class:`SafetyEvaluation`, advisor decisions,
    and events — so it survives shutdown and a restart can read prior run state
    for the recovery flow. (Replay is the opposite: an ephemeral tempdir is
    correct there, so this resolver is **not** used on the replay path.)

    Precedence:

    1. ``--db PATH`` — explicit operator choice;
    2. ``ROASTPILOT_DB`` environment variable;
    3. default ``$XDG_STATE_HOME/roastpilot/roastpilot.sqlite3``, or
       ``~/.local/state/roastpilot/roastpilot.sqlite3`` when ``XDG_STATE_HOME``
       is unset.

    The parent directory is created (``parents=True, exist_ok=True``) so the
    first live roast on a fresh machine just works.

    Args:
        args: Parsed CLI namespace; ``args.db`` is the explicit override.

    Returns:
        The resolved SQLite file path, with its parent directory ensured.
    """
    env_db = os.environ.get("ROASTPILOT_DB")
    if args.db is not None:
        path = args.db
    elif env_db:
        path = Path(env_db)
    else:
        state_home = os.environ.get("XDG_STATE_HOME")
        base = Path(state_home) if state_home else Path.home() / ".local" / "state"
        path = base / "roastpilot" / "roastpilot.sqlite3"
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_spa_dir(args: argparse.Namespace) -> Path | None:
    """The SPA dir to serve: an explicit ``--spa-dir`` else the bundled default.

    An explicit ``--spa-dir`` that lacks an ``index.html`` resolves to ``None``
    (mount nothing) rather than serving a broken tree — the SPA is optional, and
    a missing build should not wedge the entrypoint.
    """
    from roastpilot_agent.live import default_spa_dir

    if args.spa_dir is not None:
        return args.spa_dir if (args.spa_dir / "index.html").is_file() else None
    return default_spa_dir()


def _format_runtime_readout(rc: "RuntimeConfigSnapshot") -> list[str]:
    """Render the operator-facing startup readout for a runtime config snapshot.

    Turns the MCP child's resolved :class:`~roastpilot_agent.mcp_client.RuntimeConfigSnapshot`
    into a prominent, can't-miss console block answering "is it the right
    hardware, and is first-crack audio detection on?" — the questions an
    operator must not have to chase before a real roast. Loud ``⚠️`` warnings
    are appended (never an exit) when the driver is the ``mock`` driver or
    first-crack mode is not ``audio``; both are valid for a dry-run, so this
    only warns. The readout is purely informational and read-only.

    Note: the resolved microphone device name and the FC "listening" state are
    NOT in :class:`RuntimeConfigSnapshot` — the audio device is not exposed
    there, and ``audio_running`` only appears in ``get_roast_state``'s
    ``first_crack_status`` once a session starts. A pointer line directs the
    operator to confirm those on the dashboard after the roast starts.

    Args:
        rc: The runtime config snapshot read from the MCP child.

    Returns:
        The console lines to print, in order (header, fields, warnings, note).
    """
    port = rc.roaster_port if rc.roaster_port is not None else "—"
    lines = [
        "── Roaster runtime (from coffee-roaster-mcp) ──",
        f"  driver        : {rc.roaster_driver}"
        f"      (port {port}, {rc.roaster_baudrate}, {rc.temperature_unit})",
        f"  first crack   : {rc.first_crack_mode}"
        f"   (model {rc.model_repo_id} · {rc.model_precision})",
        f"  log dir       : {rc.log_dir}",
    ]
    if rc.roaster_driver == "mock":
        lines.append("⚠️  MOCK driver — NOT real hardware")
    if rc.first_crack_mode != "audio":
        lines.append(
            f"⚠️  first-crack mode is {rc.first_crack_mode!r}, not audio — no audio FC detection"
        )
    lines.append(
        "  mic + FC-listening: confirm on the dashboard "
        "(FC: listening + Diagnostics window counts) once the roast starts."
    )
    return lines


async def _emit_runtime_readout(call_tool: "ToolCaller") -> None:
    """Query the MCP child's runtime config and print the startup readout.

    Read-only: it calls the ``get_runtime_config`` read tool exactly once and
    prints :func:`_format_runtime_readout`. Robustness is the contract — the
    readout is informational and must never block startup, so any transport
    failure (``MCPConnectionError`` / timeout, or any unexpected error) is
    logged as a warning and swallowed; the live serve continues.

    Args:
        call_tool: The MCP child's ``call_tool`` transport (``mcp.call_tool``).
    """
    from roastpilot_agent.mcp_client import RoasterMCPClient

    try:
        rc = await RoasterMCPClient(call_tool).get_runtime_config()
    except Exception as exc:  # noqa: BLE001 — informational readout, never a blocker
        _log.warning("could not read runtime config: %s", exc)
        return
    for line in _format_runtime_readout(rc):
        print(line)


def _format_advisor_readout(health: "AdvisorHealth") -> list[str]:
    """Render the operator-facing advisor reachability readout (issue #168).

    Turns the startup :class:`~roastpilot_agent.models.AdvisorHealth` probe
    into a prominent console line — as can't-miss as the mock-driver ``⚠️``
    warning — so the operator learns the advisor is dead *before* charging
    beans (the #134 expired-key failure that "advisor configured" hid). A
    ``REACHABLE`` advisor prints provider/model; ``UNREACHABLE`` prints a loud
    ``⚠️`` line carrying the actual provider error (401/402/404/timeout);
    ``NOT_CONFIGURED`` notes the roast will run advisory-paused. Read-only and
    informational — an unreachable advisor never blocks serve.

    Args:
        health: The advisor reachability probe result.

    Returns:
        The console lines to print, in order.
    """
    from roastpilot_agent.models import AdvisorHealthStatus

    lines = ["── Advisor (reachability probe) ──"]
    if health.status is AdvisorHealthStatus.REACHABLE:
        lines.append(f"  advisor REACHABLE (provider={health.provider}, model={health.model_slug})")
    elif health.status is AdvisorHealthStatus.NOT_CONFIGURED:
        lines.append(
            "  advisor NOT CONFIGURED — the roast runs advisory-paused "
            "(the controller issues no advisory writes without an advisor)"
        )
    else:
        target = ""
        if health.provider is not None or health.model_slug is not None:
            target = f" (provider={health.provider}, model={health.model_slug})"
        # `error` is typed `str | None`; every UNREACHABLE path sets it, but
        # guard so the operator never sees a bare "UNREACHABLE: None".
        error_msg = health.error or "(no error detail)"
        lines.append(f"⚠️  advisor UNREACHABLE{target}: {error_msg}")
        lines.append(
            "    the roast can still start (advisory-paused); the controller "
            "runs deterministically without advice"
        )
    return lines


async def _emit_advisor_readout(service: "RoastService") -> "AdvisorHealth":
    """Probe advisor reachability, print the readout, and record it (issue #168).

    Best-effort and **non-blocking**: it runs the bounded
    :func:`~roastpilot_agent.live.probe_advisor_health` over the service's
    advisor (advisory-only — no MCP write tools), prints
    :func:`_format_advisor_readout`, and stores the result on the service so
    ``GET /api/health`` can surface it. The probe never raises or stalls, so it
    can never wedge or abort startup — an unreachable advisor warns loudly but
    serve still proceeds.

    Args:
        service: The live :class:`~roastpilot_agent.api.RoastService`.

    Returns:
        The advisor reachability probe result (also recorded on the service).
    """
    from roastpilot_agent.live import probe_advisor_health

    health = await probe_advisor_health(service.advisor)
    service.set_advisor_health(health)
    for line in _format_advisor_readout(health):
        print(line)
    return health


def _format_post_fc_loop_readout(
    *,
    enabled: bool,
    ceiling_guard_enabled: bool,
    ceiling_guard_temp_c: float,
    recovery_enabled: bool = False,
    recovery_headroom_percentage_points: int = 0,
    recovery_projection_enabled: bool = False,
    recovery_projection_entry_horizon_pp: float = 2.0,
    recovery_projection_cutoff_horizon_pp: float = 5.0,
    recovery_projection_margin_c: float = 3.0,
    recovery_entry_step_pp: int = 10,
) -> list[str]:
    """Render the operator-facing D88/D96 post-FC flag readouts (issues #460,
    #495, #559/PR #560 round 3).

    Prints the resolved ``controller.post_first_crack_control.enabled``,
    ``ceiling_guard_drop_enabled``, ``recovery_enabled``, and recovery-v2
    projection values as
    can't-miss console lines — as prominent as the mock-driver ``⚠️`` /
    advisor-experiment tag — so an operator running a baseline-vs-treatment
    A/B can *confirm* which roast is which before charging beans. A silent
    typo in any raw nested env var (``ROASTPILOT_CONTROLLER__
    POST_FIRST_CRACK_CONTROL__ENABLED`` / ``…__CEILING_GUARD_DROP_ENABLED`` /
    ``…__RECOVERY_ENABLED``) would otherwise leave the flag at its default
    ``False`` and quietly turn the "treatment" roast into a second baseline —
    or, in the opposite direction, an accidental ``True`` would silently
    activate the bounded-bidirectional heat relaxation on a roast the
    operator believed was running the plain D88 never-add-heat law. The
    THREE flags are independent by design (D88 decoupled the ceiling guard
    from the taper; D96 layers recovery on top of both), so each gets its
    own line. Read-only and informational — this never blocks startup, the
    same contract as :func:`_format_advisor_readout`.

    Args:
        enabled: The resolved ``post_first_crack_control.enabled`` value from
            the loaded :class:`~roastpilot_agent.config.AppConfig`.
        ceiling_guard_enabled: The resolved
            ``post_first_crack_control.ceiling_guard_drop_enabled`` value.
        ceiling_guard_temp_c: The resolved
            ``post_first_crack_control.ceiling_guard_temp_c`` value, shown so
            the operator confirms the guard line, not just the flag.
        recovery_enabled: The resolved
            ``post_first_crack_control.recovery_enabled`` value (D96, #559).
            Defaults ``False`` so callers that predate this field (there are
            none left in this codebase, but the default keeps the function
            signature backward-compatible) still resolve to the pre-D96
            readout shape.
        recovery_headroom_percentage_points: The resolved
            ``post_first_crack_control.recovery_headroom_percentage_points``
            value, shown alongside the flag so the operator confirms the
            CAP, not just that recovery is on — the number that actually
            bounds how far above entry heat the loop may raise. Only
            rendered when ``recovery_enabled`` is ``True``.
        recovery_projection_enabled: Whether the D162 runway-aware projection
            trigger is enabled.
        recovery_projection_entry_horizon_pp: Resolved DTR percentage-point
            horizon where a projected miss may enter recovery.
        recovery_projection_cutoff_horizon_pp: Resolved DTR percentage-point
            horizon where recovery authority unconditionally releases.
        recovery_projection_margin_c: Resolved projected-temperature shortfall
            required for entry.
        recovery_entry_step_pp: Resolved one-time fast-raise floor above entry.

    Returns:
        The console lines to print, in order.
    """
    lines: list[str] = []
    if enabled:
        lines.append(
            "⚠️  POST-FC RoR LOOP: ENABLED (#405/D88 — deterministic taper drives heat; "
            "#498/D89 — advisor's fan judgment applied by the taper's write; drop shared)"
        )
    else:
        lines.append("  post-FC RoR loop: disabled (advisor-driven post-FC)")
    if ceiling_guard_enabled:
        lines.append(
            "⚠️  CEILING-GUARD DROP: ENABLED "
            f"(D88 — deterministic drop at bean ≥ {ceiling_guard_temp_c:g} °C)"
        )
    else:
        lines.append("  ceiling-guard drop: disabled (advisor/operator own the bitter boundary)")
    if recovery_enabled:
        lines.append(
            "⚠️  BOUNDED-BIDIRECTIONAL HEAT RECOVERY: ENABLED "
            f"(D96 — the taper may raise heat up to {recovery_headroom_percentage_points:g} pp "
            "above entry when RoR runs persistently below setpoint)"
        )
    else:
        lines.append(
            "  bidirectional heat recovery: disabled (D88 never-add-heat-beyond-entry stands)"
        )
    v2_values = (
        f"entry=+{recovery_projection_entry_horizon_pp:g} pp, "
        f"cutoff=+{recovery_projection_cutoff_horizon_pp:g} pp, "
        f"margin={recovery_projection_margin_c:g} °C, "
        f"fast-raise=+{recovery_entry_step_pp:d} pp"
    )
    if recovery_projection_enabled:
        lines.append(f"⚠️  RECOVERY V2 PROJECTION: ENABLED (D162 — {v2_values})")
    else:
        lines.append(f"  recovery v2 projection: disabled (D162 — {v2_values})")
    return lines


def _residual_task_labels() -> tuple[str, ...]:
    """Return stable, non-sensitive labels for unfinished asyncio tasks."""
    current = asyncio.current_task()
    labels = {
        task.get_name() for task in asyncio.all_tasks() if task is not current and not task.done()
    }
    return tuple(sorted(labels))


async def _finish_live_teardown(
    service: "RoastService",
    mcp: "MCPServerProcess",
    store: "RoastStore",
    exit_guard: _LiveExitGuard | None,
) -> None:
    """Complete ordered teardown despite cancellation, then arm the exit bound."""
    teardown = asyncio.create_task(
        _teardown_live(service, mcp, store),
        name="roastpilot-live-ordered-teardown",
    )
    cancelled = False
    while not teardown.done():
        try:
            await asyncio.shield(teardown)
        except asyncio.CancelledError:
            cancelled = True
    # Retrieve an unexpected BaseException even though ordinary teardown-step
    # exceptions are contained by _cleanup_step.
    teardown.result()
    if exit_guard is not None:
        exit_guard.arm(_residual_task_labels())
    if cancelled:
        raise asyncio.CancelledError


async def _serve_live(
    args: argparse.Namespace,
    *,
    exit_guard: _LiveExitGuard,
    signal_guard: _LiveSignalGuard,
) -> int:
    """Build and serve the live roast app, then clean up the MCP child.

    Uses the recovery lifespan (``create_app``'s default — restart →
    ``operator_recovery_required``, never an auto-resume of heat/fan).
    Fail-closed: an MCP start failure prints a clear message and returns a
    non-zero exit, with the child cleaned up by
    :func:`~roastpilot_agent.live.build_live_service`."""
    live_task = asyncio.current_task()
    if live_task is None:  # pragma: no cover - asyncio always owns a running coroutine
        raise RuntimeError("live serve requires an asyncio task")

    def _cancel_live_task(_signum: int, _frame: FrameType | None) -> None:
        """Cancel startup safely until Uvicorn's graceful handler is bound."""
        live_task.cancel()

    signal_guard.bind_graceful_handler(_cancel_live_task)

    from pydantic import ValidationError

    from roastpilot_agent.api import create_app
    from roastpilot_agent.config_store import ConfigFileError, load_app_config
    from roastpilot_agent.live import build_live_service, forward_coffee_env
    from roastpilot_agent.mcp_client import MCPConnectionError

    try:
        config, _injected = load_app_config()
    except ConfigFileError as exc:
        print(f"error: saved-config file is malformed — {exc}")
        return 1
    except ValidationError as exc:
        print(f"error: saved-config file has invalid values — {exc}")
        return 1
    except OSError as exc:
        print(f"error: saved-config file is unreadable — {exc}")
        return 1
    # Let the operator configure the Hottop with plain `export COFFEE_…`.
    forward_coffee_env(config)

    # Live runs persist to a stable on-disk path (issue #161) — NOT a tempdir
    # like replay — so the agent decision trace survives shutdown and a restart
    # can read prior run state for recovery.
    store_path = _resolve_live_store_path(args)
    try:
        service, mcp, store = await build_live_service(config, store_path=store_path)
    except MCPConnectionError as exc:
        # Fail closed: the child is already stopped by build_live_service.
        exit_guard.arm(_residual_task_labels())
        print(f"error: could not start coffee-roaster-mcp: {exc}")
        return 1
    except BaseException:
        # build_live_service owns cleanup of a half-started MCP child. Arm the
        # process bound after that cleanup even though no service/store exists,
        # so a retained cancellation-resistant MCP owner cannot wedge
        # asyncio.run() finalization forever.
        exit_guard.arm(_residual_task_labels())
        raise

    # The MCP child is RUNNING the moment build_live_service returns, so the
    # ENTIRE post-build phase (store init, app build, serve) is wrapped: a
    # failure in store.initialize()/create_app() must still tear the child
    # down rather than orphan it.
    try:
        await store.initialize()
        spa_dir = _resolve_spa_dir(args)
        # create_app's default lifespan IS the recovery _lifespan: on startup
        # it runs recover_on_start (a possibly-active run →
        # operator_recovery_required, never an auto-resume of heat/fan) and
        # stops the loop on shutdown. The live serve path deliberately uses
        # that recovery lifespan, not replay's no-recovery one.
        app = create_app(service, spa_dir=spa_dir)
        spa_note = "with SPA" if spa_dir is not None else "API only (no SPA build found)"
        print(f"serving live roast ({spa_note}) on http://{args.host}:{args.port}")
        # The persistent trace path is part of the operator readout: it tells
        # them where the roast is being recorded and survives shutdown.
        print(f"  decision trace → {store_path}")

        # Startup hardware/sensing readout (#134): print what the MCP child
        # actually resolved — real Hottop vs mock, FC mode — before uvicorn
        # serves, so "right hardware + FC on?" is a can't-miss console line.
        # Read-only and best-effort: a failure here never blocks the serve.
        await _emit_runtime_readout(mcp.call_tool)

        # Advisor reachability probe (#168): a cheap, bounded liveness check so
        # the operator learns a dead advisor (expired key, bad model slug)
        # BEFORE charging, not after every in-roast call fails. Best-effort and
        # non-blocking — an unreachable advisor warns loudly but serve proceeds
        # (advisory-paused is valid); the result is exposed on /api/health.
        await _emit_advisor_readout(service)

        # D88/D96 post-FC flag readouts (issues #460, #495, #559/PR #560
        # round 3): read straight off the already-loaded `config` (no
        # re-load) so they can never drift from what actually serves.
        # Read-only/informational — never blocks startup.
        post_fc = config.controller.post_first_crack_control
        for line in _format_post_fc_loop_readout(
            enabled=post_fc.enabled,
            ceiling_guard_enabled=post_fc.ceiling_guard_drop_enabled,
            ceiling_guard_temp_c=post_fc.ceiling_guard_temp_c,
            recovery_enabled=post_fc.recovery_enabled,
            recovery_headroom_percentage_points=post_fc.recovery_headroom_percentage_points,
            recovery_projection_enabled=post_fc.recovery_projection_enabled,
            recovery_projection_entry_horizon_pp=post_fc.recovery_projection_entry_horizon_pp,
            recovery_projection_cutoff_horizon_pp=post_fc.recovery_projection_cutoff_horizon_pp,
            recovery_projection_margin_c=post_fc.recovery_projection_margin_c,
            recovery_entry_step_pp=post_fc.recovery_entry_step_pp,
        ):
            print(line)

        # Access-log verbosity (#267): resolve CLI > env > config and install the
        # quiet filter on uvicorn.access. Logging-only — nothing about the API
        # changes, only what its access logger emits.
        log_level, access_log = _configure_access_log(args, config.logging)
        uv = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level=log_level,
            access_log=access_log,
        )
        server = _SignalManagedServer(uv)
        signal_guard.bind_graceful_handler(server.handle_exit)
        # _lifespan runs recover_on_start (restart → recovery) on startup and
        # service.shutdown() on teardown; we stop the MCP child after the
        # server returns (graceful shutdown / SIGINT) and close the store.
        await server.serve()
    finally:
        await _finish_live_teardown(service, mcp, store, exit_guard)
    _propagate_live_termination(signal_guard.received_signal)
    return 0


async def _teardown_live(
    service: "RoastService", mcp: "MCPServerProcess", store: "RoastStore"
) -> None:
    """Tear the live serve down in the safety-critical order (#142).

    Graceful shutdown / Ctrl-C must command heat→0 through the safety path
    **before** stopping the MCP child — the write must land while the child is
    still alive to receive it. So the order is load-bearing:

    1. ``safe_shutdown_heat_off`` — heat→0 via the controller's e-stop (bounded,
       fail-closed; a no-op when no live run is active);
    2. ``service.shutdown`` — cancel the tick loop;
    3. ``mcp.stop`` — end the MCP child (after heat-off has landed);
    4. ``record_child_stop_unconfirmed`` — if ``mcp.stop`` could not confirm
       clean teardown (``mcp.stop_unconfirmed``), persist a trace marker (#177)
       while the store is still open;
    5. ``store.close`` — close the decision-trace store.

    Step 4 sits between ``mcp.stop`` and ``store.close`` deliberately: the
    clean-teardown verdict is only known after step 3, and the marker must be
    written before the store closes in step 5.

    (A hard kill / SIGKILL / power loss is uncatchable and skips this entirely —
    it still relies on restart → ``operator_recovery_required``, never an
    auto-resume of heat/fan.) Each step is best-effort: a failure is logged, not
    raised, so one failing step never aborts the rest of the chain or masks the
    error that triggered teardown.

    Args:
        service: The live :class:`~roastpilot_agent.api.RoastService`.
        mcp: The MCP child process to stop after heat-off has landed.
        store: The decision-trace store to close last.
    """
    await _cleanup_step("safe_shutdown_heat_off", service.safe_shutdown_heat_off)
    await _cleanup_step("service.shutdown", service.shutdown)
    await _cleanup_step("mcp.stop", mcp.stop)
    # After mcp.stop so stop_unconfirmed reflects the just-completed teardown;
    # before store.close so the marker can be written.
    await _cleanup_step(
        "record_child_stop_unconfirmed",
        lambda: service.record_child_stop_unconfirmed(stop_unconfirmed=mcp.stop_unconfirmed),
    )
    await _cleanup_step("store.close", store.close)


async def _cleanup_step(name: str, action: Callable[[], Awaitable[object]]) -> None:
    """Run one teardown step, logging (not raising) any failure.

    A failed ``safe_shutdown_heat_off()`` / ``service.shutdown()`` /
    ``mcp.stop()`` / ``store.close()`` must surface in the log but not abort the
    remaining cleanup or mask the error that triggered teardown — so each step
    is independently guarded and logged. The action's return value is ignored
    (``safe_shutdown_heat_off`` returns a bool the chain does not branch on).
    """
    try:
        await action()
    except Exception:  # noqa: BLE001 — best-effort cleanup, logged not raised
        _log.warning("serve teardown step %r failed", name, exc_info=True)


async def _serve_replay(args: argparse.Namespace) -> int:
    """Build and serve the replay app; free-run unless ``--step``."""
    import uvicorn
    from pydantic import ValidationError as _ValErr

    from roastpilot_agent.config_store import ConfigFileError as _CfgErr
    from roastpilot_agent.config_store import load_app_config as _load_cfg
    from roastpilot_agent.replay import clamp_speed, create_replay_app

    export_dir: Path = args.replay
    if not (export_dir / "roast.jsonl").is_file():
        print(f"error: {export_dir} has no roast.jsonl to replay")
        return 2

    # Load and validate the saved config BEFORE allocating any replay resources
    # (aiosqlite worker, ReplaySource) so that a bad saved-config file returns
    # early without leaking them (Codex P2, PR #425).
    try:
        _cfg, _ = _load_cfg()
    except _CfgErr as exc:
        print(f"error: saved-config file is malformed — {exc}")
        return 1
    except _ValErr as exc:
        print(f"error: saved-config file has invalid values — {exc}")
        return 1
    except OSError as exc:
        print(f"error: saved-config file is unreadable — {exc}")
        return 1
    log_level, access_log = _configure_access_log(args, _cfg.logging)

    with tempfile.TemporaryDirectory(prefix="roastpilot-replay-") as tmp:
        store_path = Path(tmp) / "replay.sqlite3"
        app, _service, source = await create_replay_app(
            export_dir,
            store_path,
            config=_cfg,
            step_mode=args.step,
            speed=args.speed,
            spa_dir=_resolve_spa_dir(args),
        )
        runner: asyncio.Task[None] | None = None
        completed = False
        try:
            # Report the *clamped* speed the harness actually runs at (1×–60×), not
            # the raw request — `--speed 100` runs 60×, so the banner must say 60×.
            effective_speed = clamp_speed(args.speed)
            mode = (
                "stepped (paused at tick 0)"
                if args.step
                else f"free-running at {effective_speed:g}x"
            )
            print(
                f"replaying {export_dir.name} ({source.frame_count} frames, {mode}); "
                f"run {source.run_id} on http://{args.host}:{args.port}"
            )
            if not args.step:
                # Free-running replay finishes driving the recorded frames then keeps
                # serving the terminal state — intentional for the screen-recording
                # rig, but non-obvious (the process "hangs" rather than exits). Say so.
                print("  (serves the final frame after the roast ends; Ctrl-C to stop)")
            config = uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                log_level=log_level,
                access_log=access_log,
            )
            server = uvicorn.Server(config)
            runner = asyncio.create_task(server.serve())
            if not args.step:
                await source.run()  # drive the recorded roast at the chosen speed
            await runner
            completed = True
        finally:
            # Uvicorn's lifespan normally closes these resources. Retain CLI
            # ownership as a backstop if serve returns before lifespan startup.
            try:
                if runner is not None and not runner.done():
                    runner.cancel()
                    await asyncio.gather(runner, return_exceptions=True)
            finally:
                if completed:
                    await source.aclose()
                else:
                    await _cleanup_step("replay source close", source.aclose)
    return 0


def main() -> int:
    """Parse arguments and run the agent service.

    ``serve`` drives a live roast; ``--replay`` serves the replay harness;
    without either the scaffold entrypoint prints help."""
    parser = _build_parser()
    args = parser.parse_args()
    # --db is live-serve only; replay is always ephemeral. Combining them would
    # silently ignore --db, so reject it up front rather than mislead (#161).
    if args.replay is not None and args.db is not None:
        parser.error("--db is only valid for 'serve'; replay uses an ephemeral store")
    if args.action == "serve":
        exit_guard = _LiveExitGuard()
        signal_guard = _LiveSignalGuard()
        try:
            with signal_guard:
                try:
                    result = asyncio.run(
                        _serve_live(args, exit_guard=exit_guard, signal_guard=signal_guard)
                    )
                except asyncio.CancelledError:
                    # A first signal during startup uses the temporary
                    # task-cancellation handler. Ordered teardown (when a live
                    # service was already established) has completed before
                    # this reaches the process boundary; preserve the same
                    # conventional SIGINT/SIGTERM result as the later Uvicorn
                    # graceful-shutdown path.
                    _propagate_live_termination(signal_guard.received_signal)
                    raise  # pragma: no cover - defensive non-signal cancellation passthrough
            # Leave the guard before the final sticky check. A signal arriving
            # during restoration is still recorded and seen here; one arriving
            # afterwards reaches the restored OS handler instead of an already-
            # finished Uvicorn server. This also preserves signals received while
            # asyncio.run() was finalizing residual tasks and executors.
            _propagate_live_termination(signal_guard.received_signal)
            return result
        finally:
            exit_guard.disarm()
    if args.replay is not None:
        return asyncio.run(_serve_replay(args))
    parser.print_help()
    return 0
