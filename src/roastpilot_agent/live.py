"""Live-serve composition (E11-S1 early item; supervised hardware roast #134).

The minimal entrypoint that drives a *live* roast: it spawns the
``coffee-roaster-mcp`` child, wires the real :class:`RoasterControlAdapter`
into a :class:`RoastService`, and serves the REST + SSE surface (and the built
SPA) the operator drives. It is the live-hardware twin of the ``--replay``
harness in :mod:`roastpilot_agent.replay` and mirrors its
``build_replay_service`` factory.

Invariants this module is bound by (AGENTS.md):

- It composes only — it never reimplements safety, controller transitions, or
  any enum. Every roaster write still flows controller → ``SafetyPolicy`` →
  ``RoasterControlAdapter`` → MCP; the advisor never receives MCP write tools.
- The serve path uses the **recovery** :func:`roastpilot_agent.api._lifespan`,
  so an agent restart over a possibly-active run enters
  ``operator_recovery_required`` and never auto-resumes heat or fan.
- **Fail closed**: if the MCP child fails to start, the half-started child is
  cleaned up and a clear error propagates — no service is returned half-wired.
- Temperatures are Celsius end to end; the SPA renders from server events.
"""

import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from roastpilot_agent.advisor import PydanticAIAdvisor, RoastAdvisor
from roastpilot_agent.api import RoastService
from roastpilot_agent.config import AppConfig
from roastpilot_agent.mcp_client import (
    MCPServerProcess,
    RoasterControlAdapter,
    RoasterMCPClient,
)
from roastpilot_agent.models import AdvisorHealth, AdvisorHealthStatus
from roastpilot_agent.store import RoastStore

_log = logging.getLogger(__name__)

#: Prefix for Hottop/coffee-roaster-mcp environment variables forwarded from the
#: operator's shell into the spawned child (``COFFEE_ROASTER_DRIVER``,
#: ``COFFEE_ROASTER_PORT``, ``COFFEE_ROASTER_MCP_CONFIG``, …). Forwarding lets the
#: operator configure the Hottop with plain ``export COFFEE_…`` rather than the
#: nested ``ROASTPILOT_MCP__ENV__…`` form.
COFFEE_ENV_PREFIX = "COFFEE_"


def forward_coffee_env(config: AppConfig, environ: dict[str, str] | None = None) -> None:
    """Forward ``COFFEE_*`` vars from the environment into ``config.mcp.env``.

    The operator configures the live Hottop driver with plain ``export
    COFFEE_…`` in their shell; this copies those into the child-process env
    overrides so :meth:`MCPServerProcess.build_server_parameters` passes them to
    the spawned ``coffee-roaster-mcp``. Existing ``config.mcp.env`` entries take
    precedence — an explicit config value is never overwritten by the
    environment.

    Args:
        config: The application config whose ``mcp.env`` is populated in place.
        environ: Environment mapping to read from; defaults to ``os.environ``.
    """
    source = os.environ if environ is None else environ
    for key, value in source.items():
        if key.startswith(COFFEE_ENV_PREFIX) and key not in config.mcp.env:
            config.mcp.env[key] = value


def build_advisor(config: AppConfig) -> RoastAdvisor | None:
    """Build the live advisor from ``config.advisor`` (D5/D18), or ``None``.

    When the configured API-key env var is absent we log a clear warning and
    return ``None`` rather than blocking startup: the operator can still run the
    roast advisory-paused (the controller treats a missing advisor as no advice,
    never as an unsafe write). When the key is present the
    :class:`PydanticAIAdvisor` is constructed via the provider factory.

    Args:
        config: The application config carrying the advisor section.

    Returns:
        A wired advisor, or ``None`` when the API key is unset.
    """
    if os.environ.get(config.advisor.api_key_env):
        return PydanticAIAdvisor(config.advisor)
    _log.warning(
        "advisor API key env %r is unset — live advice will be unavailable; "
        "run the roast advisory-paused (the controller issues no advisory writes "
        "without an advisor)",
        config.advisor.api_key_env,
    )
    return None


#: Hard ceiling on the startup advisor probe (issue #168). The advisor's own
#: ``healthcheck`` is already bounded, but this is a belt-and-braces wrapper
#: bound so even a misbehaving advisor whose ``healthcheck`` hangs (or raises
#: outside its own guard) can never wedge ``serve`` startup. A small margin
#: over the default ``healthcheck_timeout_seconds`` so the inner bound trips
#: first under normal operation.
ADVISOR_PROBE_WRAP_TIMEOUT_SECONDS = 8.0


