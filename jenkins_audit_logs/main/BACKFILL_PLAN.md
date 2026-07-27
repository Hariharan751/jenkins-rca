# Daily-Delta S3 Migration — Runbook

> **Zinka completed 2026-06-17.** Use this runbook to apply the same on **BBFinserv** + **TZF** Jenkins.

## What this fixes

Old behavior: `merge-audit-logs.sh` uploaded **cumulative snapshots** named with today's date. Each daily S3 file held the full history up to that day, so all daily files looked nearly identical and storage grew linearly.

New behavior:
- One S3 object per calendar day, contains **only that day's audit events** (no carry-over).
- `audit-combined-latest.log.gz` at the prefix root = full state (rolling).
- Daily cron at 1AM UTC computes delta vs previous snapshot and uploads only what's new.
- Idempotent across cron misses or manual re-runs.

## Prerequisite — per-env settings

Each Jenkins EC2 has its own copy of the scripts. **Only `S3_PREFIX` differs:**

| Env | S3_PREFIX | EC2 hostname (example) |
|-----|-----------|------------------------|
| Zinka / Divum | `zinka-divum` | `ip-10-34-42-254` |
| BBFinserv | `bbfinserv` | TBD |
| TZF | `tzf` | TBD |

`S3_BUCKET="bb-jenkins-audit-logs"` is the same everywhere.

## Step-by-step per env

> Replace `<ENV>` below with the right value (`bbfinserv` or `tzf`).

### 1. Get scripts onto the EC2

Option A — clone the Jenkins-rca repo (recommended):
```bash
# On the Jenkins EC2 as jenkins user
cd ~
git clone https://github.com/BLACKBUCK-LABS/Jenkins-rca.git
cd ~/Jenkins-rca/jenkins_audit_logs/main

# Change S3_PREFIX to this env
sed -i 's|^S3_PREFIX=.*|S3_PREFIX="<ENV>"|' merge-audit-logs.sh backfill-daily.sh
grep -E '^S3_BUCKET|^S3_PREFIX' merge-audit-logs.sh backfill-daily.sh
# Expect: S3_BUCKET="bb-jenkins-audit-logs", S3_PREFIX="<ENV>"
```

Option B — scp from Mac, then sed the prefix as above.

### 2. Snapshot current state

```bash
cd ~/jenkins-1year-archive

# Backup current scripts (rollback insurance)
cp merge-audit-logs.sh merge-audit-logs.sh.bak.$(date -u +%Y%m%d)

# Snapshot existing S3 prefix layout (rollback insurance)
aws s3 ls s3://bb-jenkins-audit-logs/<ENV>/ --recursive > /tmp/s3-pre-backfill-<ENV>.txt
wc -l /tmp/s3-pre-backfill-<ENV>.txt

# Save current crontab
crontab -l > /tmp/cron-pre-backfill-<ENV>.txt
```

### 3. Pause the merge cron

```bash
crontab -l | sed 's|^\(0 1 \* \* \* .*merge-audit-logs.sh.*\)$|# DISABLED-BACKFILL: \1|' | crontab -
crontab -l | grep -E "merge-audit|DISABLED"
# Expect: # DISABLED-BACKFILL: 0 1 * * * /var/lib/jenkins/jenkins-1year-archive/merge-audit-logs.sh
```

### 4. Deploy the new scripts

```bash
cp ~/Jenkins-rca/jenkins_audit_logs/main/merge-audit-logs.sh .
cp ~/Jenkins-rca/jenkins_audit_logs/main/backfill-daily.sh .
chmod +x merge-audit-logs.sh backfill-daily.sh

# Sanity check
grep -E "^S3_BUCKET|^S3_PREFIX|DAILY DELTA" merge-audit-logs.sh
# Expect: DAILY DELTA, bb-jenkins-audit-logs, <ENV>
```

### 5. Run backfill

```bash
time ./backfill-daily.sh
# Expect ~5-10 min depending on data volume
```

