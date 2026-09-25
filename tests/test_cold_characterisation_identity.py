"""Behavioural coverage for fail-closed cold identity freezing."""

from __future__ import annotations

import ast
import inspect
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Final, cast, get_args, get_origin, get_type_hints

import pytest
from pydantic import ValidationError

from roastpilot_agent.advisor import AdvisorDescriptor
from roastpilot_agent.appliance.model_manifest import MANIFEST_FILES, REPO_ID, REVISION
from roastpilot_agent.cold_characterisation import identity as cold_identity
from roastpilot_agent.cold_characterisation.identity import (
    REQUIRED_MCP_VERSION,
    AgentBuildProvenance,
    ColdArtefactKind,
    ColdIdentityError,
    ColdIdentityFailure,
    ColdRunIdentity,
    EffectiveMCPProfile,
    freeze_identity,
    identity_sha256,
)
from roastpilot_agent.config import MCPDeviceConfig
from roastpilot_agent.mcp_client import RuntimeConfigSnapshot, ServerInfo

_MCP_DEVICE_CONFIG_FIELD_NAMES: Final[frozenset[str]] = frozenset(MCPDeviceConfig.model_fields)
_HEX_40: Final = "b349a919c34b6130472da97c01817be404e4f629"
_HEX_64: Final = "a" * 64


def _build_provenance(**changes: object) -> AgentBuildProvenance:
    """Build a valid caller-supplied provenance assertion."""
    values: dict[str, object] = {
        "source_revision": _HEX_40,
        "source_tree_dirty": False,
        "artefact_kind": ColdArtefactKind.WHEEL,
        "artefact_sha256": _HEX_64,
    }
    values.update(changes)
    return AgentBuildProvenance.model_validate(values)


def _effective_mcp_profile(**changes: object) -> EffectiveMCPProfile:
    """Build a valid caller-supplied effective MCP profile commitment."""
    values: dict[str, object] = {
        "source_sha256": _HEX_64,
        "source_byte_length": 100,
        "first_crack_onnx_threads": 2,
        "first_crack_min_positive_windows": 3,
        "first_crack_confirmation_window_seconds": 30.0,
        "first_crack_revision": _HEX_40,
        "audio_sample_rate": 16000,
        "audio_window_seconds": 10.0,
        "audio_overlap": 0.3,
        "audio_hop_seconds": None,
        "session_ror_window_seconds": 60,
        "session_ror_min_sample_seconds": 10,
    }
    values.update(changes)
    return EffectiveMCPProfile.model_validate(values)


def _runtime(**changes: object) -> RuntimeConfigSnapshot:
    """Build the already-fetched runtime identity mirror used by freezing."""
    values: dict[str, object] = {
        "config_source": None,
        "roaster_driver": "hottop_kn8828b_2k_plus",
        "roaster_port": "/dev/ttyUSB0",
        "roaster_baudrate": 115200,
        "temperature_unit": "celsius",
        "command_interval_seconds": 0.3,
        "first_crack_mode": "audio",
        "model_repo_id": REPO_ID,
        "model_precision": "int8",
        "allow_manual_override": False,
        "log_dir": "logs",
        "sample_interval_seconds": 5.0,
        "auto_t0_detection_enabled": False,
        "auto_t0_drop_threshold_c": 25.0,
    }
    values.update(changes)
    return RuntimeConfigSnapshot.model_validate(values)


def _server() -> ServerInfo:
    """Build the already-fetched server identity mirror used by freezing."""
    return ServerInfo(
        product_name="Coffee Roaster MCP",
        package_name="coffee-roaster-mcp",
        version="0.2.1",
        transport="stdio",
        current_phase="bootstrap",
        roaster_driver="hottop_kn8828b_2k_plus",
        first_crack_mode="audio",
        bootstrap_safe=True,
        available_bootstrap_tools=(),
        started_at_utc="2026-09-22T00:00:00Z",
    )


def _freeze(tmp_path: Path, **changes: object) -> ColdRunIdentity:
    """Freeze a valid identity using only a test-local boot identifier source."""
    write_boot_id = cast(bool, changes.pop("_write_boot_id", True))
    boot_id_path = tmp_path / "boot_id"
    if write_boot_id and "boot_id_path" not in changes:
        boot_id_path.write_text("123e4567-e89b-12d3-a456-426614174000\n", encoding="ascii")
    arguments: dict[str, object] = {
        "run_id": "cold-1",
        "started_at_utc": "2026-09-22T00:00:00Z",
        "coffee_roaster_mcp_version": REQUIRED_MCP_VERSION,
        "python_version": "3.11.9",
        "platform": "linux",
        "machine": "aarch64",
        "operating_system": "Linux",
        "kernel": "6.6.0",
        "pi_model": "Raspberry Pi 5",
        "pi_revision": "d04170",
        "runtime_config": _runtime(),
        "server_info": _server(),
        "device_config": MCPDeviceConfig(recording_devices=("USB microphone",)),
        "build_provenance": _build_provenance(),
        "effective_mcp_profile": _effective_mcp_profile(),
        "audio_device_identity": "USB microphone",
        "serial_port_path": "/dev/ttyUSB0",
        "controller_tick_seconds": 1.0,
        "pi_evidence_root": "/var/lib/roastpilot-agent/evidence",
        "laptop_evidence_root": "/Volumes/evidence",
        "advisor_descriptor": AdvisorDescriptor(
            provider="openrouter", model="test/model", prompt_version="v1"
        ),
        "credential_env_var_name": "OPENROUTER_API_KEY",
        "credential_present": True,
        "stimulus_block": "Tap the empty drum once.",
        "operator_host_notes": "Active cooler installed.",
        "operator_psu_notes": "Official PSU connected.",
        "operator_cooling_notes": "Cooler audible.",
        "boot_id_path": boot_id_path,
    }
    arguments.update(changes)
    return freeze_identity(**arguments)  # type: ignore[arg-type]


