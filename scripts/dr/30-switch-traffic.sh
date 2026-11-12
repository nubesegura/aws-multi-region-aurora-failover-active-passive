#!/usr/bin/env bash
# 30-switch-traffic.sh - move the Route 53 weights by updating the dns stack in the global region (requirements 3.3, 3.4).
#
# Updates stack stk-<global region code>-drdemo-dns-prod with PrimaryWeight/SecondaryWeight (255/0 or 0/255). It runs
# against the global region only, so it works while the primary region is down. No regional stack is redeployed.
# Order of a drill: fence the old region, promote the database, ONLY THEN change the weights (design C21, T-SPLIT).
#
# Safety checks before the change:
#   - the cluster of the target region must be the live writer (--force skips this check);
#   - if the other region's cluster is NOT a reader of the same global cluster (unplanned failover: it was detached
#     and may still accept writes), its functions must be fenced (reserved concurrency 0): the script fences them
#     now, and asks for a typed confirmation when that is not possible.
#
# UNDO:    scripts/dr/30-switch-traffic.sh --to primary   (weights 255/0 back to AWS_REGION_PRIMARY)
# Usage:   scripts/dr/30-switch-traffic.sh [--to primary|secondary] [--force] [--dry-run]
#          default: --to secondary (traffic goes to AWS_REGION_SECONDARY, weights 0/255)
# Needs:   AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL; optional CFN_EXEC_ROLE_ARN
# Effect:  alias records answer with a 60 s TTL; resolvers and keep-alive connections can take longer (T-STALE).
set -euo pipefail

# shellcheck source=../lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
# shellcheck source=../lib/dr.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/dr.sh"
# shellcheck source=../lib/promote.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/promote.sh"

direction="secondary"
force=0
while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --force) force=1 ;;
    --to)
      shift
      direction="${1:-}"
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
case "${direction}" in
  primary)
    new="${AWS_REGION_PRIMARY}"
    old="${AWS_REGION_SECONDARY}"
    ;;
  secondary)
    new="${AWS_REGION_SECONDARY}"
    old="${AWS_REGION_PRIMARY}"
    ;;
  *) die "--to must be primary or secondary" ;;
esac
print_context
log "goal: send all traffic to ${new}"
dns_weights_report
read -r primary_weight secondary_weight <<<"$(weights_for_region "${new}")"
log "new weights: ${AWS_REGION_PRIMARY}=${primary_weight} ${AWS_REGION_SECONDARY}=${secondary_weight}"

log "step 1: the database in ${new} must be the writer before traffic moves"
GLOBAL_PREFERRED_REGION="${new}"
discover_global_cluster
print_members
new_state="$(member_state "$(cluster_arn "${new}")")"
old_state="$(member_state "$(cluster_arn "${old}")")"
if ! is_dry_run && [[ "${new_state}" != "writer" ]]; then
  if ((force == 1)); then
    warn "the cluster in ${new} is ${new_state}, not the writer: continuing because of --force (writes will fail with 503 database_read_only)"
  else
    die "the cluster in ${new} is ${new_state}, not the writer. Promote it first (scripts/dr/20-promote.sh or 50-failback-promote.sh)."
  fi
fi

log "step 2: the old region ${old} must not be able to take writes"
if is_dry_run; then
  log "[dry-run] old cluster state is read live: when it is not a reader of the global cluster the functions of ${old} are fenced first"
elif [[ "${old_state}" == "reader" ]]; then
  log "the cluster in ${old} is a reader of the same global cluster: it rejects writes (database_read_only); no fence needed"
else
  warn "the cluster in ${old} is ${old_state}: it is detached or unknown and may accept writes (split-brain risk)"
  if fence_report "${old}"; then
    log "functions of ${old} are already fenced"
  elif fence_set "${old}"; then
    log "functions of ${old} fenced now"
  else
    warn "could not fence ${old} (unreachable?). Moving traffic without a fence leaves a window for two writers."
    confirm_typed "proceed-unfenced ${old}" "Move traffic WITHOUT a fence on ${old}?" "${PROMOTE_UNFENCED_CONFIRM:-}"
  fi
fi

log "step 3: update the dns stack in ${AWS_REGION_GLOBAL}"
dns_apply "${primary_weight}" "${secondary_weight}"
dns_weights_report
log "done. New requests reach ${new} within about 60 s (TTL) plus resolver and keep-alive caching."
log "next: scripts/dr/40-validate.sh --expect-region ${new} --wait 180 ; undo: scripts/dr/30-switch-traffic.sh --to $([[ "${direction}" == "secondary" ]] && echo primary || echo secondary)"
