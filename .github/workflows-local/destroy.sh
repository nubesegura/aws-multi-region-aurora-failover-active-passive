#!/usr/bin/env bash
# destroy.sh - Local workflow runner equivalent of .github/workflows/destroy.yml (spec 002, task 3.2).
#
# Loads .github/workflows-local/prod.env, verifies the active local AWS profile identity, prompts for typed
# confirmation (destroy-aws-multi-region-aurora-failover-active-passive and disable-deletion-protection), acquires a local lock (.locks/destroy.lock),
# and executes scripts/destroy.sh in safe phased groups using the local AWS_PROFILE.
#
# Teardown order enforced by scripts/destroy.sh (requirement 1.9):
#   1. Disable deletion protection on Aurora clusters and global cluster.
#   2. Delete Route 53 DNS stack.
#   3. Delete regional API stacks before Cognito stacks, then lift Cognito User Pool deletion protection,
#      deactivate and delete the replica Cognito stack in us-west-2, delete the primary Cognito stack in us-east-2,
#      and schedule KMS keys for deletion with a 7-day waiting period.
#   4-6. Detach global cluster members; delete database, network, and edge stacks.
#   7-8. Empty artifact buckets, delete regional bootstrap stacks, and report any leftovers.
#
# Usage:
#   bash .github/workflows-local/destroy.sh [--steps <range>] [--confirm destroy-aws-multi-region-aurora-failover-active-passive] \
#     [--confirm-deletion-protection disable-deletion-protection] [--eni-wait-seconds N] [--dry-run] [-h|--help]
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

steps_arg=""
confirm_arg=""
protection_arg=""
eni_wait="180"

while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --steps)
      shift
      steps_arg="${1:-}"
      ;;
    --confirm)
      shift
      confirm_arg="${1:-}"
      ;;
    --confirm-deletion-protection)
      shift
      protection_arg="${1:-}"
      ;;
    --eni-wait-seconds)
      shift
      eni_wait="${1:-180}"
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
require_local_env AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL
verify_aws_identity

confirm_typed "destroy-aws-multi-region-aurora-failover-active-passive" \
  "This will tear down the DR demo infrastructure in ${AWS_REGION_PRIMARY}, ${AWS_REGION_SECONDARY}, and ${AWS_REGION_GLOBAL}." \
  "${confirm_arg}"
confirm_arg="destroy-aws-multi-region-aurora-failover-active-passive"

confirm_typed "disable-deletion-protection" \
  "Deletion protection will be disabled on the Aurora clusters, global cluster, and Cognito User Pool." \
  "${protection_arg}"
protection_arg="disable-deletion-protection"

acquire_lock destroy
init_run_summary destroy
trap 'release_lock' EXIT

declare -a common_flags=(--confirm "${confirm_arg}" --confirm-deletion-protection "${protection_arg}")
is_dry_run && common_flags+=(--dry-run)

rc=0
if [[ -n "${steps_arg}" ]]; then
  timed_step "destroy (${steps_arg})" \
    bash "${REPO_ROOT}/scripts/destroy.sh" "${common_flags[@]}" --eni-wait-seconds "${eni_wait}" --steps "${steps_arg}" || rc=$?
else
  timed_step "Group 1 (steps 1-3: deletion protection, dns, api, cognito)" \
    bash "${REPO_ROOT}/scripts/destroy.sh" "${common_flags[@]}" --steps 1-3 || rc=$?
  if ((rc == 0)); then
    timed_step "Group 2 (steps 4-6: detach global cluster, database, network, edge)" \
      bash "${REPO_ROOT}/scripts/destroy.sh" "${common_flags[@]}" --eni-wait-seconds "${eni_wait}" --steps 4-6 || rc=$?
  fi
  if ((rc == 0)); then
    timed_step "Group 3 (steps 7-8: artifact buckets, bootstrap stacks, leftovers)" \
      bash "${REPO_ROOT}/scripts/destroy.sh" "${common_flags[@]}" --steps 7-8 || rc=$?
  fi
fi

write_run_summary "Local Destroy Run Summary" \
  "Steps=${steps_arg:-1-8 (groups 1-3)}" \
  "Primary Region=${AWS_REGION_PRIMARY}" \
  "Secondary Region=${AWS_REGION_SECONDARY}" \
  "Global Region=${AWS_REGION_GLOBAL}" \
  "Result=$( ((rc == 0)) && echo SUCCESS || echo "FAILED (exit ${rc})" )"

exit "${rc}"
