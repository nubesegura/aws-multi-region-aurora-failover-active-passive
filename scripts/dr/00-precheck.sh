#!/usr/bin/env bash
# 00-precheck.sh - read-only pre-checks before a DR drill (requirement 7.1).
#
# Prints: account (masked) and regions, global cluster members and their LIVE roles, cluster status, replication
# lag and RPO exposure (CloudWatch), fence state of both regions, current Route 53 weights and, when API_DOMAIN_NAME
# and COGNITO_ID_TOKEN are set, a baseline of /stats (total and last processed_at per region, to measure a data gap).
# Changes nothing, so there is nothing to undo.
#
# Usage:   scripts/dr/00-precheck.sh [--dry-run]
# Needs:   AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL (optional: API_DOMAIN_NAME COGNITO_ID_TOKEN)
# Exit:    0 when the checks ran; 1 when the lag is above 5000 ms or a cluster is not available.
set -euo pipefail

# shellcheck source=../lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
# shellcheck source=../lib/dr.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/dr.sh"

while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
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

problems=0

log "step 1/5: global cluster members and live roles"
discover_global_cluster
print_members

log "step 2/5: cluster status in both regions"
for region in "${AWS_REGION_PRIMARY}" "${AWS_REGION_SECONDARY}"; do
  if is_dry_run; then
    log "[dry-run] would read the status of $(cluster_identifier "${region}") in ${region}"
    continue
  fi
  status="$(aws rds describe-db-clusters --region "${region}" --db-cluster-identifier "$(cluster_identifier "${region}")" \
    --query 'DBClusters[0].Status' --output text 2>/dev/null)" || status="unreachable"
  log "  ${region}: $(cluster_identifier "${region}") status=${status}"
  [[ "${status}" == "available" ]] || problems=1
done

log "step 3/5: replication lag and RPO exposure"
# The reader is the member that is not the writer; in a normal state it is the secondary region.
reader_region="${AWS_REGION_SECONDARY}"
if ! is_dry_run; then
  if [[ "$(member_state "$(cluster_arn "${AWS_REGION_PRIMARY}")")" == "reader" ]]; then
    reader_region="${AWS_REGION_PRIMARY}"
  fi
fi
rpo_report "${reader_region}"
if ! is_dry_run; then
  latest="$(metric_stat "${reader_region}" "$(cluster_identifier "${reader_region}")" AuroraGlobalDBReplicationLag 15 latest)"
  if [[ "${latest}" =~ ^[0-9]+(\.[0-9]+)?$ ]] && (($(printf '%.0f' "${latest}") > 5000)); then
    warn "replication lag ${latest} ms is above 5000 ms: do not run a planned switchover yet"
    problems=1
  fi
fi
sql_status

log "step 4/5: fence state (reserved concurrency) and Route 53 weights"
for region in "${AWS_REGION_PRIMARY}" "${AWS_REGION_SECONDARY}"; do
  fence_report "${region}" || true
done
dns_weights_report

log "step 5/5: API baseline (optional)"
if [[ -n "${API_DOMAIN_NAME:-}" && -n "${COGNITO_ID_TOKEN:-}" ]]; then
  if is_dry_run; then
    log "[dry-run] would call GET https://${API_DOMAIN_NAME}/stats and print the counts per region as a baseline"
  else
    api_init
    trap api_cleanup EXIT
    code="$(api_call GET /stats)"
    if [[ "${code}" == "200" ]]; then
      log "  baseline: served_by_region=$(jq -r '.served_by_region' "${API_BODY_FILE}") database_role=$(jq -r '.database_role' "${API_BODY_FILE}") total_count=$(jq -r '.total_count' "${API_BODY_FILE}")"
      log "  counts_by_region=$(jq -c '.counts_by_region' "${API_BODY_FILE}")"
      log "  last_processed_at_by_region=$(jq -c '.last_processed_at_by_region' "${API_BODY_FILE}")"
      log "  keep these numbers: after an unplanned failover compare them with the promoted region to measure the data gap"
    else
      warn "GET /stats returned HTTP ${code}; baseline not available"
    fi
  fi
else
  log "  skipped (set API_DOMAIN_NAME and COGNITO_ID_TOKEN to record a /stats baseline)"
fi

if ((problems > 0)); then
  warn "precheck found problems (see above)"
  exit 1
fi
log "precheck finished"
