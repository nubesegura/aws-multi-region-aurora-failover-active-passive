#!/usr/bin/env bash
# 20-promote.sh - promote the secondary region's Aurora cluster to writer (requirements 7.2 and 7.3).
#
# Default (planned, both regions healthy): switchover-global-cluster. Zero data loss: Aurora waits until the
# secondary has caught up, then swaps the roles.
# --unplanned (real regional outage): failover-global-cluster --allow-data-loss with the secondary region as the API
# region. Replication is asynchronous (RPO about one second, not zero). The old primary is FENCED first (reserved
# concurrency 0 on its functions) and an explicit typed confirmation is required.
#
# The database stacks are NOT redeployed: RDS owns the roles after this point (design C19, C21).
# UNDO:    planned: scripts/dr/50-failback-promote.sh (mirror switchover back). Unplanned: not reversible; the old
#          primary is detached and must be rebuilt as a secondary (runbook, failback after an unplanned failover).
# Usage:   scripts/dr/20-promote.sh [--unplanned] [--max-lag-ms N] [--force] [--yes] [--api-region R] [--dry-run]
# Needs:   AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL; owner credentials (rds:SwitchoverGlobalCluster)
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
log "goal: promote ${AWS_REGION_SECONDARY} (new writer); old writer region: ${AWS_REGION_PRIMARY}; mode: ${mode}"
promote_database "${AWS_REGION_SECONDARY}" "${AWS_REGION_PRIMARY}" "${mode}"
log "next: scripts/dr/30-switch-traffic.sh (moves Route 53 to ${AWS_REGION_SECONDARY}); then 40-validate.sh"
