"""FastAPI application: REST + SSE + static SPA mount (component plan §6).

App shell with the health route only; the full REST surface, SSE stream,
operator action queue, and ``web/dist`` static mount land in E7/E10. The
SPA renders from server events and snapshots — it never infers phase locally.
"""

from fastapi import FastAPI

from roastpilot_agent import __version__


async def health() -> dict[str, str]:
    """Liveness probe. MCP child status and active run id are added in E7."""
    return {"status": "ok", "version": __version__}


def create_app() -> FastAPI:
    """Create the FastAPI application shell."""
    app = FastAPI(title="roastpilot-agent", version=__version__)
    app.get("/api/health")(health)
    return app
