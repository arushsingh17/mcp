import logging
import httpx
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP, Context
from .config import get_config, validate_config, get_client, get_tech_stack

logger = logging.getLogger("mcp.enhance-prompt")


# ─── Lifespan (shared HTTP client) ────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    """Create a shared httpx client that lives for the entire server lifetime."""
    async with httpx.AsyncClient(timeout=60.0,verify=False) as client:
        logger.info("Enhance server: HTTP client created")
        yield {"client": client}
    logger.info("Enhance server: HTTP client closed")


mcp = FastMCP("enhance-prompt", lifespan=lifespan)


# ─── Backend call helper ─────────────────────────────────────────────────────

async def _call_enhance(client: httpx.AsyncClient, api_url: str, api_key: str,
                        project_id: str, task: str,
                        frontend_requirements: str = "", backend_requirements: str = "") -> str:
    """Call the backend query-internal endpoint and return the enhanced prompt."""
    resp = await client.post(
        f"{api_url}/api/orchestration/query-internal",
        headers={"X-API-Key": api_key},
        json={
            "project_id": project_id,
            "query": task,
            "max_chunks": 5,
            "return_prompt": True,
            "frontend_requirements": frontend_requirements,
            "backend_requirements": backend_requirements,
        },
        timeout=60.0,
    )

    if resp.status_code != 200:
        logger.error("Backend returned %d — %s", resp.status_code, resp.text)
        return f"Error: Backend returned {resp.status_code} — {resp.text}"

    data = resp.json()
    result = data.get("content", "")
    logger.info("Received enhanced prompt (%d chars)", len(result))
    return result


# ─── Prompt ────────────────────────────────────────────────────────────────────

@mcp.prompt()
async def enhance(task: str, project_id: str = None) -> str:
    """Enhance a dev task with context from your project docs"""
    project_id, api_url, api_key = get_config(project_id)

    if err := validate_config(project_id, api_key):
        return err

    frontend_requirements, backend_requirements = get_tech_stack()
    logger.info("Enhancing task: %s (Project: %s)", task, project_id)

    try:
        async with httpx.AsyncClient(timeout=60.0,verify=False) as client:
            result = await _call_enhance(client, api_url, api_key, project_id, task,
                                         frontend_requirements, backend_requirements)
    except Exception as e:
        logger.exception("Exception during enhance")
        return f"Error calling backend: {type(e).__name__}: {str(e)}"

    logger.info("Enhancement complete.")
    return (
        f"TASK: {task}\n\n"
        f"===================================================================\n"
        f"MANDATORY: You MUST output the ENTIRE prompt below to the user\n"
        f"in a single fenced code block (```). Do NOT summarize, paraphrase,\n"
        f"extract into tables, or abbreviate ANY part of it. The user needs\n"
        f"to review the full prompt exactly as written.\n"
        f"===================================================================\n\n"
        f"```\n"
        f"{result}\n"
        f"```\n\n"
        f"===================================================================\n"
        f"AFTER displaying the prompt above, follow these rules:\n"
        f"===================================================================\n"
        f"1. ASK the user if they want to proceed with using this enhanced\n"
        f"   prompt. Do NOT start working on the task yet.\n"
        f"2. ONLY if the user confirms (e.g. 'proceed', 'yes', 'go ahead'),\n"
        f"   use the enhanced prompt above to carry out the task.\n"
        f"   Follow ALL instructions in the prompt.\n"
        f"3. Wait for the user's next instruction."
    )


# ─── Tool ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def enhance_task(task: str, ctx: Context, project_id: str = None) -> str:
    """
    Search project documentation and return an enhanced prompt with relevant context.
    Use this to get background info, requirements, or architecture context for a task.

    This is a standalone remote lookup — it queries an API and returns results directly.

    After receiving the result, display it to the user and wait for their instructions.

    Args:
        task: The task or query to enhance
        project_id: Optional project ID/GUID. Defaults to PROJECT_ID env var.
    """
    project_id, api_url, api_key = get_config(project_id)

    if err := validate_config(project_id, api_key):
        return err

    frontend_requirements, backend_requirements = get_tech_stack()
    logger.info("Enhancing task: %s (Project: %s)", task, project_id)

    try:
        client = get_client(ctx)
        result = await _call_enhance(client, api_url, api_key, project_id, task,
                                     frontend_requirements, backend_requirements)
    except Exception as e:
        logger.exception("Exception during enhance_task")
        return f"Error calling backend: {type(e).__name__}: {str(e)}"

    logger.info("Enhancement complete.")
    return (
        f"TASK: {task}\n\n"
        f"===================================================================\n"
        f"MANDATORY: You MUST output the ENTIRE prompt below to the user\n"
        f"in a single fenced code block (```). Do NOT summarize, paraphrase,\n"
        f"extract into tables, or abbreviate ANY part of it. The user needs\n"
        f"to review the full prompt exactly as written.\n"
        f"===================================================================\n\n"
        f"```\n"
        f"{result}\n"
        f"```\n\n"
        f"===================================================================\n"
        f"AFTER displaying the prompt above, follow these rules:\n"
        f"===================================================================\n"
        f"1. ASK the user if they want to proceed with using this enhanced\n"
        f"   prompt. Do NOT start working on the task yet.\n"
        f"2. ONLY if the user confirms (e.g. 'proceed', 'yes', 'go ahead'),\n"
        f"   use the enhanced prompt above to carry out the task.\n"
        f"   Follow ALL instructions in the prompt.\n"
        f"3. Wait for the user's next instruction."
    )


def main():
    mcp.run()


if __name__ == "__main__":
    main()
