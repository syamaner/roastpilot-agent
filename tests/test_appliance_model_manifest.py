"""Tests for the bundled/pinned first-crack model identity manifest (AC5).

The manifest is metadata-only (never weights). These tests pin the exact
identity values so a future accidental edit — a wrong digest, a mutable
revision ref, a filename drift from the MCP layout — fails loudly rather than
silently shipping a broken/degraded appliance.
"""

import re

from roastpilot_agent.appliance.model_manifest import (
    MANIFEST_FILES,
    REPO_ID,
    REVISION,
    ManifestFile,
)

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_repo_id_is_pinned() -> None:
    """The repo id matches the Hugging Face model the plan/known-good YAML cite."""
    assert REPO_ID == "syamaner/coffee-first-crack-detection"


def test_revision_is_a_full_commit_sha() -> None:
    """The revision is an exact 40-hex-char commit SHA, never a mutable ref."""
    assert REVISION == "b349a919c34b6130472da97c01817be404e4f629"
    assert _HEX40.fullmatch(REVISION)


def test_manifest_files_are_the_int8_onnx_pair() -> None:
    """Exactly the two int8 files the Pi appliance's ``pi_inference`` profile needs."""
    assert (
        ManifestFile(
            relative_path="onnx/int8/model_quantized.onnx",
            sha256="022092cddd4c2cd740670c0a85786460699bc1b4f03e20f508182768d21545df",
        ),
        ManifestFile(
            relative_path="onnx/int8/preprocessor_config.json",
            sha256="8d04ba5a9c6fca5d39d0de2b1fd05ecf79deb589fbba279728bbebac39934231",
        ),
    ) == MANIFEST_FILES


def test_every_manifest_digest_is_a_full_sha256_hex_string() -> None:
    """Each digest is a complete 64-hex-char SHA-256, not a prefix or upper-case."""
    for manifest_file in MANIFEST_FILES:
        assert _HEX64.fullmatch(manifest_file.sha256), manifest_file.relative_path


def test_manifest_file_is_frozen() -> None:
    """A ``ManifestFile`` cannot be mutated after construction."""
    manifest_file = MANIFEST_FILES[0]
    try:
        manifest_file.sha256 = "0" * 64  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover - defensive, would indicate a non-frozen dataclass
        raise AssertionError("ManifestFile must be frozen")
