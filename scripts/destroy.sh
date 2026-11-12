#!/usr/bin/env bash
# destroy.sh - remove everything the demo created, in a safe order (requirements 8.5 and 8.6).
#
# Order (security design C26-C29, spec 002 requirement 1.9):
#   0. typed confirmation "destroy-aws-multi-region-aurora-failover-active-passive";
#   1. FIRST, confirmed separately ("disable-deletion-protection"): deletion protection off on both clusters and on the
#      global cluster (CLI; the stacks keep DeletionProtection=true on purpose so that nothing else can delete them);
#   2. dns stack, 3. api stacks (both regions at once), then cognito stacks (lift DeletionProtection on primary pool,
#      delete replica stack in us-west-2 first, then primary stack in us-east-2, and schedule KMS key deletion with minimum 7-day window);
#   4. read the LIVE roles (never assume the primary region is the writer), detach EVERY member of the global
#      cluster with remove-from-global-cluster, non-writers first, the writer last (the global cluster must be empty to
#      be deleted; roles may have swapped during drills);
#   5. per data region the database stack (FINAL SNAPSHOT, DeletionPolicy Snapshot) and then its network stack, both
#      regions at once once every member is detached (still attached: one region after the other, former secondary first);
#      when step 6 is selected in the same run, the edge stack is deleted at the same time;
#   6. edge: the dashboard and banking buckets are both emptied INCLUDING every version and delete marker, then the stack is deleted;
#   7. artifact buckets emptied (all versions) and the bootstrap stacks of the two data regions deleted (both at once);
#   8. leftovers are listed with the commands to delete them by hand.
# The bootstrap stack of the global region (deploy and execution roles) is NOT deleted: the workflow
# runs under that role. Its artifact bucket is emptied; delete that stack by hand with the owner's credentials.
#
# Safe to repeat: missing stacks are skipped. A stack that fails to delete is reported and the script stops (it never
# retries or forces). Lambda network interfaces can delay the deletion of the VPC for some minutes: rerun if needed.
# UNDO:    none. Final snapshots are the only way back, and their KMS key is deleted after 30 days.
# Usage:   scripts/destroy.sh [--dry-run] [--confirm destroy-aws-multi-region-aurora-failover-active-passive] [--confirm-deletion-protection disable-deletion-protection]
#          [--eni-wait-seconds N] [--steps 1-8]
#          Without the --confirm flags the phrases are typed interactively. The workflow passes them as flags.
#          --steps limits the run to some of the eight steps (for example 1-3, 4-6, 7-8 or 4): the workflow runs
#          them in three groups because the deploy role session lasts one hour. Every group re-reads the live state.
# Needs:   AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL (optional: HOSTED_ZONE_ID to list ACM records)
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
# shellcheck source=lib/dr.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/dr.sh"

readonly CONFIRM_PHRASE="destroy-aws-multi-region-aurora-failover-active-passive"
readonly PROTECTION_PHRASE="disable-deletion-protection"
confirm_arg=""
protection_arg=""
eni_wait=180
eni_wait_given=0
steps_arg="1-8"
while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
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
      eni_wait="${1:-}"
      eni_wait_given=1
      ;;
    --steps)
      shift
      steps_arg="${1:-}"
      ;;
    -h | --help)
      sed -n '2,/^set -euo pipefail/{/^set -euo/!p;}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
[[ "${eni_wait}" =~ ^[0-9]+$ ]] || die "--eni-wait-seconds must be a number"

# Step selection: "N", "N-M" or a comma list of those.
declare -A selected=()
IFS=',' read -r -a step_specs <<<"${steps_arg}"
for spec in "${step_specs[@]}"; do
  if [[ "${spec}" =~ ^([1-8])-([1-8])$ && "${BASH_REMATCH[1]}" -le "${BASH_REMATCH[2]}" ]]; then
    for ((n = BASH_REMATCH[1]; n <= BASH_REMATCH[2]; n++)); do selected["${n}"]=1; done
  elif [[ "${spec}" =~ ^[1-8]$ ]]; then
    selected["${spec}"]=1
  else
    die "--steps must be a step number 1-8, a range such as 1-3, or a comma list"
  fi
done
want_step() { [[ -n "${selected[$1]:-}" ]]; }

dr_load_config
print_context

EXEC_ROLE_ARN=""
api_deleted=0
declare -a all_regions=("${AWS_REGION_PRIMARY}" "${AWS_REGION_SECONDARY}")

