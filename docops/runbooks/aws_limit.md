# Runbook: aws_limit

## What this class means
AWS service-quota limit hit. Common quotas: EC2 vCPUs per region,
EBS volume count, Elastic IPs, ALB rules per listener, etc.

## Detect signals
- `VcpuLimitExceeded`
- `InstanceLimitExceeded`
- `Service quota exceeded`
- `RequestLimitExceeded`
- `LimitExceededException`
- `You have requested more vCPU capacity than your current limit`
- `Cannot exceed quota for ...`
- `TooManyUniqueTargetGroupsPerLoadBalancer` (ALB TG-per-LB hit, default 100)
- `TooManyTargetGroups` (account TG quota hit, default 3000)
- `TooManyLoadBalancers` (account ALB quota hit, default 50)
- `TooManyRules` (per-listener rule quota hit, default 100)

## Common mode — ALB target-group count hit (build 5177 case)

**Log signal:**
```
Error: creating ELBv2 Listener Rule: TooManyUniqueTargetGroupsPerLoadBalancer:
  You have reached the maximum number of unique target groups that
  you can associate with a load balancer of type 'application': [100]
status code: 400
with module.createProdPlusOneInfra.module.listener_rule.aws_lb_listener_rule.listener_rule
on ../../../module/listener_rule_for_prod_plus_one/main.tf line 1
```

**What it means:** The ALB has 100 unique TGs attached across all its
listener rules. Adding one more (the new prodplusone TG) is rejected
by AWS. Hard service quota — default 100 per ALB, raisable to ~200
via Service Quotas request.

**ALB ARN derivation (MANDATORY — derive from service.lookup.rule_arn):**

`service.lookup.rule_arn` format:
```
arn:aws:elasticloadbalancing:<region>:<account>:listener-rule/app/<alb-name>/<alb-id>/<listener-id>/<rule-id>
```
Extract ALB ARN by keeping the first 4 slash-segments after `arn:...:`:
```
arn:aws:elasticloadbalancing:<region>:<account>:loadbalancer/app/<alb-name>/<alb-id>
```
Example: `rule_arn` ends with `listener-rule/app/prod-private-internal-alb/fdbcf4c344dbed6d/...`
→ `alb_arn` = `arn:aws:elasticloadbalancing:ap-south-1:735317561518:loadbalancer/app/prod-private-internal-alb/fdbcf4c344dbed6d`

Use this ALB ARN in `suggested_commands`. NEVER emit `<alb_arn>` placeholder.

**Drill:**
1. Derive `alb_arn` from `service.lookup.rule_arn` using the formula above
2. `aws_describe(elbv2, DescribeRules, {ListenerArn: <listener_arn>})` — optional, confirms TG count
3. `repo_read_file("InfraComposer", "module/listener_rule_for_prod_plus_one/main.tf", 1, 80)` — see what the module declares

**Action template (ALB-TG-count case):**
```
Finding: ALB <alb_arn> has hit the AWS service quota
  'Target groups per Application Load Balancer' (default 100).
  Terraform tried to create a new rule for service <svc> with TG
  <new_tg_name> but the ALB already has 100 unique TGs attached.

Action — three paths:
  Option A (RECOMMENDED — cleanup):
    Audit existing TGs on this ALB:
      aws elbv2 describe-target-groups --load-balancer-arn <alb_arn> \
        --region <region> --profile <account_name>
    Identify TGs from decommissioned services (no recent traffic, no
    pipeline references). Delete them via:
      aws elbv2 delete-target-group --target-group-arn <orphan_arn>
    Then re-run pipeline.

  Option B (split ALB — for genuine scale):
    Move <svc> to a different ALB (existing low-utilization one or new
    one). Update InfraComposer/config/<svc>/<env>/main.tf to point at
    the new listener_rule module that targets that ALB. Plan + apply.

  Option C (quota increase):
    AWS Console (account <aws_account>) → Service Quotas → Elastic
    Load Balancing → 'Target groups per Application Load Balancer' →
    Request increase. Max ~200, approval 1-24h.

Verify:
  After cleanup OR quota increase, re-run pipeline; expect the
  aws_lb_listener_rule.listener_rule resource to create cleanly.
```

## Pipeline source to cross-check (MANDATORY)
- The pipeline file that triggers the AWS API (usually
  `vars/createGreenInfra.groovy` running terraform, or
  `vars/nonwebdeploy.groovy` for direct ec2:RunInstances)

## Drill plan
1. `get_jenkins_job_config(job)` → scriptPath
2. `repo_read_file("jenkins_pipeline", "vars/createGreenInfra.groovy", ...)` — see where the AWS call happens
3. Identify which quota from the error message:
   - vCPU type (Running On-Demand <family> instances)
   - EBS volume count
   - Elastic IP count
   - ALB rules per listener
4. From service.lookup: `aws_account`, `aws_region`
5. (Optional, if quota is on listener rules): `aws_describe_listener_rule(<rule_arn>)` to see current rule count

## Action template
```
Finding: AWS quota '<quota name>' exceeded in account <aws_account>
         (<region>). Pipeline requested <amount> but limit is <limit>.
         Resource type: <EC2 family | ALB rule | EBS volume | etc.>.

Action:
  Step 1 (immediate workaround):
    Re-run the pipeline after waiting <N minutes> for in-flight resources
    to settle, OR retry in a different region if cross-region capable.

  Step 2 (correct fix — quota request):
    AWS Console (account <aws_account>) → Service Quotas → search
    '<quota name>' → Request quota increase → set value to <current * 1.5>.
    Approval typically 1-24 hours depending on the service.

  Step 3 (audit):
    Check if existing capacity is properly cleaned up. Some services
    leave zombie resources (orphaned ENIs, undeleted EBS volumes). Run
    `aws_describe_instance(...)` for recent instances in the same TG to
    see if cleanup is needed.
Verify:
  After quota increase: re-run pipeline; expect Infra/Deploy stage to
  pass the AWS API call.
```

## Output schema notes
- `error_class: "aws_limit"`
- `evidence[]` must include:
  - `jenkins_log` with the quota exceeded message
  - `jenkins_pipeline/<helper>:<line>` (the AWS API call site)
  - For quota-on-rules: `aws:listener_rule(<rule_arn>)` showing current state

## Common pitfalls
- DO NOT cite a specific quota number unless it's in the log.
- DO NOT suggest disabling the resource creation — fix the quota.
- DO NOT use BBCTL — this is an AWS console action.
