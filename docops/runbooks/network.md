# Runbook: network

## What this class means

A TCP/IP-level network failure — the pipeline (or a service it
manages) could not establish a connection to a remote endpoint, the
endpoint refused the connection, or the DNS lookup failed. The
pipeline aborts with messages like:

```
Connection refused
Connection timed out
No route to host
UnknownHostException
```

Distinct from `timeout` (wrapper hit budget on a successful
connection) and from `health_check` (ALB target probe specifically).
`network` is the catch-all for "the underlying TCP/IP connection
never worked."

## Detect signals (primary)

- `Connection refused` — endpoint reachable but rejecting connections
  (service not listening, firewall blocking)
- `Connection timed out` (with no preceding successful TCP) — packets
  not reaching the endpoint (route or SG issue)
- `No route to host` — kernel-level routing failure (VPC peering,
  route table, IGW)
- `UnknownHostException: <name>` — DNS lookup failed (Route53
  outage, internal DNS misconfig)
- `getaddrinfo: Temporary failure in name resolution`
- `curl: (7) Failed to connect to <host>:<port>`

## Drill plan

1. **Identify endpoint + port + caller.** Search the pipeline code
   for the URL/host that failed:
   `repo_search("jenkins_pipeline", "<host_from_log>")`
   The match tells you which helper made the call.
2. **Confirm reachability.** For AWS endpoints, hit them with a
   fresh `aws_describe(<service>, <op>)` from this same host — if it
   works now, the failure was transient. For other endpoints, the
   operator can verify externally.
3. **Identify which leg failed.** Match the error message to the
   layer:
   - `UnknownHostException` → DNS
   - `Connection refused` → service not listening on that port (NOT
     a network problem in the usual sense — check the service)
   - `Connection timed out` → packets dropped (SG, route, NACL)
   - `No route to host` → kernel routing table
4. **Check security groups + VPC routing.** For AWS-side endpoints,
   `aws_describe(ec2, DescribeSecurityGroups, {...})` to verify the
   instance SG has egress to the endpoint port. For cross-account
   targets, also verify the VPC peering or Transit Gateway is up.
5. **Check DNS resolver.** For `UnknownHostException`, on the host
   running the pipeline: `dig <host>`. If `;; SERVFAIL` or empty
   answer, DNS is broken. Could be Route53 hostedzone deleted or
   `/etc/resolv.conf` misconfig.

## Action template

```
Finding:
  Build <N> of <job> failed in stage `<stage>` with `<error_text>`.
  The pipeline could not reach `<host>:<port>` from
  `<helper>.groovy:<line>`. Layer that failed: <DNS|SG|route|
  service-not-listening>.

Action:
  Step 1 (CONFIRM transient):
    Re-trigger the build. If it succeeds, the issue was transient
    (network blip, DNS cache refresh). Note the time + endpoint and
    move on.
  Step 2 (DNS — UnknownHostException):
    Operator runs `dig <host> @8.8.8.8` and `dig <host>`. If
    external resolves but internal doesn't, the internal Route53
    or `/etc/resolv.conf` is misconfigured.
  Step 3 (SG / route — Connection timed out):
    Verify the instance security group allows egress to
    `<host>:<port>`. For AWS targets, also confirm the VPC route
    table has a route to that destination.
  Step 4 (service not listening — Connection refused):
    The remote endpoint is reachable but not listening on that
    port. Check the destination service's status — this is usually
    NOT a network class, re-classify if the destination service is
    the actual offender.
  Step 5 (cross-account / cross-VPC):
    Verify peering / TGW attachment is "available" via
    `aws_describe(ec2, DescribeVpcPeeringConnections, ...)`.

Verify:
  Re-run the pipeline. The TCP connection establishes; the call
  completes inside the operation's normal duration.
```

## Output schema notes

- `error_class: "network"`
- `failed_stage`: the `[Pipeline] { (...) }` marker active when the
  network error fired
- `evidence[]` must include:
  - `jenkins_log` line with the network error
  - `jenkins_pipeline/<helper>.groovy:<line>` for the call that
    failed (cite the line containing the URL/host)
  - For AWS targets, `aws:vpc(<vpc_id>)` or `aws:sg(<sg_id>)` with
    fresh state showing reachability

## Common pitfalls

- **DO NOT classify `Connection refused` as a network problem.** It
  means the network reached the host fine; the destination service
  is just not listening. Route to the destination service's class
  (health_check / java_runtime / ssm) instead.
- **DO NOT classify a wrapper timeout as `network`** unless the
  underlying log shows TCP failure. Use `timeout` for wrapper
  exhaustion.
- **DO NOT recommend opening 0.0.0.0/0 SG rules** as the fix — find
  the specific source CIDR that needs egress.
- **DO NOT recommend `iptables` edits** on the jenkins-rca host or
  Jenkins master — these are managed via SG/NACL, not host
  firewalls.
