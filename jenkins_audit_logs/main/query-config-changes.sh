#!/bin/bash

echo "════════════════════════════════════════════════════════════════"
echo "All Configuration Changes"
echo "════════════════════════════════════════════════════════════════"
echo ""

echo "By action type:"
echo ""
echo "Config Submits: $(grep -c 'configSubmit' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"
echo "Job Creates: $(grep -c 'createItem' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"
echo "Job Deletes: $(grep -c 'doDelete' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"
echo "Rebuilds: $(grep -c 'rebuild' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"

echo ""
echo "Recent configuration changes:"
grep "configSubmit" /var/log/jenkins/audit.log* 2>/dev/null | tail -10

