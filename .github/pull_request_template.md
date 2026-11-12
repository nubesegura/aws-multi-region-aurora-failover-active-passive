## What changes and why

<!-- One or two sentences. Link the spec or task (for example the issue or task number). -->

## How it was checked

- [ ] The same commands as CI ran locally from the repository root (ruff, ruff format --check, mypy --strict, lint-imports, pytest with coverage >= 85 %, node --test, cfn-lint, sam validate --lint, checkov, shellcheck)
- [ ] Scripts changed: `--dry-run` output reviewed
- [ ] Infrastructure changed: nothing was deployed from this branch; the cost impact is noted below

## Safety

- [ ] No secrets, account ids, e-mail addresses or tokens in the diff (use variables, secrets and parameters)
- [ ] Data-security review of the diff done before pushing
- [ ] The DR runbook and the destroy procedure still match the scripts (no new leftover resources)

## Cost impact

<!-- Always-on resources added or removed, instance sizes, extra regions. "None" is a valid answer. -->
