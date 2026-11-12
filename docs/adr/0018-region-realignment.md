# ADR 0018: Region realignment of the Cognito, edge, bootstrap and dns stacks

- Status: accepted (deployed on 2026-11-10 with spec 002)
- Date: 2026-11-10

## Context

Spec 001 placed the global components (edge, dns, bootstrap) in `us-east-1`. Spec 002 adds Cognito, whose primary pool should sit with the
active workloads, and the owner wanted the active region (`us-east-2`) to hold everything that serves users.

## Decision

- `cognito` primary and `edge` (both buckets and both CloudFront distributions) are in `us-east-2` (`AWS_REGION_PRIMARY`); the Cognito
  replica and its KMS replica key are in `us-west-2` (`AWS_REGION_SECONDARY`).
- The `bootstrap` stack (CloudFormation execution role, global artifact bucket) and the `dns` stack stay in `us-east-1`
  (`AWS_REGION_GLOBAL`). The `dns` stack must stay where the weight switch still works while `us-east-2` is down
  ([ADR 0003](0003-manual-weighted-routing.md)); moving the bootstrap stack is cosmetic and risky because regional stacks reference its role.
- The `edge` stack that existed in `us-east-1` was deleted and recreated in `us-east-2`; the CloudFront addresses changed.

## Consequences

- If `us-east-2` is lost, the two sites (CloudFront origins in `us-east-2`) are not available until redeployed or re-pointed; the API, the
  weights and sign-in through the replica in `us-west-2` still work. This is a known limit of the demo, not exercised.
- `AWS_REGION_GLOBAL` now holds only `bootstrap` and `dns`.
