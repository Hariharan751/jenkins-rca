from pydantic import BaseModel
from typing import Optional


class WebhookPayload(BaseModel):
    job: str
    build: int
    service: str
    commit: str = ""
    buildUrl: str = ""
    consoleUrl: str = ""


class RCARequest(BaseModel):
    job: str
    build: int
    deep: bool = False


class Evidence(BaseModel):
    source: str
    snippet: str


class SuggestedCommand(BaseModel):
    cmd: str
    tier: str  # safe | restricted | denied
    rationale: str


class RCAResponse(BaseModel):
    request_id: str
    summary: str
    failed_stage: str
    error_class: str
    root_cause: str
    evidence: list[Evidence]
    suggested_fix: str
    suggested_commands: list[SuggestedCommand]
    confidence: float
    needs_deeper: bool
    tokens_used: dict
