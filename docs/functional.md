# Functional documentation

What the DR demo does, from the point of view of the people who use it. For how it is built see
[technical.md](technical.md); for the drill itself see [dr-runbook.md](dr-runbook.md).

## Purpose

Show, in a few hours and at a low cost, how a service survives the loss of a whole AWS region. The service is a tiny online bank
with one account: the data is replicated to a second region, the second region is promoted, traffic moves to it, and clients keep
using the same web address while no acknowledged movement is lost. It is an educational, non-production project. All data is
synthetic: amounts and balances of a demo account, no names, no account numbers, no free text. **Do not enter personal data.**

## Who uses it

| Role | What they do |
|---|---|
| Presenter (owner) | Deploys the demo, runs the drill (workflows `simulate-outage` and `failover`, or the scripts in `scripts/dr/`), watches the monitoring dashboard, destroys everything afterwards |
| Bank customer (the presenter in the demo) | Signs in to the online banking app as `bank-operator` and deposits and withdraws |
| API client | Calls the API directly (Postman, curl) with a Cognito ID token of a demo user |
| Viewer | Watches the monitoring dashboard to see which region answers and that the data survives |

## Capabilities

### Execute a deposit or a withdrawal

A client posts `{type, amount}` with `type` equal to `deposit` or `withdrawal`. The system applies the movement to the single
account and appends it to the ledger in one database transaction, and answers `201` only after the commit. The answer carries the
movement (id, type, amount, time in UTC with millisecond precision, **balance after**, the region that processed it), the resulting
balance, the serving region, the database role and the server-side processing time in milliseconds.

- Rules: `amount` is a positive number with at most 2 decimals and at most 9,999,999,999.99; unknown fields are rejected; a
  deposit that would push the balance above the maximum is rejected. Breaking a rule gives `400` listing every problem and stores
  nothing.
- No overdraft: a withdrawal above the current balance gives `422 insufficient_funds` with the current balance and stores nothing.
  The balance is never negative.
- Concurrency: two movements at the same time are applied one after the other, so the final balance equals deposits minus
  withdrawals ([ADR 0014](adr/0014-ledger-row-lock.md)). If the lock on the account cannot be obtained in time the answer is
  `503 database_busy` with `Retry-After`; nothing is stored.
- Retries: if the request carries an `Idempotency-Key` header (1 to 64 characters) that was already used, the system answers `200`
  with the original result (including the balance it produced) and does not apply the movement again, even if the first request was
  served by the other region (the data is replicated).
- Read-only database: while a region's database is still a read-only copy (before promotion) a write answers `503` with code
  `database_read_only` and `Retry-After`. Nothing is stored.
- Database unreachable: `503` with `database_unavailable`, without connection details. Any other unexpected database error answers
  `500` with `internal_error`, also without details.
- Around a switchover the service replaces its database connection and retries once by itself, so a request sent right after a
  promotion normally succeeds instead of failing ([ADR 0013](adr/0013-connection-discard-and-single-retry.md)).
- Limit of the retry rule: the same `Idempotency-Key` sent again with a **different** payload returns the original result (`200`); the
  new payload is ignored and no conflict is reported.
- Authentication: every route except `GET /health` needs a valid Cognito ID token; without one the answer is `401` and nothing is stored.
- Rate limit: 20 requests per second (burst 40) per region; beyond that the answer is `429`.

### Account, history and statistics

- `GET /account`: the current balance, the number of movements, the serving region and the database role, read from the local
  region database.
- `GET /transactions?limit=N`: the N newest movements (default 20, maximum 100), each with type, amount, balance after, processing
  region and time.
- `GET /stats`: total count, count per region, last processed time per region, the 10 latest movements, the region that answered
  (`served_by_region`), the database role it sees (`writer` or `reader`) and the generation time.
- `GET /health`: always `200` with the serving region, without touching the database and without a token. `GET /health?deep=true` also checks the database
  and reports its role; `503` when the database is unreachable.
- Every response carries the header `X-Served-By-Region`. A region reads from its own local database; there are no cross-region
  database calls.
- The account is created by the schema bootstrap at balance 0.00; repeating the bootstrap never changes the balance. There is no
  reset button: to start again from zero the demo is destroyed and deployed again.

### Consistency check

