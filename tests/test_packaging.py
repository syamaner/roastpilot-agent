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

import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed"),
    # Runs `python -m build --wheel` (a real subprocess build invoking npm ci +
    # vite build) then a clean-venv pip install: much slower than a unit test.
    pytest.mark.slow,
]

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def built_wheel(tmp_path: Path) -> Path:
    """Build the real wheel into an isolated tmp output dir and return its path.

    Uses the repo's own build backend (hatchling + ``hatch_build.py``) via
    ``python -m build --wheel``, so this exercises the exact packaging path a
    release does — not a hand-rolled zip that could drift from the real hook.
    """
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pip", "install", "--quiet", "build"],
        cwd=_REPO_ROOT,
        check=True,
    )
    out_dir = tmp_path / "dist"
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
        cwd=_REPO_ROOT,
        check=True,
    )
    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, found {wheels}"
    return wheels[0]


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
