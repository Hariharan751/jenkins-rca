#!/bin/bash
# Postgres 16 + pgvector install for jenkins-rca RAG store on Ubuntu EC2.
# Idempotent — safe to re-run.
#
# What this does:
#   1. Install Postgres 16 from PGDG apt repo.
#   2. Install pgvector extension from source (PGDG package is sometimes
#      lagging on a stable channel; building takes ~30s and is the
#      most reliable way to land matching version for PG 16).
#   3. Create `jenkins_rca` database + role with a generated password.
#   4. Enable pgvector + pg_trgm extensions in that database.
#   5. Stash the role password in AWS Secrets Manager under
#      `jenkins-rca/prod` -> `pg_password` so the service reads it the same
#      way it reads jenkins / github / llm credentials.
#
# Run as the ubuntu user on the jenkins-rca EC2 host:
#   sudo bash Jenkins-rca/infra/scripts/rag-postgres-install.sh
#
# Pre-reqs: AWS CLI configured with a write profile for Secrets Manager
# (the readonly profile cannot PutSecretValue). The script will use
# whatever profile is in $AWS_PROFILE; set it before invoking.

set -euo pipefail

PG_VERSION="${PG_VERSION:-16}"
DB_NAME="${DB_NAME:-jenkins_rca}"
DB_USER="${DB_USER:-jenkins_rca}"
SECRET_ID="${SECRET_ID:-jenkins-rca/prod}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

log() { echo "[rag-pg-install] $*" >&2; }

if [[ $EUID -ne 0 ]]; then
  log "must run as root (use sudo)"
  exit 1
fi

log "step 1 — install Postgres ${PG_VERSION} from PGDG"
if ! command -v psql >/dev/null 2>&1; then
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -y
  apt-get install -y "postgresql-${PG_VERSION}" "postgresql-server-dev-${PG_VERSION}" build-essential git
else
  log "psql already present — skipping apt install"
fi

systemctl enable --now "postgresql@${PG_VERSION}-main" || systemctl enable --now postgresql

log "step 2 — build + install pgvector"
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_available_extensions WHERE name='vector'" | grep -q 1; then
  TMP="$(mktemp -d)"
  pushd "$TMP" >/dev/null
  git clone --depth 1 --branch v0.8.0 https://github.com/pgvector/pgvector.git
  cd pgvector
  make PG_CONFIG="/usr/lib/postgresql/${PG_VERSION}/bin/pg_config"
  make install PG_CONFIG="/usr/lib/postgresql/${PG_VERSION}/bin/pg_config"
  popd >/dev/null
  rm -rf "$TMP"
else
  log "pgvector already installed"
fi

log "step 3 — create database + role"
DB_PASSWORD="$(openssl rand -hex 24)"
sudo -u postgres psql <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}';
  ELSE
    ALTER ROLE ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';
  END IF;
END
\$\$;
SQL

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
fi

log "step 4 — enable extensions + apply schema"
sudo -u postgres psql -d "${DB_NAME}" -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL

# Apply rag schema (in repo). Path is fixed relative to this script.
SCHEMA_PATH="$(dirname "$(readlink -f "$0")")/../../jenkins_rca/rag_schema.sql"
if [[ -f "$SCHEMA_PATH" ]]; then
  sudo -u postgres psql -d "${DB_NAME}" -v ON_ERROR_STOP=1 -f "$SCHEMA_PATH"
  sudo -u postgres psql -d "${DB_NAME}" -c "GRANT ALL ON SCHEMA public TO ${DB_USER}; GRANT ALL ON ALL TABLES IN SCHEMA public TO ${DB_USER}; GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO ${DB_USER};"
else
  log "WARN: schema file not found at $SCHEMA_PATH — skipping schema apply"
fi

log "step 5 — stash password in Secrets Manager (${SECRET_ID})"
if command -v aws >/dev/null 2>&1; then
  EXISTING="$(aws secretsmanager get-secret-value --secret-id "${SECRET_ID}" --region "${AWS_REGION}" --query SecretString --output text 2>/dev/null || echo '{}')"
  MERGED="$(printf '%s' "$EXISTING" | jq --arg p "$DB_PASSWORD" --arg h "127.0.0.1" --arg d "$DB_NAME" --arg u "$DB_USER" '. + {pg_password: $p, pg_host: $h, pg_db: $d, pg_user: $u, pg_port: "5432"}')"
  aws secretsmanager put-secret-value --secret-id "${SECRET_ID}" --secret-string "$MERGED" --region "${AWS_REGION}" >/dev/null
  log "secret updated"
else
  log "WARN: aws CLI not present — password NOT stashed. Manual step:"
  log "  pg_password=${DB_PASSWORD}"
fi

log "done. Verify with:"
log "  sudo -u postgres psql -d ${DB_NAME} -c '\\dx'"
log "  sudo -u postgres psql -d ${DB_NAME} -c '\\dt'"
