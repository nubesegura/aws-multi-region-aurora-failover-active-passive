# ADR 0009: IAM database authentication for the application

- Status: accepted (owner decision; deviates from the team standard)
- Date: 2026-10-04

## Context

The team standard forbids enabling IAM database authentication on RDS and prefers a password in Secrets Manager. In this
design the Lambda functions run in private subnets with **no NAT and no VPC endpoints**, so a function cannot call
Secrets Manager to read a password, and a password in an environment variable would break the "no secrets in
environment variables" rule.

## Decision

`EnableIAMDatabaseAuthentication` is on for both clusters. Each function builds a short-lived authentication token
locally from its own execution role (a presigned request, valid 15 minutes, no network call) and uses it as the PostgreSQL password.
The connection requires TLS (`rds.force_ssl=1`) and verifies the server certificate with the RDS CA bundle packaged with
the code. No database password exists in code, templates, parameters, environment variables or logs. The master
password stays in a generated Secrets Manager secret (primary region) and is used only by the schema bootstrap ([ADR 0007](0007-schema-bootstrap-via-data-api.md)).

## Consequences

- Each role has `rds-db:connect` on exactly `arn:aws:rds-db:<region>:<account-id>:dbuser:<cluster-resource-id>/<db-user>`,
  never `*`. The cluster **resource id** (not the identifier) changes if a cluster is rebuilt, so after the manual rebuild
  of a secondary the api stack must be updated with the new id ([runbook U7](../dr-runbook.md)).
- Two database users limit the blast radius ([ADR 0010](0010-two-database-users.md)).
- IAM authentication has connection-rate limits; the demo load (below one request per second) is far below them.
- The deviation from the team standard is deliberate and recorded here.
- IAM authentication works the same in the promoted region because each region has its own function roles and ids.

## Alternatives considered

RDS Proxy with a Secrets Manager password (needs a VPC endpoint or NAT, costly); a password in Secrets Manager read by
the function (needs a route to Secrets Manager); a password in an environment variable (rejected).
