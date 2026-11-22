# pyright: reportPrivateUsage=false
"""Strict simulation bootstrap, local ownership and source-bound SDK acceptance."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from mcp import ClientSession
from pydantic import ValidationError

from roastpilot_agent import mcp_client
from roastpilot_agent.human_bootstrap import (
    BOOTSTRAP_TOOL_INVENTORY,
    BootstrapInventory,
    BootstrapRead,
    BootstrapRefusal,
    HottopSimulationInventory,
    HumanBootstrapEvidence,
    HumanBootstrapRefused,
    decode_bootstrap_reply,
)
from roastpilot_agent.launch_composition import HottopSimulationLaunchRecord
from roastpilot_agent.mcp_client import HottopSimulationBootstrap, MCPServerProcess

INVENTORY = [
    *BOOTSTRAP_TOOL_INVENTORY[:9],
    "drop_beans_exact_session",
    *BOOTSTRAP_TOOL_INVENTORY[9:],
]
SOURCE_ENV = "ROASTPILOT_HOTTOP_SIMULATION_SOURCE"


def _payload(read: BootstrapRead) -> dict[str, object]:
    if read is BootstrapRead.SERVER_INFO:
        return dict(
            product_name="RoastPilot",
            package_name="coffee-roaster-mcp",
            version="0.2.4",
            transport="stdio",
            current_phase="bootstrap",
            roaster_driver="hottop_kn8828b_2k_plus",
            first_crack_mode="audio",
            session_presence="none",
            bootstrap_safe=False,
            started_at_utc="2026-11-22T00:00:00Z",
            available_bootstrap_tools=list(INVENTORY),
        )
    if read is BootstrapRead.RUNTIME_CONFIG:
        return dict(
            roaster_driver="hottop_kn8828b_2k_plus",
            temperature_unit="celsius",
            first_crack_mode="audio",
            first_crack_event_authority="manual",
            first_crack_observer_protocol="manual/v1",
            config_source="synthetic.yaml",
            roaster_port="in-memory://hottop-kn8828b-2k-plus",
            command_interval_seconds=1.0,
            sample_interval_seconds=1.0,
            auto_t0_drop_threshold_c=5.0,
            model_repo_id="fixture",
            model_precision="int8",
            log_dir="fixture",
            roaster_baudrate=115200,
            allow_manual_override=True,
            auto_t0_detection_enabled=True,
        )
    return dict(
        capture_protocol="capture_evidence/v1",
        intent="bootstrap",
        capability="manual_hottop_simulation_capture",
    )


def _members(read: BootstrapRead, payload: dict[str, object]) -> tuple[object, object, object]:
    return (
        False,
        [{"type": "text", "text": json.dumps(payload)}],
        (
            {"result": copy.deepcopy(payload)}
            if read is BootstrapRead.CAPTURE_EVIDENCE
            else copy.deepcopy(payload)
        ),
    )


def _decode(read: BootstrapRead, payload: dict[str, object]) -> dict[str, object] | None:
    is_error, content, structured = _members(read, payload)
    return decode_bootstrap_reply(
        read,
        is_error=is_error,
        content=content,
        structured=structured,
        config_present=True,
        inventory=HottopSimulationInventory.BOOTSTRAP,
    )


@pytest.mark.parametrize("read", list(BootstrapRead))
def test_simulation_grammar_and_legacy_separation(read: BootstrapRead) -> None:
    """Only the explicit simulation grammar accepts truthful Hottop metadata."""
    payload = _payload(read)
    assert _decode(read, payload) == payload
    is_error, content, structured = _members(read, payload)
    for legacy in (BootstrapInventory.LEGACY, BootstrapInventory.T3A_MANUAL_MOCK):
        assert (
            decode_bootstrap_reply(
                read,
                is_error=is_error,
                content=content,
                structured=structured,
                config_present=True,
                inventory=legacy,
            )
            is None
        )


@pytest.mark.parametrize(
    ("read", "key", "value"),
    [
        (BootstrapRead.SERVER_INFO, "roaster_driver", "mock"),
        (BootstrapRead.SERVER_INFO, "transport", "in_memory"),
        (BootstrapRead.SERVER_INFO, "bootstrap_safe", 0),
        (BootstrapRead.SERVER_INFO, "available_bootstrap_tools", list(reversed(INVENTORY))),
        (
            BootstrapRead.SERVER_INFO,
            "available_bootstrap_tools",
            [*INVENTORY, "get_hottop_receipt"],
        ),
        (BootstrapRead.RUNTIME_CONFIG, "roaster_driver", "mock"),
        (BootstrapRead.RUNTIME_CONFIG, "roaster_port", "/dev/ttyUSB0"),
        (BootstrapRead.RUNTIME_CONFIG, "first_crack_event_authority", "automatic"),
        (BootstrapRead.RUNTIME_CONFIG, "first_crack_observer_protocol", None),
        (BootstrapRead.RUNTIME_CONFIG, "allow_manual_override", 1),
        (BootstrapRead.RUNTIME_CONFIG, "command_interval_seconds", "1"),
        (BootstrapRead.RUNTIME_CONFIG, "sample_interval_seconds", float("inf")),
        (BootstrapRead.CAPTURE_EVIDENCE, "capability", "manual_mock_capture"),
    ],
)
def test_simulation_refuses_wrong_wire_identity(
    read: BootstrapRead, key: str, value: object
) -> None:
    """Wire labels, types, finite values and closed tool order are invariant."""
    payload = _payload(read)
    payload[key] = value
    assert _decode(read, payload) is None


@pytest.mark.parametrize("read", list(BootstrapRead))
def test_simulation_checks_independent_text_and_structure(read: BootstrapRead) -> None:
    """Independent mismatches and extra envelope wrappers cannot become evidence."""
    payload = _payload(read)
    is_error, content, structured = _members(read, payload)
    assert (
        decode_bootstrap_reply(
            read,
            is_error=is_error,
            content=content,
            structured={"result": structured},
            config_present=True,
            inventory=HottopSimulationInventory.BOOTSTRAP,
        )
        is None
    )
    assert (
        decode_bootstrap_reply(
            read,
            is_error=False,
            content=[{"type": "text", "text": "{}"}],
            structured=structured,
            config_present=True,
            inventory=HottopSimulationInventory.BOOTSTRAP,
        )
        is None
    )


@pytest.mark.parametrize("text_value", [2.0, 1])
def test_valid_simulation_members_must_agree_in_value_and_numeric_type(
    text_value: float,
) -> None:
    """Individually valid numeric members still need type-sensitive equality."""
    read = BootstrapRead.RUNTIME_CONFIG
    structured = _payload(read)
    textual = copy.deepcopy(structured)
    textual["command_interval_seconds"] = text_value
    assert _decode(read, structured) == structured
    assert _decode(read, textual) == textual
    assert (
        decode_bootstrap_reply(
            read,
            is_error=False,
            content=[{"type": "text", "text": json.dumps(textual)}],
            structured=structured,
            config_present=True,
            inventory=HottopSimulationInventory.BOOTSTRAP,
        )
        is None
    )


def test_capture_requires_its_single_structured_wrapper() -> None:
    """Plain valid capture text needs exactly the specified structured wrapper."""
    read = BootstrapRead.CAPTURE_EVIDENCE
    payload = _payload(read)
    assert _decode(read, payload) == payload
    assert (
        decode_bootstrap_reply(
            read,
            is_error=False,
            content=[{"type": "text", "text": json.dumps(payload)}],
            structured=payload,
            config_present=True,
            inventory=HottopSimulationInventory.BOOTSTRAP,
        )
        is None
    )


def _owner() -> HottopSimulationBootstrap:
    # Unit ownership tests deliberately use the private issuer. Only the SDK
    # integration below exercises production construction and provenance.
    return HottopSimulationBootstrap(
        seal=mcp_client._SIMULATION_FACTORY_SEAL,
        session=object.__new__(ClientSession),
        record=HottopSimulationLaunchRecord(object(), 1),
        call_timeout_seconds=1.0,
    )


def test_arbitrary_constructor_cannot_mint_binding() -> None:
    """Matching metadata or an arbitrary SDK object cannot issue local trust."""
    with pytest.raises(TypeError, match="composition root"):
        HottopSimulationBootstrap(
            seal=object(),
            session=object.__new__(ClientSession),
            record=HottopSimulationLaunchRecord(object(), 1),
            call_timeout_seconds=1.0,
        )


@pytest.mark.parametrize("foreign_member", ["session", "record"])
def test_real_seal_rejects_subclassed_session_or_record(foreign_member: str) -> None:
    """The real issuer seal cannot admit subclassed infrastructure identities."""

    class SessionSubclass(ClientSession):
        """Subclass used without opening any SDK resources."""

    class RecordSubclass(HottopSimulationLaunchRecord):
        """Subclass carrying otherwise valid immutable fields."""

    session = object.__new__(ClientSession)
    record = HottopSimulationLaunchRecord(object(), 1)
    HottopSimulationBootstrap(
        seal=mcp_client._SIMULATION_FACTORY_SEAL,
        session=session,
        record=record,
        call_timeout_seconds=1.0,
    )
    if foreign_member == "session":
        session = object.__new__(SessionSubclass)
    else:
        record = RecordSubclass(record.composition_identity, 1)
    with pytest.raises(TypeError, match="composition root"):
        HottopSimulationBootstrap(
            seal=mcp_client._SIMULATION_FACTORY_SEAL,
            session=session,
            record=record,
            call_timeout_seconds=1.0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["exact", "wrong_epoch", "owner_subclass"])
async def test_owner_type_and_epoch_are_checked_before_first_sdk_read(
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
) -> None:
    """Only the exact owner at its issued epoch can begin the read sequence."""

    class OwnerSubclass(HottopSimulationBootstrap):
        """Subclass whose inherited fields must supply no local authority."""

    calls: list[BootstrapRead] = []

    async def read(_session: object, kind: BootstrapRead) -> tuple[object, object, object]:
        calls.append(kind)
        return _members(kind, _payload(kind))

    owner_type = OwnerSubclass if identity == "owner_subclass" else HottopSimulationBootstrap
    owner = owner_type(
        seal=mcp_client._SIMULATION_FACTORY_SEAL,
        session=object.__new__(ClientSession),
        record=HottopSimulationLaunchRecord(object(), 2 if identity == "wrong_epoch" else 1),
        call_timeout_seconds=1.0,
    )
    monkeypatch.setattr(mcp_client, "_call_bootstrap_sdk", read)
    result = await owner.read()
    if identity == "exact":
        assert isinstance(result, HumanBootstrapEvidence)
        assert calls == list(BootstrapRead)
    else:
        assert isinstance(result, HumanBootstrapRefused)
        assert result.reason is BootstrapRefusal.MISSING_LAUNCH_FRAMING
        assert calls == []


@pytest.mark.asyncio
async def test_read_only_proof_cannot_be_spent_or_outlive_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only evidence never creates the mock Start proof or command interface."""
    owner = _owner()
    calls: list[BootstrapRead] = []

    async def read(_session: object, kind: BootstrapRead) -> tuple[object, object, object]:
        calls.append(kind)
        return _members(kind, _payload(kind))

    monkeypatch.setattr(mcp_client, "_call_bootstrap_sdk", read)
    result = await owner.read()
    assert isinstance(result, HumanBootstrapEvidence)
    assert result.capture_capability == "manual_hottop_simulation_capture"
    assert calls == list(BootstrapRead)
    assert not hasattr(owner, "_start_proof")
    assert not hasattr(owner, "call_tool")
    assert not hasattr(owner, "_attempt_human_start")
    owner._invalidate()
    result = await owner.read()
    assert isinstance(result, HumanBootstrapRefused)
    assert result.reason is BootstrapRefusal.MISSING_LAUNCH_FRAMING
    assert calls == list(BootstrapRead)


