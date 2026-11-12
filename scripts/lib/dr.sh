#!/usr/bin/env bash
# Helpers of the DR drill scripts (scripts/dr/*.sh) and of destroy.sh. Source after lib/common.sh.
#
# Vocabulary: "primary region" and "secondary region" are the regions at CREATION (AWS_REGION_PRIMARY and
# AWS_REGION_SECONDARY). RDS swaps the real writer at runtime, so every decision here reads the live roles from
# describe-global-clusters (design C26) and never assumes the primary region is the writer.
#
# Fencing = reserved concurrency 0 on the six functions of a region. It is the only protection against split brain
# after an unplanned failover (threat T-SPLIT): the old cluster may still accept writes.
# shellcheck shell=bash

GLOBAL_ID=""
GLOBAL_JSON=""
GLOBAL_PREFERRED_REGION=""

# ---------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------
dr_load_config() {
  require_cmds jq
  if ! is_dry_run; then
    require_cmds aws
  fi
  require_env AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL
  validate_regions
}

# ---------------------------------------------------------------------------------------------
# Global cluster discovery (live roles)
# ---------------------------------------------------------------------------------------------
# Finds the global cluster whose members include the cluster of either data region and loads GLOBAL_ID and
# GLOBAL_JSON. Tries both regions so that it still works while one of them is unreachable.
# discover_global_cluster [soft] ; "soft" returns 1 instead of exiting when none is found.
discover_global_cluster() {
  if is_dry_run; then
    GLOBAL_ID="<global-cluster-id>"
    GLOBAL_JSON='{"GlobalClusterIdentifier":"<global-cluster-id>","GlobalClusterMembers":[]}'
    return 0
  fi
  local region json arn_a arn_b
  arn_a="$(cluster_arn "${AWS_REGION_PRIMARY}")"
  arn_b="$(cluster_arn "${AWS_REGION_SECONDARY}")"
  GLOBAL_JSON=""
  # GLOBAL_PREFERRED_REGION (optional) is asked first, e.g. the surviving region during an outage.
  for region in ${GLOBAL_PREFERRED_REGION:-} "${AWS_REGION_PRIMARY}" "${AWS_REGION_SECONDARY}"; do
    json="$(aws --cli-connect-timeout 10 --cli-read-timeout 30 rds describe-global-clusters --region "${region}" --output json 2>/dev/null)" || continue
    GLOBAL_JSON="$(jq -c --arg a "${arn_a}" --arg b "${arn_b}" \
      '[.GlobalClusters[] | select(any(.GlobalClusterMembers[]?; .DBClusterArn == $a or .DBClusterArn == $b))] | .[0] // empty' <<<"${json}")"
    if [[ -n "${GLOBAL_JSON}" ]]; then
      break
    fi
  done
  if [[ -z "${GLOBAL_JSON}" ]]; then
    [[ "${1:-}" == "soft" ]] && return 1
    die "no global cluster contains the clusters of ${AWS_REGION_PRIMARY} or ${AWS_REGION_SECONDARY} (not deployed, already removed, or both regions unreachable)"
  fi
  GLOBAL_ID="$(jq -r '.GlobalClusterIdentifier' <<<"${GLOBAL_JSON}")"
}

# member_state CLUSTER_ARN -> writer | reader | absent
member_state() {
  if is_dry_run; then
    printf '<writer|reader|absent>'
    return 0
  fi
  jq -r --arg a "$1" '[.GlobalClusterMembers[] | select(.DBClusterArn == $a)] | if length == 0 then "absent" elif .[0].IsWriter then "writer" else "reader" end' <<<"${GLOBAL_JSON}"
}

print_members() {
  if is_dry_run; then
    log "[dry-run] would list the global cluster members and their roles (describe-global-clusters)"
    return 0
  fi
  log "global cluster ${GLOBAL_ID} members (live roles):"
  jq -r '.GlobalClusterMembers[] | "  \(.DBClusterArn | split(":") | .[3])  \(.DBClusterArn | split(":") | .[-1])  \(if .IsWriter then "writer" else "reader" end)  sync=\(.SynchronizationStatus // "n/a")"' <<<"${GLOBAL_JSON}"
  local state region
  for region in "${AWS_REGION_PRIMARY}" "${AWS_REGION_SECONDARY}"; do
    state="$(member_state "$(cluster_arn "${region}")")"
    log "  ${region}: ${state}"
  done
}

