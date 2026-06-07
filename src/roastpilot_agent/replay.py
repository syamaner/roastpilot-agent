"""Replay harness (component plan §7).

Streams a recorded roast export (JSONL/CSV from real past roasts) through
the *real* SSE pipeline at 1×–60×: UI development without hardware,
deterministic UI tests, and the talk's screen-capture rig. Complements —
never replaces — full-loop simulation via the MCP mock driver + WAV audio.
Implementation lands in E10.
"""

from pathlib import Path


class ReplaySource:
    """Streams a recorded roast export through the SSE pipeline (E10)."""

    def __init__(self, export_path: Path, speed: float = 1.0) -> None:
        self._export_path = export_path
        self._speed = speed

    async def stream(self) -> None:
        """Replay the export at the configured speed (E10)."""
        raise NotImplementedError("E10: replay harness")
