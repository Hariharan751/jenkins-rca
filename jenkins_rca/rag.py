"""jenkins-rca RAG store. PostgreSQL + pgvector.

Public surface
--------------
embed(text)               -> list[float]                  embedding (1536d, OpenAI text-embedding-3-small)
upsert(rows)              -> int                          # of rows inserted/updated in rca_chunks
search(query, k, filters) -> list[dict]                   top-k retrieved chunks with scores
index_docops()            -> dict                         walk docops/, embed all docs/runbooks/job_flows
index_audits()            -> dict                         walk audit dir, embed all RCA JSONs
index_log_window(...)     -> dict                         embed one Jenkins log error window (per build)

CLI entry (python -m jenkins_rca.rag <cmd>):
  embed "<text>"          quick smoke test — print first 5 dims
  search "<query>" [k]    semantic search smoke test
  index-docops            (re)embed all docops/* (incremental via content_hash)
  index-audits            (re)embed all audit JSONs
  reset                   TRUNCATE rca_chunks (dangerous — confirm with --yes)

Design notes
------------
- Embedding model = OpenAI text-embedding-3-small (1536d, $0.02/1M tokens).
  Cheap, unit-norm, cosine distance is the natural similarity metric.
- Two-layer cache: query_emb_cache (skip embed API on repeat log windows)
  and retrieval_cache (skip pgvector lookup on repeat queries). TTL-bounded.
- content_hash dedup means re-running index-docops on unchanged files is
  a no-op — only changed/new chunks get re-embedded.
- Phase-10-safe: this module is RETRIEVAL only. It never edits or post-
  processes LLM output. Inject results into prompt as ## retrieved context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

# psycopg is the v3 driver. We depend on it explicitly because:
#  - v3 is the actively maintained driver (psycopg2 is in maintenance mode)
#  - register_vector() in pgvector.psycopg adapts list[float] -> VECTOR
import psycopg
from pgvector.psycopg import register_vector


EMBED_MODEL   = os.environ.get("JENKINS_RCA_EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM     = 1536
QUERY_CACHE_TTL_HOURS    = 24
RETRIEVAL_CACHE_TTL_HOURS = 2


# ── Connection helpers ────────────────────────────────────────────────

def _conn_kwargs() -> dict:
    """Read PG creds from env. The jenkins-rca service loads these from
    AWS Secrets Manager (jenkins-rca/prod -> pg_password, pg_host, pg_db,
    pg_user, pg_port) via secrets.export_env(), which prefixes them with
    JENKINS_RCA_. rag-postgres-install.sh writes those keys into the secret.
    """
    return {
        "host":     os.environ.get("JENKINS_RCA_PG_HOST", "127.0.0.1"),
        "port":     int(os.environ.get("JENKINS_RCA_PG_PORT", "5432")),
        "dbname":   os.environ.get("JENKINS_RCA_PG_DB", "jenkins_rca"),
        "user":     os.environ.get("JENKINS_RCA_PG_USER", "jenkins_rca"),
        "password": os.environ.get("JENKINS_RCA_PG_PASSWORD", ""),
    }


@contextmanager
def _connect():
    """Yield a psycopg connection with pgvector adapter registered."""
    kw = _conn_kwargs()
    if not kw["password"]:
        raise RuntimeError(
            "JENKINS_RCA_PG_PASSWORD not set. Run rag-postgres-install.sh on the "
            "host, then restart jenkins-rca so it picks up the new pg_password "
            "from Secrets Manager."
        )
    with psycopg.connect(**kw, autocommit=False) as conn:
        register_vector(conn)
        yield conn


# ── Embedding (with cache) ────────────────────────────────────────────

def _hash(text: str) -> str:
    """sha256 of normalized text (lowercase, collapsed whitespace)."""
    norm = " ".join(text.lower().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _openai_embed(text: str) -> list[float]:
    """Call OpenAI embeddings API."""
    from openai import OpenAI
    api_key = os.environ.get("JENKINS_RCA_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("JENKINS_RCA_LLM_API_KEY (or OPENAI_API_KEY) not set")
    client = OpenAI(api_key=api_key)
    # Truncate to model's 8192-token ceiling. OpenAI accepts longer but errors.
    # We pre-chunk to ~500 tokens so this is defensive only.
    resp = client.embeddings.create(model=EMBED_MODEL, input=text[:30000])
    return list(resp.data[0].embedding)


def embed(text: str, *, use_cache: bool = True) -> list[float]:
    """Embed text via OpenAI, caching by sha256(normalized text).

    The cache reads via SELECT, then issues an UPDATE to bump the hits
    counter (and refresh ttl). Cold path inserts a fresh row.
    """
    if not text or not text.strip():
        raise ValueError("embed(): empty text")
    h = _hash(text)
    if use_cache:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT embedding FROM query_emb_cache "
                    "WHERE query_hash = %s AND ttl_expires_at > now()",
                    (h,),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "UPDATE query_emb_cache SET hits = hits + 1 "
                        "WHERE query_hash = %s",
                        (h,),
                    )
                    conn.commit()
                    return list(row[0])

    vec = _openai_embed(text)

    if use_cache:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO query_emb_cache "
                    "(query_hash, embedding, ttl_expires_at) "
                    "VALUES (%s, %s, now() + make_interval(hours => %s)) "
                    "ON CONFLICT (query_hash) DO UPDATE SET "
                    "  embedding = EXCLUDED.embedding, "
                    "  ttl_expires_at = EXCLUDED.ttl_expires_at, "
                    "  hits = query_emb_cache.hits + 1",
                    (h, vec, QUERY_CACHE_TTL_HOURS),
                )
                conn.commit()
    return vec


# ── Chunking (markdown-aware) ─────────────────────────────────────────

_TOKEN_PER_CHAR = 0.25  # rough — actual = 0.20-0.30 depending on content
TARGET_TOKENS   = 500
TARGET_CHARS    = int(TARGET_TOKENS / _TOKEN_PER_CHAR)  # ~2000 chars
OVERLAP_CHARS   = 200


def _chunk_markdown(text: str) -> list[str]:
    """Split markdown on `## ` headers, then enforce per-chunk char ceiling
    with overlap. Preserves heading text inside each chunk so each chunk is
    self-contained when retrieved out of order."""
    if not text.strip():
        return []
    # First split: by H2 headers. Keep the heading inside its section.
    parts = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    parts = [p.strip() for p in parts if p.strip()]
    chunks: list[str] = []
    for part in parts:
        if len(part) <= TARGET_CHARS:
            chunks.append(part)
            continue
        # Second split: rolling window with overlap.
        i = 0
        while i < len(part):
            chunks.append(part[i : i + TARGET_CHARS])
            i += TARGET_CHARS - OVERLAP_CHARS
    return chunks


# ── Upsert ────────────────────────────────────────────────────────────

def upsert(rows: Iterable[dict]) -> int:
    """Insert/update rca_chunks rows. Each row dict needs:
        source_type, source_id, chunk_idx, chunk_text, embedding, meta(dict)

    content_hash is computed here. ON CONFLICT updates only if hash differs.
    Returns the number of rows actually written."""
    written = 0
    with _connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                ch = _hash(r["chunk_text"])
                cur.execute(
                    """
                    INSERT INTO rca_chunks
                      (source_type, source_id, chunk_idx, chunk_text,
                       embedding, meta, content_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_type, source_id, chunk_idx) DO UPDATE
                    SET chunk_text   = EXCLUDED.chunk_text,
                        embedding    = EXCLUDED.embedding,
                        meta         = EXCLUDED.meta,
                        content_hash = EXCLUDED.content_hash,
                        updated_at   = now()
                    WHERE rca_chunks.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    """,
                    (
                        r["source_type"],
                        r["source_id"],
                        r.get("chunk_idx", 0),
                        r["chunk_text"],
                        r["embedding"],
                        json.dumps(r.get("meta", {})),
                        ch,
                    ),
                )
                if cur.rowcount:
                    written += 1
            conn.commit()
    return written


# ── Search ────────────────────────────────────────────────────────────

def search(
    query: str,
    *,
    k: int = 5,
    source_types: list[str] | None = None,
    error_class: str | None = None,
    use_cache: bool = True,
) -> list[dict]:
    """Retrieve top-k chunks semantically similar to `query`.

    Filters:
      source_types — restrict to subset of {'runbook','doc','job_flow','audit','log'}
      error_class  — restrict to chunks whose meta->>'error_class' matches

    Returns list of dicts: {id, source_type, source_id, chunk_text, meta, score}
    where score is cosine SIMILARITY (1 = identical, 0 = orthogonal).
    """
    q_emb = embed(query, use_cache=use_cache)
    cache_key = _hash(
        f"{query}|{','.join(source_types or [])}|{error_class or ''}|{k}"
    )

    # Try retrieval cache first.
    if use_cache:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT chunk_ids, scores FROM retrieval_cache "
                    "WHERE cache_key = %s AND ttl_expires_at > now()",
                    (cache_key,),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "UPDATE retrieval_cache SET hits = hits + 1 "
                        "WHERE cache_key = %s",
                        (cache_key,),
                    )
                    conn.commit()
                    return _fetch_chunks(row[0], row[1])

    # Build the WHERE clause dynamically — pgvector cosine op is `<=>`,
    # which is a DISTANCE (0 = identical). Convert to similarity = 1 - distance.
    # Use named placeholders so we don't tangle positional args.
    where_parts: list[str] = []
    bind: dict[str, Any] = {"q_emb": q_emb, "k": k}
    if source_types:
        where_parts.append("source_type = ANY(%(stypes)s)")
        bind["stypes"] = source_types
    if error_class:
        where_parts.append("meta->>'error_class' = %(eclass)s")
        bind["eclass"] = error_class
    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = f"""
        SELECT id, source_type, source_id, chunk_text, meta,
               1 - (embedding <=> %(q_emb)s::vector) AS score
        FROM rca_chunks
        {where_sql}
        ORDER BY embedding <=> %(q_emb)s::vector ASC
        LIMIT %(k)s
    """

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, bind)
            rows = cur.fetchall()

    results = [
        {
            "id": r[0],
            "source_type": r[1],
            "source_id":   r[2],
            "chunk_text":  r[3],
            "meta":        r[4],
            "score":       float(r[5]),
        }
        for r in rows
    ]

    # Write to retrieval cache.
    if use_cache and results:
        ids = [r["id"] for r in results]
        scores = [r["score"] for r in results]
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO retrieval_cache "
                    "(cache_key, chunk_ids, scores, ttl_expires_at) "
                    "VALUES (%s, %s, %s, now() + make_interval(hours => %s)) "
                    "ON CONFLICT (cache_key) DO UPDATE SET "
                    "  chunk_ids = EXCLUDED.chunk_ids, "
                    "  scores = EXCLUDED.scores, "
                    "  ttl_expires_at = EXCLUDED.ttl_expires_at, "
                    "  hits = retrieval_cache.hits + 1",
                    (cache_key, ids, scores, RETRIEVAL_CACHE_TTL_HOURS),
                )
                conn.commit()

    return results


def _fetch_chunks(ids: list[int], scores: list[float]) -> list[dict]:
    """Resolve cached chunk_ids back to row dicts."""
    if not ids:
        return []
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, source_type, source_id, chunk_text, meta "
                "FROM rca_chunks WHERE id = ANY(%s)",
                (ids,),
            )
            by_id = {r[0]: r for r in cur.fetchall()}
    out: list[dict] = []
    for i, rid in enumerate(ids):
        r = by_id.get(rid)
        if not r:
            continue
        out.append({
            "id": r[0], "source_type": r[1], "source_id": r[2],
            "chunk_text": r[3], "meta": r[4],
            "score": scores[i] if i < len(scores) else 0.0,
        })
    return out


# ── Indexers (CLI-driven) ─────────────────────────────────────────────

def _docops_root() -> Path:
    return Path(os.environ.get(
        "JENKINS_RCA_DOCS_DIR",
        str(Path(__file__).resolve().parent.parent / "docops"),
    ))


def index_docops() -> dict:
    """Walk docops/ and embed every .md file. Returns stats."""
    root = _docops_root()
    written = 0
    skipped = 0
    files   = 0
    rows: list[dict] = []
    for md in root.rglob("*.md"):
        files += 1
        rel = str(md.relative_to(root))
        # Categorize by directory.
        if rel.startswith("runbooks/"):
            stype = "runbook"
        elif rel.startswith("job_flows/"):
            stype = "job_flow"
        else:
            stype = "doc"
        text = md.read_text(errors="replace")
        chunks = _chunk_markdown(text)
        if not chunks:
            continue
        # Heading sniff for meta — first H1/H2 of the file.
        first_heading = next(
            (ln.strip("# ").strip() for ln in text.splitlines()
             if ln.startswith("#")),
            md.stem,
        )
        # Per-class meta — runbook/<class>.md becomes error_class=<class>.
        error_class = md.stem if stype == "runbook" else None

        for idx, chunk in enumerate(chunks):
            vec = embed(chunk, use_cache=True)
            rows.append({
                "source_type": stype,
                "source_id":   rel,
                "chunk_idx":   idx,
                "chunk_text":  chunk,
                "embedding":   vec,
                "meta": {
                    "title": first_heading,
                    "error_class": error_class,
                    "indexed_at": int(time.time()),
                },
            })
        # Batch into DB every 50 rows.
        if len(rows) >= 50:
            n = upsert(rows)
            written += n
            skipped += len(rows) - n
            rows.clear()
    if rows:
        n = upsert(rows)
        written += n
        skipped += len(rows) - n
    return {"files": files, "written": written, "skipped_unchanged": skipped}


def index_audits(audit_dir: str | None = None) -> dict:
    """Walk audit JSONs and embed each RCA's rationale + suggested_commands.

    Audit files are written by `jenkins_rca/audit.py:record` to
    `JENKINS_RCA_AUDIT_DIR` (default `/var/log/jenkins-rca/`). Filenames are
    UUIDs (request_id). Only files matching the UUID pattern AND
    containing a `summary` field are treated as RCA records — skips
    incidental JSONs (e.g. config snapshots, error stubs).

    Skips records older than `JENKINS_RCA_RAG_AUDIT_MAX_DAYS` (default 60)
    so the corpus doesn't bloat with stale embeddings of failures
    whose runbooks have since changed. Override via env.

    Returns: {"files": N_scanned, "written": N_upserted,
              "skipped_unchanged": N_no_change, "skipped_stale": N_old}
    """
    # Default matches where audit.py:record actually writes; legacy
    # path with `audit/` subdir still honored via env override.
    d = Path(
        audit_dir
        or os.environ.get("JENKINS_RCA_AUDIT_DIR")
        or os.environ.get("JENKINS_RCA_AUDIT_DIR", "/var/log/jenkins-rca")
    )
    if not d.is_dir():
        return {"error": f"audit dir not found: {d}",
                "files": 0, "written": 0}

    max_days = int(os.environ.get("JENKINS_RCA_RAG_AUDIT_MAX_DAYS", "60"))
    cutoff_ts = time.time() - max_days * 86400

    import re as _re
    uuid_re = _re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$"
    )

    files = 0
    written = 0
    skipped_stale = 0
    rows: list[dict] = []
    for jf in sorted(d.glob("*.json")):
        if not uuid_re.match(jf.name):
            continue  # not an RCA record
        try:
            st = jf.stat()
            if st.st_mtime < cutoff_ts:
                skipped_stale += 1
                continue
            data = json.loads(jf.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        # Real RCA payload lives at `.rca` (nested by audit.record); the
        # outer dict holds request/build metadata. Fall back to top-
        # level for older shapes (early audits before the nesting).
        rca = data.get("rca")
        if not isinstance(rca, dict):
            rca = data  # legacy / flat
        if not rca.get("summary"):
            continue  # not RCA-shaped
        files += 1
        rationale = rca.get("root_cause") or rca.get("rationale") or ""
        summary   = rca.get("summary", "")
        cmds      = rca.get("suggested_commands") or []
        cmds_str  = "\n".join(
            f"- ({c.get('tier','?')}) {c.get('cmd','')}" for c in cmds
            if isinstance(c, dict)
        )
        chunk = (
            f"# RCA {jf.stem}\n"
            f"summary: {summary}\n\n"
            f"root_cause:\n{rationale}\n\n"
            f"suggested_commands:\n{cmds_str}\n"
        )
        vec = embed(chunk, use_cache=True)
        # error_class / failed_stage may live in .rca OR top-level
        rows.append({
            "source_type": "audit",
            "source_id":   jf.name,
            "chunk_idx":   0,
            "chunk_text":  chunk,
            "embedding":   vec,
            "meta": {
                "error_class":  rca.get("error_class")
                                or data.get("error_class"),
                "failed_stage": rca.get("failed_stage"),
                "build":        data.get("build"),
                "job":          data.get("job"),
                "service":      data.get("service"),
                "indexed_at":   int(time.time()),
            },
        })
        if len(rows) >= 50:
            n = upsert(rows)
            written += n
            rows.clear()
    if rows:
        written += upsert(rows)
    return {
        "files":         files,
        "written":       written,
        "skipped_stale": skipped_stale,
    }


def index_log_window(job: str, build: int, log_window: str, error_class: str | None = None) -> dict:
    """Embed one Jenkins log error window so future builds can find similar past failures."""
    if not log_window or not log_window.strip():
        return {"written": 0, "reason": "empty log_window"}
    vec = embed(log_window, use_cache=True)
    n = upsert([{
        "source_type": "log",
        "source_id":   f"build:{job}:{build}",
        "chunk_idx":   0,
        "chunk_text":  log_window[:8000],
        "embedding":   vec,
        "meta": {
            "job": job, "build": build,
            "error_class": error_class,
            "indexed_at": int(time.time()),
        },
    }])
    return {"written": n}


# ── CLI ───────────────────────────────────────────────────────────────

def _cli_index_docops(_args: list[str]) -> int:
    print(json.dumps(index_docops(), indent=2)); return 0

def _cli_index_audits(args: list[str]) -> int:
    d = args[0] if args else None
    print(json.dumps(index_audits(d), indent=2)); return 0

def _cli_embed(args: list[str]) -> int:
    if not args: print("usage: embed <text>", file=sys.stderr); return 2
    v = embed(args[0])
    print(f"dim={len(v)} first5={v[:5]}"); return 0

def _cli_search(args: list[str]) -> int:
    if not args: print("usage: search <query> [k]", file=sys.stderr); return 2
    k = int(args[1]) if len(args) > 1 else 5
    rs = search(args[0], k=k)
    for r in rs:
        print(f"[{r['score']:.3f}] {r['source_type']}/{r['source_id']} — {r['chunk_text'][:120]}…")
    return 0

def _cli_reset(args: list[str]) -> int:
    """Empty rca_chunks + caches. Uses DELETE (not TRUNCATE) because
    the jenkins_rca DB role has table-row priv but not sequence-owner
    priv. Sequence IDs continue from where they were; gaps don't
    matter for vector lookups. Skip the cache tables if they don't
    exist (older deployments)."""
    if "--yes" not in args:
        print("refusing: pass --yes to wipe rca_chunks + caches",
              file=sys.stderr)
        return 2
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rca_chunks")
            n_chunks = cur.rowcount
            try:
                cur.execute("DELETE FROM query_emb_cache")
                n_qec = cur.rowcount
            except Exception:
                n_qec = 0
            try:
                cur.execute("DELETE FROM retrieval_cache")
                n_rc = cur.rowcount
            except Exception:
                n_rc = 0
            conn.commit()
    print(f"reset: rca_chunks={n_chunks} query_emb_cache={n_qec} "
          f"retrieval_cache={n_rc} rows deleted")
    return 0

def _cli_diag(_args: list[str]) -> int:
    """End-to-end health check. Prints PASS/FAIL per stage so operators
    can spot RAG breakage in one command. Exit non-zero on any FAIL."""
    fails = 0
    def ok(label: str)   -> None: print(f"  \033[32m✓\033[0m {label}")
    def bad(label: str)  -> None:
        nonlocal fails; fails += 1
        print(f"  \033[31m✗\033[0m {label}")
    def info(label: str) -> None: print(f"    {label}")

    print("== jenkins-rca RAG diagnostics ==\n")

    # 1. Env vars
    print("[1/6] env vars")
    missing = [k for k in ("JENKINS_RCA_PG_PASSWORD",) if not os.environ.get(k)]
    if missing:
        bad(f"missing: {', '.join(missing)} — secrets.py autoload should have set these")
    else:
        ok("JENKINS_RCA_PG_PASSWORD set")
    if not (os.environ.get("JENKINS_RCA_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        bad("JENKINS_RCA_LLM_API_KEY / OPENAI_API_KEY not set — embed will fail")
    else:
        ok("OpenAI API key set")

    # 2. PG connect
    print("\n[2/6] postgres connection")
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                ver = cur.fetchone()[0]
                ok(f"connected — {ver[:60]}…")
    except Exception as e:
        bad(f"connect failed: {e}")
        print("\nFAIL — stopping early (subsequent checks need PG)")
        return 1

    # 3. Chunks by source_type
    print("\n[3/6] chunk inventory")
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source_type, COUNT(*) FROM rca_chunks "
                    "GROUP BY source_type ORDER BY 1"
                )
                rows = cur.fetchall()
                if not rows:
                    bad("rca_chunks is EMPTY — run `python -m jenkins_rca.rag index-docops`")
                else:
                    for stype, cnt in rows:
                        marker = ok if cnt > 0 else bad
                        marker(f"{stype}: {cnt} chunks")
                    have = {r[0] for r in rows}
                    for needed in ("runbook", "job_flow", "doc"):
                        if needed not in have:
                            bad(f"no {needed} chunks — indexer never ran or excludes "
                                f"{needed}/ subfolder")
    except Exception as e:
        bad(f"query failed: {e}")

    # 4. Cache tables exist + counts
    print("\n[4/6] cache tables")
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                for tbl in ("query_emb_cache", "retrieval_cache"):
                    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                    cnt = cur.fetchone()[0]
                    ok(f"{tbl}: {cnt} rows")
    except Exception as e:
        bad(f"cache table check failed: {e}")

    # 5. Embed → search round-trip (3 known queries).
    # Per docops/MAP.md: a single failure surfaces relevant content in
    # BOTH the runbook (class drill) AND the job_flow (pipeline
    # identity), so a query may legitimately top-hit EITHER. PASS if
    # any source_id in top-3 matches one of the expected substrings.
    print("\n[5/6] semantic search smoke test")
    tests = [
        ("slave-4 seems to be removed or offline",
         ["jenkins_agent_offline", "stagger_scaling"]),
        ("Gradle build daemon disappeared OOM",
         ["build_tool_crash"]),
        ("config.json not found create-quick-infra gate",
         ["compliance", "create_quick_infra"]),
    ]
    for query, expect_any in tests:
        try:
            hits = search(query, k=3)
            if not hits:
                bad(f"\"{query[:40]}…\" → 0 hits")
                continue
            top_ids = [h["source_id"] for h in hits]
            matched = next(
                (sid for sid in top_ids
                 if any(sub in sid for sub in expect_any)),
                None,
            )
            top_score = hits[0]["score"]
            if matched:
                ok(f"\"{query[:40]}…\" → {matched} "
                   f"(top score={top_score:.3f})")
            else:
                bad(f"\"{query[:40]}…\" → top-3: {top_ids} "
                    f"(none contained any of {expect_any})")
        except Exception as e:
            bad(f"\"{query[:40]}…\" → error: {e}")

    # 6. Auto-inject wiring (sanity — check the helper exists + is importable)
    print("\n[6/6] auto-inject wiring")
    try:
        from .agent import _rag_inject_for_agent  # noqa: F401
        ok("agent.py: _rag_inject_for_agent importable")
    except Exception as e:
        bad(f"agent.py: _rag_inject_for_agent NOT importable: {e}")
    try:
        from .llm import _build_tool_context  # noqa: F401
        ok("llm.py: _build_tool_context importable (one-shot path)")
    except Exception as e:
        bad(f"llm.py: _build_tool_context NOT importable: {e}")

    print()
    if fails:
        print(f"\033[31m== FAIL — {fails} check(s) broken ==\033[0m")
        return 1
    print("\033[32m== ALL PASS — RAG wired + serving ==\033[0m")
    return 0


_CLI = {
    "embed":         _cli_embed,
    "search":        _cli_search,
    "index-docops":  _cli_index_docops,
    "index-audits":  _cli_index_audits,
    "reset":         _cli_reset,
    "diag":          _cli_diag,
}


def _autoload_secrets_if_missing() -> None:
    """When run interactively (e.g. `python -m jenkins_rca.rag index-docops`),
    the operator's shell typically has no JENKINS_RCA_PG_* / JENKINS_RCA_LLM_API_KEY
    env vars — those are loaded by `jenkins-rca-start.sh` via secrets.py
    when systemd starts the service. Fetching them from AWS Secrets
    Manager here saves the operator from manually sourcing secrets
    before every CLI run. Silently no-op when already set.

    Behavior:
      - If JENKINS_RCA_PG_PASSWORD AND JENKINS_RCA_LLM_API_KEY (or OPENAI_API_KEY)
        are already set → skip (env wins).
      - Otherwise call `secrets.load_secrets()` and export all keys as
        `JENKINS_RCA_<KEY_UPPER>` env vars.
      - Any failure (no IAM role, no secret, boto3 missing) is logged
        to stderr but does NOT raise — the existing RuntimeError at
        `_connect` will fire with the original guidance.
    """
    if os.environ.get("JENKINS_RCA_PG_PASSWORD") and (
        os.environ.get("JENKINS_RCA_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    ):
        return
    try:
        from . import secrets as _secrets
        for k, v in _secrets.load_secrets().items():
            env_key = f"JENKINS_RCA_{k.upper()}"
            # Don't overwrite anything the operator explicitly set.
            if env_key not in os.environ:
                os.environ[env_key] = str(v)
    except Exception as _e:
        print(
            f"[rag] secrets autoload skipped ({_e}); "
            f"set JENKINS_RCA_PG_PASSWORD + JENKINS_RCA_LLM_API_KEY manually if the "
            f"next call fails",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print("usage: python -m jenkins_rca.rag <cmd> [args...]")
        print("  commands: " + ", ".join(_CLI))
        return 0
    cmd = args.pop(0)
    fn = _CLI.get(cmd)
    if not fn:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    _autoload_secrets_if_missing()
    return fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
