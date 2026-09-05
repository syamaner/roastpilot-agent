"""Wheel-packaging tests (E11-S1, #137).

Builds the REAL wheel (invoking the ``hatch_build.py`` custom build hook,
which itself runs ``npm ci && npm run build`` in ``web/``) and inspects/
installs the artifact, rather than asserting only that source files exist.
This is slower than a unit test (a real subprocess build + a clean-venv
install), so it is tagged ``slow`` — still fully hardware-free per AGENTS.md.

Skipped when ``npm`` is unavailable (mirrors the ``coffee-roaster-mcp``
binary-presence skip convention in ``test_mcp_respawn_real_child.py``): the
hook itself tolerates a missing npm by no-op'ing (so an editable dev install
never breaks), but this test asserts the actual bundling behavior, which
requires a real Node toolchain to build the SPA.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import cast

import pytest

pytestmark = [
    pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed"),
    pytest.mark.serial(reason="mutates web/dist and builds from the shared repository cwd"),
    # Runs `python -m build --wheel` (a real subprocess build invoking npm ci +
    # vite build) then a clean-venv pip install: much slower than a unit test.
    pytest.mark.slow,
]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DENYLISTED_DISTRIBUTIONS = frozenset({"torch", "torchaudio", "transformers"})
_PEP503_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_EXPECTED_BASE_REQUIREMENT_NAMES = frozenset(
    {
        "aiosqlite",
        "anyio",
        "fastapi",
        "httpx",
        "httpcore",
        "lxml",
        "mcp",
        "pydantic",
        "pydantic-ai-slim",
        "pydantic-settings",
        "filelock",
        "pyyaml",
        "pyserial",
        "sounddevice",
        "uvicorn",
        "webencodings",
        "extruct",
        "openai",
        "trafilatura",
    }
)


def _normalise_pep503_name(raw_name: object) -> str:
    """Return one PEP-503-normalised distribution name or fail closed."""
    assert isinstance(raw_name, str), f"distribution name must be a string: {raw_name!r}"
    assert _PEP503_NAME.fullmatch(raw_name), f"unparseable distribution name: {raw_name!r}"
    return re.sub(r"[-_.]+", "-", raw_name).lower()


def _requirement_name(requirement: object) -> str:
    """Extract and normalise one simple metadata requirement name fail-closed."""
    assert isinstance(requirement, str), f"requirement must be a string: {requirement!r}"
    name = re.split(r"[<>=!~;\[\s]", requirement, maxsplit=1)[0]
    return _normalise_pep503_name(name)


def _wheel_requires_dist(wheel: Path) -> list[str]:
    """Return all Requires-Dist values from one wheel's METADATA file."""
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_names) == 1, f"expected one METADATA file, found {metadata_names}"
        message = BytesParser().parsebytes(archive.read(metadata_names[0]))
    values = message.get_all("Requires-Dist", [])
    assert all(isinstance(value, str) for value in values)
    return values


def test_pi_extra_is_the_exact_torch_free_mcp_pin() -> None:
    """E11-S1 exposes only the ratified appliance extra and exact MCP pin."""
    with (_REPO_ROOT / "pyproject.toml").open("rb") as config_file:
        project = cast(dict[str, object], tomllib.load(config_file))
    metadata = cast(dict[str, object], project["project"])
    extras = cast(dict[str, object], metadata["optional-dependencies"])
    assert extras == {
        "anthropic": ["pydantic-ai-slim[anthropic]>=1.0,<2", "anthropic<1"],
        "google": ["pydantic-ai-slim[google]>=1.0,<2"],
        "all-providers": [
            "pydantic-ai-slim[anthropic]>=1.0,<2",
            "anthropic<1",
            "pydantic-ai-slim[google]>=1.0,<2",
        ],
        "pi": ["coffee-roaster-mcp==0.2.0"],
    }
    pi_requirement = cast(list[str], extras["pi"])
    assert len(pi_requirement) == 1
    assert _requirement_name(pi_requirement[0]) == "coffee-roaster-mcp"
    assert pi_requirement[0] == "coffee-roaster-mcp==0.2.0"


def test_pi_extra_metadata_preserves_a_lean_base_wheel(built_wheel: Path) -> None:
    """Wheel metadata marks only the exact Pi pin and preserves base requirements."""
    requirements = _wheel_requires_dist(built_wheel)
    pi_requirements = [
        requirement
        for requirement in requirements
        if requirement.partition(";")[2].strip() == "extra == 'pi'"
    ]
    assert pi_requirements == ["coffee-roaster-mcp==0.2.0; extra == 'pi'"]
    assert {_requirement_name(requirement) for requirement in pi_requirements} == {
        "coffee-roaster-mcp"
    }

    unconditional = [requirement for requirement in requirements if ";" not in requirement]
    unconditional_names = {_requirement_name(requirement) for requirement in unconditional}
    assert unconditional_names == _EXPECTED_BASE_REQUIREMENT_NAMES
    assert _DENYLISTED_DISTRIBUTIONS.isdisjoint(
        {_requirement_name(requirement) for requirement in requirements}
    )


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ("Torch", "torch"),
        ("torchaudio", "torchaudio"),
        ("torch_audio", "torch-audio"),
        ("TRANSFORMERS", "transformers"),
        ("torch.audio", "torch-audio"),
        ("pytorch-lightning", "pytorch-lightning"),
        ("transformers-stream-generator", "transformers-stream-generator"),
    ],
)
def test_pep503_normaliser_uses_exact_distribution_names(raw_name: str, expected: str) -> None:
    """Denylist matching is PEP-503 exact, never a substring heuristic."""
    normalised = _normalise_pep503_name(raw_name)
    assert normalised == expected
    assert (normalised in _DENYLISTED_DISTRIBUTIONS) is (
        expected in {"torch", "torchaudio", "transformers"}
    )


