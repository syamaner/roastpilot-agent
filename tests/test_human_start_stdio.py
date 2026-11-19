# pyright: reportPrivateUsage=false
"""C2b-2A real SDK stdio tests with a portable generated protocol peer."""

import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from roastpilot_agent.human_bootstrap import (
    HumanBootstrapEvidence,
    HumanStartAccepted,
    HumanStartRefused,
    HumanStartUncertain,
    StartAttemptState,
    StartRefusal,
    StartUncertainty,
)
from tests.test_mcp_client import (  # pyright: ignore[reportPrivateUsage]
    _BS_PAYLOADS,
    _bs_process,
    _bs_raw,
    _bs_read,
    _hs_payload,
)

# Real subprocess stdio uses process-global SDK cleanup and runs in the serial lane.
pytestmark = [pytest.mark.slow, pytest.mark.serial]

_PEER = """
import json
import sys
from pathlib import Path

fixture = json.loads(Path(sys.argv[1]).read_text())
ledger = Path(sys.argv[2])
mode = sys.argv[3]
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "synthetic-c2b-start", "version": "1"}}
    elif method == "tools/call":
        name = request["params"]["name"]
        args = request["params"].get("arguments", {})
        with ledger.open("a") as output:
            output.write(json.dumps({"name": name, "arguments": args}) + "\\n")
        if name == "set_recording_metadata":
            payload = args
            result = {"content": [{"type": "text", "text": json.dumps(payload)}],
                      "structuredContent": payload, "isError": False}
        else:
            result = fixture[name]
        if name == "start_roast_session":
            if mode == "error":
                # Models a producer which published its session before returning an error.
                (ledger.parent / "published").write_text("active")
                result = {"content": [{"type": "text", "text": "synthetic failure"}],
                          "isError": True}
            elif mode == "sdk-invalid":
                result["isError"] = 1
            elif mode == "malformed":
                result["structuredContent"]["session"]["active"] = 1
    else:
        raise AssertionError("Unexpected SDK request: " + str(method))
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["positive", "malformed", "error", "displaced", "sdk-invalid"])
async def test_c2b_private_start_uses_real_stdio_without_discovery(
    tmp_path: Path, mode: str
) -> None:
    """The captured SDK session claims one request and retains reply uncertainty."""
    peer = tmp_path / "peer.py"
    peer.write_text(_PEER)
    fixtures = {read.value: _bs_raw(read) for read in _BS_PAYLOADS}
    fixtures["start_roast_session"] = {
        "content": [{"type": "text", "text": json.dumps(_hs_payload())}],
        "structuredContent": _hs_payload(),
        "isError": False,
    }
    data = tmp_path / "payloads.json"
    data.write_text(json.dumps(fixtures))
    ledger = tmp_path / "calls.jsonl"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-I", str(peer), str(data), str(ledger), mode],
        cwd=tmp_path,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as sdk:
        await sdk.initialize()
        process = _bs_process(sdk)
        evidence = await _bs_read(process)
        assert isinstance(evidence, HumanBootstrapEvidence)
        gates = 0

        def gate() -> bool:
            nonlocal gates
            gates += 1
            if mode == "displaced" and gates == 2:
                process._replace_session(None)
            return True

        result = await process._attempt_human_start(
            evidence, gate=gate, origin="brazil-santos", roast_num=0
        )
        if mode == "positive":
            assert result == HumanStartAccepted("a" * 32, process.child_epoch)
            assert process._human_start_state is StartAttemptState.ACCEPTED
        else:
            expected = {
                "error": StartUncertainty.PRODUCER_ERROR,
                "sdk-invalid": StartUncertainty.TRANSPORT_FAILED,
                "malformed": StartUncertainty.INVALID_REPLY,
                "displaced": StartUncertainty.DISPLACED_AFTER_CLAIM,
            }
            assert result == HumanStartUncertain(expected[mode])
            assert process._human_start_state is StartAttemptState.CLAIMED
        assert gates == 2
        assert await process._attempt_human_start(evidence, gate=lambda: True) == HumanStartRefused(
            StartRefusal.ALREADY_ATTEMPTED
        )
    calls = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [row["name"] for row in calls] == [
        "get_server_info",
        "get_runtime_config",
        "get_capture_evidence",
        "set_recording_metadata",
        "start_roast_session",
    ]
    assert calls[-1]["arguments"] == {"purpose": "roast", "first_crack_observer_ack": "manual/v1"}
    assert calls[-2]["arguments"] == {"origin": "brazil-santos", "roast_num": 0}
    assert (tmp_path / "published").exists() is (mode == "error")
