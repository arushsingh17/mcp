import re
import asyncio
import logging
import httpx
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP, Context
from .config import (
    get_config,
    validate_config,
    get_client,
    get_harness_config,
    validate_harness_config,
)
from . import harness_client as hc

logger = logging.getLogger("mcp.pipeline-analyzer")


# ─── Lifespan (shared HTTP client) ────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        logger.info("Pipeline analyzer: HTTP client created")
        yield {"client": client}
    logger.info("Pipeline analyzer: HTTP client closed")


mcp = FastMCP("pipeline-analyzer", lifespan=lifespan)


# ─── In-memory analysis cache ─────────────────────────────────────────────────

_analysis_cache: dict[str, str] = {}


# ─── Helpers ───────────────────────────────────────────────────────────────────

_EXEC_ID_RE = re.compile(r"/executions/([A-Za-z0-9_-]+)")


def _extract_execution_id(s: str) -> str:
    """Accept raw planExecutionId or full Harness UI URL."""
    if not s:
        return ""
    m = _EXEC_ID_RE.search(s)
    return m.group(1) if m else s.strip()


def _find_stage_for_step(execution_details: dict, step_node_id: str) -> str | None:
    """Walk adjacency to find the stage ancestor of a given step node."""
    graph = execution_details.get("executionGraph") or {}
    adjacency = graph.get("nodeAdjacencyListMap") or {}
    node_map = graph.get("nodeMap") or {}

    # Build parent_of mapping.  Children are explicitly nested; nodes
    # linked via nextIds from a child are siblings at the same nesting
    # level and therefore share the same parent.
    parent_of: dict[str, str] = {}
    for parent_id, adj in adjacency.items():
        if not isinstance(adj, dict):
            continue
        for child in (adj.get("children") or []):
            parent_of[child] = parent_id
            # Walk the nextIds chain — siblings belong to the same parent.
            queue = [child]
            visited = {child}
            while queue:
                cur = queue.pop(0)
                next_adj = adjacency.get(cur)
                if not isinstance(next_adj, dict):
                    continue
                for nid in (next_adj.get("nextIds") or []):
                    if nid and nid not in visited:
                        parent_of[nid] = parent_id
                        visited.add(nid)
                        queue.append(nid)

    current = step_node_id
    for _ in range(20):
        if current not in parent_of:
            return None
        current = parent_of[current]
        node = node_map.get(current) or {}
        step_type = (node.get("stepType") or "").upper()
        if "STAGE" in step_type:
            return node.get("identifier") or current
    return None


async def _call_rag(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
    project_id: str,
    query: str,
    limit: int = 5,
) -> dict | str:
    """POST {API_URL}/api/orchestration/query-internal — returns parsed JSON or error string."""
    try:
        resp = await client.post(
            f"{api_url}/api/orchestration/query-internal",
            headers={"X-API-Key": api_key},
            json={
                "project_id": project_id,
                "query": query,
                "max_chunks": limit,
                "return_prompt": True,
            },
            timeout=60.0,
        )
    except httpx.HTTPError as e:
        return f"Error: RAG request failed — {type(e).__name__}: {e}"

    if resp.status_code != 200:
        return f"Error: Backend returned {resp.status_code} — {resp.text[:200]}"

    return resp.json()


def _format_rag_results(rag_data: dict | str) -> str:
    if isinstance(rag_data, str):
        return rag_data
    results = rag_data.get("results") or rag_data.get("chunks") or []
    if not results:
        return "(no related incidents found)"
    lines = []
    for r in results:
        title = r.get("title") or r.get("ticket_id") or r.get("id") or "Result"
        summary = r.get("summary") or r.get("text") or r.get("content") or ""
        source = r.get("source") or r.get("source_type") or ""
        lines.append(f"#### {title}" + (f"  _(source: {source})_" if source else ""))
        if summary:
            lines.append(summary.strip())
        lines.append("")
    return "\n".join(lines).rstrip()


# ─── Tool 1: list_recent_failures ──────────────────────────────────────────────

