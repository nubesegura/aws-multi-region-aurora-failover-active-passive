-- Schema bootstrap of the DR demo database (requirements 2.1 and 1.5). Idempotent: safe to run any number of times.
--
-- Executed once against the PRIMARY cluster, as the master user, by scripts/init-schema.sh through the RDS Data API
-- (the Data API is enabled only for that run). The objects replicate to the secondary cluster.
-- It can also be run with psql from an in-VPC session (fallback): the "-- split" lines are plain comments there.
--
-- Statements are separated by a line that contains exactly "-- split" (the Data API runs one statement per call).
-- The SQL used by the application is in src/backend/dr_demo/adapters/outbound/postgres_repository.py; keep both in sync.
--
-- Model: one bank account. "transactions" is its append-only ledger (deposits and withdrawals, each with the balance it
-- produced); "account" holds the single balance row that the writer locks (SELECT ... FOR UPDATE) for every movement.
-- Nothing was deployed before this model (assumption A1), so there is no migration from the old synthetic table.
--
-- Roles (security design 3.3): app_writer = apply a movement (INSERT on the ledger, SELECT and UPDATE on the account);
-- app_reader = list, stats, account, consistency and deep health (SELECT only). Neither can DELETE, UPDATE the ledger
-- or run DDL. Both authenticate with IAM (rds_iam), own nothing and are not superusers.
-- The user names must match DbWriterUser and DbReaderUser in parameters/prod.json (init-schema.sh checks it).

CREATE TABLE IF NOT EXISTS transactions (
    seq              bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    id               uuid PRIMARY KEY,
    idempotency_key  text UNIQUE CHECK (char_length(idempotency_key) BETWEEN 1 AND 64),
    type             text NOT NULL CHECK (type IN ('deposit', 'withdrawal')),
    amount           numeric(12, 2) NOT NULL CHECK (amount > 0),
    balance_after    numeric(12, 2) NOT NULL CHECK (balance_after >= 0),
    processed_region text NOT NULL,
    processed_at     timestamptz NOT NULL
);
-- split
CREATE TABLE IF NOT EXISTS account (
    id             smallint PRIMARY KEY CHECK (id = 1),
    balance        numeric(12, 2) NOT NULL CHECK (balance >= 0),
    movement_count bigint NOT NULL CHECK (movement_count >= 0)
);
-- split
-- Opens the account at balance 0. Repeating the bootstrap never resets an existing balance.
INSERT INTO account (id, balance, movement_count) VALUES (1, 0, 0) ON CONFLICT DO NOTHING;
-- split
-- Matches "ORDER BY processed_at DESC, id DESC LIMIT n" (latest transactions and stats).
CREATE INDEX IF NOT EXISTS idx_transactions_processed_at ON transactions (processed_at DESC, id DESC);
-- split
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_writer') THEN
        CREATE ROLE app_writer WITH LOGIN;
    END IF;
END
$$;
-- split
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_reader') THEN
        CREATE ROLE app_reader WITH LOGIN;
    END IF;
END
$$;
-- split
GRANT rds_iam TO app_writer;
-- split
GRANT rds_iam TO app_reader;
-- split
GRANT USAGE ON SCHEMA public TO app_writer, app_reader;
-- split
REVOKE ALL ON TABLE transactions, account FROM app_writer, app_reader;
-- split
GRANT SELECT, INSERT ON TABLE transactions TO app_writer;
-- split
GRANT SELECT, UPDATE ON TABLE account TO app_writer;
-- split
GRANT SELECT ON TABLE transactions, account TO app_reader;