log "plan (in this order):"
log "  1. disable deletion protection on both clusters and the global cluster (separate confirmation)"
log "  2. delete stack $(stack_name "${AWS_REGION_GLOBAL}" dns)"
log "  3. delete stacks $(stack_name "${AWS_REGION_PRIMARY}" api) and $(stack_name "${AWS_REGION_SECONDARY}" api), then cognito stacks ($(stack_name "${AWS_REGION_SECONDARY}" cognito) before $(stack_name "${AWS_REGION_PRIMARY}" cognito))"
log "  4. read live roles, detach every global cluster member (non-writers first, writer last)"
log "  5. delete the database stacks (former secondary first): final snapshots are taken"
log "  6. empty the dashboard and banking buckets (all versions) and delete stack $(stack_name "${AWS_REGION_PRIMARY}" edge)"
log "  7. empty the artifact buckets (all versions) and delete the bootstrap stacks of ${AWS_REGION_PRIMARY} and ${AWS_REGION_SECONDARY}"
log "  8. list leftovers (final snapshots, KMS keys pending deletion, secret, log groups, ACM records)"
log "  the bootstrap stack of ${AWS_REGION_GLOBAL} is NOT deleted: remove it by hand with the owner's credentials"

confirm_typed "${CONFIRM_PHRASE}" "This DELETES the whole DR demo (databases, API, dashboard and banking sites, DNS records)." "${confirm_arg}"

# Execution role passed to CloudFormation (stored on the stacks anyway; explicit when the bootstrap stack is readable).
if ! is_dry_run; then
  if [[ -n "$(stack_status "${AWS_REGION_GLOBAL}" "$(stack_name "${AWS_REGION_GLOBAL}" bootstrap)")" ]]; then
    EXEC_ROLE_ARN="$(stack_output "${AWS_REGION_GLOBAL}" "$(stack_name "${AWS_REGION_GLOBAL}" bootstrap)" CfnExecRoleArn)"
  fi
fi

# delete_stack REGION STACK ; waits for completion; never retries or forces
delete_stack() {
  local region="$1" stack="$2" status
  local -a args=(cloudformation delete-stack --region "${region}" --stack-name "${stack}")
  [[ -z "${EXEC_ROLE_ARN}" ]] || args+=(--role-arn "${EXEC_ROLE_ARN}")
  if is_dry_run; then
    aws_write "${args[@]}"
    log "[dry-run] aws cloudformation wait stack-delete-complete --region ${region} --stack-name ${stack}"
    return 0
  fi
  status="$(stack_status "${region}" "${stack}")"
  if [[ -z "${status}" ]]; then
    log "stack ${stack} not found in ${region}: nothing to delete"
    return 0
  fi
  if [[ "${status}" != "DELETE_IN_PROGRESS" ]]; then
    aws_write "${args[@]}"
  fi
  log "waiting for ${stack} to be deleted (can take several minutes)"
  if ! aws cloudformation wait stack-delete-complete --region "${region}" --stack-name "${stack}" 2>/dev/null; then
    warn "stack ${stack} did not delete. Failed resources:"
    aws cloudformation describe-stack-events --region "${region}" --stack-name "${stack}" \
      --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" --output text 2>&1 | sanitize >&2 || true
    die "fix the cause and run the script again (it does not retry or force deletions)"
  fi
  log "stack ${stack} deleted"
}

