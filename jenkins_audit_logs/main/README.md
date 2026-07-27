# Jenkins 1-Year Audit Archive & Dashboard

## 📂 Directory Structure
```
jenkins-1year-archive/
├── audit-logs/              # Monthly archived audit logs
├── config-history/          # Monthly archived job configs
├── permission-snapshots/    # Monthly permission snapshots
├── checksums/              # SHA256 checksums for verification
├── metadata/               # Metadata files (JSON)
├── monthly-reports/        # Archive execution reports
├── build-logs/             # (Optional) Build logs
├── jenkins-audit-dashboard.html    # Web dashboard (frontend)
├── jenkins-audit-api.py            # Backend API server
├── jenkins-monthly-archive.sh      # Monthly archive script
├── query-user-actions.sh           # Query user actions
├── query-config-changes.sh         # Query config changes
├── generate-audit-report.sh        # Generate compliance report
└── README.md                       # This file
```

## 🚀 Quick Start

### 1. Start the Dashboard
```bash
cd /var/lib/jenkins/jenkins-1year-archive
python3 jenkins-audit-api.py
```

Then open browser:
```
http://localhost:9090/jenkins-audit-dashboard.html
http://jenkins-audit.blackbuck.com:9090/jenkins-audit-dashboard.html
```

### 2. Query Audit Logs
```bash
# Find all actions by a user
./query-user-actions.sh "Aman Goyal"

# Find all config changes
./query-config-changes.sh

# Generate compliance report
./generate-audit-report.sh
```

### 3. Manual Archive (Monthly)
```bash
# Run monthly archive manually
./jenkins-monthly-archive.sh
```

### 4. View Archive Status
```bash
# Show directory structure
tree . -L 2

# Show size breakdown
du -sh */

# Show latest archives
ls -lh metadata/
```

## 📊 What's Stored

- **Audit Logs**: 267 files (1.3 MB) - All Jenkins actions logged
- **Config History**: 224 jobs - All configuration changes
- **Permission Snapshots**: Monthly access control proof
- **Checksums**: SHA256 verification for integrity
- **Metadata**: JSON files with archive details

## ⏱️ Automation

Cron job runs **1st of each month at 00:00 UTC**:
```bash
0 0 1 * * /var/lib/jenkins/jenkins-1year-archive/jenkins-monthly-archive.sh
```

Check with: `crontab -l`

## 🔍 Examples

### Search by User
```bash
./query-user-actions.sh "g.hariharan"
```

### Search by Job
```bash
grep "stagger_nonweb" audit-logs/2026-03/*
```

### Search by Action Type
```bash
grep "configSubmit" audit-logs/2026-03/* | head -20
```

### View Config History
```bash
ls config-history/2026-03/
cat config-history/2026-03/stagger_nonweb/config.xml
```

### Check Integrity
```bash
cd checksums
sha256sum -c 2026-03-checksums.sha256 | head -20
```

## 📈 Dashboard Features

- 🔎 Search by username
- 🔎 Search by job name
- 🔎 Filter by action type
- 📊 View statistics
- 📋 Recent audit logs
- 💾 Archive information

## 🔐 Security

- All data is read-only after archival
- Checksums verify no tampering
- Monthly snapshots prove permissions unchanged
- 365-day retention for compliance

## ☁️ S3 Storage Location

All audit logs are mirrored to **one shared S3 bucket** with per-environment prefixes:

- **Bucket ARN:** `arn:aws:s3:::bb-jenkins-audit-logs`
- **Bucket name:** `bb-jenkins-audit-logs`

| Env | S3 path |
|-----|---------|
| BBFinserv | `s3://bb-jenkins-audit-logs/bbfinserv/<YYYY>/<MM>/audit-<YYYY-MM-DD>.log.gz` |
| TZF | `s3://bb-jenkins-audit-logs/tzf/<YYYY>/<MM>/audit-<YYYY-MM-DD>.log.gz` |
| Zinka / Divum | `s3://bb-jenkins-audit-logs/zinka-divum/<YYYY>/<MM>/audit-<YYYY-MM-DD>.log.gz` |

