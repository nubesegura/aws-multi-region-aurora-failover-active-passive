# Local Workflows Runner Suite (`.github/workflows-local/`)

Local Bash and Python orchestration suite that mirrors the GitHub Actions workflows (`.github/workflows/deploy.yml`, `destroy.yml`, `simulate-outage.yml`, and `failover.yml`) for execution directly from an administrator workstation using a local AWS CLI profile (`AWS_PROFILE`), without consuming GitHub Actions runtime minutes.

## Prerequisites

- **OS / Shell:** Git Bash on Windows (MSYS2 / Bash 4+) or Linux/macOS Bash 4+.
- **CLI Tools:** `aws` (AWS CLI v2), `sam` (AWS SAM CLI), `jq`, `curl`, `git`, `python` (Python 3.12+ with `boto3`).
- **AWS Credentials:** A configured local AWS CLI profile (default `tf-local-admin-prod`) with permissions to deploy/manage the stacks and administer the Cognito User Pool.

## Directory Layout

- `lib/common.sh` — Shared logging, carriage-return stripping (`capture`), path helpers (`winpath`), safe `KEY=VALUE` env loader (`load_env`), concurrency locks (`.locks/`), markdown run summaries (`.runs/`), interactive confirmations (`confirm_typed`), and AWS STS preflight checks.
- `config.example.env` — Versioned configuration template with safe placeholders.
- `prod.env` — Local, git-ignored environment configuration file loaded automatically by all local workflow scripts.
- `deploy.sh` — Local equivalent of `.github/workflows/deploy.yml`. Orchestrates `scripts/deploy.sh` across all stacks (`bootstrap,edge,cognito,network,db-primary,db-secondary,schema,api,dns,dashboard`).
- `destroy.sh` — Local equivalent of `.github/workflows/destroy.yml`. Orchestrates `scripts/destroy.sh` in safe teardown order (`dns`, `api`, `cognito` with deletion protection lifted and replica removed before primary, global cluster detach, `database`/`network`/`edge`, `bootstrap`, and leftover checks).
- `simulate-outage.sh` — Local equivalent of `.github/workflows/simulate-outage.yml`. Runs `status`, `fence`, or `unfence` against a data region (`AWS_REGION_PRIMARY` or `AWS_REGION_SECONDARY`).
- `failover.sh` — Local equivalent of `.github/workflows/failover.yml`. Runs planned switchover `to-secondary` (activating the Cognito replica in `us-west-2`, promoting Aurora, switching Route 53 weights, validating and checking ledger consistency) and failback `to-primary` (unfence primary, promoting Aurora back, deactivating the Cognito replica in `us-west-2`, switching Route 53 weights, validating and checking consistency).
- `provision-users/` — Standalone folder (not part of the deploy/destroy flow) with `provision_users.py`, its tests, `.env.example` (versioned template) and `.env` (git-ignored). Idempotent local CLI tool that provisions confirmed demo users (`bank-operator` and `dashboard-viewer`, plus the optional `e2e-runner` for the automated DR drill when `DEMO_E2E_EMAIL` is set; `--only e2e-runner` touches just that user) in the primary `us-east-2` User Pool and polls `us-west-2` until Multi-Region Replication completes.
- `.locks/` — Ephemeral lock directories preventing concurrent runs (git-ignored).
- `.runs/` — Timestamped Markdown run summaries and step timings (git-ignored).

## Setup

1. Copy the example configuration to `prod.env` (if not already present) and fill in your domain, hosted zone ID, and optional alert email:
   ```bash
   cp .github/workflows-local/config.example.env .github/workflows-local/prod.env
   ```
2. Verify your AWS CLI profile is active:
   ```bash
   aws sts get-caller-identity --profile tf-local-admin-prod
   ```

## Usage Examples

### 1. Deploy the Full Stack

```bash
# Dry-run (no AWS API calls):
bash .github/workflows-local/deploy.sh --dry-run

# Full live deployment:
bash .github/workflows-local/deploy.sh --yes

# Deploy specific steps only (e.g., cognito and api):
bash .github/workflows-local/deploy.sh --steps cognito,api --yes
```

### 2. Provision Demo Users (`bank-operator` and `dashboard-viewer`)

Self-contained in `.github/workflows-local/provision-users/`. It has no built-in defaults: all settings
(profile, regions, environment tier, timeout, user e-mails) come from `provision-users/.env`.

```bash
cp .github/workflows-local/provision-users/.env.example .github/workflows-local/provision-users/.env
# edit .env with your own values, including the two demo passwords (git-ignored; they can also come from the shell)
python .github/workflows-local/provision-users/provision_users.py
```

### 3. Simulate a Regional Outage (Fence / Unfence)

```bash
# Check current fence status on primary region:
bash .github/workflows-local/simulate-outage.sh --action status --region us-east-2

# Fence the primary region (simulates regional compute outage):
bash .github/workflows-local/simulate-outage.sh --action fence --region us-east-2 --confirm us-east-2

# Lift the fence on the primary region:
bash .github/workflows-local/simulate-outage.sh --action unfence --region us-east-2
```

### 4. Execute Planned Failover and Switchback

```bash
# Failover to standby region (us-west-2) after fencing primary:
bash .github/workflows-local/failover.sh --direction to-secondary --confirm failover

# Failback to primary region (us-east-2):
bash .github/workflows-local/failover.sh --direction to-primary --confirm failover
```

### 5. Teardown the Environment

Teardown follows strict dependency order:
1. Disable deletion protection on Aurora clusters and global cluster.
2. Delete Route 53 DNS stack (`dns`).
3. Delete regional API Gateway stacks (`api` in `us-east-2` and `us-west-2`) before Cognito.
4. Disable `DeletionProtection` on the primary Cognito User Pool, deactivate and delete the `us-west-2` Cognito replica stack, then delete the `us-east-2` primary Cognito stack (KMS keys schedule deletion automatically).
5. Detach global cluster members and delete `database`, `network`, and `edge` stacks.
6. Empty artifact buckets, delete regional `bootstrap` stacks, and report any remaining resources.

```bash
# Dry-run teardown preview:
bash .github/workflows-local/destroy.sh --dry-run

# Live teardown (prompts interactively or accepts confirmation flags):
bash .github/workflows-local/destroy.sh \
  --confirm destroy-aws-multi-region-aurora-failover-active-passive \
  --confirm-deletion-protection disable-deletion-protection
```
