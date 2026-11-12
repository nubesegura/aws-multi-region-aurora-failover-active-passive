# DR drill runner

Local Python runner that carries out the disaster-recovery drill of the demo against the deployed prod environment and
measures RTO and RPO. It signs in as a Cognito user, writes through the public API while it dispatches the failover
workflows with `gh`, and reads the replication lag from CloudWatch. Run all commands from this folder
(`scripts/e2e`).

## Prerequisites

- The demo is deployed in prod (run the `deploy` workflow first).
- Python 3.12 or newer and `pip install -r requirements-dev.txt`. CI uses Python 3.12; the local checks of the
  runner were also run on Python 3.14.
- `gh` logged in with access to the repository (`GITHUB_REPOSITORY`); for the `nubesegura` organization use
  `GH_TOKEN="$GITHUB_TOKEN_NUBESEGURA"` and never print it.
- The AWS CLI profile named in `AWS_PROFILE`, with read access to the prod account.
- The Cognito user `e2e-runner`, created with `provision_users.py --only e2e-runner`
  (see `.github/workflows-local/README.md`).
- Behind TLS inspection, set `AWS_CA_BUNDLE` to the PEM file of the extra certificates.

## Configure `.env`

```bash
cp .env.example .env
```

Fill every value with your own; `.env` is git-ignored, never commit it. `E2E_USERNAME` is the Cognito user name
`e2e-runner`, not an e-mail address. `API_DOMAIN_NAME` is the host name only (no scheme). Optional keys:
`AWS_CA_BUNDLE`, `PROBE_RATE_SECONDS` (default 1) and `PHASE_STEADY_SECONDS` (default 60).

## Commands

```bash
python -m dr_drill check                     # read-only preconditions; nothing is written
python -m dr_drill probe-smoke               # 60 s of probes in the primary region, no workflow
python -m dr_drill run                       # the full drill (asks for a typed confirmation)
python -m dr_drill run --authorized          # same, skipping the confirmation (owner already authorized)
python -m dr_drill formal-report --run <run folder> [--run <run folder>]
```

Suggested order: `check`, optionally `probe-smoke`, then `run`, then `formal-report`.
`python -m dr_drill <command> --help` lists the options.

### `check`

Runs eight read-only preconditions and writes nothing: the AWS identity (printed as the last 4 digits), the `edge`,
`dns` and `api` stacks complete, `GET /health` answers 200, no fence (no reserved concurrency on the functions), the
Aurora writer in the primary region, no queued or running drill workflow, the `gh` login and the Cognito sign-in of the
drill user. Any failure stops before a write is possible (exit 3).

### `probe-smoke`

Runs the probes alone for 60 s against the primary region, with no workflow and no failover. It reads `GET /account`
before the probes start and `GET /account` plus `GET /consistency` after they stop, then writes the same three files as
`run` (`run.log`, `timeline.json`, `report.md`) and prints the same summary.

- Net effect on the ledger: the write probe alternates a deposit and a withdrawal of 0.01, so the writes are net zero.
  Because it stops by time, an odd number of acknowledged movements leaves the balance 0.01 higher; the summary prints
  the expected net effect (`+0.01` or `0.00`).
- Abandoned and pending movements are counted separately; each of them may have added or removed 0.01, and the ledger
  check allows for that band.
- The smoke fails (exit 1) on any probe error, on an answer from a region other than the primary, on a ledger problem,
  or when the baseline or final read cannot be taken (the ledger check is then skipped and said so).

### `run`

The full drill; it writes in prod (see below). The duration is an estimate: the only measured run took about 13 minutes
of drill (baseline to the ledger check) plus the 120 s lag wait, close to 14.5 minutes in total (see the phase table of
`docs/dr-test-report.md`). Failover durations vary from run to run, so plan for about 15 minutes.

- Replication lag wait (120 s settle): after a clean drill, `run` waits 120 s before reading the replication lag,
  because CloudWatch publishes the last datapoints 1 to 3 minutes late. The wait is skipped when the drill was aborted
  or interrupted; the timeline then notes that the last minutes of lag may be missing. A Ctrl-C during the wait, the lag
  read or the file writing prints `settle interrupted, lag may be incomplete` (wait only), still writes the three files
  and exits 130.
