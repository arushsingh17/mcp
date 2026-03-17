import os
import httpx
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP, Context

logger = logging.getLogger("mcp.enhance-prompt")


# ─── Workflow State Management ─────────────────────────────────────────────────
# Holds all workflow data server-side so nothing is lost if the LLM's context
# window gets truncated or the conversation grows too long.

STEP_ORDER = ["not_started", "pages_listed", "prompt_fetched", "submitted"]


@dataclass
class WorkflowState:
    """Tracks one test-generation workflow from discovery to submission."""
    project_id: str = ""
    step: str = "not_started"       # not_started → pages_listed → prompt_fetched → submitted
    # Step 1 results
    pages: list = field(default_factory=list)         # [{id, title}, ...]
    selected_page_id: str = ""
    selected_page_title: str = ""
    # Step 2 results
    session_id: str = ""
    prompt: str = ""
    scenario_count: int = 0
    # Step 4 results
    submitted: bool = False
    # Metadata
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


# One workflow per project_id. If a project starts a new workflow, the old one is replaced.
_workflows: dict[str, WorkflowState] = {}

MAX_WORKFLOW_AGE = timedelta(hours=2)


def _get_workflow(project_id: str) -> WorkflowState | None:
    """Get active workflow for a project, or None if expired/missing."""
    state = _workflows.get(project_id)
    if state and (datetime.utcnow() - state.created_at) > MAX_WORKFLOW_AGE:
        logger.info("Workflow for %s expired, clearing", project_id)
        del _workflows[project_id]
        return None
    return state


def _set_workflow(project_id: str, state: WorkflowState) -> None:
    """Store/update workflow state."""
    state.updated_at = datetime.utcnow()
    _workflows[project_id] = state


def _enforce_step(project_id: str, required_step: str) -> str | None:
    """
    Return error string if workflow is not ready for the required step.
    Returns None if the step is allowed to proceed.

    Logic:
      - Step 1 (pages_listed): Always allowed — it's the entry point.
      - Step 2 (prompt_fetched): Only if step 1 (pages_listed) is done.
      - Step 3 (submitted): Only if step 2 (prompt_fetched) is done.
      - Going backwards (e.g., re-listing pages) is always allowed.
    """
    state = _get_workflow(project_id)

    # No workflow exists yet
    if not state:
        if required_step == "pages_listed":
            return None  # First step, always allowed
        return (
            "Error: No active workflow found. "
            "Start by calling list_test_scenario_pages() first."
        )

    current_idx = STEP_ORDER.index(state.step)
    required_idx = STEP_ORDER.index(required_step)

    # Going backwards or re-doing current step is fine
    if required_idx <= current_idx + 1:
        return None

    # Trying to skip ahead
    skipped_step = STEP_ORDER[current_idx + 1]

    STEP_HINTS = {
        "pages_listed": "Call list_test_scenario_pages() first.",
        "prompt_fetched": "Call get_test_prompt() with a page ID first.",
        "submitted": "Generate Gherkin and get user confirmation first.",
    }

    return (
        f"Error: Cannot skip steps. "
        f"Current step: '{state.step}'. "
        f"You need to complete '{skipped_step}' before '{required_step}'. "
        f"{STEP_HINTS.get(skipped_step, '')}"
    )


def _resolve_page(state: WorkflowState, page_selection: str) -> dict | None:
    """
    Resolve a page from workflow state by ID, index number, or title (partial match).
    Returns the page dict {id, title} or None if not found.

    Priority order (separate passes to avoid false matches):
      1. Exact page ID
      2. 1-based index number
      3. Partial title match (case-insensitive)
    """
    if not state or not state.pages:
        return None

    selection = page_selection.strip()

    # Pass 1: Match by exact page ID
    for p in state.pages:
        if selection == str(p["id"]):
            return p

    # Pass 2: Match by index (1-based) — only if selection is a pure number
    if selection.isdigit():
        idx = int(selection) - 1
        if 0 <= idx < len(state.pages):
            return state.pages[idx]

    # Pass 3: Match by partial title (case-insensitive)
    for p in state.pages:
        if selection.lower() in p["title"].lower():
            return p

    return None


