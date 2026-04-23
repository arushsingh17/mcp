import io
import json
import logging
import re
import zipfile

import httpx

logger = logging.getLogger("mcp.harness-client")


def _headers(cfg: dict) -> dict:
    return {
        "x-api-key": cfg["api_key"],
        "Content-Type": "application/json",
    }


def _common_query(cfg: dict) -> dict:
    return {
        "accountIdentifier": cfg["account_id"],
        "orgIdentifier": cfg["org_id"],
        "projectIdentifier": cfg["project_id"],
    }


def _map_error(resp: httpx.Response, context: str) -> str:
    if resp.status_code == 401:
        return "Harness API key is invalid or expired"
    if resp.status_code == 404:
        return f"Harness resource not found: {context}"
    return f"Harness API returned {resp.status_code}: {resp.text[:200]}"


async def list_recent_executions(
    client: httpx.AsyncClient,
    cfg: dict,
    status_filter: str = "Failed",
    limit: int = 10,
) -> list | str:
    """POST /gateway/pipeline/api/pipelines/execution/summary"""
    url = f"{cfg['base_url']}/gateway/pipeline/api/pipelines/execution/summary"
    params = {
        **_common_query(cfg),
        "page": 0,
        "size": limit,
        "sort": "startTs,DESC",
    }
    body = {
        "filterType": "PipelineExecution",
        "timeRange": {"timeRangeFilterType": "LAST_30_DAYS"},
        "status": [status_filter] if status_filter else [],
    }
    try:
        resp = await client.post(url, headers=_headers(cfg), params=params, json=body, timeout=30.0)
    except httpx.ConnectError:
        return f"Cannot connect to Harness API at {cfg['base_url']}"
    except httpx.HTTPError as e:
        return f"Harness request failed: {type(e).__name__}: {e}"

    if resp.status_code != 200:
        return _map_error(resp, "execution summary")

    data = resp.json()
    return data.get("data", {}).get("content", [])


async def get_execution_details(
    client: httpx.AsyncClient,
    cfg: dict,
    plan_execution_id: str,
) -> dict | str:
    """GET /pipeline/api/pipelines/execution/v2/{planExecutionId}"""
    url = f"{cfg['base_url']}/pipeline/api/pipelines/execution/v2/{plan_execution_id}"
    params = {**_common_query(cfg), "renderFullBottomGraph": "true"}
    try:
        resp = await client.get(url, headers=_headers(cfg), params=params, timeout=30.0)
    except httpx.ConnectError:
        return f"Cannot connect to Harness API at {cfg['base_url']}"
    except httpx.HTTPError as e:
        return f"Harness request failed: {type(e).__name__}: {e}"

    if resp.status_code != 200:
        return _map_error(resp, f"execution {plan_execution_id}")

    return resp.json().get("data", {})


def parse_failed_steps(execution_details: dict) -> list[dict]:
    """Walk executionGraph.nodeMap and extract Failed nodes."""
    if not isinstance(execution_details, dict):
        return []
    graph = execution_details.get("executionGraph") or {}
    node_map = graph.get("nodeMap") or {}
    failed = []
    for node_id, node in node_map.items():
        if not isinstance(node, dict):
            continue
        if node.get("status") != "Failed":
            continue
        failure_info = node.get("failureInfo") or {}
        failed.append({
            "node_id": node_id,
            "name": node.get("name", ""),
            "identifier": node.get("identifier", ""),
            "step_type": node.get("stepType", ""),
            "failure_message": failure_info.get("message", ""),
            "failure_types": failure_info.get("failureTypeList", []),
            "start_ts": node.get("startTs"),
            "end_ts": node.get("endTs"),
        })
    return failed


_YAML_KEYS = (
    "resolvedTemplatesPipelineYaml",
    "executedPipelineYaml",
    "pipelineYaml",
    "yaml",
    "yamlPipeline",
)


def extract_resolved_yaml(execution_details: dict) -> str | None:
    """Pull the post-template-expansion YAML from a /execution/v2 response.

    Harness nests these fields inside `pipelineExecutionSummary` in current
    API versions, but older variants put them at the top of `data`. Check
    the summary first, then fall back to the top level.
    """
    if not isinstance(execution_details, dict):
        return None
    summary = execution_details.get("pipelineExecutionSummary") or {}
    for key in _YAML_KEYS:
        val = summary.get(key) if isinstance(summary, dict) else None
        if not (isinstance(val, str) and val.strip()):
            val = execution_details.get(key)
        if isinstance(val, str) and val.strip():
            return val
    summary_keys = list(summary.keys()) if isinstance(summary, dict) else []
    logger.warning(
        "extract_resolved_yaml: no YAML under any of %s. summary keys=%s  top keys=%s",
        _YAML_KEYS, summary_keys, list(execution_details.keys()),
    )
    return None