@mcp.tool()
async def list_recent_failures(ctx: Context, limit: int = 10) -> str:
    """
    List the most recent failed Harness pipeline executions in this project.
    Use this to find an execution_id to pass to analyze_pipeline_failure.

    Args:
        limit: Max number of failed executions to return (default 10).
    """
    harness_cfg = get_harness_config()
    if err := validate_harness_config(harness_cfg):
        return err

    client = get_client(ctx)
    result = await hc.list_recent_executions(client, harness_cfg, status_filter="Failed", limit=limit)

    if isinstance(result, str):
        return result

    if not result:
        return "No failed executions found in the last 30 days."

    lines = [f"Found {len(result)} recent failed execution(s):\n"]
    for i, ex in enumerate(result, 1):
        name = ex.get("name") or ex.get("pipelineIdentifier") or "(unnamed)"
        exec_id = ex.get("planExecutionId", "?")
        status = ex.get("status", "?")
        start_ts = ex.get("startTs", "?")
        trigger = (ex.get("executionTriggerInfo") or {}).get("triggerType", "?")
        lines.append(
            f"  {i}. {name}\n"
            f"     Execution ID: {exec_id}\n"
            f"     Status: {status}  |  Trigger: {trigger}  |  Start: {start_ts}"
        )
    lines.append("\nNext: call analyze_pipeline_failure(execution_id=\"<id>\") to investigate.")
    return "\n".join(lines)


# ─── Tool 2: analyze_pipeline_failure ──────────────────────────────────────────