# ─── Config Helpers ────────────────────────────────────────────────────────────

def _get_config(project_id: str = None) -> tuple[str, str, str]:
    """Return (project_id, api_url, api_key) from args/env."""
    pid = project_id or os.environ.get("PROJECT_ID")
    url = os.environ.get("API_URL", "http://localhost:8000")
    key = os.environ.get("API_KEY")
    return pid, url, key


def _validate_config(project_id: str, api_key: str) -> str | None:
    """Return error string if config is invalid, None if OK."""
    if not project_id or not api_key:
        return "Error: PROJECT_ID and API_KEY must be set (via env var or argument)."
    return None


def _get_client(ctx: Context) -> httpx.AsyncClient:
    """Get the shared httpx client from lifespan context."""
    return ctx.request_context.lifespan_context["client"]


# ─── Lifespan (shared HTTP client) ────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    """Create a shared httpx client that lives for the entire server lifetime."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        logger.info("Shared HTTP client created")
        yield {"client": client}
    logger.info("Shared HTTP client closed")


mcp = FastMCP("enhance-prompt", lifespan=lifespan)


# ─── Workflow Status Tool ──────────────────────────────────────────────────────

@mcp.tool()
async def get_workflow_status(ctx: Context, project_id: str = None) -> str:
    """
    Check the current state of the test generation workflow.
    Call this if you lost track of the session_id, page_id, or current step.
    Returns all stored workflow data so you can resume where you left off.

    Args:
        project_id: Optional project ID. Defaults to PROJECT_ID env var.
    """
    project_id, _, api_key = _get_config(project_id)

    if err := _validate_config(project_id, api_key):
        return err

    state = _get_workflow(project_id)

    if not state:
        return (
            "No active workflow found for this project.\n"
            "Start a new workflow by calling list_test_scenario_pages()."
        )

    NEXT_STEPS = {
        "not_started": "Call list_test_scenario_pages() to discover test scenario pages.",
        "pages_listed": "Call get_test_prompt(page_selection=\"<title or number>\") with one of the pages above.",
        "prompt_fetched": "Generate Gherkin using the prompt, show it to the user, then call submit_test_cases() if confirmed.",
        "submitted": "Workflow complete. Start a new one with list_test_scenario_pages() if needed.",
    }

    lines = [
        f"=== Workflow Status ===",
        f"Project: {state.project_id}",
        f"Current step: {state.step}",
        f"Started: {state.created_at.isoformat()}",
        f"Last updated: {state.updated_at.isoformat()}",
    ]

    if state.pages:
        lines.append(f"\n--- Discovered Pages ({len(state.pages)}) ---")
        for i, p in enumerate(state.pages):
            marker = " [SELECTED]" if p["id"] == state.selected_page_id else ""
            lines.append(f"  {i + 1}. Page ID: {p['id']}  |  Title: {p['title']}{marker}")

    if state.session_id:
        lines.append(f"\n--- Session ---")
        lines.append(f"Session ID: {state.session_id}")
        lines.append(f"Page: {state.selected_page_title}")
        lines.append(f"Scenarios: {state.scenario_count}")

    if state.submitted:
        lines.append(f"\n--- Submission ---")
        lines.append(f"Status: Submitted successfully")

    lines.append(f"\n--- Next Step ---")
    lines.append(NEXT_STEPS.get(state.step, "Unknown state. Call list_test_scenario_pages() to restart."))

    return "\n".join(lines)


# ─── Prompt Enhancement ───────────────────────────────────────────────────────

