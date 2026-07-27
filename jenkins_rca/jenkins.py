import httpx
from functools import lru_cache

# Jenkins REST client
# Base: http://10.34.42.254:8080
# Auth: g.hariharan@blackbuck.com : <jenkins_token from SOPS keys.enc.yaml>


async def get_console_log(job: str, build: str | int, base_url: str, auth: tuple) -> str:
    url = f"{base_url}/job/{job}/{build}/consoleText"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, auth=auth)
        r.raise_for_status()
        return r.text


async def get_build_meta(job: str, build: str | int, base_url: str, auth: tuple) -> dict:
    url = f"{base_url}/job/{job}/{build}/api/json"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, auth=auth)
        r.raise_for_status()
        return r.json()


async def get_stage_errors(job: str, build: str | int, base_url: str, auth: tuple) -> list[dict]:
    """Fetch per-stage status + error.message via Jenkins workflow REST API.

    The `consoleText` endpoint may not have flushed the trailing exception
    trace yet when we're called from a post.failure block — Jenkins emits
    things like `Also: groovy.lang.MissingMethodException ... at WorkflowScript:330`
    AFTER the post block completes. `wfapi/describe`, on the other hand,
    populates `error.message` as soon as the stage transitions to FAILED.

    Returns list of {name, status, error_message} for FAILED/UNSTABLE stages.
    Empty list if endpoint unavailable (older Jenkins) or no failed stages.
    """
    url = f"{base_url}/job/{job}/{build}/wfapi/describe"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, auth=auth)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return []
    out = []
    for st in data.get("stages", []):
        status = (st.get("status") or "").upper()
        if status not in ("FAILED", "UNSTABLE", "ABORTED"):
            continue
        err = st.get("error") or {}
        msg = err.get("message") or ""
        out.append({
            "name": st.get("name", ""),
            "status": status,
            "error_message": msg,
        })
    return out


async def get_last_failed_build(job: str, base_url: str, auth: tuple) -> dict:
    url = f"{base_url}/job/{job}/lastFailedBuild/api/json"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, auth=auth)
        r.raise_for_status()
        return r.json()


async def get_job_config(job: str, base_url: str, auth: tuple) -> dict:
    """Fetch the Jenkins job's config.xml and extract the bits the agent
    cares about: which SCM repo holds the pipeline definition, which branch,
    and which script path is run (e.g. `main_stagger_prod_plus_one.groovy`).

    Returns a structured dict; the raw XML stays on the side under "raw_xml"
    in case the agent needs it.
    """
    url = f"{base_url}/job/{job}/config.xml"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, auth=auth)
        r.raise_for_status()
        xml = r.text

    return _parse_job_config(xml)


async def get_node_info(node_name: str, base_url: str, auth: tuple) -> dict:
    """Resolve a Jenkins node label (e.g. `slave-4`) to its underlying
    EC2 instance ID, hostname, labels, and online state.

    Jenkins exposes `/computer/<name>/api/json` for every executor.
    The EC2 instance ID is hidden inside `description` (typed by the
    AWS-EC2 plugin) or `labelString` depending on plugin version.
    Walk both + fall back to regex over the whole response.

    Returns:
        {
          "name":          "slave-4",
          "online":        true | false,
          "offline_cause": "..." | null,
          "instance_id":   "i-XXXXXXXXXXXXXXXXX" | null,
          "host":          "10.34.x.y" | null,
          "labels":        ["...", "..."],
        }

    On HTTP/parse error, returns {"error": "<message>"}. Empty
    instance_id is normal for non-EC2 agents — caller falls back to
    naming-convention discovery (`bbctl ec2 list --tag jenkins-node`).
    """
    import re as _re

    url = f"{base_url}/computer/{node_name}/api/json?depth=1"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, auth=auth)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"name": node_name, "error": f"jenkins node API: {e}"}

    name = data.get("displayName") or node_name
    online = not bool(data.get("offline"))
    cause = None
    if not online:
        oc = data.get("offlineCause") or {}
        cause = oc.get("description") or oc.get("name") or str(oc)[:200]

    desc = data.get("description") or ""
    labels = [
        lbl.get("name") for lbl in (data.get("assignedLabels") or [])
        if isinstance(lbl, dict) and lbl.get("name")
    ]

    # Instance ID lookup — most reliable order:
    # 1. AWS-EC2-plugin "ec2-fleet" stores `i-...` in description
    # 2. Some configs put it in labelString — match anywhere in JSON
    # 3. Some templated node names include the ID literally (slave-i-XXX)
    instance_id = None
    m = _re.search(r"\bi-[0-9a-f]{8,17}\b", desc)
    if not m:
        # Wide fallback — search whole JSON serialization
        import json as _json
        m = _re.search(r"\bi-[0-9a-f]{8,17}\b", _json.dumps(data))
    if not m:
        m = _re.search(r"\bi-[0-9a-f]{8,17}\b", node_name)
    if m:
        instance_id = m.group(0)

    # Host / IP
    host = None
    mh = _re.search(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b",
                    desc + " " + (data.get("description") or ""))
    if mh:
        host = mh.group(1)

    return {
        "name":          name,
        "online":        online,
        "offline_cause": cause,
        "instance_id":   instance_id,
        "host":          host,
        "labels":        labels,
    }


def _parse_job_config(xml: str) -> dict:
    """Pull SCM + script path out of a Jenkins job config.xml.

    Avoids a real XML parser — Jenkins' config.xml uses well-known tag names
    so a tolerant regex sweep is good enough and stays dependency-free.
    """
    import re as _re

    def _find(tag: str) -> str | None:
        m = _re.search(rf"<{tag}>([^<]+)</{tag}>", xml)
        return m.group(1).strip() if m else None

    return {
        "scm_url": _find("url"),
        "scm_branch": _find("name") or _find("branchSpec") or _find("branch"),
        "script_path": _find("scriptPath"),
        # Inline pipeline scripts live under <script>...</script> (not common
        # for the stagger family — those use scriptPath — but cover both)
        "inline_script": _find("script"),
        "raw_xml": xml[:8000],  # cap so the LLM doesn't choke on massive XML
    }
