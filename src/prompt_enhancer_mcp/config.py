import os
import logging
import httpx
from mcp.server.fastmcp import Context

logger = logging.getLogger("mcp.config")


def get_config(project_id: str = None) -> tuple[str, str, str]:
    """Return (project_id, api_url, api_key) from args/env."""
    pid = project_id or os.environ.get("PROJECT_ID")
    url = os.environ.get("API_URL", "http://localhost:8000")
    key = os.environ.get("API_KEY")
    return pid, url, key


def validate_config(project_id: str, api_key: str) -> str | None:
    """Return error string if config is invalid, None if OK."""
    if not project_id or not api_key:
        return "Error: PROJECT_ID and API_KEY must be set (via env var or argument)."
    return None


def get_tech_stack() -> tuple[str, str]:
    """Return (frontend_requirements, backend_requirements) from env."""
    frontend = os.environ.get("FRONTEND_REQUIREMENTS", "")
    backend = os.environ.get("BACKEND_REQUIREMENTS", "")
    return frontend, backend


def get_client(ctx: Context) -> httpx.AsyncClient:
    """Get the shared httpx client from lifespan context."""
    return ctx.request_context.lifespan_context["client"]
