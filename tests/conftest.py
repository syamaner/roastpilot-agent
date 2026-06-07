"""Shared test fixtures (component plan §8).

Placeholders established at E1; each grows into the real test double with
its epic: fake MCP client (E4/E5), fake advisor (E8), temp SQLite store
(E6), event-sink test double (E4/E7). All M1 tests run hardware-free.
"""

from pathlib import Path

import pytest

from roastpilot_agent.advisor import AdvisorContext, RoastAdvisor, RoastDecision
from roastpilot_agent.store import RoastStore


class FakeMCPClient:
    """Test double for RoasterMCPClient.

    The scripted 13-tool contract (state sequences, latched T0/FC events,
    read/write fault injection) lands in E4/E5.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []


class ScriptedAdvisor(RoastAdvisor):
    """Deterministic advisor double returning pre-scripted decisions.

    The full fixture set (valid / malformed / unsafe / timeout / provider
    error) lands in E8.
    """

    def __init__(self, decisions: list[RoastDecision] | None = None) -> None:
        self._decisions: list[RoastDecision] = list(decisions or [])

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Pop and return the next scripted decision."""
        if not self._decisions:
            raise AssertionError("ScriptedAdvisor has no scripted decisions left")
        return self._decisions.pop(0)


class EventSink:
    """Records emitted UI events for assertions (E4/E7)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def emit(self, kind: str, payload: object) -> None:
        """Record an emitted event."""
        self.events.append((kind, payload))


@pytest.fixture
def fake_mcp_client() -> FakeMCPClient:
    """A fake MCP client with no scripted behavior yet (E4/E5)."""
    return FakeMCPClient()


@pytest.fixture
def scripted_advisor() -> ScriptedAdvisor:
    """A deterministic advisor double with an empty script (E8)."""
    return ScriptedAdvisor()


@pytest.fixture
def event_sink() -> EventSink:
    """An event-sink test double recording emitted events (E4/E7)."""
    return EventSink()


@pytest.fixture
def tmp_store(tmp_path: Path) -> RoastStore:
    """A RoastStore backed by a temporary SQLite path (initialization: E6)."""
    return RoastStore(db_path=tmp_path / "roastpilot-test.sqlite3")