# ---------------------------------------------------------------------------------------------
# Fencing (reserved concurrency 0)
# ---------------------------------------------------------------------------------------------
# fence_assert_functions REGION ; returns 1, changing nothing, unless all six functions exist and are readable.
# A partial fence (some functions fenced, the rest missing) is worse than none, so both callers check first.
fence_assert_functions() {
  local region="$1" descriptor name missing=0
  if is_dry_run; then
    log "[dry-run] would check that all ${#FUNCTION_DESCRIPTORS[@]} functions exist in ${region} (get-function-configuration): ${FUNCTION_DESCRIPTORS[*]}"
    return 0
  fi
  for descriptor in "${FUNCTION_DESCRIPTORS[@]}"; do
    name="$(function_name "${region}" "${descriptor}")"
    if ! aws lambda get-function-configuration --region "${region}" --function-name "${name}" --query FunctionName --output text >/dev/null 2>&1; then
      warn "function ${name} does not exist or cannot be read in ${region}; nothing was changed (deploy the api stack in this region first)"
      missing=1
    fi
  done
  return "${missing}"
}

# fence_set REGION ; returns 1 when at least one function could not be fenced (region unreachable, no permission)
# or when any of the six functions is missing (nothing is changed in that case).
fence_set() {
  local region="$1" descriptor failed=0
  fence_assert_functions "${region}" || return 1
  for descriptor in "${FUNCTION_DESCRIPTORS[@]}"; do
    aws_write lambda put-function-concurrency --region "${region}" \
      --function-name "$(function_name "${region}" "${descriptor}")" --reserved-concurrent-executions 0 || failed=1
  done
  return "${failed}"
}

# fence_clear REGION ; the template sets no reserved concurrency, so the default restore is "no reservation".
# Set RESERVED_CONCURRENCY=<n> to restore a specific reservation instead (design 3.3 mentions 5).
fence_clear() {
  local region="$1" descriptor failed=0 name
  fence_assert_functions "${region}" || return 1
  for descriptor in "${FUNCTION_DESCRIPTORS[@]}"; do
    name="$(function_name "${region}" "${descriptor}")"
    if [[ -n "${RESERVED_CONCURRENCY:-}" ]]; then
      aws_write lambda put-function-concurrency --region "${region}" --function-name "${name}" \
        --reserved-concurrent-executions "${RESERVED_CONCURRENCY}" || failed=1
    else
      aws_write lambda delete-function-concurrency --region "${region}" --function-name "${name}" || failed=1
    fi
  done
  return "${failed}"
}

# fence_report REGION ; prints the reserved concurrency of each function and returns 0 only when all six are 0
# (an unreadable or missing function counts as NOT fenced)
fence_report() {
  local region="$1" descriptor value all_zero=0
  if is_dry_run; then
    log "[dry-run] would read the reserved concurrency of the six functions in ${region}:"
    for descriptor in "${FUNCTION_DESCRIPTORS[@]}"; do
      log "[dry-run]   $(function_name "${region}" "${descriptor}")"
    done
    return 0
  fi
  for descriptor in "${FUNCTION_DESCRIPTORS[@]}"; do
    value="$(aws lambda get-function-concurrency --region "${region}" \
      --function-name "$(function_name "${region}" "${descriptor}")" \
      --query ReservedConcurrentExecutions --output text 2>/dev/null)" || value="unreadable"
    [[ "${value}" == "None" ]] && value="unreserved"
    log "  ${region} ${descriptor}: reserved concurrency ${value}"
    [[ "${value}" == "0" ]] || all_zero=1
  done
  return "${all_zero}"
}

# Lifting the fence is safe only while the cluster of that region is a member of the global cluster (reader or writer).
# A standalone cluster (detached by an unplanned failover) may be writable: two writers would diverge.
assert_safe_to_unfence() { # region
  local region="$1" state
  discover_global_cluster
  state="$(member_state "$(cluster_arn "${region}")")"
  if [[ "${state}" == "absent" ]]; then
    warn "the cluster of ${region} is NOT a member of global cluster ${GLOBAL_ID}: it is standalone and may accept writes (split-brain risk)."
    confirm_typed "unfence-standalone ${region}" "Lifting the fence on a standalone cluster can create two writers (threat T-SPLIT)." "${FORCE_UNFENCE:-}"
  fi
}

