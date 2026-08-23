"""Tests for config reload invalidation at PUT and roast start (D76/D78, #430).

Verifies that:
- A saved config change (e.g. advisor model slug, controller tick interval) is
  picked up by ``start_roast`` and used by the runner built for that roast,
  without requiring an agent restart.
- A successful ``PUT /api/config`` clears only advisor-health probes made stale
  by the freshly resolved effective advisor dispatch, without applying config
  to the current roast or making a network call.
- A roast already in progress keeps its captured config (no mid-loop mutation).
- Safety limits cannot be changed via the saved config file (the injector skips
  ``ROASTPILOT_SAFETY__`` unconditionally).

All tests are hardware-free (API-only mode: no roaster wired, so ``start_roast``
persists the run record and reloads config but does not start the tick loop).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import roastpilot_agent.api as api_module
from roastpilot_agent.advisor import FakeAdvisor
from roastpilot_agent.api import RoastService, create_app
from roastpilot_agent.config import (
    AdvisorConfig,
    AmbientFanDoctrine,
    AppConfig,
    ControllerConfig,
    MCPDeviceConfig,
)
from roastpilot_agent.config_store import (
    AdvisorConfigEdit,
    AppConfigEdit,
    ConfigFileError,
    ControllerConfigEdit,
    PreFirstCrackLeversEdit,
    persist_config_edit,
)
from roastpilot_agent.models import (
    AdvisorHealth,
    AdvisorHealthStatus,
    RoastPhase,
    RoastProfile,
)
from roastpilot_agent.store import RoastStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile(**kwargs: Any) -> dict[str, Any]:
    base = {
        "name": "Test Roast",
        "bean_origin": "Kenya",
        "bean_weight_grams": 250.0,
        "initial_heat_percent": 70,
        "initial_fan_percent": 40,
        "target_drop_temp_c": 205.0,
        "target_development_percent": 20.0,
    }
    base.update(kwargs)
    return base


def _reachable_probe(service: RoastService) -> AdvisorHealth:
    """Build a reachable probe for the service's current advisor config."""
    return AdvisorHealth(
        status=AdvisorHealthStatus.REACHABLE,
        provider="openai_compatible",
        model_slug=service._config.advisor.model_slug,  # pyright: ignore[reportPrivateUsage]
    )


async def _put_config(app: Any, body: dict[str, Any]) -> Any:
    """Send one config PUT through the public ASGI boundary."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.put("/api/config", json=body)


async def _health_body(app: Any) -> dict[str, Any]:
    """Read the public health response through the public ASGI boundary."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[RoastStore]:
    """Initialised store backed by a per-test SQLite file."""
    db = RoastStore(tmp_path / "test.db")
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated config file path; point load_app_config at it via env var.

    Also clears inherited ``ROASTPILOT_*`` (qa finding, folded pre-open). These
    tests assert on specific resolved model identities, and env beats the saved
    file, so a real ``ROASTPILOT_ADVISOR__MODEL_SLUG`` exported in a developer's
    shell reddens six of them. Same guard ``test_launch_banner``'s own
    ``config_file`` already carries; the sibling class was fixed in
    ``test_config_store`` and simply had not been propagated here.
    """
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_"):
            monkeypatch.delenv(key, raising=False)
    path = tmp_path / "roastpilot-config.yaml"
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(path))
    return path


# ---------------------------------------------------------------------------
# Core apply-next-roast tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saved_advisor_model_reflected_in_next_roast_service_config(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A PUT-saved advisor model slug is picked up at the start of the next roast.

    The service starts with the schema default model slug; a persisted edit
    changes it; the next start_roast reloads the file and self._config reflects
    the new value.  Verifies the D78 apply-next-roast guarantee for the advisor
    config path.
    """
    svc = RoastService(store, live_serve_mode=True)
    initial_model = svc._config.advisor.model_slug  # pyright: ignore[reportPrivateUsage]

    # Persist a change to the advisor model slug via the same write path as PUT /api/config.
    persist_config_edit(
        AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4-turbo-preview"))
    )

    # Start a roast — reload should pick up the saved model slug.
    await svc.start_roast(RoastProfile(**_profile()))

    reloaded_model = svc._config.advisor.model_slug  # pyright: ignore[reportPrivateUsage]
    assert reloaded_model == "openai/gpt-4-turbo-preview"
    assert reloaded_model != initial_model