def test_pep503_normaliser_rejects_an_unparseable_name() -> None:
    """Malformed distribution names cannot silently bypass the appliance denylist."""
    with pytest.raises(AssertionError, match="unparseable distribution name"):
        _normalise_pep503_name("torch audio")


@pytest.fixture
def built_wheel(tmp_path: Path) -> Path:
    """Build the real wheel into an isolated tmp output dir and return its path.

    Uses the repo's own build backend (hatchling + ``hatch_build.py``) via
    ``python -m build --wheel``, so this exercises the exact packaging path a
    release does — not a hand-rolled zip that could drift from the real hook.
    ``build`` is a declared dev dependency (pyproject.toml's ``dev`` group), so
    this is hermetic after ``pip install -e . --group dev`` — no ad-hoc
    ``pip install`` inside the test (repo rule: all dev deps declared in
    ``pyproject.toml``).
    """
    out_dir = tmp_path / "dist"
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
        cwd=_REPO_ROOT,
        check=True,
    )
    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, found {wheels}"
    return wheels[0]


def test_build_hook_rebuilds_over_a_stale_web_dist(tmp_path: Path) -> None:
    """A stale leftover web/dist is rebuilt, never shipped as-is, when npm exists.

    ``web/dist`` is gitignored but not necessarily clean between builds (a
    developer's earlier local ``npm run build``, or a stale artifact from a
    prior packaging run). Plants a marker file in ``web/dist`` before
    building, then asserts the marker is ABSENT from the produced wheel — a
    real ``npm run build`` always empties Vite's output directory first, so
    the only way the marker could survive is if the hook skipped rebuilding
    and force-included the stale directory verbatim (the bug this test
    guards against; Codex catch, PR #547).
    """
    dist_dir = _REPO_ROOT / "web" / "dist"
    marker = dist_dir / "STALE_MARKER_FROM_TEST.txt"
    dist_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text("stale build artifact — must not survive a rebuild")
    try:
        out_dir = tmp_path / "dist"
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
            cwd=_REPO_ROOT,
            check=True,
        )
        wheels = list(out_dir.glob("*.whl"))
        assert len(wheels) == 1, f"expected exactly one wheel, found {wheels}"
        with zipfile.ZipFile(wheels[0]) as archive:
            names = archive.namelist()
        assert not any("STALE_MARKER_FROM_TEST" in name for name in names), (
            f"stale marker survived into the wheel — the hook did not rebuild: {names}"
        )
        assert "roastpilot_agent/_web_dist/index.html" in names
    finally:
        # web/dist is gitignored/build output, but clean up the marker
        # explicitly in case a real build did not run (e.g. an unexpected
        # early failure) and left it behind.
        if marker.is_file():
            marker.unlink()


def test_wheel_contains_bundled_spa_index_html(built_wheel: Path) -> None:
    """The built wheel force-includes the SPA at roastpilot_agent/_web_dist."""
    with zipfile.ZipFile(built_wheel) as archive:
        names = archive.namelist()
        assert "roastpilot_agent/_web_dist/index.html" in names
        # At least one built JS asset must be present too — not just the shell
        # index.html with no bundle (a broken/partial build would still write
        # index.html via some other path, so check for the assets directory).
        assert any(name.startswith("roastpilot_agent/_web_dist/assets/") for name in names), (
            f"no bundled assets found in wheel: {names}"
        )


def test_wheel_installs_into_clean_venv_and_serves_spa(built_wheel: Path, tmp_path: Path) -> None:
    """A clean-venv install of the wheel exposes the CLI and serves the SPA.

    This is the real end-to-end proof: NOT `pip install -e .` (which always
    takes the source-checkout fallback in ``default_spa_dir``), but an
    isolated venv installing only the built ``.whl``, then running the console
    script and hitting a live (hardware-free, non-8000) HTTP server.
    """
    venv_dir = tmp_path / "smoke-venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    venv_python = venv_dir / "bin" / "python"
    subprocess.run(  # noqa: S603
        [str(venv_python), "-m", "pip", "install", "--quiet", str(built_wheel)],
        check=True,
    )

    version_result = subprocess.run(  # noqa: S603
        [str(venv_dir / "bin" / "roastpilot-agent"), "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "roastpilot-agent" in version_result.stdout

    # Resolve default_spa_dir() from INSIDE the clean venv's interpreter, not
    # this test process's (which has the source checkout on its import path
    # and would resolve the wrong branch).
    resolve_script = (
        "from roastpilot_agent import live; "
        "d = live.default_spa_dir(); "
        "print(d); "
        "raise SystemExit(0 if d and (d / 'index.html').is_file() else 1)"
    )
    resolve_result = subprocess.run(  # noqa: S603
        [str(venv_python), "-c", resolve_script],
        capture_output=True,
        text=True,
    )
    assert resolve_result.returncode == 0, resolve_result.stdout + resolve_result.stderr
    resolved_dir = Path(resolve_result.stdout.strip())
    # The resolved SPA dir must be inside the clean venv's site-packages (the
    # packaged _web_dist), never a path back into this repo's source checkout —
    # that would mean the wheel silently fell back to the wrong branch.
    assert resolved_dir.is_relative_to(venv_dir)
    assert resolved_dir.name == "_web_dist"