cluster_exists() { # region
  aws rds describe-db-clusters --region "$1" --db-cluster-identifier "$(cluster_identifier "$1")" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------------------------
log "reading the live state of the global cluster (read-only)"
GLOBAL_FOUND=0
if discover_global_cluster soft; then
  GLOBAL_FOUND=1
  print_members
else
  log "no global cluster with the project clusters was found (already detached or never created)"
fi

if want_step 1; then
  log "step 1/8: deletion protection (explicit, confirmed)"
  confirm_typed "${PROTECTION_PHRASE}" "Deletion protection will be switched OFF on both clusters and on the global cluster." "${protection_arg}"
  for region in "${all_regions[@]}"; do
    if is_dry_run || cluster_exists "${region}"; then
      aws_write rds modify-db-cluster --region "${region}" --db-cluster-identifier "$(cluster_identifier "${region}")" \
        --no-deletion-protection --apply-immediately --query 'DBCluster.DeletionProtection' --output text
    else
      log "cluster $(cluster_identifier "${region}") not found in ${region}: skipped"
    fi
  done
  if is_dry_run; then
    aws_write rds modify-global-cluster --region "${AWS_REGION_PRIMARY}" --global-cluster-identifier "<global-cluster-id>" --no-deletion-protection
  elif ((GLOBAL_FOUND == 1)); then
    aws_write rds modify-global-cluster --region "${AWS_REGION_PRIMARY}" --global-cluster-identifier "${GLOBAL_ID}" --no-deletion-protection ||
      aws_write rds modify-global-cluster --region "${AWS_REGION_SECONDARY}" --global-cluster-identifier "${GLOBAL_ID}" --no-deletion-protection
  fi

fi

if want_step 2; then
  log "step 2/8: dns stack"
  delete_stack "${AWS_REGION_GLOBAL}" "$(stack_name "${AWS_REGION_GLOBAL}" dns)"

fi

# Jobs for run_parallel (lib/common.sh): one argument each, independent of each other.
delete_api_region() { delete_stack "$1" "$(stack_name "$1" api)"; }
# delete_data_region REGION ; database stack, then the network stack of the same region (the VPC needs the database gone)
delete_data_region() {
  delete_stack "$1" "$(stack_name "$1" database)"
  delete_stack "$1" "$(stack_name "$1" network)"
}
delete_edge() {
  local kind
  ensure_account
  for kind in "${SITE_BUCKET_DESCRIPTORS[@]}"; do
    log "bucket ${kind}: $(bucket_name "${AWS_REGION_PRIMARY}" "${kind}" | sanitize)"
    empty_bucket "${AWS_REGION_PRIMARY}" "$(bucket_name "${AWS_REGION_PRIMARY}" "${kind}")"
  done
  delete_stack "${AWS_REGION_PRIMARY}" "$(stack_name "${AWS_REGION_PRIMARY}" edge)"
}
# delete_bootstrap_region REGION ; artifact bucket emptied (all versions), then the bootstrap stack of a data region
delete_bootstrap_region() {
  empty_bucket "$1" "$(bucket_name "$1" artifacts)"
  delete_stack "$1" "$(stack_name "$1" bootstrap)"
}
edge_deleted=0

if want_step 3; then
  log "step 3/8: api stacks (both regions at once), then cognito stacks (replica before primary)"
  for region in "${all_regions[@]}"; do
    if [[ -n "$(stack_status "${region}" "$(stack_name "${region}" api)")" ]]; then
      api_deleted=1
    fi
  done
  run_parallel "${AWS_REGION_PRIMARY}" delete_api_region "${AWS_REGION_PRIMARY}" \
    "${AWS_REGION_SECONDARY}" delete_api_region "${AWS_REGION_SECONDARY}"

  # Cognito teardown (requirement 1.9): after API stacks are gone, lift DeletionProtection on primary user pool,
  # deactivate replica attachment in us-west-2, delete replica stack in us-west-2, then delete primary stack in us-east-2,
  # and schedule any remaining Cognito KMS keys for deletion with the minimum 7-day window.
  stack_cog_primary="$(stack_name "${AWS_REGION_PRIMARY}" cognito)"
  stack_cog_secondary="$(stack_name "${AWS_REGION_SECONDARY}" cognito)"
  if is_dry_run; then
    aws_write cognito-idp update-user-pool --region "${AWS_REGION_PRIMARY}" --user-pool-id "<user-pool-id>" --deletion-protection INACTIVE
    CFN_EXEC_ROLE_ARN="${EXEC_ROLE_ARN}" cognito_replica_set_status INACTIVE
    delete_stack "${AWS_REGION_SECONDARY}" "${stack_cog_secondary}"
    delete_stack "${AWS_REGION_PRIMARY}" "${stack_cog_primary}"
    aws_write kms schedule-key-deletion --region "${AWS_REGION_SECONDARY}" --key-id "<replica-kms-key-id>" --pending-window-in-days 7
    aws_write kms schedule-key-deletion --region "${AWS_REGION_PRIMARY}" --key-id "<primary-kms-key-id>" --pending-window-in-days 7
  else
    pool_id=""
    if [[ -n "$(stack_status "${AWS_REGION_PRIMARY}" "${stack_cog_primary}")" ]]; then
      pool_id="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_cog_primary}" UserPoolId 2>/dev/null)" || pool_id=""
    fi
    if [[ -z "${pool_id}" ]]; then
      pool_id="$(aws ssm get-parameter --region "${AWS_REGION_PRIMARY}" --name "/drdemo/${ENV_TYPE}/cognito/UserPoolId" --query Parameter.Value --output text 2>/dev/null)" || pool_id=""
    fi
    if [[ -n "${pool_id}" && "${pool_id}" != "None" ]]; then
      log "lifting deletion protection on Cognito User Pool ${pool_id} in ${AWS_REGION_PRIMARY}"
      aws_write cognito-idp update-user-pool --region "${AWS_REGION_PRIMARY}" --user-pool-id "${pool_id}" --deletion-protection INACTIVE || true
    fi
    if [[ -n "$(stack_status "${AWS_REGION_SECONDARY}" "${stack_cog_secondary}")" ]]; then
      log "ensuring Cognito replica regional attachment is INACTIVE in ${AWS_REGION_SECONDARY}"
      CFN_EXEC_ROLE_ARN="${EXEC_ROLE_ARN}" cognito_replica_set_status INACTIVE || true
    fi
    primary_kms_arn=""
    replica_kms_arn=""
    if [[ -n "$(stack_status "${AWS_REGION_SECONDARY}" "${stack_cog_secondary}")" ]]; then
      replica_kms_arn="$(stack_output "${AWS_REGION_SECONDARY}" "${stack_cog_secondary}" ReplicaKmsKeyArn 2>/dev/null)" || replica_kms_arn=""
    fi
    if [[ -n "$(stack_status "${AWS_REGION_PRIMARY}" "${stack_cog_primary}")" ]]; then
      primary_kms_arn="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_cog_primary}" KmsKeyArn 2>/dev/null)" || primary_kms_arn=""
    fi
    delete_stack "${AWS_REGION_SECONDARY}" "${stack_cog_secondary}"
    delete_stack "${AWS_REGION_PRIMARY}" "${stack_cog_primary}"
    for pair in "${AWS_REGION_SECONDARY}:${replica_kms_arn}" "${AWS_REGION_PRIMARY}:${primary_kms_arn}"; do
      k_reg="${pair%%:*}"
      k_arn="${pair#*:}"
      if [[ -n "${k_arn}" && "${k_arn}" != "None" ]]; then
        k_state="$(aws kms describe-key --region "${k_reg}" --key-id "${k_arn}" --query 'KeyMetadata.KeyState' --output text 2>/dev/null)" || k_state=""
        if [[ -n "${k_state}" && "${k_state}" != "PendingDeletion" ]]; then
          aws_write kms schedule-key-deletion --region "${k_reg}" --key-id "${k_arn}" --pending-window-in-days 7 || true
        fi
      fi
    done
  fi

