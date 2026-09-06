"""Model identity manifest for the bundled/pinned first-crack model (AC5).

This module is the **single source of truth** for the exact model bytes the
Pi appliance places locally so a roast never waits on a live Hugging Face
pull (`roastpilot-agent/plan.md:30`; `torch-free-pi-appliance.md:107-108`).
The identities pinned here already match the committed known-good example
(`docs/examples/coffee-roaster-mcp.known-good.yaml:34-37`).

Everything in this module is **metadata only** — a revision string and
SHA-256 digests, never weights. No binary model artifact is committed to
this repository (AGENTS.md: "Do not commit model weights").

The relative file paths below are also exactly the paths
``coffee-roaster-mcp``'s ``first_crack.local_model_dir`` resolution expects
(`coffee_roaster_mcp.artifacts.INT8_ONNX_MODEL_FILENAME` /
``INT8_FEATURE_EXTRACTOR_FILENAME``: it joins ``local_model_dir`` with the
artifact's repository-relative POSIX path unchanged). This repo's dev
dependency group is deliberately pinned to ``coffee-roaster-mcp==0.1.13``,
never the ``[pi]`` extra's ``0.2.0`` (`pyproject.toml:136-139`), so this was
confirmed both against the installed ``0.1.13`` package and by a byte-for-byte
comparison of ``0.2.0``'s downloaded wheel `artifacts.py` (identical to
``0.1.13``'s — see `model_install.py`'s module docstring). Placement under
these same relative paths is therefore sufficient to satisfy the MCP server's
local-directory layout — no MCP-side change is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Hugging Face repository that supplies the pinned first-crack model.
REPO_ID: Final[str] = "syamaner/coffee-first-crack-detection"

#: Exact pinned revision (a full 40-character lowercase hex commit SHA) —
#: never a mutable ref such as a branch or tag, so the bytes an appliance
#: installs cannot silently drift out from under a pinned digest.
REVISION: Final[str] = "b349a919c34b6130472da97c01817be404e4f629"


@dataclass(frozen=True, slots=True)
class ManifestFile:
    """One pinned model file: its repository-relative path and SHA-256 digest.

    Attributes:
        relative_path: POSIX, repository-relative path (also the
            ``local_model_dir``-relative placement path).
        sha256: Lowercase hex SHA-256 digest of the exact file bytes.
    """

    relative_path: str
    sha256: str


#: The complete, closed set of files the appliance must place. Order is
#: stable and matches the known-good example's citations.
MANIFEST_FILES: Final[tuple[ManifestFile, ...]] = (
    ManifestFile(
        relative_path="onnx/int8/model_quantized.onnx",
        sha256="022092cddd4c2cd740670c0a85786460699bc1b4f03e20f508182768d21545df",
    ),
    ManifestFile(
        relative_path="onnx/int8/preprocessor_config.json",
        sha256="8d04ba5a9c6fca5d39d0de2b1fd05ecf79deb589fbba279728bbebac39934231",
    ),
)
