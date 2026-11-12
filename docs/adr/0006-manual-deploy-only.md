# ADR 0006: Deploy and destroy are manual workflows only

- Status: accepted
- Date: 2026-10-04

## Context

The stack costs about USD 13 per day while it runs (unverified estimate), mostly two Aurora instances. A push to `main`
that deployed by itself would start that meter, and a mistaken automatic deploy could also disturb a drill.

## Decision

- `deploy.yml`, `destroy.yml`, `simulate-outage.yml` and `failover.yml` run only on `workflow_dispatch`. `ci.yml` runs on every
  push and pull request, needs no AWS access and never deploys. The four workflows that touch AWS share the concurrency group
  `drdemo-prod-infrastructure` (no two run at the same time; GitHub keeps one pending run per group, so a queued run can be
  replaced by a newer one: check the run list after dispatching).
- All use the GitHub Environment `prod`. Authentication is GitHub OIDC with the organization role (secret `AWS_ROLE_ARN`,
  [ADR 0016](0016-organization-github-role-and-account-baseline.md)); no long-lived keys exist. The drill workflows refuse to
  run unless dispatched from `main`, need a typed confirmation and verify the AWS account. There are no required reviewers on
  the Environment (single-user organization): the typed confirmations and the branch check are the controls.
- The first step of each workflow fails with a clear message when a required variable or secret is empty.
- `destroy.yml` needs two typed confirmations (`destroy-aws-multi-region-aurora-failover-active-passive` and `disable-deletion-protection`).
- `deploy`, `destroy` and `failover` split their work into three groups with a fresh OIDC login before each, because the deploy role
  session lasts one hour while creating or deleting Aurora can take longer.
- Actions are pinned by commit SHA; concurrency is serialized with one group for all infrastructure workflows.

## Consequences

- Nobody gets a deployment by merging; the owner starts it deliberately and approves the Environment.
- The drill (fence, planned switchover, weight change) is also available as two workflows, `simulate-outage` and `failover`
  ([ADR 0015](0015-planned-switchover-only.md)). Unplanned failover (`FailoverGlobalCluster`, data loss) is never run from a
  workflow: it stays a manual runbook step with the owner's credentials.
- The global bootstrap stack (CloudFormation execution role and the unused dedicated deploy role) is applied by hand by the owner
  with a change set and is never touched by the deploy or destroy workflows; the owner deletes it by hand at the end.

## Alternatives considered

Deploy on push to `main` (cost and drill risk); a scheduled destroy (surprising and destructive); long-lived access keys
in secrets (rejected in favour of OIDC).