def _assert_failure(tmp_path: Path, failure: ColdIdentityFailure, **changes: object) -> None:
    """Assert that a freezing input rejects with its closed failure reason."""
    with pytest.raises(ColdIdentityError) as raised:
        _freeze(tmp_path, **changes)
    assert raised.value.failure is failure


def test_freeze_requires_and_carries_provenance_and_effective_profile(tmp_path: Path) -> None:
    """Freezing cannot omit either new immutable identity component."""
    identity = _freeze(tmp_path)

    assert identity.build_provenance == _build_provenance()
    assert identity.effective_mcp_profile == _effective_mcp_profile()
    assert ColdRunIdentity.model_fields["build_provenance"].is_required()
    assert ColdRunIdentity.model_fields["effective_mcp_profile"].is_required()
    signature = inspect.signature(freeze_identity)
    for field in ("build_provenance", "effective_mcp_profile"):
        parameter = signature.parameters[field]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    payload = identity.model_dump(mode="python")
    for missing in ("build_provenance", "effective_mcp_profile"):
        incomplete = dict(payload)
        del incomplete[missing]
        with pytest.raises(ValidationError):
            ColdRunIdentity.model_validate(incomplete)
    for field in ("build_provenance", "effective_mcp_profile"):
        with pytest.raises(ValidationError):
            _freeze(tmp_path, **{field: None})


def test_new_models_round_trip_through_json_identity_and_are_frozen(tmp_path: Path) -> None:
    """Closed enum JSON values reconstruct while all new state remains immutable."""
    identity = _freeze(tmp_path)
    reconstructed = ColdRunIdentity.model_validate(identity.model_dump(mode="json"))

    assert reconstructed == identity
    assert identity_sha256(reconstructed) == identity_sha256(identity)
    assert reconstructed.build_provenance.artefact_kind is ColdArtefactKind.WHEEL
    with pytest.raises(ValidationError):
        identity.build_provenance.source_tree_dirty = True
    with pytest.raises(ValidationError):
        identity.effective_mcp_profile.audio_overlap = 0.2
    with pytest.raises(ValidationError):
        identity.build_provenance = _build_provenance()
    with pytest.raises(ValidationError):
        identity.effective_mcp_profile = _effective_mcp_profile()


@pytest.mark.parametrize("value", ["WHEEL", "Wheel", "editable", "", 1])
def test_provenance_rejects_unknown_artefact_kind_values(value: object) -> None:
    """Only the three closed artefact kind values are admitted during reconstruction."""
    payload = _build_provenance().model_dump(mode="json")
    payload["artefact_kind"] = value
    with pytest.raises(ValidationError):
        AgentBuildProvenance.model_validate(payload)


@pytest.mark.parametrize(
    ("kind", "digest"),
    [
        (ColdArtefactKind.WHEEL, _HEX_64),
        (ColdArtefactKind.SDIST, _HEX_64),
        (ColdArtefactKind.EDITABLE_SOURCE, None),
    ],
)
def test_provenance_admits_matching_artefact_digest(
    kind: ColdArtefactKind, digest: str | None
) -> None:
    """Packaged and editable provenance assertions have opposite digest requirements."""
    assert _build_provenance(artefact_kind=kind, artefact_sha256=digest).artefact_kind is kind


@pytest.mark.parametrize(
    ("kind", "digest"),
    [
        (ColdArtefactKind.WHEEL, None),
        (ColdArtefactKind.SDIST, None),
        (ColdArtefactKind.EDITABLE_SOURCE, _HEX_64),
    ],
)
def test_provenance_refuses_mismatched_artefact_digest(
    kind: ColdArtefactKind, digest: str | None
) -> None:
    """Both directions of the provenance digest guard fail closed."""
    with pytest.raises(ColdIdentityError) as raised:
        _build_provenance(artefact_kind=kind, artefact_sha256=digest)
    assert raised.value.failure is ColdIdentityFailure.PROVENANCE_ARTEFACT_DIGEST_MISMATCHED