@pytest.mark.asyncio
async def test_changing_the_model_invalidates_the_startup_reachability_probe(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A model change clears the stale REACHABLE on ``GET /api/health`` (#747).

    The probe runs once, at serve startup, against ``advisor.model_slug``.
    Before D151 a changed slug could not reach a roast, so a stale probe result
    was harmless. Now it IS the model that answers, and the operator checks
    health before charging — so health must not vouch for a slug nothing has
    contacted. ``None`` renders as "not probed", which is the honest state.
    """
    svc = RoastService(store, live_serve_mode=True)
    svc.set_advisor_health(
        AdvisorHealth(
            status=AdvisorHealthStatus.REACHABLE,
            provider="openai_compatible",
            model_slug=svc._config.advisor.model_slug,  # pyright: ignore[reportPrivateUsage]
        )
    )

    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4.1-mini")))
    await svc.start_roast(RoastProfile(**_profile()))

    assert (await svc.health()).advisor is None


@pytest.mark.asyncio
async def test_an_advisory_paused_readout_survives_a_model_change(
    store: RoastStore,
    config_file: Path,
) -> None:
    """NOT_CONFIGURED is about the ADVISOR being absent, not about a model.

    With no API key, `probe_advisor_health(None)` returns NOT_CONFIGURED and
    names no model. Keying invalidation on "the slug differs" therefore wiped
    it on the first roast, regressing an explicit advisory-paused readout to
    the ambiguous `advisor: null` — a defect introduced by the invalidation fix
    itself (local Codex P2, folded pre-open).
    """
    svc = RoastService(store, live_serve_mode=True)
    paused = AdvisorHealth(status=AdvisorHealthStatus.NOT_CONFIGURED)
    svc.set_advisor_health(paused)

    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4.1-mini")))
    await svc.start_roast(RoastProfile(**_profile()))

    assert (await svc.health()).advisor == paused


@pytest.mark.asyncio
async def test_a_model_less_UNREACHABLE_does_not_survive_a_model_change(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A failed probe names no model either — and it MUST still go stale.

    `probe_advisor_health` returns UNREACHABLE with `model_slug=None` when the
    probe times out or raises, so "named no model" does not imply
    NOT_CONFIGURED. Keying the preservation on the missing slug pinned that
    failure forever, reporting the old advisor offline after a model change the
    probe never saw — the defect the NOT_CONFIGURED fix introduced (local Codex
    P2, folded pre-open). The invalidation keys on the STATUS instead.
    """
    svc = RoastService(store, live_serve_mode=True)
    svc.set_advisor_health(
        AdvisorHealth(status=AdvisorHealthStatus.UNREACHABLE, error="probe timed out")
    )

    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4.1-mini")))
    await svc.start_roast(RoastProfile(**_profile()))

    assert (await svc.health()).advisor is None


@pytest.mark.asyncio
async def test_an_endpoint_change_alone_invalidates_the_probe(
    store: RoastStore,
    config_file: Path,
) -> None:
    """Changing WHERE the model is reached stales the probe too.

    A probe describes what it CONTACTED, and that is decided by provider +
    base URL + key env var + the resolved model, not by the slug alone. Keying
    invalidation on the slug let a REACHABLE result survive an endpoint swap —
    same slug, different server — vouching for something nothing had contacted
    (Claude review, folded pre-open).
    """
    svc = RoastService(store, live_serve_mode=True)
    svc.set_advisor_health(
        AdvisorHealth(
            status=AdvisorHealthStatus.REACHABLE,
            provider="openai_compatible",
            model_slug=svc._config.advisor.model_slug,  # pyright: ignore[reportPrivateUsage]
        )
    )

    persist_config_edit(
        AppConfigEdit(advisor=AdvisorConfigEdit(provider_base_url="http://proxy.local/v1"))
    )
    await svc.start_roast(RoastProfile(**_profile()))

    assert (await svc.health()).advisor is None


@pytest.mark.asyncio
async def test_a_base_slug_change_invalidates_even_with_a_pinned_phase_slot(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PROBED model is the base slug, so it belongs in the identity.

    `healthcheck` probes `model_slug`, while advice dispatches the
    phase-RESOLVED model. With a DEVELOPMENT slot pinned, the base slug can
    change while the advice model does not — and comparing only the advice
    models left `/api/health` describing a probe of the previous base model
    (local Codex P2, folded pre-open).

    The slot is pinned via the env blob because `model_slug_by_phase` is
    deliberately absent from `AdvisorConfigEdit` (D151) — env and a hand-edited
    saved file are the supported ways to reach it, which is exactly why the
    shadowing path still has to be handled.
    """
    monkeypatch.setenv(
        "ROASTPILOT_ADVISOR__MODEL_SLUG_BY_PHASE", '{"development": "openai/gpt-4o-mini"}'
    )
    svc = RoastService(store, live_serve_mode=True)
    probed_base = svc._config.advisor.model_slug  # pyright: ignore[reportPrivateUsage]
    svc.set_advisor_health(
        AdvisorHealth(
            status=AdvisorHealthStatus.REACHABLE,
            provider="openai_compatible",
            model_slug=probed_base,
        )
    )

    # Only the probed BASE slug moves; the pinned slot keeps the advice model
    # identical across the reload.
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4.1-mini")))
    await svc.start_roast(RoastProfile(**_profile()))

    reloaded = svc._config.advisor  # pyright: ignore[reportPrivateUsage]
    assert reloaded.model_for(RoastPhase.DEVELOPMENT) == "openai/gpt-4o-mini"
    assert reloaded.model_slug != probed_base
    assert (await svc.health()).advisor is None


@pytest.mark.asyncio
async def test_a_reasoning_effort_change_alone_invalidates_the_probe(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reasoning effort is part of what the probe contacted.

    `build_model` bakes `reasoning_effort` into the model settings every cached
    agent uses, including the one `healthcheck` probes with — so an effort-only
    change makes the recorded probe describe a different call (Claude review,
    folded pre-open). Set via env because `reasoning_effort` is not in
    `AdvisorConfigEdit`.
    """
    svc = RoastService(store, live_serve_mode=True)
    svc.set_advisor_health(
        AdvisorHealth(
            status=AdvisorHealthStatus.REACHABLE,
            provider="openai_compatible",
            model_slug=svc._config.advisor.model_slug,  # pyright: ignore[reportPrivateUsage]
        )
    )

    monkeypatch.setenv("ROASTPILOT_ADVISOR__REASONING_EFFORT", "high")
    await svc.start_roast(RoastProfile(**_profile()))

    assert svc._config.advisor.reasoning_effort == "high"  # pyright: ignore[reportPrivateUsage]
    assert (await svc.health()).advisor is None


@pytest.mark.asyncio
async def test_a_cosmetic_base_url_edit_keeps_the_probe(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A trailing slash is the same endpoint, so the probe still describes it.

    The identity compares the NORMALISED base URL, using the same helper the
    FC-latency screen uses. Comparing raw strings would discard a valid probe on
    a purely cosmetic edit and report the advisor "not probed" for no reason
    (Claude review, folded pre-open).
    """
    svc = RoastService(store, live_serve_mode=True)
    probed = AdvisorHealth(
        status=AdvisorHealthStatus.REACHABLE,
        provider="openai_compatible",
        model_slug=svc._config.advisor.model_slug,  # pyright: ignore[reportPrivateUsage]
    )
    svc.set_advisor_health(probed)

    persist_config_edit(
        AppConfigEdit(advisor=AdvisorConfigEdit(provider_base_url="https://openrouter.ai/api/v1/"))
    )
    await svc.start_roast(RoastProfile(**_profile()))

    assert (await svc.health()).advisor == probed


@pytest.mark.asyncio
async def test_an_unchanged_model_keeps_its_reachability_probe(
    store: RoastStore,
    config_file: Path,
) -> None:
    """Starting a roast does NOT wipe a probe that still describes the advisor.

    The invalidation must be keyed on the model actually changing. Clearing
    unconditionally would blank the advisor readout on every ordinary roast —
    trading a stale-but-usually-right signal for no signal at all, which is the
    worse failure for a check the operator runs before every charge.
    """
    svc = RoastService(store, live_serve_mode=True)
    probed = AdvisorHealth(
        status=AdvisorHealthStatus.REACHABLE,
        provider="openai_compatible",
        model_slug=svc._config.advisor.model_slug,  # pyright: ignore[reportPrivateUsage]
    )
    svc.set_advisor_health(probed)

    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(prompt_version="c11")))
    await svc.start_roast(RoastProfile(**_profile()))

    assert (await svc.health()).advisor == probed


@pytest.mark.asyncio
async def test_saved_controller_field_reflected_in_next_roast_service_config(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A saved pre-FC heat target is reflected in self._config after start_roast.

    The controller config carries non-safety operational parameters (pre-FC
    heat/fan targets, etc.) that the operator may tune between roasts via
    PUT /api/config.
    """
    svc = RoastService(store, live_serve_mode=True)
    default_heat = svc._config.controller.pre_first_crack_levers.heat_target_percent  # pyright: ignore[reportPrivateUsage]

    # Write a non-default pre-FC heat target (must differ from the default of 100).
    new_heat = 85
    assert new_heat != default_heat
    persist_config_edit(
        AppConfigEdit(
            controller=ControllerConfigEdit(
                pre_first_crack_levers=PreFirstCrackLeversEdit(heat_target_percent=new_heat)
            )
        )
    )

    await svc.start_roast(RoastProfile(**_profile()))

    assert (
        svc._config.controller.pre_first_crack_levers.heat_target_percent  # pyright: ignore[reportPrivateUsage]
        == new_heat
    )


@pytest.mark.asyncio
async def test_second_roast_picks_up_second_save(
    store: RoastStore,
    config_file: Path,
) -> None:
    """Each roast start reloads from file so two consecutive saves are both honoured.

    Roast 1 uses save-A; roast 2 uses save-B (not save-A).  A completed first
    roast is simulated by marking the run ended and clearing the in-memory
    active_run_id (mirrors the controller's finalize path).
    """
    from roastpilot_agent.models import RoastPhase

    svc = RoastService(store, live_serve_mode=True)

    # First roast with first saved model.
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini")))
    await svc.start_roast(RoastProfile(**_profile()))
    assert svc._config.advisor.model_slug == "openai/gpt-4o-mini"  # pyright: ignore[reportPrivateUsage]

    # Simulate completing the first roast: persist the outcome and clear in-memory state.
    first_run_id = svc.active_run_id
    assert first_run_id is not None
    await store.complete_run(
        run_id=first_run_id,
        outcome="completed",
        agent_phase=RoastPhase.COMPLETE,
    )
    svc.active_run_id = None

    # Second save and second roast.
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4-1-mini")))
    await svc.start_roast(RoastProfile(**_profile()))
    assert svc._config.advisor.model_slug == "openai/gpt-4-1-mini"  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Safety-limit immutability through reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_limits_unaffected_by_saved_config(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety limits are always env-resolved; the saved file cannot change them.

    The _inject_saved_as_env injector skips the ROASTPILOT_SAFETY__ prefix so
    no operator-editable file field can weaken a safety ceiling.  This test
    writes a YAML with a safety section by manually constructing the file (the
    public API doesn't expose safety edits — they are read-only in M1) and
    verifies the safety field is ignored on reload.
    """
    import yaml

    # Directly write a config file with a safety stanza (bypassing the public
    # API, which is the adversarial path the injector guards against).
    config_file.write_text(
        yaml.safe_dump(
            {
                "safety": {
                    # Adversarial: try to lower the bean-temp ceiling far below
                    # the schema default of 230 °C.  The injector skips the
                    # ROASTPILOT_SAFETY__ prefix so this must be ignored.
                    "max_bean_temp_c": 100.0,
                }
            }
        ),
        encoding="utf-8",
    )

    svc = RoastService(store, live_serve_mode=True)
    default_max_bean = svc._config.safety.max_bean_temp_c  # pyright: ignore[reportPrivateUsage]

    await svc.start_roast(RoastProfile(**_profile()))

    # Safety limits must not be reduced by the file — they remain at the
    # env/schema default of 230 °C.
    reloaded_max_bean = svc._config.safety.max_bean_temp_c  # pyright: ignore[reportPrivateUsage]
    assert reloaded_max_bean == default_max_bean
    assert reloaded_max_bean > 100.0  # file's adversarial value was NOT applied


@pytest.mark.asyncio
async def test_safety_policy_rebuilt_from_reloaded_config(
    store: RoastStore,
    config_file: Path,
) -> None:
    """self._safety is rebuilt from the reloaded config after start_roast.

    The SafetyPolicy must always match self._config.  Even though safety limits
    are env-resolved (and therefore identical after a reload), the _safety
    object reference must be fresh so it pairs with the new config instance.
    """

    svc = RoastService(store, live_serve_mode=True)
    original_safety = svc._safety  # pyright: ignore[reportPrivateUsage]

    # Write a benign (non-safety) saved change to trigger a meaningful reload.
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini")))

    await svc.start_roast(RoastProfile(**_profile()))

    # The SafetyPolicy object must be a new instance (rebuilt from fresh config).
    assert svc._safety is not original_safety  # pyright: ignore[reportPrivateUsage]
    # Its limits must still match the schema default (file cannot change safety).
    assert (
        svc._safety.limits.max_bean_temp_c  # pyright: ignore[reportPrivateUsage]
        == original_safety.limits.max_bean_temp_c  # pyright: ignore[reportPrivateUsage]
    )


# ---------------------------------------------------------------------------
# No mid-roast config mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_not_reloaded_while_roast_active(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A concurrent save during an active roast cannot start a second roast.

    start_roast returns 409 when a run is active, which means the reload code
    path is never reached on a concurrent attempt — the in-progress roast's
    config is never mutated.
    """
    from roastpilot_agent.api import RoastRunConflictError

    svc = RoastService(store, live_serve_mode=True)

    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4o-mini")))
    await svc.start_roast(RoastProfile(**_profile()))
    model_after_first_start = svc._config.advisor.model_slug  # pyright: ignore[reportPrivateUsage]

    # Now save a different model slug — simulates PUT /api/config mid-roast.
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4-1")))

    # A second start_roast attempt must be rejected (roast already active).
    with pytest.raises(RoastRunConflictError):
        await svc.start_roast(RoastProfile(**_profile()))

    # self._config must not have changed — the rejected start_roast must not
    # have mutated the running roast's config.
    assert svc._config.advisor.model_slug == model_after_first_start  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_http_start_roast_with_save_applies_next_roast(
    store: RoastStore,
    config_file: Path,
) -> None:
    """End-to-end: PUT /api/config then POST /api/roasts reflects saved model.

    Uses the full HTTP stack (test client) to verify the apply-next-roast
    behaviour is visible at the API boundary, not just at the service layer.
    """
    svc = RoastService(store, live_serve_mode=True)
    app = create_app(svc)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Persist a model change via PUT /api/config.
        put_body = {"advisor": {"model_slug": "openai/gpt-4-turbo"}}
        put_resp = await client.put("/api/config", json=put_body)
        assert put_resp.status_code == 200

        # Start a roast — the service should reload and pick up the saved model.
        post_resp = await client.post("/api/roasts", json=_profile())
        assert post_resp.status_code == 201

    # Verify via the service object (not just HTTP status).
    assert svc._config.advisor.model_slug == "openai/gpt-4-turbo"  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# PUT invalidates stale advisor health without applying runtime config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_put_that_changes_the_model_invalidates_the_probe_before_any_roast(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A successful model PUT clears a stale probe before any roast starts."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))
    app = create_app(service)

    response = await _put_config(app, {"advisor": {"model_slug": "openai/gpt-4.1-mini"}})

    assert response.status_code == 200
    assert (await service.health()).advisor is None
    assert await store.active_run() is None


@pytest.mark.asyncio
async def test_health_route_reports_not_probed_after_a_put(
    store: RoastStore,
    config_file: Path,
) -> None:
    """The public health JSON reports the cleared probe as ``advisor: null``."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))
    app = create_app(service)

    response = await _put_config(app, {"advisor": {"model_slug": "openai/gpt-4.1-mini"}})

    assert response.status_code == 200

    assert (await _health_body(app))["advisor"] is None


@pytest.mark.asyncio
async def test_a_put_provider_change_invalidates_the_probe(
    store: RoastStore,
    config_file: Path,
) -> None:
    """Changing provider changes the dispatch identity even with one slug."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))

    response = await _put_config(create_app(service), {"advisor": {"provider": "openai"}})

    assert response.status_code == 200
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_put_endpoint_change_invalidates_the_probe(
    store: RoastStore,
    config_file: Path,
) -> None:
    """Changing the provider endpoint invalidates the contacted-dispatch claim."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))

    response = await _put_config(
        create_app(service), {"advisor": {"provider_base_url": "http://proxy.local/v1"}}
    )

    assert response.status_code == 200
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_cosmetic_base_url_put_keeps_the_probe(
    store: RoastStore,
    config_file: Path,
) -> None:
    """Normalised-equivalent endpoint spelling preserves the exact probe object."""
    service = RoastService(store, live_serve_mode=True)
    probe = _reachable_probe(service)
    service.set_advisor_health(probe)

    response = await _put_config(
        create_app(service),
        {"advisor": {"provider_base_url": "https://OPENROUTER.AI/api/v1/"}},
    )

    assert response.status_code == 200
    assert (await service.health()).advisor is probe


@pytest.mark.asyncio
async def test_an_api_key_env_change_invalidates_on_put(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An effective API-key environment-variable name change clears on PUT."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))
    monkeypatch.setenv("ROASTPILOT_ADVISOR__API_KEY_ENV", "ANOTHER_ADVISOR_KEY")

    response = await _put_config(create_app(service), {"advisor": {"prompt_version": "c11"}})

    assert response.status_code == 200
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_reasoning_effort_change_invalidates_on_put(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reasoning effort belongs to the provider dispatch identity."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))
    monkeypatch.setenv("ROASTPILOT_ADVISOR__REASONING_EFFORT", "high")

    response = await _put_config(create_app(service), {"advisor": {"prompt_version": "c11"}})

    assert response.status_code == 200
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_pinned_phase_slot_change_invalidates_on_put(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phase-specific advice slot change invalidates even with the base slug fixed."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))
    monkeypatch.setenv(
        "ROASTPILOT_ADVISOR__MODEL_SLUG_BY_PHASE", '{"development": "openai/gpt-4o-mini"}'
    )

    response = await _put_config(create_app(service), {"advisor": {"prompt_version": "c11"}})

    assert response.status_code == 200
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_base_slug_put_invalidates_even_with_a_pinned_phase_slot(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A base-slug change clears even when a pinned advice slot is unchanged."""
    monkeypatch.setenv(
        "ROASTPILOT_ADVISOR__MODEL_SLUG_BY_PHASE", '{"development": "openai/gpt-4o-mini"}'
    )
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}}
    )

    assert response.status_code == 200
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_an_advisory_paused_readout_survives_a_put(
    store: RoastStore,
    config_file: Path,
) -> None:
    """NOT_CONFIGURED records an absent advisor, not a contacted model."""
    service = RoastService(store, live_serve_mode=True)
    paused = AdvisorHealth(status=AdvisorHealthStatus.NOT_CONFIGURED)
    service.set_advisor_health(paused)

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}}
    )

    assert response.status_code == 200
    assert (await service.health()).advisor == paused


