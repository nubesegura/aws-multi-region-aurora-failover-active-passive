# ADR 0013: Discard a failed connection and retry once; read the role on a fresh connection

- Status: accepted
- Date: 2026-10-04

## Context

A warm Lambda execution environment keeps its database connection between invocations. During a switchover or failover the
cluster endpoint starts pointing at a different instance: a kept connection may then reach a node that became read-only,
or a dead socket. Reporting `database_read_only` or an error for a request that a fresh connection would serve is
confusing in a drill. Also, `database_role` (shown on the dashboard and used by `40-validate.sh`) must not come from a
stale connection.

## Decision

In `PostgresTransactionRepository` (`src/backend/dr_demo/adapters/outbound/postgres_repository.py`):

- Every failure discards the connection (rollback and close, errors ignored).
- If the failed connection had been **reused**, the operation is retried **once** on a fresh connection (new DNS
  resolution, TLS handshake and IAM token) after a read-only rejection (SQLSTATE `25006` or "read-only transaction") or a
  connection or resource error (SQLSTATE classes `08`, `53`, `57P`, and non-database socket or protocol errors).
- No retry for authentication errors (`28*`) and statement timeout (`57014`): both answer `503 database_unavailable`. Any
  other database error answers `500 internal_error` without details.
- A write without `Idempotency-Key` is not replayed once its `COMMIT` may have been sent; a `503` is returned instead, so
  the row is not duplicated. Writes with a key are safe to replay because the key is unique.
- `database_role` always discards the current connection first and reads `pg_is_in_recovery()` on a fresh one.
- Timeouts: 3 s TCP connect, 5 s server `statement_timeout`, 8 s socket backstop (3 + 5 + 8 seconds fit inside the 20 s function timeout). A wait for the account row lock that hits the statement timeout answers `503 database_busy` with `Retry-After`, without retry.

## Consequences

- The first request after a promotion is slower (one extra connection) but normally succeeds instead of failing.
- `/stats` and `/health?deep=true` open a new connection each time, a small extra cost per call.
- A read-only answer from a freshly opened connection is real (the cluster is a reader) and is reported as `503
  database_read_only` with `Retry-After: 5`.
- Replaying an `Idempotency-Key` with a different payload returns the original record; the API does not report a conflict.
- The retry logic was tested with fake connections. In the planned switchovers of the 2026-10-14 drill writes were unavailable for about 35 to 40 seconds each and the ledger stayed consistent; the single retry itself was not observed separately.

## Alternatives considered

A new connection on every invocation (simple, but slower and more connections); a connection pool library (extra
dependency for a function that handles one request at a time); retrying more than once (hides a real outage and extends
latency past the function timeout).
