#!/usr/bin/env bash
# 10-simulate-outage.sh - simulate (not reproduce) the loss of a region by fencing its functions (requirement 7.4).
#
# Sets reserved concurrency 0 on the six API functions of the region: every request to that region is throttled by
# Lambda (HTTP 429/500 from API Gateway). The database is NOT touched. The same mechanism fences the old region
# after an unplanned failover (see 20-promote.sh), so the old cluster cannot take writes (threat T-SPLIT).
#
# UNDO:    scripts/dr/10-simulate-outage.sh --undo     (removes the reservation; set RESERVED_CONCURRENCY=<n> to
#          restore a specific value instead). Lifting is refused for a standalone cluster unless the typed phrase
#          is given (FORCE_UNFENCE="unfence-standalone <region>" or interactively).
# Usage:   scripts/dr/10-simulate-outage.sh [--region <region>] [--undo | --status] [--dry-run]
#          default region: AWS_REGION_PRIMARY ; --status only reports the reserved concurrency of the six functions
# Needs:   AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL; credentials with lambda:PutFunctionConcurrency
#          (--status needs only lambda:GetFunctionConcurrency)
# Exit:    0 done (--status: always 0 when it could report); 1 a function could not be fenced or unfenced, or one of the
#          six functions does not exist (nothing is changed then).
set -euo pipefail

# shellcheck source=../lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
# shellcheck source=../lib/dr.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/dr.sh"

target=""
undo=0
status=0
while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --undo) undo=1 ;;
    --status) status=1 ;;
    --region)
      shift
      target="${1:-}"
      ;;
    -h | --help)
      sed -n '2,/^set -euo pipefail/{/^set -euo/!p;}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

dr_load_config
target="${target:-${AWS_REGION_PRIMARY}}"
[[ "${target}" == "${AWS_REGION_PRIMARY}" || "${target}" == "${AWS_REGION_SECONDARY}" ]] ||
  die "--region must be AWS_REGION_PRIMARY or AWS_REGION_SECONDARY"
((undo == 0 || status == 0)) || die "--undo and --status cannot be combined"
print_context

if ((status == 1)); then
  log "STATUS (report only, nothing is changed) of ${target}:"
  if is_dry_run; then
    fence_report "${target}"
  elif fence_report "${target}"; then
    log "${target} is fenced: all ${#FUNCTION_DESCRIPTORS[@]} functions have reserved concurrency 0"
  else
    log "${target} is NOT fully fenced (a function with another value, unreserved or unreadable counts as not fenced)"
  fi
elif ((undo == 1)); then
  log "UNDO: lifting the simulated outage of ${target}"
  assert_safe_to_unfence "${target}"
  if fence_clear "${target}"; then
    log "reserved concurrency removed in ${target}"
  else
    die "could not lift the fence on every function of ${target}"
  fi
  fence_report "${target}" || true
else
  log "simulating the loss of ${target}: reserved concurrency 0 on its six functions (the database keeps running)"
  if fence_set "${target}"; then
    log "fence set in ${target}"
  else
    die "could not fence every function of ${target}"
  fi
  fence_report "${target}" || true
  log "next: scripts/dr/20-promote.sh (planned) ; undo with: scripts/dr/10-simulate-outage.sh --undo"
fi
