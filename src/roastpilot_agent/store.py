"""SQLite persistence (component plan §5).

aiosqlite store with WAL + ``synchronous=FULL``; commit per tick during
active roasts. Schema v1 (roast_runs, roast_events, telemetry_snapshots,
safety_evaluations, advisor_decisions, command_log, operator_actions,
sync_jobs, reference_roasts) and recovery reads land in E6.
"""

from pathlib import Path


class RoastStore:
    """aiosqlite-backed persistence and recovery reads (E6)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file."""
        return self._db_path

    async def initialize(self) -> None:
        """Open the database, set durability PRAGMAs, apply schema v1 (E6)."""
        raise NotImplementedError("E6: store schema v1")
