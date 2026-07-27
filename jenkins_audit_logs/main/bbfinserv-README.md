# BBFinserv Jenkins — 1-Year Audit Archive & Dashboard Setup

---

## Step 1 — Install Jenkins Plugins (if not already installed)

Go to **Manage Jenkins → Plugins → Available** in the BBFinserv Jenkins UI and install:

1. **Audit Trail Plugin** — logs all management events (configSubmit, createItem, role changes, credential usage)
2. **Job Configuration History Plugin** — tracks every pipeline config change with diff

After installing, restart Jenkins:

```bash
# Connect to BBFinserv Jenkins via SSM
aws ssm start-session --target <INSTANCE_ID> --region ap-south-1 --profile bbfinservmain

# Or if already on the server
sudo systemctl restart jenkins
sudo systemctl status jenkins
```

### Configure Audit Trail Plugin

Go to **Manage Jenkins → System → Audit Trail**. Match production:

**Logger 1 — Log file:**
- Log Location: `/var/log/jenkins/audit.log`
- Log File Size MB: `50`
- Log File Count: `20`
- Log Separator: _(empty)_

**Logger 2 — Console:** Output = `STD_OUT`

**Advanced → URL patterns to log** (paste one line):
```
.*/(?:createItem|doDelete|configSubmit|configureSecurity|addUser|doGrantRole|doAssignRole|doRevokeRole|doDeleteUser|securityRealm|role-strategy|manage(?:Roles|Assignment)/?|authorization)/?.*
```

**Tick all four flags:**
- ☑ Log how each build is triggered
- ☑ Log credentials usage
- ☑ Display Username instead of UserID
- ☑ Log Groovy scripts

> Full plugin spec + expected XML in `AUDIT_TRAIL_CONFIG.md`.

Save and verify:

```bash
ls -la /var/log/jenkins/audit.log*
```

---

## Step 2 — Create the Archive Directory Structure

```bash
# Connect via SSM, then:

# Create the 1-year archive directory
sudo mkdir -p /var/lib/jenkins/jenkins-1year-archive/{audit-logs,config-history,permission-snapshots,checksums,metadata,monthly-reports,build-logs}

# Set ownership
sudo chown -R jenkins:jenkins /var/lib/jenkins/jenkins-1year-archive

# Verify structure
tree /var/lib/jenkins/jenkins-1year-archive -L 1
```

Expected output:

```
/var/lib/jenkins/jenkins-1year-archive/
├── audit-logs/
├── build-logs/
├── checksums/
├── config-history/
├── metadata/
├── monthly-reports/
└── permission-snapshots/
```

---

## Step 3 — Create the Archive Config File

```bash
sudo tee /var/lib/jenkins/jenkins-archive-config.sh << 'EOF'
#!/bin/bash
# BBFinserv Jenkins Archive Configuration
ARCHIVE_PATH="/var/lib/jenkins/jenkins-1year-archive"
AUDIT_LOG_PATH="/var/log/jenkins/audit.log.0"
JENKINS_JOBS_DIR="/var/lib/jenkins/jobs"
RETENTION_DAYS=365

# S3 backup — shared bucket, per-env prefix
S3_BUCKET="bb-jenkins-audit-logs"
S3_PREFIX="bbfinserv"
EOF

sudo chown jenkins:jenkins /var/lib/jenkins/jenkins-archive-config.sh
sudo chmod 644 /var/lib/jenkins/jenkins-archive-config.sh
```

---

## Step 4 — Deploy All Scripts

### 4a. Merge Audit Logs Script

