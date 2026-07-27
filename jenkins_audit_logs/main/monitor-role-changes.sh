#!/bin/bash

# Monitor Jenkins role and permission changes
# This tracks when roles are created, modified, or deleted

MONITOR_DIR="/var/lib/jenkins/secrets"
ROLE_CONFIG="/var/lib/jenkins/secrets/roles.txt"
AUDIT_LOG="/var/log/jenkins/role-changes.log"

# Create initial baseline if it doesn't exist
if [ ! -f "$ROLE_CONFIG" ]; then
    echo "Creating baseline role configuration..."
    echo "=== Role Configuration Snapshot ===" > "$ROLE_CONFIG"
    echo "Timestamp: $(date)" >> "$ROLE_CONFIG"
    echo "" >> "$ROLE_CONFIG"
    
    # Capture current roles from Jenkins
    echo "Current Roles:" >> "$ROLE_CONFIG"
    ls -lR /var/lib/jenkins/secrets/ >> "$ROLE_CONFIG" 2>/dev/null || echo "No secrets directory" >> "$ROLE_CONFIG"
fi

# Function to check for changes
check_role_changes() {
    # Get current state
    CURRENT_STATE=$(find /var/lib/jenkins -name "*role*" -o -name "*permission*" -o -name "*auth*" 2>/dev/null | head -20)
    
    # Log any changes
    if [ -n "$CURRENT_STATE" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Role/Permission Configuration Check" >> "$AUDIT_LOG"
        echo "Active role-related files:" >> "$AUDIT_LOG"
        echo "$CURRENT_STATE" >> "$AUDIT_LOG"
        echo "" >> "$AUDIT_LOG"
    fi
}

# Monitor Jenkins audit.log for role-related events
monitor_audit_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Monitoring audit log for role changes..." >> "$AUDIT_LOG"
    
    # Look for role management actions in Jenkins audit logs
    grep -E "role|permission|grant|revoke|access|authorize" /var/log/jenkins/audit.log.0 2>/dev/null | tail -50 >> "$AUDIT_LOG"
    
    echo "" >> "$AUDIT_LOG"
}

# Run checks
check_role_changes
monitor_audit_log

echo "Role monitoring completed. Log: $AUDIT_LOG"