async def _build_analysis(
    client: httpx.AsyncClient,
    exec_id: str,
    include_logs: bool,
    include_rag: bool,
) -> str:
    """Core analysis logic — called by the tool and reused by document_resolved_incident."""
    harness_cfg = get_harness_config()
    if err := validate_harness_config(harness_cfg):
        return err

    project_id, api_url, api_key = get_config()
    if include_rag and (err := validate_config(project_id, api_key)):
        return "Error: PROJECT_ID and API_KEY must be set for RAG search (include_rag=False to skip)."

    details = await hc.get_execution_details(client, harness_cfg, exec_id)
    if isinstance(details, str):
        return details

    summary = details.get("pipelineExecutionSummary") or {}
    pipeline_name = summary.get("name") or summary.get("pipelineIdentifier") or "(unknown)"
    pipeline_id = summary.get("pipelineIdentifier", "")
    run_sequence = str(summary.get("runSequence", ""))
    status = summary.get("status", "?")
    trigger = (summary.get("executionTriggerInfo") or {}).get("triggerType", "?")
    start_ts = summary.get("startTs", "?")
    end_ts = summary.get("endTs", "?")

    # YAML precedence: (1) inline YAML on /execution/v2 response, (2) the
    # /execution/{id}/metadata endpoint's executionYaml (templates inlined,
    # <+...> expressions still literal), (3) committed pipeline definition.
    resolved_yaml = hc.extract_resolved_yaml(details)
    yaml_source: str | None = (
        "execution response (resolved, post-template-expansion)" if resolved_yaml else None
    )
    yaml_error: str | None = None

    if not resolved_yaml:
        metadata_yaml = await hc.get_execution_resolved_yaml(client, harness_cfg, exec_id)
        if metadata_yaml:
            resolved_yaml = metadata_yaml
            yaml_source = (
                "execution metadata (templates inlined; `<+...>` expressions "
                "remain literal — see per-step **Executed Step Parameters** for substituted values)"
            )

    if not resolved_yaml:
        if not pipeline_id:
            yaml_error = "execution response had no YAML field and pipelineIdentifier is missing"
        else:
            definition = await hc.get_pipeline_definition(client, harness_cfg, pipeline_id)
            if isinstance(definition, str):
                yaml_error = f"pipeline definition fetch failed — {definition}"
            else:
                resolved_yaml = hc.extract_pipeline_definition_yaml(definition)
                if resolved_yaml:
                    yaml_source = "pipeline definition (raw source, not template-expanded)"
                else:
                    yaml_error = "no YAML field in pipeline definition response"

    failed_steps = hc.parse_failed_steps(details)

    if not failed_steps:
        return (
            f"## Pipeline Execution {exec_id}\n\n"
            f"Execution status: {status}. No failed step nodes found in the execution graph."
        )

    # Step types that are structural containers — they never produce logs.
    skip_log_types = {
        "PIPELINE_SECTION", "STAGES_STEP", "NG_EXECUTION",
        "NG_SECTION", "NG_FORK", "STEP_GROUP",
        "IntegrationStageStepPMS", "DEPLOYMENT_STAGE_STEP",
    }

    # Build concurrent tasks: log fetches + single RAG call.
    log_tasks = []
    log_targets = []
    if include_logs and pipeline_id and run_sequence:
        for step in failed_steps:
            if step.get("step_type", "") in skip_log_types:
                continue
            stage_id = _find_stage_for_step(details, step["node_id"])
            step_identifier = step.get("identifier") or step["node_id"]
            if stage_id:
                log_tasks.append(
                    hc.download_step_logs(
                        client, harness_cfg, pipeline_id, run_sequence,
                        exec_id, stage_id, step_identifier,
                    )
                )
                log_targets.append(step)
            else:
                logger.warning(
                    "Could not resolve stage for step %s (node %s) — skipping log fetch",
                    step.get("name"), step["node_id"],
                )

    rag_task = None
    if include_rag:
        rag_query = " | ".join(
            f"{s.get('step_type', '')}: {s.get('failure_message', '')}".strip(": ")
            for s in failed_steps if s.get("failure_message")
        )[:1000] or pipeline_name
        rag_task = _call_rag(client, api_url, api_key, project_id, rag_query, limit=5)

    input_set_task = hc.get_execution_input_set(client, harness_cfg, exec_id)

    gathered = await asyncio.gather(
        *log_tasks,
        rag_task if rag_task is not None else asyncio.sleep(0, result=None),
        input_set_task,
        return_exceptions=True,
    )

    log_results = gathered[: len(log_tasks)]
    rag_result = gathered[len(log_tasks)] if rag_task is not None else None
    input_set_result = gathered[-1]
    input_set_yaml: str | None = (
        input_set_result if isinstance(input_set_result, str) and input_set_result.strip() else None
    )

    # Apply 500-line aggregate cap across all logs.
    capped_logs: dict[str, str] = {}
    remaining = 500
    for step, log in zip(log_targets, log_results):
        if isinstance(log, Exception):
            capped_logs[step["node_id"]] = f"(log fetch failed: {type(log).__name__}: {log})"
            continue
        lines = log.splitlines() if isinstance(log, str) else []
        take = lines[-200:] if len(lines) > 200 else lines
        if len(take) > remaining:
            take = take[-remaining:]
        remaining -= len(take)
        capped_logs[step["node_id"]] = "\n".join(take)

    capped_yaml: str | None = None
    if resolved_yaml:
        yaml_lines = resolved_yaml.splitlines()
        if len(yaml_lines) > 500:
            capped_yaml = "\n".join(yaml_lines[:500]) + f"\n# ... truncated ({len(yaml_lines) - 500} more lines)"
        else:
            capped_yaml = resolved_yaml

    node_map = (details.get("executionGraph") or {}).get("nodeMap") or {}

    # Build the markdown blob.
    out = [
        "## Pipeline Failure Analysis",
        "",
        "### Analysis Framing",
        "Attribute the root cause of this failure to exactly one of:",
        "1. **Pipeline / infra misconfiguration** — the YAML stanza, runner image, registry, or platform config is wrong.",
        "2. **Application code or dependencies** — the repo's source, `package.json`, tests, or build config is wrong.",
        "3. **Environmental / transient** — network blip, registry outage, flaky upstream, intermittent timeout.",
        "",
        "**Evidence hierarchy — use in this order:**",
        "1. **Executed Command (resolved)** under each failed step — this is the actual string Harness ran, "
        "after `<+...>` expressions and template inputs were applied. Ground truth.",
        "2. **Runtime Input Overrides** — shows which runtime inputs and template overrides were supplied "
        "when the pipeline was triggered. Explains *why* the resolved command differs from the YAML.",
        "3. **Pipeline YAML** — the authored pipeline (templates may be inlined depending on source; see the "
        "label under the YAML section). Still contains unresolved `<+...>` expressions, so it does not "
        "show what actually ran at the variable level.",
        "4. **Logs** — the runtime output.",
        "",
        "If the resolved command differs from the YAML stanza, the divergence comes from runtime inputs or a "
        "template override — name the specific input/override rather than speculating.",
        "",
        "### Execution Summary",
        f"- Pipeline: {pipeline_name}",
        f"- Execution ID: {exec_id}",
        f"- Status: {status}",
        f"- Triggered by: {trigger}",
        f"- Started: {start_ts} | Ended: {end_ts}",
        "",
        "### Failed Steps",
    ]

    for step in failed_steps:
        out.append(f"#### {step['name']} ({step['step_type']})")
        if step.get("failure_message"):
            out.append(f"- Error: {step['failure_message']}")
        if step.get("failure_types"):
            out.append(f"- Failure types: {', '.join(step['failure_types'])}")
        if step.get("start_ts") or step.get("end_ts"):
            out.append(f"- Window: {step.get('start_ts', '?')} → {step.get('end_ts', '?')}")

        resolved = hc.extract_resolved_step_details(node_map.get(step["node_id"]) or {})
        if resolved:
            unresolved = resolved.pop("expressions_unresolved", False)
            label = "Executed Step Parameters (resolved)" if not unresolved else \
                    "Executed Step Parameters (partially resolved — some `<+...>` expressions remain)"
            out.append("")
            out.append(f"##### {label}")
            for key in ("image", "shell", "connectorRef", "templateRef"):
                if key in resolved:
                    out.append(f"- **{key}**: `{resolved[key]}`")
            for key in ("command", "script"):
                if key in resolved:
                    out.append(f"- **{key}**:")
                    out.append("```sh")
                    out.append(resolved[key])
                    out.append("```")

        log_text = capped_logs.get(step["node_id"])
        if log_text:
            out.append("")
            out.append("##### Logs")
            out.append("```")
            out.append(log_text)
            out.append("```")
        out.append("")

    out.append("### Runtime Input Overrides")
    if input_set_yaml:
        out.append(
            "_Input-set YAML supplied for this execution — shows what values were bound to "
            "`<+input>` placeholders and which template inputs were overridden at trigger time._"
        )
        out.append("")
        out.append("```yaml")
        out.append(input_set_yaml if len(input_set_yaml.splitlines()) <= 300
                   else "\n".join(input_set_yaml.splitlines()[:300]) +
                        f"\n# ... truncated ({len(input_set_yaml.splitlines()) - 300} more lines)")
        out.append("```")
    else:
        out.append(
            "_No input-set YAML available. This usually means the pipeline had no runtime inputs "
            "or the endpoint was unreachable — check the resolved step parameters above for the "
            "ground truth of what ran._"
        )
    out.append("")

    out.append("### Pipeline YAML")
    if capped_yaml:
        out.append(
            f"_Source: {yaml_source}. For values actually substituted at runtime "
            "(e.g. `<+stage.variables.X>`, `<+secrets.getValue(...)>`), prefer the "
            "**Executed Step Parameters** sections above — those are the ground truth._"
        )
        out.append("")
        out.append("```yaml")
        out.append(capped_yaml)
        out.append("```")
    else:
        out.append(f"_Not available — {yaml_error or 'unknown reason'}._")
    out.append("")

    if include_rag:
        out.append("### Organizational Context (from RAG)")
        if isinstance(rag_result, Exception):
            out.append(f"Error: RAG call failed — {type(rag_result).__name__}: {rag_result}")
        else:
            out.append(_format_rag_results(rag_result))
        out.append("")

    blob = "\n".join(out).rstrip() + "\n"
    _analysis_cache[exec_id] = blob
    logger.info("Cached analysis for %s (%d chars)", exec_id, len(blob))
    return blob