# ---------------------------------------------------------------------------------------------
# Replication lag and RPO
# ---------------------------------------------------------------------------------------------
# metric_stat REGION CLUSTER_ID METRIC MINUTES FIELD ; FIELD is latest | max | avg ; prints a number, n/a (no
# datapoint in the window) or error (the CloudWatch query itself failed, for example a missing permission)
metric_stat() {
  local region="$1" cluster="$2" metric="$3" minutes="$4" field="$5" query out start end
  if is_dry_run; then
    printf 'n/a'
    return 0
  fi
  start="$(date -u -d "-${minutes} minutes" +%Y-%m-%dT%H:%M:%SZ)"
  end="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  case "${field}" in
    latest) query='sort_by(Datapoints,&Timestamp)[-1].Maximum' ;;
    max) query='max_by(Datapoints,&Maximum).Maximum' ;;
    *) query='sort_by(Datapoints,&Timestamp)[-1].Average' ;;
  esac
  out="$(aws cloudwatch get-metric-statistics --region "${region}" --namespace AWS/RDS --metric-name "${metric}" \
    --dimensions "Name=DBClusterIdentifier,Value=${cluster}" --start-time "${start}" --end-time "${end}" \
    --period 60 --statistics Maximum Average --query "${query}" --output text 2>/dev/null)" || out="error"
  if [[ -z "${out}" || "${out}" == "None" ]]; then
    out="n/a"
  fi
  printf '%s' "${out}"
}

# rpo_report SECONDARY_REGION ; replication lag of the cluster that is (or becomes) the reader
rpo_report() {
  local region="$1" cluster latest max rpo
  cluster="$(cluster_identifier "${region}")"
  if is_dry_run; then
    log "[dry-run] would read AuroraGlobalDBReplicationLag and AuroraGlobalDBRPOLag of ${cluster} in ${region} (CloudWatch)"
    return 0
  fi
  latest="$(metric_stat "${region}" "${cluster}" AuroraGlobalDBReplicationLag 15 latest)"
  max="$(metric_stat "${region}" "${cluster}" AuroraGlobalDBReplicationLag 60 max)"
  rpo="$(metric_stat "${region}" "${cluster}" AuroraGlobalDBRPOLag 60 max)"
  log "replication lag of ${cluster} in ${region} (CloudWatch AuroraGlobalDBReplicationLag, milliseconds):"
  log "  latest datapoint (15 min window): ${latest}"
  log "  maximum of the last 60 minutes:   ${max}"
  log "  AuroraGlobalDBRPOLag maximum (60 min): ${rpo} (only filled when rds.global_db_rpo is configured)"
  log "RPO reading: an unplanned failover now would lose at most the writes of the last ~${latest} ms (asynchronous"
  log "replication, not zero). A planned switchover waits for the secondary to catch up and loses nothing."
}

# aurora_global_db_status() gives the exact lag per secondary. It is a SQL function, so it needs a database session:
# the script runs it through the Data API only when that is enabled on the writer (it is normally disabled after the
# bootstrap, and it exists only on the original primary), otherwise it prints how to run it from an in-VPC session.
sql_status() {
  local writer_arn writer_region writer_id enabled secret output
  if is_dry_run; then
    log "[dry-run] would run aurora_global_db_status() through the Data API when it is enabled, else print the SQL to run in-VPC"
    return 0
  fi
  writer_arn="$(jq -r '[.GlobalClusterMembers[] | select(.IsWriter)] | .[0].DBClusterArn // ""' <<<"${GLOBAL_JSON}")"
  writer_region="$(cut -d: -f4 <<<"${writer_arn}")"
  writer_id="${writer_arn##*:cluster:}"
  enabled="False"
  if [[ -n "${writer_arn}" && "${writer_region}" == "${AWS_REGION_PRIMARY}" ]]; then
    enabled="$(aws rds describe-db-clusters --region "${writer_region}" --db-cluster-identifier "${writer_id}" \
      --query 'DBClusters[0].HttpEndpointEnabled' --output text 2>/dev/null)" || enabled="False"
  fi
  if [[ "${enabled}" == "True" ]]; then
    secret="$(stack_output "${AWS_REGION_PRIMARY}" "$(stack_name "${AWS_REGION_PRIMARY}" database)" MasterSecretArn 2>/dev/null)" || secret=""
    if [[ -n "${secret}" ]] && output="$(aws rds-data execute-statement --region "${writer_region}" --resource-arn "${writer_arn}" \
      --secret-arn "${secret}" --database drdemo --sql "SELECT aws_region, durability_lag_in_msec, rpo_lag_in_msec FROM aurora_global_db_status()" \
      --output json 2>/dev/null)"; then
      log "aurora_global_db_status() (aws_region, durability_lag_in_msec, rpo_lag_in_msec):"
      jq -r '.records[] | map(.stringValue // .longValue // "null") | "  " + join("  ")' <<<"${output}"
      return 0
    fi
  fi
  log "aurora_global_db_status() was not run (the Data API is disabled after the bootstrap, as designed). For the exact lag per"
  log "secondary run on the WRITER, from an in-VPC session:  SELECT * FROM aurora_global_db_status();"
}

