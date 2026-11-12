# Backend (Lambda, Python)

Hexagonal Python code for the DR demo API. Python 3.11+ compatible; the Lambda runtime is the latest
one listed in the team standard (`python3.14`, arm64).

## Layout

```
src/backend/
  dr_demo/            domain/ application/ adapters/{inbound,outbound}/ config/   (the package)
  services/<fn>/handler.py   thin Lambda entrypoints: create_transaction, list_transactions, get_stats, get_health,
                             get_account, get_consistency
  tests/              unit/{domain,application}, adapters, entrypoints, fakes
  requirements.txt    runtime pins (pg8000 and its transitives; boto3 comes with the Lambda runtime)
  requirements-dev.txt  tools and test dependencies (never packaged)
  ruff.toml  pyproject.toml (pytest, coverage, mypy)  .importlinter
  certs/              RDS CA bundle, created by scripts/fetch-rds-ca.sh (git-ignored, *.pem)
```

## Packaging with SAM

`dr_demo/` and `services/` are siblings, so a single `CodeUri` ships both and the handlers import the
package as `dr_demo.*` (the zip root is `/var/task`).

- `CodeUri: src/backend`
- `Handler: services.create_transaction.handler.handler` (same pattern for `list_transactions`,
  `get_stats`, `get_health`, `get_account`, `get_consistency`)
- `sam build` runs `pip install -r src/backend/requirements.txt` into the build folder and copies the
  whole `src/backend` tree (including `certs/`, and also `tests/`; harmless, but tests are not needed at run time).
- Run `scripts/fetch-rds-ca.sh` before `sam build` so `certs/global-bundle.pem` is packaged.
- Environment variables read at cold start (first invocation): `DB_HOST`, `DB_PORT` (default 5432),
  `DB_NAME`, `DB_USER`, `AWS_REGION` (set by Lambda), `ALLOWED_ORIGINS` (comma-separated list of the two site
  origins, set by the API template; the response echoes the request origin only when it is in the list), optional `LOG_LEVEL` (default INFO) and `DB_CA_BUNDLE_PATH`
  (default: `certs/global-bundle.pem` next to the package).

## Database connection behaviour

- One connection per execution environment, opened lazily with a fresh IAM token, TLS verified with the RDS CA,
  3 s TCP connect timeout, server-side `statement_timeout` of 5 s, 8 s socket backstop (budget inside the
  20 s function timeout: backstop, then one retry with connect and statement).
- Every failure discards the connection. If it had been reused, the call is retried once on a fresh one
  (read-only rejection, connection or resource errors: SQLSTATE `25006`, `08*`, `53*`, `57P*`). Auth (`28*`)
  and statement timeout (`57014`) give 503 `database_unavailable` without retry; any other database error gives
  500 `internal_error` without details. A deposit or withdrawal locks the single account row
  (`SELECT ... FOR UPDATE`); when the lock is not obtained within the statement timeout the answer is 503
  `database_busy` and nothing is stored. `database_role` always uses a fresh connection, so `/stats` and
  `/health?deep=true` never report a stale role after a switchover. A write without `Idempotency-Key` is not
  replayed once its COMMIT may have been sent (a 503 is returned instead).
- A missing CA bundle fails at cold start (`SettingsError` naming `scripts/fetch-rds-ca.sh`).

## Known limitation

Replaying an `Idempotency-Key` with a different payload returns the original record (200) and ignores the new
payload; the API does not report a conflict.

## Local checks (the exact CI commands, from the repository root)

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r src/backend/requirements-dev.txt
ruff check src/backend
ruff format --check src/backend
mypy --strict --config-file src/backend/pyproject.toml src/backend
PYTHONPATH=src/backend lint-imports --config src/backend/.importlinter
pytest -c src/backend/pyproject.toml src/backend/tests --cov=dr_demo \
  --cov-config=src/backend/pyproject.toml --cov-fail-under=85
shellcheck scripts/fetch-rds-ca.sh
```

`ruff` picks up `src/backend/ruff.toml` by directory, so running from the root is equivalent to running inside `src/backend`.