@pytest.mark.asyncio
@pytest.mark.parametrize("displacement", ["session", "record", "composition", "generation"])
async def test_late_reply_cannot_survive_local_replacement(
    monkeypatch: pytest.MonkeyPatch,
    displacement: str,
) -> None:
    """A replacement with the same numeric epoch never inherits old evidence."""
    owner = _owner()
    calls: list[BootstrapRead] = []

    async def read(_session: object, kind: BootstrapRead) -> tuple[object, object, object]:
        calls.append(kind)
        if displacement == "session":
            owner._session = object.__new__(ClientSession)
        elif displacement == "record":
            owner._record = HottopSimulationLaunchRecord(owner._record.composition_identity, 1)
        elif displacement == "composition":
            owner._composition_identity = object()
        else:
            owner._read_generation += 1
        return _members(kind, _payload(kind))

    monkeypatch.setattr(mcp_client, "_call_bootstrap_sdk", read)
    result = await owner.read()
    assert isinstance(result, HumanBootstrapRefused)
    assert result.reason is BootstrapRefusal.DISPLACED_FRAMING
    assert result.read is BootstrapRead.SERVER_INFO
    assert calls == [BootstrapRead.SERVER_INFO]


@pytest.mark.asyncio
async def test_mock_start_route_refuses_simulation_inventory() -> None:
    """The new enum cannot select a simulation grammar on the old Start issuer."""
    process = MCPServerProcess()
    result = await process._read_human_bootstrap(
        inventory=cast(BootstrapInventory, HottopSimulationInventory.BOOTSTRAP)
    )
    assert isinstance(result, HumanBootstrapRefused)
    assert result.reason is BootstrapRefusal.INVALID_PAYLOAD
    assert process._start_proof is None