@pytest.mark.parametrize(
    "revision",
    [
        "a" * 39,
        "a" * 41,
        _HEX_40.upper(),
        "g" + "a" * 39,
        "main",
        "abcdef0",
        f" {_HEX_40}",
        f"{_HEX_40}\n",
        f"{_HEX_40}x",
        1,
    ],
)
def test_provenance_refuses_noncanonical_source_revision(revision: object) -> None:
    """Build revisions require exactly one lowercase full SHA token."""
    with pytest.raises(ValidationError):
        _build_provenance(source_revision=revision)


@pytest.mark.parametrize("digest", ["a" * 63, "a" * 65, _HEX_64.upper(), "g" * 64])
def test_provenance_and_profile_refuse_noncanonical_digests(digest: str) -> None:
    """Both caller assertions use anchored lowercase digest grammars."""
    with pytest.raises(ValidationError):
        _build_provenance(artefact_sha256=digest)
    with pytest.raises(ValidationError):
        _effective_mcp_profile(source_sha256=digest)


@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (_build_provenance, "source_tree_dirty", 1),
        (_build_provenance, "source_tree_dirty", "true"),
        (_effective_mcp_profile, "first_crack_onnx_threads", "2"),
        (_effective_mcp_profile, "first_crack_onnx_threads", True),
        (_effective_mcp_profile, "first_crack_confirmation_window_seconds", "0.9"),
        (_effective_mcp_profile, "source_byte_length", "0"),
        (_effective_mcp_profile, "session_ror_window_seconds", "60"),
        (_effective_mcp_profile, "session_ror_window_seconds", 60.0),
        (_effective_mcp_profile, "session_ror_min_sample_seconds", "10"),
        (_effective_mcp_profile, "session_ror_min_sample_seconds", 10.0),
    ],
)
def test_new_model_scalar_fields_are_strict(factory: Any, field: str, value: object) -> None:
    """New scalar inputs refuse coercion at their closed model boundaries."""
    with pytest.raises(ValidationError):
        factory(**{field: value})


def test_new_model_scalars_are_field_strict_without_model_level_strictness() -> None:
    """Strictness belongs to new scalar fields while the enum remains JSON-round-trippable."""
    for model in (AgentBuildProvenance, EffectiveMCPProfile):
        assert model.model_config.get("frozen") is True
        assert model.model_config.get("extra") == "forbid"
        assert model.model_config.get("allow_inf_nan") is False
        assert "strict" not in model.model_config
    assert ColdRunIdentity.model_config.get("frozen") is True
    assert ColdRunIdentity.model_config.get("extra") == "forbid"
    assert ColdRunIdentity.model_config.get("allow_inf_nan") is False
    assert "strict" not in ColdRunIdentity.model_config

    def is_strict(annotation: object) -> bool:
        """Return whether one resolved scalar annotation carries strict field metadata."""
        if get_origin(annotation) is Annotated:
            _, *metadata = get_args(annotation)
            return any(
                getattr(item, "strict", False) is True
                or any(
                    getattr(detail, "strict", False) is True
                    for detail in getattr(item, "metadata", ())
                )
                for item in metadata
            )
        return any(is_strict(item) for item in get_args(annotation))

    for model, enum_fields in (
        (AgentBuildProvenance, {"artefact_kind"}),
        (EffectiveMCPProfile, set[str]()),
    ):
        annotations = get_type_hints(model, include_extras=True)
        for field_name in model.model_fields:
            if field_name not in enum_fields:
                assert is_strict(annotations[field_name])
    artefact_kind = get_type_hints(AgentBuildProvenance, include_extras=True)["artefact_kind"]
    assert is_strict(artefact_kind) is False