```bash
sudo tee /var/lib/jenkins/jenkins-1year-archive/merge-audit-logs.sh << 'SCRIPT'
#!/bin/bash
# Source per-env config (S3_BUCKET, S3_PREFIX, ARCHIVE_PATH)
source /var/lib/jenkins/jenkins-archive-config.sh 2>/dev/null || true

ARCHIVE_DIR="${ARCHIVE_PATH:-/var/lib/jenkins/jenkins-1year-archive}"
COMBINED="$ARCHIVE_DIR/audit-logs/audit-combined-all.log"
TEMP="/tmp/audit-merge-temp.log"
MERGE_LOG="$ARCHIVE_DIR/merge-audit-logs.log"
S3_BUCKET="${S3_BUCKET:-bb-jenkins-audit-logs}"
S3_PREFIX="${S3_PREFIX:?S3_PREFIX must be set — use bbfinserv for BBFinserv}"

# --- STEP 1: Merge logs ---
cat /var/log/jenkins/audit.log.0 >> "$TEMP" 2>/dev/null
for f in $(find /var/log/jenkins -name "*.gz" -size +100c 2>/dev/null); do
    zcat "$f" >> "$TEMP" 2>/dev/null
done
cat "$COMBINED" "$TEMP" 2>/dev/null | sort -u > /tmp/audit-final.log
mv -f /tmp/audit-final.log "$COMBINED"
rm -f "$TEMP"

TOTAL=$(wc -l < "$COMBINED")
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ): Merged. Total lines: $TOTAL" >> "$MERGE_LOG"

# --- STEP 2: Compress and upload to S3 ---
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
YEAR=$(date -u +%Y)
MONTH=$(date -u +%m)
MONTH_PREFIX="${S3_PREFIX}/${YEAR}/${MONTH}"
COMPRESSED="/tmp/audit-combined-${TIMESTAMP}.log.gz"

gzip -c "$COMBINED" > "$COMPRESSED"

aws s3 cp "$COMPRESSED" "s3://${S3_BUCKET}/${MONTH_PREFIX}/audit-combined-${TIMESTAMP}.log.gz" \
    --storage-class STANDARD_IA 2>> "$MERGE_LOG"

aws s3 cp "$COMPRESSED" "s3://${S3_BUCKET}/${S3_PREFIX}/audit-combined-latest.log.gz" \
    --storage-class STANDARD_IA 2>> "$MERGE_LOG"

rm -f "$COMPRESSED"

# --- STEP 3: Keep only last 30 files within current month prefix ---
aws s3 ls "s3://${S3_BUCKET}/${MONTH_PREFIX}/" \
    | grep "audit-combined-" \
    | sort \
    | head -n -30 \
    | awk '{print $4}' \
    | while read f; do
        aws s3 rm "s3://${S3_BUCKET}/${MONTH_PREFIX}/$f" 2>/dev/null
    done
SCRIPT

sudo chmod +x /var/lib/jenkins/jenkins-1year-archive/merge-audit-logs.sh
sudo chown jenkins:jenkins /var/lib/jenkins/jenkins-1year-archive/merge-audit-logs.sh
```

### 4b. Monthly Archive Script