If it hangs >2 min with no progress in `backfill-daily-*.log`, it's likely scanning `/var/log/jenkins/` deep rotation chain. **The new script avoids that** by reading only `audit.log` + `audit.log.0`. If it still stalls, check `pstree -p $(pgrep -f backfill-daily.sh)`.

### 6. Verify

```bash
# Spot check 3 days — each should contain ONE distinct date
for DAY in 2026-04-15 2026-05-15 2026-06-15; do
  echo "=== $DAY ==="
  YR=${DAY:0:4}; MO=${DAY:5:2}
  TMP=$(mktemp); GZ="$TMP.gz"
  aws s3 cp "s3://bb-jenkins-audit-logs/<ENV>/${YR}/${MO}/audit-${DAY}.log.gz" "$GZ" 2>/dev/null
  gunzip "$GZ"
  echo "lines: $(wc -l < $TMP)"
  echo "distinct dates inside:"
  grep -oE '^[A-Z][a-z]{2} [0-9]+, [0-9]{4}' "$TMP" | sort -u
  rm -f "$TMP"
done
```

Each day should show exactly one date matching the filename. If multiple dates → backfill bucketing logic broke; stop and investigate.

### 7. Re-enable cron + first delta run

```bash
crontab -l | sed 's|^# DISABLED-BACKFILL: \(.*\)$|\1|' | crontab -
crontab -l | grep merge-audit-logs
# Expect: 0 1 * * * /var/lib/jenkins/jenkins-1year-archive/merge-audit-logs.sh

# Force-run merge to confirm daily-delta mode works
./merge-audit-logs.sh
tail -20 merge-audit-logs.log
# Expect: "Delta lines (new since last run): <small N>" — only lines added
# since backfill ran. May be 0 if no audit activity in last minutes.
```

## Rollback

If something looks wrong before re-enabling cron:

```bash
# Restore old merge script
cp merge-audit-logs.sh.bak.$(date -u +%Y%m%d) merge-audit-logs.sh

# Re-enable original cron
crontab -l | sed 's|^# DISABLED-BACKFILL: \(.*\)$|\1|' | crontab -

# S3 rollback: each per-day object was OVERWRITTEN. If bucket versioning was
# enabled before backfill, restore prior versions:
aws s3api list-object-versions --bucket bb-jenkins-audit-logs \
  --prefix <ENV>/2026/ --max-items 100 \
  | jq -r '.Versions[] | "\(.Key) \(.VersionId)"' | head
```

(Versioning is recommended but optional. Without it, restore would require re-running backfill from `audit-combined-all.log` or the on-disk `.bak`.)

## Known gotchas (learned the hard way on zinka)

1. **Don't use `find /var/log/jenkins`** — Audit Trail plugin's rotation creates recursive filenames (`audit.log.0.1.1.1.1.gz...`), can be 10k+ files, find stalls. New scripts skip this dir scan.
2. **Don't use `$LINES` as a variable in bash** — bash auto-resets it to terminal rows after commands like `aws s3 cp`. Use `CNT` or `NUM`.
3. **`gzip "$f" > "${f}.gz"` is wrong** — gzip without `-c` deletes the source. Always use `gzip -c "$f" > "${f}.gz"`.
4. **Don't merge with existing S3 object during backfill** — old cumulative content would re-contaminate the per-day file. Overwrite cleanly. (Already fixed in current `backfill-daily.sh`.)

## Per-env tracking

| Env | Backfilled on | First daily-delta cron run | Total days in S3 | Notes |
|-----|---------------|------------------------------|------------------|-------|
| Zinka / Divum | 2026-06-17 | 2026-06-18 01:00 UTC | 102 (Mar 3 → Jun 17) | ✓ verified |
| BBFinserv | _TBD_ | _TBD_ | _TBD_ | |
| TZF | _TBD_ | _TBD_ | _TBD_ | |