def test_dirty_source_tree_is_recorded_frozen_and_hashed(tmp_path: Path) -> None:
    """This model-only slice records dirty source state without run-admission enforcement."""
    identity = _freeze(tmp_path, build_provenance=_build_provenance(source_tree_dirty=True))

    assert identity.build_provenance.source_tree_dirty is True
    assert identity.model_dump(mode="json")["build_provenance"]["source_tree_dirty"] is True
    assert len(identity_sha256(identity)) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_byte_length", -1),
        ("first_crack_onnx_threads", 0),
        ("first_crack_min_positive_windows", 0),
        ("first_crack_confirmation_window_seconds", 0.0),
        ("audio_sample_rate", 0),
        ("audio_window_seconds", 0.0),
        ("audio_overlap", -0.1),
        ("audio_overlap", 1.0),
        ("audio_hop_seconds", 0.0),
        ("session_ror_window_seconds", 0),
        ("session_ror_min_sample_seconds", 0),
    ],
)
def test_effective_profile_refuses_out_of_range_comparables(field: str, value: object) -> None:
    """Typed effective configuration comparables use their ratified bounds."""
    with pytest.raises(ValidationError):
        _effective_mcp_profile(**{field: value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "field", ["first_crack_confirmation_window_seconds", "audio_window_seconds", "audio_overlap"]
)
def test_effective_profile_refuses_nonfinite_float_comparables(field: str, value: float) -> None:
    """Finite profile values cannot carry a non-finite identity representation."""
    with pytest.raises(ValidationError):
        _effective_mcp_profile(**{field: value})


def test_effective_profile_admits_revision_tokens_without_operator_entropy_screen() -> None:
    """Pinned revisions use a bounded identifier grammar, not operator free-text screening."""
    assert _effective_mcp_profile(first_crack_revision=_HEX_40).first_crack_revision == _HEX_40
    assert cold_identity._operator_text_is_safe(_HEX_40) is False  # pyright: ignore[reportPrivateUsage]
    assert _effective_mcp_profile(first_crack_revision="a" * 128).first_crack_revision == "a" * 128
    for value in ("", "a" * 129, "model revision", "model/path", "model:tag", "é"):
        with pytest.raises(ValidationError):
            _effective_mcp_profile(first_crack_revision=value)


@pytest.mark.parametrize(
    "revision",
    [
        "sk-abcdefghijklmnop",
        "api_key=synthetic-value",
        "ghp_abcdefghijklmnop",
        "gho_abcdefghijklmnop",
        "ghu_abcdefghijklmnop",
        "ghs_abcdefghijklmnop",
        "ghr_abcdefghijklmnop",
        "github_pat_abcdefghijklmnop",
        "glpat-abcdefghijklmnop",
        "xoxb-abcdefghijklmnop",
        "xoxp-abcdefghijklmnop",
        "xoxa-abcdefghijklmnop",
        "xoxr-abcdefghijklmnop",
        "xoxs-abcdefghijklmnop",
        "AKIA1234567890ABCDEF",
        "eyJabcde.eyJfghij.abcdefgh",
        "eyJabcde.eyJfghij." + "a" * 129,
    ],
)
def test_effective_profile_refuses_credential_shaped_revisions(revision: str) -> None:
    """Credential-shaped revision tokens fail closed without echoing their contents."""
    with pytest.raises(ColdIdentityError) as raised:
        _effective_mcp_profile(first_crack_revision=revision)

    assert raised.value.failure is ColdIdentityFailure.OPERATOR_TEXT_REJECTED
    assert revision not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_nested_identity_refuses_credential_shaped_revision(tmp_path: Path) -> None:
    """Containing identity reconstruction repeats revision credential-shape admission."""
    revision = "github_pat_abcdefghijklmnop"
    payload = _freeze(tmp_path).model_dump(mode="python")
    profile = cast(dict[str, object], payload["effective_mcp_profile"])
    profile["first_crack_revision"] = revision

    with pytest.raises(ColdIdentityError) as raised:
        ColdRunIdentity.model_validate(payload)

    assert raised.value.failure is ColdIdentityFailure.OPERATOR_TEXT_REJECTED
    assert revision not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_nested_identity_refuses_assignment_shaped_revision(tmp_path: Path) -> None:
    """Nested reconstruction hides assignment-shaped credential values on rejection."""
    revision = "api_key=synthetic-value"
    payload = _freeze(tmp_path).model_dump(mode="python")
    profile = cast(dict[str, object], payload["effective_mcp_profile"])
    profile["first_crack_revision"] = revision

    with pytest.raises(ColdIdentityError) as raised:
        ColdRunIdentity.model_validate(payload)

    assert raised.value.failure is ColdIdentityFailure.OPERATOR_TEXT_REJECTED
    assert revision not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_effective_profile_preserves_noncredential_revision_grammar_failure() -> None:
    """Non-credential values outside the bounded grammar still raise ValidationError."""
    with pytest.raises(ValidationError):
        _effective_mcp_profile(first_crack_revision="plain=value")


def test_unrelated_profile_failure_does_not_relabel_a_valid_credential_shape() -> None:
    """Only a revision-field error receives the closed credential-shape reason."""
    with pytest.raises(ValidationError):
        _effective_mcp_profile(
            first_crack_revision="github_pat_abcdefghijklmnop",
            audio_sample_rate=0,
        )


def test_cold_artefact_kind_is_a_plain_enum() -> None:
    """Cold artefact kinds remain closed plain enums rather than string enums."""
    assert issubclass(ColdArtefactKind, Enum)
    assert not issubclass(ColdArtefactKind, str)


def test_effective_profile_field_set_excludes_leak_and_duplicate_surfaces() -> None:
    """The typed profile remains the ratified twelve-field allow-list."""
    assert set(EffectiveMCPProfile.model_fields) == {
        "source_sha256",
        "source_byte_length",
        "first_crack_onnx_threads",
        "first_crack_min_positive_windows",
        "first_crack_confirmation_window_seconds",
        "first_crack_revision",
        "audio_sample_rate",
        "audio_window_seconds",
        "audio_overlap",
        "audio_hop_seconds",
        "session_ror_window_seconds",
        "session_ror_min_sample_seconds",
    }
    assert "fc_confidence_threshold" in cold_identity.ManagedDeviceIdentity.model_fields
    assert len(ColdIdentityFailure) == 11
    with pytest.raises(ValidationError):
        _effective_mcp_profile(temperature_unit="fahrenheit")


def test_profile_float_normalisation_and_identity_digest_sensitivity(tmp_path: Path) -> None:
    """A float comparable normalises integers and each component changes the digest."""
    profile = _effective_mcp_profile(audio_window_seconds=10)
    assert profile.audio_window_seconds == 10.0

    baseline = _freeze(tmp_path)
    changed_provenance = _freeze(
        tmp_path, build_provenance=_build_provenance(source_revision="c" * 40)
    )
    changed_profile = _freeze(
        tmp_path, effective_mcp_profile=_effective_mcp_profile(audio_overlap=0.7)
    )
    assert identity_sha256(baseline) != identity_sha256(changed_provenance)
    assert identity_sha256(baseline) != identity_sha256(changed_profile)


@pytest.mark.parametrize(
    ("component", "field", "value"),
    [
        ("provenance", "source_revision", "c" * 40),
        ("provenance", "source_tree_dirty", True),
        ("provenance", "artefact_kind", ColdArtefactKind.SDIST),
        ("provenance", "artefact_sha256", "b" * 64),
        ("profile", "source_sha256", "b" * 64),
        ("profile", "source_byte_length", 101),
        ("profile", "first_crack_onnx_threads", 8),
        ("profile", "first_crack_min_positive_windows", 5),
        ("profile", "first_crack_confirmation_window_seconds", 20.0),
        ("profile", "first_crack_revision", "c" * 40),
        ("profile", "audio_sample_rate", 44100),
        ("profile", "audio_window_seconds", 12.0),
        ("profile", "audio_overlap", 0.7),
        ("profile", "audio_hop_seconds", 1.0),
        ("profile", "session_ror_window_seconds", 61),
        ("profile", "session_ror_min_sample_seconds", 11),
    ],
)
def test_each_new_identity_field_changes_canonical_digest(
    tmp_path: Path, component: str, field: str, value: object
) -> None:
    """Every provenance and profile field contributes to the canonical identity digest."""
    baseline = _freeze(tmp_path)
    if component == "provenance":
        values = baseline.build_provenance.model_dump(mode="python")
        values[field] = value
        changed = _freeze(tmp_path, build_provenance=AgentBuildProvenance.model_validate(values))
    else:
        values = baseline.effective_mcp_profile.model_dump(mode="python")
        values[field] = value
        changed = _freeze(
            tmp_path, effective_mcp_profile=EffectiveMCPProfile.model_validate(values)
        )

    assert identity_sha256(changed) != identity_sha256(baseline)


def test_freeze_records_manifest_and_credential_name_without_its_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity contains the permitted name and boolean, never an environment value."""
    secret = "sk-this-must-not-appear-in-the-frozen-identity"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    identity = _freeze(tmp_path)
    dumped = identity.model_dump(mode="json")
    assert dumped["credential_env_var_name"] == "OPENROUTER_API_KEY"
    assert dumped["credential_present"] is True
    assert secret not in repr(dumped)
    assert identity.model_repo_id == REPO_ID
    assert identity.model_revision == REVISION
    assert [(entry.relative_path, entry.sha256) for entry in identity.model_manifest] == [
        (entry.relative_path, entry.sha256) for entry in MANIFEST_FILES
    ]
    tree = ast.parse(inspect.getsource(cold_identity))
    environment_value_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id in {"environ", "getenv"}
        or isinstance(node, ast.Attribute)
        and node.attr in {"environ", "getenv"}
    ]
    assert environment_value_nodes == []


def test_freeze_copies_packaged_manifest_without_model_artifact_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freezing copies manifest constants and performs only the explicit boot-ID read."""
    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("123e4567-e89b-12d3-a456-426614174000\n", encoding="ascii")
    opened_paths: list[Path] = []
    original_open = Path.open

    def tracking_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        """Record the explicitly allowed boot-ID file open."""
        opened_paths.append(path)
        return cast(Any, original_open)(path, *args, **kwargs)

    def fail_hash(*args: object, **kwargs: object) -> None:
        """Fail if freezing attempts to hash an artifact instead of copying constants."""
        pytest.fail("freeze_identity must not hash model artifact files")

    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr("roastpilot_agent.cold_characterisation.identity.hashlib.sha256", fail_hash)

    _freeze(tmp_path, boot_id_path=boot_id_path, _write_boot_id=False)

    assert opened_paths == [boot_id_path]


def test_identity_rejects_substituted_packaged_manifest_constants(tmp_path: Path) -> None:
    """The frozen model refuses a caller-substituted packaged identity value."""
    payload = _freeze(tmp_path).model_dump(mode="python")
    payload["model_revision"] = "untrusted-revision"
    with pytest.raises(ValidationError, match="packaged identity constants"):
        ColdRunIdentity.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "failure"),
    [
        ("coffee_roaster_mcp_version", "0.2.2", ColdIdentityFailure.MCP_VERSION_NOT_PINNED),
        (
            "runtime_config",
            _runtime(temperature_unit="fahrenheit"),
            ColdIdentityFailure.TEMPERATURE_UNIT_NOT_CELSIUS,
        ),
        (
            "device_config",
            MCPDeviceConfig(recording_devices=("first", "second")).model_dump(mode="json"),
            ColdIdentityFailure.RECORDING_DEVICE_NOT_SINGLE,
        ),
        (
            "runtime_config",
            _runtime(first_crack_mode="disabled"),
            ColdIdentityFailure.INFERENCE_NOT_ACTIVE_IN_IDENTITY,
        ),
        ("credential_env_var_name", "OTHER_TOKEN", ColdIdentityFailure.CREDENTIAL_NAME_NOT_ALLOWED),
        (
            "operator_host_notes",
            "sk-abcdefghijklmnopqrstuvwx",
            ColdIdentityFailure.OPERATOR_TEXT_REJECTED,
        ),
    ],
)
def test_model_validate_repeats_closed_identity_admissions(
    tmp_path: Path, field: str, value: object, failure: ColdIdentityFailure
) -> None:
    """Direct reconstruction cannot bypass the same closed identity admissions."""
    payload = _freeze(tmp_path).model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ColdIdentityError) as raised:
        ColdRunIdentity.model_validate(payload)

    assert raised.value.failure is failure


