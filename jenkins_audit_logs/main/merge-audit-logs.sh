#!/bin/bash
##############################################################
# Jenkins Audit Log Merge + S3 Upload — DAILY DELTA mode
# Cron: 0 1 * * * (daily 1AM UTC)
#
# Behavior:
#  - Reads /var/log/jenkins/audit.log + audit.log.0 (skips deep rotation
#    chain — older rotated content is already in audit-combined-all.log)
#  - Computes delta vs previous run's snapshot
#  - Buckets delta lines by their own timestamp (Audit Trail format:
#    "Mon DD, YYYY H:MM:SS ...")
#  - For each date bucket: pulls existing S3 object, merges (sort -u),
#    re-uploads. Idempotent across cron misses or re-runs.
#  - Keeps audit-combined-latest.log.gz as the full state.
#
# S3 layout (single shared bucket, per-env prefix):
#   s3://bb-jenkins-audit-logs/<env>/<YYYY>/<MM>/audit-<YYYY-MM-DD>.log.gz  (per-day)
#   s3://bb-jenkins-audit-logs/<env>/audit-combined-latest.log.gz          (full)
#
# <env> on each Jenkins EC2: bbfinserv | tzf | zinka-divum
##############################################################

set -u

ARCHIVE_DIR="/var/lib/jenkins/jenkins-1year-archive"
COMBINED="$ARCHIVE_DIR/audit-logs/audit-combined-all.log"
PREV_SNAP="$ARCHIVE_DIR/audit-logs/.audit-prev-snapshot.log"
MERGE_LOG="$ARCHIVE_DIR/merge-audit-logs.log"
TEMP="/tmp/audit-merge-temp.log"
DELTA="/tmp/audit-delta.log"

# Hardcoded per env. Change on bbfinserv/tzf EC2s accordingly.
S3_BUCKET="bb-jenkins-audit-logs"
S3_PREFIX="zinka-divum"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts): $*" >> "$MERGE_LOG"; }

# ------------------------------------------------------------
# STEP 1: Merge live + rotated logs into COMBINED (dedup, sort)
# ------------------------------------------------------------
: > "$TEMP"
# Audit Trail plugin rotation chain in /var/log/jenkins/ can grow to tens of
# thousands of files (audit.log.0.1.1.1.1.1.1.gz...). Scanning it with `find`
# stalls. Older lines from rotated content are already merged into
# audit-combined-all.log by previous runs, so we only need the current live
# file + the most recent rotated buffer.
cat /var/log/jenkins/audit.log /var/log/jenkins/audit.log.0 2>/dev/null >> "$TEMP" || true

cat "$COMBINED" "$TEMP" 2>/dev/null | sort -u > /tmp/audit-final.log
mv -f /tmp/audit-final.log "$COMBINED"
rm -f "$TEMP"

TOTAL=$(wc -l < "$COMBINED")
log "Merged. Total lines: $TOTAL"

# ------------------------------------------------------------
# STEP 2: Compute delta vs previous snapshot
# ------------------------------------------------------------
if [ -f "$PREV_SNAP" ]; then
    comm -23 <(sort "$COMBINED") <(sort "$PREV_SNAP") > "$DELTA"
else
    cp "$COMBINED" "$DELTA"
    log "No previous snapshot — first run, delta = full history"
fi

NEW_LINES=$(wc -l < "$DELTA")
log "Delta lines (new since last run): $NEW_LINES"

if [ "$NEW_LINES" -eq 0 ]; then
    log "Nothing to upload. Skipping S3 PUT."
    rm -f "$DELTA"
    exit 0
fi

# ------------------------------------------------------------
# STEP 3: Bucket delta by line timestamp
#   Audit Trail format: "Mar 18, 2026 12:24:01 ..."
# ------------------------------------------------------------
BUCKET_DIR=$(mktemp -d)
awk -v BD="$BUCKET_DIR" '
{
  months="JanFebMarAprMayJunJulAugSepOctNovDec"
  if (match($0, /^[A-Z][a-z]{2} [0-9]{1,2}, [0-9]{4}/)) {
    mon=substr($0,1,3)
    rest=substr($0,5)
    split(rest, parts, ",")
    day=parts[1] + 0
    year=substr(rest, index(rest, ",")+2, 4)
    mm=sprintf("%02d", (index(months,mon)+2)/3)
    dd=sprintf("%02d", day)
    key=year "-" mm "-" dd
    print >> (BD "/" key ".log")
  } else {
    print >> (BD "/unknown.log")
  }
}' "$DELTA"