- Write probe errors: transport errors, 408, 429 and 5xx are retried with the same idempotency key for up to 600 s. A
  first 401 is retried once (token refresh); 400, 403, 404, 409, 422, a second 401 and any other non-transient status
  abandon the movement at once and count as a probe error. Abandoned movements and the one in flight at stop form an
  uncertainty band for the ledger check.

### `formal-report`

Builds the formal EN/ES report from the `timeline.json` of one or two run folders and writes `docs/dr-test-report.md`
and `docs-es/dr-test-report.md` (versioned; review them before committing). It reads local files only. The probe
cadences in the text come from the constants of the probes, not from the run: a run made with another
`PROBE_RATE_SECONDS` is described with the default values. The output guard is built with the secrets of `.env` when
the settings load; otherwise the command warns on stderr and only the shape checks apply.

### Exit codes

0 clean, 1 aborted (stop condition, refused confirmation or failed smoke), 2 usage or settings, 3 preconditions failed,
4 unexpected internal error (class name only), 130 interrupted (Ctrl-C; the probes are stopped and the three files are
still written). Lines that mention the configured user name are refused by the output guard.

## Outputs

Each `probe-smoke` and `run` writes a folder `reports/e2e/<UTC timestamp>/` (git-ignored) with:

- `run.log`: one line per step, `UTC time | step | action | result | region`.
- `timeline.json`: every event, the single data source of the metrics and of the formal report.
- `report.md`: verdict, RTO and RPO per direction, workflow durations and replication lag.

The console shows the same lines and a final summary.

## What the drill writes in prod

- Net-zero ledger entries through the public API: the write probe alternates a deposit and a withdrawal of 0.01.
  An odd number of acknowledged movements leaves the balance 0.01 higher.
- Three workflow dispatches, in order: `simulate-outage` (fence of the primary), `failover` to-secondary and `failover`
  to-primary (which lifts the fence).

Nothing else is written: no deploy, no destroy, no manual unfence, no settings change.

## Stop rules

The drill stops at the first problem: a workflow that does not conclude `success`, an answer from an unexpected region
or database role, a broken balance chain, an inconsistent ledger, an unreadable baseline, no conforming answer or a
permanent probe error in a steady phase. On a stop nothing else is dispatched: no retry, no `--force`, no unfence. The
state is printed and the owner follows `docs/dr-runbook.md`. The verdicts are `PASS`, `FAIL`, `INCONCLUSIVE` (the
ledger could not prove the RPO) and `ABORTED` (Ctrl-C).

## Output guard

Every console line, log line and stored document passes through `dr_drill.redact.OutputGuard`. Its primary defence is
exact matching: every secret the runner reads (settings values, tokens from Cognito) must be passed to
`guard.register`, and a registered value is refused wherever it appears. The shape heuristics (JWT, hexadecimal, long
opaque runs, account ids, ARNs, e-mail) are a second line only: a token made of short lowercase segments joined by
hyphens looks like a resource name and passes, as does a secret split by a space. Any new module that reads a secret
(SSM, Secrets Manager) must register it.

The account-id rule is strict: any run of 12 digits not adjacent to another digit is refused, whatever surrounds it
(dots included). Numbers that are serialized are rounded at the source (milliseconds to 3 decimals), so no float
carries such a run. The only exception is a standalone, well-formed UUID v4 (the idempotency keys); a deliberately
built v4 whose last group is an account id would pass, an accepted residual.

## Cost

The added cost of a run (API calls, Lambda, GitHub Actions minutes, about 900 ledger rows) stays below 1 USD per run.
The running demo already costs about 13 USD per day (estimate from `docs/dr-runbook.md`, see
`docs/cost-and-cleanup.md`).

## Limitations

- The RPO measured is the one of a planned switchover (the drill does not promote the secondary without switchover), so
  it is 0 by construction; the replication lag is reported as the bound. The managed RPO and an unplanned-failover
  variant are future work.
- The drill measures the demo as deployed, with one write per second; it is not a load test.
- Only one run at a time; the report can combine up to two runs.
- A recorded demo with two browser sessions is not part of this runner.

## Development checks (from the repository root)

```bash
ruff check scripts/e2e
ruff format --check scripts/e2e
mypy --strict --config-file scripts/e2e/pyproject.toml scripts/e2e/dr_drill
python -m pytest -q scripts/e2e/tests
```