def test_direct_reconstruction_rejects_unknown_device_config_key_before_hashing(
    tmp_path: Path,
) -> None:
    """A credential-named unknown device key is structurally unrepresentable."""
    payload = _freeze(tmp_path).model_dump(mode="json")
    device_config = cast(dict[str, object], payload["device_config"])
    device_config["api_key"] = "synthetic-value"

    with pytest.raises(ValidationError, match="api_key"):
        ColdRunIdentity.model_validate(payload)


def test_device_config_round_trip_preserves_identity_and_digest(tmp_path: Path) -> None:
    """The closed device projection survives JSON reconstruction canonically."""
    identity = _freeze(tmp_path)

    reconstructed = ColdRunIdentity.model_validate(identity.model_dump(mode="json"))

    assert reconstructed == identity
    assert identity_sha256(reconstructed) == identity_sha256(identity)


def test_managed_device_config_is_frozen_and_recording_devices_are_a_tuple(tmp_path: Path) -> None:
    """Nested device identity state cannot be reassigned or retain a mutable list."""
    identity = _freeze(tmp_path)

    with pytest.raises(ValidationError):
        identity.device_config.serial_port = "changed"

    assert isinstance(identity.device_config.recording_devices, tuple)


def test_freeze_detaches_from_caller_device_config_and_input_list(tmp_path: Path) -> None:
    """Later caller mutations cannot change a frozen identity or its digest."""
    devices = ["USB microphone"]
    caller_config = MCPDeviceConfig.model_validate({"recording_devices": devices})
    identity = _freeze(tmp_path, device_config=caller_config)
    digest = identity_sha256(identity)

    devices.append("later microphone")
    caller_config.recording_devices = ("replacement microphone",)

    assert identity.device_config.recording_devices == ("USB microphone",)
    assert identity_sha256(identity) == digest