@mcp.tool()
async def analyze_pipeline_failure(
    execution_id: str,
    ctx: Context,
    include_logs: bool = True,
    include_rag: bool = True,
) -> str:
    """
    Fetch a failed Harness execution, extract failed steps, optionally pull step
    logs and RAG context from past incidents, and return a combined markdown
    analysis blob for the IDE's AI to reason over.

    Args:
        execution_id: Harness planExecutionId, or a full Harness UI execution URL.
        include_logs: If True, fetch raw logs for each failed step (default True).
        include_rag: If True, query the backend RAG index for similar past incidents (default True).
    """
    exec_id = _extract_execution_id(execution_id)
    if not exec_id:
        return "Error: execution_id is required (raw ID or Harness UI URL)."
    return await _build_analysis(get_client(ctx), exec_id, include_logs, include_rag)


# ─── FUTURE: standalone RAG lookups ────────────────────────────────────────────
# Tools 3 and 4 disabled — their surface overlaps with analyze_pipeline_failure
# (which already queries RAG). Re-enable only if users ask for RAG search
# without a failing execution (e.g. "show me the payments-service runbook" or
# "has anyone hit this error before?" pasted standalone).
#
# @mcp.tool()
# async def get_similar_past_incidents(error_message: str, ctx: Context, limit: int = 5) -> str:
#     """
#     Search the backend RAG index for past Jira incidents and Confluence runbook
#     sections matching an error message. No Harness call — pure RAG lookup.
#
#     Args:
#         error_message: The error text to search for.
#         limit: Max results to return (default 5).
#     """
#     if not error_message or not error_message.strip():
#         return "Error: error_message is required."
#
#     _, api_url, api_key = get_config()
#     if err := validate_config("placeholder", api_key):
#         return err
#
#     client = get_client(ctx)
#     rag = await _call_rag(client, api_url, api_key, error_message.strip(), limit=limit)
#     if isinstance(rag, str):
#         return rag
#     return "## Similar Past Incidents\n\n" + _format_rag_results(rag)
#
#
# @mcp.tool()
# async def get_service_runbook(service_name: str, ctx: Context) -> str:
#     """
#     Fetch troubleshooting runbook content for a specific service from the RAG index.
#
#     Args:
#         service_name: Service identifier, e.g. "payments-service".
#     """
#     if not service_name or not service_name.strip():
#         return "Error: service_name is required."
#
#     _, api_url, api_key = get_config()
#     if err := validate_config("placeholder", api_key):
#         return err
#
#     client = get_client(ctx)
#     query = f"runbook {service_name.strip()} troubleshooting"
#     rag = await _call_rag(client, api_url, api_key, query, limit=5)
#     if isinstance(rag, str):
#         return rag
#     return f"## Runbook for {service_name}\n\n" + _format_rag_results(rag)


