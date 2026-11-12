# ADR 0014: The ledger serializes movements with a row lock on the single account row

- Status: accepted
- Date: 2026-10-14

## Context

The demo is a bank with one account. A deposit or a withdrawal must update the balance and append the movement to the ledger
together, the balance must never be negative, and two movements executed at the same time must give the same balance as if
they had run one after the other. Several Lambda execution environments can write at once.

## Decision

`create-transaction` (database user `app_writer`) runs one database transaction per movement
(`src/backend/dr_demo/adapters/outbound/postgres_repository.py`, SQL kept in sync with `scripts/sql/schema.sql`):

1. `SELECT balance FROM account WHERE id = 1 FOR UPDATE` locks the single account row. Other writers wait.
2. When the request has an `Idempotency-Key` that is already stored, the stored movement is returned (`200`, its original
   `balance_after`) and nothing changes.
3. The domain computes the next balance; a withdrawal above the balance raises `InsufficientFunds` and the transaction is
   rolled back (`422 insufficient_funds`).
4. `INSERT` into `transactions` with `ON CONFLICT (idempotency_key) DO NOTHING`; if a concurrent insert with the same key won,
   the stored movement is returned instead.
5. `UPDATE account` with the new balance and movement count, then `COMMIT`; the `201` is sent only after the commit.

Table constraints back the rule: `balance >= 0`, `amount > 0`, `type` in (`deposit`, `withdrawal`), unique `idempotency_key`,
and a single `account` row (`id = 1`). The ledger carries a `seq` identity column that gives the replay order of
`GET /consistency`. A wait for the lock that reaches the 5 s `statement_timeout` answers `503 database_busy` with
`Retry-After`, stores nothing, and the banking app retries it with the same key.

## Consequences

- Movements are strictly serialized. With one account and a demo load below the stage throttle (20 requests per second) this is
  not a bottleneck; it would be for a real system with many accounts, where the lock would be per account and still short.
- The final balance equals deposits minus withdrawals, which `GET /consistency` checks by replaying the ledger.
- Only the writer database holds the lock; in the standby region the same code answers `503 database_read_only`.
- The idempotency rule works across regions only after replication has caught up. A planned switchover waits for it; an
  unplanned failover does not ([ADR 0015](0015-planned-switchover-only.md)).

## Alternatives considered

Optimistic concurrency with a version column and retries (more code and retry paths for no gain at this load); serializable
isolation for the whole transaction (more aborts to handle); computing the balance from the ledger on each read (slow and
needs a lock anyway); an advisory lock (not tied to the row it protects).
