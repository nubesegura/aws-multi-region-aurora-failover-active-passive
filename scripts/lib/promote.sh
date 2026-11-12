#!/usr/bin/env bash
# Database promotion shared by scripts/dr/20-promote.sh (to the secondary region) and 50-failback-promote.sh (back to
# the primary region). Source after lib/common.sh and lib/dr.sh.
#
# Expected globals (set by the caller): PROMOTE_MAX_LAG_MS PROMOTE_FORCE PROMOTE_ASSUME_YES PROMOTE_API_REGION
# Optional: PROMOTE_LAG_WAIT_SECONDS (default 300) and PROMOTE_LAG_POLL_SECONDS (default 30), see lag_gate.
# shellcheck shell=bash
# shellcheck disable=SC2034  # GLOBAL_PREFERRED_REGION is read by discover_global_cluster in lib/dr.sh

# wait_for_writer REGION ; polls the live roles until the cluster of REGION is the writer (max 20 minutes)
wait_for_writer() {
  local region="$1" tries=0 max=80 state
  if is_dry_run; then
    log "[dry-run] would poll describe-global-clusters every 15 s until the cluster in ${region} is the writer (max 20 min)"
    return 0
  fi
  GLOBAL_PREFERRED_REGION="${region}"
  while ((tries < max)); do
    discover_global_cluster
    state="$(member_state "$(cluster_arn "${region}")")"
    log "  ${region}: ${state} (check $((tries + 1)) of ${max})"
    if [[ "${state}" == "writer" ]]; then
      print_members
      return 0
    fi
    tries=$((tries + 1))
    sleep 15
  done
  die "the cluster in ${region} is not the writer after 20 minutes; inspect with: aws rds describe-global-clusters (nothing was retried)"
}

# lag_gate REGION ; gate of a planned switchover on AuroraGlobalDBReplicationLag of the cluster that becomes the writer.
# A cluster that became a secondary minutes ago has no datapoint yet (CloudWatch starts publishing the metric 2 to 3
# minutes after a switchover), so a missing datapoint is polled before the gate fails. It always fails closed: no
# datapoint after the wait, or a failed query, stops the step before anything is changed (unless --force was given).
lag_gate() {
  local region="$1" cluster latest waited=0
  local wait_max="${PROMOTE_LAG_WAIT_SECONDS:-300}" poll="${PROMOTE_LAG_POLL_SECONDS:-30}"
  cluster="$(cluster_identifier "${region}")"
  latest="$(metric_stat "${region}" "${cluster}" AuroraGlobalDBReplicationLag 15 latest)"
  while ! is_dry_run && [[ "${latest}" == "n/a" ]] && ((waited < wait_max)); do
    log "no replication lag datapoint for ${region} yet (CloudWatch publishes it 2 to 3 minutes after a switchover): waiting ${poll} s (${waited} of ${wait_max} s)"
    sleep "${poll}"
    waited=$((waited + poll))
    latest="$(metric_stat "${region}" "${cluster}" AuroraGlobalDBReplicationLag 15 latest)"
  done
  log "replication lag of the cluster in ${region}: ${latest} ms (limit ${PROMOTE_MAX_LAG_MS} ms)"
  if [[ "${latest}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    if (($(printf '%.0f' "${latest}") > PROMOTE_MAX_LAG_MS)) && [[ "${PROMOTE_FORCE}" != "1" ]]; then
      die "lag ${latest} ms is above ${PROMOTE_MAX_LAG_MS} ms: wait for it to drop and run the step again (nothing was changed)"
    fi
    return 0
  fi
  is_dry_run && return 0
  if [[ "${PROMOTE_FORCE}" == "1" ]]; then
    warn "no usable lag value (${latest}); continuing because --force was given (the switchover itself waits for the secondary to catch up)"
    return 0
  fi
  if [[ "${latest}" == "error" ]]; then
    die "cannot read the replication lag of ${region}: the CloudWatch query failed (check cloudwatch:GetMetricStatistics on the role). Nothing was changed."
  fi
  die "no replication lag datapoint for ${region} after waiting ${waited} s: the lag gate cannot be checked and nothing was changed. Check the global cluster with scripts/dr/00-precheck.sh and run the step again; skipping the gate (--force) is a terminal-only decision of the owner."
}

# promote_database NEW_REGION OLD_REGION planned|unplanned
promote_database() {
  local new="$1" old="$2" mode="$3" new_arn old_arn new_state old_state api_region
  GLOBAL_PREFERRED_REGION=""
  discover_global_cluster
  print_members
  new_arn="$(cluster_arn "${new}")"
  old_arn="$(cluster_arn "${old}")"
  new_state="$(member_state "${new_arn}")"
  old_state="$(member_state "${old_arn}")"

  if ! is_dry_run; then
    case "${new_state}" in
      writer)
        log "the cluster in ${new} is already the writer: nothing to do"
        return 0
        ;;
      absent)
        die "the cluster in ${new} is not a member of ${GLOBAL_ID}, so it cannot be promoted by the global API. After an unplanned failover the old primary must be rebuilt as a secondary first (see the runbook, failback after an unplanned failover)."
        ;;
    esac
  fi

  if [[ "${mode}" == "planned" ]]; then
    if ! is_dry_run && [[ "${old_state}" != "writer" ]]; then
      die "the cluster in ${old} is ${old_state}, not the writer: the roles are not what this step expects. Run scripts/dr/00-precheck.sh."
    fi
    lag_gate "${new}"
    confirm_yes "Planned switchover: ${new} becomes the writer, ${old} becomes a reader. Writes pause for a few minutes. Continue?" "${PROMOTE_ASSUME_YES}"
    api_region="${PROMOTE_API_REGION:-${old}}"
    if ! aws_write rds switchover-global-cluster --region "${api_region}" --global-cluster-identifier "${GLOBAL_ID}" \
      --target-db-cluster-identifier "${new_arn}"; then
      die "switchover request failed (nothing else was changed). If the API region is the problem, retry with --api-region ${new}."
    fi
    wait_for_writer "${new}"
    log "switchover finished: ${new} is the writer. No data was lost."
    return 0
  fi

  # Unplanned failover.
  warn "UNPLANNED FAILOVER with data loss: the writes of the last moments (replication lag) can be lost; the old"
  warn "primary leaves the global cluster and may still accept writes (split brain) unless its functions are fenced."
  rpo_report "${new}"
  log "step 1: fence the old region ${old} (reserved concurrency 0) BEFORE promoting"
  if fence_set "${old}"; then
    log "old region fenced"
  else
    warn "could not fence ${old} (its API may be unreachable). Without the fence a client that still reaches it can write to a diverging copy."
    confirm_typed "proceed-unfenced ${old}" "Continue WITHOUT a fence on ${old}?" "${PROMOTE_UNFENCED_CONFIRM:-}"
  fi
  log "step 2: failover to ${new}"
  confirm_typed "allow-data-loss ${new}" "failover-global-cluster --allow-data-loss promotes ${new} and can lose the last writes." ""
  api_region="${PROMOTE_API_REGION:-${new}}"
  if ! aws_write rds failover-global-cluster --region "${api_region}" --global-cluster-identifier "${GLOBAL_ID}" \
    --target-db-cluster-identifier "${new_arn}" --allow-data-loss; then
    die "failover request failed (the old region stays fenced). If the API region is the problem, retry with --api-region ${old}."
  fi
  wait_for_writer "${new}"
  log "failover finished: ${new} is the writer. KEEP ${old} FENCED until its cluster is deleted or rebuilt as a secondary."
  log "measure the data gap: compare /stats of ${new} with the baseline printed by 00-precheck.sh"
}
