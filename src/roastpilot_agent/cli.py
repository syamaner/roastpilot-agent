"""Console entrypoint for the roastpilot-agent service.

``--replay`` streams a recorded roast export through the real SSE pipeline
(E10-S1): UI development without hardware, deterministic Playwright snapshots
(``--step``), and the talk's 1× screen-recording rig (``--speed 1``). Serving
a *live* roast (the wired MCP child) is the E9/E11 surface and is not wired
here yet.
"""

import argparse
import asyncio
import tempfile
from pathlib import Path

from roastpilot_agent import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roastpilot-agent",
        description="Deterministic agent harness for autonomous coffee roasting.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default 8000)")
    return parser


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
            export_dir, store_path, step_mode=args.step, speed=args.speed
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

    ``--replay`` serves the replay harness; without it the scaffold entrypoint
    prints help (the live-serve path lands in E9/E11)."""
    parser = _build_parser()
    args = parser.parse_args()
    if args.replay is not None:
        return asyncio.run(_serve_replay(args))
    parser.print_help()
    return 0
