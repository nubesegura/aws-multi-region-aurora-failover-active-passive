# DR runbook: simulate the loss of us-east-2, promote us-west-2, fail back

Spanish copy: [docs-es/dr-runbook.md](../docs-es/dr-runbook.md) (English is the source; if they differ, English wins).

> **Honesty notes.**
> 1. The drill **simulates** a regional failure by throttling the functions of `us-east-2` to zero concurrency. It does
>    not reproduce a real regional outage: in a real one the database, the network and the control plane of the region
>    are gone too. The simulation is reversible; a real failover is not.
> 2. Aurora Global Database replication is **asynchronous**. The usual lag is about one second, so an unplanned failover
>    can lose the writes of roughly the last second. That is a typical value, not a guarantee. A planned switchover loses nothing.
> 3. **The planned drill was run on AWS twice**: by hand on 2026-10-14 and, with the automated runner, on 2026-11-11 (see
>    [Observed values](#observed-values) and the [DR drill test report](dr-test-report.md)). The
>    **unplanned path (U1 to U8) and the manual rebuild of a secondary (U7) were never run**: they were written from the AWS
>    documentation. Do them in a throwaway account first.
> 4. The RTO target is 15 minutes. In the one measured run of 2026-11-11 the write RTO was 196.8 s to the secondary and 82.7 s back
>    to the primary (the whole drill, from baseline to ledger check, took about 12 minutes); one run is not a guarantee.
> 5. Deletion protection is **on** again (`DeletionProtection` is `true` in `parameters/prod.json`, restored by commit `9c40f1b`); the
>    destroy procedure switches it off first, after a typed confirmation.
> 6. The 2026-10-14 values below are history from the **API key era**. Since spec 002 the API uses Cognito Bearer ID tokens
>    and the `failover` workflow also activates and deactivates the Cognito replica (about 40 s per direction). The current
>    measurement is the 2026-11-11 drill in the [test report](dr-test-report.md).

## Cheat sheet

### Planned drill with the workflows (both regions healthy, no data loss)

Run from GitHub: *Actions > simulate-outage* and *Actions > failover*, branch `main`, Environment `prod`. They use the same scripts
as the table below, stop at the first failing step and write a job summary (actor, seconds per step, writer region before and after,
consistency result). No `--force`, `--unplanned` or `FORCE_UNFENCE` is ever passed by a workflow.

| # | Workflow run | Inputs | Expected result | Undo |
|---|---|---|---|---|
| 0 | `simulate-outage` | `action` = `status`, `region` = `us-east-2` | Lists the six functions as not fenced | Nothing (read-only) |
| 1 | `simulate-outage` | `action` = `fence`, `region` = `us-east-2`, `confirm` = `us-east-2` | Reserved concurrency 0 on the six functions of us-east-2; the banking app starts to retry | `simulate-outage` with `unfence` |
| 2 | `failover` | `direction` = `to-secondary`, `confirm` = `failover` | Requires us-east-2 fenced and us-west-2 not fenced; **activates the Cognito replica in us-west-2** (`ReplicaAttachmentStatus=ACTIVE`); planned switchover; weights 0 / 255; validation (region check through `GET /health?deep=true`); ends green when every step passes | `failover` with `to-primary` |
| 3 | `failover` | `direction` = `to-primary`, `confirm` = `failover` | Unfences us-east-2, switches the database back, **deactivates the Cognito replica** (`ReplicaAttachmentStatus=INACTIVE`), weights 255 / 0, validation | `failover` with `to-secondary` |

Notes for the workflows:

- `simulate-outage`, `failover`, `deploy` and `destroy` share the concurrency group `drdemo-prod-infrastructure`: they never run at the
  same time. GitHub keeps only one **pending** run per group, so a queued `deploy` can silently replace a queued `failover` (and the
  other way round). After dispatching, open the run list and confirm that your run started.
- The workflows need the `prod` Environment variables (`AWS_REGION_PRIMARY`, `AWS_REGION_SECONDARY`, `AWS_REGION_GLOBAL`,
  `API_DOMAIN_NAME`, `HOSTED_ZONE_ID`) and the secret `AWS_ROLE_ARN`. They hold no Cognito token or password. Without a token the
  workflow validates only the serving region through `GET /health?deep=true`: the `/stats` baseline, the 0.01 deposit probe and the
  ledger consistency check (`45-consistency.sh`) are **skipped** in the workflow (the step prints that it skipped). Run
  `bash scripts/dr/45-consistency.sh` yourself with a token afterwards (see below) to prove the ledger.
- A failing step leaves the system as it is. Read the summary (writer region before and after) and the log before dispatching again.
- `failover` to the secondary also refuses to start when the secondary region is fenced (unfence it first).

### Planned drill with the scripts (both regions healthy, no data loss)

Run from the repository root with **your owner credentials**. Variables to export first:
`AWS_REGION_PRIMARY=us-east-2`, `AWS_REGION_SECONDARY=us-west-2`, `AWS_REGION_GLOBAL=us-east-1`,
`API_DOMAIN_NAME=api.example.com`. The protected routes need a Cognito **ID token** of a demo user (`bank-operator` or
`dashboard-viewer`, created by `.github/workflows-local/provision-users/provision_users.py`): export it as `COGNITO_ID_TOKEN`. It is
valid for 15 minutes, so get a fresh one before each validation (sign in to the banking app, or call Cognito `InitiateAuth` with
`USER_PASSWORD_AUTH` against the bank app client; never paste a password or token into a shared log). `45-consistency.sh` requires it;
`40-validate.sh` uses it for `/stats` and the probe and without it checks only `GET /health?deep=true`; `00-precheck.sh` records a
`/stats` baseline only when both `API_DOMAIN_NAME` and `COGNITO_ID_TOKEN` are exported. The scripts never print the token. Every script
accepts `--dry-run` and `--help`.

**Cognito replica in the script path.** The workflow `failover` activates the replica in us-west-2 before the switchover and deactivates
it on `to-primary`; the individual scripts of the table below do **not** touch Cognito. If you run the steps by hand, activate the replica
right before step 2 and deactivate it after step 5a, with the same helper the workflow uses (needs the CloudFormation execution role in
`CFN_EXEC_ROLE_ARN` when you do not use the owner role):

```bash
bash -c 'set -euo pipefail; source scripts/lib/common.sh; source scripts/lib/dr.sh; cognito_replica_set_status ACTIVE'    # or INACTIVE
```

It updates the `cognito` stack of us-west-2 with `ReplicaAttachmentStatus=<ACTIVE|INACTIVE>` and keeps its other parameters. The local
runner `.github/workflows-local/failover.sh` does it for you.

| # | Step | Command | Expected result | Verify | Undo |
|---|---|---|---|---|---|
| 0 | Pre-checks | `bash scripts/dr/00-precheck.sh` | us-east-2 writer, us-west-2 reader, lag below 5000 ms, no fence, weights 255/0, `/stats` baseline printed (needs `COGNITO_ID_TOKEN`) | Exit code 0 | Nothing (read-only) |
| 1 | Simulate the loss of us-east-2 | `bash scripts/dr/10-simulate-outage.sh` | Reserved concurrency 0 on the 6 functions of us-east-2 | The banking app shows "recovering", the dashboard shows Unavailable; `40-validate.sh` fails (`10-simulate-outage.sh --status` reports the fence) | `bash scripts/dr/10-simulate-outage.sh --undo` |
| 2 | Promote us-west-2 (planned switchover) | `bash scripts/dr/20-promote.sh` | `switchover finished: us-west-2 is the writer. No data was lost.` | `bash scripts/dr/00-precheck.sh` shows the roles swapped | `bash scripts/dr/50-failback-promote.sh` |
| 3 | Switch Route 53 to us-west-2 | `bash scripts/dr/30-switch-traffic.sh` | dns stack updated, weights 0 / 255 | `bash scripts/dr/40-validate.sh --expect-region us-west-2 --wait 180 --probe` | `bash scripts/dr/30-switch-traffic.sh --to primary` (only while us-east-2 is the writer) |
| 4 | Validate | Banking app, dashboard, Postman folder `04 DR drill`, `40-validate.sh --probe`, `bash scripts/dr/45-consistency.sh` | `served_by_region` us-west-2, `database_role` writer, balance and counts not lower than the baseline, ledger consistent | `validation passed`, `consistent: true` (exit 0) | n/a |
| 5a | Fail back: database | `bash scripts/dr/50-failback-promote.sh` | us-east-2 is the writer again | `bash scripts/dr/00-precheck.sh` | `bash scripts/dr/20-promote.sh` |
| 5b | Fail back: fence and traffic | `bash scripts/dr/60-restore.sh` | Fence lifted, weights 255 / 0 | `bash scripts/dr/40-validate.sh --expect-region us-east-2 --wait 180 --probe` | `bash scripts/dr/10-simulate-outage.sh` and `bash scripts/dr/30-switch-traffic.sh --to secondary` |
| 6 | Final state check | `bash scripts/dr/00-precheck.sh` | Normal state again | Exit code 0 | n/a |

### Unplanned path (real regional outage, data loss possible)

| # | Step | Command | Expected result | Verify | Undo |
|---|---|---|---|---|---|
| U1 | Record what you know | `bash scripts/dr/00-precheck.sh` (it may report problems for us-east-2) | Last baseline, weights, live roles | Note the last replication lag (CloudWatch, us-west-2) | Nothing |
| U2 | Fence us-east-2, then promote with data loss | `bash scripts/dr/20-promote.sh --unplanned` | Typed confirmations, then `failover finished: us-west-2 is the writer` | Members list in the output; `00-precheck.sh` | **None.** Not reversible |
| U3 | Switch Route 53 | `bash scripts/dr/30-switch-traffic.sh` | Weights 0 / 255, old region fenced | `bash scripts/dr/40-validate.sh --expect-region us-west-2 --wait 180 --probe` | Only after a rebuild, see U8 |
| U4 | Validate | Banking app, dashboard, Postman folder `04`, `40-validate.sh --probe`, `45-consistency.sh` | us-west-2 serves reads and writes | `validation passed`; the consistency report states what, if anything, is inconsistent | n/a |
| U5 | Measure the data gap | Compare the baseline with `/stats` (section U5) | Number of writes missing | Written note | n/a |
| U6 | Keep us-east-2 fenced | `bash scripts/dr/10-simulate-outage.sh` when the region answers again | Fence set | `bash scripts/dr/00-precheck.sh` shows reserved concurrency 0 | Only after U8 (`60-restore.sh`) |
| U7 | Rebuild a secondary in us-east-2 | Manual AWS CLI steps (section U7) | New cluster joins the global cluster as a reader | `describe-global-clusters`, `00-precheck.sh` | Delete the new cluster |
| U8 | Fail back | `bash scripts/dr/50-failback-promote.sh`, then `bash scripts/dr/60-restore.sh` | Normal state | `bash scripts/dr/40-validate.sh --expect-region us-east-2 --wait 180 --probe` | See steps 5a and 5b |

### Typed confirmations and overrides

| Where | Phrase or variable | Meaning |
|---|---|---|
| `20-promote.sh --unplanned` | type `allow-data-loss us-west-2` | Accept that the last writes can be lost. Always typed on an interactive terminal: there is no flag or variable for it, so it cannot run from a non-interactive job |
| `20-promote.sh --unplanned`, `30-switch-traffic.sh` | type `proceed-unfenced us-east-2` or set `PROMOTE_UNFENCED_CONFIRM` to that phrase | Continue although the old region could not be fenced (its API is unreachable) |
| `10-simulate-outage.sh --undo`, `60-restore.sh` | type `unfence-standalone <region>` or set `FORCE_UNFENCE` to that phrase | Lift the fence on a cluster that is **not** a member of the global cluster (it may be writable: split brain) |
| `20-promote.sh`, `50-failback-promote.sh` | `--yes` | Skip the y/N question of a planned switchover |
| `10-simulate-outage.sh --undo`, `60-restore.sh` | `RESERVED_CONCURRENCY=<n>` | Restore a specific reservation instead of removing it (the template sets none) |
| `30-switch-traffic.sh`, `60-restore.sh` | `CFN_EXEC_ROLE_ARN` | Pass the CloudFormation execution role (`--role-arn`) when updating the dns stack (the `failover` workflow reads it from the global bootstrap stack) |
| Workflow `simulate-outage`, `action` = `fence` | `confirm` = the region name | Typed confirmation for every fence |
| Workflow `failover` | `confirm` = `failover` | Typed confirmation for either direction |

## Observed values

### Measured drill (2026-11-11, current)

One automated planned drill, there and back, on the deployed demo with Cognito authentication (one run, not statistics). Full tables,
method and limits: [DR drill test report](dr-test-report.md). Verdict PASS.

| Measure | Observed |
|---|---|
| Write RTO, to-secondary | 196.8 s (writes unavailable 196.2 s) |
| Write RTO, to-primary | 82.7 s (writes unavailable 82.5 s) |
| `failover` workflow, total | 223 s to-secondary, 236 s to-primary (each includes about 40 s for the Cognito replica step) |
| Measured RPO (planned switchover) | 0 acknowledged writes lost: 504 movements stored, 504 expected |
| Replication lag during the run | `AuroraGlobalDBReplicationLag` maximum 933 ms; `AuroraGlobalDBRPOLag` maximum 1531 ms |

The write RTO is the time the write probe could not get an acknowledged write served from the new region. It is **not comparable** with
the 195 s of 2026-10-14 below, which was the total duration of the `failover` workflow. Do not quote the DNS convergence of the report
(negative in both directions: the DNS answer changed before the traffic-switch step ended) nor the secondary sign-in availability (70 % and
43 %, partly by design because the workflow activates and deactivates the replica). Not exercised: the unplanned failover.

### Observed values (2026-10-14, history)

One planned drill, there and back, on the deployed demo with the **API key** authentication of spec 001 (owner-reported values; one run
each, not statistics). They are kept as history and were measured with a different metric (total workflow time); the current values are above.

| Step | Observed |
|---|---|
| Fence of us-east-2 (`simulate-outage`) | 41 s |
| `failover` to-secondary, total | 195 s (planned switchover 36 s, DNS weight change about 70 s) |
| `failover` to-primary, total | 170 s (planned switchover 35 s) |
| Writes unavailable per switchover | about 35 to 40 s |
| Ledger after both moves | consistent (`45-consistency.sh`); no acknowledged movement lost |

Not observed: the unplanned failover, the data gap measurement (U5), the rebuild of a secondary (U7), the time resolvers and keep-alive
connections take to follow a weight change on a client you do not control, and a run with a non-zero replication lag.

## Why manual weighted routing and not failover routing with health checks

The demo uses two Route 53 weighted alias records (weights 255 and 0) that a person changes. Automatic failover routing
with health checks was considered and rejected:

- **The database must be promoted before the traffic moves.** Failover routing flips DNS as soon as a health check
  fails. The secondary region's database is a read-only copy until somebody promotes it, so every write sent there
  would answer `503 database_read_only`. Traffic must follow the promotion, never precede it.
- **Promotion is a human decision.** An unplanned failover (`--allow-data-loss`) can lose writes and detaches the old
  primary for good. A health check cannot decide that.
- **Split brain.** After an unplanned failover the old primary may still accept writes. The old region must be fenced
  (functions throttled to zero) before any traffic moves; health checks know nothing about that.
- **False alarms and cost.** A flapping health check would move clients to a read-only region; health checks also cost money.
- **Rollback is trivial** with weights (255 / 0 and back), and the same stack update works while `us-east-2` is down
  because it runs in `us-east-1`.

The `failover` workflow automates exactly that order for the **planned** case (fence check, switchover, wait for the writer, then the
weights), triggered by a person. A health-check-driven version stays out of scope. See
[ADR 0003](adr/0003-manual-weighted-routing.md) and [ADR 0015](adr/0015-planned-switchover-only.md).

## Conventions of the scripts

- Vocabulary: "primary region" and "secondary region" mean the regions **at creation** (`AWS_REGION_PRIMARY` is
  `us-east-2`, `AWS_REGION_SECONDARY` is `us-west-2`). After a promotion the real writer is the other one; every script
  reads the live roles from `describe-global-clusters` and never assumes.
- Each script prints the account (masked), the regions and timestamped actions (UTC). `--help` prints its header,
  which documents its undo. `--dry-run` calls no AWS API: use it to rehearse.
- The database stacks (`stk-useast2-drdemo-database-prod`, `stk-uswest2-drdemo-database-prod`) are **never redeployed
  during or after a drill**. RDS swaps the roles; CloudFormation will show the pre-swap roles (drift) by design
  ([ADR 0005](adr/0005-database-stacks-not-redeployed-during-drills.md)). `scripts/deploy.sh` refuses to touch them
  while the roles are swapped.
- The `lag_gate` of a planned switchover (`scripts/lib/promote.sh`) waits up to 5 minutes for the first
  `AuroraGlobalDBReplicationLag` datapoint of a cluster that just became a secondary (CloudWatch publishes it 2 to 3 minutes after a
  switchover), then fails closed: no datapoint or a failed query stops the step before anything is changed. Going to the secondary and
  straight back therefore waits, by design.
- The functions connect to the **cluster endpoint** of their region, which is a reader before a promotion and a writer
  after it. Nothing is redeployed in either region during a drill. A warm function that held a connection to the old
  writer discards it on the first error and retries once on a fresh connection, and `database_role` is always read on a
  fresh one ([ADR 0013](adr/0013-connection-discard-and-single-retry.md)); the first request after a promotion can be a
  little slower, but it should not report a stale role.
- Deployment notes that matter during a drill: the global bootstrap stack is never touched by the deploy or destroy workflows (it is applied
  by hand with a change set, [ADR 0016](adr/0016-organization-github-role-and-account-baseline.md)); `templates/bootstrap.yaml` is under the
  51,200 byte inline limit of CloudFormation, and `scripts/deploy.sh` sends any larger template through the artifact bucket of its region.

## Step 0. Prerequisites and pre-checks

Prerequisites:

- The demo is deployed and healthy ([README](../README.md#deploy)); the monitoring dashboard works and shows `us-east-2` / `writer`.
- For the **workflows**: the `prod` Environment variables and the secret `AWS_ROLE_ARN` of the README, and the two demo users in the
  Cognito user pool (provisioned once with `.github/workflows-local/provision-users/provision_users.py`).
- For the **scripts and the unplanned path**: owner credentials with permission to read RDS, CloudWatch, Lambda and CloudFormation, to call
  `rds:SwitchoverGlobalCluster` / `rds:FailoverGlobalCluster`, `lambda:PutFunctionConcurrency` and
  `lambda:DeleteFunctionConcurrency`, to update the dns stack (CloudFormation and Route 53) and to update the `cognito` stack of the replica region (the activation of the replica).
- Tools: AWS CLI v2, `bash` 4+, GNU `date`, `jq`, `curl`.
- Exported variables: `AWS_REGION_PRIMARY`, `AWS_REGION_SECONDARY`, `AWS_REGION_GLOBAL`, `API_DOMAIN_NAME` (and `COGNITO_ID_TOKEN` for the
  authenticated checks).
- Postman imported with `baseUrl` (`https://api.example.com`) and `cognito_id_token` (secret) set; the banking app and the monitoring
  dashboard open in two browser tabs, signed in as `bank-operator` and `dashboard-viewer`.
- A short rehearsal: `bash scripts/dr/00-precheck.sh --dry-run` and the same for the other scripts.

**Command**

```bash
bash scripts/dr/00-precheck.sh
```

**Expected result.** Five numbered sections: (1) the global cluster members with their live roles (`us-east-2 ... writer`,
`us-west-2 ... reader`, `sync=...`); (2) both clusters `status=available`; (3) the replication lag of the reader from
CloudWatch (`AuroraGlobalDBReplicationLag`, milliseconds) and the RPO reading; (4) the reserved concurrency of the six
functions in each region (`unreserved`) and the Route 53 weights (`us-east-2 (record use2)=255  us-west-2 (record usw2)=0`);
(5) a baseline of `/stats` (`served_by_region`, `database_role`, `total_count`, `counts_by_region`,
`last_processed_at_by_region`), printed only when `API_DOMAIN_NAME` and `COGNITO_ID_TOKEN` are exported. **Write the baseline down**
(also the balance shown by the banking app): it is how you measure data loss later.

**How to verify.** Exit code 0 and `precheck finished`. The script exits 1 when a cluster is not `available` or the lag is
above 5000 ms (do not start a planned switchover then). In the monitoring dashboard the active region is `us-east-2` and the role `writer`.
Run Postman folder `04 DR drill` once; all four requests pass and `X-Served-By-Region` is `us-east-2`.

**How to undo.** Nothing: it is read-only.

## Planned drill

### Step 1. Simulate the loss of us-east-2

This sets reserved concurrency 0 on the six functions of `us-east-2` (`create-transaction`, `list-transactions`,
`get-stats`, `get-health`, `get-account`, `get-consistency`). It first checks that all six exist and changes nothing otherwise.
With the workflow: `simulate-outage`, `action` = `fence`, `region` = `us-east-2`, `confirm` = `us-east-2`. Lambda then rejects every invocation, so every request that Route 53 still sends to
`us-east-2` fails. The database, API Gateway and DNS of `us-east-2` keep working: **this simulates, it does not reproduce,
a regional failure.**

**Command**

```bash
bash scripts/dr/10-simulate-outage.sh
```

(`--region <region>` targets another region, `--status` only reports; the default region is `AWS_REGION_PRIMARY`.)

**Expected result.** `simulating the loss of us-east-2 ...`, `fence set in us-east-2`, then six lines
`us-east-2 <function>: reserved concurrency 0`.

**How to verify.** The banking app shows "recovering, attempt N" for an operation; the dashboard turns to the "Unavailable" banner and
keeps retrying; Postman requests fail; `bash scripts/dr/40-validate.sh` ends with `validation FAILED`. The CloudWatch alarms `...lambda-throttles...` and `...api-5xx...`
of `us-east-2` go to ALARM (an e-mail arrives if `ALERT_EMAIL` is set): that is the expected signal.

**How to undo.** `bash scripts/dr/10-simulate-outage.sh --undo` (workflow: `action` = `unfence`) removes the reservation (set `RESERVED_CONCURRENCY=<n>`
to restore a specific value). It checks first that the cluster of that region is a member of the global cluster; for a
detached cluster it asks for the typed phrase (see the cheat sheet).

### Step 2. Promote us-west-2 (planned switchover)

Aurora waits until the secondary has caught up, then swaps the roles with no data loss. Writes pause for a short time (about 35 to 40 s
in the observed run). With the workflows, steps 2 and 3 are one `failover` run with `direction` = `to-secondary`, which first activates the Cognito replica in
us-west-2 (`ReplicaAttachmentStatus=ACTIVE`) so that the secondary region can authenticate users while it serves traffic.

**Command**

```bash
bash scripts/dr/20-promote.sh
```

Options: `--max-lag-ms N` (default 5000), `--force` (ignore the lag limit and a missing datapoint; terminal only), `--yes` (skip the y/N question),
`--api-region <region>` (the region whose RDS API is called; default is the old writer's region, use
`--api-region us-west-2` if that API does not answer).

**Expected result.** The members list, `replication lag of the cluster in us-west-2: <n> ms (limit 5000 ms)`, the y/N
question (`Planned switchover: us-west-2 becomes the writer, us-east-2 becomes a reader ...`), then polling lines
`us-west-2: reader (check 1 of 80)` that end with `us-west-2: writer`, and
`switchover finished: us-west-2 is the writer. No data was lost.` The script waits up to 20 minutes for the role change and, before
the question, up to 5 minutes for the first lag datapoint of a new secondary (`no replication lag datapoint for ... yet ... waiting 30 s`).

**How to verify.** `bash scripts/dr/00-precheck.sh` now lists `us-west-2 ... writer` and `us-east-2 ... reader` (the script
reads the live roles). The dashboard is still unavailable: DNS still points to the fenced region. Do **not** redeploy the
database stacks.

**How to undo.** `bash scripts/dr/50-failback-promote.sh` (the mirror switchover back to `us-east-2`).

### Step 3. Switch Route 53 to us-west-2

Moves the weights from 255 / 0 to 0 / 255 by updating the dns stack in `us-east-1`. It does not need `us-east-2`.

**Command**

```bash
bash scripts/dr/30-switch-traffic.sh
```

(`--to secondary` is the default; `--force` skips the "target is the writer" check and is only for tests.)

**Expected result.** `goal: send all traffic to us-west-2`, the current and the new weights, `the cluster in us-east-2 is a
reader of the same global cluster ...` (a detached or unknown old cluster would be fenced first), the CloudFormation
deploy of `stk-useast1-drdemo-dns-prod`, `dns weights: us-east-2 (record use2)=0  us-west-2 (record usw2)=255`, and
`done. New requests reach us-west-2 within about 60 s (TTL) plus resolver and keep-alive caching.`

The script refuses to move traffic to a region whose cluster is not the writer; promote first.

**How to verify**

```bash
bash scripts/dr/40-validate.sh --expect-region us-west-2 --wait 180 --probe
```

It polls `/stats` every 10 seconds up to 180 seconds until `served_by_region` is `us-west-2`, then prints `database_role`,
the header `X-Served-By-Region`, the counts, calls `/health?deep=true`, and posts one probe: a **deposit of 0.01** (there is no delete
API, so the balance of the demo account keeps that cent; a later withdrawal of 0.01 compensates it). Expected: `validation passed`.
Then check the ledger:

```bash
bash scripts/dr/45-consistency.sh
```

Exit 0 means `consistent: true` (replayed and stored balance equal, chain intact, no duplicated key); 1 means inconsistent or no valid
answer; 2 means the ledger is larger than 50,000 rows and nothing is claimed.

**How to undo.** `bash scripts/dr/30-switch-traffic.sh --to primary`. This is only valid while the `us-east-2` cluster is
the writer; if the roles are swapped, undo step 2 first, because moving traffic to a reader would answer
`503 database_read_only`.

### Step 4. Validate on the dashboard and with Postman

1. Monitoring dashboard: the active region changes to `us-west-2` and the change is highlighted in the banner and the timeline; the
   database role is `writer`; the balance and the total are at least the baseline (a planned switchover loses nothing) and the old
   movements are still listed under `us-east-2`. Press **Verify**: the report is consistent.
   Banking app: execute a deposit; it is processed by `us-west-2` and the balance grows. **Verify my operations** reports none missing.
2. Postman folder `04 DR drill`: `Drill: where am I served from (stats)`, `Drill: health deep (region and role)`,
   `Drill: write probe` and `Drill: header matches body region` pass. `served_by_region` is `us-west-2`.
3. Some clients can still reach `us-east-2` for a while (resolver caches, keep-alive connections that ignore DNS): reload
   the dashboard tab if it stays on the old region. They get errors, not data divergence, because `us-east-2` is
   fenced and its database is now a reader.

**How to undo.** Nothing to undo; validation only adds a probe row if you asked for `--probe`.

### Step 5. Fail back to us-east-2

Failback mirrors the forward path. It requires the planned path above: the `us-east-2` cluster must be a reader member of
the global cluster.

**5a. Promote the original primary again**

```bash
bash scripts/dr/50-failback-promote.sh
```

Same options and output as step 2, mirrored: `switchover finished: us-east-2 is the writer. No data was lost.` Verify with
`bash scripts/dr/00-precheck.sh`. Undo: `bash scripts/dr/20-promote.sh`.

**5b. Lift the fence and move traffic back**

```bash
bash scripts/dr/60-restore.sh
```

It checks that the `us-east-2` cluster is the writer, lifts the fence (reserved concurrency removed, refused for a
detached cluster without the typed phrase), runs `30-switch-traffic.sh --to primary` (weights 255 / 0) and prints the fence
state of both regions (none should be fenced). Expected last line:
`restored. Run: scripts/dr/40-validate.sh --expect-region us-east-2 --wait 180 --probe`.

**5c. Validate**

```bash
bash scripts/dr/40-validate.sh --expect-region us-east-2 --wait 180 --probe
bash scripts/dr/45-consistency.sh
```

Expected `validation passed` and `consistent: true`, dashboard on `us-east-2` / `writer`, counts include everything written during the drill.
With the workflows, steps 5a to 5c are one `failover` run with `direction` = `to-primary`, which unfences the primary **first** (its database is
then a reader and serves reads only while traffic is still on the secondary), then switches the database, the weights and validates.
Undo of 5b: `bash scripts/dr/10-simulate-outage.sh` (fence again) and `bash scripts/dr/30-switch-traffic.sh --to secondary`.

### Step 6. Final state check

`bash scripts/dr/00-precheck.sh` shows the normal state: `us-east-2` writer, `us-west-2` reader, lag below 5000 ms, no
fence, weights 255 / 0. The CloudWatch alarms return to OK. If you are finished, destroy the demo
([cost-and-cleanup.md](cost-and-cleanup.md)): it costs about USD 13 per day while it runs (unverified estimate).

## Unplanned path: a real regional outage

Use this only when `us-east-2` is really unavailable and you accept losing the last writes. The scripts are the same ones;
the differences are the fence, the typed confirmations, and that the old primary cannot be switched back.

**What you accept.** Replication is asynchronous (RPO about one second typically, not zero). `failover-global-cluster
--allow-data-loss` promotes the secondary at whatever point replication had reached and **detaches** the old primary from
the global cluster. The old primary, when its region returns, is a standalone cluster that **may still accept writes**
(split brain). So the order is always: **fence the old region, promote the database, then change DNS.**

### U1. Record what you know

```bash
bash scripts/dr/00-precheck.sh
```

It tries both regions for the global cluster, so it still works with one region down. It will report problems for
`us-east-2` (exit 1); that is expected. Keep the last baseline (from the pre-check before the outage, if you have it) and
open CloudWatch in `us-west-2`: the metric `AuroraGlobalDBReplicationLag` of `rdsc-uswest2-drdemo-prod` before the outage
tells you how many milliseconds of writes the failover could lose. Undo: nothing.

### U2. Fence the old region and fail over

```bash
bash scripts/dr/20-promote.sh --unplanned
```

What it does, in order: prints the RPO reading; tries to fence `us-east-2` (reserved concurrency 0). If the Lambda API of
`us-east-2` does not answer it warns and asks you to type `proceed-unfenced us-east-2` (or set `PROMOTE_UNFENCED_CONFIRM`
to that phrase); then it asks you to type `allow-data-loss us-west-2`; then it calls `failover-global-cluster
--allow-data-loss` through the RDS API of `us-west-2` and polls until `us-west-2` is the writer. If the API call fails,
retry with `--api-region us-east-2` only if that region answers.

**Expected result.** `failover finished: us-west-2 is the writer. KEEP us-east-2 FENCED until its cluster is deleted or
rebuilt as a secondary.`

**How to verify.** The members list printed by the script, or `aws rds describe-global-clusters --region us-west-2`.

**How to undo.** There is no undo. The old primary is detached. The way back is U7 and U8.

If you could not fence `us-east-2` during the outage, fence it **the moment the region answers again**, before anything
else, with `bash scripts/dr/10-simulate-outage.sh` (the same mechanism; the name says "simulate", the effect is the fence).

### U3. Switch Route 53

```bash
bash scripts/dr/30-switch-traffic.sh
```

The script checks that the `us-west-2` cluster is the writer, notices that the `us-east-2` cluster is not a reader of the
global cluster (it is detached or unreachable), verifies that its functions are fenced, fences them now if not, and asks
for `proceed-unfenced us-east-2` only if that is impossible. Then it updates the dns stack in `us-east-1`, which works
while `us-east-2` is down. Expected: weights 0 / 255 and `done. New requests reach us-west-2 within about 60 s ...`.

### U4. Validate

```bash
bash scripts/dr/40-validate.sh --expect-region us-west-2 --wait 180 --probe
```

Plus the dashboard and Postman folder `04 DR drill`, as in step 4. Expected `validation passed`, `database_role` `writer`.

### U5. Measure the data gap

The surviving database cannot tell you by itself what it never received. Compare it with an independent record:

1. **Against the baseline.** In the promoted region, read `/stats` right after the failover (`40-validate.sh` prints it) and
   compare `counts_by_region` and `last_processed_at_by_region` for `us-east-2` with the last baseline of
   `00-precheck.sh` and with what the dashboard showed before the outage. The difference in count, plus the time between
   `last_processed_at_by_region["us-east-2"]` and the time of the outage, is the visible gap. Do this before new writes
   confuse the comparison (writes of the new region count under `us-west-2`, so the `us-east-2` figure stays comparable).
2. **Against the clients.** Any client that holds an `id` from a `201` answer can check it with `GET /transactions?limit=100`; the banking
   app does this for the operations of its session with **Verify my operations** (it lists the acknowledged ids missing from the ledger).
   Clients that did not get an answer, or whose answer is missing from the surviving data, can resubmit with the **same
   `Idempotency-Key`**: if the row was replicated they get `200` with the original record and nothing is duplicated; if
   it was not, it is created in the new primary (with the new region and time).
3. **Against the old cluster.** When `us-east-2` returns, take a snapshot of the old standalone cluster before touching
   it (`aws rds create-db-cluster-snapshot --region us-east-2 --db-cluster-identifier rdsc-useast2-drdemo-prod
   --db-cluster-snapshot-identifier drdemo-before-rebuild`), restore it to a temporary cluster, and compare transaction ids
   with the new primary. Missing rows can be re-inserted through the API. This is manual work and was never run.
4. **Bound.** `AuroraGlobalDBReplicationLag` (and `AuroraGlobalDBRPOLag`) before the outage bounds the time window of lost writes.
   `scripts/lib/dr.sh` states that `AuroraGlobalDBRPOLag` is only filled when `rds.global_db_rpo` is configured, and the templates of
   this repository do not set it. Even so, the drill of 2026-11-11 read 14 datapoints of that metric (maximum 1531 ms). Why it is
   populated was not verified from the repository: check the metric in CloudWatch before relying on it, and treat
   `AuroraGlobalDBReplicationLag` as the primary bound.

Write the result down. State it as "about N transactions were not replicated", not as "zero loss".

### U6. Keep us-east-2 fenced

Until the old cluster is deleted or rebuilt as a secondary, its functions stay at reserved concurrency 0. Verify with
`bash scripts/dr/00-precheck.sh` (step 4 lists the reserved concurrency of both regions). If the region was unreachable
when you promoted, run `bash scripts/dr/10-simulate-outage.sh` as soon as it answers.

### Why the master secret is lost with the region

The Aurora master password is a Secrets Manager secret **in the primary region only** (`us-east-2`, `sm-useast2-drdemo-master-prod-<suffix>`,
generated by the database stack because RDS cannot manage the master password of a global database). A secondary cluster has no secret of its own. When `us-east-2` is lost, the secret is lost or
unreachable with it, and the promoted cluster in `us-west-2` has no master secret in its own region. Consequences:

- The application is **not** affected: the functions use IAM database authentication (a token signed by the function role).
  No password is involved.
- The only users of the master credentials are the one-time schema bootstrap (already done and replicated) and manual
  administration. `scripts/deploy.sh` skips the schema step when the roles are swapped.
- If you need master access in `us-west-2`, set a new master password on the promoted cluster (for example
  `aws rds modify-db-cluster --region us-west-2 --db-cluster-identifier rdsc-uswest2-drdemo-prod --master-user-password
  '<new password>' --apply-immediately`). Whether RDS accepts this on a former secondary was not verified, and RDS-managed master
  secrets are not available for global databases; check the RDS documentation first.
  Do not put the password in the repository.

### Alternative: detach instead of failover

If `failover-global-cluster` cannot be used, the secondary can be detached and becomes a standalone, writable cluster:
`aws rds remove-from-global-cluster --region us-west-2 --global-cluster-identifier <global cluster id> --db-cluster-identifier
<us-west-2 cluster arn>`. It is also irreversible, loses the same writes, and leaves a global cluster that still lists the dead
primary; `scripts/dr/*` expect membership in the global cluster, so most scripts will stop with a message. This path is
not scripted and was never run. Prefer U2.

### U7. Manual rebuild of a secondary in us-east-2 (for failback)

The scripts cannot fail back to a cluster that is not a member of the global cluster (`20-promote.sh` and
`50-failback-promote.sh` stop with a message). To return to `us-east-2` you must first create a new secondary there that
replicates from `us-west-2`. **This procedure is manual, was written from the AWS CLI documentation and was never run.** It
creates a billable cluster (about USD 6.40 per day, unverified estimate). Do it with owner credentials, in `us-east-2`,
after the region is healthy and while its functions are still fenced.

1. **Keep evidence** (section U5, item 3): snapshot the old standalone cluster and reconcile data first.
2. **Free the identifiers.** The scripts and the destroy procedure look for `rdsc-useast2-drdemo-prod` and
   `rds-useast2-drdemo-prod`, so the rebuilt cluster must reuse those names. This is destructive; confirm you have the
   snapshot first.

   ```bash
   aws rds modify-db-cluster --region us-east-2 --db-cluster-identifier rdsc-useast2-drdemo-prod \
     --no-deletion-protection --apply-immediately
   aws rds delete-db-instance --region us-east-2 --db-instance-identifier rds-useast2-drdemo-prod
   aws rds delete-db-cluster --region us-east-2 --db-cluster-identifier rdsc-useast2-drdemo-prod --skip-final-snapshot
   ```

   (Wait for both deletions to finish. If the region lost its data, skip this step.)
3. **Create the new cluster as a member of the same global cluster.** Reuse the objects the database stack still owns in
   `us-east-2` (KMS key, subnet group `dbsg-useast2-drdemo-prod`, security group `sgp-useast2-drdemo-db-prod`, parameter group). Read the values first:

   ```bash
   aws cloudformation describe-stacks --region us-east-2 --stack-name stk-useast2-drdemo-database-prod \
     --query "Stacks[0].Outputs[?OutputKey=='KmsKeyArn'].OutputValue" --output text
   aws cloudformation describe-stack-resource --region us-east-2 --stack-name stk-useast2-drdemo-database-prod \
     --logical-resource-id DbSecurityGroup --query StackResourceDetail.PhysicalResourceId --output text
   ```

   Then create the cluster and its instance (the engine version must match the one in use; `16.15` is the default; tags are
   the five mandatory ones; no master user or password, because a secondary inherits them):

   ```bash
   aws rds create-db-cluster --region us-east-2 --db-cluster-identifier rdsc-useast2-drdemo-prod \
     --engine aurora-postgresql --engine-version 16.15 \
     --global-cluster-identifier rdsg-useast2-drdemo-prod --source-region us-west-2 \
     --storage-encrypted --kms-key-id <KmsKeyArn of the us-east-2 database stack> \
     --db-subnet-group-name dbsg-useast2-drdemo-prod --vpc-security-group-ids <DbSecurityGroup id> \
     --db-cluster-parameter-group-name dbpg-useast2-drdemo-cluster-prod \
     --enable-iam-database-authentication --backup-retention-period 1 --copy-tags-to-snapshot \
     --enable-cloudwatch-logs-exports postgresql --deletion-protection \
     --tags Key=team-owner,Value=nube-segura Key=project-name,Value=aws-multi-region-aurora-failover-active-passive Key=app-name,Value=dr-demo \
            Key=repo-name,Value=aws-multi-region-aurora-failover-active-passive Key=env-type,Value=prod
   aws rds create-db-instance --region us-east-2 --db-instance-identifier rds-useast2-drdemo-prod \
     --db-cluster-identifier rdsc-useast2-drdemo-prod --db-instance-class db.r6g.large --engine aurora-postgresql \
     --db-subnet-group-name dbsg-useast2-drdemo-prod --no-publicly-accessible --no-enable-performance-insights \
     --tags Key=team-owner,Value=nube-segura Key=project-name,Value=aws-multi-region-aurora-failover-active-passive Key=app-name,Value=dr-demo \
            Key=repo-name,Value=aws-multi-region-aurora-failover-active-passive Key=env-type,Value=prod
   ```

   If the global cluster identifier is not `rdsg-useast2-drdemo-prod`, use the one printed by U2 (`aws rds
   describe-global-clusters --region us-west-2`).
4. **Wait for the initial copy.** `aws rds describe-global-clusters --region us-west-2 --global-cluster-identifier
   <global cluster id> --query 'GlobalClusters[0].GlobalClusterMembers'` must list both clusters, the new one with
   `IsWriter` false. `bash scripts/dr/00-precheck.sh` should then show `us-east-2 ... reader` and a replication lag
   below 5000 ms. The time the copy takes depends on the amount of data; it was not measured.
5. **Point the us-east-2 functions at the new cluster.** IAM database authentication is granted per **cluster resource id**,
   and the new cluster has a new one, so the `rds-db:connect` permission of the six function roles no longer matches.
   Read the new id and update the api stack of `us-east-2` keeping every other parameter:

   ```bash
   aws rds describe-db-clusters --region us-east-2 --db-cluster-identifier rdsc-useast2-drdemo-prod \
     --query 'DBClusters[0].[DbClusterResourceId,Endpoint]' --output text
   aws cloudformation update-stack --region us-east-2 --stack-name stk-useast2-drdemo-api-prod --use-previous-template \
     --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
     --parameters ParameterKey=DbClusterResourceId,ParameterValue=<new resource id> \
       ParameterKey=EnvType,UsePreviousValue=true ParameterKey=ApiDomainName,UsePreviousValue=true \
       ParameterKey=HostedZoneId,UsePreviousValue=true ParameterKey=CognitoUserPoolArn,UsePreviousValue=true \
       ParameterKey=CognitoBankClientId,UsePreviousValue=true ParameterKey=CognitoDashboardClientId,UsePreviousValue=true \
       ParameterKey=AllowedOrigins,UsePreviousValue=true ParameterKey=DbEndpoint,UsePreviousValue=true \
       ParameterKey=DbName,UsePreviousValue=true ParameterKey=DbWriterUser,UsePreviousValue=true \
       ParameterKey=DbReaderUser,UsePreviousValue=true ParameterKey=PrivateSubnetIds,UsePreviousValue=true \
       ParameterKey=LambdaSecurityGroupId,UsePreviousValue=true ParameterKey=KmsKeyArn,UsePreviousValue=true \
       ParameterKey=AlertTopicArn,UsePreviousValue=true ParameterKey=LogLevel,UsePreviousValue=true \
       ParameterKey=LogRetentionDays,UsePreviousValue=true ParameterKey=ThrottleRate,UsePreviousValue=true \
       ParameterKey=ThrottleBurst,UsePreviousValue=true \
       ParameterKey=PermissionsBoundaryArn,UsePreviousValue=true
   ```

   The printed endpoint should equal the stack's `DbEndpoint` (it is derived from the cluster identifier); if it differs, pass
   the new endpoint as well. Do not use `scripts/deploy.sh --steps api` for this: it reads the (stale) outputs of the
   database stack. Check afterwards with `bash scripts/dr/00-precheck.sh` that the functions are still fenced (step 4).
6. **Drift.** The `us-east-2` database stack no longer matches reality and must never be updated again. Because the new
   cluster reuses the same identifiers, `scripts/destroy.sh` finds it (step 1 switches its deletion protection off, step 4
   detaches it) and CloudFormation will probably delete it with the stack; whether it does was not verified, so check the
   leftovers list of the destroy ([cost-and-cleanup.md](cost-and-cleanup.md)).

### U8. Fail back

When the new secondary is `available` and in sync (lag low), the normal failback applies:

```bash
bash scripts/dr/50-failback-promote.sh
bash scripts/dr/60-restore.sh
bash scripts/dr/40-validate.sh --expect-region us-east-2 --wait 180 --probe
```

Same expected results, verification and undo as steps 5a to 5c. `60-restore.sh` lifts the fence only because the cluster
is a member of the global cluster again; otherwise it demands the typed phrase `unfence-standalone us-east-2`.
You can also **not** fail back: `us-west-2` can stay the writer for the rest of the demo (the weights are then 0 / 255),
and you destroy everything at the end.

## Known limitations

- `20-promote.sh --unplanned` (and the unplanned mode of `50-failback-promote.sh`) needs an interactive terminal for the
  typed phrase `allow-data-loss <region>`; it cannot be pre-supplied.
- `10-simulate-outage.sh` is the simulation of the drill **and** the fence used after an unplanned failover (same
  mechanism, reserved concurrency 0). The name says "simulate"; in U2 and U6 its effect is the fence.
- From a workstation, updating the dns stack (`30-switch-traffic.sh`, `60-restore.sh`) needs your owner credentials. The `failover`
  workflow does it with the organization role. `CFN_EXEC_ROLE_ARN` only chooses the role CloudFormation uses for the change.
- The workflows run a planned switchover only. A real outage (unplanned) is always a manual step from this runbook.
- The shared concurrency group `drdemo-prod-infrastructure` can silently replace a queued run with a newer pending one.
- Replaying an `Idempotency-Key` with a different payload returns the original record (`200`); the API reports no conflict.
  Keep this in mind when resubmitting after a data gap (U5, item 2).
- The scripts assume the environment `prod`.
- The unplanned path, U5 and U7 were never run on AWS. The planned path was run by hand before Cognito (2026-10-14) and once with the automated runner after it (2026-11-11); see [Observed values](#observed-values).
- The `failover` workflow cannot prove the ledger by itself: it holds no Cognito token, so the probe deposit and `45-consistency.sh` are
  skipped there. Run `45-consistency.sh` with `COGNITO_ID_TOKEN` after each move.
- An ID token lives 15 minutes and a refresh token 24 hours; the sites refresh and re-authenticate against the serving region
  by themselves, scripts and Postman do not.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `00-precheck.sh` exits 1, lag above 5000 ms | Replication is behind | Wait and repeat; do not run a planned switchover until it drops |
| `20-promote.sh`: `lag ... is above ... ms` | Same | Wait, or `--force` (the switchover itself still waits for the secondary to catch up) |
| `20-promote.sh`: `switchover request failed (nothing else was changed)` | RDS API of the old writer region unreachable or permission missing | Retry with `--api-region us-west-2` |
| `20-promote.sh`: `the cluster in us-west-2 is not a member of ...` | An earlier unplanned failover detached it, or the global cluster is gone | Follow U7 |
| `30-switch-traffic.sh`: `... is reader, not the writer. Promote it first` | Traffic must follow the promotion | Run step 2 (or 5a) first |
| `40-validate.sh`: `expected region ... was not reached` | DNS and keep-alive caching | Wait, reload the dashboard tab, check weights with `00-precheck.sh` |
| `503 database_read_only` after the switch | DNS moved before the database was promoted, or the promotion is not finished | Check roles with `00-precheck.sh`; finish the promotion; `Retry-After` is 5 s |
| `503 database_unavailable` right after a promotion | Connections to the old writer are being replaced, or authentication or a statement timed out | Repeat the request after a few seconds; if it persists, check roles with `00-precheck.sh` and the function logs |
| `500 internal_error` | A database error that is neither connectivity nor role related (schema, data or a bug) | Read the function log group (`sqlstate` and `kind` fields; no bodies are logged) |
| `422 insufficient_funds` | A withdrawal above the balance | Expected; deposit first. The response carries the current balance |
| `503 database_busy` | The account row lock was not obtained within 5 s | Retry after a few seconds (the banking app retries by itself with the same key) |
| `429` | The stage throttle (20 requests per second, burst 40) was exceeded | Wait for the rate to drop; close extra dashboard tabs (each makes three requests every 5 seconds) |
| `401` (or `403`) on every protected request | Missing, expired (15 minutes) or wrong token: no `Authorization: Bearer <ID token>` header, an access token instead of the ID token, or a token of another pool or client | Sign in again in the banking app or the dashboard; for Postman and the scripts get a new ID token and set `cognito_id_token` / `COGNITO_ID_TOKEN`. `GET /health` and `OPTIONS` need no token |
| `401` from a region right after a switchover | The Cognito replica is not active in us-west-2 (script path: the activation was not run), or the token is not accepted by the regional authorizer | Check that the `failover` run activated the replica (step "Activate Cognito replica") or run the helper above; sign in again so the page re-authenticates against the serving region |
| A `failover` run never starts or is replaced | Another workflow of the shared concurrency group is running or pending | Open the run list, wait for the running job, dispatch again |
| `failover` to-secondary stops at "Primary region not fenced" | The primary functions are not at concurrency 0 | Run `simulate-outage` with `fence` first |
| `20-promote.sh`: `no replication lag datapoint ... after waiting` | The new secondary has not published the metric yet, or the metric query failed | Run `00-precheck.sh`, wait a few minutes and run the step again; do not use `--force` unless you accept the risk |
| `--undo` or `60-restore.sh` asks for `unfence-standalone ...` | The cluster is not a member of the global cluster | Do not lift the fence until U7 is done |
| Dashboard still unavailable after step 3 | Resolver or connection caching, old tab | Hard reload; wait up to a few minutes; run `40-validate.sh --wait 180` |
| `ROLLBACK_COMPLETE` or another stuck stack | A deploy failed earlier | Investigate with `aws cloudformation describe-stack-events`; the scripts never delete or retry a stuck stack |