# ---------------------------------------------------------------------------------------------
# DNS weights (dns stack in the global region)
# ---------------------------------------------------------------------------------------------
dns_stack_name() { stack_name "${AWS_REGION_GLOBAL}" dns; }

dns_weights_report() {
  if is_dry_run; then
    log "[dry-run] would read the current PrimaryWeight and SecondaryWeight of $(dns_stack_name) in ${AWS_REGION_GLOBAL}"
    return 0
  fi
  local pw sw
  pw="$(stack_param "${AWS_REGION_GLOBAL}" "$(dns_stack_name)" PrimaryWeight)"
  sw="$(stack_param "${AWS_REGION_GLOBAL}" "$(dns_stack_name)" SecondaryWeight)"
  [[ -n "${pw}" ]] || die "stack $(dns_stack_name) not found in ${AWS_REGION_GLOBAL} (deploy it first)"
  log "dns weights: ${AWS_REGION_PRIMARY} (record use2)=${pw}  ${AWS_REGION_SECONDARY} (record usw2)=${sw}"
}

# dns_apply PRIMARY_WEIGHT SECONDARY_WEIGHT ; update of the existing dns stack, all other parameters kept
dns_apply() {
  local pw="$1" sw="$2" stack params
  stack="$(dns_stack_name)"
  local -a overrides=() args
  if is_dry_run; then
    overrides=("<current parameters kept>" "PrimaryWeight=${pw}" "SecondaryWeight=${sw}")
  else
    params="$(aws cloudformation describe-stacks --region "${AWS_REGION_GLOBAL}" --stack-name "${stack}" --query 'Stacks[0].Parameters' --output json 2>/dev/null)" ||
      die "stack ${stack} not found in ${AWS_REGION_GLOBAL}"
    mapfile -t overrides < <(jq -r '.[] | select(.ParameterKey != "PrimaryWeight" and .ParameterKey != "SecondaryWeight") | "\(.ParameterKey)=\(.ParameterValue)"' <<<"${params}")
    overrides+=("PrimaryWeight=${pw}" "SecondaryWeight=${sw}")
  fi
  args=(cloudformation deploy --region "${AWS_REGION_GLOBAL}" --stack-name "${stack}"
    --template-file "${REPO_ROOT}/templates/dns.yaml" --no-fail-on-empty-changeset --tags "${TAGS[@]}")
  if [[ -n "${CFN_EXEC_ROLE_ARN:-}" ]]; then
    args+=(--role-arn "${CFN_EXEC_ROLE_ARN}")
  fi
  args+=(--parameter-overrides "${overrides[@]}")
  assert_stack_deployable "${AWS_REGION_GLOBAL}" "${stack}"
  aws_write "${args[@]}"
}

# weights_for_region REGION -> "PRIMARY SECONDARY" weights that send all traffic to REGION
weights_for_region() {
  if [[ "$1" == "${AWS_REGION_PRIMARY}" ]]; then
    printf '255 0'
  elif [[ "$1" == "${AWS_REGION_SECONDARY}" ]]; then
    printf '0 255'
  else
    die "region $1 is neither AWS_REGION_PRIMARY nor AWS_REGION_SECONDARY"
  fi
}

