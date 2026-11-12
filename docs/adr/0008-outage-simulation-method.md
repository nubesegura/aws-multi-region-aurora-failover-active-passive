# ADR 0008: Simulate the outage with reserved concurrency 0

- Status: accepted
- Date: 2026-10-04

## Context

The drill needs to look like the loss of `us-east-2` to clients without harming anything that cannot be undone.

## Decision

`scripts/dr/10-simulate-outage.sh` sets **reserved concurrency to 0** on the six functions of the region (`create-transaction`, `list-transactions`, `get-stats`,
`get-health`, `get-account`, `get-consistency`). Before it changes anything it checks that all six exist, so a fence is never partial.
`--status` only reports. The workflow `simulate-outage` (inputs `action` = `status`, `fence` or `unfence`, a region and, for `fence`,
the region name typed again) runs the same script from `main` in the `prod` Environment and writes an audit summary. Lambda then
rejects every invocation, so requests that still reach that region fail. `--undo` removes the reservation. The database,
API Gateway and DNS of the region are untouched. The templates set no reserved concurrency, so a stack update never
reverts or fights the fence.

The same mechanism is the **fence** after an unplanned failover (`20-promote.sh --unplanned`, `30-switch-traffic.sh`):
it is the only protection against split brain, because the detached old primary may still accept writes. Lifting it is
refused for a cluster that is not a member of the global cluster unless a phrase is typed.

## Consequences

- It **simulates, it does not reproduce**, a regional failure. In a real outage the database, the network and the
  regional control plane (including the API that sets the fence) are also affected.
- It is reversible and needs only Lambda permissions, so it is safe to rehearse. Observed on 2026-10-14: fencing a region took 41 s end to end.
- The `failover` workflow to the secondary refuses to start unless the primary region is fenced and the secondary is not.
- It trips the throttles and 5xx alarms of the region, which is the expected signal.

## Alternatives considered

Changing security groups or network ACLs (touches stack-owned resources and drifts them); stopping or deleting the
cluster (a cluster in a global database cannot simply be stopped as far as is known; deletion is irreversible); removing the
DNS records (does not simulate a failure, only a routing change); an explicit IAM deny (needs a policy edit on a stack-owned
role); AWS Fault Injection Service (not evaluated for cost and setup effort).