@pytest.mark.asyncio
async def test_synchronous_decoder_retirement_prevents_next_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retirement during strict decoding refuses before the next SDK request."""
    owner = _owner()
    calls: list[BootstrapRead] = []
    decoded: list[dict[str, object]] = []

    async def read(_session: object, kind: BootstrapRead) -> tuple[object, object, object]:
        calls.append(kind)
        return _members(kind, _payload(kind))

    def retire_after_decode(
        kind: BootstrapRead,
        *,
        is_error: object,
        content: object,
        structured: object,
        config_present: bool,
        inventory: BootstrapInventory | HottopSimulationInventory,
    ) -> dict[str, object] | None:
        result = decode_bootstrap_reply(
            kind,
            is_error=is_error,
            content=content,
            structured=structured,
            config_present=config_present,
            inventory=inventory,
        )
        assert result is not None
        assert result == _payload(BootstrapRead.SERVER_INFO)
        decoded.append(result)
        owner._invalidate()
        return result

    monkeypatch.setattr(mcp_client, "_call_bootstrap_sdk", read)
    monkeypatch.setattr(mcp_client, "decode_bootstrap_reply", retire_after_decode)
    result = await owner.read()
    assert isinstance(result, HumanBootstrapRefused)
    assert result.reason is BootstrapRefusal.DISPLACED_FRAMING
    assert result.read is BootstrapRead.RUNTIME_CONFIG
    assert calls == [BootstrapRead.SERVER_INFO]
    assert decoded == [_payload(BootstrapRead.SERVER_INFO)]


@pytest.mark.asyncio
async def test_foreign_composition_refuses_before_server_or_sdk_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constructor returning a foreign object cannot enter the SDK lifetime."""
    from types import SimpleNamespace

    calls: list[str] = []

    class ForeignComposition:
        """Synthetic constructor which violates the producer's exact type."""

        def __new__(cls, **_kwargs: object) -> object:
            """Return a foreign value before any server can be constructed."""
            calls.append("composition")
            return object.__new__(CompositionSubclass)

        def __init__(self, **_kwargs: object) -> None:
            """Accept the constructor arguments without issuing an exact identity."""

    class CompositionSubclass(ForeignComposition):
        """An isinstance match still fails the factory's exact-type requirement."""

    def forbidden_entry(**_kwargs: object) -> object:
        calls.append("server_or_sdk")
        pytest.fail("foreign composition reached server or SDK creation")

    modules = {
        "coffee_roaster_mcp.hottop_simulation": SimpleNamespace(
            HottopSimulationComposition=ForeignComposition
        ),
        "coffee_roaster_mcp.mcp_server": SimpleNamespace(
            create_hottop_simulation_mcp_server=forbidden_entry
        ),
        "mcp.shared.memory": SimpleNamespace(
            create_connected_server_and_client_session=forbidden_entry
        ),
    }
    original_import = importlib.import_module

    def module_import(name: str) -> object:
        return modules[name] if name in modules else original_import(name)

    monkeypatch.setattr(importlib, "import_module", module_import)
    with pytest.raises(TypeError, match="Simulation composition identity is invalid"):
        async with mcp_client.hottop_simulation_bootstrap(
            config_path=Path("unused.yaml"),
            clock=lambda: 0.0,
            receipt_age_bound_seconds=1.0,
        ):
            pytest.fail("foreign composition admitted")
    assert calls == ["composition"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [0.0, -1.0, float("inf"), float("nan"), True])