# ---------------------------------------------------------------------------------------------
# Cognito Multi-Region Replication attachment status (spec 002, requirements 6.1 and 6.4)
# ---------------------------------------------------------------------------------------------
# cognito_replica_set_status ACTIVE|INACTIVE
# Updates the regional configuration attachment in AWS_REGION_SECONDARY (us-west-2) via CloudFormation
# so the replica serves authentication during failover (ACTIVE) and returns to read-only standby on switchback (INACTIVE).
cognito_replica_set_status() {
  local desired="$1" stack params
  [[ "${desired}" == "ACTIVE" || "${desired}" == "INACTIVE" ]] || die "cognito_replica_set_status requires ACTIVE or INACTIVE"
  stack="$(stack_name "${AWS_REGION_SECONDARY}" cognito)"
  local -a overrides=() args
  if is_dry_run; then
    overrides=("<current parameters kept>" "ReplicaAttachmentStatus=${desired}")
  else
    params="$(aws cloudformation describe-stacks --region "${AWS_REGION_SECONDARY}" --stack-name "${stack}" --query 'Stacks[0].Parameters' --output json 2>/dev/null)" ||
      die "stack ${stack} not found in ${AWS_REGION_SECONDARY}"
    mapfile -t overrides < <(jq -r '.[] | select(.ParameterKey != "ReplicaAttachmentStatus") | "\(.ParameterKey)=\(.ParameterValue)"' <<<"${params}")
    overrides+=("ReplicaAttachmentStatus=${desired}")
  fi
  args=(cloudformation deploy --region "${AWS_REGION_SECONDARY}" --stack-name "${stack}"
    --template-file "${REPO_ROOT}/templates/cognito.yaml" --no-fail-on-empty-changeset --tags "${TAGS[@]}")
  if [[ -n "${CFN_EXEC_ROLE_ARN:-}" ]]; then
    args+=(--role-arn "${CFN_EXEC_ROLE_ARN}")
  fi
  args+=(--parameter-overrides "${overrides[@]}")
  assert_stack_deployable "${AWS_REGION_SECONDARY}" "${stack}"
  log "setting Cognito replica regional attachment in ${AWS_REGION_SECONDARY} (${stack}) to ReplicaAttachmentStatus=${desired}"
  aws_write "${args[@]}"
}

# ---------------------------------------------------------------------------------------------
# API calls on the custom domain (token never appears in the process list or in the output)
# ---------------------------------------------------------------------------------------------
API_HEADERS_FILE=""
API_BODY_FILE=""

api_init() {
  require_env API_DOMAIN_NAME
  if [[ -z "${COGNITO_ID_TOKEN:-}" ]]; then
    die "COGNITO_ID_TOKEN is required"
  fi
  validate_domain
  require_cmds curl jq
  local dir
  dir="$(mktemp -d)"
  API_HEADERS_FILE="${dir}/headers"
  API_BODY_FILE="${dir}/body"
  : >"${API_HEADERS_FILE}"
  : >"${API_BODY_FILE}"
}

api_cleanup() {
  if [[ -n "${API_HEADERS_FILE}" ]]; then
    rm -rf "$(dirname "${API_HEADERS_FILE}")"
  fi
}

# api_call METHOD PATH [JSON_BODY] [IDEMPOTENCY_KEY] ; prints the HTTP status ("000" on connection failure)
api_call() {
  local method="$1" path="$2" body="${3:-}" idem="${4:-}" code auth_hdr
  local -a args=(--silent --show-error --max-time 20 --proto '=https' --tlsv1.2 --request "${method}"
    --dump-header "${API_HEADERS_FILE}" --output "${API_BODY_FILE}" --write-out '%{http_code}' --config -)
  if [[ -n "${body}" ]]; then
    args+=(--header 'Content-Type: application/json' --data "${body}")
  fi
  if [[ -n "${idem}" ]]; then
    args+=(--header "Idempotency-Key: ${idem}")
  fi
  # Credentials go to curl through stdin (printf is a shell builtin), never through the argument list.
  auth_hdr="Authorization: Bearer ${COGNITO_ID_TOKEN}"
  code="$(printf 'header = "%s"\n' "${auth_hdr}" | curl "${args[@]}" "https://${API_DOMAIN_NAME}${path}" 2>/dev/null)" || code="000"
  printf '%s' "${code}"
}

# api_header NAME ; value of a response header of the last call
api_header() {
  grep -i "^$1:" "${API_HEADERS_FILE}" | tail -n 1 | cut -d: -f2- | tr -d '\r' | sed 's/^ *//' || true
}
