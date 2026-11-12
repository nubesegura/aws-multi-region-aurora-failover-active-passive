# DR drill run report - 2026-11-11T12-00-00Z

**Verdict: PASS**

## Environment

- Run: `2026-11-11T12-00-00Z`
- Primary region: us-east-2; secondary region: us-west-2
- First event: 2026-11-11T12:00:00.000Z; last event: 2026-11-11T12:03:02.000Z
- Probe attempts recorded: account 18, auth 10, health 18, write 91

## Phases

| Phase | Start (UTC) | End (UTC) | Duration |
|---|---|---|---|
| baseline | 2026-11-11T12:00:00.000Z | 2026-11-11T12:00:20.000Z | 20.0 s |
| fence | 2026-11-11T12:00:20.000Z | 2026-11-11T12:00:42.000Z | 22.0 s |
| to-secondary | 2026-11-11T12:00:42.000Z | 2026-11-11T12:01:42.000Z | 60.0 s |
| steady-secondary | 2026-11-11T12:01:42.000Z | 2026-11-11T12:02:02.000Z | 20.0 s |
| to-primary | 2026-11-11T12:02:02.000Z | 2026-11-11T12:02:41.000Z | 39.0 s |
| steady-primary | 2026-11-11T12:02:41.000Z | 2026-11-11T12:03:01.000Z | 20.0 s |
| consistency | 2026-11-11T12:03:01.000Z | 2026-11-11T12:03:02.000Z | 1.0 s |

## Metrics

Percentiles are nearest-rank. A metric without data is reported as not measured, never as zero.

| Metric | Value | Definition | Source | Resolution |
|---|---|---|---|---|
| to-secondary: write RTO | 60.00 s | Last 2xx write answered by us-east-2 before the first failed or retried write, to the first 2xx write answered by us-west-2. | write probe, one attempt per second (timeline `request` events) | about 1 s (write probe cadence) |
| to-secondary: read RTO | 70.00 s | Same definition with GET /account: last 2xx answered by us-east-2 before the first failed read, to the first 2xx answered by us-west-2. | read probe, GET /account every 5 s (timeline `request` events) | about 5 s (read probe cadence) |
| to-secondary: write unavailability | 58.05 s | First failed write to the first 2xx write answered by us-west-2. | write probe, one attempt per second (timeline `request` events) | about 1 s (write probe cadence) |
| to-secondary: secondary sign-in availability | 66.7 % | Share of successful secondary-region sign-ins from the start of the outage to the end of the steady phase (6 attempts, 2 failed). | auth probe, one sign-in every 10 s | 10 s (auth probe cadence) |
| to-secondary: longest sign-in failure run | 2 sign-ins, 20.04 s | Longest run of consecutive failed sign-ins and its duration. | auth probe, one sign-in every 10 s | 10 s (auth probe cadence) |
| to-secondary: DNS convergence | 1.00 s | End of the traffic-switch step to the first changed DNS answer (negative when the answer changed before the step ended). | DNS watcher (`dns` events) and the step times from GitHub | 2 s (DNS poll), one resolver |
| to-secondary: decision time | 3.00 s | End of the fence run to the dispatch of the failover. | GitHub run end time and the dispatch time | 1 s (GitHub timestamps), workstation clock skew not corrected |
| to-primary: write RTO | 10.00 s | Last 2xx write answered by us-west-2 before the first failed or retried write, to the first 2xx write answered by us-east-2. | write probe, one attempt per second (timeline `request` events) | about 1 s (write probe cadence) |
| to-primary: read RTO | not measured: no failed or retried request was seen in the window (no outage observed) | Same definition with GET /account: last 2xx answered by us-west-2 before the first failed read, to the first 2xx answered by us-east-2. | read probe, GET /account every 5 s (timeline `request` events) | about 5 s (read probe cadence) |
| to-primary: write unavailability | 8.05 s | First failed write to the first 2xx write answered by us-east-2. | write probe, one attempt per second (timeline `request` events) | about 1 s (write probe cadence) |
| to-primary: secondary sign-in availability | 100.0 % | Share of successful secondary-region sign-ins from the start of the outage to the end of the steady phase (3 attempts, 0 failed). | auth probe, one sign-in every 10 s | 10 s (auth probe cadence) |
| to-primary: longest sign-in failure run | 0 sign-ins, 0.00 s | Longest run of consecutive failed sign-ins and its duration. | auth probe, one sign-in every 10 s | 10 s (auth probe cadence) |
| to-primary: DNS convergence | 1.00 s | End of the traffic-switch step to the first changed DNS answer (negative when the answer changed before the step ended). | DNS watcher (`dns` events) and the step times from GitHub | 2 s (DNS poll), one resolver |
| to-primary: decision time | not measured: not applicable: the return is dispatched after a fixed steady phase | End of the fence run to the dispatch of the failover. | GitHub run end time and the dispatch time | 1 s (GitHub timestamps), workstation clock skew not corrected |
| simulate-outage (fence): queue time | 3.50 s | Dispatch to the start of the run. | GitHub run times and the dispatch time | 1 s (GitHub timestamps), workstation clock skew not corrected |
| simulate-outage (fence): run time | 16.00 s | Start of the run to its last update. | GitHub run times and the dispatch time | 1 s (GitHub timestamps), workstation clock skew not corrected |
| simulate-outage (fence): total | 19.50 s | Dispatch to the end of the run. | GitHub run times and the dispatch time | 1 s (GitHub timestamps), workstation clock skew not corrected |
| failover (to-secondary): queue time | 4.00 s | Dispatch to the start of the run. | GitHub run times and the dispatch time | 1 s (GitHub timestamps), workstation clock skew not corrected |
| failover (to-secondary): run time | 54.00 s | Start of the run to its last update. | GitHub run times and the dispatch time | 1 s (GitHub timestamps), workstation clock skew not corrected |
| failover (to-secondary): total | 58.00 s | Dispatch to the end of the run. | GitHub run times and the dispatch time | 1 s (GitHub timestamps), workstation clock skew not corrected |
| failover (to-primary): queue time | 4.00 s | Dispatch to the start of the run. | GitHub run times and the dispatch time | 1 s (GitHub timestamps), workstation clock skew not corrected |
| failover (to-primary): run time | 33.00 s | Start of the run to its last update. | GitHub run times and the dispatch time | 1 s (GitHub timestamps), workstation clock skew not corrected |
| failover (to-primary): total | 37.00 s | Dispatch to the end of the run. | GitHub run times and the dispatch time | 1 s (GitHub timestamps), workstation clock skew not corrected |
| AuroraGlobalDBReplicationLag: maximum | 1300.0 ms | maximum of the 4 samples taken during the run. | CloudWatch AuroraGlobalDBReplicationLag, maximum per period | 60 s period (milliseconds) |
| AuroraGlobalDBReplicationLag: 95th percentile | 1300.0 ms | 95th percentile of the 4 samples taken during the run. | CloudWatch AuroraGlobalDBReplicationLag, maximum per period | 60 s period (milliseconds) |
| AuroraGlobalDBReplicationLag: mean | 1042.5 ms | mean of the 4 samples taken during the run. | CloudWatch AuroraGlobalDBReplicationLag, maximum per period | 60 s period (milliseconds) |

