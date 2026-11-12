# ADR 0015: The workflows run a planned switchover only; unplanned failover stays a manual runbook step

- Status: accepted
- Date: 2026-10-14

## Context

The presenter wants to show that the service and the data continue in the other region without typing commands. Aurora Global
Database offers two operations: a planned switchover (waits for the secondary to catch up, swaps the roles, no data loss,
reversible) and an unplanned failover (`--allow-data-loss`, promotes at whatever point replication had reached and detaches
the old primary for good). The unplanned one can lose the last writes and leaves a diverging copy that must be fenced and
rebuilt.

## Decision

- The workflow `failover` (`direction` = `to-secondary` or `to-primary`, typed confirmation `failover`) performs only the planned
  switchover, in a fixed order and stopping at the first failing step:
  - `to-secondary`: pre-check; require the primary region fenced and the secondary not fenced; `20-promote.sh --yes`;
    `30-switch-traffic.sh --to secondary` (weights 0/255); `40-validate.sh --expect-region <secondary> --wait 180 --probe`;
    `45-consistency.sh` (the job ends red if the ledger is not consistent).
  - `to-primary`: unfence the primary; `50-failback-promote.sh --yes`; weights 255/0; validate; consistency check.
- The workflow `simulate-outage` fences or unfences a region (reserved concurrency 0, [ADR 0008](0008-outage-simulation-method.md));
  nothing is deleted or stopped.
- No workflow passes `--force`, `--unplanned` or `FORCE_UNFENCE` (a CI grep fails if one does). The lag gate of the switchover
  waits up to 5 minutes for the first `AuroraGlobalDBReplicationLag` datapoint of a new secondary (CloudWatch publishes it 2 to
  3 minutes after a switchover) and then fails closed: no datapoint or a failed query stops the step before anything changes.
- `scripts/dr/20-promote.sh --unplanned` still exists for a real outage. It asks for a typed phrase on an interactive terminal,
  so it cannot run from a non-interactive job. The procedure, the data-gap measurement and the rebuild of a secondary are in the
  runbook (section "Unplanned path").

## Consequences

- RPO of the workflow path is zero and no acknowledged movement is lost; the consistency check proves it after each move.
- Observed on 2026-10-14 (one run each, owner-reported): to-secondary 195 s in total (switchover 36 s, DNS change about 70 s),
  to-primary 170 s (switchover 35 s); writes unavailable about 35 to 40 s per switchover; ledger consistent after both.
- The real-outage path is never exercised by the workflows. It was written from the AWS documentation and not run.
- A pending `deploy` can replace a queued `failover` in the shared concurrency group ([ADR 0006](0006-manual-deploy-only.md)).

## Alternatives considered

Both modes in the workflow behind an input (a data-loss decision inside a button; the typed phrase could not be kept interactive);
automatic failover from Route 53 health checks ([ADR 0003](0003-manual-weighted-routing.md)); simulating a real outage by
deleting or stopping resources (irreversible or costly).
