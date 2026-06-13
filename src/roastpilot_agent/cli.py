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
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from roastpilot_agent import __version__

if TYPE_CHECKING:
    from roastpilot_agent.mcp_client import RuntimeConfigSnapshot, ToolCaller

_log = logging.getLogger(__name__)


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
        help="replay speed multiplier 1x-60x (1x is the screen-recording rig); ignored with --step",
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


async def _serve_live(args: argparse.Namespace) -> int:
    """Build and serve the live roast app, then clean up the MCP child.

    Uses the recovery lifespan (``create_app``'s default — restart →
    ``operator_recovery_required``, never an auto-resume of heat/fan).
    Fail-closed: an MCP start failure prints a clear message and returns a
    non-zero exit, with the child cleaned up by
    :func:`~roastpilot_agent.live.build_live_service`."""
    import uvicorn

    from roastpilot_agent.api import create_app
    from roastpilot_agent.config import AppConfig
    from roastpilot_agent.live import build_live_service, forward_coffee_env
    from roastpilot_agent.mcp_client import MCPConnectionError

    config = AppConfig()
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
        print(f"error: could not start coffee-roaster-mcp: {exc}")
        return 1

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

        uv = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        server = uvicorn.Server(uv)
        # _lifespan runs recover_on_start (restart → recovery) on startup and
        # service.shutdown() on teardown; we stop the MCP child after the
        # server returns (graceful shutdown / SIGINT) and close the store.
        await server.serve()
    finally:
        # Best-effort cleanup: each step is logged-not-raised so one failure
        # never aborts the rest of the chain (or masks the original error).
        await _cleanup_step("service.shutdown", service.shutdown)
        await _cleanup_step("mcp.stop", mcp.stop)
        await _cleanup_step("store.close", store.close)
    return 0


async def _cleanup_step(name: str, action: Callable[[], Awaitable[None]]) -> None:
    """Run one teardown step, logging (not raising) any failure.

    A failed ``service.shutdown()`` / ``mcp.stop()`` / ``store.close()`` must
    surface in the log but not abort the remaining cleanup or mask the error
    that triggered teardown — so each step is independently guarded and logged.
    """
    try:
        await action()
    except Exception:  # noqa: BLE001 — best-effort cleanup, logged not raised
        _log.warning("live-serve teardown step %r failed", name, exc_info=True)


async def _serve_replay(args: argparse.Namespace) -> int:
    """Build and serve the replay app; free-run unless ``--step``."""
    import uvicorn

    from roastpilot_agent.replay import clamp_speed, create_replay_app

    export_dir: Path = args.replay
    if not (export_dir / "roast.jsonl").is_file():
        print(f"error: {export_dir} has no roast.jsonl to replay")
        return 2

    with tempfile.TemporaryDirectory(prefix="roastpilot-replay-") as tmp:
        store_path = Path(tmp) / "replay.sqlite3"
        app, _service, source = await create_replay_app(
            export_dir,
            store_path,
            step_mode=args.step,
            speed=args.speed,
            spa_dir=_resolve_spa_dir(args),
        )
        # Report the *clamped* speed the harness actually runs at (1×–60×), not
        # the raw request — `--speed 100` runs 60×, so the banner must say 60×.
        effective_speed = clamp_speed(args.speed)
        mode = (
            "stepped (paused at tick 0)" if args.step else f"free-running at {effective_speed:g}x"
        )
        print(
            f"replaying {export_dir.name} ({source.frame_count} frames, {mode}); "
            f"run {source.run_id} on http://{args.host}:{args.port}"
        )
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        server = uvicorn.Server(config)
        runner = asyncio.create_task(server.serve())
        if not args.step:
            await source.run()  # drive the recorded roast at the chosen speed
        await runner
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
        return asyncio.run(_serve_live(args))
    if args.replay is not None:
        return asyncio.run(_serve_replay(args))
    parser.print_help()
    return 0