# ─── FUTURE: incident documentation + Confluence publish ──────────────────────
# Tools 5 and 6 are disabled for now. To re-enable, un-comment the block below
# and ensure the backend exposes /api/incidents/draft and /api/incidents/publish.
# Related infrastructure kept live: _analysis_cache in _build_analysis() — it
# still populates on every analyze_pipeline_failure call so tool 5 can reuse it
# once re-enabled.
#
# @mcp.tool()
# async def document_resolved_incident(
#     execution_id: str,
#     solution_summary: str,
#     ctx: Context,
#     classification: str = "unknown",
# ) -> str:
#     """
#     Draft an incident report for a resolved pipeline failure and store it on the
#     backend. This is a WRITE operation — the caller must get explicit user
#     confirmation before invoking publish_incident_report afterwards.
#
#     Args:
#         execution_id: The execution previously analyzed (raw ID or Harness UI URL).
#         solution_summary: What fixed the issue, written by the developer or IDE AI.
#         classification: One of "code", "infrastructure", "configuration", "unknown".
#     """
#     exec_id = _extract_execution_id(execution_id)
#     if not exec_id:
#         return "Error: execution_id is required."
#     if not solution_summary or not solution_summary.strip():
#         return "Error: solution_summary is required."
#
#     _, api_url, api_key = get_config()
#     if err := validate_config("placeholder", api_key):
#         return err
#
#     client = get_client(ctx)
#
#     analysis_blob = _analysis_cache.get(exec_id)
#     if not analysis_blob:
#         logger.info("No cached analysis for %s — regenerating", exec_id)
#         analysis_blob = await _build_analysis(client, exec_id, include_logs=True, include_rag=True)
#         if analysis_blob.startswith("Error:"):
#             return analysis_blob
#
#     payload = {
#         "execution_id": exec_id,
#         "analysis": analysis_blob,
#         "solution_summary": solution_summary.strip(),
#         "classification": classification,
#     }
#     try:
#         resp = await client.post(
#             f"{api_url}/api/incidents/draft",
#             headers={"X-API-Key": api_key},
#             json=payload,
#             timeout=60.0,
#         )
#     except httpx.HTTPError as e:
#         return f"Error calling backend: {type(e).__name__}: {e}"
#
#     if resp.status_code != 200:
#         return f"Error: Backend returned {resp.status_code} — {resp.text[:200]}"
#
#     draft = resp.json()
#     draft_id = draft.get("draft_id") or exec_id
#
#     preview = (
#         f"Draft ID: {draft_id}\n"
#         f"Execution: {exec_id}\n"
#         f"Classification: {classification}\n\n"
#         f"--- Solution Summary ---\n{solution_summary.strip()}\n\n"
#         f"--- Full Analysis ---\n{analysis_blob}"
#     )
#
#     return (
#         f"DRAFT INCIDENT REPORT CREATED\n\n"
#         f"===================================================================\n"
#         f"MANDATORY: You MUST display the full draft preview below to the user\n"
#         f"in a single fenced code block. Do NOT summarize or abbreviate.\n"
#         f"===================================================================\n\n"
#         f"```\n{preview}\n```\n\n"
#         f"===================================================================\n"
#         f"AFTER displaying the draft above, follow these rules:\n"
#         f"===================================================================\n"
#         f"1. ASK the user if they want to publish this report to Confluence.\n"
#         f"   Do NOT call publish_incident_report yet.\n"
#         f"2. ONLY if the user explicitly confirms (e.g. 'publish', 'yes', 'send it'),\n"
#         f"   call publish_incident_report(execution_id=\"{exec_id}\").\n"
#         f"3. If the user wants changes, ask them for the edits and call this tool\n"
#         f"   again with the revised solution_summary."
#     )
#
#
# @mcp.tool()
# async def publish_incident_report(
#     execution_id: str,
#     ctx: Context,
#     target_space: str = "Engineering/Incidents",
# ) -> str:
#     """
#     Publish a previously drafted incident report to Confluence. Only call this
#     after document_resolved_incident has run AND the user has explicitly confirmed
#     publication.
#
#     Args:
#         execution_id: The execution ID whose draft should be published.
#         target_space: Confluence space (default "Engineering/Incidents").
#     """
#     exec_id = _extract_execution_id(execution_id)
#     if not exec_id:
#         return "Error: execution_id is required."
#
#     _, api_url, api_key = get_config()
#     if err := validate_config("placeholder", api_key):
#         return err
#
#     client = get_client(ctx)
#     try:
#         resp = await client.post(
#             f"{api_url}/api/incidents/publish",
#             headers={"X-API-Key": api_key},
#             json={"execution_id": exec_id, "target_space": target_space},
#             timeout=60.0,
#         )
#     except httpx.HTTPError as e:
#         return f"Error calling backend: {type(e).__name__}: {e}"
#
#     if resp.status_code != 200:
#         return f"Error: Backend returned {resp.status_code} — {resp.text[:200]}"
#
#     data = resp.json()
#     page_url = data.get("confluence_url") or data.get("url") or "(no URL returned)"
#     return (
#         f"Incident report published to Confluence space '{target_space}'.\n"
#         f"Page: {page_url}\n"
#         f"This report will be indexed for future RAG queries."
#     )


# ─── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
