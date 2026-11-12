"""Criteria 2.1 and 1.5: the schema bootstrap, checked without a database."""

import re
from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parents[4] / "scripts" / "sql" / "schema.sql"


def _statements() -> list[str]:
    """Split like scripts/init-schema.sh: a "-- split" line ends a statement, comment lines go."""
    statements: list[str] = []
    current: list[str] = []
    for line in SCHEMA.read_text(encoding="utf-8").splitlines():
        if line == "-- split":
            statements.append("\n".join(current).strip())
            current = []
        elif not line.lstrip().startswith("--"):
            current.append(line)
    statements.append("\n".join(current).strip())
    return [re.sub(r"\s+", " ", item) for item in statements if item]


@pytest.fixture(scope="module")
def statements() -> list[str]:
    return _statements()


def test_every_statement_is_separated_and_complete(statements: list[str]) -> None:
    assert len(statements) >= 10
    assert all(item.endswith(";") for item in statements)
    plain = [item for item in statements if not item.startswith("DO ")]
    assert all(item.count(";") == 1 for item in plain)


def test_ledger_columns_and_checks(statements: list[str]) -> None:
    ledger = next(item for item in statements if "CREATE TABLE IF NOT EXISTS transactions" in item)

    for fragment in (
        "seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE",
        "id uuid PRIMARY KEY",
        "idempotency_key text UNIQUE CHECK (char_length(idempotency_key) BETWEEN 1 AND 64)",
        "type text NOT NULL CHECK (type IN ('deposit', 'withdrawal'))",
        "amount numeric(12, 2) NOT NULL CHECK (amount > 0)",
        "balance_after numeric(12, 2) NOT NULL CHECK (balance_after >= 0)",
    ):
        assert fragment in ledger
    assert "currency" not in ledger
    assert "description" not in ledger


def test_account_is_a_single_row_with_a_non_negative_balance(statements: list[str]) -> None:
    account = next(item for item in statements if "CREATE TABLE IF NOT EXISTS account" in item)

    assert "CHECK (id = 1)" in account
    assert "balance numeric(12, 2) NOT NULL CHECK (balance >= 0)" in account


def test_opening_the_account_is_conflict_safe(statements: list[str]) -> None:
    insert = next(item for item in statements if item.startswith("INSERT INTO account"))

    assert "VALUES (1, 0, 0)" in insert
    assert insert.endswith("ON CONFLICT DO NOTHING;")


def test_every_create_is_repeatable(statements: list[str]) -> None:
    creates = [item for item in statements if item.startswith("CREATE ")]

    assert creates
    assert all("IF NOT EXISTS" in item for item in creates if not item.startswith("CREATE ROLE"))


def test_writer_cannot_delete_alter_or_ddl_and_reader_cannot_write(statements: list[str]) -> None:
    grants = [item for item in statements if item.startswith("GRANT") and " ON TABLE " in item]
    granted = {
        (match["role"], table.strip()): set(match["privileges"].replace(" ", "").split(","))
        for item in grants
        if (
            match := re.fullmatch(
                r"GRANT (?P<privileges>[A-Z, ]+) ON TABLE (?P<tables>[a-z_, ]+) TO (?P<role>\w+);",
                item,
            )
        )
        for table in match["tables"].split(",")
    }

    assert granted == {
        ("app_writer", "transactions"): {"SELECT", "INSERT"},
        ("app_writer", "account"): {"SELECT", "UPDATE"},
        ("app_reader", "transactions"): {"SELECT"},
        ("app_reader", "account"): {"SELECT"},
    }
    sql = " ".join(statements)
    assert "DELETE" not in sql.replace("ON DELETE", "")
    assert not re.search(r"\b(TRUNCATE|SUPERUSER|CREATEDB|CREATEROLE|ALL PRIVILEGES)\b", sql)