def extract_pipeline_definition_yaml(definition: dict) -> str | None:
    """Pull raw YAML from a /pipelines/{id} response body."""
    if not isinstance(definition, dict):
        return None
    for key in _YAML_KEYS:
        val = definition.get(key)
        if isinstance(val, str) and val.strip():
            return val
    logger.warning(
        "extract_pipeline_definition_yaml: no YAML under any of %s. keys=%s",
        _YAML_KEYS, list(definition.keys()),
    )
    return None


_EXPR_RE = re.compile(r"<\+[^>]+>")


def extract_resolved_step_details(node: dict) -> dict:
    """Pull the executed step parameters from a nodeMap entry.

    Harness evaluates `<+...>` expressions and applies template inputs before
    executing a step, then persists the result under `stepParameters`. So when
    the committed YAML says `command: npm run <+stage.variables.script>`, this
    returns the actual string that ran (e.g. `npm run build`). That string is
    the ground truth for reconstructing what executed — the YAML alone can't
    show it, because runtime inputs and template overrides happen after the
    YAML is authored.

    Returns a dict with any of: command, script, image, shell, connectorRef,
    templateRef, expressions_unresolved (bool — true if any `<+...>` remains
    inside the resolved strings, which means Harness couldn't fully resolve).
    """
    if not isinstance(node, dict):
        return {}
    params = node.get("stepParameters")
    if not isinstance(params, dict):
        return {}

    spec = params.get("spec") if isinstance(params.get("spec"), dict) else params

    resolved: dict = {}
    for key in ("command", "script", "image", "shell", "connectorRef"):
        val = spec.get(key) if isinstance(spec, dict) else None
        if isinstance(val, str) and val.strip():
            resolved[key] = val

    tlc = params.get("templateLinkConfig")
    if isinstance(tlc, dict):
        ref = tlc.get("templateRef")
        if isinstance(ref, str) and ref.strip():
            resolved["templateRef"] = ref

    has_unresolved = any(
        isinstance(v, str) and _EXPR_RE.search(v)
        for v in resolved.values()
    )
    if resolved:
        resolved["expressions_unresolved"] = has_unresolved
    return resolved


async def get_execution_resolved_yaml(
    client: httpx.AsyncClient,
    cfg: dict,
    plan_execution_id: str,
) -> str | None:
    """GET /pipeline/api/pipelines/execution/{planExecutionId}/metadata

    Returns `executionYaml` — Harness's template-expanded pipeline YAML for this
    execution. Templates (e.g. `templateRef: account.SonarQube_Step`) are
    inlined into their full step bodies, so this is a material upgrade over the
    committed source. Note: `<+...>` variable/secret expressions are NOT
    substituted here — those remain under each step's `stepParameters`. Returns
    None on any failure, since callers already have the committed-YAML fallback.
    """
    url = (
        f"{cfg['base_url']}/pipeline/api/pipelines/execution/"
        f"{plan_execution_id}/metadata"
    )
    try:
        resp = await client.get(url, headers=_headers(cfg), params=_common_query(cfg), timeout=30.0)
    except httpx.HTTPError as e:
        logger.info("get_execution_resolved_yaml failed (non-fatal): %s", e)
        return None
    if resp.status_code != 200:
        logger.info(
            "get_execution_resolved_yaml: %d for %s (non-fatal)",
            resp.status_code, plan_execution_id,
        )
        return None
    data = resp.json().get("data") or {}
    val = data.get("executionYaml")
    return val if isinstance(val, str) and val.strip() else None


