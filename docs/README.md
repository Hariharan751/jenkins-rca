# bbctl — Gated EC2 Command Execution

## Table of Contents

1. [What is bbctl?](#1-what-is-bbctl)
2. [Why we built it](#2-why-we-built-it)
3. [How it works](#3-how-it-works)
4. [Security model](#4-security-model)
5. [Installation](#5-installation)
6. [Daily usage](#6-daily-usage)
7. [Command reference](#7-command-reference)
8. [Getting access approved](#8-getting-access-approved)
9. [Audit and break-glass](#9-audit-and-break-glass)
10. [Troubleshooting](#10-troubleshooting)
11. [FAQ](#11-faq)

---

## 1. What is bbctl?

bbctl is Blackbuck's internal tool for running commands on EC2 instances. It replaces direct SSH and AWS SSM access with an auditable, approval-gated pipeline. Every command passes through a classifier — safe commands run immediately, sensitive commands require a manager-approved Jira ticket before executing, and dangerous commands are blocked unconditionally. Every execution, approved or denied, is written to an immutable audit log in S3. Developers get the access they need to do their jobs; the organization gets a complete, tamper-proof record of who ran what, on which instance, and when.

---

## 2. Why we built it

**The problem with direct SSM/SSH access:**

When developers have raw IAM permissions to SSM or direct SSH keys, there is no enforcement layer between intent and execution. Anyone with access can run anything — restart a service, delete a file, exfiltrate data, or modify configuration — and the only record is a CloudTrail API call that logs the SSM invocation but not the command contents or output. There is no approval trail, no record of what was actually run, and no way to prove after the fact that a given action was authorized.

**What bbctl adds:**

- **Approval gating.** Commands that modify state (restart services, change files, run database queries) require a manager-approved Jira ticket. The ticket is auto-created with the exact command pre-filled; the manager approves or rejects it in Jira.
- **Exact command binding.** Approval is granted for one specific command string on one specific instance in one specific AWS account. A ticket for `systemctl restart app` cannot be used to run `systemctl stop app`.
- **One-ticket-one-use.** Once a ticket is used to execute a command, it is transitioned to "Access Granted" and cannot be replayed. Concurrent attempts are rejected at the database constraint level.
- **Immutable audit log.** Every execution is written to S3 with Object Lock in COMPLIANCE mode. Not even the root AWS account can delete or overwrite a log entry before the 13-month retention period expires. The log includes the developer's identity, source IP, SSO groups, raw command, rewritten command, stdout, stderr, Jira ticket ID, approver, and outcome.
- **Compliance posture.** The audit log answers "who ran what, on which instance, when, and who approved it" for any command in the past 13 months. Break-glass access is logged identically to normal access.

---

## 3. How it works

```
Developer                  bbctl backend              AWS
─────────                  ─────────────              ───
bbctl login
  └─ Google OAuth ─────────────────────────────────► blackbuck.com GSuite
  └─ id_token stored locally

bbctl run i-xxx -- <cmd>
  └─ POST /v1/commands ──► Classifier
                               │
                    ┌──────────┼──────────────┐
                    ▼          ▼               ▼
                  SAFE     RESTRICTED        DENIED
                    │          │               │
                    │    ticket_id set?        └─ 403 blocked
                    │    ┌─────┴─────┐
                    │   NO          YES
                    │    │           │
                    │    │    Validate ticket
                    │    │    (status=Approved,
                    │    │     requester=you,
                    │    │     instance+account match,
                    │    │     exact command match,
                    │    │     approver≠you)
                    │    │           │
                    │   Auto-create  │
                    │   Jira ticket  │
                    │   REQ-xxx      │
                    │    │           │
                    └────┴───────────┘
                               │
                    Assume SSM executor role (STS)
                    SSM SendCommand ──────────────► EC2 instance
                    Poll GetCommandInvocation ◄──── stdout / stderr
                               │
                    Write audit record ───────────► S3 (Object Lock, KMS)
                    Transition ticket → "Access Granted"
                               │
                    Response ◄─┘
  └─ stdout printed to terminal
```

**Numbered flow:**

1. Developer runs `bbctl login` once. bbctl opens a browser, completes Google OAuth, and stores the id\_token locally.
2. Developer runs a command: `bbctl run i-0abc123 -- systemctl restart app`.
3. The backend classifies the command as safe, restricted, or denied.
4. **Safe:** the command executes immediately via SSM. Output is printed and logged.
5. **Restricted (no ticket):** the backend auto-creates a Jira ticket in the REQ project, pre-filled with the exact command, instance ID, and AWS account. The developer is shown the ticket URL and told to wait for approval.
6. **Restricted (ticket provided):** the backend validates the ticket — status must be Approved, requester must match the authenticated developer, instance and account must match, and the allowed command must exactly match what was submitted. If all checks pass, the command executes.
7. **Denied:** the command is rejected immediately. No ticket can override a denied command.
8. The backend assumes the SSM executor IAM role via STS (with ExternalId), sends the command via AWS SSM `AWS-RunShellScript`, and polls until completion.
9. Output is redacted for secrets, capped at 10MB, and written to S3 alongside a structured audit record.
10. The Jira ticket is transitioned to "Access Granted." It cannot be reused.

---

## 4. Security model

### Authentication and identity

- Developers authenticate via Google OAuth (loopback redirect flow, RFC 8252).
- JWTs are validated against Google's JWKS endpoint with automatic key rotation support.
- The `hd` claim is verified: only `blackbuck.com` Google Workspace accounts are accepted. Personal Gmail accounts are rejected.
- Tokens have short expiry. There is no server-side session; revocation is enforced by expiry.

### Authorization

- Every command request includes the developer's verified email and SSO groups.
- Account-level authorization: the developer's groups must be permitted to target the requested AWS account.
- Restricted commands additionally require a valid Jira ticket, and every field on that ticket is validated server-side.

### Command parsing and containment

- Commands are parsed using a POSIX shell AST before classification. Pipes (`|`), redirects (`>`, `>>`), command substitutions (`` ` ``, `$()`), and background operators (`&`, `&&`, `||`, `;`) are rejected at the metacharacter level — they cannot be smuggled through in ticket-approved commands.
- URL arguments in `curl` and `wget` are validated against a host allowlist (env `URL_HOST_ALLOWLIST`). Only `http` and `https` schemes are permitted.
- Certain commands are rewritten before execution (`htop` → `ps` snapshot, `less` → `cat | head -n 500`) to remove interactive modes that cannot safely run over SSM.

### Infrastructure isolation

- The backend assumes a purpose-scoped IAM role (`bbctl-ssm-executor`) via STS to send SSM commands. It never uses long-lived credentials. The role trust requires a shared ExternalId, preventing confused-deputy attacks.
- A separate role (`bbctl-audit-writer`) is assumed for S3 writes.

### Audit integrity

- Audit records are written to S3 with **Object Lock in COMPLIANCE mode** and a **13-month retention period**. This means no AWS user — including the root account — can delete or modify a record before retention expires.
- Records are encrypted with a dedicated KMS key.
- Each record includes: timestamp, request ID, developer email, source IP, SSO groups, client version, target instance, target account, raw command, parsed argv, effective (rewritten) argv, classification tier, outcome, exit code, stdout (redacted), stderr (redacted), redaction count, Jira ticket ID, approver display name, and SSM command ID.

### Replay prevention

- Each (ticket ID, instance ID, command hash) triple is recorded on first use. Duplicate attempts are rejected before execution. Concurrent requests race at the constraint level — only one can win.

### Output redaction

- Stdout and stderr are scanned against configurable redaction patterns before being returned to the client and written to the audit log. Matches are replaced with `[REDACTED]` and the redaction count is recorded.

---

## 5. Installation

**Prerequisites:** macOS or Linux, Go 1.21+ (for building from source).

```bash
# From source (until Homebrew tap is live)
git clone git@github.com:blackbuck/bbctl.git
cd bbctl
make build
sudo cp bin/bbctl /usr/local/bin/bbctl
```

**First-time setup:**

```bash
bbctl login
```

This opens your browser for Google sign-in. After authenticating, your token is stored at `~/.bbctl/token`. Configuration lives at `~/.bbctl/config.yaml`.

```yaml
# ~/.bbctl/config.yaml
backend_url: https://bbctl.internal.blackbuck.com
default_account_id: "735317561518"   # your usual AWS account
oidc_client_id: 396628175360-...     # set by your platform team
```

---

## 6. Daily usage

### Login

```bash
bbctl login
```

Opens a browser window for Google OAuth. Completes in seconds. Re-run when your token expires (you will see "Not authenticated" errors).

---

### One-shot command

```bash
bbctl run <instance-id> -- <command>
```

**Safe command — runs immediately:**

```
$ bbctl run i-0e2c3537c218a22ea -- ps aux
USER       PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND
root         1  0.0  0.1  19364  1544 ?        Ss   09:00   0:01 /sbin/init
app       1234  2.1  8.4 4213456 345M ?        Sl   09:01  42:13 java -jar app.jar
...
```

**Restricted command — no ticket, ticket is auto-created:**

```
$ bbctl run i-0e2c3537c218a22ea -- systemctl restart app

Jira ticket created: REQ-726
   https://blackbuck.atlassian.net/browse/REQ-726

Waiting for manager approval.
   Once approved, run:
     bbctl run i-0e2c3537c218a22ea --ticket REQ-726 -- systemctl restart app
```

After the manager approves REQ-726 in Jira:

```
$ bbctl run i-0e2c3537c218a22ea --ticket REQ-726 -- systemctl restart app
```

The command executes. The ticket is marked "Access Granted" and cannot be reused.

**Denied command — flat rejection:**

```
$ bbctl run i-0e2c3537c218a22ea -- bash

Error: command "bash" is permanently denied (shell interpreters are not permitted)
```

No ticket can override a denied command.

---

### Interactive shell

```bash
bbctl shell <instance-id>
```

Opens an interactive session. Safe commands run immediately; restricted commands auto-create tickets as above.

```
$ bbctl shell i-0e2c3537c218a22ea

Connected to i-0e2c3537c218a22ea.
Safe mode. Type /help for commands, /exit to quit.

[alice@i-0e2c] $ ps aux
...

[alice@i-0e2c] $ systemctl restart app

Jira ticket created: REQ-727
   https://blackbuck.atlassian.net/browse/REQ-727

Waiting for manager approval.
   Once approved:
     /ticket REQ-727
     systemctl restart app

[alice@i-0e2c] $ /ticket REQ-727
Ticket REQ-727 set.

[alice@i-0e2c 🎫] $ systemctl restart app
[command executes]
```

**Shell slash commands:**

| Command | Description |
|---|---|
| `/ticket REQ-xxx` | Set the active Jira ticket for this session |
| `/classify <cmd>` | Check what tier a command falls into without running it |
| `/whoami` | Show your authenticated email and active ticket |
| `/history` | Show command history for this session |
| `/help` | Show all available commands |
| `/exit` | End the session |

---

## 7. Command reference

### Safe — run immediately, no approval needed

| Command | Notes |
|---|---|
| `ls` | Path restrictions apply (no `/root`, `/.ssh`, etc.) |
| `cat` | Path restrictions apply; `/proc/*/environ` blocked |
| `tail` | `-f` (follow) denied; path restrictions apply |
| `head` | Path restrictions apply |
| `grep` | `-r`/`-R`/`--recursive` denied; path restrictions apply |
| `less` | Rewritten to `cat \| head -n 500` |
| `top` | Rewritten to `top -b -n 1` (non-interactive) |
| `htop` | Rewritten to `ps` snapshot sorted by CPU |
| `ps` | |
| `df` | |
| `du` | Path restrictions apply |
| `free` | |
| `netstat` | |
| `ss` | |
| `pwd` | |
| `whoami` | |
| `date` | |
| `uptime` | |
| `hostname` | |
| `wc` | Path restrictions apply |
| `find` | `-exec`/`-delete`/`-execdir`/`-fprint` denied; path restrictions apply |
| `echo` | |
| `pgrep` | |
| `pstree` | |
| `lscpu` | |
| `lsmem` | |
| `lshw` | |
| `uname` | |
| `arch` | |
| `nproc` | |
| `id` | |
| `groups` | |
| `last` | |
| `lastlog` | |
| `w` | |
| `who` | |
| `finger` | |
| `lsblk` | |

Also safe with flag-based tiering: `jmap -histo`, `jmap -heap`, `jmap -finalizerinfo`, `jcmd <pid> Thread.print`, `jcmd <pid> VM.flags`, `jcmd <pid> VM.version`, `jcmd <pid> VM.uptime`, `jcmd <pid> GC.class_histogram`.

---

### Restricted — require Jira ticket with exact command match

Approval is granted for one specific command string. `rm -rf /tmp/cache` and `rm -rf /tmp/logs` are two distinct approvals.

| Category | Commands |
|---|---|
| **File operations** | `rm`, `mv`, `cp`, `chmod`, `chown`, `tee`, `vi`, `nano`, `touch` |
| **Privilege** | `sudo`, `supervisorctl` |
| **Services** | `systemctl`, `service` |
| **Process control** | `kill`, `pkill` |
| **Java / JVM** | `java`, `jar`, `jps`, `jinfo`, `mvn`, `gradle` |
| **Python tools** | `pip`, `pip3`, `gunicorn`, `uwsgi`, `celery` |
| **Node.js tools** | `npm`, `yarn`, `pm2` |
| **System info** | `timedatectl`, `hostnamectl`, `localectl` |
| **Disk** | `fdisk`, `parted`, `blkid`, `tune2fs`, `iotop` |
| **Performance** | `vmstat`, `iostat`, `sar`, `mpstat`, `pidstat`, `dstat`, `glances` |
| **Network diagnostics** | `ping`, `traceroute`, `tracepath`, `mtr`, `nmap`, `ab`, `wrk` |
| **Outbound HTTP** | `curl`, `wget` (host allowlist enforced) |
| **Tracing** | `strace`, `tcpdump`, `lsof`, `jstat`, `jmap -dump` |
| **DNS** | `dig`, `nslookup` |
| **Logging** | `journalctl`, `dmesg`, `logrotate`, `filebeat`, `fluent-bit`, `fluentd` |
| **Databases** | `mysql`, `mysqldump`, `mysqladmin`, `psql`, `pg_dump`, `mongosh`, `mongo`, `redis-cli` |
| **Packages** | `apt`, `apt-get`, `dpkg`, `snap` |
| **Monitoring agents** | `newrelic-daemon`, `newrelic-infra`, `amazon-cloudwatch-agent-ctl` |
| **Web servers** | `nginx`, `apache2`, `apache2ctl` |
| **Archives** | `tar`, `zip`, `unzip`, `gzip`, `gunzip` |
| **Resource control** | `watch`, `timeout`, `nice`, `ionice`, `ulimit`, `fuser`, `pmap` |

Also restricted with subcommand tiering: `jmap -dump`, `jcmd <pid> GC.heap_dump`, `jcmd <pid> JFR.start/dump/stop`.

---

### Denied — blocked unconditionally, no ticket override

| Category | Commands |
|---|---|
| **Shell interpreters** | `bash`, `sh`, `zsh`, `ksh`, `dash` |
| **Code interpreters** | `python`, `python3`, `perl`, `ruby`, `node`, `php`, `awk`, `gawk` |
| **Reverse shell tools** | `nc`, `ncat`, `socat` |
| **Disk destruction** | `dd`, `mkfs` |
| **Emergency power** | `reboot`, `shutdown`, `halt`, `init` |
| **Privilege escalation** | `su` |
| **Container escape** | `chroot`, `nsenter`, `unshare`, `mount`, `umount` |
| **Network config** | `iptables`, `ip`, `ifconfig`, `route`, `tc`, `nft`, `sysctl` |
| **Kernel modules** | `modprobe`, `insmod`, `rmmod` |
| **Job scheduling** | `crontab`, `at`, `batch` |
| **Remote access** | `ssh`, `scp`, `sftp`, `rsync` |
| **Session detachment** | `screen`, `tmux`, `nohup`, `disown`, `setsid` |
| **Debuggers** | `gdb`, `ltrace` |

---

## 8. Getting access approved

When you run a restricted command without a `--ticket` flag, bbctl automatically:

1. Creates a ticket in the **REQ** Jira project (issue type: bbctl Access Request).
2. Pre-fills all required fields: your email, the instance ID, the AWS account, and the **exact command string** you submitted.
3. Returns the ticket URL immediately.

**What the ticket contains:**

| Field | Value |
|---|---|
| Summary | `bbctl access: <command> on <instance-id>` |
| Requester | Your authenticated email |
| Instance ID | The EC2 instance you targeted |
| AWS Account | The account label (e.g. Zinka, Divum) |
| Allowed Command | The exact command string — this is what will be permitted |

**Approval flow:**

1. Your manager receives the ticket and reviews the command and instance.
2. Manager transitions the ticket to **Approved** in Jira.
3. You re-run the exact same command with `--ticket REQ-xxx`.
4. The backend validates every field. If anything mismatches (different command, different instance, different account), the ticket is rejected.
5. On success, the ticket is transitioned to **Access Granted** automatically.
6. The ticket is spent. Running the same command again requires a new ticket.

**Important:** The ticket approves the exact command string. `rm -rf /tmp/cache` and `rm -rf /tmp/logs` are distinct commands that each require their own ticket. This is intentional — approvers know exactly what will run.

---

## 9. Audit and break-glass

### Audit log

Every command execution — safe, restricted, or denied — produces a structured JSON record written to S3. Records are:

- **Immutable.** Object Lock in COMPLIANCE mode. No one can delete or overwrite them before the 13-month retention period expires.
- **Encrypted.** KMS-encrypted with a dedicated key.
- **Complete.** Each record includes developer identity, source IP, SSO groups, raw command, effective command, stdout (redacted), stderr (redacted), outcome, duration, exit code, approver (for restricted commands), and SSM command ID.

Authorized log readers: Thejasvi, Rahul (and any delegate they designate). Contact them for audit queries.

### Break-glass

If the bbctl backend is unreachable during a production incident, access to the underlying SSM console is possible but must follow the break-glass process:

1. Obtain MFA-authenticated access via the break-glass IAM role (contact Thejasvi or Rahul).
2. All actions taken via the AWS console or direct SSM are still logged by CloudTrail.
3. After the incident, file a postmortem entry documenting what was accessed and why.

Break-glass access is not exempt from audit. The CloudTrail record is permanent.

---

## 10. Troubleshooting

**"Not authenticated. Run: bbctl login"**

Your token has expired. Run `bbctl login` and authenticate again. Tokens are short-lived by design.

**"Command X is not in the allowlist" or unexpected denial**

Run `/classify <command>` in shell mode or check the command reference table above. Some commands are denied regardless of context (e.g. `bash`, `python`). If you believe a command should be in the safe or restricted tier, contact the platform team.

**"Ticket REQ-xxx not found" or validation failure**

- Ensure the ticket is in the **REQ** project (not PRODACCESS or any other project).
- Ensure the ticket status is exactly **Approved** (not "In Review" or "Open").
- Ensure you are running the **exact same command** that is in the ticket's Allowed Command field — including flags, paths, and spacing.
- Ensure the ticket's instance ID and AWS account match what you passed to `bbctl run`.

**"Jira approval system unavailable"**

The backend cannot reach Jira. Check your network or VPN, or contact the platform team. Safe commands are unaffected; only restricted commands require Jira connectivity.

**"Instance not reachable" or SSM timeout**

The SSM agent on the instance may be stopped or the instance may be in a bad state. Check the instance in the AWS console. The SSM agent can be restarted via the EC2 console's Run Command if you have console access.

**Ticket marked "Access Granted" before you ran the command**

Someone else used the ticket, or you already ran the command and it succeeded. A ticket is single-use. Request a new one.

---

## 11. FAQ

**Why not just use SSM directly?**

Direct SSM access provides no approval layer and no command-level audit. Anyone with `ssm:SendCommand` IAM permission can run anything on any instance. bbctl adds classification, approval gating, and an immutable audit trail without removing the operational flexibility engineers need.

**Why Jira and not a custom approval UI?**

Jira is where Blackbuck's operational workflows already live. Managers are already in Jira. Using Jira for approvals means no new tool to adopt, no new notification channel to monitor, and audit of the approval workflow itself is captured in Jira's own history. A custom UI would require building, hosting, and securing another service.

**What if Jira is down?**

Safe commands are unaffected — they never touch Jira. Restricted commands will fail with "Jira approval system unavailable." This is intentional: if we cannot verify that a ticket exists and is approved, we do not execute the command. In a production incident where restricted commands are urgently needed and Jira is unavailable, use the break-glass process (see section 9).

**Can I bypass this in an emergency?**

The only sanctioned bypass is the break-glass process documented in section 9. Break-glass access is not silent — it is logged by AWS CloudTrail and must be followed by a postmortem entry. There is no way to run a command and have it not logged. If you find a way, please report it immediately to the platform team.

**Can I run multiple commands in one ticket?**

No. Each ticket approves one exact command string on one instance. This is by design — the approver knows precisely what will execute. If you need to run three different commands, you need three tickets (or one ticket per command, run sequentially after approval).

**How do I request a new command be added to the safe list?**

Open a ticket with the platform team describing the command, why it is read-only or low-risk, and the use case. Commands are added to the safe tier only if they cannot modify system state, exfiltrate data, or be chained with other commands to achieve a dangerous result. Argument-level restrictions (path denylists, flag denylists) are also considered.

**How do I request a new command be added to the restricted list?**

Same process as above. The bar for restricted is lower — "a manager approving the exact command string provides sufficient control." Most operational commands belong here.

**Why is `reboot` denied and not restricted?**

Rebooting an instance is catastrophic and not recoverable mid-flight. There is no way to approve a reboot after reviewing the command and be confident about timing or impact on dependent services. Reboots are handled through the AWS console by authorized personnel as a deliberate, out-of-band action.

**I need to run a database query. Can I use `mysql` or `psql`?**

Yes — both are in the restricted tier. The approved command must include the full query. For example: `mysql -u app -p -e "SELECT COUNT(*) FROM orders WHERE status='pending'"`. The ticket approves that exact query on that exact instance.