## Breakdown per direction

### to-secondary

| Category | Duration |
|---|---|
| queue | 7.50 s |
| setup | 19.00 s |
| pre_checks | 8.00 s |
| fence | 11.00 s |
| cognito_replica | 5.00 s |
| switchover | 12.00 s |
| traffic_switch | 3.00 s |
| validation | 6.00 s |
| consistency | 3.00 s |
| other | not measured: no step of this category ran with recorded times |

| Phase | Workflow step | Category | Duration |
|---|---|---|---|
| fence | Validate the branch, the inputs, the variables and the secrets | setup | 1.0 s |
| fence | Check out the code | setup | 1.0 s |
| fence | AWS login | setup | 2.0 s |
| fence | Run 10-simulate-outage.sh | fence | 8.0 s |
| fence | Summary | setup | 1.0 s |
| to-secondary | Validate the branch, the inputs, the variables and the secrets | setup | 1.0 s |
| to-secondary | Check out the code | setup | 1.0 s |
| to-secondary | Prepare the step timer and the helper functions | setup | 1.0 s |
| to-secondary | AWS login (group 1) | setup | 2.0 s |
| to-secondary | Verify the AWS account (group 1) | setup | 1.0 s |
| to-secondary | Record the writer region before | setup | 1.0 s |
| to-secondary | Group 1 - pre-checks (read only) | pre_checks | 4.0 s |
| to-secondary | Group 1 - confirm that the primary region is fenced | pre_checks | 2.0 s |
| to-secondary | Group 1 - confirm that the secondary region is NOT fenced | pre_checks | 2.0 s |
| to-secondary | Group 1 - unfence the primary region | fence | 3.0 s |
| to-secondary | AWS login (group 2) | setup | 2.0 s |
| to-secondary | Verify the AWS account (group 2) | setup | 1.0 s |
| to-secondary | Group 2 - activate the Cognito replica in the secondary region | cognito_replica | 5.0 s |
| to-secondary | Group 2 - planned switchover to the secondary region | switchover | 12.0 s |
| to-secondary | AWS login (group 3) | setup | 2.0 s |
| to-secondary | Verify the AWS account (group 3) | setup | 1.0 s |
| to-secondary | Group 3 - switch the Route 53 weights | traffic_switch | 3.0 s |
| to-secondary | Group 3 - validate the service on the custom domain | validation | 6.0 s |
| to-secondary | Group 3 - consistency check of the ledger | consistency | 3.0 s |
| to-secondary | Summary | setup | 1.0 s |