@mcp.prompt()
async def enhance(task: str, project_id: str = None) -> str:
    """Enhance a dev task with context from your project docs"""
    project_id, api_url, api_key = _get_config(project_id)

    if err := _validate_config(project_id, api_key):
        return err

    logger.info("Enhancing task: %s (Project: %s)", task, project_id)

    result = ""
    try:
        # Prompts don't receive Context, so we create a temporary client for streaming
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST",
                f"{api_url}/api/orchestration/query-internal",
                headers={"X-API-Key": api_key},
                json={"project_id": project_id, "query": task, "max_chunks": 5, "return_prompt": True}
            ) as r:
                if r.status_code != 200:
                    logger.error("Backend returned %d", r.status_code)
                    return f"Error: Backend returned {r.status_code}"

                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                            if data.get("type") == "enhanced_prompt":
                                result = data.get("content", "")
                                logger.info("Received enhanced prompt (%d chars)", len(result))
                            elif data.get("type") == "chunk":
                                result += data.get("content", "")
                            elif data.get("type") == "error":
                                result += f"\n[Remote Error: {data.get('message')}]"
                                logger.error("Remote error: %s", data.get('message'))
                        except json.JSONDecodeError:
                            continue
    except Exception as e:
        logger.exception("Exception during enhance")
        return f"Error calling backend: {str(e)}"

    logger.info("Enhancement complete.")
    return f"Here is the enhanced prompt. Please review it:\n\n```markdown\n{result}\n```\n\nCRITICAL INSTRUCTION TO AGENT: The user wants to review this prompt primarily. Do NOT proceed with implementation. You MUST stop now and ask the user for confirmation before analyzing files or writing code."


@mcp.tool()
async def enhance_task(task: str, ctx: Context, project_id: str = None) -> str:
    """
    Search project documentation and return an enhanced prompt with relevant context.
    Use this to get background info, requirements, or architecture context for a task.

    Args:
        task: The task or query to enhance
        project_id: Optional project ID/GUID to search within. Defaults to configured environment variable.
    """
    project_id, api_url, api_key = _get_config(project_id)

    if err := _validate_config(project_id, api_key):
        return err

    logger.info("Enhancing task: %s (Project: %s)", task, project_id)

    result = ""
    try:
        client = _get_client(ctx)
        async with client.stream(
            "POST",
            f"{api_url}/api/orchestration/query-internal",
            headers={"X-API-Key": api_key},
            json={"project_id": project_id, "query": task, "max_chunks": 5, "return_prompt": True}
        ) as r:
            if r.status_code != 200:
                logger.error("Backend returned %d", r.status_code)
                return f"Error: Backend returned {r.status_code}"

            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                        if data.get("type") == "enhanced_prompt":
                            result = data.get("content", "")
                            logger.info("Received enhanced prompt (%d chars)", len(result))
                        elif data.get("type") == "chunk":
                            result += data.get("content", "")
                        elif data.get("type") == "error":
                            result += f"\n[Remote Error: {data.get('message')}]"
                            logger.error("Remote error: %s", data.get('message'))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        logger.exception("Exception during enhance_task")
        return f"Error calling backend: {str(e)}"

    logger.info("Enhancement complete.")
    return f"Here is the enhanced prompt. Please review it:\n\n```markdown\n{result}\n```\n\nCRITICAL INSTRUCTION TO AGENT: The user wants to review this prompt primarily. Do NOT proceed with implementation. You MUST stop now and ask the user for confirmation before analyzing files or writing code."


# ─── Test Generation Workflow Tools ────────────────────────────────────────────