@pytest.mark.asyncio
async def test_a_model_less_unreachable_does_not_survive_a_put(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A model-less UNREACHABLE remains a stale contacted-provider claim."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(
        AdvisorHealth(status=AdvisorHealthStatus.UNREACHABLE, error="probe timed out")
    )

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}}
    )

    assert response.status_code == 200
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_controller_only_put_keeps_the_probe(
    store: RoastStore,
    config_file: Path,
) -> None:
    """The route delegates controller-only edits to the equal-identity helper path."""
    service = RoastService(store, live_serve_mode=True)
    probe = _reachable_probe(service)
    service.set_advisor_health(probe)

    response = await _put_config(
        create_app(service),
        {"controller": {"pre_first_crack_levers": {"heat_target_percent": 85}}},
    )

    assert response.status_code == 200
    assert (await service.health()).advisor is probe


@pytest.mark.asyncio
async def test_an_env_shadowed_advisor_put_keeps_the_probe(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved edit shadowed by a real env value leaves the effective dispatch equal."""
    monkeypatch.setenv("ROASTPILOT_ADVISOR__MODEL_SLUG", "openai/gpt-4.1-mini")
    service = RoastService(store, live_serve_mode=True)
    probe = _reachable_probe(service)
    service.set_advisor_health(probe)

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4o-mini"}}
    )

    assert response.status_code == 200
    assert (await service.health()).advisor is probe


@pytest.mark.asyncio
async def test_the_put_does_not_patch_the_running_config(
    store: RoastStore,
    config_file: Path,
) -> None:
    """D78 leaves service config unchanged until a next-roast reload."""
    service = RoastService(store, live_serve_mode=True)
    original_slug = service._config.advisor.model_slug  # pyright: ignore[reportPrivateUsage]
    service.set_advisor_health(_reachable_probe(service))

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}}
    )

    assert response.status_code == 200
    assert service._config.advisor.model_slug == original_slug  # pyright: ignore[reportPrivateUsage]
    await service.start_roast(RoastProfile(**_profile()))
    assert service._config.advisor.model_slug == "openai/gpt-4.1-mini"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_the_put_makes_no_network_call(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PUT invalidates only in-memory state and never re-probes the advisor."""
    advisor = FakeAdvisor()
    service = RoastService(store, advisor=advisor, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))

    def _unexpected_probe(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("PUT /api/config must not probe the advisor")

    async def _unexpected_healthcheck() -> AdvisorHealth:
        raise AssertionError("PUT /api/config must not healthcheck the advisor")

    monkeypatch.setattr("roastpilot_agent.live.probe_advisor_health", _unexpected_probe)
    monkeypatch.setattr(advisor, "healthcheck", _unexpected_healthcheck)

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}}
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_start_roast_still_invalidates_through_the_shared_helper(
    store: RoastStore,
    config_file: Path,
) -> None:
    """The retained roast-start trigger clears when only the base slug moved."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))
    persist_config_edit(AppConfigEdit(advisor=AdvisorConfigEdit(model_slug="openai/gpt-4.1-mini")))

    await service.start_roast(RoastProfile(**_profile()))

    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_rejected_put_keeps_the_probe(
    store: RoastStore,
    config_file: Path,
) -> None:
    """A cross-field persistence rejection leaves the previous probe untouched."""
    service = RoastService(store, live_serve_mode=True)
    probe = _reachable_probe(service)
    service.set_advisor_health(probe)
    app = create_app(service)
    setup = {
        "controller": {
            "pre_first_crack_levers": {
                "late_maillard_trim": {"min_trim": 30, "base_trim": 40, "max_trim": 50}
            }
        }
    }
    assert (await _put_config(app, setup)).status_code == 200

    rejected = await _put_config(
        app,
        {"controller": {"pre_first_crack_levers": {"late_maillard_trim": {"min_trim": 70}}}},
    )

    assert rejected.status_code == 422
    assert (await service.health()).advisor is probe


@pytest.mark.asyncio
async def test_a_failed_persist_keeps_the_probe(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No invalidation runs if persistence fails before effective reload."""
    service = RoastService(store, live_serve_mode=True)
    probe = _reachable_probe(service)
    service.set_advisor_health(probe)

    def _fail_persist(edit: AppConfigEdit) -> None:
        raise ConfigFileError("persist failed")

    monkeypatch.setattr(api_module, "persist_config_edit", _fail_persist)

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}}
    )

    assert response.status_code == 500
    assert (await service.health()).advisor is probe


