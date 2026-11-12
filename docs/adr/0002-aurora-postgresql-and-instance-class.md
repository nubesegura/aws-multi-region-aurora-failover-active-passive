# ADR 0002: Aurora PostgreSQL, provisioned, `db.r6g.large`

- Status: accepted
- Date: 2026-10-04

## Context

The demo needs a database replicated across regions with a managed way to promote the copy. Aurora Global Database
offers that for Aurora MySQL and Aurora PostgreSQL. Cost matters: two instances run for the whole demo.

## Decision

- Engine: **Aurora PostgreSQL**-compatible (default engine version `16.15`, a parameter; `16.6` did not accept Global Database at the first deployment). Reasons: a managed
  replication-lag and RPO feature exists for it, the role of a cluster is visible from SQL with `pg_is_in_recovery()`
  (used for `database_role`), and a pure-Python driver (`pg8000`) needs no layer.
- Mode: **provisioned**, never Serverless, one instance per cluster (no reader replica inside a region: a possible later feature).
- Instance class: **`db.r6g.large`** (default parameter `DbInstanceClass`). **Burstable classes (`db.t*`) are not
  supported by Aurora Global Database**, so the cheapest class that Global Database accepts is a memory-optimized
  Graviton class; `db.r6g.large` is the smallest of that family used here. `db.r5.large` is the documented fallback
  (about USD 44 more per month in total, unverified estimate).
- Verification before deploying: `scripts/deploy.sh` runs, in both data regions, `describe-db-engine-versions`
  (`SupportsGlobalDatabases`) and `describe-orderable-db-instance-options` for the chosen version and class, and stops
  with a clear message if they are not accepted. The same check by hand:

  ```bash
  aws rds describe-db-engine-versions --region us-east-2 --engine aurora-postgresql --engine-version 16.15 \
    --query 'DBEngineVersions[0].SupportsGlobalDatabases'
  aws rds describe-orderable-db-instance-options --region us-east-2 --engine aurora-postgresql --engine-version 16.15 \
    --db-instance-class db.r6g.large --query 'OrderableDBInstanceOptions[?SupportsGlobalDatabases].DBInstanceClass'
  ```

## Consequences

- Two `db.r6g.large` instances are about 97 percent of the running cost (see [cost-and-cleanup.md](../cost-and-cleanup.md));
  the way to save money is to destroy the demo after use.
- One instance per cluster means no high availability inside a region: this demo shows regional DR, not AZ resilience.
- Backups are kept for 1 day only (synthetic data); deletion protection is on (the parameter `DeletionProtection` drives the two clusters and the global cluster; the destroy workflow turns it off first).
- `db.r6g.large` with engine `16.15` was accepted for Global Database in both regions at the first deployment (2026-10-14);
  the deploy script checks it before it creates anything. Choose another version or class through the variables `ENGINE_VERSION` and `DB_INSTANCE_CLASS`.

## Alternatives considered

- Aurora Serverless v2: not what the requirement asks for (provisioned), and its minimum capacity is billed continuously.
- Burstable `db.t*` classes: not supported by Global Database.
- Writer only, no secondary: no DR demo. A headless secondary (no instance): breaks the 15-minute RTO target.
- Aurora MySQL: possible, but the points above favour PostgreSQL.
