"""Hardware-free tests for the synchronous cold-characterisation CLI layer (#954 U4).

No test reads real ``/proc``, opens a serial port, spawns an MCP child or calls a
provider: the config loader, host-fact reader, host-bound reader, SPA resolver and
the hosted runner are replaced per test, and the host-fact reader is driven through
its injected opener.
"""

# pyright: reportPrivateUsage=false

import builtins
import importlib.metadata
import os
import sys
import typing
from collections.abc import Callable
from pathlib import Path

import pydantic
import pytest

from roastpilot_agent import cli, cold_cli, cold_runner, live
from roastpilot_agent.cold_characterisation.host import (
    ColdHostBoundError,
    ColdHostBoundFailure,
)
from roastpilot_agent.cold_characterisation.identity import BOOT_ID_PATH, ColdArtefactKind
from roastpilot_agent.cold_composition import ColdCompositionInputs, ColdHostFacts
from roastpilot_agent.config import AppConfig
from roastpilot_agent.config_store import ConfigFileError

MARKER = "PLANTEDMARKER7f3c"
SUMMARY_KEYS = (
    "mode",
    "run_invoked",
    "result",
    "cli_refusal",
    "composition_refusal",
    "outcome",
    "start_refusal",
    "termination_reason",
    "child_ownership",
    "advisory_path",
    "provider_check",
    "conformance_outcome",
    "manifest_sha256",
    "signal",
    "exit_code",
)
REVISION = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "ab" * 32
SECONDARY = "/srv/secondary-evidence-root"


def base_argv(**overrides: str | None) -> list[str]:
    """A complete valid argv; ``None`` drops a flag, a value replaces it."""
    values: dict[str, str | None] = {
        "--profile-name": "cold-profile",
        "--target-drop-temp-c": "200.5",
        "--evidence-dir": "/srv/primary-evidence-root",
        "--secondary-evidence-dir": SECONDARY,
        "--audio-device-identity": "usb-mic-1",
        "--serial-port-path": "/dev/ttyUSB-test",
        "--stimulus-block": "stimulus-a",
        "--operator-host-notes": "host notes",
        "--operator-psu-notes": "psu notes",
        "--operator-cooling-notes": "cooling notes",
        "--source-revision": REVISION,
        "--source-tree": "clean",
        "--artefact-kind": "editable_source",
    }
    values.update(overrides)
    argv: list[str] = []
    for flag, value in values.items():
        if value is not None:
            argv.extend([flag, value])
    return argv


def parse_summary(text: str) -> dict[str, str]:
    """Parse the closed summary, asserting the exact 15 keys in order."""
    lines = [line for line in text.splitlines() if "=" in line]
    keys = tuple(line.split("=", 1)[0] for line in lines)
    assert keys == SUMMARY_KEYS
    return dict(line.split("=", 1) for line in lines)


def make_facts() -> ColdHostFacts:
    return ColdHostFacts(
        coffee_roaster_mcp_version="0.2.2",
        python_version="3.11.9",
        platform="Linux-test",
        machine="aarch64",
        operating_system="Linux",
        kernel="6.6.0-test",
        pi_model="Raspberry Pi 5 Model B Rev 1.0",
        pi_revision="c04170",
        boot_id_path=BOOT_ID_PATH,
    )


