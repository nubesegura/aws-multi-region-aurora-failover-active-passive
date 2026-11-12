# Cost and cleanup

> **Every cost figure here is an unverified estimate.** It comes from FinOps reviews written before the first deployment: the prices
> are mostly recollections of public on-demand list prices (us regions) and the usage is assumed (below one request per second, a few
> hundred movements). The demo was deployed on 2026-10-14 and extended with Cognito on 2026-11-10, but no bill or Cost Explorer figure has been compared with the estimate yet.
> Re-check in the AWS Pricing Calculator before relying on them.

## Estimated cost while everything runs (730 hours a month)

| Period | Estimate (USD) | Safe upper bound for budgeting |
|---|---|---|
| Per hour | about 0.54 | |
| Per day | about 13.0 | about 15 |
| Per month | about 395 | about 450 |
| An 8-hour drill | about 4.30 | |

By stack:

| Stack | USD per month | USD per day |
|---|---|---|
| bootstrap, edge (two sites, `us-east-2`), dns (`us-east-1`); network stacks (VPC, subnets, route tables: no charge) | about 0 | about 0 |
| cognito (`us-east-2` pool and KMS multi-region key, `us-west-2` replica key; Essentials tier, 2 demo users) | about 2.1 (two KMS keys about 2.00, replication a cent or so; FinOps pre-deploy estimate, unverified) | about 0.07 |
| database `us-east-2` (writer instance 189.80, storage, I/O, replicated I/O, KMS key, master secret, alarms) | about 196.40 | about 6.46 |
| database `us-west-2` (reader instance 189.80, storage, I/O, KMS key, alarms) | about 195.00 | about 6.41 |
| api `us-east-2` (Lambda, API Gateway, logs, alarms) | about 0.37 | about 0.01 |
| api `us-west-2` | about 0.37 | about 0.01 |
| **Total** | **about 395** | **about 13.0** |

The per-stack figures are the original estimate; the banking-ledger change (two more functions per region, more alarm metrics, a second
site) was estimated at about USD 0.95 per month more, and Cognito (pool, two KMS keys, replication, one more alarm) at about USD 2.1 more, so the total is about USD 395 per month (FinOps pre-deploy review of 2026-11-10, no bill compared). The split of the database and api rows was not redone.

### Cost drivers

- **Two provisioned Aurora instances (`db.r6g.large`) are about 97 percent of the cost** (about USD 380 per month in total).
  Each is billed per hour from creation until deletion, whether or not anyone uses it.
- Storage, I/O and cross-region replication traffic are small for a demo.
- Everything else (API Gateway, Lambda, logs, KMS keys, SNS, CloudFront, S3, Route 53 alias records) is under USD 15 per month.
- Eight alarms exist per data region (16 alarms; the errors and throttles alarms use one metric per function, about 36 alarm metrics in
  total at about USD 0.10 each per month, unverified).
- There is **no** NAT gateway, VPC endpoint, WAF (accepted risk), load balancer or public IPv4 address, which would each add a fixed monthly
  charge.
- Not included: the Route 53 hosted zone (assumed to exist; a new one adds about USD 0.50 per month), the domain, data
  transfer from your own clients, and anything outside this project.
- Fallback instance class `db.r5.large` adds about USD 44 per month (about USD 14.33 per day in total).

### How to keep it low

- **Run it for hours, then destroy it.** That is the only large lever. Burstable (`db.t*`) classes are not supported by
  Global Database, and a cluster in a global database cannot simply be stopped as far as is known (verify in the RDS
  documentation before relying on it).
- Reserved instances do not pay off for a demo.
- Billing data lags by 8 to 24 hours, so budget alerts are late warnings; the destroy workflow is the real control.

### Recommended budget alerts (owner action, outside this repository)

| Alert | Setting |
|---|---|
| Monthly budget filtered by tag `project-name=aws-multi-region-aurora-failover-active-passive` | USD 75, notify at 50 %, 80 %, 100 % and forecast 100 % |
| Daily safety budget | USD 15 |
| "After destroy" watch | USD 2 per month on the same tag, to catch leftovers |
| Cost Anomaly Detection | By service, threshold USD 10 |

