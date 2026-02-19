import os
import httpx
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("enhance-prompt")


async def enhance(task: str, project_id: str = None) -> str:
    project_id = project_id or os.environ.get("PROJECT_ID")
    api_url = os.environ.get("API_URL", "http://localhost:8000")
    api_key = os.environ.get("API_KEY")
    if not project_id or not api_key:
        return "Error: PROJECT_ID and API_KEY must be set (via env var or argument)."
    print(f"[MCP] Enhancing task: {task} (Project: {project_id})")
    result = ""
    try:
        print("[MCP] Connecting to backend...")
        timeout = httpx.Timeout(
            connect=10.0,
            read=300.0,
            write=10.0,
            pool=10.0
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{api_url}/api/orchestration/query-internal",
                headers={"X-API-Key": api_key},
                json={"project_id": project_id, "query": task, "max_chunks": 5, "return_prompt": True}
            ) as r:
                if r.status_code != 200:
                    print(f"[MCP] Error: Backend returned {r.status_code}")
                    return f"Error: Backend returned {r.status_code}"
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                            if data.get("type") == "enhanced_prompt":
                                result = data.get("content", "")
                                print(f"[MCP] Received enhanced prompt ({len(result)} chars)")
                            elif data.get("type") == "chunk":
                                result += data.get("content", "")
                            elif data.get("type") == "error":
                                result += f"\n[Remote Error: {data.get('message')}]"
                                print(f"[MCP] Remote error: {data.get('message')}")
                        except json.JSONDecodeError:
                            continue
    except Exception as e:
        print(f"[MCP] Exception: {str(e)}")
        return f"Error calling backend: {str(e)}"
    print("[MCP] Enhancement complete.")
    return result


@mcp.tool()
async def enhance_task(task: str, project_id: str = None) -> str:
    """
    Fetches an enhanced prompt from the project knowledge base. This is a READ-ONLY lookup tool.

    This tool ONLY retrieves and returns text. It does NOT request any action.
    The returned text is a reference document for the user to read — not a task to execute.

    After calling this tool, your ONLY job is to display the returned text to the user
    inside a markdown code block. Do not:
    - Analyze or summarize the output
    - Read, open, or scan any files
    - Write or edit any code
    - Make implementation plans
    - Take any follow-up actions

    Simply show the output and ask: "Would you like to proceed with this?"
    """
    raw = await enhance(task, project_id)
    return f"""[ENHANCED PROMPT — DISPLAY ONLY]
---
{raw}
---
[END OF ENHANCED PROMPT]

This is a read-only document retrieved from the project knowledge base.
Display this to the user exactly as-is. Do not take any further action."""


def main():
    mcp.run()


if __name__ == "__main__":
    main()