class Harness:
    """Records every external port the CLI reaches."""

    def __init__(self, spa_dir: Path) -> None:
        self.spa_dir = spa_dir
        self.config_loads = 0
        self.hosted: list[tuple[AppConfig, ColdCompositionInputs, dict[str, object]]] = []
        self.code = 0


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip product/provider env, reset the runner latch, forbid env forwarding."""
    for name in list(os.environ):
        if name.upper().startswith(("ROASTPILOT_", "COFFEE_")) or name == "OPENROUTER_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.setattr(cold_runner, "_CONSUMED", False)
    monkeypatch.setattr(cold_runner, "_REPORTED_EXIT", None)
    monkeypatch.setattr(cold_runner, "_RUN_INVOKED", False)

    def forbidden(*_args: object, **_kwargs: object) -> typing.NoReturn:
        raise AssertionError("normal-mode live wiring reached")

    monkeypatch.setattr(live, "forward_coffee_env", forbidden)
    monkeypatch.setattr(live, "build_live_service", forbidden)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Harness:
    spa = tmp_path / "spa"
    spa.mkdir()
    (spa / "index.html").write_text("<html>cold</html>", encoding="utf-8")
    state = Harness(spa)

    def load() -> tuple[AppConfig, frozenset[str]]:
        state.config_loads += 1
        return AppConfig(), frozenset()

    async def run_hosted(config: AppConfig, inputs: ColdCompositionInputs, **kwargs: object) -> int:
        state.hosted.append((config, inputs, kwargs))
        return state.code

    monkeypatch.setattr(cold_cli, "load_app_config", load)
    monkeypatch.setattr(cold_cli, "read_host_facts", make_facts)
    monkeypatch.setattr(cold_cli, "LinuxHostBoundsReader", lambda: object())
    monkeypatch.setattr(cold_cli, "default_spa_dir", lambda: spa)
    monkeypatch.setattr(cold_runner, "run_hosted", run_hosted)
    return state


# --- 1. dispatch -------------------------------------------------------------------


def test_exact_token_dispatches_to_the_cold_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``cold-characterisation`` reaches the cold parser (its fixed help)."""
    monkeypatch.setattr(sys, "argv", ["roastpilot-agent", "cold-characterisation", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "usage: roastpilot-agent cold-characterisation" in capsys.readouterr().out


@pytest.mark.parametrize(
    "token", ["cold-characterisationx", "Cold-characterisation", "--cold-characterisation"]
)
def test_near_miss_tokens_fall_through_to_the_unchanged_normal_parser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], token: str
) -> None:
    """C1: only the exact token dispatches; near misses hit the normal parser."""
    monkeypatch.delitem(sys.modules, "roastpilot_agent.cold_cli")
    monkeypatch.setattr(sys, "argv", ["roastpilot-agent", token])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "usage: roastpilot-agent [-h]" in capsys.readouterr().err
    assert "roastpilot_agent.cold_cli" not in sys.modules


@pytest.mark.parametrize("argv", [["--help"], ["--version"]], ids=["help", "version"])
def test_normal_entry_never_imports_cold_modules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """The lazy dispatch keeps normal help/version free of cold imports."""
    monkeypatch.delitem(sys.modules, "roastpilot_agent.cold_cli")
    monkeypatch.delitem(sys.modules, "roastpilot_agent.cold_runner")
    monkeypatch.setattr(sys, "argv", ["roastpilot-agent", *argv])
    with pytest.raises(SystemExit):
        cli.main()
    capsys.readouterr()
    assert "roastpilot_agent.cold_cli" not in sys.modules
    assert "roastpilot_agent.cold_runner" not in sys.modules


