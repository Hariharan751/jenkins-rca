import diskcache
import hashlib
from pathlib import Path
from datetime import date

CACHE_DIR = Path("/var/cache/jenkins-rca")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_cache = diskcache.Cache(str(CACHE_DIR / "cache.db"))

DEDUP_TTL = 60          # seconds — in-flight collision window
RCA_RESULT_TTL = 86400  # 24h — same job+build returns cached RCA (no LLM call)
TOOL_CACHE_TTL = 86400  # 24h
DAILY_COST_CAP = 20.0   # USD


def dedup_key(job: str, build: int) -> str:
    return f"dedup:{job}:{build}"


def rca_key(job: str, build: int) -> str:
    return f"rca:{job}:{build}"


def is_duplicate(job: str, build: int) -> str | None:
    return _cache.get(dedup_key(job, build))


def mark_processed(job: str, build: int, request_id: str):
    _cache.set(dedup_key(job, build), request_id, expire=DEDUP_TTL)


def get_rca(job: str, build: int) -> dict | None:
    """Return cached RCA result for this (job, build) if any."""
    return _cache.get(rca_key(job, build))


def set_rca(job: str, build: int, result: dict):
    _cache.set(rca_key(job, build), result, expire=RCA_RESULT_TTL)


def tool_cache_key(tool: str, args: dict) -> str:
    h = hashlib.sha256(f"{tool}:{sorted(args.items())}".encode()).hexdigest()[:16]
    return f"tool:{h}"


def get_tool_cache(tool: str, args: dict):
    return _cache.get(tool_cache_key(tool, args))


def set_tool_cache(tool: str, args: dict, value):
    _cache.set(tool_cache_key(tool, args), value, expire=TOOL_CACHE_TTL)


def daily_spend() -> float:
    return _cache.get(f"cost:{date.today().isoformat()}", default=0.0)


def add_spend(usd: float):
    key = f"cost:{date.today().isoformat()}"
    current = _cache.get(key, default=0.0)
    _cache.set(key, current + usd, expire=86400 * 2)


def over_daily_cap() -> bool:
    return daily_spend() >= DAILY_COST_CAP
