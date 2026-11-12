# ADR 0017: Cognito User Pool with Multi-Region Replication replaces the API key

- Status: accepted (spec 002, deployed and working in `prod` on 2026-11-10); supersedes [ADR 0004](0004-api-key-only-accepted-risk.md)
- Date: 2026-11-10

## Context

The API key of ADR 0004 was a single shared credential typed into a browser. The demo should show that the **sign-in itself** survives the
loss of a region, not only the data, so the identity provider must exist in both regions.

## Decision

- Amazon Cognito User Pool (`ESSENTIALS` tier) in `us-east-2`, replicated to `us-west-2` with Multi-Region Replication
  (`AWS::Cognito::UserPoolReplica`). The pool is encrypted with a customer managed KMS multi-region key (replica key in `us-west-2`).
  Users are created by an administrator only (`AllowAdminCreateUserOnly`); `.github/workflows-local/provision-users/provision_users.py`
  creates `bank-operator` and `dashboard-viewer`.
- Two public app clients without secret (banking app, monitoring dashboard), `USER_PASSWORD_AUTH` and refresh tokens. ID and access tokens
  last 15 minutes, refresh tokens 24 hours. The replica shares the pool ID and the client IDs; only the pool ARN differs per region.
- Every API route except `GET /health` and `OPTIONS` needs `Authorization: Bearer <ID token>`. Each regional API Gateway has a native Cognito
  User Pools authorizer that reads its own regional pool ARN from SSM (`/drdemo/prod/cognito/UserPoolArn`, `BankClientId`,
  `DashboardClientId`). The functions repeat the check (`aud` is one of the two client IDs, `token_use` is `id`) and answer `401` otherwise.
- In `us-west-2` the regional configuration attachment of the replica is `INACTIVE` at rest. The `failover` workflow sets it `ACTIVE` before
  the switchover (`to-secondary`) and `INACTIVE` after the database is back (`to-primary`).
- Deployment is two-pass because the replica key must exist before the replica pool (`EnableReplica=false` then `true`), and the pool's
  deletion protection is turned on last (see [architecture.md](../architecture.md#stacks)). The static API key paths were removed.
- **No AWS WAF** on CloudFront or API Gateway: accepted risk of a demo (owner decision, 2026-11-10).

## Consequences

- The sites sign in themselves, keep tokens in `sessionStorage`, refresh them, and re-authenticate against the other region when it
  serves the traffic. Scripts and Postman need an ID token (`COGNITO_ID_TOKEN`, `cognito_id_token`) that expires every 15 minutes.
- The `failover` workflow holds no Cognito credential, so its probe deposit and ledger consistency check are skipped there; the
  runbook asks for `45-consistency.sh` with a token afterwards.
- Applying the `cognito` stack by hand with `EnableReplica=false` after the replica exists would delete the replica; use `scripts/deploy.sh`.
- Added cost is small (two KMS keys, replication of two demo users; unverified estimate in [cost-and-cleanup.md](../cost-and-cleanup.md)).
- Without a WAF there is no rate-based rule or managed rule set in front of the API; the stage throttle (20 per second, burst 40) and the
  authorizer are the only controls. A regional or CloudFront WAF stays a possible later addition.

## Alternatives considered

Keeping the API key (no regional identity), a Lambda authorizer, IAM authorization, an external identity provider, a second independent
user pool per region (users would have to be created twice and tokens would not be portable).