fi

if want_step 4; then
  log "step 4/8: detach every global cluster member (live roles)"
  writer_region="${AWS_REGION_PRIMARY}"
  former_secondary_region="${AWS_REGION_SECONDARY}"
  if is_dry_run; then
    log "[dry-run] would read the live writer with describe-global-clusters, then for each non-writer member:"
    log "[dry-run] aws rds remove-from-global-cluster --region <member region> --global-cluster-identifier <id> --db-cluster-identifier <member arn>"
    log "[dry-run] (wait until the member is gone), then the same for the writer, last"
  elif ((GLOBAL_FOUND == 1)); then
    discover_global_cluster soft || true # fresh roles after step 1
    writer_arn="$(jq -r '[.GlobalClusterMembers[] | select(.IsWriter)] | .[0].DBClusterArn // ""' <<<"${GLOBAL_JSON}")"
    mapfile -t nonwriters < <(jq -r '.GlobalClusterMembers[] | select(.IsWriter | not) | .DBClusterArn' <<<"${GLOBAL_JSON}")
    if [[ -n "${writer_arn}" ]]; then
      writer_region="$(cut -d: -f4 <<<"${writer_arn}")"
    fi
    if [[ "${writer_region}" == "${AWS_REGION_PRIMARY}" ]]; then
      former_secondary_region="${AWS_REGION_SECONDARY}"
    else
      former_secondary_region="${AWS_REGION_PRIMARY}"
    fi
    log "live writer region: ${writer_region} (the original roles may have swapped)"

    # detach_member ARN ; returns 1 when the API refuses
    detach_member() {
      local arn="$1" member_region tries=0
      member_region="$(cut -d: -f4 <<<"${arn}")"
      aws_write rds remove-from-global-cluster --region "${member_region}" --global-cluster-identifier "${GLOBAL_ID}" \
        --db-cluster-identifier "${arn}" || return 1
      while ((tries < 40)); do
        discover_global_cluster soft || return 0 # global cluster already gone
        if [[ "$(member_state "${arn}")" == "absent" ]]; then
          log "  member in ${member_region} detached"
          return 0
        fi
        tries=$((tries + 1))
        sleep 15
      done
      return 1
    }
    for arn in "${nonwriters[@]}"; do
      [[ -n "${arn}" ]] || continue
      detach_member "${arn}" || die "could not detach the non-writer member in $(cut -d: -f4 <<<"${arn}"); nothing else was deleted after this point"
    done
    if [[ -n "${writer_arn}" ]]; then
      log "detaching the writer last, so that the global cluster is empty and can be deleted with its stack"
      detach_member "${writer_arn}" || warn "the writer could not be detached; the stack deletion will show whether CloudFormation can remove the last member"
    fi
  else
    log "no global cluster membership found: nothing to detach"
  fi

