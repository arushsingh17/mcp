import logging
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
    params = _common_query(cfg)
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
    try:
        resp = await client.post(url, headers=_headers(cfg), params=params, timeout=30.0)
    except httpx.ConnectError:
        return f"Cannot connect to Harness API at {cfg['base_url']}"
    except httpx.HTTPError as e:
        return f"Harness log request failed: {type(e).__name__}: {e}"

    if resp.status_code != 200:
        return _map_error(resp, f"logs for step {step_id}")

    download_link = resp.json().get("link", "")
    if not download_link:
        return "(no log download link returned)"

    try:
        log_resp = await client.get(download_link, timeout=30.0)
    except httpx.HTTPError as e:
        return f"Failed to fetch log blob: {type(e).__name__}: {e}"

    if log_resp.status_code != 200:
        return f"Log blob fetch returned {log_resp.status_code}"

    text = log_resp.text
    if len(text) > 50_000:
        lines = text.splitlines()
        text = "\n".join(lines[-200:])
    return text


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
