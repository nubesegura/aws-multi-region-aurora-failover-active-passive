# ADR 0001: AWS SAM (CloudFormation) as the only infrastructure tool

- Status: accepted
- Date: 2026-10-04

## Context

The project needs infrastructure for two regions plus a global edge, and Lambda code that must be built and packaged
(`pg8000` and the RDS CA bundle). The team default for new projects is to propose Terraform first; the owner asked for
AWS SAM. One repository uses one infrastructure tool.

## Decision

Use AWS SAM / CloudFormation, with **one template per stack** in `templates/`: `bootstrap`, `network`, `edge`, `database`,
`api`, `dns` (the network is its own stack, deployed before the database; account-wide resources are not part of this
project, see [ADR 0016](0016-organization-github-role-and-account-baseline.md)). Only `api.yaml` uses the SAM transform (`AWS::Serverless::Function`, `AWS::Serverless::Api`); the others are plain
CloudFormation. `sam build` and `sam deploy` deploy the api stack; `aws cloudformation deploy` deploys the rest. The only
environment file is `parameters/prod.json`. Stacks exchange values **as parameters**, read from stack outputs by
`scripts/deploy.sh`; there are no CloudFormation exports or imports, so each stack can be deleted on its own.

## Consequences

- SAM builds and uploads the Lambda package; no separate packaging pipeline is needed.
- The same `database.yaml` and `api.yaml` deploy to both regions; only parameters differ.
- CloudFormation has no state to import and no plan step beyond change sets; drift after a database role swap is handled
  by [ADR 0005](0005-database-stacks-not-redeployed-during-drills.md).
- A stuck stack is investigated with `describe-stack-events`; the scripts never retry or delete it.
- Validation is offline: `cfn-lint`, `sam validate --lint`, `checkov`. Real behaviour is only known after a deployment.

## Alternatives considered

- Terraform (with or without Terragrunt): the team default, rejected by the owner's explicit choice.
- Raw CloudFormation without SAM: would need a separate packaging step for the Lambda code.
- One large template: would couple regions and make partial deletion and independent updates impossible.