Latest pointer (overwritten each run) lives at the prefix root:
```
s3://bb-jenkins-audit-logs/<env>/audit-combined-latest.log.gz
```

**Upload cadence:** daily, 1:00 AM UTC (cron `0 1 * * * merge-audit-logs.sh`).
**Storage class:** `STANDARD_IA`.
**Object content:** **one file per calendar day** holding only that day's audit events (no cumulative snapshots).
**Retention:** indefinite — append-only S3 layout, no auto-prune. Per-day files are tiny.

`S3_BUCKET` and `S3_PREFIX` are hardcoded near the top of `merge-audit-logs.sh` on each EC2:
```bash
S3_BUCKET="bb-jenkins-audit-logs"
S3_PREFIX="bbfinserv"   # or "tzf" or "zinka-divum"
```

## 📅 Daily-Delta Semantics

`merge-audit-logs.sh` runs once a day (cron `0 1 * * *` UTC) and uploads **only that day's audit events** to `<env>/<YYYY>/<MM>/audit-<YYYY-MM-DD>.log.gz`. Pipeline:

1. Read `/var/log/jenkins/audit.log*` + rotated `.gz` → merge into `audit-combined-all.log` (full history, dedup, sort).
2. Diff vs previous run's snapshot (`.audit-prev-snapshot.log`) → produces delta of new lines.
3. Bucket delta lines **by their own timestamp** (Audit Trail format `"Mon DD, YYYY H:MM:SS ..."`).
4. For each date bucket: pull existing S3 object (if any), merge in new lines (`sort -u`), re-upload. **Idempotent** — safe across cron misses or re-runs.
5. Refresh full-state pointer at `<env>/audit-combined-latest.log.gz`.
6. Save current snapshot as baseline for next run.

Lines whose first-line timestamp can't be parsed (stack-trace continuations, etc.) land under `<env>/unknown/audit-unknown-<TS>.log.gz`.

> **Historical files (pre-fix) under `<env>/<YYYY>/<MM>/` were cumulative snapshots** (each file held the full history up to that date). The `backfill-daily.sh` script rewrites them in place to true per-day content — see `BACKFILL_PLAN.md`.

If `bbfinserv/`, `tzf/`, `zinka-divum/` ever hold identical files again, the cause is a wrong `S3_PREFIX` hardcoded in `merge-audit-logs.sh` on one of the EC2s — verify with `grep S3_PREFIX ~/jenkins-1year-archive/merge-audit-logs.sh`.

## 🔌 Where the Data Comes From

Audit lines are produced by the **Audit Trail** Jenkins plugin running on each controller. Plugin writes to `/var/log/jenkins/audit.log` → rotates to `audit.log.0..audit.log.19` → our cron picks it up.

**Full plugin config (URL patterns, flags, expected XML, restore steps):** see `AUDIT_TRAIL_CONFIG.md`.

Quick reference — events captured:
- `createItem` / `doDelete` — job lifecycle
- `configSubmit` — any config save (biggest signal for "someone changed something")
- `configureSecurity` / `securityRealm` / `authorization` — auth/security config
- `addUser` / `doDeleteUser` — user management
- `doGrantRole` / `doAssignRole` / `doRevokeRole` / `role-strategy` / `manage(Roles|Assignment)` — RBAC changes
- Plus: build triggers, credentials usage, Groovy Script Console executions

## 📞 Support

For more info, see:
- Audit Trail plugin config: `AUDIT_TRAIL_CONFIG.md`
- Live audit file on EC2: `/var/log/jenkins/audit.log*`
- Job Config History (separate plugin): `/var/lib/jenkins/jobs/*/configHistory/`
- Archive Reports: `monthly-reports/`
- S3 archive: `s3://bb-jenkins-audit-logs/<env>/`