`GET /consistency` replays the whole ledger in order, with read access only, and answers: number of movements, total deposited, total
withdrawn, the replayed balance, the stored balance, whether they are equal, whether every movement's balance-after follows from the
previous one, the number of duplicated idempotency keys (must be 0), the count per region and `consistent`. When something differs it
reports `consistent: false` with the first offending movement. Above 50,000 movements it answers `truncated: true` and claims nothing.
This is the proof, after a switchover, that no movement was lost or duplicated.

### Online banking app

A static web page on its own address (its own CloudFront distribution and private bucket).

- The user signs in with a username and password (demo user `bank-operator`, created by the owner). The tokens are kept only in that browser
  tab (`sessionStorage`), refreshed before they expire, and a **Sign Out** button clears them. No credential is part of the published files.
  If the other region starts serving, the page signs in again against that region by itself (the Cognito replica must be active, see the failover workflow).
- It shows the current balance, an amount field, a selector with the two operations and an **Execute** button. The button is disabled
  until the answer arrives. An empty, zero, negative or more-than-2-decimals amount is refused on the page without calling the API.
- After each operation it shows the result (accepted, or rejected with the reason in plain words), the new balance, the region and
  backend that answered, the database role, the **round-trip time** measured in the browser and the **server time** returned by the API.
- **Transparent recovery.** One `Idempotency-Key` is generated per click and reused in every retry. On a network error, `429`, `500`,
  `502`, `503` or `504` the page retries by itself with a growing wait for at most 60 seconds, showing "recovering, attempt N"; a success
  after retries is shown as a normal result marked as recovered, with the number of attempts and the total time. `400` and `422` are not
  retried. If all retries fail the page shows the failure and the key of the operation and does not assume the balance changed.
- It lists the operations of the session with their times and shows the minimum, median and maximum round-trip time.
- **Verify my operations** compares the ids of the operations acknowledged in this session with the newest 100 movements of the ledger and
  names any that are missing (none is expected after a planned switchover).
- Keyboard usable, light and dark themes, usable at phone width, no inline code.

### Monitoring dashboard

A separate, read-only static page on its own address. It never posts a movement.

- The viewer signs in as `dashboard-viewer` (tokens kept only in that tab, like the banking app).
- Every 5 seconds the page shows: the serving region, the database role, the balance, movements per region (count, share, time of the last
  one), and a table of the latest 50 movements (time, type, amount, balance after, processing region, status).
- If the serving region or the database role changes between two polls the page shows a visible banner and adds an event (time, from, to)
  to a timeline.
- It shows its own poll round-trip times with minimum, median and maximum.
- If the API fails or times out the page shows an "Unavailable" banner with the time since the last successful update and keeps the last
  data on screen while it retries.
- **Verify** calls `GET /consistency` on demand (it is not polled) and shows the result with the serving region and time; an inconsistent
  ledger is shown in an error state.
- Labelled controls, polite announcements for screen readers, light and dark themes.

Both sites may call the API from the browser; the API accepts browser calls from these two addresses and from no other origin.

### One address, manual traffic switch

Clients always call the same domain (for example `api.example.com`). Two Route 53 weighted alias records (stack `dns` in `us-east-1`) point to the regional API of
each region. With weights 255 / 0 every request is answered by `us-east-2`; with 0 / 255 by `us-west-2`. The presenter changes the
weights with one script or one workflow run; no client change and no redeployment of either region is needed. New requests follow within
the record time to live (60 seconds) plus the caching of resolvers and connections. Why this is manual and not automatic failover:
[adr/0003-manual-weighted-routing.md](adr/0003-manual-weighted-routing.md).

### Disaster recovery drill

Two manual GitHub Actions workflows run the drill without deleting anything ([ADR 0015](adr/0015-planned-switchover-only.md)):

| Workflow | Inputs | What it does |
|---|---|---|
| `simulate-outage` | `action` (`status`, `fence`, `unfence`), `region`, `confirm` (the region name, required for `fence`) | Cuts the traffic to a region by setting reserved concurrency 0 on its six API functions, lifts it, or only reports it. The database and everything else keep running |
| `failover` | `direction` (`to-secondary`, `to-primary`), `confirm` (`failover`) | `to-secondary`: requires the primary fenced, activates the Cognito replica in `us-west-2`, planned switchover of the database, Route 53 weights 0 / 255, validation. `to-primary`: unfence the primary, switch the database back, deactivate the Cognito replica, weights 255 / 0, validation. The ledger consistency check and the probe deposit run only with a token (`scripts/dr/45-consistency.sh`) |

