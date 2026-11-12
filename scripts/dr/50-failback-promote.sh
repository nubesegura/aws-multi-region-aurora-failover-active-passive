#!/usr/bin/env bash
# 50-failback-promote.sh - failback, step 1: promote the PRIMARY region's cluster back to writer (mirror of 20-promote.sh).
#
# Default (planned): switchover-global-cluster back to AWS_REGION_PRIMARY, zero data loss. It needs the primary
# region's cluster to be a reader member of the global cluster, which is the case after a PLANNED forward switchover.
# --unplanned mirrors the unplanned path (failover --allow-data-loss, fence of the other region, typed confirmation).
# After an UNPLANNED forward failover the old primary was detached and has to be rebuilt as a secondary first;
# this script stops with that message (runbook: failback after an unplanned failover).
#
# Failback order: 50 (database) -> 60-restore.sh (lift fence, move traffic) -> 40-validate.sh.
# UNDO:    scripts/dr/20-promote.sh (planned switchover forward again).
# Usage:   scripts/dr/50-failback-promote.sh [--unplanned] [--max-lag-ms N] [--force] [--yes] [--api-region R] [--dry-run]
# Needs:   AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL; owner credentials
set -euo pipefail

# shellcheck source=../lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
# shellcheck source=../lib/dr.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/dr.sh"
# shellcheck source=../lib/promote.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/promote.sh"

mode="planned"
PROMOTE_MAX_LAG_MS=5000
PROMOTE_FORCE=0
PROMOTE_ASSUME_YES=0
PROMOTE_API_REGION=""
while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --unplanned) mode="unplanned" ;;
    --force) PROMOTE_FORCE=1 ;;
    --yes) PROMOTE_ASSUME_YES=1 ;;
    --max-lag-ms)
      shift
      PROMOTE_MAX_LAG_MS="${1:-}"
      ;;
    --api-region)
      shift
      PROMOTE_API_REGION="${1:-}"
      ;;
    -h | --help)
      sed -n '2,/^set -euo pipefail/{/^set -euo/!p;}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
[[ "${PROMOTE_MAX_LAG_MS}" =~ ^[0-9]+$ ]] || die "--max-lag-ms must be a number"

dr_load_config
print_context
log "goal: promote ${AWS_REGION_PRIMARY} (writer again); current writer region should be ${AWS_REGION_SECONDARY}; mode: ${mode}"
promote_database "${AWS_REGION_PRIMARY}" "${AWS_REGION_SECONDARY}" "${mode}"
log "next: scripts/dr/60-restore.sh (lifts the fence on ${AWS_REGION_PRIMARY} and moves traffic back)"
