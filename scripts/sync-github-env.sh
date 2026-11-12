#!/usr/bin/env bash
# sync-github-env.sh - Synchronize GitHub Environment variables and secrets from local prod.env
#
# Reads .github/workflows-local/prod.env and uses the GitHub CLI (gh) to populate
# or update variables and secrets in the target GitHub Environment ("prod" by default).
#
# Prerequisites:
#   - GitHub CLI (gh) authenticated (e.g. gh auth status)
#   - .github/workflows-local/prod.env populated locally
#
# Usage:
#   bash scripts/sync-github-env.sh [--env prod] [--repo owner/repo] [--dry-run]
set -euo pipefail

ENV_NAME="prod"
REPO_ARG=""
DRY_RUN=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.github/workflows-local/prod.env"

while (($# > 0)); do
  case "$1" in
    --env)
      shift
      ENV_NAME="${1:-prod}"
      ;;
    --repo)
      shift
      REPO_ARG="${1:-}"
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    -h|--help)
      sed -n '2,/^set -euo pipefail/{/^set -euo/!p;}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
  shift
done

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Error: Local environment file not found: ${ENV_FILE}" >&2
  echo "Copy .github/workflows-local/config.example.env to ${ENV_FILE} and fill in the values." >&2
  exit 1
fi

GH_CMD=(gh)
if [[ -n "${REPO_ARG}" ]]; then
  GH_CMD+=(--repo "${REPO_ARG}")
fi

echo "Loading configuration from ${ENV_FILE}..."
# Read KEY=VALUE ignoring comments and empty lines without eval
while IFS='=' read -r key value || [[ -n "${key}" ]]; do
  # Trim whitespace
  key="$(echo "${key}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  value="$(echo "${value}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [[ -z "${key}" || "${key}" =~ ^# ]] && continue
  
  # Remove optional surrounding quotes
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  
  export "${key}=${value}"
done < "${ENV_FILE}"

echo "Syncing variables to GitHub Environment '${ENV_NAME}'..."

VARIABLES=(
  "API_DOMAIN_NAME"
  "AWS_REGION_GLOBAL"
  "AWS_REGION_PRIMARY"
  "AWS_REGION_SECONDARY"
  "DB_INSTANCE_CLASS"
  "ENGINE_VERSION"
  "HOSTED_ZONE_ID"
  "ALERT_EMAIL"
)

for var in "${VARIABLES[@]}"; do
  val="${!var:-}"
  if [[ -n "${val}" ]]; then
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      echo "[DRY-RUN] Would set variable: ${var}=${val}"
    else
      echo "Setting variable: ${var}..."
      "${GH_CMD[@]}" variable set "${var}" --env "${ENV_NAME}" --body "${val}"
    fi
  else
    echo "Skipping empty variable: ${var}"
  fi
done

# Sync AWS_ROLE_ARN as secret if present
if [[ -n "${AWS_ROLE_ARN:-}" ]]; then
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "[DRY-RUN] Would set secret: AWS_ROLE_ARN=***"
  else
    echo "Setting secret: AWS_ROLE_ARN..."
    echo "${AWS_ROLE_ARN}" | "${GH_CMD[@]}" secret set AWS_ROLE_ARN --env "${ENV_NAME}"
  fi
fi

echo "Done! GitHub Environment '${ENV_NAME}' configuration updated."

