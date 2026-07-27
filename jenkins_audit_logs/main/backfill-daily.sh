#!/bin/bash
##############################################################
# Jenkins Audit Log — Historical Backfill to per-day S3 objects
#
# ONE-SHOT script. Run on each Jenkins EC2 after switching to
# daily-delta merge-audit-logs.sh.
#
# What it does:
#   1. Read audit-combined-all.log (already contains every audit line
#      ever rotated through this server, via prior merge cron runs) plus
#      the current live audit.log + audit.log.0 (most recent rotated).
#   2. Bucket each line by its own timestamp (Audit Trail format
#      "Mon DD, YYYY ...").
#   3. For each date: gzip the bucket file and OVERWRITE the per-day S3
#      object — the local bucketed content IS the truth for that date.
#      Pre-existing S3 objects (whatever format they had) get replaced.
#   4. Save baseline snapshot at audit-logs/.audit-prev-snapshot.log so
#      the daily-delta cron has a clean diff target starting from this run.
#
# NOTE: Avoid scanning /var/log/jenkins/ with find — Audit Trail plugin
# rotation can produce tens of thousands of files (audit.log.0.1.1.1...)
# and find stalls. Older content is already in audit-combined-all.log.
#
# Safe to re-run. Idempotent.
##############################################################

set -euo pipefail

ARCHIVE_DIR="/var/lib/jenkins/jenkins-1year-archive"
COMBINED="$ARCHIVE_DIR/audit-logs/audit-combined-all.log"
PREV_SNAP="$ARCHIVE_DIR/audit-logs/.audit-prev-snapshot.log"
LOG="$ARCHIVE_DIR/backfill-daily-$(date -u +%Y%m%d_%H%M%S).log"

# Hardcoded per env. Change on bbfinserv/tzf accordingly.
S3_BUCKET="bb-jenkins-audit-logs"
S3_PREFIX="zinka-divum"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts): $*" | tee -a "$LOG"; }

WORK=$(mktemp -d)
ALL="$WORK/all.log"

log "Backfill start — env=$S3_PREFIX"

# ------------------------------------------------------------
# 1. Collect: combined-all.log + current live + most recent rotated
# ------------------------------------------------------------
cat "$COMBINED" 2>/dev/null > "$ALL" || true
cat /var/log/jenkins/audit.log /var/log/jenkins/audit.log.0 2>/dev/null >> "$ALL" || true

log "Raw lines collected: $(wc -l < "$ALL")"
sort -u "$ALL" -o "$ALL"
log "After dedup: $(wc -l < "$ALL")"

# ------------------------------------------------------------
# 2. Bucket by line timestamp
# ------------------------------------------------------------
BUCKETS="$WORK/by-day"
mkdir -p "$BUCKETS"

awk -v BD="$BUCKETS" '
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
}' "$ALL"

log "Buckets created: $(ls "$BUCKETS" | wc -l)"

# ------------------------------------------------------------
# 3. Upload each bucket — OVERWRITE existing S3 object
#    (bucket content IS the truth for that date)
# ------------------------------------------------------------
for f in "$BUCKETS"/*.log; do
    [ -f "$f" ] || continue
    DATE=$(basename "$f" .log)

    if [ "$DATE" = "unknown" ]; then
        KEY="${S3_PREFIX}/unknown/audit-unknown-backfill-$(date -u +%Y%m%d_%H%M%S).log.gz"
        gzip -c "$f" > "$WORK/upload.gz"
        aws s3 cp "$WORK/upload.gz" "s3://${S3_BUCKET}/${KEY}" \
            --storage-class STANDARD_IA 2>&1 | tee -a "$LOG"
        rm -f "$WORK/upload.gz"
        log "unknown bucket -> ${KEY} ($(wc -l < "$f") lines)"
        continue
    fi

    YEAR="${DATE%%-*}"
    MONTH=$(echo "$DATE" | cut -d- -f2)
    KEY="${S3_PREFIX}/${YEAR}/${MONTH}/audit-${DATE}.log.gz"
    CNT=$(wc -l < "$f")

    gzip -c "$f" > "$WORK/audit-${DATE}.log.gz"
    aws s3 cp "$WORK/audit-${DATE}.log.gz" "s3://${S3_BUCKET}/${KEY}" \
        --storage-class STANDARD_IA \
        --metadata "content=daily-events,total-lines=${CNT},backfill=true,source-host=$(hostname)" \
        2>&1 | tee -a "$LOG" >/dev/null

    log "bucket=${DATE} lines=${CNT} -> ${KEY}"
    rm -f "$WORK/audit-${DATE}.log.gz"
done

# ------------------------------------------------------------
# 4. Save baseline snapshot for daily-delta cron
# ------------------------------------------------------------
cp -f "$ALL" "$COMBINED"
cp -f "$ALL" "$PREV_SNAP"
log "Baseline snapshot saved at: $PREV_SNAP ($(wc -l < "$PREV_SNAP") lines)"

# ------------------------------------------------------------
# 5. Upload latest pointer = full snapshot
# ------------------------------------------------------------
gzip -c "$COMBINED" > "$WORK/audit-combined-latest.log.gz"
aws s3 cp "$WORK/audit-combined-latest.log.gz" \
    "s3://${S3_BUCKET}/${S3_PREFIX}/audit-combined-latest.log.gz" \
    --storage-class STANDARD_IA 2>&1 | tee -a "$LOG" >/dev/null

rm -rf "$WORK"
log "Backfill done. Verify: aws s3 ls s3://${S3_BUCKET}/${S3_PREFIX}/ --recursive | head -20"
