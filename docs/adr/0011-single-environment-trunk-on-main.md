# ADR 0011: One environment (`prod`) and trunk-based development on `main`

- Status: accepted
- Date: 2026-10-04

## Context

The demo is small, short-lived and costs money while it runs. Maintaining a `dev` copy next to `prod` would double the
Aurora cost for no benefit, and the project has one owner.

## Decision

- There is a single environment, `prod` (`env-type=prod`, last segment of every resource name, GitHub Environment
  `prod`). Every template still has an `EnvType` parameter (allowed `dev` and `prod`, default `prod`) and
  `parameters/prod.json` is the only parameter file, so a second environment is possible later without restructuring. The scripts under `scripts/` fix the environment to
  `prod` (`ENV_TYPE` in `scripts/lib/common.sh`), so a `dev` copy would also need script changes.
- Trunk-based development: work happens on short-lived branches merged into `main`; `main` is the only deployment branch
  (set as the deployment branch rule of the Environment). No long-lived release or develop branches.
- The OIDC trust is pinned to `repo:<owner>/<repo>:environment:prod`.
- Because the environment is called `prod`, prod-tier controls apply, but several are relaxed on purpose for cost and
  recorded as accepted risks (single instance per cluster, one-day backups, 30-day logs, no WAF, default CloudFront certificate).

## Consequences

- There is no safe place to try a change: rehearse in a throwaway AWS account, and use `--dry-run` and the CI checks first.
- Everything created is easy to destroy, which is the real safety net ([cost-and-cleanup.md](../cost-and-cleanup.md)).
- The word "prod" does not mean production data or production service levels: the project is educational and uses synthetic data.

## Alternatives considered

A `dev` and `prod` pair (double cost); Gitflow (ceremony without a release train); deploying every branch (cost).
