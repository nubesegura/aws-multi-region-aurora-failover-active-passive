# ADR 0003: Manual Route 53 weighted routing instead of failover routing with health checks

- Status: accepted
- Date: 2026-10-04

## Context

Clients must use one domain that survives the loss of a region. Route 53 can switch automatically with failover routing
and health checks, or a person can change weighted records.

## Decision

Two weighted alias A records for the API domain (`SetIdentifier` `use2` and `usw2`), weights `255 / 0` in normal
operation and `0 / 255` after the promotion, `EvaluateTargetHealth` false, no health checks. The weights are changed by
updating the `dns` stack in `us-east-1` (`scripts/dr/30-switch-traffic.sh`). Both weights at 0 are rejected by a stack rule.

## Why not automatic failover

- The secondary region's database is read-only until it is promoted. Failover routing would send writes there as soon as a
  health check failed, and every write would answer `503 database_read_only`. **The traffic switch must follow the
  database promotion**, never precede it.
- An unplanned promotion can lose writes and detaches the old primary; that is a decision for a person, not for a health check.
- After an unplanned failover the old primary can still accept writes. The old region must be fenced before DNS moves.
- A false-positive health check would move clients to a read-only region; health checks also cost money.
- Weights are trivially reversible, and the update runs in `us-east-1`, so it works while `us-east-2` is down.

## Consequences

- RTO depends on a human running the steps (target 15 minutes, not measured) and on caching: alias answers carry a 60
  second TTL, but resolvers and keep-alive connections can keep the old answer longer.
- Automating it would need a workflow that fences, promotes, waits for the writer and only then changes the weights; out of scope.
- The Route 53 control plane in `us-east-1` is a dependency; a `us-east-1` outage is out of scope.
- The `dns` stack (the records) and the bootstrap stack live in `us-east-1` so that losing `us-east-2` does not remove the tools
  needed to fail over. The CloudFront sites and their buckets moved to `us-east-2` with the 2026-11-10 realignment
  ([ADR 0018](0018-region-realignment.md)); the weight switch does not depend on them.

## Alternatives considered

Failover routing with health checks (rejected above); latency or geolocation routing (does not express active-passive);
a single regional endpoint with a Global Accelerator (extra cost and moving part).
