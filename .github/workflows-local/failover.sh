#!/usr/bin/env bash
# failover.sh - Local workflow runner equivalent of .github/workflows/failover.yml (spec 002, task 3.3).
#
# Supports directions:
#   to-secondary:
#     1. Prechecks (00-precheck.sh)
#     2. Verify primary region is fenced and secondary region is NOT fenced
#     3. Activate the Cognito replica in us-west-2 (Status=ACTIVE on UserPoolRegionalConfigurationAttachment)
#     4. Promote Aurora secondary cluster to writer (20-promote.sh --yes)
#     5. Switch Route 53 DNS weights to secondary (30-switch-traffic.sh --to secondary)
#     6. Validate transactions (40-validate.sh) and check ledger consistency (45-consistency.sh)
#   to-primary:
#     1. Unfence the primary region (10-simulate-outage.sh --undo --region <primary>)
#     2. Promote Aurora primary cluster back to writer (50-failback-promote.sh --yes)
#     3. Deactivate the Cognito replica in us-west-2 (Status=INACTIVE on UserPoolRegionalConfigurationAttachment)
#     4. Restore Route 53 DNS weights to primary (30-switch-traffic.sh --to primary)
#     5. Validate transactions (40-validate.sh) and check ledger consistency (45-consistency.sh)
#
# Enforces local lock (.locks/failover.lock) and typed confirmation (confirm_typed failover).
#
# Usage:
#   bash .github/workflows-local/failover.sh --direction to-secondary|to-primary [--confirm failover] \
#     [--skip-http-validation] [--force] [--dry-run] [-h|--help]
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

cognito_replica_set_status() {
  local target_status="$1"
  bash -c '
    set -euo pipefail
    source "$1/scripts/lib/common.sh"
    source "$1/scripts/lib/dr.sh"
    cognito_replica_set_status "$2"
  ' _ "${REPO_ROOT}" "${target_status}"
}

direction=""
confirm_arg=""
force_branch=0
skip_http=0

while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --force) force_branch=1 ;;
    --skip-http-validation) skip_http=1 ;;
    --direction)
      shift
      direction="${1:-}"
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
require_local_env AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL API_DOMAIN_NAME HOSTED_ZONE_ID

case "${direction}" in
  to-secondary | to-primary) ;;
  *) fail "--direction must be to-secondary or to-primary (got '${direction}')" ;;
esac

confirm_typed "failover" \
  "Execute planned failover (${direction}) between ${AWS_REGION_PRIMARY} and ${AWS_REGION_SECONDARY}." \
  "${confirm_arg}"

verify_aws_identity
acquire_lock failover
init_run_summary "failover-${direction}"
trap 'release_lock' EXIT

declare -a dry_flag=()
is_dry_run && dry_flag=(--dry-run)

assert_primary_fenced() {
  if is_dry_run; then
    log "[dry-run] would verify that ${AWS_REGION_PRIMARY} is fenced"
    return 0
  fi
  local out
  out="$(bash "${REPO_ROOT}/scripts/dr/10-simulate-outage.sh" --status --region "${AWS_REGION_PRIMARY}")" || return 1
  printf '%s\n' "${out}"
  if ! grep -q ' is fenced: all ' <<<"${out}"; then
    fail "Primary region ${AWS_REGION_PRIMARY} is not fenced. Run simulate-outage.sh --action fence --region ${AWS_REGION_PRIMARY} first."
  fi
}

assert_secondary_not_fenced() {
  if is_dry_run; then
    log "[dry-run] would verify that ${AWS_REGION_SECONDARY} is not fenced"
    return 0
  fi
  local out
  out="$(bash "${REPO_ROOT}/scripts/dr/10-simulate-outage.sh" --status --region "${AWS_REGION_SECONDARY}")" || true
  printf '%s\n' "${out}"
  if grep -qE 'reserved concurrency 0$' <<<"${out}"; then
    fail "Secondary region ${AWS_REGION_SECONDARY} has fenced functions. Unfence ${AWS_REGION_SECONDARY} before failing over to it."
  fi
}