The tag must be activated as a cost allocation tag in the Billing console before it can filter a budget.

## Destroy everything

Destroying is irreversible: the databases and their data are deleted. A final cluster snapshot is taken for each cluster
(`DeletionPolicy: Snapshot`), but its encryption key is itself scheduled for deletion after 30 days, so the snapshots are
usable for 30 days at most.

### With the workflow

1. *Actions > destroy > Run workflow* on `main`. The Environment `prod` must define `AWS_REGION_PRIMARY`,
   `AWS_REGION_SECONDARY`, `AWS_REGION_GLOBAL` and the secret `AWS_ROLE_ARN` (the organization GitHub role; and, optionally,
   `HOSTED_ZONE_ID`, used only to list the ACM validation records). `destroy` shares the concurrency group
   `drdemo-prod-infrastructure` with deploy, simulate-outage and failover.
2. Type `destroy-aws-multi-region-aurora-failover-active-passive` in `confirm` and `disable-deletion-protection` in `confirm_deletion_protection`.
3. The workflow runs `scripts/destroy.sh` in three groups with a fresh OIDC login before each (the role session lasts one hour):
   `--steps 1-3`, `--steps 4-6` (databases, networks and edge at the same time), `--steps 7-8`.

### From a workstation

```bash
export AWS_REGION_PRIMARY=us-east-2 AWS_REGION_SECONDARY=us-west-2 AWS_REGION_GLOBAL=us-east-1
bash scripts/destroy.sh --dry-run     # prints the whole ordered plan, calls no AWS API
bash scripts/destroy.sh               # asks you to type both phrases
```

Options: `--confirm destroy-aws-multi-region-aurora-failover-active-passive`, `--confirm-deletion-protection disable-deletion-protection` (to pass the phrases
as flags), `--steps 1-8` (or `1-3`, `4-6`, `7-8`, `4`), `--eni-wait-seconds N` (pause before the database stacks, default 180).

### Order

| Step | What happens |
|---|---|
| 0 | You type `destroy-aws-multi-region-aurora-failover-active-passive` |
| 1 | You type `disable-deletion-protection`; deletion protection is switched off on both clusters and on the global cluster (`DeletionProtection` in `parameters/prod.json` is `true` and keeps it on, so nothing else can delete them by accident; the primary Cognito user pool has its own deletion protection, lifted in step 3) |
| 2 | dns stack deleted (the weighted records go away) |
| 3 | api stacks deleted in both regions, then the Cognito stacks: pool deletion protection lifted, replica attachment set `INACTIVE`, the replica-region stack deleted before the primary one, remaining Cognito KMS keys scheduled for deletion (7-day minimum window) |
| 4 | The **live** writer is read with `describe-global-clusters` (roles may have swapped during drills); every member of the global cluster is detached, non-writers first, the writer last |
| 5 | Database stacks deleted, the former secondary first; each cluster takes a final snapshot. Then the network stacks of both data regions (VPC, subnets, route tables). Lambda network interfaces can delay the VPC deletion for some minutes: the script pauses (`--eni-wait-seconds`) and you can run it again |
| 6 | The dashboard and banking buckets are emptied including every version and delete marker, then the edge stack deleted |
| 7 | Artifact buckets emptied (all versions), bootstrap stacks of the two data regions deleted |
| 8 | Leftovers listed, with the commands to delete them |

The script is safe to repeat (missing stacks are skipped). A stack that fails to delete is reported with its failed
resources and the script stops; it never retries or forces a deletion.

### What to delete by hand afterwards