async def get_execution_input_set(
    client: httpx.AsyncClient,
    cfg: dict,
    plan_execution_id: str,
) -> str | None:
    """GET /pipeline/api/pipelines/execution/{planExecutionId}/inputset

    Returns the runtime input YAML supplied for this execution — what values
    were bound to `<+input>` placeholders and which template inputs the
    trigger overrode. Complements the pipeline definition YAML by showing the
    runtime side of the contract. Best-effort: returns None on any failure,
    since this is supplementary context rather than a hard dependency.
    """
    url = (
        f"{cfg['base_url']}/pipeline/api/pipelines/execution/"
        f"{plan_execution_id}/inputset"
    )
    params = {**_common_query(cfg), "resolveExpressions": "true"}
    try:
        resp = await client.get(url, headers=_headers(cfg), params=params, timeout=30.0)
    except httpx.HTTPError as e:
        logger.info("get_execution_input_set failed (non-fatal): %s", e)
        return None
    if resp.status_code != 200:
        logger.info(
            "get_execution_input_set: %d for %s (non-fatal)",
            resp.status_code, plan_execution_id,
        )
        return None
    data = resp.json().get("data") or {}
    for key in ("inputSetYaml", "inputSetTemplateYaml", "yaml", "pipelineYaml"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


async def download_step_logs(
    client: httpx.AsyncClient,
    cfg: dict,
    pipeline_id: str,
    run_sequence: str,
    plan_execution_id: str,
    stage_id: str,
    step_id: str,
) -> str:
    """POST /gateway/log-service/blob/download → follow URL → return log text."""
    url = f"{cfg['base_url']}/gateway/log-service/blob/download"
    prefix = (
        f"{cfg['account_id']}/pipeline/{pipeline_id}/{run_sequence}/"
        f"-{plan_execution_id}/{stage_id}/{step_id}"
    )
    params = {"accountID": cfg["account_id"], "prefix": prefix}
    logger.info("download_step_logs: POST %s  prefix=%s", url, prefix)
    try:
        resp = await client.post(url, headers=_headers(cfg), params=params, timeout=30.0)
    except httpx.ConnectError:
        return f"Cannot connect to Harness API at {cfg['base_url']}"
    except httpx.HTTPError as e:
        return f"Harness log request failed: {type(e).__name__}: {e}"

    if resp.status_code != 200:
        logger.warning(
            "download_step_logs: log-service POST returned %d for prefix=%s body=%s",
            resp.status_code, prefix, resp.text[:300],
        )
        return _map_error(resp, f"logs for step {step_id}")

    download_link = resp.json().get("link", "")
    if not download_link:
        logger.warning("download_step_logs: no download link returned for prefix=%s", prefix)
        return "(no log download link returned)"

    logger.info("download_step_logs: following link=%s", download_link[:300])
    try:
        log_resp = await client.get(download_link, timeout=30.0)
    except httpx.HTTPError as e:
        return f"Failed to fetch log blob: {type(e).__name__}: {e}"

    if log_resp.status_code != 200:
        logger.warning(
            "download_step_logs: blob GET returned %d  prefix=%s  link=%s  body=%s",
            log_resp.status_code, prefix, download_link[:300], log_resp.text[:300],
        )
        return f"Log blob fetch returned {log_resp.status_code}"

    text = _decode_harness_log_blob(log_resp.content)
    if len(text) > 50_000:
        lines = text.splitlines()
        text = "\n".join(lines[-200:])
    return text


def _decode_harness_log_blob(body: bytes) -> str:
    """Harness log-service returns a ZIP of JSON-lines files ({"out": "..."} per line).

    Unzip, pull the first entry, and extract the `out` field from each JSON line.
    """
    if body[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                names = zf.namelist()
                if not names:
                    return "(empty log archive)"
                raw = zf.read(names[0]).decode("utf-8", errors="replace")
        except zipfile.BadZipFile as e:
            return f"(failed to unzip log blob: {e})"
    else:
        raw = body.decode("utf-8", errors="replace")

    out_lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            out_lines.append(line)
            continue
        if isinstance(obj, dict):
            out_lines.append(str(obj.get("out", "")).rstrip("\n"))
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


async def get_pipeline_definition(
    client: httpx.AsyncClient,
    cfg: dict,
    pipeline_id: str,
) -> dict | str:
    """GET /pipeline/api/pipelines/{pipelineIdentifier}"""
    url = f"{cfg['base_url']}/pipeline/api/pipelines/{pipeline_id}"
    params = _common_query(cfg)
    try:
        resp = await client.get(url, headers=_headers(cfg), params=params, timeout=30.0)
    except httpx.ConnectError:
        return f"Cannot connect to Harness API at {cfg['base_url']}"
    except httpx.HTTPError as e:
        return f"Harness request failed: {type(e).__name__}: {e}"

    if resp.status_code != 200:
        return _map_error(resp, f"pipeline {pipeline_id}")

    return resp.json().get("data", {})