```bash
sudo tee /var/lib/jenkins/jenkins-1year-archive/jenkins-monthly-archive.sh << 'SCRIPT'
#!/bin/bash
set -e

source /var/lib/jenkins/jenkins-archive-config.sh 2>/dev/null || {
  echo "ERROR: Cannot load jenkins-archive-config.sh"
  exit 1
}

MONTH=$(date -u +%Y-%m)
TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S")

mkdir -p "$ARCHIVE_PATH/audit-logs/$MONTH"
mkdir -p "$ARCHIVE_PATH/config-history/$MONTH"
mkdir -p "$ARCHIVE_PATH/permission-snapshots"
mkdir -p "$ARCHIVE_PATH/checksums"
mkdir -p "$ARCHIVE_PATH/metadata"

LOG_FILE="$ARCHIVE_PATH/monthly-reports/archive-$MONTH-$TIMESTAMP.log"
mkdir -p "$(dirname "$LOG_FILE")"

{
  echo "================================================================"
  echo "Jenkins Monthly Archive - $MONTH - BBFinserv"
  echo "================================================================"
  echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo ""

  # 1. ARCHIVE AUDIT LOGS
  echo "Archiving audit logs..."
  if [ -f "$AUDIT_LOG_PATH" ] || [ -f "${AUDIT_LOG_PATH}.1" ]; then
    cp ${AUDIT_LOG_PATH}* "$ARCHIVE_PATH/audit-logs/$MONTH/" 2>/dev/null || true
    AUDIT_COUNT=$(find "$ARCHIVE_PATH/audit-logs/$MONTH" -type f | wc -l)
    echo "   Audit logs: $AUDIT_COUNT files"
  else
    echo "   No audit logs found"
    AUDIT_COUNT=0
  fi

  # 2. ARCHIVE JOB CONFIG HISTORY
  echo "Archiving job configuration history..."
  JOB_COUNT=0
  CONFIG_COUNT=0
  for job_dir in "$JENKINS_JOBS_DIR"/*/; do
    if [ -d "$job_dir" ]; then
      JOB_NAME=$(basename "$job_dir")
      mkdir -p "$ARCHIVE_PATH/config-history/$MONTH/$JOB_NAME"
      if [ -f "${job_dir}config.xml" ]; then
        cp "${job_dir}config.xml" "$ARCHIVE_PATH/config-history/$MONTH/$JOB_NAME/" 2>/dev/null || true
        ((CONFIG_COUNT++))
      fi
      if [ -d "${job_dir}configHistory" ]; then
        cp -r "${job_dir}configHistory" "$ARCHIVE_PATH/config-history/$MONTH/$JOB_NAME-history/" 2>/dev/null || true
      fi
      ((JOB_COUNT++))
    fi
  done
  echo "   Job configs: $JOB_COUNT jobs, $CONFIG_COUNT config files"

  # 3. PERMISSION SNAPSHOT
  echo "Creating permission snapshot..."
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
        grep -o "permission>[^<]*<" "$CONFIG_FILE" 2>/dev/null | sed 's/permission>//; s/<$//' || true
        echo ""
      fi
    done
  } > "$SNAP_FILE"
  echo "   Permission snapshot created"

  # 4. CHECKSUMS
  echo "Generating checksums..."
  CHECKSUM_FILE="$ARCHIVE_PATH/checksums/${MONTH}-checksums.sha256"
  cd "$ARCHIVE_PATH"
  find "audit-logs/$MONTH" "config-history/$MONTH" "permission-snapshots/${MONTH}-permissions.txt" \
    -type f 2>/dev/null | xargs sha256sum > "$CHECKSUM_FILE" 2>/dev/null || true
  CHECKSUM_COUNT=$(wc -l < "$CHECKSUM_FILE" 2>/dev/null || echo "0")
  echo "   Checksums: $CHECKSUM_COUNT files"

  # 5. METADATA
  echo "Creating metadata..."
  cat > "$ARCHIVE_PATH/metadata/${MONTH}-metadata.json" << METAEOF
{
  "archive_month": "$MONTH",
  "account": "bbfinserv",
  "created_timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "archive_location": "$ARCHIVE_PATH",
  "contents": {
    "audit_logs": { "location": "audit-logs/$MONTH", "files": $AUDIT_COUNT },
    "config_history": { "location": "config-history/$MONTH", "jobs": $JOB_COUNT, "files": $CONFIG_COUNT },
    "permission_snapshot": { "location": "permission-snapshots/${MONTH}-permissions.txt" }
  },
  "checksums": "$CHECKSUM_FILE",
  "retention_days": 365
}
METAEOF

  echo ""
  echo "================================================================"
  echo "ARCHIVE COMPLETE"
  echo "================================================================"
  echo "Month: $MONTH"
  echo "Audit logs: $AUDIT_COUNT files"
  echo "Job configs: $JOB_COUNT jobs"
  TOTAL_SIZE=$(du -sh "$ARCHIVE_PATH" | awk '{print $1}')
  echo "Total archive size: $TOTAL_SIZE"
  echo "Completed: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

} | tee "$LOG_FILE"
SCRIPT

sudo chmod +x /var/lib/jenkins/jenkins-1year-archive/jenkins-monthly-archive.sh
sudo chown jenkins:jenkins /var/lib/jenkins/jenkins-1year-archive/jenkins-monthly-archive.sh
```

### 4c. Query Scripts