| Item | Why it remains | How |
|---|---|---|
| Stack `stk-useast1-drdemo-bootstrap-prod` (CloudFormation execution role and policies, the unused dedicated deploy role, global artifact bucket) | It is applied by hand and the workflows never touch it. Its bucket is already empty | Owner credentials: `aws cloudformation delete-stack --region us-east-1 --stack-name stk-useast1-drdemo-bootstrap-prod` |
| SSM SecureString `/drdemo/prod/api-key` (`us-east-1`), **only if it still exists from the baseline deployment** | Created by the removed `scripts/init-api-key.sh` during spec 001, not by a stack; nothing uses it any more | `aws ssm delete-parameter --region us-east-1 --name /drdemo/prod/api-key` (owner credentials) |
| Demo users `bank-operator` and `dashboard-viewer` | Live inside the Cognito pool and go with it | Nothing |
| Account baseline stacks `stk-<region code>-baseline-iam-prod` (OIDC provider, API Gateway log role) | Account-wide resources of another repository (`nubesegura-accounts-baseline`); not part of this project | Nothing |
| Final cluster snapshots (both regions) | `DeletionPolicy: Snapshot`; a few cents per month | The script prints `aws rds delete-db-cluster-snapshot --region <region> --db-cluster-snapshot-identifier <id>` for each |
| KMS keys pending deletion | 30-day deletion window for the database keys (the snapshots need them until then); the Cognito keys use the 7-day minimum | Nothing, or cancel with `aws kms cancel-key-deletion --region <region> --key-id <id>` if you still need a snapshot |
| Master secret `sm-useast2-drdemo-master-prod-<suffix>` | Removed with the database stack; a deleted secret keeps its name reserved during its recovery window (the suffix avoids a clash on redeploy) | Listed by step 8; wait for the window or delete it with `aws secretsmanager delete-secret` if you want it gone sooner |
| CloudWatch log groups | Groups that CloudFormation did not remove keep their retention | Step 8 prints `aws logs delete-log-group --region <region> --log-group-name '<name>'` (check the prefix first, some are shared) |
| ACM DNS validation CNAME records in your hosted zone | CloudFormation does not remove them | Step 8 lists their names (needs `HOSTED_ZONE_ID`); delete them with your own credentials once the certificates are gone |
| API Gateway account setting of the two data regions | Owned by the account baseline, not by this project: it keeps pointing to the baseline log role, which still exists. | Nothing |
| **Any Aurora cluster you created by hand** during the unplanned-failover rebuild ([runbook U7](dr-runbook.md#u7-manual-rebuild-of-a-secondary-in-us-east-2-for-failback)) | Not owned by CloudFormation; about USD 6.40 per day each | Check `aws rds describe-db-clusters` in both regions after the destroy and delete what remains |
| Manual snapshots you took during the drill | Not tagged for cleanup | `aws rds describe-db-cluster-snapshots --snapshot-type manual` in both regions |
| The hosted zone, if you created one just for the demo | Outside this project | Delete it in Route 53 (about USD 0.50 per month) |
| GitHub Environment `prod` variables and secrets; budget alerts | Outside AWS | Remove them if the demo is over |

### Verify that nothing is left (read-only)

```bash
aws cloudformation list-stacks --region us-east-2 --query "StackSummaries[?contains(StackName,'drdemo') && StackStatus!='DELETE_COMPLETE'].StackName"
aws cloudformation list-stacks --region us-west-2 --query "StackSummaries[?contains(StackName,'drdemo') && StackStatus!='DELETE_COMPLETE'].StackName"
aws cloudformation list-stacks --region us-east-1 --query "StackSummaries[?contains(StackName,'drdemo') && StackStatus!='DELETE_COMPLETE'].StackName"
aws rds describe-db-clusters --region us-east-2 --query 'DBClusters[].DBClusterIdentifier'
aws rds describe-db-clusters --region us-west-2 --query 'DBClusters[].DBClusterIdentifier'
aws rds describe-global-clusters --region us-east-2 --query 'GlobalClusters[].GlobalClusterIdentifier'
```

Only the global bootstrap stack should remain until you delete it by hand. Then watch the "after destroy" budget for a
few days: billing data lags by 8 to 24 hours.

The destroy procedure has not been run against the deployed account yet (spec task A4.8 is open). Treat the list above as a checklist to
confirm, not as a guarantee.
