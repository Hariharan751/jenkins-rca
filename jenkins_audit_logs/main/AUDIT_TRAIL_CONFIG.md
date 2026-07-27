# Jenkins Audit Trail Plugin — Source of Truth

The S3 audit logs in `s3://bb-jenkins-audit-logs/<env>/` originate from the **Audit Trail** Jenkins plugin running on each Jenkins controller. This doc captures the exact production config so it can be reproduced, audited, or changed.

## Plugin

- **Name:** [Audit Trail](https://plugins.jenkins.io/audit-trail/)
- **Install:** Manage Jenkins → Plugins → Available → `Audit Trail`
- **Configure:** Manage Jenkins → System → **Audit Trail** section

## Loggers

### Logger 1 — Log file

| Field | Value |
|-------|-------|
| Log Location | `/var/log/jenkins/audit.log` |
| Log File Size MB | `50` |
| Log File Count | `20` |
| Log Separator | _(empty — default newline)_ |

Rotation: when `audit.log` reaches 50 MB, plugin rotates to `audit.log.0`, `audit.log.1`, … keeping last 20 files. Older are gzipped (`.gz`). This produces the recursive chain we have to deal with in scripts — **do not `find` the directory**, use `audit.log` + `audit.log.0` only (see `merge-audit-logs.sh`).

### Logger 2 — Console

| Field | Value |
|-------|-------|
| Output | `STD_OUT` |

Audit events also stream to Jenkins JVM stdout (visible in systemd journal / `journalctl -u jenkins`). Useful for live tail.

## Advanced — URL patterns to log

```
.*/(?:createItem|doDelete|configSubmit|configureSecurity|addUser|doGrantRole|doAssignRole|doRevokeRole|doDeleteUser|securityRealm|role-strategy|manage(?:Roles|Assignment)/?|authorization)/?.*
```

Regex — `^.*/` then one of the action names below, then optional trailing `/...`:

| Pattern | What it captures |
|---------|------------------|
| `createItem` | Any new Jenkins item (job, folder, pipeline, multibranch) |
| `doDelete` | Delete on any item, build, view, or config |
| `configSubmit` | Save on job config / global config / view config — **biggest signal for "someone changed something"** |
| `configureSecurity` | Global security configuration changes |
| `addUser` | Add a user to Jenkins user DB |
| `doGrantRole` | Grant a role (Role-Based Strategy plugin) |
| `doAssignRole` | Assign existing role to a user/group |
| `doRevokeRole` | Revoke role from user/group |
| `doDeleteUser` | Delete a user |
| `securityRealm` | Authentication realm changes (LDAP, OAuth, internal DB) |
| `role-strategy` | Role-Based Strategy management UI |
| `manage(?:Roles\|Assignment)` | Manage Roles / Manage Assignments pages |
| `authorization` | Authorization strategy changes |

## Flags

All four enabled in production:

| Flag | Meaning |
|------|---------|
| ☑ **Log how each build is triggered** | Records user / SCM hook / timer / upstream job that started each build |
| ☑ **Log credentials usage** | Logs which credential ID was used by which job (forensic trail for secrets) |
| ☑ **Display Username instead of UserID** | Audit lines show `by Aman Goyal` instead of `by aman.goyal` — human-readable |
| ☑ **Log Groovy scripts** | Logs Script Console executions (`/script` endpoint) — critical, this is the admin-bypass surface |

## Example audit lines

```
Jun 17, 2026 10:00:10,527 AM /job/preprod-jenkins-deployment-job/45084/rebuild/configSubmit by Gaurav B V from 182.76.96.138
Jun 17, 2026 10:04:16,828 AM preprod-jenkins-deployment-job ... Started by user Shreesh Bhimsenrao, Parameters:[...]
Jun 17, 2026 10:32:54,123 AM /role-strategy/strategy/assignRole by Krishna Pratap from 10.10.10.42
```

## Two sinks for the same data

The plugin emits each audit event to two places by default:

```
Audit Trail plugin (single event source)
├── LogFileAuditLogger    → /var/log/jenkins/audit.log    → merge-audit-logs.sh → S3 (forensic, off-host)
├── ConsoleAuditLogger    → STD_OUT                       → systemd journal (live tail)
└── Jenkins Log Recorder  → $JENKINS_HOME/logs/Audit Logs/audit-YYYY-MM-DD.log
                            UI: Manage Jenkins → System Log → "Audit Logs"
                            (HTML viewer, ~30 day rolling retention managed by Jenkins)
```

### Authoritative vs convenience

| Sink | Use it for | Don't use it for |
|------|-----------|------------------|
| **S3 `audit-YYYY-MM-DD.log.gz`** | Forensics, long-term retention, compliance | Quick spot-check while debugging |
| **Jenkins Log Recorder HTML** | Quick lookup in browser, recent activity | Anything beyond ~30 days (Jenkins purges) |
| **`/var/log/jenkins/audit.log`** | Grep on the EC2 itself | After EC2 rebuild (gone) |

The HTML viewer and the file logger record **the same events** (same plugin emits to both). Format differs: HTML has table columns (Time/Thread/Level/Logger/Message), file has one plain-text line per event. 99% content overlap.

### Why `Level=OFF` in the HTML viewer

Every row shows `Level: OFF`. **By design** — Audit Trail plugin emits at log level `OFF` so that downstream Jenkins logger-level filters don't drop the events. It's the *value*, not missing data. Don't try to "fix" it.

## Cloudflare Email Obfuscation — emails show as `[email protected]`

If the Jenkins audit dashboard is served through Cloudflare and you see `userId="[email protected]"` instead of the real email, **Cloudflare Scrape Shield → Email Address Obfuscation is rewriting the HTML on the fly**. Plugin/Jenkins are fine — the email is intact in the underlying file.

### Verify it's Cloudflare

Open browser DevTools → Elements → find the audit row → the `<a class="__cf_email__" data-cfemail="HEX">` element holds the real email hex-encoded. CF's `email-decode.min.js` decodes on render — but only the visible text gets obfuscated; the `data-cfemail` attribute has the truth.

### Three fixes

| Option | What | When to use |
|--------|------|-------------|
| **1. Disable Email Obfuscation (recommended)** | Cloudflare dashboard → Scrape Shield → Email Address Obfuscation → OFF for the Jenkins zone | Audit dashboards are internal/auth-gated — no scraper risk. One toggle, done. |
| **2. Page Rule (scoped off)** | Rules → Page Rules → URL `*jenkins.example.com/log/Audit*Logs/*` → Email Obfuscation OFF | Want CF obfuscation kept on elsewhere |
| **3. Bypass UI entirely** | Read S3 file or `/var/log/jenkins/audit.log` directly | One-off lookup, don't want to touch CF |

### Option 3 — decode without touching Cloudflare

The CF-decoded email is hex-XOR-encoded with a single-byte key. To decode the `data-cfemail` value (e.g. `data-cfemail="b1d3d8c4...">`):

```python
def cf_decode(hex_str):
    key = int(hex_str[:2], 16)
    return ''.join(chr(int(hex_str[i:i+2], 16) ^ key) for i in range(2, len(hex_str), 2))

# Example
cf_decode("b1d3d8c4d3d8d2c4c5d8c4f1d4d8d5dec5")  # → "[email protected]"
```

Or just read the raw file — no CF in the loop:

```bash
# On EC2 (plain text, no CF)
grep "test-private-deploy-poc" /var/log/jenkins/audit.log

# From S3 (plain text, no CF)
aws s3 cp s3://bb-jenkins-audit-logs/<env>/2026/05/audit-2026-05-22.log.gz - \
  | gunzip | grep "test-private-deploy-poc"
```

### Recommendation

Use **Option 1**. Audit logs are forensic data — hiding the actor's email defeats the purpose. Cloudflare email obfuscation is anti-public-scraper protection that doesn't apply to internal admin dashboards.

## What is NOT captured

Audit Trail plugin does **not** log:

- **Build console output** (stdout/stderr of pipeline steps) → that lives in `$JENKINS_HOME/jobs/<job>/builds/<n>/log`
- **Read-only browsing** (viewing a job, listing builds, etc.) — only the URLs in the pattern above
- **Plugin install / Jenkins restart** — those are in `/var/log/jenkins/jenkins.log` (separate file)
- **System log appenders** added via Manage Jenkins → System Log

If you need any of these, either expand the URL pattern in plugin config, or ship the relevant log file separately to S3.

## Config drift detection

If audit-line volume suddenly drops on one env, suspect:

1. Plugin was disabled / uninstalled — check Manage Jenkins → Plugins → Installed.
2. Log Location field was changed — verify `/var/log/jenkins/audit.log` still exists + recent mtime.
3. URL pattern was simplified — re-paste from this doc.
4. Flags were unticked.

Verify deployed config (read-only API):

```bash
# Live config snapshot (XML)
sudo cat /var/lib/jenkins/audit-trail.xml
# Compare against expected — see "Expected XML" below
```

## Expected `audit-trail.xml`

```xml
<?xml version='1.1' encoding='UTF-8'?>
<hudson.plugins.audit__trail.AuditTrailPlugin>
  <log>true</log>
  <logBuildCause>true</logBuildCause>
  <logCredentialsUsage>true</logCredentialsUsage>
  <displayUserName>true</displayUserName>
  <logGroovyScripts>true</logGroovyScripts>
  <pattern>.*/(?:createItem|doDelete|configSubmit|configureSecurity|addUser|doGrantRole|doAssignRole|doRevokeRole|doDeleteUser|securityRealm|role-strategy|manage(?:Roles|Assignment)/?|authorization)/?.*</pattern>
  <loggers>
    <hudson.plugins.audit__trail.LogFileAuditLogger>
      <log>/var/log/jenkins/audit.log</log>
      <limit>50</limit>
      <count>20</count>
      <logSeparator></logSeparator>
    </hudson.plugins.audit__trail.LogFileAuditLogger>
    <hudson.plugins.audit__trail.ConsoleAuditLogger>
      <output>STD_OUT</output>
    </hudson.plugins.audit__trail.ConsoleAuditLogger>
  </loggers>
</hudson.plugins.audit__trail.AuditTrailPlugin>
```

(Exact tag names may vary slightly across plugin versions; treat this as descriptive, not a copy-paste-to-prod artifact.)

## Restoring config from scratch

If a Jenkins controller is rebuilt from a fresh AMI:

1. Install Audit Trail + Role-Based Strategy + Job Configuration History plugins.
2. Manage Jenkins → System → Audit Trail.
3. Add Logger → **Log file**. Fill in fields from "Logger 1" table above.
4. Add Logger → **Console**. Output = `STD_OUT`.
5. Click **Advanced** → paste URL pattern (one line from this doc).
6. Tick all four flags.
7. Save. Restart Jenkins (`sudo systemctl restart jenkins`) — restart not strictly required but cleanest.
8. Verify: `ls -la /var/log/jenkins/audit.log` should appear within a minute.

Then deploy `merge-audit-logs.sh` + cron per `BACKFILL_PLAN.md`.
