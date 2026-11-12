#!/usr/bin/env bash
# simulate-outage.sh - Local workflow runner equivalent of .github/workflows/simulate-outage.yml (spec 002, task 3.3).
#
# Sources lib/common.sh, loads .github/workflows-local/prod.env, validates the target data region
# (AWS_REGION_PRIMARY or AWS_REGION_SECONDARY), requires typed confirmation of the region name when action=fence,
# and executes scripts/dr/10-simulate-outage.sh using local AWS_PROFILE credentials without GitHub Actions minutes.
#
# Usage:
#   bash .github/workflows-local/simulate-outage.sh [--action status|fence|unfence] [--region <region>] \
#     [--confirm <region>] [--force] [--dry-run] [-h|--help]
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

action="status"
target_region=""
confirm_arg=""
force_branch=0

while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --force) force_branch=1 ;;
    --action)
      shift
      action="${1:-}"
      ;;
    --region)
      shift
      target_region="${1:-}"
      ;;
    --confirm)
      shift
      confirm_arg="${1:-}"
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
require_local_env AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL

target_region="${target_region:-${AWS_REGION_PRIMARY}}"
case "${action}" in
  status | fence | unfence) ;;
  *) fail "--action must be status, fence, or unfence (got '${action}')" ;;
esac

if [[ "${target_region}" != "${AWS_REGION_PRIMARY}" && "${target_region}" != "${AWS_REGION_SECONDARY}" ]]; then
  fail "--region must equal AWS_REGION_PRIMARY (${AWS_REGION_PRIMARY}) or AWS_REGION_SECONDARY (${AWS_REGION_SECONDARY})"
fi

if [[ "${action}" == "fence" ]]; then
  confirm_typed "${target_region}" \
    "Fencing ${target_region} sets reserved concurrency 0 on all six regional API Lambda functions." \
    "${confirm_arg}"
fi

verify_aws_identity
acquire_lock simulate-outage
init_run_summary simulate-outage
trap 'release_lock' EXIT

declare -a script_args=(--region "${target_region}")
is_dry_run && script_args+=(--dry-run)
case "${action}" in
  status) script_args+=(--status) ;;
  unfence) script_args+=(--undo) ;;
  fence) ;;
esac

rc=0
timed_step "10-simulate-outage.sh (${action} ${target_region})" \
  bash "${REPO_ROOT}/scripts/dr/10-simulate-outage.sh" "${script_args[@]}" || rc=$?

write_run_summary "Local Simulate-Outage Run Summary" \
  "Action=${action}" \
  "Target Region=${target_region}" \
  "Result=$( ((rc == 0)) && echo SUCCESS || echo "FAILED (exit ${rc})" )"

exit "${rc}"