```bash
# Query User Actions
sudo tee /var/lib/jenkins/jenkins-1year-archive/query-user-actions.sh << 'SCRIPT'
#!/bin/bash
if [ -z "$1" ]; then
  echo "Usage: $0 <username>"
  echo "Example: $0 'Aman Goyal'"
  exit 1
fi
USERNAME="$1"
echo "All actions by: $USERNAME"
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
SCRIPT

# Query Config Changes
sudo tee /var/lib/jenkins/jenkins-1year-archive/query-config-changes.sh << 'SCRIPT'
#!/bin/bash
echo "All Configuration Changes"
echo ""
echo "Config Submits: $(grep -c 'configSubmit' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"
echo "Job Creates: $(grep -c 'createItem' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"
echo "Job Deletes: $(grep -c 'doDelete' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"
echo "Rebuilds: $(grep -c 'rebuild' /var/log/jenkins/audit.log* 2>/dev/null || echo 0)"
echo ""
echo "Recent configuration changes:"
grep "configSubmit" /var/log/jenkins/audit.log* 2>/dev/null | tail -10
SCRIPT

# Generate Audit Report
sudo tee /var/lib/jenkins/jenkins-1year-archive/generate-audit-report.sh << 'SCRIPT'
#!/bin/bash
REPORT_FILE="jenkins-audit-report-$(date +%Y%m%d).txt"
{
  echo "JENKINS 1-YEAR AUDIT COMPLIANCE REPORT — BBFinserv"
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
  echo "ARCHIVE STATUS:"
  du -sh /var/lib/jenkins/jenkins-1year-archive 2>/dev/null || echo "Archive not found"
} > "$REPORT_FILE"
cat "$REPORT_FILE"
echo ""
echo "Report saved to: $REPORT_FILE"
SCRIPT

# Monitor Role Changes
sudo tee /var/lib/jenkins/jenkins-1year-archive/monitor-role-changes.sh << 'SCRIPT'
#!/bin/bash
AUDIT_LOG="/var/log/jenkins/role-changes.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Role/Permission Configuration Check" >> "$AUDIT_LOG"
CURRENT_STATE=$(find /var/lib/jenkins -name "*role*" -o -name "*permission*" -o -name "*auth*" 2>/dev/null | head -20)
if [ -n "$CURRENT_STATE" ]; then
    echo "Active role-related files:" >> "$AUDIT_LOG"
    echo "$CURRENT_STATE" >> "$AUDIT_LOG"
fi
grep -E "role|permission|grant|revoke|access|authorize" /var/log/jenkins/audit.log.0 2>/dev/null | tail -50 >> "$AUDIT_LOG"
echo "" >> "$AUDIT_LOG"
echo "Role monitoring completed. Log: $AUDIT_LOG"
SCRIPT

# Set permissions for all scripts
sudo chmod +x /var/lib/jenkins/jenkins-1year-archive/*.sh
sudo chown -R jenkins:jenkins /var/lib/jenkins/jenkins-1year-archive/
```

---

## Step 5 — Deploy the Python API Server

