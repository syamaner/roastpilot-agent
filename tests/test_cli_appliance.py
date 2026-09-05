"""Tests for the ``roastpilot-agent appliance model install`` CLI wiring (#138).

Placement/verification behavior itself (digests, redirects, byte caps, etc.)
is covered end to end in ``tests/test_appliance_model_install.py``; these
tests cover only argument parsing, destination resolution, dispatch from
``main()``, and output formatting — using a fake ``install_model`` so no real
filesystem placement or network call happens here.
"""

import json
from pathlib import Path

import pytest

from roastpilot_agent import cli
from roastpilot_agent.appliance import model_install as model_install_module
from roastpilot_agent.appliance.model_install import ModelInstallError, ModelInstallSummary


def _summary(*, network_used: bool = False, dest: Path | None = None) -> ModelInstallSummary:
    return ModelInstallSummary(
        dest=dest if dest is not None else Path("/var/lib/roastpilot/models"),
        files=(
            model_install_module.ManifestFileResult(
                "onnx/int8/model_quantized.onnx", "0" * 64, "cached"
            ),
            model_install_module.ManifestFileResult(
                "onnx/int8/preprocessor_config.json",
                "1" * 64,
                "fetched" if network_used else "cached",
            ),
        ),
        network_used=network_used,
    )


# --- parser -------------------------------------------------------------


def test_appliance_model_install_parser_defaults() -> None:
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["model", "install"]
    )
    assert args.appliance_command == "model"
    assert args.model_command == "install"
    assert args.dest is None
    assert args.from_dir is None
    assert args.verify_only is False
    assert args.json_output is False


def test_appliance_model_install_parser_all_flags(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    src = tmp_path / "src"
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "model",
            "install",
            "--dest",
            str(dest),
            "--from-dir",
            str(src),
            "--verify-only",
            "--json",
        ]
    )
    assert args.dest == dest
    assert args.from_dir == src
    assert args.verify_only is True
    assert args.json_output is True


def test_appliance_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._build_appliance_parser().parse_args([])  # pyright: ignore[reportPrivateUsage]
    assert exc_info.value.code == 2


def test_appliance_model_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli._build_appliance_parser().parse_args(["model"])  # pyright: ignore[reportPrivateUsage]
    assert exc_info.value.code == 2


def test_top_level_help_advertises_appliance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The normal help surface advertises the specialised appliance tree."""
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "{serve,appliance}" in out
    assert "'appliance' manages native Pi appliance support" in " ".join(out.split())


# --- destination resolution ----------------------------------------------


def test_resolve_appliance_model_dest_prefers_explicit(tmp_path: Path) -> None:
    import argparse

    explicit = tmp_path / "explicit-models"
    args = argparse.Namespace(dest=explicit)
    resolved = cli._resolve_appliance_model_dest(args)  # pyright: ignore[reportPrivateUsage]
    assert resolved == explicit


def test_resolve_appliance_model_dest_uses_xdg_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    args = argparse.Namespace(dest=None)
    resolved = cli._resolve_appliance_model_dest(args)  # pyright: ignore[reportPrivateUsage]
    assert resolved == tmp_path / "xdg" / "roastpilot" / "models"


def test_resolve_appliance_model_dest_falls_back_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    args = argparse.Namespace(dest=None)
    resolved = cli._resolve_appliance_model_dest(args)  # pyright: ignore[reportPrivateUsage]
    assert resolved == tmp_path / "home" / ".local" / "share" / "roastpilot" / "models"


# --- _run_appliance_model_install ----------------------------------------


def test_run_appliance_model_install_success_plain_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "dest"
    summary = _summary(dest=dest)

    def fake_install_model(
        given_dest: Path, *, from_dir: Path | None = None, verify_only: bool = False
    ) -> ModelInstallSummary:
        assert given_dest == dest
        assert from_dir is None
        assert verify_only is False
        return summary

    monkeypatch.setattr(model_install_module, "install_model", fake_install_model)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["model", "install", "--dest", str(dest)]
    )
    exit_code = cli._run_appliance_model_install(args)  # pyright: ignore[reportPrivateUsage]

    assert exit_code == 0
    out = capsys.readouterr().out
    assert str(dest) in out
    assert "onnx/int8/model_quantized.onnx" in out
    assert "no network fetch was required" in out


def test_run_appliance_model_install_success_plain_text_when_network_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When at least one file was actually fetched over the network, the
    plain-text summary reports the ``fetched`` source per file and never
    claims that no network fetch was required."""
    dest = tmp_path / "dest"
    summary = _summary(dest=dest, network_used=True)

    def fake_install_model(
        given_dest: Path, *, from_dir: Path | None = None, verify_only: bool = False
    ) -> ModelInstallSummary:
        assert given_dest == dest
        assert from_dir is None
        assert verify_only is False
        return summary

    monkeypatch.setattr(model_install_module, "install_model", fake_install_model)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["model", "install", "--dest", str(dest)]
    )
    exit_code = cli._run_appliance_model_install(args)  # pyright: ignore[reportPrivateUsage]

    assert exit_code == 0
    out = capsys.readouterr().out
    assert str(dest) in out
    assert "fetched" in out
    assert "no network fetch was required" not in out


def test_run_appliance_model_install_success_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "dest"
    summary = _summary(dest=dest, network_used=True)

    def fake_install_model(
        given_dest: Path, *, from_dir: Path | None = None, verify_only: bool = False
    ) -> ModelInstallSummary:
        return summary

    monkeypatch.setattr(model_install_module, "install_model", fake_install_model)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["model", "install", "--dest", str(dest), "--json"]
    )
    exit_code = cli._run_appliance_model_install(args)  # pyright: ignore[reportPrivateUsage]

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dest"] == str(dest)
    assert payload["network_used"] is True
    assert payload["files"][0]["relative_path"] == "onnx/int8/model_quantized.onnx"


