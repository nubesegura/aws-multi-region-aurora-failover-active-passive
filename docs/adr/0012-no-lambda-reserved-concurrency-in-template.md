# ADR 0012: No Lambda reserved concurrency in the template

- Status: accepted (accepted deviation from the team baseline, with mitigations)
- Date: 2026-10-04

## Context

The team baseline asks for reserved concurrency on every function, as a ceiling on how many database connections a request
storm can open. This project already uses reserved concurrency for something else: the drill sets it to `0` from the CLI to
simulate the loss of a region and to fence the old primary after an unplanned failover
([ADR 0008](0008-outage-simulation-method.md)). A value in the template would be reverted by the next stack update
(or would make the stack fight the fence), and the stacks must not own a setting that the drill changes at run time.

## Decision

`templates/api.yaml` sets no `ReservedConcurrentExecutions`; the checkov findings for it are skipped with a comment. The drill
scripts own the setting. Instead, the connection load is bounded by other controls that the template does set:

- Stage throttling of 20 requests per second with burst 40 (`ThrottleRate`, `ThrottleBurst`). Since spec 002 there is no usage plan and no
  daily quota; the caller must also hold a valid Cognito ID token ([ADR 0017](0017-cognito-multi-region-replication.md)).
- A function timeout of 20 seconds (one attempt, one retry and the answer must fit, [ADR 0013](0013-connection-discard-and-single-retry.md)), so at most about `ThrottleRate x 20` executions run at once, and one database
  connection per execution environment ([ADR 0013](0013-connection-discard-and-single-retry.md)).
- A 3 second connect timeout and a 5 second server-side `statement_timeout` in the adapter.
- Alarms: database `DatabaseConnections` above 100 and `CPUUtilization` above 80 percent, Lambda throttles, Lambda errors
  and API 5xx, all to the regional SNS topic.
- A database-side `max_connections` limit was not added: it was not verified offline, and a low value could lock out
  administrative access.

## Consequences

- A burst above the account's unreserved Lambda concurrency is not capped per function. A brand-new account can have a low
  account concurrency quota; check it before the first deploy.
- The throttle is per stage and per region, not a hard guarantee against a determined holder of a valid token
  ([ADR 0017](0017-cognito-multi-region-replication.md); the earlier API key design is [ADR 0004](0004-api-key-only-accepted-risk.md), superseded).
- The fence must be removed by the drill scripts. A fence left in place shows up as the `lambda-throttles` alarm.
- None of this has been load tested.

## Alternatives considered

Reserved concurrency in the template (conflicts with the fence); setting it from the deploy script (the drill would still have to override it); provisioned concurrency (cost); a lower `max_connections`
(unverified, risks locking out administration).