def test_normal_replay_entry_never_imports_cold_modules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A parsed normal action (``--replay``) also stays free of cold imports."""
    monkeypatch.delitem(sys.modules, "roastpilot_agent.cold_cli")
    monkeypatch.delitem(sys.modules, "roastpilot_agent.cold_runner")
    monkeypatch.setattr(sys, "argv", ["roastpilot-agent", "--replay", str(tmp_path)])
    assert cli.main() == 2
    capsys.readouterr()
    assert "roastpilot_agent.cold_cli" not in sys.modules
    assert "roastpilot_agent.cold_runner" not in sys.modules


def test_normal_parser_grammar_is_unchanged() -> None:
    """The normal parser keeps its action choices and never mentions the cold mode."""
    parser = cli._build_parser()
    action = next(item for item in parser._actions if item.dest == "action")
    assert action.choices == ["serve", "appliance"]
    assert "cold" not in parser.format_help().casefold()


# --- 2. grammar ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    [
        "--profile-name",
        "--target-drop-temp-c",
        "--evidence-dir",
        "--secondary-evidence-dir",
        "--audio-device-identity",
        "--serial-port-path",
        "--stimulus-block",
        "--operator-host-notes",
        "--operator-psu-notes",
        "--operator-cooling-notes",
        "--source-revision",
        "--source-tree",
        "--artefact-kind",
    ],
)
def test_each_missing_required_flag_is_a_fixed_usage_error(
    harness: Harness, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        cold_cli.main(base_argv(**{flag: None}))
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.err == cold_cli.USAGE_ERROR_LINE
    assert captured.out == ""
    assert harness.config_loads == 0


@pytest.mark.parametrize(
    "argv",
    [
        base_argv(**{"--target-drop-temp-c": "nan"}),
        base_argv(**{"--target-drop-temp-c": "inf"}),
        base_argv(**{"--target-drop-temp-c": "-inf"}),
        base_argv(**{"--target-drop-temp-c": f"abc{MARKER}"}),
        base_argv(**{"--source-tree": MARKER}),
        [*base_argv(), f"--x={MARKER}"],
        [*base_argv(**{"--profile-name": None}), "--profile", MARKER],
        base_argv(**{"--evidence-dir": f"relative/{MARKER}"}),
        base_argv(**{"--secondary-evidence-dir": MARKER}),
        base_argv(**{"--source-revision": MARKER}),
        base_argv(**{"--source-revision": REVISION.upper()}),
        base_argv(**{"--profile-name": ""}),
        [*base_argv(), "--artefact-sha256", MARKER],
        [*base_argv(), "--port", MARKER],
        [*base_argv(), "--protected-root", MARKER],
    ],
)
def test_malformed_values_are_fixed_usage_errors_without_echo(
    harness: Harness, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """C15: every parser diagnostic is the fixed line; no planted value is echoed."""
    with pytest.raises(SystemExit) as exc:
        cold_cli.main(argv)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.err == cold_cli.USAGE_ERROR_LINE
    assert MARKER not in captured.out + captured.err
    assert harness.config_loads == 0


#: Every single-use flag with two VALID values, so only the duplicate rule can refuse
#: it.  Free-text values carry the marker, which must never be echoed.
DUPLICATE_ROWS: list[tuple[str, list[str]]] = [
    ("profile", [*base_argv(**{"--profile-name": "a"}), "--profile-name", f"b-{MARKER}"]),
    ("drop", [*base_argv(), "--target-drop-temp-c", "201.0"]),
    ("evidence", [*base_argv(), "--evidence-dir", f"/srv/{MARKER}"]),
    ("secondary", [*base_argv(), "--secondary-evidence-dir", f"/srv/2-{MARKER}"]),
    ("audio", [*base_argv(), "--audio-device-identity", f"mic-{MARKER}"]),
    ("serial", [*base_argv(), "--serial-port-path", f"/dev/{MARKER}"]),
    ("stimulus", [*base_argv(), "--stimulus-block", f"s-{MARKER}"]),
    ("host-notes", [*base_argv(), "--operator-host-notes", f"h-{MARKER}"]),
    ("psu-notes", [*base_argv(), "--operator-psu-notes", f"p-{MARKER}"]),
    ("cooling-notes", [*base_argv(), "--operator-cooling-notes", f"c-{MARKER}"]),
    ("revision", [*base_argv(), "--source-revision", "f" * 40]),
    ("tree", [*base_argv(**{"--source-tree": "clean"}), "--source-tree", "dirty"]),
    ("kind", [*base_argv(**{"--artefact-kind": "wheel"}), "--artefact-kind", "sdist"]),
    ("digest", [*base_argv(), "--artefact-sha256", DIGEST, "--artefact-sha256", "cd" * 32]),
    ("host", [*base_argv(), "--host", "127.0.0.1", "--host", "0.0.0.0"]),
    ("port", [*base_argv(), "--port", "8001", "--port", "8002"]),
    ("spa", [*base_argv(), "--spa-dir", "/srv/a", "--spa-dir", f"/srv/{MARKER}"]),
]


def exit_status(argv: list[str]) -> object:
    """The CLI's exit status: a ``SystemExit`` code or the returned code."""
    try:
        return cold_cli.main(argv)
    except SystemExit as exc:
        return exc.code