async def probe_advisor_health(advisor: RoastAdvisor | None) -> AdvisorHealth:
    """Probe advisor reachability for the startup readout (issue #168).

    Best-effort and **non-blocking**: it returns a typed
    :class:`~roastpilot_agent.models.AdvisorHealth` and never raises or stalls.
    A ``None`` advisor (no API key / advisory-paused) reports
    ``NOT_CONFIGURED``. Otherwise the advisor's own bounded
    :meth:`~roastpilot_agent.advisor.RoastAdvisor.healthcheck` is awaited under
    an outer :func:`asyncio.wait_for` ceiling
    (:data:`ADVISOR_PROBE_WRAP_TIMEOUT_SECONDS`) so even a ``healthcheck`` that
    hangs or raises cannot wedge or abort startup — the controller still runs
    deterministically without advice. The probe is advisory-only: it never
    touches MCP write tools.

    Args:
        advisor: The wired advisor, or ``None`` when none is configured.

    Returns:
        The reachability result: ``NOT_CONFIGURED``, ``REACHABLE``, or
        ``UNREACHABLE`` with the captured error.
    """
    if advisor is None:
        return AdvisorHealth(status=AdvisorHealthStatus.NOT_CONFIGURED)
    try:
        return await asyncio.wait_for(
            advisor.healthcheck(), timeout=ADVISOR_PROBE_WRAP_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return AdvisorHealth(
            status=AdvisorHealthStatus.UNREACHABLE,
            error=(
                f"reachability probe did not return within {ADVISOR_PROBE_WRAP_TIMEOUT_SECONDS:g}s"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — startup probe must never block serve
        return AdvisorHealth(status=AdvisorHealthStatus.UNREACHABLE, error=str(exc))


async def build_live_service(
    config: AppConfig,
    *,
    store_path: Path,
) -> tuple[RoastService, MCPServerProcess, RoastStore]:
    """Wire a live :class:`RoastService` over a spawned ``coffee-roaster-mcp``.

    The live twin of :func:`roastpilot_agent.replay.build_replay_service`: it
    spawns and health-checks the MCP child, wraps it in the
    :class:`RoasterControlAdapter` that satisfies the controller's read + write
    protocols, builds the advisor and store, and assembles the service the same
    way the milestone vertical slice does. The store is returned so the caller
    owns its lifecycle (``initialize`` before serving, ``close`` after); the MCP
    child is returned so the caller stops it on shutdown.

    Fail-closed: if :meth:`MCPServerProcess.start` raises, the half-started
    child is stopped before the error propagates — no half-wired service is
    ever returned.

    Args:
        config: The application config (controller timing, safety limits,
            advisor provider, and MCP child settings).
        store_path: Filesystem path for the SQLite roast store.

    Returns:
        The wired service, the running MCP child process, and the store.

    Raises:
        Exception: Whatever :meth:`MCPServerProcess.start` raises (typically
            :class:`~roastpilot_agent.mcp_client.MCPConnectionError`), after the
            child is cleaned up.
    """
    # D78-4 (#420): pass device_config so the managed fields are rendered into
    # the MCP yaml via passthrough-merge on each (re)spawn.  When mcp_device
    # is all-None the render step is a no-op (overlay = {}) and the child reads
    # its yaml directly, preserving the existing E9-S2 env-var-only path.
    mcp = MCPServerProcess(config.mcp, device_config=config.mcp_device)
    try:
        await mcp.start()
    except BaseException:
        # Fail closed: never leave a half-started child behind.
        await mcp.stop()
        raise

    adapter = RoasterControlAdapter(RoasterMCPClient(mcp.call_tool))
    advisor = build_advisor(config)
    store = RoastStore(store_path)
    service = RoastService(
        store,
        config=config,
        mcp=mcp,
        roaster=adapter,
        advisor=advisor,
        # Enable config+advisor reload at each start_roast (D78).  Only the live
        # serve path sets this — test doubles and replay inject explicit values
        # that must not be replaced on reload.
        live_serve_mode=True,
        exporter=adapter,
        raw_state=adapter,
    )
    # Record the device config the child was just spawned with so the
    # between-roast respawn path in start_roast can detect drift (#431).
    service.set_spawned_mcp_device(config.mcp_device)
    return service, mcp, store


def default_spa_dir() -> Path | None:
    """Resolve the built SPA directory: bundled package data, else source checkout.

    Tries two locations in order, returning the first that holds an
    ``index.html`` (otherwise ``None``, so the caller mounts nothing and serves
    API-only):

    1. **Bundled package data** — ``roastpilot_agent/_web_dist`` (E11-S1, #137).
       A standard wheel install force-includes the SPA built by ``npm run
       build`` at this path (see ``hatch_build.py``), so ``pip install
       roastpilot-agent`` serves the SPA with no extra flags.
    2. **Source-checkout** ``<repo-root>/web/dist`` — the SPA a developer built
       locally in a source checkout. This is also the path an *editable*
       install (``pip install -e .``) resolves, since the build hook does not
       run for editable installs.

    Returns:
        The resolved SPA directory holding an ``index.html``, else ``None``.
    """
    packaged = _packaged_spa_dir()
    if packaged is not None:
        return packaged

    # src/roastpilot_agent/live.py -> source-checkout repo root is three parents up.
    candidate = Path(__file__).resolve().parents[2] / "web" / "dist"
    if (candidate / "index.html").is_file():
        return candidate
    return None


def _packaged_spa_dir() -> Path | None:
    """Resolve the bundled ``roastpilot_agent/_web_dist`` package data, if present.

    Uses :mod:`importlib.resources` and requires the resolved resource to be a
    real filesystem path (:class:`pathlib.Path`), which is what pip installs
    are in practice — pip always extracts a wheel to a directory on disk, it
    never runs a package from inside the ``.whl`` zip. A hypothetical
    zip-importer install would need ``importlib.resources.as_file``'s
    extract-to-tempdir path instead; that is out of scope here (StaticFiles
    needs a stable, long-lived directory for the life of the app, not a
    context-managed extraction that could be cleaned up mid-request), so this
    falls back to ``None`` (source-checkout fallback in ``default_spa_dir``)
    rather than risk serving from a directory that could vanish underneath a
    request.

    Returns:
        The bundled ``_web_dist`` directory when it is a real path holding an
        ``index.html``, else ``None`` (no bundled SPA — e.g. an editable
        install, where the build hook never ran).
    """
    from importlib import resources

    try:
        traversable = resources.files("roastpilot_agent") / "_web_dist"
    except ModuleNotFoundError:  # pragma: no cover — package always importable here
        return None

    if not isinstance(traversable, Path):
        return None  # pragma: no cover — zip-importer install, not pip's behavior
    if (traversable / "index.html").is_file():
        return traversable
    return None


def _is_api_path(scope: Scope) -> bool:
    """Whether the request path is in the ``/api`` namespace.

    Reads the original request path from the ASGI scope (StaticFiles is mounted
    at ``/`` so the mount-relative ``path`` has the leading slash stripped; the
    raw scope path is the reliable source for the namespace check).
    """
    raw = scope.get("path", "")
    path = raw if isinstance(raw, str) else ""
    return path == "/api" or path.startswith("/api/")


class SpaStaticFiles(StaticFiles):
    """``StaticFiles`` that falls back to ``index.html`` for client-side routes.

    A real asset (``/assets/app.js``, ``/index.html``, ``/``) is served as
    usual; any other path with no file on disk (the SPA's client-side deep
    links — ``/history``, ``/roasts/<id>``) resolves to ``index.html`` so the
    browser router takes over, instead of a bare 404. Because this is mounted at
    ``/`` *after* the ``/api/*`` routes, an API path always matches its own
    route first — the SPA mount only ever sees non-API paths, so it cannot
    shadow or rewrite the API namespace.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Serve the asset, or ``index.html`` when the path is a client route."""
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404 and not _is_api_path(scope):
                # A missing file under the SPA mount is a client-side route, not
                # a server 404 — hand back the shell so the SPA router resolves
                # it. An unknown /api/* path is NOT rewritten: it stays a real
                # 404 so the SPA mount never shadows the API namespace.
                return await super().get_response("index.html", scope)
            raise


def mount_spa(app: FastAPI, spa_dir: Path) -> None:
    """Mount the built SPA at ``/`` with client-side-route fallback.

    Registered AFTER every ``/api/*`` route so it never shadows the API: a
    request to ``/api/...`` is matched by the API routes first and the SPA mount
    only sees non-API paths. Static assets are served from ``spa_dir``; any
    other GET falls back to ``index.html`` (see :class:`SpaStaticFiles`) so the
    browser's client-side router owns deep links. The SPA renders purely from
    the server's events/snapshots — it never calls MCP and never infers roast
    phase locally.

    Args:
        app: The FastAPI app whose ``/api/*`` routes are already registered.
        spa_dir: Directory holding the built SPA (must contain ``index.html``).
    """
    app.mount("/", SpaStaticFiles(directory=spa_dir, html=True), name="spa")