@pytest.mark.asyncio
async def test_a_failed_post_write_reload_clears_the_probe(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful write with unknown effective state clears a stale probe."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))

    def _fail_reload() -> tuple[AppConfig, frozenset[str]]:
        raise ConfigFileError("reload failed")

    monkeypatch.setattr(api_module, "load_app_config", _fail_reload)

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}}
    )

    assert response.status_code == 500
    assert "openai/gpt-4.1-mini" in config_file.read_text()
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_failed_post_write_reload_clears_an_advisory_paused_probe(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown post-write state clears NOT_CONFIGURED as well as contacted probes."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(AdvisorHealth(status=AdvisorHealthStatus.NOT_CONFIGURED))

    def _fail_reload() -> tuple[AppConfig, frozenset[str]]:
        raise ConfigFileError("reload failed")

    monkeypatch.setattr(api_module, "load_app_config", _fail_reload)

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}}
    )

    assert response.status_code == 500
    assert "openai/gpt-4.1-mini" in config_file.read_text()
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_failed_post_write_validation_clears_the_probe(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validation failure after persistence leaves no affirmative probe state."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))

    def _fail_reload() -> tuple[AppConfig, frozenset[str]]:
        raise ValidationError.from_exception_data(
            "ReloadError", [{"type": "missing", "loc": ("advisor",), "input": {}}]
        )

    monkeypatch.setattr(api_module, "load_app_config", _fail_reload)

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}}
    )

    assert response.status_code == 500
    assert "openai/gpt-4.1-mini" in config_file.read_text()
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_failed_post_write_os_error_clears_the_probe(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An I/O failure after persistence leaves no affirmative probe state."""
    service = RoastService(store, live_serve_mode=True)
    service.set_advisor_health(_reachable_probe(service))

    def _fail_reload() -> tuple[AppConfig, frozenset[str]]:
        raise OSError("reload failed")

    monkeypatch.setattr(api_module, "load_app_config", _fail_reload)

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}}
    )

    assert response.status_code == 500
    assert "openai/gpt-4.1-mini" in config_file.read_text()
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_put_without_an_attached_service_still_succeeds(config_file: Path) -> None:
    """The API-only scaffold keeps config PUT available without a service."""
    response = await _put_config(create_app(), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_a_put_with_a_non_roastservice_state_object_is_ignored(config_file: Path) -> None:
    """A non-service state value is tolerated rather than treated as a dependency."""
    app = create_app()
    app.state.service = object()

    response = await _put_config(app, {"advisor": {"model_slug": "openai/gpt-4.1-mini"}})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_an_unprobed_service_survives_a_put(
    store: RoastStore,
    config_file: Path,
) -> None:
    """An unprobed service short-circuits safely during a successful PUT."""
    service = RoastService(store, live_serve_mode=True)

    response = await _put_config(
        create_app(service), {"advisor": {"model_slug": "openai/gpt-4.1-mini"}}
    )

    assert response.status_code == 200
    assert (await service.health()).advisor is None


@pytest.mark.asyncio
async def test_a_refused_start_keeps_the_probe(
    store: RoastStore,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation of the reloaded config runs before start-path invalidation."""
    doctrine = AmbientFanDoctrine(enabled=True, max_reading_age_seconds=90.0)
    initial = AppConfig(
        controller=ControllerConfig(ambient_fan_doctrine=doctrine),
        mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=30.0),
    )
    refused = AppConfig(
        advisor=AdvisorConfig(model_slug="openai/gpt-4.1-mini"),
        controller=ControllerConfig(ambient_fan_doctrine=doctrine),
        mcp_device=MCPDeviceConfig(),
    )
    service = RoastService(store, config=initial, live_serve_mode=True)
    probe = _reachable_probe(service)
    service.set_advisor_health(probe)

    def _load_refused() -> tuple[AppConfig, frozenset[str]]:
        return refused, frozenset()

    monkeypatch.setattr(api_module, "load_app_config", _load_refused)

    with pytest.raises(api_module.RoastConfigError, match="ambient_poll_interval_seconds"):
        await service.start_roast(RoastProfile(**_profile()))

    assert (await service.health()).advisor is probe