### to-primary

| Category | Duration |
|---|---|
| queue | 4.00 s |
| setup | 12.00 s |
| pre_checks | 4.00 s |
| fence | not measured: no step of this category ran with recorded times |
| cognito_replica | 2.00 s |
| switchover | 7.00 s |
| traffic_switch | 2.00 s |
| validation | 4.00 s |
| consistency | 1.00 s |
| other | 1.00 s |

| Phase | Workflow step | Category | Duration |
|---|---|---|---|
| to-primary | Validate the branch, the inputs, the variables and the secrets | setup | 1.0 s |
| to-primary | Check out the code | setup | 1.0 s |
| to-primary | Prepare the step timer and the helper functions | setup | 1.0 s |
| to-primary | AWS login (group 1) | setup | 2.0 s |
| to-primary | Verify the AWS account (group 1) | setup | 1.0 s |
| to-primary | Record the writer region before | setup | 1.0 s |
| to-primary | Group 1 - pre-checks (read only) | pre_checks | 4.0 s |
| to-primary | AWS login (group 2) | setup | 2.0 s |
| to-primary | Verify the AWS account (group 2) | setup | 1.0 s |
| to-primary | Group 2 - planned switchover back to the primary region | switchover | 7.0 s |
| to-primary | Group 2 - deactivate the Cognito replica in the secondary region | cognito_replica | 2.0 s |
| to-primary | AWS login (group 3) | setup | 1.0 s |
| to-primary | Verify the AWS account (group 3) | setup | 1.0 s |
| to-primary | Group 3 - switch the Route 53 weights | traffic_switch | 2.0 s |
| to-primary | Group 3 - validate the service on the custom domain | validation | 4.0 s |
| to-primary | Group 3 - consistency check of the ledger | consistency | 1.0 s |
| to-primary | Upload diagnostic bundle | other | 1.0 s |

Steps not recognised (counted under `other`): Upload diagnostic bundle

## Measured RPO and ledger

Measured RPO: 0 acknowledged writes lost (range 0 to 0).

| Ledger | Value |
|---|---|
| Baseline movements | 10 |
| Expected movements (baseline + acknowledged + proven deposits) | 69 |
| Stored movements | 69 |
| Uncertainty band | 0 |
| Unexpected extra movements | 0 |
| Expected balance range | 100.01 to 100.01 |
| Stored balance | 100.01 |
| Balance within the range | True |

### Consistency

| Check | Value |
|---|---|
| Consistent | True |
| Truncated | False |
| Replayed balance | 100.01 |
| Stored balance | 100.01 |
| Duplicate keys | 0 |
| Balance chain intact | True |
| Balances match | True |
| Movements processed in us-east-2 | 49 |
| Movements processed in us-west-2 | 20 |

Replication lag bound: maximum 1300.0 ms, 95th percentile 1300.0 ms, mean 1042.5 ms over 4 samples (60 s period; samples of both regions together).

The RPO of an unplanned failover would be at most the replication lag at the moment of the loss. This drill performs planned switchovers and does not exercise an unplanned failover, so these figures bound what was observed during the run and are not a measured unplanned RPO.

### Band and failover probe deposits

| Failover | Deposit outcome | Effect on the ledger |
|---|---|---|
| to-secondary | deposited | posted: +1 certain in the expected movement count |
| to-primary | skipped | not posted: adds nothing |

Uncertainty band: 0 = 0 abandoned or pending probe movements + 0 uncertain failover deposits. The stored count may legitimately be between the expected count and the expected count plus 0.

## Errors, retries and latency

A failed attempt has no answering region and is listed under `unknown`. Latency is computed over successful attempts only.