def test_freeze_projects_every_managed_device_config_field(tmp_path: Path) -> None:
    """The frozen projection copies every managed field without retaining caller types."""
    source = MCPDeviceConfig.model_validate(
        {
            "serial_port": "/dev/ttyS11",
            "roaster_driver": "hottop_kn8828b_2k_plus",
            "audio_input_device": "hw:2,0",
            "recording_enabled": True,
            "recording_autocapture": False,
            "recording_devices": ["USB microphone"],
            "fc_mode": "manual",
            "fc_confidence_threshold": 0.75,
            "auto_t0_detection_enabled": True,
            "auto_t0_drop_threshold_c": 31.5,
            "mcp_yaml_source_path": Path("/etc/roastpilot/managed-mcp.yaml"),
            "ambient_mode": "yoctopuce",
            "ambient_device": "YOCTO-USB-1234567890ABCDEF",
            "ambient_poll_interval_seconds": 12.5,
        }
    )
    identity = _freeze(tmp_path, device_config=source)

    assert identity.device_config.model_dump(mode="python") == {
        "serial_port": "/dev/ttyS11",
        "roaster_driver": "hottop_kn8828b_2k_plus",
        "audio_input_device": "hw:2,0",
        "recording_enabled": True,
        "recording_autocapture": False,
        "recording_devices": ("USB microphone",),
        "fc_mode": "manual",
        "fc_confidence_threshold": 0.75,
        "auto_t0_detection_enabled": True,
        "auto_t0_drop_threshold_c": 31.5,
        "mcp_yaml_source_path": "/etc/roastpilot/managed-mcp.yaml",
        "ambient_mode": "yoctopuce",
        "ambient_device": "YOCTO-USB-1234567890ABCDEF",
        "ambient_poll_interval_seconds": 12.5,
    }
    assert frozenset(type(identity.device_config).model_fields) == _MCP_DEVICE_CONFIG_FIELD_NAMES


