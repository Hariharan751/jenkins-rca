#!/bin/bash

REPORT_FILE="jenkins-audit-report-$(date +%Y%m%d).txt"

{
  echo "╔════════════════════════════════════════════════════════════════╗"
  echo "║        JENKINS 1-YEAR AUDIT COMPLIANCE REPORT                  ║"
  echo "╚════════════════════════════════════════════════════════════════╝"
  echo ""
  echo "Report Date: $(date)"
  echo ""
  
  echo "TOTAL AUDIT LOG ENTRIES:"
  cat /var/log/jenkins/audit.log* 2>/dev/null | wc -l
  echo ""
  
  echo "USERS WHO MADE CHANGES:"
  grep -oh "by [^ ]*\|user [^ ]*" /var/log/jenkins/audit.log* 2>/dev/null | \
    sed 's/by //; s/user //' | sort | uniq -c | sort -rn | head -20
  echo ""
  
  echo "ACTIONS BREAKDOWN:"
  echo "Config Changes: $(grep -c 'configSubmit' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"
  echo "Job Creates: $(grep -c 'createItem' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"
  echo "Job Deletes: $(grep -c 'doDelete' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"
  echo "Rebuilds: $(grep -c 'rebuild' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"
  echo ""
  
  echo "RECENT CHANGES:"
  tail -30 /var/log/jenkins/audit.log 2>/dev/null
  echo ""
  
  echo "ARCHIVE STATUS:"
  du -sh /var/lib/jenkins/jenkins-1year-archive 2>/dev/null || echo "Archive not found"

} > "$REPORT_FILE"

cat "$REPORT_FILE"
echo ""
echo "Report saved to: $REPORT_FILE"