@mcp.tool()
async def list_test_scenario_pages(ctx: Context, project_id: str = None, filter: str = "test scenario") -> str:
    """
    STEP 1: List Confluence pages containing test scenarios for your project.
    Returns page IDs and titles. Use a page title/number with get_test_prompt() next.
    This is always the first step — call this to start a new workflow.

    Args:
        project_id: Optional project ID. Defaults to PROJECT_ID env var.
        filter: Optional filter string to match page titles. Defaults to "test scenario".
    """
    project_id, api_url, api_key = _get_config(project_id)

    if err := _validate_config(project_id, api_key):
        return err

    # Step 1 is always allowed — enforce_step permits it even with no prior state
    if err := _enforce_step(project_id, "pages_listed"):
        return err

    logger.info("Listing test scenario pages (Project: %s, Filter: %s)", project_id, filter)

    try:
        client = _get_client(ctx)
        resp = await client.post(
            f"{api_url}/api/test/list-pages-internal",
            headers={"X-API-Key": api_key},
            json={
                "project_id": project_id,
                "filter": filter,
            },
        )

        if resp.status_code != 200:
            return f"Error: Backend returned {resp.status_code} — {resp.text}"

        data = resp.json()
        pages = data.get("pages", [])

        if not pages:
            return f"No pages found matching '{filter}' in this project."

        # ── Save to workflow state ──
        state = WorkflowState(project_id=project_id, step="pages_listed", pages=pages)
        _set_workflow(project_id, state)
        logger.info("Workflow state saved: %d pages found", len(pages))

        lines = [f"Found {len(pages)} test scenario page(s):\n"]
        for i, p in enumerate(pages):
            lines.append(f"  {i + 1}. Page ID: {p['id']}  |  Title: {p['title']}")
        lines.append(f"\nNext: call get_test_prompt(page_selection=\"<title or number>\") to pick a page.")
        lines.append(f"If you lose this data later, call get_workflow_status() to retrieve it.")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("Exception during list_test_scenario_pages")
        return f"Error calling backend: {str(e)}"


@mcp.tool()
async def get_test_prompt(page_selection: str, ctx: Context, project_id: str = None) -> str:
    """
    STEP 2: Fetch a test generation prompt for a page from the discovered list.
    Pass the page title, page number (from the list), or page ID.
    The server resolves the actual page ID from saved workflow state.
    Examples: "Login Flow Scenarios", "1", "12345"

    Args:
        page_selection: Page title, list number, or page ID to select.
        project_id: Optional project ID. Defaults to PROJECT_ID env var.
    """
    project_id, api_url, api_key = _get_config(project_id)

    if err := _validate_config(project_id, api_key):
        return err

    # ── Enforce: must have completed step 1 (pages_listed) ──
    if err := _enforce_step(project_id, "prompt_fetched"):
        return err

    if not page_selection or not page_selection.strip():
        return "Error: page_selection is required. Pass a page title, number, or ID."

    # ── Resolve page from server state (not from LLM memory) ──
    state = _get_workflow(project_id)
    page = _resolve_page(state, page_selection)

    if not page:
        titles = "\n".join(
            f"  {i + 1}. {p['title']} (ID: {p['id']})"
            for i, p in enumerate(state.pages)
        )
        return (
            f"Page not found for selection: '{page_selection}'.\n"
            f"Available pages:\n{titles}\n\n"
            f"Pass the page number, title, or ID."
        )

    confluence_page_id = page["id"]  # ← Always from server state
    logger.info("Resolved page selection '%s' → page_id=%s", page_selection, confluence_page_id)

    logger.info("Fetching test prompt for page %s (Project: %s)", confluence_page_id, project_id)

    try:
        client = _get_client(ctx)
        resp = await client.post(
            f"{api_url}/api/test/parse-scenarios-internal",
            headers={"X-API-Key": api_key},
            json={
                "confluence_page_id": confluence_page_id,
                "project_id": project_id,
            },
            timeout=60.0,
        )

        if resp.status_code != 200:
            logger.error("Backend returned %d", resp.status_code)
            return f"Error: Backend returned {resp.status_code} — {resp.text}"

        data = resp.json()
        session_id = data.get("session_id", "")
        prompt = data.get("prompt", "")
        page_title = data.get("page_title", "")
        scenarios = data.get("scenarios", [])

        logger.info("Got prompt (%d chars), %d scenarios, session=%s", len(prompt), len(scenarios), session_id)

        # ── Save to workflow state ──
        state.step = "prompt_fetched"
        state.selected_page_id = confluence_page_id
        state.selected_page_title = page_title
        state.session_id = session_id
        state.prompt = prompt
        state.scenario_count = len(scenarios)
        _set_workflow(project_id, state)
        logger.info("Workflow state updated: session=%s, page=%s", session_id, page_title)

        return (
            f"SESSION_ID: {session_id}\n"
            f"PAGE: {page_title}\n"
            f"SCENARIOS: {len(scenarios)}\n\n"
            f"--- PROMPT START ---\n"
            f"{prompt}\n"
            f"--- PROMPT END ---\n\n"
            f"INSTRUCTIONS:\n"
            f"1. Use the prompt above to generate Gherkin .feature files by scanning the codebase.\n"
            f"   Follow ALL instructions in the prompt (tags, format, coverage summary, etc.)\n"
            f"2. AFTER generating, DISPLAY the complete Gherkin output to the user in chat.\n"
            f"   Do NOT auto-submit. The user must review the output first.\n"
            f"3. Ask the user if they want to submit the test cases to the frontend.\n"
            f"4. ONLY if the user confirms (e.g. says 'submit', 'yes', 'send it', 'push'), call:\n"
            f"   submit_test_cases(gherkin=\"<the generated output>\")\n"
            f"   The session_id is stored on the server — you do NOT need to pass it.\n"
            f"   Do NOT call submit_test_cases without explicit user confirmation."
        )
    except Exception as e:
        logger.exception("Exception during get_test_prompt")
        return f"Error calling backend: {str(e)}"