fi

if want_step 5; then
  log "step 5/8: database stacks (former secondary first, then the former writer), then the network stacks"
  if (((api_deleted == 1) || (eni_wait_given == 1))) && ((eni_wait > 0)) && ! is_dry_run; then
    log "pausing ${eni_wait} s: Lambda network interfaces of the deleted functions are released asynchronously and can block the VPC deletion"
    sleep "${eni_wait}"
  elif is_dry_run; then
    log "[dry-run] would pause ${eni_wait} s after deleting the api stacks (Lambda network interfaces) before the database stacks"
  fi
  # With both clusters detached (no global cluster holds a project cluster any more) the two regions are independent:
  # each deletes its database and then its network at the same time as the other, and the edge (step 6, when selected)
  # runs alongside. If a member is still attached (the writer could not be detached in step 4), the original order is
  # kept: former secondary first, then the former writer, one after the other.
  if is_dry_run || ! discover_global_cluster soft; then
    data_jobs=("${AWS_REGION_PRIMARY}" delete_data_region "${AWS_REGION_PRIMARY}"
      "${AWS_REGION_SECONDARY}" delete_data_region "${AWS_REGION_SECONDARY}")
    if want_step 6; then
      log "the edge stack (step 6) is deleted at the same time"
      data_jobs+=(edge delete_edge -)
      edge_deleted=1
    fi
    run_parallel "${data_jobs[@]}"
  else
    warn "a cluster is still a member of ${GLOBAL_ID}: deleting the regions one after the other (former secondary first)"
    delete_data_region "${former_secondary_region}"
    delete_data_region "${writer_region}"
  fi

fi

if want_step 6; then
  if ((edge_deleted == 1)); then
    log "step 6/8: edge already deleted together with step 5"
  else
    log "step 6/8: edge (dashboard and banking buckets emptied including all versions)"
    delete_edge
  fi

fi

if want_step 7; then
  log "step 7/8: artifact buckets and bootstrap stacks (both data regions at once)"
  ensure_account
  empty_bucket "${AWS_REGION_GLOBAL}" "$(bucket_name "${AWS_REGION_GLOBAL}" artifacts)"
  run_parallel "${AWS_REGION_PRIMARY}" delete_bootstrap_region "${AWS_REGION_PRIMARY}" \
    "${AWS_REGION_SECONDARY}" delete_bootstrap_region "${AWS_REGION_SECONDARY}"

# ---------------------------------------------------------------------------------------------
fi

