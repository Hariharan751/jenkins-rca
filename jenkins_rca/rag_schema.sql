-- bbctl-rca RAG store schema.
-- Run by infra/scripts/rag-postgres-install.sh against the bbctl_rca DB.
-- Idempotent — safe to re-apply.
--
-- Tables
--   rca_chunks         — embedded text chunks (runbooks, org docs, audits, log windows).
--   query_emb_cache    — cache of (log_window → embedding) to skip re-embedding repeat fingerprints.
--   retrieval_cache    — cache of (query_embedding → top-k chunk ids) for back-to-back calls.
--
-- All three are bounded by TTL columns; an indexer cron prunes old rows.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS rca_chunks (
  id            BIGSERIAL PRIMARY KEY,
  source_type   TEXT NOT NULL CHECK (source_type IN ('runbook', 'doc', 'job_flow', 'audit', 'log')),
  source_id     TEXT NOT NULL,
  chunk_idx     INT  NOT NULL DEFAULT 0,
  chunk_text    TEXT NOT NULL,
  embedding     VECTOR(1536) NOT NULL,
  meta          JSONB NOT NULL DEFAULT '{}'::jsonb,
  content_hash  TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_type, source_id, chunk_idx)
);

-- HNSW index for fast approximate-NN search on the embedding column.
-- cosine ops (vector_cosine_ops) is the right choice for OpenAI embeddings;
-- they are unit-norm and cosine is what their docs recommend.
CREATE INDEX IF NOT EXISTS rca_chunks_emb_hnsw
  ON rca_chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- GIN index on meta for cheap filter-by-class / filter-by-source_type before vector rank.
CREATE INDEX IF NOT EXISTS rca_chunks_meta_gin
  ON rca_chunks USING gin (meta);

-- BM25-ish keyword fallback / hybrid: full-text index on chunk_text.
CREATE INDEX IF NOT EXISTS rca_chunks_text_fts
  ON rca_chunks USING gin (to_tsvector('english', chunk_text));

CREATE INDEX IF NOT EXISTS rca_chunks_source_type_idx
  ON rca_chunks (source_type);

-- Query embedding cache. Key = sha256 of the normalized log_window text.
-- Avoids re-paying for the same query embedding when a build flaps and
-- re-runs the same RCA shortly after.
CREATE TABLE IF NOT EXISTS query_emb_cache (
  query_hash    TEXT PRIMARY KEY,
  embedding     VECTOR(1536) NOT NULL,
  ttl_expires_at TIMESTAMPTZ NOT NULL,
  hits          INT NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS query_emb_cache_ttl
  ON query_emb_cache (ttl_expires_at);

-- Retrieval result cache. Key = sha256(query_hash + filters + top_k).
-- Skips the PG vector search round-trip when we just computed the same
-- neighbors for the same query.
CREATE TABLE IF NOT EXISTS retrieval_cache (
  cache_key      TEXT PRIMARY KEY,
  chunk_ids      BIGINT[] NOT NULL,
  scores         REAL[] NOT NULL,
  ttl_expires_at TIMESTAMPTZ NOT NULL,
  hits           INT NOT NULL DEFAULT 0,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS retrieval_cache_ttl
  ON retrieval_cache (ttl_expires_at);
