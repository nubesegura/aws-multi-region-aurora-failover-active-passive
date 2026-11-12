# ADR 0010: Two database users, writer and reader

- Status: accepted
- Date: 2026-10-04

## Context

Only one function writes (`create-transaction`); the other five only read. A single application user with write rights
on every function would give a read-only route the ability to modify data if it were compromised.

## Decision

`scripts/sql/schema.sql` creates two PostgreSQL roles, both with login and `GRANT rds_iam`, neither owning any object,
with no DDL rights and not superusers:

| User | Rights | Used by |
|---|---|---|
| `app_writer` | `SELECT`, `INSERT` on `transactions` (the ledger); `SELECT`, `UPDATE` on `account` (the balance row) | `create-transaction` |
| `app_reader` | `SELECT` on `transactions` and `account` | `list-transactions`, `get-stats`, `get-health`, `get-account`, `get-consistency` |

The user names are the parameters `DbWriterUser` and `DbReaderUser` (`parameters/prod.json`); `scripts/init-schema.sh`
refuses to run if they differ from the SQL file. Each function role is allowed `rds-db:connect` for **its own** user
only. The idempotency lookup of `create-transaction` needs `SELECT`, which the writer has. Neither user can `DELETE` from or update the ledger.

## Consequences

- A compromised read function cannot insert, update or delete rows.
- The users replicate to the secondary with the rest of the data, so no step is needed after a failover.
- The reader user also works on a secondary cluster (read-only), which is how `database_role` is reported there.
- Changing the names means changing the parameter file and the SQL together.

## Alternatives considered

One shared application user (simpler, wider rights); one user per function (more objects for no extra benefit here); the
master user in the application (never).