def test_cold_identity_failure_is_plain_enum_without_member_string_comparisons() -> None:
    """Closed failures stay plain enum members and are not compared to string literals."""
    assert issubclass(ColdIdentityFailure, Enum)
    assert not issubclass(ColdIdentityFailure, str)
    assert all(not isinstance(member, str) for member in ColdIdentityFailure)

    tree = ast.parse(inspect.getsource(cold_identity))

    def is_failure_member_or_value(node: ast.expr) -> bool:
        """Recognise direct references to one closed failure member or its value."""
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return node.value.id == "ColdIdentityFailure"
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "value"
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "ColdIdentityFailure"
        )

    string_compared_members = [
        comparison
        for comparison in ast.walk(tree)
        if isinstance(comparison, ast.Compare)
        and any(
            is_failure_member_or_value(operand)
            for operand in [comparison.left, *comparison.comparators]
        )
        and any(
            isinstance(operand, ast.Constant) and isinstance(operand.value, str)
            for operand in [comparison.left, *comparison.comparators]
        )
    ]

    assert string_compared_members == []


@pytest.mark.parametrize(
    "field_names", [set(_MCP_DEVICE_CONFIG_FIELD_NAMES | {"new_field"}), set[str]()]
)
def test_freeze_refuses_mcp_device_field_set_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field_names: set[str]
) -> None:
    """Added, removed, or renamed upstream managed fields refuse freezing."""
    monkeypatch.setattr(MCPDeviceConfig, "model_fields", {name: object() for name in field_names})

    _assert_failure(tmp_path, ColdIdentityFailure.DEVICE_CONFIG_FIELD_SET_DRIFTED)


@pytest.mark.parametrize(
    "field,value",
    [
        ("audio_input_device", "api" + "_key" + "=" + "synthetic-value"),
        ("audio_input_device", "contains" + chr(1) + "control"),
        ("audio_input_device", "x" * 513),
    ],
)
def test_freeze_rejects_unsafe_managed_device_text(tmp_path: Path, field: str, value: str) -> None:
    """Credential-shaped, non-printable, and oversized device strings fail closed."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.DEVICE_CONFIG_VALUE_REJECTED,
        device_config=MCPDeviceConfig.model_validate(
            {field: value, "recording_devices": ("USB microphone",)}
        ),
    )


def test_freeze_rejects_unsafe_recording_device_text(tmp_path: Path) -> None:
    """Every recording-device entry uses the same closed device text screen."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.DEVICE_CONFIG_VALUE_REJECTED,
        device_config=MCPDeviceConfig(
            recording_devices=("api" + "_key" + "=" + "synthetic-value",)
        ),
    )


def test_freeze_admits_high_entropy_device_identifiers(tmp_path: Path) -> None:
    """Legitimate device identifiers do not use the operator-note entropy guard."""
    identity = _freeze(
        tmp_path,
        device_config=MCPDeviceConfig(
            ambient_device="0123456789abcdef0123456789abcdef01234567",
            serial_port="/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A10B2C3D-if00-port0",
            recording_devices=("USB microphone",),
        ),
    )

    assert identity.device_config.ambient_device == "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize("version", ["0.2.0", "0.2.2", "0.3.0", "0.2.1.post1", "0.2.10", " 0.2.1"])
def test_freeze_requires_exact_mcp_version(tmp_path: Path, version: str) -> None:
    """Only the exact ratified MCP release may be frozen."""
    _assert_failure(
        tmp_path, ColdIdentityFailure.MCP_VERSION_NOT_PINNED, coffee_roaster_mcp_version=version
    )


@pytest.mark.parametrize("unit", ["F", "fahrenheit", "celsius-ish", "", "Celsius"])
def test_freeze_requires_exact_celsius_token(tmp_path: Path, unit: str) -> None:
    """Forward-tolerant runtime strings are admitted only by whole-token membership."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.TEMPERATURE_UNIT_NOT_CELSIUS,
        runtime_config=_runtime(temperature_unit=unit),
    )


@pytest.mark.parametrize("devices", [None, (), ("first", "second")])
def test_freeze_requires_one_recording_device(
    tmp_path: Path, devices: tuple[str, ...] | None
) -> None:
    """A characterisation run refuses zero or multiple recording devices."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.RECORDING_DEVICE_NOT_SINGLE,
        device_config=MCPDeviceConfig(recording_devices=devices),
    )


@pytest.mark.parametrize("mode", ["disabled", "manual"])
def test_freeze_refuses_non_audio_inference_mode(tmp_path: Path, mode: str) -> None:
    """Disabled detector configuration cannot start a qualifying cold run."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.INFERENCE_NOT_ACTIVE_IN_IDENTITY,
        runtime_config=_runtime(first_crack_mode=mode),
    )


def test_freeze_refuses_non_int8_precision_and_committed_bare_fixture_shape(tmp_path: Path) -> None:
    """The captured bare runtime remains an inadmissible disabled-detector identity."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.INFERENCE_NOT_ACTIVE_IN_IDENTITY,
        runtime_config=_runtime(model_precision="fp32"),
    )
    fixture_runtime = _runtime(first_crack_mode="disabled", model_precision="int8")
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.INFERENCE_NOT_ACTIVE_IN_IDENTITY,
        runtime_config=fixture_runtime,
    )


