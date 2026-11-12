# Architecture decision records

Format: context, decision, consequences, alternatives. ADRs 0001 to 0013 are dated 2026-10-04 (updated at the close of spec 001 where the
implementation differs); 0014 to 0016 are dated 2026-10-14; 0017 and 0018 are dated 2026-11-10 (spec 002). Status "accepted" means decided for this
demo; several record accepted risks of a non-production project.

| ADR | Decision |
|---|---|
| [0001](0001-aws-sam.md) | AWS SAM / CloudFormation, one template per stack, parameters instead of exports |
| [0002](0002-aurora-postgresql-and-instance-class.md) | Aurora PostgreSQL, provisioned, `db.r6g.large` (burstable classes are not supported by Global Database) |
| [0003](0003-manual-weighted-routing.md) | Manual Route 53 weighted routing instead of failover routing with health checks |
| [0004](0004-api-key-only-accepted-risk.md) | API key (SSM SecureString) and usage plan only, no authorizer (accepted risk); `OPTIONS` without key. **Superseded by 0017** |
| [0005](0005-database-stacks-not-redeployed-during-drills.md) | Database stacks are not redeployed during or after a drill (drift) |
| [0006](0006-manual-deploy-only.md) | Deploy, destroy, simulate-outage and failover are manual workflows only |
| [0007](0007-schema-bootstrap-via-data-api.md) | Schema bootstrap through the RDS Data API, then disabled |
| [0008](0008-outage-simulation-method.md) | Outage simulation with reserved concurrency 0 on the six functions (script and workflow) |
| [0009](0009-iam-database-authentication.md) | IAM database authentication for the application |
| [0010](0010-two-database-users.md) | Two database users: writer and reader |
| [0011](0011-single-environment-trunk-on-main.md) | One environment (`prod`), trunk-based on `main` |
| [0012](0012-no-lambda-reserved-concurrency-in-template.md) | No Lambda reserved concurrency in the template; throttling, timeouts and alarms instead |
| [0013](0013-connection-discard-and-single-retry.md) | Discard a failed connection, retry once, read the database role on a fresh connection |
| [0014](0014-ledger-row-lock.md) | Ledger movements serialized with `SELECT ... FOR UPDATE` on the single account row |
| [0015](0015-planned-switchover-only.md) | Workflows run a planned switchover only; unplanned failover stays a manual runbook step |
| [0016](0016-organization-github-role-and-account-baseline.md) | Reuse the organization GitHub role; account-wide resources live in the account baseline repository |
| [0017](0017-cognito-multi-region-replication.md) | Cognito User Pool with Multi-Region Replication and Bearer ID tokens replace the API key; no WAF (accepted risk) |
| [0018](0018-region-realignment.md) | Cognito primary and edge in `us-east-2`, replica in `us-west-2`; bootstrap and `dns` stay in `us-east-1` |

Other design choices (no NAT or VPC endpoints, a network stack apart from the database, bootstrap and `dns` in `us-east-1`, default CloudFront
domains for the two sites, deletion protection plus a confirmed destroy, a separate CloudFormation execution role) are described
in [technical.md](../technical.md) and [architecture.md](../architecture.md).