# ---------------------------------------------------------------------------
# Gate: live_serve_mode=False (default) never reloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_service_does_not_reload_on_start_roast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default RoastService (live_serve_mode=False) does not reload on start_roast.

    Verifies that the reload path is gated correctly: a caller that does not
    pass ``live_serve_mode=True`` keeps its injected config/advisor untouched
    even when a saved config file with a different value is present.  Guards the
    gate against regression — if the guard is accidentally removed, a test
    double's injected config would be silently replaced.
    """
    from roastpilot_agent.config import AdvisorConfig, AppConfig

    # Wire an explicit config with a distinctive model slug — this simulates a
    # test double or replay caller injecting its own config.
    sentinel_slug = "sentinel/injected-model"
    injected_config = AppConfig(advisor=AdvisorConfig(model_slug=sentinel_slug))

    # Write a saved config file with a different slug that would be picked up
    # by reload if the gate were missing.
    import yaml

    config_file = tmp_path / "roastpilot-config.yaml"
    config_file.write_text(
        yaml.safe_dump({"advisor": {"model_slug": "openai/should-not-appear"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ROASTPILOT_CONFIG_FILE", str(config_file))

    db = RoastStore(tmp_path / "test.db")
    await db.initialize()
    try:
        # live_serve_mode defaults to False — reload must be skipped.
        svc = RoastService(db, config=injected_config)
        await svc.start_roast(RoastProfile(**_profile()))

        # The injected sentinel slug must be unchanged.
        assert svc._config.advisor.model_slug == sentinel_slug  # pyright: ignore[reportPrivateUsage]
    finally:
        await db.close()