def test_run_appliance_model_install_expands_from_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--from-dir`` is expanduser()'d before being passed to install_model."""
    dest = tmp_path / "dest"
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    seen: dict[str, object] = {}

    def fake_install_model(
        given_dest: Path, *, from_dir: Path | None = None, verify_only: bool = False
    ) -> ModelInstallSummary:
        seen["from_dir"] = from_dir
        return _summary(dest=given_dest)

    monkeypatch.setattr(model_install_module, "install_model", fake_install_model)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["model", "install", "--dest", str(dest), "--from-dir", "~/models-src"]
    )
    cli._run_appliance_model_install(args)  # pyright: ignore[reportPrivateUsage]

    assert seen["from_dir"] == home / "models-src"


def test_run_appliance_model_install_failure_prints_message_and_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_install_model(
        given_dest: Path, *, from_dir: Path | None = None, verify_only: bool = False
    ) -> ModelInstallSummary:
        raise ModelInstallError("digest mismatch for 'onnx/int8/model_quantized.onnx'")

    monkeypatch.setattr(model_install_module, "install_model", fake_install_model)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["model", "install", "--dest", str(tmp_path / "dest")]
    )
    exit_code = cli._run_appliance_model_install(args)  # pyright: ignore[reportPrivateUsage]

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "model install failed" in out
    assert "digest mismatch" in out
    assert "model install warning" not in out


def test_run_appliance_model_install_prints_sanitised_cleanup_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Recognised cleanup notes are actionable without exposing arbitrary text."""
    failure = ModelInstallError("digest mismatch")
    failure.add_note("cleanup failed while removing temporary model file: OSError")
    failure.add_note("https://example.invalid/?token=secret")

    def fake_install_model(
        given_dest: Path, *, from_dir: Path | None = None, verify_only: bool = False
    ) -> ModelInstallSummary:
        raise failure

    monkeypatch.setattr(model_install_module, "install_model", fake_install_model)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["model", "install", "--dest", str(tmp_path / "dest")]
    )

    exit_code = cli._run_appliance_model_install(args)  # pyright: ignore[reportPrivateUsage]

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "model install failed: digest mismatch" in out
    assert (
        "model install warning: cleanup failed while removing temporary model file: OSError" in out
    )
    assert "example.invalid" not in out
    assert "secret" not in out


def test_run_appliance_model_install_oserror_prints_friendly_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A filesystem-level failure (e.g. a permission error creating the
    destination) is reported plainly, matching the repo's other CLI
    subcommands (``cli.py:910``/``1094``), not an uncaught traceback."""

    def fake_install_model(
        given_dest: Path, *, from_dir: Path | None = None, verify_only: bool = False
    ) -> ModelInstallSummary:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(model_install_module, "install_model", fake_install_model)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["model", "install", "--dest", str(tmp_path / "dest")]
    )
    exit_code = cli._run_appliance_model_install(args)  # pyright: ignore[reportPrivateUsage]

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "model install failed" in out
    assert "Permission denied" in out


@pytest.mark.parametrize("option", ["--dest", "--from-dir"])
def test_run_appliance_model_install_unknown_user_path_is_friendly_failure(
    option: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unknown-user ``~name`` expansion remains inside the CLI error boundary."""
    unknown_user_path = Path("~no_such_roastpilot_user/models")
    original_expanduser = type(unknown_user_path).expanduser

    def raising_expanduser(path: Path) -> Path:
        if path == unknown_user_path:
            raise RuntimeError("unknown user")
        return original_expanduser(path)

    monkeypatch.setattr(type(unknown_user_path), "expanduser", raising_expanduser)
    args_values = ["model", "install", "--dest", "/tmp/dest"]
    if option == "--dest":
        args_values = ["model", "install", "--dest", str(unknown_user_path)]
    else:
        args_values.extend(["--from-dir", str(unknown_user_path)])
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        args_values
    )

    exit_code = cli._run_appliance_model_install(args)  # pyright: ignore[reportPrivateUsage]

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "model install failed" in out
    assert "unknown user" in out
    assert "Traceback" not in out


def test_run_appliance_model_install_passes_verify_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "dest"
    seen: dict[str, object] = {}

    def fake_install_model(
        given_dest: Path, *, from_dir: Path | None = None, verify_only: bool = False
    ) -> ModelInstallSummary:
        seen["verify_only"] = verify_only
        return _summary(dest=given_dest)

    monkeypatch.setattr(model_install_module, "install_model", fake_install_model)
    args = cli._build_appliance_parser().parse_args(  # pyright: ignore[reportPrivateUsage]
        ["model", "install", "--dest", str(dest), "--verify-only"]
    )
    cli._run_appliance_model_install(args)  # pyright: ignore[reportPrivateUsage]

    assert seen["verify_only"] is True


# --- dispatch from main() -------------------------------------------------


def test_main_dispatches_appliance_model_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "dest"
    summary = _summary(dest=dest)

    def fake_install_model(
        given_dest: Path, *, from_dir: Path | None = None, verify_only: bool = False
    ) -> ModelInstallSummary:
        return summary

    monkeypatch.setattr(model_install_module, "install_model", fake_install_model)
    monkeypatch.setattr(
        "sys.argv",
        ["roastpilot-agent", "appliance", "model", "install", "--dest", str(dest), "--json"],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dest"] == str(dest)


def test_main_appliance_missing_subcommand_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.argv", ["roastpilot-agent", "appliance"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 2