async def test_invalid_bounds_refuse_before_optional_producer_import(bound: float) -> None:
    """Nonpositive, nonfinite and coercible bounds never enter a producer context."""
    with pytest.raises(ValueError, match="finite and positive"):
        async with mcp_client.hottop_simulation_bootstrap(
            config_path=Path("unused"),
            clock=lambda: 0.0,
            receipt_age_bound_seconds=bound,
        ):
            pytest.fail("invalid factory bounds admitted")


def test_registered_hottop_simulation_read_only(tmp_path: Path) -> None:
    """Use exact opt-in producer source over the actual SDK without any Start."""
    source_value = os.environ.get(SOURCE_ENV)
    if source_value is None:
        pytest.skip("requires the explicitly source-bound 440a6715 simulation producer")
    source = Path(source_value).resolve()
    expected = {
        "hottop_simulation.py": "bb199ad4b8cfd846198b4e40988dc2ed3bfa41717cc6c9644493eec7e383de9b",
        "config.py": "4ac44800d6517a5491e38e91a56be8b7b9256e291c363d1e1861e5f9ef6dd1b9",
        "first_crack_runtime.py": (
            "aebd5718670251ac402c3637bd0874d145a03be9c944700f1dd63b8f7247ef35"
        ),
        "mcp_server.py": "fef1127470ed28831a4afd1391d48f66400286448f3ecf21b711b662a1a2c36c",
    }
    for filename, digest in expected.items():
        assert (
            hashlib.sha256((source / "src/coffee_roaster_mcp" / filename).read_bytes()).hexdigest()
            == digest
        )
    script = r'''
import asyncio
import time
import wave
from pathlib import Path
from unittest.mock import patch
from coffee_roaster_mcp import audio, artifacts, ambient_runtime, mcp_server
from roastpilot_agent.human_bootstrap import (
    BootstrapRefusal, HumanBootstrapEvidence, HumanBootstrapRefused,
    HumanStartRefused, StartRefusal,
)
from roastpilot_agent import mcp_client
root = Path.cwd()
with wave.open(str(root / 'silence.wav'), 'wb') as output:
    output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
    output.writeframes(bytes(32000))
(root / 'model').mkdir()
config = root / 'simulation.yaml'
config.write_text(f"""roaster:
  driver: hottop_kn8828b_2k_plus
  port: in-memory://hottop-kn8828b-2k-plus
  hottop_reception: candidate/v1
  temperature_unit: celsius
first_crack:
  mode: audio
  event_authority: manual
  local_model_dir: {root / 'model'}
audio:
  source: wav
  wav_path: {root / 'silence.wav'}
recording:
  enabled: true
  autocapture: true
  export_location: {root / 'recording'}
ambient:
  mode: disabled
logging:
  log_dir: {root / 'logs'}
""")
compositions = []
original_server = mcp_server.create_hottop_simulation_mcp_server
original_read = mcp_client._call_bootstrap_sdk
reads = []
def make_server(**kwargs):
    compositions.append(kwargs['composition'])
    return original_server(**kwargs)
def forbidden(*args, **kwargs):
    raise AssertionError('read-only bootstrap reached a device/model factory')
async def read(session, kind):
    reads.append(kind.value)
    return await original_read(session, kind)
async def run():
    with patch.object(mcp_server, 'create_hottop_simulation_mcp_server', make_server), \
         patch.object(audio, '_load_sounddevice', forbidden), \
         patch.object(ambient_runtime, 'build_configured_ambient_reader', forbidden), \
         patch.object(artifacts, '_hf_hub_download', forbidden), \
         patch.object(mcp_client, '_call_bootstrap_sdk', read):
        async with mcp_client.hottop_simulation_bootstrap(
            config_path=config, clock=time.monotonic, receipt_age_bound_seconds=1.0,
        ) as owner:
            assert owner._record.composition_identity is compositions[0]
            result = await owner.read()
            assert isinstance(result, HumanBootstrapEvidence), result
            assert result.capture_capability == 'manual_hottop_simulation_capture'
            legacy = mcp_client.MCPServerProcess(session=owner._session)
            refused = await legacy._read_human_bootstrap()
            assert isinstance(refused, HumanBootstrapRefused)
            assert refused.reason is BootstrapRefusal.MISSING_LAUNCH_FRAMING
            claim = await legacy._attempt_human_start(result, gate=lambda: True)
            assert isinstance(claim, HumanStartRefused) and claim.reason is StartRefusal.NO_PROOF
            assert legacy._start_proof is None
            assert reads == ['get_server_info', 'get_runtime_config', 'get_capture_evidence']
            assert compositions[0].serial_factory_calls == 0
            assert compositions[0].transport.writes_snapshot() == ()
            assert not hasattr(owner, '_start_proof')
            assert not hasattr(owner, 'call_tool')
        after = await owner.read()
        assert isinstance(after, HumanBootstrapRefused)
        assert after.reason is BootstrapRefusal.MISSING_LAUNCH_FRAMING
        assert reads == ['get_server_info', 'get_runtime_config', 'get_capture_evidence']
asyncio.run(run())
print('registered read-only bootstrap passed; no Start, device, model or actuator call')
'''
    env = dict(os.environ)
    env.update(
        PYTHONPATH=os.pathsep.join((str(source / "src"), str(Path(__file__).parents[1] / "src"))),
        PYTHONDONTWRITEBYTECODE="1",
        HF_HUB_OFFLINE="1",
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "registered read-only bootstrap passed" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "timeout", "payload", "validation"])
async def test_read_failure_never_continues_to_later_reads(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Timeout, transport failure and invalid envelopes all stop the read batch."""
    owner = _owner()
    owner._call_timeout_seconds = 0.01
    calls: list[BootstrapRead] = []

    async def read(_session: object, kind: BootstrapRead) -> tuple[object, object, object]:
        calls.append(kind)
        if failure == "transport":
            raise OSError("fixture transport unavailable")
        if failure == "validation":
            raise ValidationError.from_exception_data("fixture", [])
        if failure == "timeout":
            await asyncio.Event().wait()
        return True, [], {}

    monkeypatch.setattr(mcp_client, "_call_bootstrap_sdk", read)
    result = await owner.read()
    assert isinstance(result, HumanBootstrapRefused)
    assert result.reason is (
        BootstrapRefusal.INVALID_PAYLOAD
        if failure in {"payload", "validation"}
        else BootstrapRefusal.READ_FAILED
    )
    assert result.read is BootstrapRead.SERVER_INFO
    assert calls == [BootstrapRead.SERVER_INFO]


@pytest.mark.asyncio
async def test_cancellation_propagates_without_any_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot return partial bootstrap evidence."""
    owner = _owner()

    async def read(_session: object, _kind: BootstrapRead) -> tuple[object, object, object]:
        raise asyncio.CancelledError

    monkeypatch.setattr(mcp_client, "_call_bootstrap_sdk", read)
    with pytest.raises(asyncio.CancelledError):
        await owner.read()


@pytest.mark.asyncio
@pytest.mark.parametrize("body_fails", [False, True])
async def test_composition_factory_owns_record_and_invalidates_before_exit(
    monkeypatch: pytest.MonkeyPatch,
    body_fails: bool,
) -> None:
    """The local factory issues one identity and retires it before SDK teardown."""
    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager
    from dataclasses import FrozenInstanceError
    from types import SimpleNamespace

    composition_args: list[dict[str, object]] = []
    compositions: list[object] = []
    teardown_checks: list[bool] = []
    owners: list[HottopSimulationBootstrap] = []
    session = object.__new__(ClientSession)
    server = object()

    class Composition:
        """Local factory boundary stand-in; actual SDK proof is separate."""

        def __init__(self, **kwargs: object) -> None:
            """Retain the root's arguments for an independent assertion."""
            composition_args.append(kwargs)
            compositions.append(self)

    def build_server(**kwargs: object) -> object:
        assert kwargs == {"config_path": Path("synthetic.yaml"), "composition": compositions[0]}
        return server

    @asynccontextmanager
    async def connect(server_arg: object, **_kwargs: object) -> AsyncGenerator[ClientSession, None]:
        assert server_arg is server
        try:
            yield session
        finally:
            teardown_checks.append(owners[0]._session is None)

    modules = {
        "coffee_roaster_mcp.hottop_simulation": SimpleNamespace(
            HottopSimulationComposition=Composition
        ),
        "coffee_roaster_mcp.mcp_server": SimpleNamespace(
            create_hottop_simulation_mcp_server=build_server
        ),
        "mcp.shared.memory": SimpleNamespace(create_connected_server_and_client_session=connect),
    }
    original_import = importlib.import_module

    def module_import(name: str) -> object:
        return modules[name] if name in modules else original_import(name)

    monkeypatch.setattr(importlib, "import_module", module_import)

    def clock() -> float:
        return 10.0

    async def use_factory() -> None:
        async with mcp_client.hottop_simulation_bootstrap(
            config_path=Path("synthetic.yaml"),
            clock=clock,
            receipt_age_bound_seconds=1.0,
        ) as owner:
            owners.append(owner)
            assert owner._session is session
            assert owner._record.composition_identity is compositions[0]
            assert owner._record.child_epoch == 1
            assert composition_args == [{"clock": clock, "receipt_age_bound_seconds": 1.0}]
            with pytest.raises(FrozenInstanceError):
                owner._record.child_epoch = 2  # type: ignore[misc]
            if body_fails:
                raise RuntimeError("caller exits early")

    if body_fails:
        with pytest.raises(RuntimeError, match="caller exits early"):
            await use_factory()
    else:
        await use_factory()
    assert teardown_checks == [True]
    assert isinstance(await owners[0].read(), HumanBootstrapRefused)