```bash
sudo tee /var/lib/jenkins/jenkins-1year-archive/jenkins-audit-api.py << 'PYEOF'
#!/usr/bin/env python3
import http.server
import socketserver
import json
import subprocess
import os
from urllib.parse import urlparse, parse_qs

PORT = 9090
LOG_FILE = "/var/lib/jenkins/jenkins-1year-archive/audit-logs/audit-combined-all.log"
DASHBOARD_FILE = "/var/lib/jenkins/jenkins-1year-archive/jenkins-audit-dashboard.html"

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == '/api/stats':
                self.send_stats()
            elif path == '/api/logs':
                self.send_logs()
            elif path == '/api/search':
                query = parse_qs(urlparse(self.path).query)
                self.send_search(query)
            elif path == '/api/role-diff':
                self.send_role_diff()
            else:
                self.send_dashboard()
        except Exception as e:
            self.send_error(500, str(e))

    def send_stats(self):
        try:
            result = subprocess.run(f"wc -l {LOG_FILE} | awk '{{print $1}}'",
                                  shell=True, capture_output=True, text=True, timeout=5)
            total = result.stdout.strip() or "0"

            result = subprocess.run(f"grep -c 'configSubmit' {LOG_FILE}",
                                  shell=True, capture_output=True, text=True, timeout=5)
            config = int(result.stdout.strip() or "0")

            result = subprocess.run(f"grep -c 'rebuild' {LOG_FILE}",
                                  shell=True, capture_output=True, text=True, timeout=5)
            rebuild = int(result.stdout.strip() or "0")

            result = subprocess.run(f"grep -c 'createItem' {LOG_FILE}",
                                  shell=True, capture_output=True, text=True, timeout=5)
            creates = int(result.stdout.strip() or "0")

            result = subprocess.run(f"grep -c 'used credentials' {LOG_FILE}",
                                  shell=True, capture_output=True, text=True, timeout=5)
            creds = int(result.stdout.strip() or "0")

            result = subprocess.run(f"grep -iE 'manage/role|rolesSubmit|assign-roles' {LOG_FILE} | wc -l",
                                  shell=True, capture_output=True, text=True, timeout=5)
            roles = int(result.stdout.strip() or "0")

            result = subprocess.run(f"du -sh /var/lib/jenkins/jenkins-1year-archive | awk '{{print $1}}'",
                                  shell=True, capture_output=True, text=True, timeout=5)
            archive = result.stdout.strip() or "0M"

            data = {
                "status": "success",
                "total_logs": total,
                "total_users": "21",
                "archive_size": archive,
                "actions": {
                    "configSubmit": config,
                    "rebuild": rebuild,
                    "createItem": creates,
                    "credentials": creds,
                    "role": roles
                }
            }
            self.send_json(data)
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)})

    def send_logs(self):
        try:
            result = subprocess.run(f"cat {LOG_FILE}",
                                  shell=True, capture_output=True, text=True, timeout=10)
            logs = [l for l in result.stdout.split('\n') if l.strip()]
            self.send_json({"status": "success", "total": len(logs), "logs": logs})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)})

    def send_role_diff(self):
        try:
            config_dir = "/var/lib/jenkins/config-history/config"
            if not os.path.exists(config_dir):
                self.send_json({"status": "success", "changes": []})
                return
            dirs = sorted([d for d in os.listdir(config_dir) if os.path.isdir(os.path.join(config_dir, d))])
            results = []
            for i in range(len(dirs)-1, 0, -1):
                newer = os.path.join(config_dir, dirs[i], "config.xml")
                older = os.path.join(config_dir, dirs[i-1], "config.xml")
                history = os.path.join(config_dir, dirs[i], "history.xml")
                user = "unknown"
                if os.path.exists(history):
                    with open(history) as hf:
                        hcontent = hf.read()
                        import re
                        um = re.search(r'<user>(.*?)</user>', hcontent)
                        if um: user = um.group(1)
                if not os.path.exists(newer) or not os.path.exists(older):
                    continue
                result = subprocess.run(["diff", older, newer], capture_output=True, text=True)
                added = []
                removed = []
                for line in result.stdout.split("\n"):
                    if line.startswith(">"):
                        val = line[1:].strip()
                        if any(k in val for k in ["sid", "role name", "permission"]):
                            added.append(val)
                    elif line.startswith("<"):
                        val = line[1:].strip()
                        if any(k in val for k in ["sid", "role name", "permission"]):
                            removed.append(val)
                if added or removed:
                    results.append({
                        "timestamp": dirs[i].replace("_", " ").replace("-", "/", 2).replace("-", ":"),
                        "user": user,
                        "added": added,
                        "removed": removed
                    })
            self.send_json({"status": "success", "changes": results})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)})

    def send_search(self, query):
        try:
            username = query.get('username', [''])[0]
            jobname = query.get('jobname', [''])[0]
            if username:
                cmd = f"grep -i '{username}' {LOG_FILE} | tail -100"
            elif jobname:
                cmd = f"grep -i '{jobname}' {LOG_FILE} | tail -100"
            else:
                self.send_json({"status": "error", "message": "No search criteria"})
                return
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
            results = [l for l in result.stdout.split('\n') if l.strip()]
            self.send_json({"status": "success", "results": results, "count": len(results)})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)})

    def send_dashboard(self):
        try:
            if os.path.exists(DASHBOARD_FILE):
                with open(DASHBOARD_FILE, 'rb') as f:
                    content = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
            else:
                self.send_response(404)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b"Dashboard file not found")
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(str(e).encode())

    def send_json(self, data):
        response = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        pass

if __name__ == '__main__':
    print(f"API running on port {PORT}")
    print(f"Dashboard: http://localhost:{PORT}/jenkins-audit-dashboard.html")
    print(f"Reading logs from: {LOG_FILE}")
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped")
PYEOF

sudo chown jenkins:jenkins /var/lib/jenkins/jenkins-1year-archive/jenkins-audit-api.py
sudo chmod +x /var/lib/jenkins/jenkins-1year-archive/jenkins-audit-api.py
```

---

## Step 6 — Deploy the Dashboard HTML

Download the `jenkins-audit-dashboard.html` file from the zip and upload it:

```bash
# From your Mac terminal — SCP the dashboard file to bbfinserv Jenkins
scp ~/Downloads/jenkins_audit_logs/jenkins_audit_logs/jenkins-audit-dashboard.html ubuntu@<JENKINS_SERVER_IP>:~/

# SSH into the server
ssh ubuntu@<JENKINS_SERVER_IP>

# Copy to archive directory and update title
sudo cp ~/jenkins-audit-dashboard.html /var/lib/jenkins/jenkins-1year-archive/jenkins-audit-dashboard.html

# Update the title to say BBFinserv
sudo sed -i 's/<title>Jenkins Audit Dashboard<\/title>/<title>Jenkins Audit Dashboard - BBFinserv<\/title>/' /var/lib/jenkins/jenkins-1year-archive/jenkins-audit-dashboard.html

sudo chown jenkins:jenkins /var/lib/jenkins/jenkins-1year-archive/jenkins-audit-dashboard.html
sudo chmod 644 /var/lib/jenkins/jenkins-1year-archive/jenkins-audit-dashboard.html
```

