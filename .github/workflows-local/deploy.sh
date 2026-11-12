#!/usr/bin/env bash
# deploy.sh - Local workflow runner equivalent of .github/workflows/deploy.yml (spec 002, task 3.2).
#
# Validates that the active git branch is main (overridable via --force), loads .github/workflows-local/prod.env,
# verifies the active local AWS profile identity, acquires a local lock (.locks/deploy.lock), and invokes
# scripts/deploy.sh in sequenced groups (bootstrap, edge, cognito, network, db-primary, db-secondary, schema,
# api, dns, dashboard). Writes a markdown execution summary in .github/workflows-local/.runs/.
#
# Usage:
#   bash .github/workflows-local/deploy.sh [--steps <list>] [--dry-run] [--yes] [--force] [-h|--help]
#
# Options:
#   --steps <list>   Comma-separated subset of steps (default: bootstrap,edge,cognito,network,db-primary,db-secondary,schema,api,dns,dashboard)
#   --dry-run        Print planned actions without calling AWS APIs
#   --yes            Skip interactive confirmation prompt
#   --force          Allow running from a branch other than main
#   -h, --help       Show this help text
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

steps_arg=""
assume_yes=0
force_branch=0

while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --yes) assume_yes=1 ;;
    --force) force_branch=1 ;;
    --steps)
      shift
      steps_arg="${1:-}"
      [[ -n "${steps_arg}" ]] || fail "--steps requires a comma-separated list of steps"
      ;;
    -h | --help)
      sed -n '2,/^set -euo pipefail/{/^set -euo/!p;}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) fail "Unknown argument: $1 (pass --help for usage)" ;;
  esac
  shift
done

export DRY_RUN
load_env
verify_branch main "${force_branch}"
require_local_env AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL API_DOMAIN_NAME HOSTED_ZONE_ID GITHUB_REPOSITORY
verify_aws_identity

confirm_yes "Deploy the DR demo infrastructure to AWS (profile=${AWS_PROFILE}, primary=${AWS_REGION_PRIMARY}, secondary=${AWS_REGION_SECONDARY}, global=${AWS_REGION_GLOBAL})?" "${assume_yes}"

acquire_lock deploy
init_run_summary deploy
trap 'release_lock' EXIT

declare -a dry_flag=()
is_dry_run && dry_flag=(--dry-run)

rc=0
if [[ -n "${steps_arg}" ]]; then
  timed_step "deploy (${steps_arg})" bash "${REPO_ROOT}/scripts/deploy.sh" "${dry_flag[@]}" --steps "${steps_arg}" || rc=$?
else
  timed_step "Group 1 (bootstrap,edge,cognito,network,db-primary)" \
    bash "${REPO_ROOT}/scripts/deploy.sh" "${dry_flag[@]}" --steps bootstrap,edge,cognito,network,db-primary || rc=$?
  if ((rc == 0)); then
    timed_step "Group 2 (db-secondary)" \
      bash "${REPO_ROOT}/scripts/deploy.sh" "${dry_flag[@]}" --steps db-secondary || rc=$?
  fi
  if ((rc == 0)); then
    timed_step "Group 3 (schema,api,dns,dashboard)" \
      bash "${REPO_ROOT}/scripts/deploy.sh" "${dry_flag[@]}" --steps schema,api,dns,dashboard || rc=$?
  fi
fi

write_run_summary "Local Deploy Run Summary" \
  "Steps=${steps_arg:-all (groups 1-3)}" \
  "Primary Region=${AWS_REGION_PRIMARY}" \
  "Secondary Region=${AWS_REGION_SECONDARY}" \
  "Global Region=${AWS_REGION_GLOBAL}" \
  "Result=$( ((rc == 0)) && echo SUCCESS || echo "FAILED (exit ${rc})" )"

exit "${rc}"