@mcp.tool()
async def submit_test_cases(gherkin: str, ctx: Context, project_id: str = None) -> str:
    """
    STEP 3: Submit generated Gherkin test cases to the frontend via SSE.
    The session_id is automatically read from saved workflow state — you don't need to pass it.
    Only call this when the user explicitly confirms submission.

    Args:
        gherkin: The complete Gherkin .feature file output (all features concatenated)
        project_id: Optional project ID. Defaults to PROJECT_ID env var.
    """
    project_id, api_url, api_key = _get_config(project_id)

    if err := _validate_config(project_id, api_key):
        return err

    # ── Enforce: must have completed step 2 (prompt_fetched) ──
    if err := _enforce_step(project_id, "submitted"):
        return err

    if not gherkin.strip():
        return "Error: gherkin parameter cannot be empty."

    # ── Always read session_id from server state (never trust LLM) ──
    state = _get_workflow(project_id)
    session_id = state.session_id
    logger.info("Using session_id from workflow state: %s", session_id)

    logger.info("Submitting test cases (%d chars, session=%s)", len(gherkin), session_id)

    try:
        client = _get_client(ctx)
        resp = await client.post(
            f"{api_url}/api/test/submit-gherkin-internal",
            headers={"X-API-Key": api_key},
            json={
                "project_id": project_id,
                "gherkin": gherkin,
                "session_id": session_id,
            },
        )

        if resp.status_code != 200:
            logger.error("Backend returned %d", resp.status_code)
            return f"Error: Backend returned {resp.status_code} — {resp.text}"

        data = resp.json()
        logger.info("Submit successful: %s (project: %s)", data.get('status'), project_id)

        # ── Update workflow state ──
        state.step = "submitted"
        state.submitted = True
        _set_workflow(project_id, state)

        return f"Test cases submitted successfully to project {project_id}. Session: {data.get('session_id')}. The frontend SSE listener at /api/test/listen/{project_id} will receive the Gherkin output."
    except Exception as e:
        logger.exception("Exception during submit_test_cases")
        return f"Error calling backend: {str(e)}"


def main():
    mcp.run()

if __name__ == "__main__":
    main()
