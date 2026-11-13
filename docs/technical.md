# Technical documentation

How the DR demo is built. Architecture diagrams and the security summary are in [architecture.md](architecture.md); user
view in [functional.md](functional.md); the drill in [dr-runbook.md](dr-runbook.md). The baseline stacks were deployed to one AWS account
on 2026-10-14 and checked read-only afterwards; the Cognito authentication of spec 002 and the region realignment were deployed on
2026-11-10 ([ADR 0017](adr/0017-cognito-multi-region-replication.md), [ADR 0018](adr/0018-region-realignment.md)); AWS behaviour that was not observed is said so in the README
([What was and was not verified](../README.md#what-was-and-was-not-verified)).

## Stack

| Layer | Choice |
|---|---|
| IaC | AWS SAM / CloudFormation, one template per stack in `templates/` ([ADR 0001](adr/0001-aws-sam.md)) |
| Database | Aurora PostgreSQL Global Database, provisioned, `db.r6g.large`, engine `16.15` by default, one instance per cluster ([ADR 0002](adr/0002-aurora-postgresql-and-instance-class.md)) |
| Compute | Lambda, `python3.14`, arm64, 256 MB, 20 s timeout, hexagonal architecture, `pg8000` driver |
| API | API Gateway REST, regional, custom domain in both regions, Cognito User Pools authorizer (Bearer ID token), stage throttling |
| Identity | Cognito User Pool in `us-east-2` replicated to `us-west-2` (Multi-Region Replication), customer managed KMS multi-region key ([ADR 0017](adr/0017-cognito-multi-region-replication.md)) |
| DNS | Route 53 weighted alias records 255 / 0, changed by a script or the `failover` workflow ([ADR 0003](adr/0003-manual-weighted-routing.md)) |
| Sites | Two static sites (HTML, CSS, JavaScript modules, no build step): the online banking app `src/bank/` and the read-only monitoring dashboard `src/dashboard/`, each in a private S3 bucket behind its own CloudFront distribution (OAC) |
| Delivery | GitHub Actions with OIDC (organization role), Environment `prod`, bash scripts under `scripts/` |
| Tests | pytest with a coverage gate of 85 percent (287 tests, 99 percent at the last recorded run, 2026-10-13), `ruff`, `mypy --strict`, `import-linter`, `node --test` for both sites |

## Components

### Stacks

Stack name pattern: `stk-<region without dashes>-drdemo-<stack>-prod`, for example `stk-useast2-drdemo-database-prod`.
Values move between stacks as parameters read by `scripts/deploy.sh` from stack outputs; there are no CloudFormation
exports, so each stack can be deleted on its own. Thirteen stacks belong to the project: the global bootstrap, and per data region `bootstrap`,
`cognito`, `network`, `database` and `api`, plus `edge` and `dns`.

| Stack | Template | Region | Contents | Key outputs |
|---|---|---|---|---|
| bootstrap (global) | `bootstrap.yaml` | global region (`us-east-1`), **by hand once, with a change set** | Artifact bucket, CloudFormation execution role and policies, dedicated deploy role (not used) | `DeployRoleArn`, `CfnExecRoleArn`, `ArtifactBucketName` |
| bootstrap (data) | `bootstrap.yaml` | each data region, by the workflow | Artifact bucket | `ArtifactBucketName` |
| cognito | `cognito.yaml` | primary region (pool, KMS multi-region key, app clients, replica declaration) and replica region (KMS replica key, regional attachment); both write their IDs to SSM `/drdemo/prod/cognito/*` | Customer managed KMS multi-region key, User Pool, two app clients, `UserPoolReplica`, replica key, `UserPoolRegionalConfigurationAttachment`, SSM parameters | `UserPoolId`, `UserPoolArn`, `BankClientId`, `DashboardClientId`, `KmsKeyArn` |
| network | `network.yaml` | each data region | VPC, two private subnets, two route tables | `VpcId`, `VpcCidr`, `PrivateSubnetIds` |
| database | `database.yaml` | each data region (`Role` primary or secondary **at creation**) | KMS key, security groups `sgp-*`, subnet group, cluster parameter group, global cluster and master secret (primary only), cluster and one instance, log group, SNS topic, alarms (replication lag, CPU, connections) | `DbClusterIdentifier`, `DbClusterArn`, `DbClusterEndpoint`, `DbClusterResourceId`, `LambdaSecurityGroupId`, `KmsKeyArn`, `AlertTopicArn`, `GlobalClusterIdentifier`, `MasterSecretArn` (primary) |
| api | `api.yaml` (SAM) | each data region | REST API with Cognito authorizer, six functions with one role each, log groups, custom domain with ACM certificate, base path mapping, alarms | `RegionalDomainName`, `RegionalHostedZoneId`, `ApiId`, `CertificateArn` |
| edge | `edge.yaml` | primary region (`us-east-2`) | Two private buckets, two CloudFront distributions, shared OAC and response headers policy (HSTS, CSP) | `DashboardBucketName`, `DistributionId`, `DistributionDomainName`, `BankBucketName`, `BankDistributionId`, `BankDistributionDomainName` |
| dns | `dns.yaml` | global region (`us-east-1`, stays there so the weight switch survives a `us-east-2` outage) | Two weighted alias records (`use2`, `usw2`) | `ApiFqdn` |

**Not in this project.** The GitHub OIDC provider and the API Gateway CloudWatch Logs role and account setting of each data
region are account-wide resources from the repository `nubesegura-accounts-baseline` (`templates/iam.yaml`, stacks
`stk-<region code>-baseline-iam-prod`). `scripts/deploy.sh` only checks that the API Gateway log role is set
([ADR 0016](adr/0016-organization-github-role-and-account-baseline.md)).

Deploy order (`scripts/deploy.sh --steps ...`; the `deploy` workflow runs it in three groups, each after a fresh OIDC login):

| Group | Steps |
|---|---|
| 1 | `bootstrap` (data regions), `edge`, `cognito`, `network`, `db-primary` |
| 2 | `db-secondary` |
| 3 | `schema`, `api`, `dns`, `dashboard` (publishes both sites) |

The `edge` stack comes before `api` because the API needs the addresses of both sites for CORS. The `cognito` step runs in two passes
(`EnableReplica=false` creates the pool and the KMS keys, then `true` creates the replica; the primary pool's deletion protection is set last);
it skips pass 1 when the replica already exists, because going back to `false` would delete it. The `api` step reads the regional Cognito
values from SSM in each data region. The `network` stack comes before the
database stacks, which receive the VPC and subnets as parameters. The global bootstrap stack is verified, never deployed, by the
script; its execution role is passed to CloudFormation with `--role-arn`.

No physical resource name contains "primary", "secondary", "writer" or "reader": roles swap at runtime, so names carry
the region only. The word "primary" in parameters (`PrimaryWeight`, `AWS_REGION_PRIMARY`) always means **the region
that was primary at creation**, `us-east-2`.

Resource name pattern: `<acronym>-<region without dashes>-drdemo-<descriptor>-prod`. Examples (the full list is the inventory in
[architecture.md](architecture.md#resource-inventory)):

| Resource | Name |
|---|---|
| VPC, subnets | `vpc-useast2-drdemo-prod`, `snet-useast2-drdemo-az1-prod` |
| Security groups | `sgp-useast2-drdemo-lambda-prod`, `sgp-useast2-drdemo-db-prod` |
| Aurora cluster, instance | `rdsc-useast2-drdemo-prod`, `rds-useast2-drdemo-prod` |
| Global cluster (created by the us-east-2 database stack) | `rdsg-useast2-drdemo-prod` |
| Functions | `fnc-useast2-drdemo-create-transaction-prod`, `...-list-transactions-...`, `...-get-stats-...`, `...-get-health-...`, `...-get-account-...`, `...-get-consistency-...` |
| IAM roles, managed policies | `iamr-useast2-drdemo-create-transaction-prod` (one per function), `iamr-useast1-drdemo-cfnexec-prod`, `iamp-useast1-drdemo-cfnexec-app-prod` |
| API, authorizer | `apig-useast2-drdemo-prod`, `apiga-useast2-drdemo-cognito-prod` (and `uswest2`) |
| Cognito pool, app clients, KMS key | `cgnp-useast2-drdemo-prod`, `cgnc-useast2-drdemo-bank-prod`, `cgnc-useast2-drdemo-dash-prod`, `alias/kms-useast2-drdemo-cognito-prod` |
| Alarm topic | `sns-useast2-drdemo-alerts-prod` |
| Master secret | `sm-useast2-drdemo-master-prod-<suffix>` |
| Buckets | `bckt-useast2-drdemo-dashboard-<account-id>-prod`, `bckt-useast2-drdemo-bank-<account-id>-prod`, `bckt-<region>-drdemo-artifacts-<account-id>-prod` |
| Distributions | `cfd-useast2-drdemo-dashboard-prod`, `cfd-useast2-drdemo-bank-prod` |

Mandatory tags on every taggable resource: `team-owner=nube-segura`, `project-name=aws-multi-region-aurora-failover-active-passive`, `app-name=dr-demo`,
`repo-name=aws-multi-region-aurora-failover-active-passive`, `env-type=prod`. Data stores also carry the `sec-*` classification tags (internal, synthetic
data, no PII). Exception: the `UserPoolTagsAtCreate` of the Cognito replica (`templates/cognito.yaml`) is create-only and keeps
the values the replica was created with (the project's former name), because changing it replaces the replica pool; the
cognito step of `scripts/deploy.sh` applies the current tags to the replica pool with `cognito-idp tag-resource`.

### Backend (hexagonal)

```
src/backend/
  dr_demo/domain/          Transaction (movement), NewTransaction validation, Account, consistency check, errors
  dr_demo/application/     ports (repository, clock, id generator, region provider) and use cases
  dr_demo/adapters/        inbound (API Gateway event and response mapping, CORS), outbound (pg8000 repository, IAM token)
  dr_demo/config/          settings, logging, container (the only composition root)
  services/<function>/handler.py   thin Lambda entry points (six)
```

`domain` and `application` never import `boto3` or `pg8000` (enforced by `import-linter` in CI). The function
authenticates with an IAM token generated locally by its role and verifies the server certificate with the RDS CA bundle
packaged under `certs/` (`scripts/fetch-rds-ca.sh` downloads it before `sam build`). Handlers write structured JSON logs
(request id, region, route, status, latency, and for movements the movement id and type) and never log bodies, tokens or passwords.

Movements are applied under a row lock on the single account row ([ADR 0014](adr/0014-ledger-row-lock.md)).

Database connection behaviour ([ADR 0013](adr/0013-connection-discard-and-single-retry.md)):

- One connection per execution environment, opened lazily with a fresh IAM token, 3 s TCP connect timeout, server-side
  `statement_timeout` of 5 s, 8 s socket backstop (they fit inside the 20 s function timeout together with one retry).
- Every failure discards the connection. If the connection had been reused, the call is retried **once** on a fresh one
  after a read-only rejection (SQLSTATE `25006`) or a connection or resource error (`08*`, `53*`, `57P*`). Authentication
  errors (`28*`) and statement timeouts (`57014`) answer `503 database_unavailable` without retry; any other database error
  answers `500 internal_error` without details. A wait for the account row lock that reaches the statement timeout answers
  `503 database_busy`.
- `database_role` always reads on a fresh connection, so `/stats`, `/account` and `/health?deep=true` never report a stale role after
  a switchover or failover.
- A write without `Idempotency-Key` is not replayed once its `COMMIT` may have been sent (a `503` is returned instead).

### Network and security controls

- Functions and Aurora live in two private subnets per region (the `network` stack: `10.40.0.0/24` in us-east-2,
  `10.41.0.0/24` in us-west-2, /25 subnets). There is no internet gateway, NAT gateway, VPC endpoint, public address or peering
  anywhere (CI greps for them). Functions never call AWS APIs from the VPC; the IAM authentication token is computed locally.
- The database security group `sgp-<rc>-drdemo-db-prod` accepts port 5432 only from the Lambda group `sgp-<rc>-drdemo-lambda-prod`;
  the Lambda group has no ingress and can only reach the database group.
- Aurora: encrypted with a customer managed key per region (rotation on, 30-day deletion window), not publicly
  accessible, `rds.force_ssl=1`, IAM database authentication on, backup retention 1 day, deletion protection driven by the
  parameter `DeletionProtection` (clusters and global cluster), `DeletionPolicy: Snapshot` on the clusters, PostgreSQL log
  exported to CloudWatch Logs.
- API Gateway: a Cognito User Pools authorizer validates `Authorization: Bearer <ID token>` on every method **except `GET /health` and
  `OPTIONS`** (CORS preflight, which cannot carry the token; it answers static headers and invokes nothing). The functions repeat the check:
  `aud` must be one of the two app client IDs and `token_use` must be `id` (`COGNITO_ALLOWED_CLIENT_IDS`), otherwise `401`. The default
  `execute-api` endpoint is disabled, TLS 1.2 security policy. CORS: the
  functions echo the request `Origin` only when it is one of the two site addresses (`ALLOWED_ORIGINS`) and always send `Vary: Origin`;
  the static preflight and the gateway's own `4XX`/`5XX` responses answer `Access-Control-Allow-Origin: *` because API Gateway cannot
  vary them (the token is still required). Access logs record `sub` and `username` and the authorizer latency, never bodies, headers or tokens.
- Sites: private buckets (all public access blocked, TLS-only policy), CloudFront OAC, CSP
  `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https://<api domain> https://cognito-idp.<primary region>.amazonaws.com https://cognito-idp.<replica region>.amazonaws.com; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`,
  HSTS, no inline code, tokens only in `sessionStorage`. `'self'` is needed so a page can load its own `config.json`.
- Delivery: the workflows assume the organization role `github-actions-terraform-prod-role` (secret `AWS_ROLE_ARN`) through OIDC and run
  from `main` in the `prod` Environment; CloudFormation uses the execution role `iamr-useast1-drdemo-cfnexec-prod`. Details and
  reasons: [ADR 0016](adr/0016-organization-github-role-and-account-baseline.md).
- Cognito: `ESSENTIALS` tier, administrator-created users only, min. 12-character complex password, ID and access tokens 15 minutes, refresh
  token 24 hours, pool encrypted with a customer managed KMS multi-region key, pool deletion protection `ACTIVE` after creation
  (`INACTIVE` only during the first create and the teardown).
- Accepted deviations from the team baseline (local Cognito users without federation, no WAF on CloudFront or API Gateway, no Multi-AZ, one-day backups, default CloudFront
  certificate, IAM database authentication, a broad execution role) are recorded in the ADRs and in the comments of the templates.

### Known limits (2026-11-10)

- `scripts/dr/*` do not toggle the Cognito replica attachment status; activating it during a drill is a manual step
  (see the [DR runbook](dr-runbook.md)). The `failover` workflow does it.
- `EnableIAMDatabaseAuthentication: true` is a deviation from the team baseline, recorded in
  [ADR 0009](adr/0009-iam-database-authentication.md).

## Data model

Two tables, created by `scripts/sql/schema.sql` (idempotent) and replicated to the secondary.

`transactions` is the append-only ledger:

| Column | Type | Notes |
|---|---|---|
| `seq` | `bigint generated always as identity`, unique | Replay order for the consistency check; gaps are normal |
| `id` | `uuid` primary key | Generated by the application |
| `idempotency_key` | `text` unique, nullable, 1 to 64 characters | Replays return the stored movement |
| `type` | `text` not null | `deposit` or `withdrawal` |
| `amount` | `numeric(12,2)` not null, `> 0` | |
| `balance_after` | `numeric(12,2)` not null, `>= 0` | Balance produced by this movement |
| `processed_region` | `text` not null | Region of the Lambda that served the write (`AWS_REGION`) |
| `processed_at` | `timestamptz` not null | UTC, millisecond precision as returned by the API |

`account` holds the one account: `id smallint` (always 1), `balance numeric(12,2)` (`>= 0`), `movement_count bigint`. The bootstrap
inserts it at balance 0 with `ON CONFLICT DO NOTHING`, so repeating the bootstrap never resets the balance. There is no currency and
no description (the currency is fixed to USD and not shown). Index `idx_transactions_processed_at` on `(processed_at DESC, id DESC)`
serves the latest-movements queries.

Database users (both log in with IAM, `GRANT rds_iam`; neither owns objects, has DDL rights or is a superuser;
[ADR 0010](adr/0010-two-database-users.md)):

| User | Rights | Used by |
|---|---|---|
| `app_writer` | `SELECT`, `INSERT` on `transactions`; `SELECT`, `UPDATE` on `account` | `create-transaction` only |
| `app_reader` | `SELECT` on `transactions` and `account` | the other five functions |

The master user is `dbadmin`; its password is generated into a Secrets Manager secret by the primary database stack (RDS cannot manage
the master password of a global database) and is used only by the one-time schema bootstrap. The database name is `drdemo`.

`database_role` in responses comes from `pg_is_in_recovery()`: `reader` on a secondary cluster, `writer` on a writable one. The
`POST /transactions` answer reports `writer` without a query (a successful write proves it).

## API

Base URL: `https://<API_DOMAIN_NAME>` (the base path mapping has no prefix; the stage name `prod` is not in the URL).
Every method needs the header `Authorization: Bearer <Cognito ID token>`, except `GET /health` and `OPTIONS` (CORS preflight, answered by
API Gateway). Without a valid token API Gateway answers `401` before any function runs; a token that passes the authorizer but has the wrong
audience or is not an ID token gets `401 unauthorized` from the function. The ID token comes from Cognito `InitiateAuth` with
`USER_PASSWORD_AUTH` against the pool of the serving region and lives 15 minutes.

Headers on every function response: `Content-Type: application/json`, `X-Served-By-Region: <region>`, `Vary: Origin`,
`Access-Control-Allow-Origin: <request origin>` only when the origin is one of the two sites,
`Access-Control-Expose-Headers: X-Served-By-Region, Retry-After`.

### Routes

| Method and path | Success | Errors |
|---|---|---|
| `POST /transactions` | `201` new movement, `200` replay of a used `Idempotency-Key` | `401 unauthorized`, `400 validation_error`, `422 insufficient_funds`, `503 database_read_only` and `503 database_busy` (header `Retry-After: 5`), `503 database_unavailable`, `500 internal_error` |
| `GET /transactions?limit=N` | `200` | `401`, `400 validation_error` (limit not an integer from 1 to 100), `503 database_unavailable` |
| `GET /stats` | `200` | `401`, `503 database_unavailable` |
| `GET /account` | `200` | `401`, `503 database_unavailable` |
| `GET /consistency` | `200` | `401`, `503 database_unavailable` |
| `GET /health` | `200` (no database access, no token needed) | |
| `GET /health?deep=true` | `200` with database role (no token needed) | `503 database_unavailable` |

### Shapes

Amounts and balances are JSON strings with exactly two decimals (they never pass through floating point). `POST` request body: `type`
(`deposit` or `withdrawal`) and `amount` (a JSON number, positive, at most 2 decimals, at most 9,999,999,999.99); unknown fields
are rejected; every problem is listed together. A deposit that would push the balance above the maximum is a `400` on `amount`.

Movement record (`transaction_json`):

```json
{
  "id": "0b5f1a52-6a9e-4c1c-9a3a-2f1f6e6f1c11",
  "type": "deposit",
  "amount": "12.50",
  "balance_after": "112.50",
  "processed_at": "2026-10-14T12:00:00.123Z",
  "processed_region": "us-east-2"
}
```

`POST /transactions` answer (`201`, or `200` with the original result for a known key):

```json
{
  "transaction": { "id": "...", "type": "deposit", "amount": "12.50", "balance_after": "112.50",
                   "processed_at": "2026-10-14T12:00:00.123Z", "processed_region": "us-east-2" },
  "balance": "112.50",
  "served_by_region": "us-east-2",
  "database_role": "writer",
  "server_ms": 18.4
}
```

`GET /transactions` returns `{"items": [<movement>, ...]}`, newest first (default 20, maximum 100).

`GET /account`:

```json
{ "balance": "112.50", "movement_count": 7, "served_by_region": "us-west-2", "database_role": "writer" }
```

`GET /stats`:

```json
{
  "total_count": 42,
  "counts_by_region": {"us-east-2": 40, "us-west-2": 2},
  "last_processed_at_by_region": {"us-east-2": "2026-10-14T12:00:00.123Z", "us-west-2": "2026-10-14T12:30:01.456Z"},
  "latest_transactions": [ "<up to 10 movements>" ],
  "served_by_region": "us-west-2",
  "database_role": "writer",
  "generated_at": "2026-10-14T12:30:05.000Z"
}
```

With no movements the counts are empty objects and `latest_transactions` is `[]`; `served_by_region` and `database_role` are still present.

`GET /consistency` replays the ledger in `seq` order with read-only access (at most 50,000 rows):

```json
{
  "count": 7, "total_deposited": "150.00", "total_withdrawn": "37.50",
  "replayed_balance": "112.50", "stored_balance": "112.50",
  "balances_match": true, "chain_ok": true, "duplicate_keys": 0,
  "counts_by_region": {"us-east-2": 5, "us-west-2": 2},
  "consistent": true, "first_offender": null, "truncated": false,
  "served_by_region": "us-west-2"
}
```

`consistent` is true only when the replayed and stored balances match, every `balance_after` follows from the previous one and no
idempotency key is duplicated; otherwise `first_offender` carries the id of the first offending movement. When the ledger has more
than 50,000 rows the answer is `200` with `truncated: true` and `consistent: null` (nothing is claimed either way).

`GET /health` returns `{"status": "ok", "region": "us-east-2"}`; with `deep=true` it adds `"database_role": "writer"`.

Error body (same shape for every error; `fields` is empty except for validation errors; `insufficient_funds` adds `balance`):

```json
{"error": {"code": "validation_error", "message": "The request is not valid.",
           "fields": [{"field": "amount", "message": "<reason>"}]}}
```

Error codes: `unauthorized` (401), `validation_error` (400), `insufficient_funds` (422), `database_read_only` (503), `database_busy` (503), `database_unavailable`
(503), `internal_error` (500).

Limits: stage throttling per region of 20 requests per second, burst 40 (there is no usage plan and no daily quota). One open monitoring
dashboard tab makes three requests every 5 seconds (`/stats`, `/account`, `/transactions?limit=50`). Beyond the throttle API Gateway answers `429`.

Known limitation: replaying an `Idempotency-Key` with a **different** payload returns the original result (`200`) and
ignores the new payload; the API does not report a conflict.

## Configuration

### `parameters/prod.json`

The only file with environment values; `scripts/deploy.sh` reads it with `jq`.

| Section | Key | Default | Meaning |
|---|---|---|---|
| `network.Common` | `EnvType` | `prod` | Name segment and tag. Templates accept `dev` or `prod`; the scripts fix `prod` ([ADR 0011](adr/0011-single-environment-trunk-on-main.md)) |
| `network.Primary` / `network.Secondary` | `VpcCidr`, `SubnetCidrs` | `10.40.0.0/24` with `10.40.0.0/25,10.40.0.128/25`; `10.41.0.0/24` with `10.41.0.0/25,10.41.0.128/25` | Must not overlap each other or other projects of the organization |
| `database.Common` | `EnvType` | `prod` | Name segment, tag |
| | `DbInstanceClass` | `db.r6g.large` | Overridden by the variable `DB_INSTANCE_CLASS` |
| | `EngineVersion` | `16.15` | Overridden by the variable `ENGINE_VERSION` |
| | `DbName`, `DbMasterUsername` | `drdemo`, `dbadmin` | |
| | `PreferredMaintenanceWindow` | `sun:06:00-sun:06:30` | |
| | `DeletionProtection` | `true` | Drives the two clusters **and** the global cluster; the destroy procedure switches it off first. During the first deployment it was set to `false` to avoid a stuck `ROLLBACK_FAILED` stack |
| | `EnableDataApi` | `false` | Turned on only around the schema bootstrap |
| | `LogRetentionDays` | `30` | |
| | `AlertEmail`, `KeyAdminRoleArn` | empty | `AlertEmail` comes from the variable `ALERT_EMAIL`; the key admin defaults to the execution role |
| `database.Primary` / `database.Secondary` | `Role` | `primary`, `secondary` | Role **at creation** |
| `cognito.Common` | `EnvType`, `PrimaryRegion`, `ReplicaRegion` | `prod`, `us-east-2`, `us-west-2` | |
| | `EnableReplica` | `false` | The deploy script drives it (two passes); do not apply it by hand as `false` once the replica exists |
| | `KeyAdminRoleArn` | empty | Optional KMS key administrator; empty delegates to the account root |
| `cognito.Primary` | `UserPoolDeletionProtection` | `INACTIVE` | Set to `ACTIVE` by the last deploy pass; lifted again by the destroy |
| `cognito.Secondary` | `ReplicaAttachmentStatus` | `INACTIVE` | `ACTIVE` only while the replica serves (the `failover` workflow sets it); `PrimaryKeyArn`, `PrimaryUserPoolId`, `BankClientId`, `DashboardClientId` are filled by the deploy script from the primary stack |
| `api.Common` | `EnvType`, `DbName` | `prod`, `drdemo` | |
| | `DbWriterUser`, `DbReaderUser` | `app_writer`, `app_reader` | Must match `scripts/sql/schema.sql` (checked by `init-schema.sh`) |
| | `LogLevel`, `LogRetentionDays` | `INFO`, `30` | |
| | `ThrottleRate`, `ThrottleBurst` | `20`, `40` | Stage throttling per region |
| | `CognitoUserPoolArn`, `CognitoBankClientId`, `CognitoDashboardClientId` | empty | Filled by the deploy script from the regional SSM parameters |
| | `PermissionsBoundaryArn` | empty | Optional |

Values that depend on the account or region (`ApiDomainName`, `HostedZoneId`, `AllowedOrigins`, endpoints,
VPC and subnet ids, ARNs) are passed on the command line by the deploy script from the variables and stack outputs.

### Lambda environment variables

`DB_HOST` (cluster endpoint of the region), `DB_PORT` (`5432`), `DB_NAME`, `DB_USER` (`app_writer` or `app_reader`),
`ALLOWED_ORIGINS` (the two site origins, comma-separated, each an `https://` origin, validated at cold start),
`COGNITO_ALLOWED_CLIENT_IDS` (the two app client IDs, comma-separated), `LOG_LEVEL`.
`AWS_REGION` is set by Lambda. Optional `DB_CA_BUNDLE_PATH`. No secret is stored in an environment variable. The connection uses the
**cluster endpoint**, which is a reader before a promotion and a writer after it; nothing in the functions or stacks changes during a drill.

Reserved concurrency is intentionally not set in the template: the drill sets it to `0` and removes it afterwards, and a template
value would be reverted by the next stack update. The consequences and the mitigations that stand in for it are in
[ADR 0012](adr/0012-no-lambda-reserved-concurrency-in-template.md).

## Delivery

| Script | Purpose |
|---|---|
| `scripts/deploy.sh [--dry-run] [--steps ...]` | Ordered deployment: `bootstrap`, `edge`, `cognito`, `network`, `db-primary`, `db-secondary`, `schema`, `api`, `dns`, `dashboard` |
| `.github/workflows-local/provision-users/provision_users.py` | Owner step after the first deploy: creates the confirmed demo users `bank-operator` and `dashboard-viewer` in the primary pool and waits until the replica shows them (settings in its own `.env`, template `.env.example`; tests in `tests/`) |
| `scripts/init-schema.sh [--dry-run]` | Applies `scripts/sql/schema.sql` to the primary through the RDS Data API |
| `scripts/publish-dashboard.sh`, `scripts/publish-bank.sh [--dry-run]` | Upload `src/dashboard/` or `src/bank/`, write `config.json` (`apiBaseUrl` from `API_DOMAIN_NAME`, and the Cognito region, pool ID, client ID and endpoint of both regions read from SSM), invalidate CloudFront |
| `scripts/fetch-rds-ca.sh` | Downloads the RDS CA bundle into `src/backend/certs/` |
| `scripts/destroy.sh [--dry-run] [--steps 1-8]` | Ordered removal with typed confirmations |
| `scripts/dr/*.sh` | The drill ([dr-runbook.md](dr-runbook.md)) |

Workflows (all in `.github/workflows/`, actions pinned by commit SHA): `ci.yml` (every push and pull request, no AWS access);
`deploy.yml` and `destroy.yml` (manual, three groups each); `simulate-outage.yml` and `failover.yml` (manual, from `main` only); `demo-1-outage.yml`,
`demo-2-failover.yml` and `demo-3-failback.yml` (manual, from `main` only, no inputs: the same steps with the region and direction
fixed, for live demos). All the manual ones share the concurrency group `drdemo-prod-infrastructure`.

Behaviour worth knowing:

- Every script supports `--dry-run` (no AWS call, placeholders), prints timestamped actions and the account masked
  (last four digits), and masks secrets in everything it prints.
- `deploy.sh` checks that the engine version and class support Global Database in both regions before creating anything.
- A template larger than the 51,200 byte inline limit of CloudFormation is sent through the artifact bucket of its region; `bootstrap.yaml`
  is currently under the limit (about 48,500 bytes).
- `deploy.sh` reads the live global cluster first. If the primary-region cluster is no longer the writer (a drill or
  failover swapped the roles) it **skips** the database and schema steps with a warning ([ADR 0005](adr/0005-database-stacks-not-redeployed-during-drills.md)).
- An existing `dns` stack keeps its current weights on a repeat deploy; only `30-switch-traffic.sh` (or the `failover` workflow) moves traffic.
- Schema bootstrap: the Data API is switched on for the primary cluster only for this step and switched off again right
  afterwards, with a check ([ADR 0007](adr/0007-schema-bootstrap-via-data-api.md)). If the deploy fails in between, an exit handler tries to close it.
- The `api` step reads the regional Cognito parameters (`UserPoolArn`, `BankClientId`, `DashboardClientId`) from SSM in each data region and
  stops with a message when one is missing (deploy the `cognito` stack first). No secret is passed to the API stacks.
- The `failover` workflow (and `.github/workflows-local/failover.sh`) sets the Cognito replica attachment `ACTIVE` before the switchover and
  `INACTIVE` after the database is back, by updating the replica-region `cognito` stack (`cognito_replica_set_status` in `scripts/lib/dr.sh`).
- The `api` step also checks that the API Gateway account CloudWatch Logs role is set in both data regions (account baseline) and stops
  otherwise.
- A stuck stack (for example `ROLLBACK_COMPLETE`) is never retried or deleted by the scripts; investigate with
  `aws cloudformation describe-stack-events`.

CI (`.github/workflows/ci.yml`, on every push and pull request, no AWS access): backend (`ruff check`, `ruff format
--check`, `mypy --strict`, `lint-imports`, `pytest` with 85 percent coverage, plus the `.github/workflows-local` suites), both sites (`node --test`, `node --check`,
`jq .` on JSON), templates (`cfn-lint`, `sam validate --lint`, `checkov`, a forbidden-network-resource grep), scripts
(`shellcheck`, `bash -n`, `--dry-run` of every script, the tests under `scripts/tests/`), workflows (`actionlint`, `checkov`, and a grep
that fails if a workflow passes `--force`, `--unplanned` or `FORCE_UNFENCE`).

## Sites

Two independent static sites, each with `index.html`, `app.js`, `logic.js` (pure functions, tested with `node --test`), `styles.css`,
`error.html` and `config.json` (generated at publish time with `apiBaseUrl` and the `cognito` block of both regions). No code is shared; each
is published alone. Each site has a sign-in form (the banking app expects `bank-operator`, the dashboard `dashboard-viewer`), calls Cognito
`InitiateAuth` (`USER_PASSWORD_AUTH`) from the browser, keeps the tokens in `sessionStorage` of its own origin, refreshes them before they
expire, signs out on demand, and sends the ID token as `Authorization: Bearer`. When the API answers from the other region it
re-authenticates against that region's pool endpoint (4 second timeout). The password is never written to web storage or the page.

- **Online banking app** (`src/bank/`): balance (`GET /account` on load and after every movement), an amount field, a selector
  (deposit or withdrawal) and an **Execute** button. One `Idempotency-Key` (`crypto.randomUUID()`) per click, reused in every retry;
  automatic retries with a growing wait for at most 60 seconds on a network error, `429`, `500`, `502`, `503` and `504` (never on `400`
  or `422`), shown as "recovering, attempt N". It shows the region and backend that answered, the database role, the browser round-trip
  time and the server time, the attempts, the session list with min, median and maximum, and a **Verify my operations** button that
  compares the acknowledged ids with `GET /transactions?limit=100`.
- **Monitoring dashboard** (`src/dashboard/`, read-only, never posts): polls every 5 seconds (request timeout 4 seconds) `GET /stats`,
  `GET /account` and `GET /transactions?limit=50`; shows the serving region, database role, balance, movements per region, the latest 50
  movements, a banner and a timeline when the region or role changes, its own poll round-trip statistics, an "Unavailable" state that keeps
  the last data, and a **Verify** button that calls `GET /consistency` only on demand.

`scripts/dashboard-mock.mjs` is a local mock API that serves both sites on two ports (two origins, as in production) for trying the
pages without AWS.

## Drill runner (`scripts/e2e/`)

A local Python package, `dr_drill`, that carries out the planned DR drill against the deployed prod environment and measures RTO and RPO.
Full usage, exit codes, stop rules and the output guard are in `scripts/e2e/README.md`; the results of the run are in
[dr-test-report.md](dr-test-report.md).

- **Commands** (run from `scripts/e2e`): `python -m dr_drill check` (read-only preconditions, writes nothing),
  `python -m dr_drill probe-smoke` (60 s of probes in the primary region, no workflow), `python -m dr_drill run` (the full drill, typed
  confirmation; `--authorized` skips it) and `python -m dr_drill formal-report --run <folder>` (writes `docs/dr-test-report.md` and
  `docs-es/dr-test-report.md` from the stored timelines).
- **What it does:** signs in with `USER_PASSWORD_AUTH`, writes net-zero deposits and withdrawals of 0.01 through the public API (about one
  per second, retries with the same `Idempotency-Key`), reads `GET /account` and `GET /health?deep=true`, signs in against the secondary
  region, watches the DNS answer, dispatches the `simulate-outage` and `failover` workflows with `gh`, and reads the replication lag from
  CloudWatch (read calls only). It stops at the first problem and never retries, forces or unfences.
- **Cognito user:** `e2e-runner`, created with `provision_users.py --only e2e-runner`
  (see `.github/workflows-local/README.md`). Its password and e-mail address come from git-ignored local settings (`scripts/e2e/.env`,
  `.github/workflows-local/provision-users/.env`) and are not recorded in any document.
- **CI:** the `drill-runner` job of `.github/workflows/ci.yml` runs `ruff check`, `ruff format --check`, `mypy --strict` and `pytest` over
  `scripts/e2e`, with no AWS call and no network. The 85 % coverage gate is set in `scripts/e2e/pyproject.toml`, not on the command line.
- **Outputs:** each `probe-smoke` and `run` writes `run.log`, `timeline.json` and `report.md` to
  `reports/e2e/<UTC timestamp>/`, a git-ignored folder. Only the formal reports under `docs/` and `docs-es/` are versioned.

### Known limits (2026-11-11)

From the single measured drill of 2026-11-11 (planned path only):

- **DNS convergence is negative** (-43.7 s to-secondary, -49.7 s to-primary): the resolver answer changed before the traffic-switch step
  ended, and it changed 9 times for 2 switches. It is an indication from one resolver, not a measurement to quote.
- **Secondary sign-in availability** was 70 % (to-secondary) and 43 % (to-primary) in the outage windows. This is partly by design: the
  `failover` workflow activates and deactivates the Cognito replica, so it is not the availability of the service for a user.
- **Failover probe deposits were SKIPPED** in both directions: `failover.yml` gives its validate step no Cognito token, so the workflow's
  own validation proves no authenticated write. The runner's write probe covers it; the workflow gap remains.
- **12 probe records were dropped by the old output guard** and are absent from the timeline, so counts and percentiles cover slightly
  fewer attempts than the probes made. The pattern that matched was not investigated.
- **One drill run so far:** the RTO is a single sample, with no range. The RPO is the one of a planned switchover (0 by construction);
  the unplanned failover was not exercised. The replication lag is only a bound.
- **`AuroraGlobalDBRPOLag` returned datapoints** (maximum 1531 ms) although the repository does not set `rds.global_db_rpo`; the reason
  was not verified.

## Runbook summary

Full steps, expected results and undo: [dr-runbook.md](dr-runbook.md).

| Step | Script or workflow | What it does |
|---|---|---|
| Pre-check | `scripts/dr/00-precheck.sh` | Read-only: global members and live roles, cluster status, replication lag, fence state, Route 53 weights, `/stats` baseline (with `COGNITO_ID_TOKEN`) |
| Simulate outage | `scripts/dr/10-simulate-outage.sh`, workflow `simulate-outage` | Reserved concurrency 0 on the six functions of a region (`--undo` restores, `--status` reports) |
| Promote | `scripts/dr/20-promote.sh` | Planned switchover (default) or `--unplanned` failover with data loss allowed (manual only) |
| Switch traffic | `scripts/dr/30-switch-traffic.sh` | Updates the dns stack weights (`--to secondary` default, `--to primary`) |
| Validate | `scripts/dr/40-validate.sh` | `/stats` (with `COGNITO_ID_TOKEN`) and `/health?deep=true` on the custom domain, optional `--probe` deposit of 0.01 (needs the token) |
| Consistency | `scripts/dr/45-consistency.sh` | `GET /consistency` (needs `COGNITO_ID_TOKEN`); exit 0 consistent, 1 not, 2 truncated |
| Failback | `scripts/dr/50-failback-promote.sh`, `scripts/dr/60-restore.sh` | Promote the original primary again, lift the fence, move traffic back |
| Both directions in one run | workflow `failover` (`to-secondary`, `to-primary`) | The ordered steps above plus the Cognito replica activation and deactivation, with timings and a job summary; no token, so no probe or consistency check |

The planned switchover waits (`lag_gate` in `scripts/lib/promote.sh`) up to 5 minutes for the first `AuroraGlobalDBReplicationLag`
datapoint of a cluster that just became a secondary, then fails closed if there is none or the query fails.

## Alarms

Every alarm sends to the regional SNS topic `sns-<region>-drdemo-alerts-prod` (on ALARM and OK). The topic has an e-mail
subscription only if `ALERT_EMAIL` is set; the subscriber must confirm it. Eight alarms exist per data region.

| Alarm | Region | Condition | Meaning |
|---|---|---|---|
| `alrm-<region>-drdemo-replication-lag-prod` | both data regions (follows the cluster that is the secondary) | `AuroraGlobalDBReplicationLag` maximum above 5000 ms for 3 consecutive 1-minute periods; missing data is not breaching | Replication is behind: do not start a planned switchover; an unplanned failover now would lose more |
| `alrm-<region>-drdemo-api-4xx-prod` | each API | `4XXError` sum above 25 in one 5-minute period | Spike of authorizer or client errors (`401`): check the access logs for rejected tokens |
| `alrm-<region>-drdemo-api-5xx-prod` | each API | `5XXError` sum above 5 in 5 minutes | Server errors: look at function logs and the database state |
| `alrm-<region>-drdemo-lambda-errors-prod` | each API | Errors summed over the six functions above 5 in 5 minutes | Function failures |
| `alrm-<region>-drdemo-lambda-throttles-prod` | each API | Throttles summed over the six functions above 5 in 5 minutes | Expected during the simulated outage (concurrency 0); otherwise a quota or fence left in place |
| `alrm-<region>-drdemo-db-cpu-prod` | both data regions (database stack) | `CPUUtilization` average of the cluster above 80 percent for 3 consecutive 5-minute periods; missing data is not breaching | High query load: check the connection count |
| `alrm-<region>-drdemo-db-connections-prod` | both data regions (database stack) | `DatabaseConnections` maximum of the cluster above 100 for 2 consecutive 5-minute periods; missing data is not breaching | Many open connections (one per Lambda execution environment): look for a request storm |
| `alrm-<region>-drdemo-cert-expiry-prod` | each API | ACM `DaysToExpiry` below 30 | Certificates renew by themselves while the DNS validation record exists |

During the drill the fence triggers the throttles and the 5xx alarms of the fenced region; that is the expected signal and
clears after the undo.

## Logs

Lambda: `/aws/lambda/<function name>`; API access log group per API; API Gateway execution log group
`API-Gateway-Execution-Logs_<api id>/prod` (error level); RDS `/aws/rds/cluster/rdsc-<region>-drdemo-prod/postgresql`. All have 30-day
retention and are encrypted with the regional key. CloudTrail is assumed to exist at account level (not part of this project).