rc=0
if [[ "${direction}" == "to-secondary" ]]; then
  timed_step "00-precheck.sh" bash "${REPO_ROOT}/scripts/dr/00-precheck.sh" "${dry_flag[@]}" || rc=$?
  ((rc == 0)) && { timed_step "Verify primary fenced" assert_primary_fenced || rc=$?; }
  ((rc == 0)) && { timed_step "Verify secondary not fenced" assert_secondary_not_fenced || rc=$?; }
  ((rc == 0)) && { timed_step "Activate Cognito replica (${AWS_REGION_SECONDARY})" cognito_replica_set_status ACTIVE || rc=$?; }
  ((rc == 0)) && { timed_step "20-promote.sh --yes" bash "${REPO_ROOT}/scripts/dr/20-promote.sh" "${dry_flag[@]}" --yes || rc=$?; }
  ((rc == 0)) && { timed_step "30-switch-traffic.sh --to secondary" bash "${REPO_ROOT}/scripts/dr/30-switch-traffic.sh" "${dry_flag[@]}" --to secondary || rc=$?; }
  if ((rc == 0 && skip_http == 0)); then
    if is_dry_run || [[ -n "${COGNITO_ID_TOKEN:-}" ]]; then
      timed_step "40-validate.sh (${AWS_REGION_SECONDARY})" \
        bash "${REPO_ROOT}/scripts/dr/40-validate.sh" "${dry_flag[@]}" --expect-region "${AWS_REGION_SECONDARY}" --wait 180 --probe || rc=$?
      ((rc == 0)) && { timed_step "45-consistency.sh" bash "${REPO_ROOT}/scripts/dr/45-consistency.sh" "${dry_flag[@]}" || rc=$?; }
    else
      log "Skipping HTTP validation in CLI (set COGNITO_ID_TOKEN, or verify through the browser with a Cognito session)."
    fi
  fi
else
  timed_step "Unfence primary (${AWS_REGION_PRIMARY})" \
    bash "${REPO_ROOT}/scripts/dr/10-simulate-outage.sh" "${dry_flag[@]}" --undo --region "${AWS_REGION_PRIMARY}" || rc=$?
  ((rc == 0)) && { timed_step "50-failback-promote.sh --yes" bash "${REPO_ROOT}/scripts/dr/50-failback-promote.sh" "${dry_flag[@]}" --yes || rc=$?; }
  ((rc == 0)) && { timed_step "Deactivate Cognito replica (${AWS_REGION_SECONDARY})" cognito_replica_set_status INACTIVE || rc=$?; }
  ((rc == 0)) && { timed_step "30-switch-traffic.sh --to primary" bash "${REPO_ROOT}/scripts/dr/30-switch-traffic.sh" "${dry_flag[@]}" --to primary || rc=$?; }
  if ((rc == 0 && skip_http == 0)); then
    if is_dry_run || [[ -n "${COGNITO_ID_TOKEN:-}" ]]; then
      timed_step "40-validate.sh (${AWS_REGION_PRIMARY})" \
        bash "${REPO_ROOT}/scripts/dr/40-validate.sh" "${dry_flag[@]}" --expect-region "${AWS_REGION_PRIMARY}" --wait 180 --probe || rc=$?
      ((rc == 0)) && { timed_step "45-consistency.sh" bash "${REPO_ROOT}/scripts/dr/45-consistency.sh" "${dry_flag[@]}" || rc=$?; }
    else
      log "Skipping HTTP validation in CLI (set COGNITO_ID_TOKEN, or verify through the browser with a Cognito session)."
    fi
  fi
fi

write_run_summary "Local Failover Run Summary (${direction})" \
  "Direction=${direction}" \
  "Primary Region=${AWS_REGION_PRIMARY}" \
  "Secondary Region=${AWS_REGION_SECONDARY}" \
  "Cognito Replica Status=$( [[ "${direction}" == "to-secondary" ]] && echo ACTIVE || echo INACTIVE )" \
  "Result=$( ((rc == 0)) && echo SUCCESS || echo "FAILED (exit ${rc})" )"

exit "${rc}"