@pytest.mark.parametrize(
    "argv", [row for _, row in DUPLICATE_ROWS], ids=[name for name, _ in DUPLICATE_ROWS]
)
def test_duplicate_single_use_options_are_fixed_usage_errors(
    harness: Harness, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """C22/C30: every option except ``--protected-root`` is single-use, even when valid."""
    assert exit_status(argv) == 2
    captured = capsys.readouterr()
    assert captured.err == cold_cli.USAGE_ERROR_LINE
    assert MARKER not in captured.out + captured.err
    assert harness.config_loads == 0


def test_every_single_use_flag_has_an_all_valid_duplicate_row() -> None:
    """The duplicate rows cover every option except the repeatable ``--protected-root``."""
    flags = {
        action.option_strings[0]
        for action in cold_cli.build_parser()._actions
        if action.option_strings and action.dest not in {"help", "protected_root"}
    }
    duplicated = {flag for _, row in DUPLICATE_ROWS for flag in flags if row.count(flag) == 2}
    assert duplicated == flags


def test_help_is_fixed_grammar_with_only_host_and_port_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cold_cli.main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--target-drop-temp-c" in text
    assert "Celsius" in text
    defaults = [line for line in text.splitlines() if "default" in line]
    assert len(defaults) == 2
    assert any("default 127.0.0.1" in line for line in defaults)
    assert any("default 8000" in line for line in defaults)


@pytest.mark.parametrize("drop", ["-40.0", "1000.0"])
def test_accepted_inputs_reach_the_runner_without_range_or_guidance(
    harness: Harness, capsys: pytest.CaptureFixture[str], drop: str
) -> None:
    """No temperature range; charge guidance is always ``None``; defaults resolve."""
    argv = [
        *base_argv(**{"--target-drop-temp-c": drop}),
        "--protected-root",
        "/a",
        "--protected-root",
        "/b",
    ]
    assert cold_cli.main(argv) == 0
    config, inputs, kwargs = harness.hosted[0]
    assert type(config) is AppConfig
    assert inputs.spec.target_drop_temp_c == float(drop)
    assert inputs.spec.charge_guidance_min_c is None
    assert inputs.spec.charge_guidance_max_c is None
    assert inputs.protected_roots == ("/a", "/b")
    assert inputs.laptop_evidence_root == SECONDARY
    assert inputs.device_config == config.mcp_device
    assert inputs.build_provenance.artefact_kind is ColdArtefactKind.EDITABLE_SOURCE
    assert inputs.build_provenance.artefact_sha256 is None
    assert inputs.build_provenance.source_tree_dirty is False
    assert kwargs["bind_host"] == "127.0.0.1"
    assert kwargs["bind_port"] == 8000
    assert kwargs["spa_dir"] == harness.spa_dir
    assert capsys.readouterr().out == ""


def test_packaged_provenance_and_explicit_hosting_options(harness: Harness, tmp_path: Path) -> None:
    spa = tmp_path / "explicit"
    spa.mkdir()
    (spa / "index.html").write_text("x", encoding="utf-8")
    argv = [
        *base_argv(**{"--artefact-kind": "wheel", "--source-tree": "dirty"}),
        "--artefact-sha256",
        DIGEST,
        "--host",
        "0.0.0.0",
        "--port",
        "8123",
        "--spa-dir",
        str(spa),
    ]
    harness.code = 6
    assert cold_cli.main(argv) == 6
    _config, inputs, kwargs = harness.hosted[0]
    assert inputs.protected_roots == ()
    assert inputs.build_provenance.artefact_kind is ColdArtefactKind.WHEEL
    assert inputs.build_provenance.artefact_sha256 == DIGEST
    assert inputs.build_provenance.source_tree_dirty is True
    assert (kwargs["bind_host"], kwargs["bind_port"], kwargs["spa_dir"]) == ("0.0.0.0", 8123, spa)


@pytest.mark.parametrize(
    "argv",
    [
        base_argv(**{"--artefact-kind": "wheel"}),
        [*base_argv(), "--artefact-sha256", DIGEST],
        base_argv(**{"--profile-name": "   "}),
    ],
    ids=["packaged-without-digest", "editable-with-digest", "blank-profile"],
)
def test_inputs_the_models_refuse_are_input_not_admitted(
    harness: Harness, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    assert cold_cli.main(argv) == 2
    summary = parse_summary(capsys.readouterr().out)
    assert summary["cli_refusal"] == "input_not_admitted"
    assert summary["run_invoked"] == "false"
    assert summary["child_ownership"] == "none"
    assert summary["exit_code"] == "2"
    assert harness.config_loads == 0
    assert harness.hosted == []


def test_a_composition_input_the_model_refuses_is_input_not_admitted(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refusing(**_kwargs: object) -> typing.NoReturn:
        raise pydantic.ValidationError.from_exception_data("ColdCompositionInputs", [])

    monkeypatch.setattr(cold_cli, "ColdCompositionInputs", refusing)
    assert cold_cli.main(base_argv()) == 2
    assert parse_summary(capsys.readouterr().out)["cli_refusal"] == "input_not_admitted"
    assert harness.hosted == []


# --- 3. config -------------------------------------------------------------------------


def _config_errors() -> list[BaseException]:
    validation = pydantic.ValidationError.from_exception_data(
        MARKER, [{"type": "missing", "loc": (MARKER,), "input": MARKER}]
    )
    decode = UnicodeDecodeError("utf-8", MARKER.encode(), 0, 1, MARKER)
    return [ConfigFileError(MARKER), validation, OSError(MARKER), decode]


@pytest.mark.parametrize(
    "error", _config_errors(), ids=["file", "validation", "os", "unicode-decode"]
)
def test_config_failures_print_fixed_text_only(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
) -> None:
    """C18: no exception text; the run is never reached."""

    def failing() -> typing.NoReturn:
        raise error

    monkeypatch.setattr(cold_cli, "load_app_config", failing)
    assert cold_cli.main(base_argv()) == 3
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err
    summary = parse_summary(captured.out)
    assert summary["cli_refusal"] == "config_not_loaded"
    assert summary["result"] == "cli_refused"
    assert harness.hosted == []


def test_secondary_root_is_never_opened_and_env_is_never_forwarded(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C21: the secondary root is recorded only; ``forward_coffee_env`` raises if called."""
    touched: list[str] = []
    real_open = builtins.open
    real_os_open = os.open
    real_stat = os.stat
    real_scandir = os.scandir

    def spy(real: Callable[..., typing.Any]) -> Callable[..., typing.Any]:
        def wrapper(path: typing.Any, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
            if str(path).startswith(SECONDARY):
                touched.append(str(path))
            return real(path, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(builtins, "open", spy(real_open))
    monkeypatch.setattr(os, "open", spy(real_os_open))
    monkeypatch.setattr(os, "stat", spy(real_stat))
    monkeypatch.setattr(os, "scandir", spy(real_scandir))
    assert cold_cli.main(base_argv()) == 0
    assert touched == []
    assert harness.hosted[0][1].laptop_evidence_root == SECONDARY


# --- 4. host facts -----------------------------------------------------------------------

CPUINFO = b"processor\t: 0\nBogoMIPS\t: 108.00\n\nHardware\t: BCM2835\nRevision\t: c04170\n"
MODEL = b"Raspberry Pi 5 Model B Rev 1.0\x00"


def opener_for(files: dict[Path, bytes | OSError]) -> Callable[[Path, int], bytes]:
    def opener(path: Path, limit: int) -> bytes:
        item = files.get(path, FileNotFoundError())
        if isinstance(item, OSError):
            raise item
        return item[:limit]

    return opener


def facts(
    files: dict[Path, bytes | OSError] | None = None,
    *,
    version: Callable[[], str] = lambda: "0.2.2",
    values: tuple[str, str, str, str, str] = ("3.11.9", "Linux-x", "aarch64", "Linux", "6.6"),
) -> ColdHostFacts | None:
    table: dict[Path, bytes | OSError] = {
        cold_cli.PI_MODEL_PATH: MODEL,
        cold_cli.CPUINFO_PATH: CPUINFO,
    }
    table.update(files or {})
    return cold_cli.read_host_facts(
        opener=opener_for(table), mcp_version=version, platform_values=lambda: values
    )


def test_valid_host_facts_are_admitted() -> None:
    result = facts()
    assert result is not None
    assert result.pi_model == "Raspberry Pi 5 Model B Rev 1.0"
    assert result.pi_revision == "c04170"
    assert result.coffee_roaster_mcp_version == "0.2.2"
    assert result.boot_id_path == BOOT_ID_PATH
    assert (result.machine, result.kernel) == ("aarch64", "6.6")


def test_model_at_exactly_the_bound_is_admitted() -> None:
    assert facts({cold_cli.PI_MODEL_PATH: b"M" * 256}) is not None


def _missing_package() -> str:
    raise importlib.metadata.PackageNotFoundError("coffee-roaster-mcp")


@pytest.mark.parametrize(
    ("files", "version", "values"),
    [
        ({cold_cli.PI_MODEL_PATH: FileNotFoundError()}, None, None),
        ({cold_cli.CPUINFO_PATH: PermissionError()}, None, None),
        ({cold_cli.PI_MODEL_PATH: b"M" * 257}, None, None),
        ({cold_cli.PI_MODEL_PATH: "Pi é".encode()}, None, None),
        ({cold_cli.PI_MODEL_PATH: b"\x00\n"}, None, None),
        ({cold_cli.PI_MODEL_PATH: b"Pi\tmodel"}, None, None),
        ({cold_cli.CPUINFO_PATH: b"processor\t: 0\n"}, None, None),
        ({cold_cli.CPUINFO_PATH: CPUINFO + b"Revision\t: c04171\n"}, None, None),
        ({cold_cli.CPUINFO_PATH: b"Revision\t: C04170\n"}, None, None),
        ({cold_cli.CPUINFO_PATH: b"Revision\t: c0\n"}, None, None),
        ({cold_cli.CPUINFO_PATH: b"Revision\t: c04170 extra\n"}, None, None),
        ({cold_cli.CPUINFO_PATH: b"Revision : c04170\n" + b"x" * 65536}, None, None),
        ({cold_cli.CPUINFO_PATH: "Revision : c04170\né".encode()}, None, None),
        ({}, _missing_package, None),
        ({}, None, ("3.11.9", "", "aarch64", "Linux", "6.6")),
    ],
    ids=[
        "model-missing",
        "cpuinfo-unreadable",
        "model-257-bytes",
        "model-non-ascii",
        "model-empty-after-strip",
        "model-not-printable",
        "zero-revision-lines",
        "two-revision-lines",
        "uppercase-revision",
        "short-revision",
        "trailing-text",
        "cpuinfo-over-bound",
        "cpuinfo-non-ascii",
        "package-not-found",
        "empty-platform",
    ],
)
def test_host_fact_failures_refuse(
    files: dict[Path, bytes | OSError],
    version: Callable[[], str] | None,
    values: tuple[str, str, str, str, str] | None,
) -> None:
    """C17: absent, oversized, malformed or ambiguous facts refuse closed."""
    kwargs: dict[str, typing.Any] = {}
    if version is not None:
        kwargs["version"] = version
    if values is not None:
        kwargs["values"] = values
    assert facts(files, **kwargs) is None


def test_production_opener_reads_at_most_the_limit(tmp_path: Path) -> None:
    path = tmp_path / "fact"
    path.write_bytes(b"0123456789")
    assert cold_cli.read_bounded(path, 4) == b"0123"
    assert cold_cli.read_bounded(path, 64) == b"0123456789"


def test_production_platform_and_version_probes_return_text() -> None:
    values = cold_cli._platform_values()
    assert len(values) == 5
    assert all(isinstance(value, str) for value in values)
    assert cold_cli._mcp_version() == importlib.metadata.version("coffee-roaster-mcp")


def test_host_fact_failure_refuses_before_the_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cold_cli, "read_host_facts", lambda: None)
    assert cold_cli.main(base_argv()) == 3
    assert parse_summary(capsys.readouterr().out)["cli_refusal"] == "host_facts_not_read"
    assert harness.hosted == []


def test_host_reader_unavailable_refuses_before_the_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable() -> typing.NoReturn:
        raise ColdHostBoundError(ColdHostBoundFailure.PLATFORM_UNSUPPORTED)

    monkeypatch.setattr(cold_cli, "LinuxHostBoundsReader", unavailable)
    assert cold_cli.main(base_argv()) == 3
    assert parse_summary(capsys.readouterr().out)["cli_refusal"] == "host_reader_unavailable"
    assert harness.hosted == []


@pytest.mark.parametrize("explicit", [False, True])
def test_missing_spa_refuses_before_the_run(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    explicit: bool,
) -> None:
    empty = tmp_path / "no-index"
    empty.mkdir()
    if explicit:
        argv = [*base_argv(), "--spa-dir", str(empty)]
    else:
        monkeypatch.setattr(cold_cli, "default_spa_dir", lambda: None)
        argv = base_argv()
    assert cold_cli.main(argv) == 3
    assert parse_summary(capsys.readouterr().out)["cli_refusal"] == "spa_not_found"
    assert harness.hosted == []


@pytest.mark.parametrize(
    "fault", ["default-permission", "default-value", "probe-permission", "probe-value"]
)
def test_spa_probe_errors_refuse_before_the_run(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str,
) -> None:
    """C26: an unreadable or malformed SPA path refuses with fixed text only."""
    error: Exception = PermissionError(MARKER) if "permission" in fault else ValueError(MARKER)

    def raising(*_args: object, **_kwargs: object) -> typing.NoReturn:
        raise error

    if fault.startswith("default"):
        monkeypatch.setattr(cold_cli, "default_spa_dir", raising)
        argv = base_argv()
    else:
        monkeypatch.setattr(Path, "is_file", raising)
        argv = [*base_argv(), "--spa-dir", f"/srv/{MARKER}"]
    assert cold_cli.main(argv) == 3
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err
    assert parse_summary(captured.out)["cli_refusal"] == "spa_not_found"
    assert harness.hosted == []


# --- interrupts -------------------------------------------------------------------------


def test_interrupt_before_the_runner_maps_to_cancelled_before_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted() -> typing.NoReturn:
        raise KeyboardInterrupt

    monkeypatch.setattr(cold_cli, "load_app_config", interrupted)
    assert cold_cli.main(base_argv()) == 130
    summary = parse_summary(capsys.readouterr().out)
    assert summary["result"] == "cancelled_before_run"
    assert summary["run_invoked"] == "false"
    assert summary["child_ownership"] == "none"
    assert summary["signal"] == "none"
    assert summary["exit_code"] == "130"


def test_interrupt_after_a_report_attempt_writes_nothing_more_and_returns_130(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A late interrupt never appends a second summary or keeps an unproven code."""

    async def reported_then_interrupted(*_args: object, **_kwargs: object) -> int:
        monkeypatch.setattr(cold_runner, "_REPORTED_EXIT", 6)
        raise KeyboardInterrupt

    monkeypatch.setattr(cold_runner, "run_hosted", reported_then_interrupted)
    assert cold_cli.main(base_argv()) == 130
    assert capsys.readouterr().out == ""


def test_interrupt_after_the_engine_was_reached_reports_the_child_unknown(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def invoked_then_interrupted(*_args: object, **_kwargs: object) -> int:
        monkeypatch.setattr(cold_runner, "_RUN_INVOKED", True)
        raise KeyboardInterrupt

    monkeypatch.setattr(cold_runner, "run_hosted", invoked_then_interrupted)
    assert cold_cli.main(base_argv()) == 130
    summary = parse_summary(capsys.readouterr().out)
    assert (summary["result"], summary["run_invoked"]) == ("cancelled", "true")
    assert (summary["child_ownership"], summary["signal"]) == ("unknown", "none")


def test_main_dispatch_returns_the_runner_code(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.code = 7
    monkeypatch.setattr(sys, "argv", ["roastpilot-agent", "cold-characterisation", *base_argv()])
    assert cli.main() == 7
