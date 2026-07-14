"""Hatchling build hook: bundle the built SPA (``web/dist``) into the wheel.

E11-S1 (issue #137). Resolves the open packaging item recorded in
``roastpilot-plan/roastpilot-agent/plan.md`` §11 item 3 ("Hatchling build-hook
details for ``web/dist`` — resolve at E11; fallback: commit built dist for the
first release"): the build-hook approach below is what shipped, so the plan's
fallback (committing ``web/dist``) was not needed.

Only the **standard** (non-editable) wheel build runs ``npm run build`` and
force-includes the SPA. Editable installs (``pip install -e .``, the normal
developer loop) skip this entirely: :func:`roastpilot_agent.live.default_spa_dir`
already falls back to the source-checkout ``web/dist`` for that case, and
running an npm build on every dev install would be slow and would require Node
as a hard dev-environment dependency.

The built assets land inside the package at ``roastpilot_agent/_web_dist`` so
:mod:`importlib.resources` can find them the same way whether the wheel is
installed as a directory or a zipped egg.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

#: Package-relative destination for the bundled SPA (mirrors the source
#: import path ``roastpilot_agent._web_dist`` used by ``live.default_spa_dir``).
_PACKAGE_DEST = "src/roastpilot_agent/_web_dist"


class SpaBuildHook(BuildHookInterface):  # type: ignore[type-arg]
    """Builds ``web/dist`` and force-includes it into the standard wheel."""

    PLUGIN_NAME = "spa-dist"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Build the SPA and register it for inclusion in a standard wheel.

        Args:
            version: ``"standard"`` for a real wheel build, ``"editable"`` for
                ``pip install -e .``. Only ``"standard"`` triggers the npm
                build; editable installs rely on the source-checkout fallback
                in ``live.default_spa_dir``.
            build_data: The hatchling build-data mapping this hook may mutate
                to add ``force_include`` entries.
        """
        if version != "standard":
            return

        web_dir = Path(self.root) / "web"
        dist_dir = web_dir / "dist"

        if not (dist_dir / "index.html").is_file():
            self._build_spa(web_dir)

        if not (dist_dir / "index.html").is_file():
            # No Node toolchain available and no pre-built dist checked in —
            # fail closed rather than silently shipping an API-only wheel
            # under a name that promises the bundled SPA (D1: the SPA ships
            # inside the wheel).
            msg = (
                f"web/dist/index.html not found and the SPA build did not "
                f"produce it (checked {dist_dir}). Install Node (npm) and "
                f"retry, or build web/dist manually before packaging."
            )
            raise RuntimeError(msg)

        build_data.setdefault("force_include", {})
        build_data["force_include"][str(dist_dir)] = _PACKAGE_DEST
        build_data["artifacts"] = [*build_data.get("artifacts", []), f"/{_PACKAGE_DEST}/**"]

    @staticmethod
    def _build_spa(web_dir: Path) -> None:
        """Run ``npm ci && npm run build`` in ``web/``, tolerating a missing npm.

        Args:
            web_dir: The ``web/`` directory containing the Vite SPA project.
        """
        npm = shutil.which("npm")
        if npm is None:
            return
        subprocess.run([npm, "ci"], cwd=web_dir, check=True)  # noqa: S603
        subprocess.run([npm, "run", "build"], cwd=web_dir, check=True)  # noqa: S603