if want_step 8; then
  log "step 8/8: leftovers to review and delete by hand (listing is best effort: a missing permission is only a warning)"
  if is_dry_run; then
    log "[dry-run] would check that the dashboard and banking buckets of ${AWS_REGION_PRIMARY} are gone, and list in ${AWS_REGION_PRIMARY} and ${AWS_REGION_SECONDARY}: final cluster snapshots, KMS keys pending deletion, the RDS master secret, log groups; and the ACM validation records of the hosted zone"
  else
    ensure_account soft
    for kind in "${SITE_BUCKET_DESCRIPTORS[@]}"; do
      site_bucket="$(bucket_name "${AWS_REGION_PRIMARY}" "${kind}")"
      if aws s3api head-bucket --region "${AWS_REGION_PRIMARY}" --bucket "${site_bucket}" >/dev/null 2>&1; then
        warn "the ${kind} bucket still exists: $(sanitize <<<"${site_bucket}") (the edge stack was not deleted; rerun step 6)"
      else
        log "-- ${AWS_REGION_PRIMARY}: ${kind} bucket is gone"
      fi
    done
    for region in "${all_regions[@]}"; do
      log "-- ${region}: final cluster snapshots (usable only until their KMS key is deleted, 30 days)"
      aws rds describe-db-cluster-snapshots --region "${region}" --snapshot-type manual \
        --query "DBClusterSnapshots[?contains(DBClusterSnapshotIdentifier, 'drdemo')].[DBClusterSnapshotIdentifier,Status]" --output text 2>&1 |
        sanitize | while IFS= read -r line; do
        [[ -n "${line}" ]] && log "   ${line%%[[:space:]]*}  delete: aws rds delete-db-cluster-snapshot --region ${region} --db-cluster-snapshot-identifier ${line%%[[:space:]]*}"
      done || warn "could not list snapshots in ${region}"
      log "-- ${region}: KMS keys of the project (tag project-name=aws-multi-region-aurora-failover-active-passive)"
      keys="$(aws resourcegroupstaggingapi get-resources --region "${region}" --resource-type-filters kms:key \
        --tag-filters Key=project-name,Values=aws-multi-region-aurora-failover-active-passive --query 'ResourceTagMappingList[].ResourceARN' --output text 2>/dev/null)" || keys=""
      if [[ -z "${keys}" ]]; then
        log "   none found or not listable"
      else
        for key in ${keys}; do
          log "   $(aws kms describe-key --region "${region}" --key-id "${key}" --query 'KeyMetadata.[KeyState,DeletionDate]' --output text 2>/dev/null | tr '\t' ' ') (key id ${key##*/}); cancel the deletion with: aws kms cancel-key-deletion --region ${region} --key-id ${key##*/}"
        done
      fi
      log "-- ${region}: log groups (ones left behind keep their retention; check before deleting, some prefixes are shared)"
      for prefix in "/aws/lambda/fnc-$(region_code "${region}")-drdemo-" "/aws/rds/cluster/rdsc-$(region_code "${region}")-drdemo-" \
        "/aws/apigateway/apig-$(region_code "${region}")-drdemo-" "API-Gateway-Execution-Logs_"; do
        aws logs describe-log-groups --region "${region}" --log-group-name-prefix "${prefix}" --query 'logGroups[].logGroupName' --output text 2>/dev/null |
          tr '\t' '\n' | while IFS= read -r group; do
          [[ -n "${group}" ]] && log "   ${group}  delete: aws logs delete-log-group --region ${region} --log-group-name '${group}'"
        done || true
      done
    done
    log "-- ${AWS_REGION_PRIMARY}: RDS managed master secret (normally removed with the cluster; shows planned deletions too)"
    aws secretsmanager list-secrets --region "${AWS_REGION_PRIMARY}" --include-planned-deletion --filters Key=name,Values=rds!cluster \
      --query 'SecretList[].[Name,DeletedDate]' --output text 2>&1 | sanitize | while IFS= read -r line; do
      [[ -n "${line}" ]] && log "   ${line}"
    done || warn "could not list secrets"
    if [[ -n "${HOSTED_ZONE_ID:-}" ]]; then
      log "-- hosted zone: ACM DNS validation records are not removed by CloudFormation"
      aws route53 list-resource-record-sets --hosted-zone-id "${HOSTED_ZONE_ID}" \
        --query "ResourceRecordSets[?Type=='CNAME' && contains(ResourceRecords[0].Value, 'acm-validations.aws')].Name" --output text 2>/dev/null |
        tr '\t' '\n' | while IFS= read -r name; do
        [[ -n "${name}" ]] && log "   ${name}  (delete the record with the owner's credentials once the certificates are gone)"
      done || warn "could not list the hosted zone records"
    fi
  fi
fi

log "manual items that remain:"
log "  - stack $(stack_name "${AWS_REGION_GLOBAL}" bootstrap) in ${AWS_REGION_GLOBAL} (deploy and execution roles): delete it by hand with the owner's credentials; its artifact bucket is already empty"
log "  - API Gateway account setting of ${AWS_REGION_PRIMARY} and ${AWS_REGION_SECONDARY}: kept on purpose (it belongs to the account baseline, repository nubesegura-accounts-baseline, and other APIs of the account use it)"
log "  - the GitHub variables and secrets of the prod environment, if the demo is over"
log "destroy finished"