Each run writes a job summary (actor, time, seconds per step, writer region before and after) and ends red if a
step fails. The scripts in `scripts/dr/` do the same steps one by one from a workstation. Two paths exist:

| Path | When | Data loss | Reversible |
|---|---|---|---|
| Planned (switchover) | Both regions healthy: a drill (the workflows) | None | Yes, with `to-primary` or the mirror failback scripts |
| Unplanned (failover with `--allow-data-loss`) | A real regional outage (manual runbook step only) | Possibly the writes of the last moments (replication is asynchronous, about one second typically) | No: the old primary leaves the global cluster and must be rebuilt as a secondary |

The outage in a drill is **simulated**, not reproduced: the functions of the fenced region are throttled to zero concurrency
(reversible). See [dr-runbook.md](dr-runbook.md).

### Delivery, cost and removal

- CI runs on every push and pull request and never deploys.
- Deploy is a manual workflow; destroy is a manual workflow that needs two typed confirmations and lists what must be deleted by hand.
  Cost per hour, day and month and the cost drivers are in the [README](../README.md#cost).

## Recovery objectives

| Objective | Value | Note |
|---|---|---|
| RTO (time to serve again from the second region) | 15 minutes target, manual or workflow steps | Measured on 2026-11-11 by the automated drill (one run, planned path, Cognito era): write RTO 196.8 s to the secondary region and 82.7 s back to the primary; verdict PASS. A single run, not a guarantee. See [dr-test-report.md](dr-test-report.md). The 195 s figure of 2026-10-14 was the total workflow time (writes were unavailable 35 to 40 s) and is not comparable with these write RTO values |
| RPO, planned switchover | zero | Aurora waits for the secondary to catch up. Measured on 2026-11-11: 0 acknowledged writes lost (504 movements stored, 504 expected); see [dr-test-report.md](dr-test-report.md) |
| RPO, unplanned failover | about 1 second typically, not zero | Asynchronous replication; never exercised in this project. During the 2026-11-11 drill the replication lag peaked at 933 ms (`AuroraGlobalDBReplicationLag`) and 1531 ms (`AuroraGlobalDBRPOLag`); that bounds what could have been lost at that moment, it is not a measured RPO |

## Out of scope

Automatic DNS failover and health checks, WAF (accepted risk, [ADR 0017](adr/0017-cognito-multi-region-replication.md)), self-registration, federated or multi-factor sign-in, several accounts or customers, a custom domain for
the two sites, several environments, a reader instance inside a region (a possible later feature), a us-east-1 outage, load testing,
the unplanned failover as a workflow.

## Requirements traceability (short)

| Area | Documents |
|---|---|
| Movements, account, statistics, health | this file; API shapes in [technical.md](technical.md#api); [adr/0014](adr/0014-ledger-row-lock.md) |
| Consistency check | this file; [technical.md](technical.md#api) |
| Banking app and monitoring dashboard | this file; [technical.md](technical.md#sites) |
| Single entry point and traffic switch | this file; [adr/0003](adr/0003-manual-weighted-routing.md); [dr-runbook.md](dr-runbook.md) |
| Outage and failover workflows | this file; [adr/0008](adr/0008-outage-simulation-method.md); [adr/0015](adr/0015-planned-switchover-only.md); [dr-runbook.md](dr-runbook.md) |
| Aurora Global Database | [technical.md](technical.md#data-model); [adr/0002](adr/0002-aurora-postgresql-and-instance-class.md) |
| Sign-in with Cognito, replica activation | [adr/0017](adr/0017-cognito-multi-region-replication.md); [adr/0018](adr/0018-region-realignment.md); [technical.md](technical.md#api) |
| Diagrams and resource inventory | [architecture.md](architecture.md) |
| Runbook, failback, Postman | [dr-runbook.md](dr-runbook.md); [README](../README.md#postman) |
| Delivery, cost, destruction | [README](../README.md); [cost-and-cleanup.md](cost-and-cleanup.md) |