---

## Step 7 — Run Initial Log Merge

```bash
# Run the merge script to create the combined log file
sudo -u jenkins /var/lib/jenkins/jenkins-1year-archive/merge-audit-logs.sh

# Verify combined log was created
wc -l /var/lib/jenkins/jenkins-1year-archive/audit-logs/audit-combined-all.log
```

---

## Step 8 — Create Systemd Service (Auto-Start on Boot)

```bash
sudo tee /etc/systemd/system/jenkins-audit-dashboard.service << 'EOF'
[Unit]
Description=Jenkins Audit Dashboard API - BBFinserv
After=network.target jenkins.service

[Service]
Type=simple
User=jenkins
Group=jenkins
WorkingDirectory=/var/lib/jenkins/jenkins-1year-archive
ExecStart=/usr/bin/python3 /var/lib/jenkins/jenkins-1year-archive/jenkins-audit-api.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable jenkins-audit-dashboard
sudo systemctl start jenkins-audit-dashboard
sudo systemctl status jenkins-audit-dashboard
```

---

## Step 9 — Set Up Cron Jobs

```bash
# Switch to jenkins user
sudo su - jenkins

# Edit crontab
crontab -e
```

Add these lines:

```cron
# Merge audit logs every 6 hours
0 */6 * * * /var/lib/jenkins/jenkins-1year-archive/merge-audit-logs.sh

# Monthly archive on 1st of each month at midnight
0 0 1 * * /var/lib/jenkins/jenkins-1year-archive/jenkins-monthly-archive.sh

# Monitor role changes daily at 6 AM
0 6 * * * /var/lib/jenkins/jenkins-1year-archive/monitor-role-changes.sh
```

Save and verify:

```bash
crontab -l
exit
```

---

## Step 10 — Open Port in AWS Security Group

The dashboard runs on port **9090**. Add an inbound rule to the BBFinserv Jenkins EC2 Security Group:

1. Go to **AWS Console → EC2 → Instances** → find the Jenkins server
2. Click **Security** tab → Click the **Security Group** link
3. Click **Edit inbound rules → Add rule**:
   - **Type:** Custom TCP
   - **Port range:** `9090`
   - **Source:** Your office IP / VPN CIDR (e.g., `0.0.0.0/0` or restrict to office)
   - **Description:** Jenkins Audit Dashboard
4. Click **Save rules**

---

## Step 11 — Verify Everything Works

```bash
# Test API from the server
curl -s http://localhost:9090/api/stats | python3 -m json.tool
curl -s http://localhost:9090/api/logs | python3 -m json.tool | head -20

# Test dashboard HTML loads
curl -s http://localhost:9090/jenkins-audit-dashboard.html | head -10
```

From your **Mac terminal** (not SSH):

```bash
# Replace with actual public IP of bbfinserv Jenkins
curl -s https://finserv-jenkins-audit.jinka.in/jenkins-audit-dashboard.html | head -10
```

Then open in browser:

```
https://finserv-jenkins-audit.jinka.in/jenkins-audit-dashboard.html
```

---

## Step 12 — Run First Monthly Archive

```bash
sudo -u jenkins /var/lib/jenkins/jenkins-1year-archive/jenkins-monthly-archive.sh
```

---

## Audit Viewer — Terminal-Based Log Inspector

Since the BBFinserv Jenkins is in a **private subnet** (no public IP access), the audit-viewer script lets you inspect all audit data directly from the terminal without needing a web browser.

### Deploy

```bash
# Copy audit-viewer.sh to the archive directory
sudo cp ~/audit-viewer.sh /var/lib/jenkins/jenkins-1year-archive/audit-viewer.sh
sudo chmod +x /var/lib/jenkins/jenkins-1year-archive/audit-viewer.sh
sudo chown jenkins:jenkins /var/lib/jenkins/jenkins-1year-archive/audit-viewer.sh
```

### Run

