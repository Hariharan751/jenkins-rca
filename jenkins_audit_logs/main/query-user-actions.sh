#!/bin/bash

if [ -z "$1" ]; then
  echo "Usage: $0 <username>"
  echo "Example: $0 'Aman Goyal'"
  exit 1
fi

USERNAME="$1"
echo "════════════════════════════════════════════════════════════════"
echo "All actions by: $USERNAME"
echo "════════════════════════════════════════════════════════════════"
echo ""

# Search all audit logs
COUNT=$(grep -i "by $USERNAME\|user $USERNAME" /var/log/jenkins/audit.log* 2>/dev/null | wc -l)
echo "Total actions: $COUNT"
echo ""

echo "Recent actions:"
grep -i "by $USERNAME\|user $USERNAME" /var/log/jenkins/audit.log* 2>/dev/null | tail -20

echo ""
echo "Action breakdown:"
grep -i "by $USERNAME\|user $USERNAME" /var/log/jenkins/audit.log* 2>/dev/null | \
  grep -oE "(Started by|configSubmit|createItem|doDelete|rebuild)" | \
  sort | uniq -c | sort -rn