# ------------------------------------------------------------
# STEP 4: For each date bucket — merge with existing S3 object, re-upload
# ------------------------------------------------------------
for f in "$BUCKET_DIR"/*.log; do
    [ -f "$f" ] || continue
    DATE=$(basename "$f" .log)

    if [ "$DATE" = "unknown" ]; then
        KEY="${S3_PREFIX}/unknown/audit-unknown-$(date -u +%Y%m%d_%H%M%S).log.gz"
        gzip -c "$f" > "/tmp/upload-unknown.gz"
        aws s3 cp "/tmp/upload-unknown.gz" "s3://${S3_BUCKET}/${KEY}" \
            --storage-class STANDARD_IA 2>> "$MERGE_LOG"
        rm -f /tmp/upload-unknown.gz
        log "Uploaded unknown-timestamp bucket: s3://${S3_BUCKET}/${KEY} ($(wc -l < "$f") lines)"
        continue
    fi

    YEAR="${DATE%%-*}"
    MONTH=$(echo "$DATE" | cut -d- -f2)
    KEY="${S3_PREFIX}/${YEAR}/${MONTH}/audit-${DATE}.log.gz"
    EXISTING_GZ="/tmp/existing-${DATE}.log.gz"
    EXISTING="/tmp/existing-${DATE}.log"
    MERGED="/tmp/merged-${DATE}.log"

    # Pull existing object if present
    if aws s3 cp "s3://${S3_BUCKET}/${KEY}" "$EXISTING_GZ" 2>/dev/null; then
        gunzip -f "$EXISTING_GZ"
        cat "$EXISTING" "$f" | sort -u > "$MERGED"
        rm -f "$EXISTING"
    else
        sort -u "$f" > "$MERGED"
    fi

    NEW_IN_BUCKET=$(wc -l < "$f")
    AFTER=$(wc -l < "$MERGED")

    gzip -c "$MERGED" > "/tmp/audit-${DATE}.log.gz"
    aws s3 cp "/tmp/audit-${DATE}.log.gz" "s3://${S3_BUCKET}/${KEY}" \
        --storage-class STANDARD_IA \
        --metadata "content=daily-events,total-lines=${AFTER},source-host=$(hostname)" \
        2>> "$MERGE_LOG"

    log "bucket=${DATE} delta-added=${NEW_IN_BUCKET} total=${AFTER} -> ${KEY}"

    rm -f "$MERGED" "/tmp/audit-${DATE}.log.gz"
done

rm -rf "$BUCKET_DIR" "$DELTA"

# ------------------------------------------------------------
# STEP 5: Upload latest pointer = full snapshot
# ------------------------------------------------------------
gzip -c "$COMBINED" > "/tmp/audit-combined-latest.log.gz"
aws s3 cp "/tmp/audit-combined-latest.log.gz" \
    "s3://${S3_BUCKET}/${S3_PREFIX}/audit-combined-latest.log.gz" \
    --storage-class STANDARD_IA 2>> "$MERGE_LOG"
rm -f /tmp/audit-combined-latest.log.gz
log "Updated latest pointer: s3://${S3_BUCKET}/${S3_PREFIX}/audit-combined-latest.log.gz"

# ------------------------------------------------------------
# STEP 6: Save current snapshot as baseline for next delta
# ------------------------------------------------------------
cp -f "$COMBINED" "$PREV_SNAP"

# ------------------------------------------------------------
# STEP 7: Auto-lock previous month's archive dir (immutable)
# ------------------------------------------------------------
PREV_MONTH=$(date -u -d "1 month ago" +%Y-%m 2>/dev/null || date -u -v-1m +%Y-%m)
PREV_AUDIT="${ARCHIVE_DIR}/audit-logs/${PREV_MONTH}"

if [ -d "$PREV_AUDIT" ]; then
    if ! lsattr -d "$PREV_AUDIT" 2>/dev/null | grep -q "^....i"; then
        if sudo /usr/bin/chattr -R +i "$PREV_AUDIT" 2>/dev/null; then
            log "Locked immutable: $PREV_AUDIT"
        else
            log "chattr FAILED on $PREV_AUDIT"
        fi
    fi
fi