```bash
sudo su - jenkins
cd /var/lib/jenkins/jenkins-1year-archive
./audit-viewer.sh
```

### Menu Options (24 total)

| # | Option | Description |
|---|--------|-------------|
| **View Logs** | | |
| 1 | View All Audit Logs | Full log dump (paginated if >100 entries) |
| 2 | View Recent Entries | Last 20 entries |
| 3 | Config Changes | All `configSubmit` events |
| 4 | Role/Permission Changes | Role strategy + config-history diffs with add/remove |
| 5 | Credential Usage | Tracks which credentials were used by which jobs |
| 6 | Job Creates/Deletes | `createItem` and `doDelete` events |
| 7 | Build Operations | Build start/complete with SUCCESS/FAILURE coloring |
| **Statistics** | | |
| 8 | Full Statistics | Visual bar chart of all categories |
| 9 | Top Users | Users ranked by activity count |
| 10 | Top Source IPs | Source IPs ranked by event count |
| 11 | Activity by Date | Events per day with visual bars |
| **Search & Filter** | | |
| 12 | Search by Keyword | Free-text grep with highlighting |
| 13 | Search by Username | Lists available users, shows action breakdown |
| 14 | Search by Job Name | Partial match on job names |
| 15 | Search by Date Range | Filter entries between two dates |
| **Reports & Export** | | |
| 16 | Compliance Report | Full text report saved to `/tmp/` |
| 17 | Export CSV | CSV export with SCP download command |
| 18 | Export JSON | JSON export with SCP download command |
| **System** | | |
| 19 | Service Status | Dashboard service, Jenkins, ports, disk |
| 20 | Archive Info | Directory sizes, metadata, combined log status |
| 21 | Cron Jobs | Current crontab + last merge log entries |
| 22 | Run Log Merge | Trigger `merge-audit-logs.sh` on-demand |
| 23 | Run Monthly Archive | Trigger `jenkins-monthly-archive.sh` on-demand |
| 24 | Complete Audit | Runs status + stats + users + archives |

### Download Exports to Your Mac

After generating CSV or JSON exports from the viewer:

```bash
# From your Mac terminal
scp ubuntu@<JENKINS_SERVER_IP>:/tmp/jenkins-audit-BBFinserv-*.csv ~/Downloads/
scp ubuntu@<JENKINS_SERVER_IP>:/tmp/jenkins-audit-BBFinserv-*.json ~/Downloads/
```

---

## Accessing the Dashboard

Since BBFinserv Jenkins is in a **private subnet**, the web dashboard is only accessible via:

**Option 1 — Direct URL (via VPN or public domain):**

```
https://finserv-jenkins-audit.jinka.in/jenkins-audit-dashboard.html
```

**Option 2 — SSH Tunnel via SSM:**

```bash
# From your Mac terminal
aws ssm start-session \
  --target <INSTANCE_ID> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["9090"],"localPortNumber":["9090"]}' \
  --region ap-south-1 \
  --profile bbfinservmain
```

Then open in browser:

```
http://localhost:9090/jenkins-audit-dashboard.html
```

**Option 3 — Terminal Viewer (recommended for quick checks):**

```bash
# Via SSM, then:
sudo su - jenkins
cd /var/lib/jenkins/jenkins-1year-archive
./audit-viewer.sh
```

---

## Quick Reference Commands

```bash
# Check dashboard service status
sudo systemctl status jenkins-audit-dashboard

# Restart dashboard if needed
sudo systemctl restart jenkins-audit-dashboard

# View dashboard logs
sudo journalctl -u jenkins-audit-dashboard -n 30 --no-pager

# Launch the terminal audit viewer
cd /var/lib/jenkins/jenkins-1year-archive && ./audit-viewer.sh

# Query user actions
./query-user-actions.sh "username"

# Query config changes
./query-config-changes.sh

# Generate compliance report
./generate-audit-report.sh

# Check archive size
du -sh /var/lib/jenkins/jenkins-1year-archive/*/

# Verify checksums
cd /var/lib/jenkins/jenkins-1year-archive/checksums
sha256sum -c $(ls -t *.sha256 | head -1) | head -20

# Manual log merge
./merge-audit-logs.sh

# Manual monthly archive
./jenkins-monthly-archive.sh
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Dashboard not loading | Check `curl https://finserv-jenkins-audit.jinka.in/` — verify DNS + nginx/proxy up |
| Dashboard not loading via public IP | **Expected** — private subnet, use VPN or SSM tunnel |
| Dashboard blank page | `Cmd + Option + J` in Chrome to check JS console errors |
| API returns empty logs | Run `./merge-audit-logs.sh` first to create combined log |
| Service won't start | Check `journalctl -u jenkins-audit-dashboard -n 50` |
| Port already in use | `sudo ss -tlnp \| grep 9090` then `sudo kill <PID>`, restart service |
| audit-viewer.sh permission denied | `chmod +x audit-viewer.sh` |
| No data in viewer | Run option 22 (Log Merge) from the viewer menu first |

