#!/bin/bash

################################################################################
# JENKINS AUDIT LOGS VIEWER - BBFinserv
# Purpose: View all audit logs, statistics, and reports directly on the server
# Location: /var/lib/jenkins/jenkins-1year-archive/audit-viewer.sh
# Usage: ./audit-viewer.sh
################################################################################

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Configuration
JENKINS_HOME="/var/lib/jenkins"
AUDIT_LOG="/var/log/jenkins/audit.log.0"
COMBINED_LOG="${JENKINS_HOME}/jenkins-1year-archive/audit-logs/audit-combined-all.log"
ARCHIVE_DIR="${JENKINS_HOME}/jenkins-1year-archive"
ACCOUNT="BBFinserv"

# Use combined log if available, fallback to audit.log.0
if [ -f "$COMBINED_LOG" ]; then
    LOG_FILE="$COMBINED_LOG"
    LOG_SOURCE="Combined Archive"
else
    LOG_FILE="$AUDIT_LOG"
    LOG_SOURCE="Live Audit Log"
fi

################################################################################
# HELPER FUNCTIONS
################################################################################
print_header() {
    clear
    echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║${NC} ${CYAN}${BOLD}$1${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

print_section() {
    echo -e "${YELLOW}━━━ $1 ━━━${NC}"
    echo ""
}

print_ok() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_err() {
    echo -e "${RED}❌ $1${NC}"
}

print_info() {
    echo -e "${CYAN}ℹ  $1${NC}"
}

check_log() {
    if [ ! -f "$LOG_FILE" ]; then
        print_err "Log file not found at $LOG_FILE"
        print_info "Run merge-audit-logs.sh first to create the combined log"
        return 1
    fi
    return 0
}

################################################################################
# MENU
################################################################################
show_menu() {
    print_header "JENKINS AUDIT VIEWER — ${ACCOUNT}"

    echo -e "  ${BOLD}Log Source:${NC} $LOG_SOURCE ($LOG_FILE)"
    echo -e "  ${BOLD}Total Entries:${NC} $(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)"
    echo ""
    echo -e "  ${CYAN}── View Logs ──${NC}"
    echo "  1.  View All Audit Logs"
    echo "  2.  View Recent Entries (Last 20)"
    echo "  3.  View Config Changes (configSubmit)"
    echo "  4.  View Role/Permission Changes"
    echo "  5.  View Credential Usage"
    echo "  6.  View Job Creates/Deletes"
    echo "  7.  View Build Operations"
    echo ""
    echo -e "  ${CYAN}── Statistics ──${NC}"
    echo "  8.  Full Statistics Summary"
    echo "  9.  Top Users by Activity"
    echo "  10. Top Source IPs"
    echo "  11. Activity by Date"
    echo ""
    echo -e "  ${CYAN}── Search & Filter ──${NC}"
    echo "  12. Search by Keyword"
    echo "  13. Search by Username"
    echo "  14. Search by Job Name"
    echo "  15. Search by Date Range"
    echo ""
    echo -e "  ${CYAN}── Reports & Export ──${NC}"
    echo "  16. Generate Compliance Report"
    echo "  17. Export to CSV"
    echo "  18. Export to JSON"
    echo ""
    echo -e "  ${CYAN}── System ──${NC}"
    echo "  19. Service & System Status"
    echo "  20. Archive Information"
    echo "  21. Cron Jobs Status"
    echo "  22. Run Log Merge Now"
    echo "  23. Run Monthly Archive Now"
    echo "  24. Complete Audit (All Checks)"
    echo ""
    echo "  0.  Exit"
    echo ""
    echo -n "  Select option (0-24): "
}

################################################################################
# 1. VIEW ALL AUDIT LOGS
################################################################################
view_all_logs() {
    print_header "ALL AUDIT LOG ENTRIES"
    check_log || return

    TOTAL=$(wc -l < "$LOG_FILE")
    print_info "Total entries: $TOTAL | Source: $LOG_SOURCE"
    echo ""

    if [ "$TOTAL" -gt 100 ]; then
        echo -e "${YELLOW}Large log file. Showing with 'less' (press q to quit)${NC}"
        echo ""
        cat "$LOG_FILE" | less
    else
        cat "$LOG_FILE"
    fi
    echo ""
    print_ok "Displayed $TOTAL entries"
}

################################################################################
# 2. VIEW RECENT ENTRIES
################################################################################
view_recent() {
    print_header "LAST 20 AUDIT LOG ENTRIES"
    check_log || return

    print_info "Most recent entries from $LOG_SOURCE:"
    echo ""
    tail -20 "$LOG_FILE"
    echo ""
}

################################################################################
# 3. CONFIG CHANGES
################################################################################
view_config_changes() {
    print_header "CONFIGURATION CHANGES (configSubmit)"
    check_log || return

    COUNT=$(grep -ic "configSubmit" "$LOG_FILE" 2>/dev/null || echo 0)
    print_info "Found $COUNT config changes"
    echo ""

    if [ "$COUNT" -gt 0 ]; then
        grep -i "configSubmit" "$LOG_FILE" | while IFS= read -r line; do
            echo -e "  ${YELLOW}▸${NC} $line"
        done
    else
        echo "  No config changes found"
    fi
    echo ""
}

################################################################################
# 4. ROLE/PERMISSION CHANGES
################################################################################
view_role_changes() {
    print_header "ROLE & PERMISSION CHANGES"
    check_log || return

    COUNT=$(grep -icE "role-strategy|rolesSubmit|assignSubmit|assign-roles|manage/role" "$LOG_FILE" 2>/dev/null || echo 0)
    print_info "Found $COUNT role/permission changes"
    echo ""

    if [ "$COUNT" -gt 0 ]; then
        grep -iE "role-strategy|rolesSubmit|assignSubmit|assign-roles|manage/role" "$LOG_FILE" | while IFS= read -r line; do
            echo -e "  ${RED}▸${NC} $line"
        done
    else
        echo "  No role changes found"
    fi
    echo ""

    # Also check config-history for role diffs
    CONFIG_HISTORY="/var/lib/jenkins/config-history/config"
    if [ -d "$CONFIG_HISTORY" ]; then
        echo ""
        print_section "Role Config History Diffs"
        DIRS=($(ls -d "$CONFIG_HISTORY"/*/ 2>/dev/null | sort | tail -10))
        for i in $(seq $((${#DIRS[@]}-1)) -1 1); do
            NEWER="${DIRS[$i]}config.xml"
            OLDER="${DIRS[$((i-1))]}config.xml"
            if [ -f "$NEWER" ] && [ -f "$OLDER" ]; then
                DIFF=$(diff "$OLDER" "$NEWER" 2>/dev/null | grep -E "^[<>].*sid|^[<>].*role|^[<>].*permission" | head -5)
                if [ -n "$DIFF" ]; then
                    TIMESTAMP=$(basename "${DIRS[$i]}")
                    USER="unknown"
                    if [ -f "${DIRS[$i]}history.xml" ]; then
                        USER=$(grep -o '<user>[^<]*</user>' "${DIRS[$i]}history.xml" | sed 's/<[^>]*>//g')
                    fi
                    echo -e "  ${CYAN}[$TIMESTAMP]${NC} by ${BOLD}$USER${NC}"
                    echo "$DIFF" | while IFS= read -r d; do
                        if [[ "$d" == ">"* ]]; then
                            echo -e "    ${GREEN}+ ${d:1}${NC}"
                        elif [[ "$d" == "<"* ]]; then
                            echo -e "    ${RED}- ${d:1}${NC}"
                        fi
                    done
                    echo ""
                fi
            fi
        done
    fi
}

################################################################################
# 5. CREDENTIAL USAGE
################################################################################
view_credentials() {
    print_header "CREDENTIAL USAGE"
    check_log || return

    COUNT=$(grep -ic "used credentials\|credentials" "$LOG_FILE" 2>/dev/null || echo 0)
    print_info "Found $COUNT credential events"
    echo ""

    if [ "$COUNT" -gt 0 ]; then
        grep -i "used credentials\|credentials" "$LOG_FILE" | while IFS= read -r line; do
            echo -e "  ${YELLOW}▸${NC} $line"
        done
    else
        echo "  No credential usage found"
    fi
    echo ""
}

################################################################################
# 6. JOB CREATES/DELETES
################################################################################
view_job_changes() {
    print_header "JOB CREATES & DELETES"
    check_log || return

    CREATES=$(grep -ic "createItem" "$LOG_FILE" 2>/dev/null || echo 0)
    DELETES=$(grep -ic "doDelete" "$LOG_FILE" 2>/dev/null || echo 0)
    print_info "Creates: $CREATES | Deletes: $DELETES"
    echo ""

    if [ "$CREATES" -gt 0 ]; then
        print_section "Job Creates"
        grep -i "createItem" "$LOG_FILE" | while IFS= read -r line; do
            echo -e "  ${GREEN}+ $line${NC}"
        done
        echo ""
    fi

    if [ "$DELETES" -gt 0 ]; then
        print_section "Job Deletes"
        grep -i "doDelete" "$LOG_FILE" | while IFS= read -r line; do
            echo -e "  ${RED}- $line${NC}"
        done
        echo ""
    fi

    if [ "$CREATES" -eq 0 ] && [ "$DELETES" -eq 0 ]; then
        echo "  No job creates or deletes found"
    fi
    echo ""
}

################################################################################
# 7. BUILD OPERATIONS
################################################################################
view_builds() {
    print_header "BUILD OPERATIONS"
    check_log || return

    COUNT=$(grep -icE "Started by|completed:" "$LOG_FILE" 2>/dev/null || echo 0)
    print_info "Found $COUNT build operations"
    echo ""

    if [ "$COUNT" -gt 0 ]; then
        echo "  Recent builds (last 20):"
        echo ""
        grep -iE "Started by|completed:" "$LOG_FILE" | tail -20 | while IFS= read -r line; do
            if echo "$line" | grep -qi "SUCCESS"; then
                echo -e "  ${GREEN}▸ $line${NC}"
            elif echo "$line" | grep -qi "FAILURE"; then
                echo -e "  ${RED}▸ $line${NC}"
            else
                echo -e "  ${CYAN}▸ $line${NC}"
            fi
        done
    else
        echo "  No build operations found"
    fi
    echo ""
}

################################################################################
# 8. FULL STATISTICS
################################################################################
view_statistics() {
    print_header "AUDIT LOG STATISTICS — ${ACCOUNT}"
    check_log || return

    TOTAL=$(wc -l < "$LOG_FILE")
    CONFIG=$(grep -ic "configSubmit" "$LOG_FILE" 2>/dev/null || echo 0)
    ROLES=$(grep -icE "role-strategy|rolesSubmit|assignSubmit|assign-roles|manage/role" "$LOG_FILE" 2>/dev/null || echo 0)
    CREDS=$(grep -ic "used credentials" "$LOG_FILE" 2>/dev/null || echo 0)
    CREATES=$(grep -ic "createItem" "$LOG_FILE" 2>/dev/null || echo 0)
    DELETES=$(grep -ic "doDelete" "$LOG_FILE" 2>/dev/null || echo 0)
    BUILDS=$(grep -icE "Started by|completed:" "$LOG_FILE" 2>/dev/null || echo 0)
    SECURITY=$(grep -icE "security|realm" "$LOG_FILE" 2>/dev/null || echo 0)
    PLUGIN=$(grep -ic "plugin" "$LOG_FILE" 2>/dev/null || echo 0)

    echo -e "  ${BOLD}Category                    Count       Bar${NC}"
    echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    # Function to draw bar
    draw_bar() {
        local label="$1"
        local count="$2"
        local color="$3"
        local max="$TOTAL"
        local bar_len=0
        if [ "$max" -gt 0 ]; then
            bar_len=$((count * 30 / max))
        fi
        local bar=""
        for ((i=0; i<bar_len; i++)); do bar+="█"; done
        local pct=0
        if [ "$max" -gt 0 ]; then
            pct=$((count * 100 / max))
        fi
        printf "  %-28s %-10d ${color}%-30s${NC} %d%%\n" "$label" "$count" "$bar" "$pct"
    }

    draw_bar "Total Entries" "$TOTAL" "$BLUE"
    draw_bar "Config Changes" "$CONFIG" "$YELLOW"
    draw_bar "Role/Permission Changes" "$ROLES" "$RED"
    draw_bar "Credential Usage" "$CREDS" "$CYAN"
    draw_bar "Job Creates" "$CREATES" "$GREEN"
    draw_bar "Job Deletes" "$DELETES" "$RED"
    draw_bar "Build Operations" "$BUILDS" "$GREEN"
    draw_bar "Security Changes" "$SECURITY" "$RED"
    draw_bar "Plugin Changes" "$PLUGIN" "$YELLOW"
    echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    echo "  Time Range:"
    echo "    First Entry: $(head -1 "$LOG_FILE" | awk '{print $1, $2, $3}')"
    echo "    Last Entry:  $(tail -1 "$LOG_FILE" | awk '{print $1, $2, $3}')"
    echo ""
}

################################################################################
# 9. TOP USERS
################################################################################
view_top_users() {
    print_header "TOP USERS BY ACTIVITY"
    check_log || return

    print_section "Users ranked by number of audit events"

    grep -oE "by [^ ,]+" "$LOG_FILE" | sed 's/by //' | sort | uniq -c | sort -rn | head -15 | while read count user; do
        BAR=""
        for ((i=0; i<count && i<50; i++)); do BAR+="█"; done
        printf "  %-30s %4d  ${GREEN}%s${NC}\n" "$user" "$count" "$BAR"
    done
    echo ""
}

################################################################################
# 10. TOP SOURCE IPS
################################################################################
view_top_ips() {
    print_header "TOP SOURCE IPs"
    check_log || return

    print_section "IPs ranked by number of events"

    grep -oE "from [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" "$LOG_FILE" | sed 's/from //' | sort | uniq -c | sort -rn | head -10 | while read count ip; do
        printf "  %-20s %4d actions\n" "$ip" "$count"
    done
    echo ""
}

################################################################################
# 11. ACTIVITY BY DATE
################################################################################
view_by_date() {
    print_header "ACTIVITY BY DATE"
    check_log || return

    print_section "Events per day"

    grep -oE "^[A-Z][a-z]+ [0-9]+, [0-9]+" "$LOG_FILE" | sort | uniq -c | sort -t',' -k1 | while read count date; do
        BAR=""
        for ((i=0; i<count && i<40; i++)); do BAR+="▓"; done
        printf "  %-20s %4d  ${CYAN}%s${NC}\n" "$date" "$count" "$BAR"
    done
    echo ""
}

################################################################################
# 12. SEARCH BY KEYWORD
################################################################################
search_keyword() {
    print_header "SEARCH BY KEYWORD"
    check_log || return

    echo -n "  Enter search term: "
    read TERM

    if [ -z "$TERM" ]; then
        print_err "No search term provided"
        return
    fi

    COUNT=$(grep -ic "$TERM" "$LOG_FILE" 2>/dev/null || echo 0)
    echo ""
    print_info "Found $COUNT entries matching '$TERM'"
    echo ""

    if [ "$COUNT" -gt 0 ]; then
        grep -i --color=always "$TERM" "$LOG_FILE"
    fi
    echo ""
}

################################################################################
# 13. SEARCH BY USERNAME
################################################################################
search_username() {
    print_header "SEARCH BY USERNAME"
    check_log || return

    echo "  Available users:"
    grep -oE "by [^ ,]+" "$LOG_FILE" | sed 's/by //' | sort -u | while read u; do
        echo "    - $u"
    done
    echo ""
    echo -n "  Enter username: "
    read USERNAME

    if [ -z "$USERNAME" ]; then
        print_err "No username provided"
        return
    fi

    COUNT=$(grep -ic "$USERNAME" "$LOG_FILE" 2>/dev/null || echo 0)
    echo ""
    print_info "Found $COUNT entries for '$USERNAME'"
    echo ""

    if [ "$COUNT" -gt 0 ]; then
        grep -i --color=always "$USERNAME" "$LOG_FILE"
        echo ""
        print_section "Action Breakdown for $USERNAME"
        grep -i "$USERNAME" "$LOG_FILE" | grep -oE "(configSubmit|createItem|doDelete|rebuild|Started by|rolesSubmit|assignSubmit)" | sort | uniq -c | sort -rn | while read count action; do
            printf "    %-25s %d\n" "$action" "$count"
        done
    fi
    echo ""
}

################################################################################
# 14. SEARCH BY JOB NAME
################################################################################
search_jobname() {
    print_header "SEARCH BY JOB NAME"
    check_log || return

    echo -n "  Enter job name (partial match): "
    read JOBNAME

    if [ -z "$JOBNAME" ]; then
        print_err "No job name provided"
        return
    fi

    COUNT=$(grep -ic "$JOBNAME" "$LOG_FILE" 2>/dev/null || echo 0)
    echo ""
    print_info "Found $COUNT entries for job '$JOBNAME'"
    echo ""

    if [ "$COUNT" -gt 0 ]; then
        grep -i --color=always "$JOBNAME" "$LOG_FILE"
    fi
    echo ""
}

################################################################################
# 15. SEARCH BY DATE RANGE
################################################################################
search_date_range() {
    print_header "SEARCH BY DATE RANGE"
    check_log || return

    echo "  Available date range:"
    echo "    First: $(head -1 "$LOG_FILE" | awk '{print $1, $2, $3}')"
    echo "    Last:  $(tail -1 "$LOG_FILE" | awk '{print $1, $2, $3}')"
    echo ""
    echo -n "  Enter start date (e.g., Mar 25): "
    read START_DATE
    echo -n "  Enter end date (e.g., Mar 26): "
    read END_DATE

    if [ -z "$START_DATE" ] || [ -z "$END_DATE" ]; then
        print_err "Both dates required"
        return
    fi

    echo ""
    print_info "Entries between '$START_DATE' and '$END_DATE':"
    echo ""

    FOUND=0
    while IFS= read -r line; do
        echo "  $line"
        ((FOUND++))
    done < <(awk "/$START_DATE/,/$END_DATE/" "$LOG_FILE")

    echo ""
    print_info "Found $FOUND entries in range"
    echo ""
}

################################################################################
# 16. COMPLIANCE REPORT
################################################################################
generate_report() {
    print_header "GENERATING COMPLIANCE REPORT"
    check_log || return

    REPORT="/tmp/jenkins-audit-report-${ACCOUNT}-$(date +%Y%m%d_%H%M%S).txt"

    {
        echo "╔══════════════════════════════════════════════════════════════╗"
        echo "║   JENKINS AUDIT COMPLIANCE REPORT — ${ACCOUNT}             ║"
        echo "╚══════════════════════════════════════════════════════════════╝"
        echo ""
        echo "Generated: $(date)"
        echo "Server: $(hostname)"
        echo "Log Source: $LOG_FILE"
        echo ""
        echo "═══ STATISTICS ═══"
        echo ""
        echo "Total Entries:          $(wc -l < "$LOG_FILE")"
        echo "Config Changes:         $(grep -ic "configSubmit" "$LOG_FILE" 2>/dev/null || echo 0)"
        echo "Role/Permission Changes:$(grep -icE "role-strategy|rolesSubmit|assignSubmit" "$LOG_FILE" 2>/dev/null || echo 0)"
        echo "Credential Usage:       $(grep -ic "used credentials" "$LOG_FILE" 2>/dev/null || echo 0)"
        echo "Job Creates:            $(grep -ic "createItem" "$LOG_FILE" 2>/dev/null || echo 0)"
        echo "Job Deletes:            $(grep -ic "doDelete" "$LOG_FILE" 2>/dev/null || echo 0)"
        echo ""
        echo "═══ TOP USERS ═══"
        echo ""
        grep -oE "by [^ ,]+" "$LOG_FILE" | sed 's/by //' | sort | uniq -c | sort -rn | head -10
        echo ""
        echo "═══ TOP SOURCE IPs ═══"
        echo ""
        grep -oE "from [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" "$LOG_FILE" | sed 's/from //' | sort | uniq -c | sort -rn | head -10
        echo ""
        echo "═══ ARCHIVE STATUS ═══"
        echo ""
        du -sh "$ARCHIVE_DIR" 2>/dev/null || echo "Archive not found"
        echo ""
        ls -la "$ARCHIVE_DIR"/metadata/*.json 2>/dev/null || echo "No metadata files"
        echo ""
        echo "═══ CRON JOBS ═══"
        echo ""
        crontab -l 2>/dev/null || echo "No crontab"
        echo ""
        echo "═══ SERVICE STATUS ═══"
        echo ""
        systemctl is-active jenkins-audit-dashboard 2>/dev/null || echo "Service not found"
        echo ""
        echo "Report generated at $(date)"
    } > "$REPORT"

    cat "$REPORT"
    echo ""
    print_ok "Report saved to: $REPORT"
    echo ""
}

################################################################################
# 17. EXPORT CSV
################################################################################
export_csv() {
    print_header "EXPORTING TO CSV"
    check_log || return

    CSV="/tmp/jenkins-audit-${ACCOUNT}-$(date +%Y%m%d_%H%M%S).csv"

    {
        echo "Timestamp,Raw_Entry,Category"
        while IFS= read -r line; do
            TIMESTAMP=$(echo "$line" | grep -oE "^[A-Z][a-z]+ [0-9]+, [0-9]+ [0-9:,]+ [AP]M" || echo "unknown")
            CATEGORY="Other"
            echo "$line" | grep -qi "configSubmit" && CATEGORY="Config"
            echo "$line" | grep -qiE "role-strategy|rolesSubmit|assignSubmit" && CATEGORY="Role"
            echo "$line" | grep -qi "used credentials" && CATEGORY="Credential"
            echo "$line" | grep -qi "createItem" && CATEGORY="JobCreate"
            echo "$line" | grep -qi "doDelete" && CATEGORY="JobDelete"
            echo "$line" | grep -qiE "Started by|completed:" && CATEGORY="Build"
            echo "$line" | grep -qi "plugin" && CATEGORY="Plugin"
            ESCAPED=$(echo "$line" | sed 's/"/""/g')
            echo "\"$TIMESTAMP\",\"$ESCAPED\",\"$CATEGORY\""
        done < "$LOG_FILE"
    } > "$CSV"

    ROWS=$(wc -l < "$CSV")
    print_ok "CSV exported: $CSV ($ROWS rows)"
    echo ""
    echo "  To download to your Mac:"
    echo "    scp ubuntu@<JENKINS_SERVER_IP>:$CSV ~/Downloads/"
    echo ""
}

################################################################################
# 18. EXPORT JSON
################################################################################
export_json() {
    print_header "EXPORTING TO JSON"
    check_log || return

    JSON="/tmp/jenkins-audit-${ACCOUNT}-$(date +%Y%m%d_%H%M%S).json"

    {
        echo "{"
        echo "  \"account\": \"${ACCOUNT}\","
        echo "  \"generated\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
        echo "  \"server\": \"$(hostname)\","
        echo "  \"total_entries\": $(wc -l < "$LOG_FILE"),"
        echo "  \"statistics\": {"
        echo "    \"configSubmit\": $(grep -ic "configSubmit" "$LOG_FILE" 2>/dev/null || echo 0),"
        echo "    \"roleChanges\": $(grep -icE "role-strategy|rolesSubmit|assignSubmit" "$LOG_FILE" 2>/dev/null || echo 0),"
        echo "    \"credentials\": $(grep -ic "used credentials" "$LOG_FILE" 2>/dev/null || echo 0),"
        echo "    \"createItem\": $(grep -ic "createItem" "$LOG_FILE" 2>/dev/null || echo 0),"
        echo "    \"doDelete\": $(grep -ic "doDelete" "$LOG_FILE" 2>/dev/null || echo 0)"
        echo "  },"
        echo "  \"entries\": ["
        FIRST=1
        while IFS= read -r line; do
            ESCAPED=$(echo "$line" | sed 's/\\/\\\\/g; s/"/\\"/g')
            if [ "$FIRST" -eq 1 ]; then
                echo "    \"$ESCAPED\""
                FIRST=0
            else
                echo "    ,\"$ESCAPED\""
            fi
        done < "$LOG_FILE"
        echo "  ]"
        echo "}"
    } > "$JSON"

    print_ok "JSON exported: $JSON"
    echo ""
    echo "  To download to your Mac:"
    echo "    scp ubuntu@<JENKINS_SERVER_IP>:$JSON ~/Downloads/"
    echo ""
}

################################################################################
# 19. SERVICE STATUS
################################################################################
view_service_status() {
    print_header "SERVICE & SYSTEM STATUS"

    print_section "Jenkins Audit Dashboard Service"
    sudo systemctl status jenkins-audit-dashboard --no-pager 2>/dev/null | head -12 || echo "  Service not found"
    echo ""

    print_section "Jenkins Service"
    sudo systemctl status jenkins --no-pager 2>/dev/null | head -8 || echo "  Jenkins not running"
    echo ""

    print_section "Port Status"
    echo "  Listening ports:"
    sudo ss -tlnp | grep -E "9090|8080" | while IFS= read -r line; do
        echo "    $line"
    done
    echo ""

    print_section "Disk Usage"
    echo "  Archive: $(du -sh "$ARCHIVE_DIR" 2>/dev/null | awk '{print $1}')"
    echo "  Audit logs: $(du -sh /var/log/jenkins/ 2>/dev/null | awk '{print $1}')"
    echo "  Jenkins home: $(du -sh "$JENKINS_HOME" 2>/dev/null | awk '{print $1}')"
    echo ""

    print_section "Python3 Check"
    python3 --version 2>/dev/null || print_err "Python3 not found"
    echo ""
}

################################################################################
# 20. ARCHIVE INFO
################################################################################
view_archives() {
    print_header "ARCHIVE INFORMATION"

    print_section "Archive Directory: $ARCHIVE_DIR"
    echo "  Total Size: $(du -sh "$ARCHIVE_DIR" 2>/dev/null | awk '{print $1}')"
    echo ""

    echo "  Subdirectories:"
    for dir in audit-logs config-history permission-snapshots checksums metadata monthly-reports; do
        SIZE=$(du -sh "$ARCHIVE_DIR/$dir" 2>/dev/null | awk '{print $1}')
        COUNT=$(find "$ARCHIVE_DIR/$dir" -type f 2>/dev/null | wc -l)
        printf "    %-25s %8s  (%d files)\n" "$dir/" "$SIZE" "$COUNT"
    done
    echo ""

    if ls "$ARCHIVE_DIR"/metadata/*.json &>/dev/null; then
        print_section "Archive Metadata"
        for f in "$ARCHIVE_DIR"/metadata/*.json; do
            echo "  $(basename "$f"):"
            cat "$f" | sed 's/^/    /'
            echo ""
        done
    fi

    print_section "Combined Log"
    if [ -f "$COMBINED_LOG" ]; then
        echo "  File: $COMBINED_LOG"
        echo "  Size: $(ls -lh "$COMBINED_LOG" | awk '{print $5}')"
        echo "  Lines: $(wc -l < "$COMBINED_LOG")"
        echo "  Last merged: $(stat -c %y "$COMBINED_LOG" 2>/dev/null | cut -d. -f1)"
    else
        print_err "Combined log not found. Run merge-audit-logs.sh"
    fi
    echo ""
}

################################################################################
# 21. CRON JOBS
################################################################################
view_cron() {
    print_header "CRON JOBS STATUS"

    print_section "Jenkins User Crontab"
    crontab -l 2>/dev/null || echo "  No crontab configured"
    echo ""

    print_section "Merge Log (last 5 runs)"
    if [ -f "$ARCHIVE_DIR/merge-audit-logs.log" ]; then
        tail -5 "$ARCHIVE_DIR/merge-audit-logs.log"
    else
        echo "  No merge log found yet (first merge hasn't run via cron)"
    fi
    echo ""
}

################################################################################
# 22. RUN LOG MERGE NOW
################################################################################
run_merge_now() {
    print_header "RUNNING LOG MERGE"
    print_info "Merging audit logs..."
    echo ""
    "$ARCHIVE_DIR/merge-audit-logs.sh"
    echo ""
    print_ok "Merge complete. Combined log: $(wc -l < "$COMBINED_LOG") entries"
    echo ""
}

################################################################################
# 23. RUN MONTHLY ARCHIVE
################################################################################
run_archive_now() {
    print_header "RUNNING MONTHLY ARCHIVE"
    print_info "Starting archive..."
    echo ""
    "$ARCHIVE_DIR/jenkins-monthly-archive.sh"
    echo ""
}

################################################################################
# 24. COMPLETE AUDIT
################################################################################
run_complete_audit() {
    print_header "COMPLETE AUDIT — ${ACCOUNT}"
    view_service_status
    echo ""
    echo -n "Press Enter to continue to statistics..."
    read
    view_statistics
    echo ""
    echo -n "Press Enter to continue to top users..."
    read
    view_top_users
    echo ""
    echo -n "Press Enter to continue to archives..."
    read
    view_archives
    echo ""
    print_ok "COMPLETE AUDIT FINISHED"
}

################################################################################
# MAIN LOOP
################################################################################
while true; do
    show_menu
    read -r OPTION

    case $OPTION in
        1)  view_all_logs ;;
        2)  view_recent ;;
        3)  view_config_changes ;;
        4)  view_role_changes ;;
        5)  view_credentials ;;
        6)  view_job_changes ;;
        7)  view_builds ;;
        8)  view_statistics ;;
        9)  view_top_users ;;
        10) view_top_ips ;;
        11) view_by_date ;;
        12) search_keyword ;;
        13) search_username ;;
        14) search_jobname ;;
        15) search_date_range ;;
        16) generate_report ;;
        17) export_csv ;;
        18) export_json ;;
        19) view_service_status ;;
        20) view_archives ;;
        21) view_cron ;;
        22) run_merge_now ;;
        23) run_archive_now ;;
        24) run_complete_audit ;;
        0)
            echo -e "\n  ${GREEN}Goodbye!${NC}\n"
            exit 0
            ;;
        *)
            print_err "Invalid option. Try again."
            sleep 1
            ;;
    esac

    echo ""
    echo -n "  Press Enter to return to menu..."
    read
done