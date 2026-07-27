"""Post RCA result to Slack via incoming webhook.

Fire-and-forget: failures logged but do not break the RCA response. Webhook
URL comes from secrets (`slack_webhook_url`). If not set, this is a no-op.
"""
import os
import httpx


WEBHOOK_URL = os.environ.get("JENKINS_RCA_SLACK_WEBHOOK_URL", "")


def _build_blocks(rca: dict, job: str, build: int) -> list[dict]:
    summary = rca.get("summary", "—")
    root = rca.get("root_cause", "—")
    fix = rca.get("suggested_fix", "—")
    cls = rca.get("error_class", "unknown")
    conf = rca.get("confidence", 0)
    req_id = rca.get("request_id", "—")
    cmds = rca.get("suggested_commands", [])

    cmd_lines = []
    for c in cmds[:3]:
        tier = c.get("tier", "?")
        cmd = c.get("cmd", "")
        cmd_lines.append(f"`[{tier}] {cmd}`")
    cmds_text = "\n".join(cmd_lines) if cmd_lines else "_none_"

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"❌ {job} #{build}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Class:*\n{cls}"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n{conf}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary*\n{summary}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Root cause*\n{root}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Fix*\n{fix}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Commands*\n{cmds_text}"}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"request_id: `{req_id}`"}],
        },
    ]


async def post(rca: dict, job: str, build: int) -> bool:
    """Returns True if posted, False if skipped or failed."""
    if not WEBHOOK_URL:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(WEBHOOK_URL, json={"blocks": _build_blocks(rca, job, build)})
            r.raise_for_status()
        return True
    except Exception as e:
        print(f"[slack] post failed: {e}")
        return False
