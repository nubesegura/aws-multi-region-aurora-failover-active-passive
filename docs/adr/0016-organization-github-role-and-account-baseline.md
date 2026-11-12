# ADR 0016: Reuse the organization GitHub role; account-wide resources live in the account baseline repository

- Status: accepted (amendment 4 of the banking-ledger spec)
- Date: 2026-10-14

## Context

The first design created, inside this project, the GitHub OIDC provider, a dedicated deploy role and the API Gateway CloudWatch
Logs role with its account setting. Those are account-wide singletons: an account can hold one OIDC provider and each region
one API Gateway log role, so a project cannot own them without breaking the next project or failing when they already exist. At
the first deployment two things also happened: the GitHub trust of the dedicated role used the old subject format
(`repo:<owner>/<repo>:environment:prod`) while the organization issues the immutable format (owner and repository ids), and the
bootstrap template grew past the 51,200 byte inline limit of CloudFormation.

## Decision

- **Account baseline.** The GitHub OIDC provider and the API Gateway CloudWatch Logs role and account setting of each data region
  come from the repository `nubesegura-accounts-baseline` (`templates/iam.yaml`, stacks `stk-<region code>-baseline-iam-prod`).
  This project creates none of them. `scripts/deploy.sh` only checks, in the `api` step, that the API Gateway account log role
  is set in both data regions and stops with a message naming the baseline otherwise.
- **GitHub role.** The workflows assume the organization role `github-actions-terraform-prod-role`, stored as the secret
  `AWS_ROLE_ARN` of the `prod` Environment. The dedicated deploy role of `templates/bootstrap.yaml` still exists
  (`CreateDeployRole=true`, role `iamr-useast1-drdemo-deploy-prod`) but is not used; its trust is the old subject format.
- **Execution role.** CloudFormation still runs through the project's own execution role
  `iamr-useast1-drdemo-cfnexec-prod` (output `CfnExecRoleArn`), which `deploy.sh` passes with `--role-arn`.
- **Global bootstrap.** The `bootstrap` stack of the global region (execution role, dedicated deploy role, artifact bucket) is applied
  by hand by the owner with a change set (it requires `ExistingOidcProviderArn` with `CreateDeployRole=true`) and is never touched by
  the deploy or destroy workflows. The bootstrap stacks of the two data regions (artifact bucket only) are managed by the deploy
  workflow. `bootstrap.yaml` stays under the 51,200 byte inline limit; `deploy.sh` sends any template above it through the artifact
  bucket of the region.
- **Names.** IAM roles `iamr-*`, managed policies `iamp-*`, security groups `sgp-*` (the acronym table of the team naming standard).
- **Stack layers.** Account baseline (other repository) -> per data region bootstrap -> network -> database -> api; global: bootstrap,
  edge, dns. The network is its own stack (VPC, two private subnets, route tables), so a database change does not touch it.

## Consequences

- Creating and destroying the demo never touches account-wide resources. The destroy workflow leaves the baseline and the global
  bootstrap stack; the API Gateway account setting keeps pointing to the baseline role.
- A new account needs the baseline deployed first. An account that already has the OIDC provider and the organization role (this
  one) needs no change.
- The permissions of the organization role are managed outside this repository; the deploy workflow ran with it on 2026-10-14.
  The dedicated deploy role could be removed from the template later.
- IAM names changed (`role-*` to `iamr-*`, `pol-*` to `iamp-*`, security groups `sg-*` to `sgp-*`); everything deployed before
  amendment 4 was destroyed and redeployed.

## Alternatives considered

Keep the OIDC provider and the log role in the project with an `Existing...Arn` switch (works for one project, breaks for the
second); fix the trust of the dedicated role (a second role to maintain next to the organization one).
