#!/usr/bin/env bash
# 60-restore.sh - failback, step 2: return to the normal state (mirror of 10 + 30 of the forward path).
#
# 1. checks that the PRIMARY region's cluster is the live writer again (run 50-failback-promote.sh first);
# 2. lifts the fence (reserved concurrency) on the primary region's functions, refused for a standalone cluster
#    unless the typed phrase is given (a detached cluster may be writable: split brain);
# 3. moves Route 53 back to the primary region (30-switch-traffic.sh --to primary: weights 255/0);
# 4. reports the fence state of both regions.
#
# UNDO:    scripts/dr/10-simulate-outage.sh (fence again) and scripts/dr/30-switch-traffic.sh --to secondary.
# Usage:   scripts/dr/60-restore.sh [--force] [--dry-run]
# Needs:   AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL; optional CFN_EXEC_ROLE_ARN, RESERVED_CONCURRENCY
set -euo pipefail

# shellcheck source=../lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
# shellcheck source=../lib/dr.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/dr.sh"

force=0
passthrough=()
while (($# > 0)); do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      passthrough+=(--dry-run)
      ;;
    --force)
      force=1
      passthrough+=(--force)
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
print_context

log "step 1/4: the database in ${AWS_REGION_PRIMARY} must be the writer again"
GLOBAL_PREFERRED_REGION="${AWS_REGION_PRIMARY}"
discover_global_cluster
print_members
state="$(member_state "$(cluster_arn "${AWS_REGION_PRIMARY}")")"
if ! is_dry_run && [[ "${state}" != "writer" ]]; then
  if ((force == 1)); then
    warn "the cluster in ${AWS_REGION_PRIMARY} is ${state}: continuing because of --force"
  else
    die "the cluster in ${AWS_REGION_PRIMARY} is ${state}, not the writer. Run scripts/dr/50-failback-promote.sh first."
  fi
fi

log "step 2/4: lift the fence on ${AWS_REGION_PRIMARY}"
assert_safe_to_unfence "${AWS_REGION_PRIMARY}"
if ! fence_clear "${AWS_REGION_PRIMARY}"; then
  die "could not lift the fence on every function of ${AWS_REGION_PRIMARY}; traffic was NOT moved"
fi

log "step 3/4: move Route 53 back to ${AWS_REGION_PRIMARY}"
"$(dirname "${BASH_SOURCE[0]}")/30-switch-traffic.sh" --to primary "${passthrough[@]}"

log "step 4/4: fence state of both regions (none should be fenced now)"
for region in "${AWS_REGION_PRIMARY}" "${AWS_REGION_SECONDARY}"; do
  fence_report "${region}" || true
done
log "restored. Run: scripts/dr/40-validate.sh --expect-region ${AWS_REGION_PRIMARY} --wait 180 --probe"