| Probe | Phase | Region | Attempts | Errors | Error rate | Retries | Latency median | Latency p95 | Latency max |
|---|---|---|---|---|---|---|---|---|---|
| account | baseline | us-east-2 | 2 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| account | fence | unknown | 1 | 1 | 100.0 % | 0 | not measured: no successful request in this group | not measured: no successful request in this group | not measured: no successful request in this group |
| account | fence | us-east-2 | 1 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| account | to-secondary | unknown | 5 | 5 | 100.0 % | 0 | not measured: no successful request in this group | not measured: no successful request in this group | not measured: no successful request in this group |
| account | to-secondary | us-west-2 | 1 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| account | steady-secondary | us-west-2 | 2 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| account | to-primary | us-east-2 | 1 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| account | to-primary | us-west-2 | 3 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| account | steady-primary | us-east-2 | 2 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| health | baseline | us-east-2 | 2 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| health | fence | unknown | 1 | 1 | 100.0 % | 0 | not measured: no successful request in this group | not measured: no successful request in this group | not measured: no successful request in this group |
| health | fence | us-east-2 | 1 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| health | to-secondary | unknown | 5 | 5 | 100.0 % | 0 | not measured: no successful request in this group | not measured: no successful request in this group | not measured: no successful request in this group |
| health | to-secondary | us-west-2 | 1 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| health | steady-secondary | us-west-2 | 2 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| health | to-primary | unknown | 1 | 1 | 100.0 % | 0 | not measured: no successful request in this group | not measured: no successful request in this group | not measured: no successful request in this group |
| health | to-primary | us-east-2 | 1 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| health | to-primary | us-west-2 | 2 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| health | steady-primary | us-east-2 | 2 | 0 | 0.0 % | 0 | 40.0 ms | 40.0 ms | 40.0 ms |
| write | baseline | us-east-2 | 10 | 0 | 0.0 % | 0 | 45.0 ms | 55.0 ms | 55.0 ms |
| write | fence | unknown | 4 | 4 | 100.0 % | 3 | not measured: no successful request in this group | not measured: no successful request in this group | not measured: no successful request in this group |
| write | fence | us-east-2 | 7 | 0 | 0.0 % | 0 | 45.0 ms | 55.0 ms | 55.0 ms |
| write | to-secondary | unknown | 25 | 25 | 100.0 % | 25 | not measured: no successful request in this group | not measured: no successful request in this group | not measured: no successful request in this group |
| write | to-secondary | us-west-2 | 5 | 0 | 0.0 % | 1 | 45.0 ms | 55.0 ms | 55.0 ms |
| write | steady-secondary | us-west-2 | 10 | 0 | 0.0 % | 0 | 45.0 ms | 55.0 ms | 55.0 ms |
| write | to-primary | unknown | 4 | 4 | 100.0 % | 3 | not measured: no successful request in this group | not measured: no successful request in this group | not measured: no successful request in this group |
| write | to-primary | us-east-2 | 4 | 0 | 0.0 % | 1 | 40.0 ms | 55.0 ms | 55.0 ms |
| write | to-primary | us-west-2 | 12 | 0 | 0.0 % | 0 | 45.0 ms | 55.0 ms | 55.0 ms |
| write | steady-primary | us-east-2 | 10 | 0 | 0.0 % | 0 | 45.0 ms | 55.0 ms | 55.0 ms |

## What was not measured

- RPO of an unplanned failover: the drill performs planned switchovers only.
- Detection time: how long a real incident takes to be noticed; the outage is simulated and starts when the fence run is dispatched.
- DNS as seen by other resolvers, browsers and caches: only one resolver is polled, every 2 s.
- Client-side behaviour beyond the probe: browsers, retries of real users and TTL caches.
- Behaviour under production load: the probe offers about one write per second.
- In this run, to-primary read RTO: no failed or retried request was seen in the window (no outage observed)

## Limitations

- One run from one workstation and one network path: the numbers are a sample, not a distribution.
- Probe cadence bounds the resolution: writes about 1 s, reads and health 5 s, sign-in 10 s, DNS 2 s, replication lag 60 s.
- GitHub timestamps have a 1 s resolution and come from another clock than the workstation's.
- The RPO is verified from the ledger of net-zero probe movements: movements that were not acknowledged may or may not have committed (uncertainty band), and a band greater than 0 can hide a loss.
- The failover probe deposit is classified from the step log; when the log cannot be read it stays uncertain.
- Replication lag is a CloudWatch maximum per 60 s period: it bounds the lag, it does not measure the RPO of this planned switchover.