---

## S3 Backup

All audit logs are mirrored to **one shared S3 bucket** with per-env prefix:

- **Bucket:** `bb-jenkins-audit-logs` (ARN: `arn:aws:s3:::bb-jenkins-audit-logs`)
- **BBFinserv prefix:** `bbfinserv/`

| Object | Path |
|--------|------|
| Per-day events | `s3://bb-jenkins-audit-logs/bbfinserv/<YYYY>/<MM>/audit-<YYYY-MM-DD>.log.gz` |
| Latest full snapshot | `s3://bb-jenkins-audit-logs/bbfinserv/audit-combined-latest.log.gz` |

- **Cadence:** daily 1:00 AM UTC (`0 1 * * * merge-audit-logs.sh`)
- **Object content:** only that day's audit events (delta), not cumulative
- **Storage class:** `STANDARD_IA`
- **Retention:** indefinite (append-only daily files)

Verify upload:
```bash
aws s3 ls s3://bb-jenkins-audit-logs/bbfinserv/$(date -u +%Y)/$(date -u +%m)/ --profile bbfinservmain
```

## 📅 Daily-Delta Behavior

`merge-audit-logs.sh` runs daily at 1AM UTC. Each S3 object holds **only that day's events**, bucketed by the audit-line timestamp (Audit Trail format `"Mon DD, YYYY ..."`). Pipeline:

1. Merge live + rotated audit logs into `audit-combined-all.log`.
2. Diff vs `.audit-prev-snapshot.log` → delta lines.
3. Bucket delta by line timestamp → per-day files.
4. For each day: download existing S3 object (if any), `sort -u` merge, re-upload (idempotent across cron misses).
5. Refresh `audit-combined-latest.log.gz` as full state.
6. Save current snapshot as next-run baseline.

If `bb-jenkins-audit-logs/bbfinserv/`, `/tzf/`, `/zinka-divum/` show **identical** files, one server has wrong `S3_PREFIX` hardcoded in `merge-audit-logs.sh` — fix by setting `S3_PREFIX="bbfinserv"` on BBFinserv EC2 and redeploying.

Historical files written before this fix were cumulative snapshots — see `BACKFILL_PLAN.md` to rewrite them in place via `backfill-daily.sh`.

## Files Deployed

```
/var/lib/jenkins/jenkins-1year-archive/
├── audit-logs/                          # Monthly archived + combined log
│   └── audit-combined-all.log           # All logs merged (used by API + viewer)
├── config-history/                      # Monthly job config snapshots
├── permission-snapshots/                # Monthly permission proofs
├── checksums/                           # SHA256 integrity verification
├── metadata/                            # JSON metadata per month
├── monthly-reports/                     # Archive execution logs
├── build-logs/                          # (Optional)
├── jenkins-audit-api.py                 # Python API server (port 9090)
├── jenkins-audit-dashboard.html         # Web dashboard frontend
├── audit-viewer.sh                      # Terminal-based audit viewer (24 options)
├── merge-audit-logs.sh                  # Merges all audit logs into combined file
├── jenkins-monthly-archive.sh           # Monthly archive script (cron: 1st of month)
├── query-user-actions.sh                # Query by username
├── query-config-changes.sh              # Query config changes
├── generate-audit-report.sh             # Generate compliance report
└── monitor-role-changes.sh              # Monitor role/permission changes (cron: daily)
```

## Cron Schedule

```
0 */6 * * *  merge-audit-logs.sh          # Every 6 hours
0 0 1 * *    jenkins-monthly-archive.sh   # 1st of each month at midnight
0 6 * * *    monitor-role-changes.sh      # Daily at 6 AM UTC
```

## Systemd Service

```
Service:  jenkins-audit-dashboard
Port:     9090
User:     jenkins
Auto-start: enabled (on boot)
```