# ADR 0005: Database stacks are not redeployed during or after a drill (accepted drift)

- Status: accepted
- Date: 2026-10-04

## Context

A planned switchover or an unplanned failover changes which Aurora cluster is the writer. RDS does this, not
CloudFormation. The database stacks describe the roles **at creation**: the `us-east-2` stack owns the global cluster
and a writer, the `us-west-2` stack owns a secondary. After a drill the real roles differ from what the stacks and their
outputs say, and redeploying a stack could try to change or replace live clusters.

## Decision

- The `Role` parameter means the role at creation. Conditions on it only gate creation-time resources (the global cluster,
  the Data API option and the master secret exist only in the `us-east-2` stack).
- Nothing in the templates manages anything that changes at runtime: global membership, the writer flag, reserved
  concurrency used for fencing, or the Route 53 weights outside the `dns` stack. The global cluster has
  `UpdateReplacePolicy: Retain` and must not receive property changes by stack update.
- Names never contain "primary", "secondary", "writer" or "reader", only the region.
- `scripts/deploy.sh` reads the live global cluster first. If the cluster of the primary region is not the writer, it
  **skips** the `db-primary`, `db-secondary` and `schema` steps with a warning. Everything else can still be redeployed.
- The drill and destroy scripts read the live roles from `describe-global-clusters` and never assume.
- The replication lag alarm exists in both database stacks, so it follows whichever cluster is the secondary.
- The functions use the cluster endpoint, which is a reader before a promotion and a writer after it, so no application
  stack needs a redeploy either.

## Consequences

- CloudFormation drift is expected and harmless: a drift check or the stack outputs show the pre-swap roles.
- Do not update the database stacks after a swap. If a database setting must change, fail back first, or change it by hand.
- After an unplanned failover and the manual rebuild of a secondary ([runbook U7](../dr-runbook.md)), the `us-east-2`
  stack no longer matches reality at all and must never be updated again; only the destroy procedure touches it.
- That deleting such a stack works cleanly was not verified.

## Alternatives considered

Updating the stacks after each swap to keep them in sync (risky: could replace live clusters); modelling roles as
parameters that are flipped by a stack update (CloudFormation would try to recreate resources); custom resources that
call the failover API (hides a data-loss decision inside a deployment).
