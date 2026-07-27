#!/bin/bash

#################################################################
# Jenkins 1-Year Archive Script
# Purpose: Archive all logs/configs at month-end
# Schedule: 1st of month at 00:00 (via cron)
# Retention: 12 months (365+ days)
#################################################################

set -e  # Exit on error

# Load configuration
source ~/jenkins-archive-config.sh 2>/dev/null || {
  echo "ERROR: Cannot load jenkins-archive-config.sh"
  exit 1
}

MONTH=$(date -u +%Y-%m)
TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S")
YEAR=$(date -u +%Y)

# Create monthly directories
mkdir -p "$ARCHIVE_PATH/audit-logs/$MONTH"
mkdir -p "$ARCHIVE_PATH/config-history/$MONTH"
mkdir -p "$ARCHIVE_PATH/permission-snapshots"
mkdir -p "$ARCHIVE_PATH/checksums"
mkdir -p "$ARCHIVE_PATH/metadata"

# Log file for this run
LOG_FILE="$ARCHIVE_PATH/monthly-reports/archive-$MONTH-$TIMESTAMP.log"
mkdir -p "$(dirname "$LOG_FILE")"

{
  echo "════════════════════════════════════════════════════════════════"
  echo "📦 Jenkins Monthly Archive - $MONTH"
  echo "════════════════════════════════════════════════════════════════"
  echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo ""

  # ─────────────────────────────────────────────────────────────────
  # 1. ARCHIVE AUDIT LOGS
  # ─────────────────────────────────────────────────────────────────
  
  echo "📝 Archiving audit logs..."
  
  if [ -f "$AUDIT_LOG_PATH" ] || [ -f "${AUDIT_LOG_PATH}.1" ]; then
    # Copy all current audit log files
    cp ${AUDIT_LOG_PATH}* "$ARCHIVE_PATH/audit-logs/$MONTH/" 2>/dev/null || true
    
    # Count entries
    AUDIT_COUNT=$(find "$ARCHIVE_PATH/audit-logs/$MONTH" -type f | wc -l)
    echo "   ✓ Audit logs: $AUDIT_COUNT files"
  else
    echo "   ⚠️  No audit logs found"
    AUDIT_COUNT=0
  fi
  
  # ─────────────────────────────────────────────────────────────────
  # 2. ARCHIVE JOB CONFIG HISTORY
  # ─────────────────────────────────────────────────────────────────
  
  echo "⚙️  Archiving job configuration history..."
  
  JOB_COUNT=0
  CONFIG_COUNT=0
  
  for job_dir in "$JENKINS_JOBS_DIR"/*/; do
    if [ -d "$job_dir" ]; then
      JOB_NAME=$(basename "$job_dir")
      
      # Create job directory
      mkdir -p "$ARCHIVE_PATH/config-history/$MONTH/$JOB_NAME"
      
      # Copy config.xml
      if [ -f "${job_dir}config.xml" ]; then
        cp "${job_dir}config.xml" "$ARCHIVE_PATH/config-history/$MONTH/$JOB_NAME/" 2>/dev/null || true
        ((CONFIG_COUNT++))
      fi
      
      # Copy configHistory directory
      if [ -d "${job_dir}configHistory" ]; then
        cp -r "${job_dir}configHistory" "$ARCHIVE_PATH/config-history/$MONTH/$JOB_NAME-history/" 2>/dev/null || true
      fi
      
      ((JOB_COUNT++))
    fi
  done
  
  echo "   ✓ Job configs: $JOB_COUNT jobs, $CONFIG_COUNT config files"
  
  # ─────────────────────────────────────────────────────────────────
  # 3. CREATE PERMISSION SNAPSHOT
  # ─────────────────────────────────────────────────────────────────
  
  echo "📸 Creating permission snapshot..."
  
  SNAP_FILE="$ARCHIVE_PATH/permission-snapshots/${MONTH}-permissions.txt"
  
  {
    echo "=== Permission Snapshot for $MONTH ==="
    echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo ""
    
    for job_dir in "$JENKINS_JOBS_DIR"/*/; do
      JOB_NAME=$(basename "$job_dir")
      CONFIG_FILE="${job_dir}config.xml"
      
      if [ -f "$CONFIG_FILE" ]; then
        echo "Job: $JOB_NAME"
        # Extract authorization matrix (job-level permissions)
        grep -o "permission>[^<]*<" "$CONFIG_FILE" 2>/dev/null | sed 's/permission>//; s/<$//' || true
        echo ""
      fi
    done
  } > "$SNAP_FILE"
  
  echo "   ✓ Permission snapshot created"
  
  # ─────────────────────────────────────────────────────────────────
  # 4. GENERATE CHECKSUMS
  # ─────────────────────────────────────────────────────────────────
  
  echo "🔐 Generating checksums..."
  
  CHECKSUM_FILE="$ARCHIVE_PATH/checksums/${MONTH}-checksums.sha256"
  
  cd "$ARCHIVE_PATH"
  find "audit-logs/$MONTH" "config-history/$MONTH" "permission-snapshots/${MONTH}-permissions.txt" \
    -type f 2>/dev/null | xargs sha256sum > "$CHECKSUM_FILE" 2>/dev/null || true
  
  CHECKSUM_COUNT=$(wc -l < "$CHECKSUM_FILE" 2>/dev/null || echo "0")
  echo "   ✓ Checksums: $CHECKSUM_COUNT files"
  
  # ─────────────────────────────────────────────────────────────────
  # 5. CREATE METADATA FILE
  # ─────────────────────────────────────────────────────────────────
  
  echo "📄 Creating metadata..."
  
  METADATA_FILE="$ARCHIVE_PATH/metadata/${MONTH}-metadata.json"
  
  cat > "$METADATA_FILE" << EOF
{
  "archive_month": "$MONTH",
  "created_timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "archive_location": "$ARCHIVE_PATH",
  "contents": {
    "audit_logs": {
      "location": "audit-logs/$MONTH",
      "files": $AUDIT_COUNT
    },
    "config_history": {
      "location": "config-history/$MONTH",
      "jobs": $JOB_COUNT,
      "files": $CONFIG_COUNT
    },
    "permission_snapshot": {
      "location": "permission-snapshots/${MONTH}-permissions.txt"
    }
  },
  "checksums": "$CHECKSUM_FILE",
  "retention_days": 365
}
EOF
  
  echo "   ✓ Metadata file created"
  
  # ─────────────────────────────────────────────────────────────────
  # 6. CALCULATE TOTALS
  # ─────────────────────────────────────────────────────────────────
  
  echo ""
  echo "════════════════════════════════════════════════════════════════"
  echo "✓ ARCHIVE COMPLETE"
  echo "════════════════════════════════════════════════════════════════"
  echo ""
  echo "Month: $MONTH"
  echo "Audit logs: $AUDIT_COUNT files"
  echo "Job configs: $JOB_COUNT jobs"
  echo "Config files: $CONFIG_COUNT files"
  echo "Checksums: $CHECKSUM_COUNT files"
  echo ""
  
  TOTAL_SIZE=$(du -sh "$ARCHIVE_PATH" | awk '{print $1}')
  echo "Total archive size: $TOTAL_SIZE"
  echo "Retention until: $(date -u -d '+365 days' +%Y-%m-%d)"
  echo ""
  
  echo "Completed: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  
} | tee "$LOG_FILE"

echo ""
echo "Log saved to: $LOG_FILE"