@pytest.mark.parametrize(
    "content",
    [
        "",
        "123E4567-E89B-12D3-A456-426614174000\n",
        "123e4567-e89b-12d3-a456-426614174000\nsecond",
        "123e4567-e89b-12d3-a456-42661417400",
        "é",
        "x" * 129,
    ],
)
def test_freeze_refuses_malformed_or_oversized_boot_id(tmp_path: Path, content: str) -> None:
    """The byte-capped, anchored boot-ID grammar has no default identity."""
    boot_id_path = tmp_path / "bad_boot_id"
    boot_id_path.write_text(content, encoding="utf-8")
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.BOOT_ID_UNREADABLE
        if content == "x" * 129
        else ColdIdentityFailure.BOOT_ID_MALFORMED,
        boot_id_path=boot_id_path,
    )


@pytest.mark.parametrize(
    "boot_id",
    [
        "123E4567-E89B-12D3-A456-426614174000",
        "123e4567-e89b-12d3-a456-426614174000\n",
        "123e4567-e89b-12d3-a456-42661417400",
        "123e4567-e89b-12d3-a456-426614174000\nsecond",
    ],
)
def test_model_validate_refuses_noncanonical_stored_boot_id(tmp_path: Path, boot_id: str) -> None:
    """Reconstruction admits only the normalized boot ID, without filesystem I/O."""
    payload = _freeze(tmp_path).model_dump(mode="python")
    payload["boot_id"] = boot_id

    with pytest.raises(ColdIdentityError) as raised:
        ColdRunIdentity.model_validate(payload)

    assert raised.value.failure is ColdIdentityFailure.BOOT_ID_MALFORMED


def test_model_validate_round_trips_normalized_boot_id_without_filesystem_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconstruction preserves a valid frozen boot ID without rereading its source."""
    identity = _freeze(tmp_path)
    monkeypatch.setattr(Path, "open", pytest.fail)

    reconstructed = ColdRunIdentity.model_validate(identity.model_dump(mode="python"))

    assert reconstructed == identity
    assert reconstructed.boot_id == "123e4567-e89b-12d3-a456-426614174000"


def test_freeze_refuses_missing_boot_id(tmp_path: Path) -> None:
    """An unreadable boot source cannot become an unknown identity field."""
    _assert_failure(
        tmp_path, ColdIdentityFailure.BOOT_ID_UNREADABLE, boot_id_path=tmp_path / "missing"
    )


def test_freeze_maps_invalid_boot_id_path_to_closed_unreadable_failure(tmp_path: Path) -> None:
    """Caller-path validation errors cannot escape the closed boot-ID grammar."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.BOOT_ID_UNREADABLE,
        boot_id_path=Path("embedded\x00nul"),
    )


def test_freeze_requires_allowed_credential_name(tmp_path: Path) -> None:
    """Only the declared OpenRouter credential name may be represented."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.CREDENTIAL_NAME_NOT_ALLOWED,
        credential_env_var_name="OTHER_TOKEN",
    )


@pytest.mark.parametrize(
    "field",
    ["stimulus_block", "operator_host_notes", "operator_psu_notes", "operator_cooling_notes"],
)
@pytest.mark.parametrize(
    "text",
    ["sk-abcdefghijklmnopqrstuvwx", "api_key=secret-value", "contains\x01control", "x" * 2001],
)
def test_freeze_rejects_secret_shaped_or_invalid_operator_text(
    tmp_path: Path, field: str, text: str
) -> None:
    """All operator free text raises rather than redacting secret-shaped content."""
    _assert_failure(tmp_path, ColdIdentityFailure.OPERATOR_TEXT_REJECTED, **{field: text})


def test_freeze_rejects_high_entropy_operator_text(tmp_path: Path) -> None:
    """A credential-like high-entropy token is rejected through public admission."""
    _assert_failure(
        tmp_path,
        ColdIdentityFailure.OPERATOR_TEXT_REJECTED,
        operator_host_notes="abcdefghijklmnopqrstuvwxyz",
    )


def test_identity_hash_is_canonical_stable_and_sensitive(tmp_path: Path) -> None:
    """Canonical JSON hashing is stable for equals and changes for one field."""
    identity = _freeze(tmp_path)
    equivalent = _freeze(tmp_path)
    changed = _freeze(tmp_path, pi_revision="d04171")
    digest = identity_sha256(identity)
    assert digest == identity_sha256(equivalent)
    assert digest != identity_sha256(changed)
    assert len(digest) == 64
    assert digest == digest.lower()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "field",
    [
        "command_interval_seconds",
        "sample_interval_seconds",
        "auto_t0_drop_threshold_c",
    ],
)
def test_identity_rejects_non_finite_runtime_values_before_hashing(
    tmp_path: Path, field: str, value: float
) -> None:
    """Nested tolerant runtime mirrors cannot carry non-finite identity values."""
    runtime = _runtime(**{field: value})
    with pytest.raises(
        ValidationError, match="runtime configuration identity values must be finite"
    ):
        identity_sha256(_freeze(tmp_path, runtime_config=runtime))


def test_identity_rejects_non_finite_controller_tick(tmp_path: Path) -> None:
    """The frozen finite model refuses non-finite values before hashing."""
    with pytest.raises(ValidationError):
        _freeze(tmp_path, controller_tick_seconds=float("nan"))
