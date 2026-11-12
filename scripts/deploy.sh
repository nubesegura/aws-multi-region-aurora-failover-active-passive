#!/usr/bin/env bash
# deploy.sh - ordered deployment of every stack (requirement 8.2). Used by the deploy workflow and by the owner.
#
# Order (security design 3.13, spec 002): bootstrap (artifact bucket of the two data regions) -> edge -> cognito
# (two passes: primary User Pool, KMS MRK and App Clients in us-east-2, then the KMS ReplicaKey in us-west-2, then the
# UserPoolReplica in us-east-2 and the regional attachment and SSM outputs in us-west-2, then deletion protection ACTIVE)
# -> network (VPC of both data regions) -> database primary -> database secondary ->
# schema bootstrap (Data API on, SQL, Data API off) -> api (both regions, receiving regional Cognito parameters from SSM) ->
# dns -> publish of the two static sites (monitoring dashboard and banking app; the step named dashboard runs both publishers).
# Values move between stacks as parameters read from stack outputs or SSM (no cross-region exports).
#
# One-time owner step, not done here: the bootstrap stack of the global region (us-east-1; deploy role and CloudFormation
# execution role; deploy templates/bootstrap.yaml with CreateDeployRole=true). This script verifies it exists and passes
# its execution role to CloudFormation (--role-arn). The dns stack also stays in the global region (ADR 0003): the Route 53
# control plane is reached through us-east-1, so a weight switch survives a real us-east-2 outage.
# Account baseline, not part of this project: the GitHub OIDC provider and the API Gateway CloudWatch Logs role of
# each region come from the repository nubesegura-accounts-baseline. The api step checks that the role is set.
#
# Safe to repeat. It never redeploys the database stacks after the roles swapped (drill/failover): it checks the
# live writer first and skips them with a warning (design C19, T-DRIFT). The dns weights of an existing dns stack are
# kept (a repeat deploy never moves traffic back by itself).
#
# UNDO:    scripts/destroy.sh removes everything. A single stack can be rolled back by redeploying the previous commit.
# Usage:   scripts/deploy.sh [--dry-run] [--steps bootstrap,edge,cognito,network,db-primary,db-secondary,schema,api,dns,dashboard]
#          (default: all steps in that order; the workflow runs them in groups so that each group fits one role session)
# Variables (GitHub variables unless noted):
#   AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL API_DOMAIN_NAME HOSTED_ZONE_ID
#   GITHUB_REPOSITORY owner/repo (set by GitHub Actions; bootstrap step)
#   DB_INSTANCE_CLASS ENGINE_VERSION ALERT_EMAIL (optional; defaults come from parameters/prod.json)
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

SCRIPT_DIR="${REPO_ROOT}/scripts"
readonly -a ALL_STEPS=(bootstrap edge cognito network db-primary db-secondary schema api dns dashboard)
steps_arg=""
while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
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

declare -A selected=()
if [[ -z "${steps_arg}" ]]; then
  for step in "${ALL_STEPS[@]}"; do selected["${step}"]=1; done
else
  IFS=',' read -r -a requested <<<"${steps_arg}"
  for step in "${requested[@]}"; do
    valid=0
    for known in "${ALL_STEPS[@]}"; do [[ "${step}" == "${known}" ]] && valid=1; done
    ((valid == 1)) || die "unknown step '${step}' (valid: ${ALL_STEPS[*]})"
    selected["${step}"]=1
  done
fi
want() { [[ -n "${selected[$1]:-}" ]]; }

# ---------------------------------------------------------------------------------------------
# Configuration (validated before anything is used)
# ---------------------------------------------------------------------------------------------
require_cmds jq
is_dry_run || require_cmds aws
require_env AWS_REGION_PRIMARY AWS_REGION_SECONDARY AWS_REGION_GLOBAL API_DOMAIN_NAME HOSTED_ZONE_ID
validate_regions
validate_domain
validate_zone
if want api; then
  is_dry_run || require_cmds sam
fi
if want bootstrap; then
  require_env GITHUB_REPOSITORY
  [[ "${GITHUB_REPOSITORY}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "GITHUB_REPOSITORY must look like owner/repo"
fi

declare -A db_common=()
load_params db_common '.database.Common'
db_class="${DB_INSTANCE_CLASS:-${db_common[DbInstanceClass]}}"
engine_version="${ENGINE_VERSION:-${db_common[EngineVersion]}}"
[[ "${db_class}" =~ ^db\.[a-z0-9]+\.[a-z0-9]+$ ]] || die "DB_INSTANCE_CLASS must look like db.r6g.large"
[[ "${engine_version}" =~ ^[0-9]+\.[0-9]+$ ]] || die "ENGINE_VERSION must look like 16.15"
if [[ -n "${ALERT_EMAIL:-}" ]]; then
  [[ "${ALERT_EMAIL}" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || die "ALERT_EMAIL is not an e-mail address"
fi

print_context
log "steps: $(for step in "${ALL_STEPS[@]}"; do want "${step}" && printf '%s ' "${step}"; done)"
log "database: class ${db_class}, engine aurora-postgresql ${engine_version}"

stack_primary_db="$(stack_name "${AWS_REGION_PRIMARY}" database)"
stack_secondary_db="$(stack_name "${AWS_REGION_SECONDARY}" database)"
stack_edge="$(stack_name "${AWS_REGION_PRIMARY}" edge)"
stack_cognito_primary="$(stack_name "${AWS_REGION_PRIMARY}" cognito)"
stack_cognito_secondary="$(stack_name "${AWS_REGION_SECONDARY}" cognito)"
stack_dns="$(stack_name "${AWS_REGION_GLOBAL}" dns)"
stack_boot_global="$(stack_name "${AWS_REGION_GLOBAL}" bootstrap)"
EXEC_ROLE_ARN=""
DATA_API_ENABLED=0

# Last line of defence for threat T-MASTER: if anything fails while the Data API is on, try to close it.
on_exit() {
  local rc=$?
  trap - EXIT
  if [[ "${DATA_API_ENABLED}" == "1" ]]; then
    warn "failure while the RDS Data API is enabled: trying to disable it now"
    if deploy_database primary false; then
      log "Data API disabled again"
    else
      warn "COULD NOT DISABLE THE DATA API. Run: scripts/deploy.sh --steps schema  (or deploy ${stack_primary_db} with EnableDataApi=false)."
    fi
  fi
  exit "${rc}"
}

# ---------------------------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------------------------
check_engine_and_class() {
  local region count supports
  for region in "${AWS_REGION_PRIMARY}" "${AWS_REGION_SECONDARY}"; do
    if is_dry_run; then
      log "[dry-run] would check in ${region}: engine aurora-postgresql ${engine_version} and class ${db_class} support Global Database"
      continue
    fi
    supports="$(aws rds describe-db-engine-versions --region "${region}" --engine aurora-postgresql --engine-version "${engine_version}" \
      --query 'DBEngineVersions[0].SupportsGlobalDatabases' --output text 2>&1 | sanitize)" || true
    [[ "${supports}" == "True" ]] || die "engine aurora-postgresql ${engine_version} is not available with Global Database in ${region} (answer: ${supports}). Pick another ENGINE_VERSION."
    count="$(aws rds describe-orderable-db-instance-options --region "${region}" --engine aurora-postgresql \
      --engine-version "${engine_version}" --db-instance-class "${db_class}" \
      --query 'length(OrderableDBInstanceOptions[?SupportsGlobalDatabases])' --output text 2>&1 | sanitize)" || true
    [[ "${count}" =~ ^[0-9]+$ && "${count}" -gt 0 ]] ||
      die "class ${db_class} with engine ${engine_version} does not support Global Database in ${region} (burstable db.t* classes never do). Try db.r5.large or check: aws rds describe-orderable-db-instance-options"
    log "  ${region}: engine ${engine_version} and ${db_class} support Global Database"
  done
}

load_global_context() {
  local status
  status="$(stack_status "${AWS_REGION_GLOBAL}" "${stack_boot_global}")"
  if ! is_dry_run; then
    case "${status}" in
      CREATE_COMPLETE | UPDATE_COMPLETE | UPDATE_ROLLBACK_COMPLETE) ;;
      *) die "bootstrap stack ${stack_boot_global} is missing or not healthy in ${AWS_REGION_GLOBAL} (state: ${status:-absent}). It is a one-time owner step: deploy templates/bootstrap.yaml there with CreateDeployRole=true (see the README)." ;;
    esac
  fi
  EXEC_ROLE_ARN="$(stack_output "${AWS_REGION_GLOBAL}" "${stack_boot_global}" CfnExecRoleArn)"
}

# ---------------------------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------------------------
# bootstrap_region REGION ; artifact bucket stack of one data region
bootstrap_region() {
  local region="$1" owner repo boot_stack bucket
  owner="${GITHUB_REPOSITORY%%/*}"
  repo="${GITHUB_REPOSITORY#*/}"
  boot_stack="$(stack_name "${region}" bootstrap)"
  # A template larger than the 51,200 byte inline limit goes through the artifact bucket that this same stack owns
  # (an update only: the first creation has no bucket yet, and cfn_deploy stops with a message then).
  bucket=""
  if is_dry_run || [[ -n "$(stack_status "${region}" "${boot_stack}")" ]]; then
    bucket="$(stack_output "${region}" "${boot_stack}" ArtifactBucketName)"
  fi
  cfn_deploy "${region}" "${boot_stack}" "${REPO_ROOT}/templates/bootstrap.yaml" CAPABILITY_NAMED_IAM "${EXEC_ROLE_ARN}" "${bucket}" \
    "EnvType=${ENV_TYPE}" "CreateDeployRole=false" "GitHubOrg=${owner}" "GitHubRepo=${repo}"
}

# The two data regions are independent: both bootstrap stacks are deployed at the same time.
step_bootstrap() {
  run_parallel "${AWS_REGION_PRIMARY}" bootstrap_region "${AWS_REGION_PRIMARY}" \
    "${AWS_REGION_SECONDARY}" bootstrap_region "${AWS_REGION_SECONDARY}"
  log "bootstrap of ${AWS_REGION_GLOBAL} (deploy role and execution role) is owner-managed and was only verified"
}

step_edge() {
  local -A params=()
  local -a overrides=()
  load_params params '.edge.Common'
  params[ApiDomainName]="${API_DOMAIN_NAME}"
  params[EnvType]="${ENV_TYPE}"
  params[ReplicaRegion]="${AWS_REGION_SECONDARY}"
  params_to_overrides params overrides
  cfn_deploy "${AWS_REGION_PRIMARY}" "${stack_edge}" "${REPO_ROOT}/templates/edge.yaml" "" "${EXEC_ROLE_ARN}" "" "${overrides[@]}"
}

# cognito_primary ENABLE_REPLICA PROTECTION ; the primary-region Cognito stack (AWS_REGION_PRIMARY, us-east-2)
cognito_primary() {
  local enable="$1" protection="$2"
  local -A params=()
  local -a overrides=()
  load_params params '.cognito.Common + .cognito.Primary'
  params[EnvType]="${ENV_TYPE}"
  params[PrimaryRegion]="${AWS_REGION_PRIMARY}"
  params[ReplicaRegion]="${AWS_REGION_SECONDARY}"
  params[EnableReplica]="${enable}"
  params[UserPoolDeletionProtection]="${protection}"
  [[ -n "${params[KeyAdminRoleArn]:-}" ]] || params[KeyAdminRoleArn]="${EXEC_ROLE_ARN}"
  params_to_overrides params overrides
  cfn_deploy "${AWS_REGION_PRIMARY}" "${stack_cognito_primary}" "${REPO_ROOT}/templates/cognito.yaml" "" "${EXEC_ROLE_ARN}" "" "${overrides[@]}"
}

# cognito_secondary ENABLE_REPLICA ; the replica-region Cognito stack (AWS_REGION_SECONDARY, us-west-2). It always
# creates the KMS ReplicaKey; the regional attachment and the SSM parameters need ENABLE_REPLICA=true.
cognito_secondary() {
  local enable="$1"
  local -A params=()
  local -a overrides=()
  load_params params '.cognito.Common + .cognito.Secondary'
  params[EnvType]="${ENV_TYPE}"
  params[PrimaryRegion]="${AWS_REGION_PRIMARY}"
  params[ReplicaRegion]="${AWS_REGION_SECONDARY}"
  params[EnableReplica]="${enable}"
  [[ -n "${params[KeyAdminRoleArn]:-}" ]] || params[KeyAdminRoleArn]="${EXEC_ROLE_ARN}"
  params[PrimaryKeyArn]="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_cognito_primary}" KmsKeyArn)"
  params[PrimaryUserPoolId]="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_cognito_primary}" UserPoolId)"
  params[BankClientId]="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_cognito_primary}" BankClientId)"
  params[DashboardClientId]="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_cognito_primary}" DashboardClientId)"
  params_to_overrides params overrides
  cfn_deploy "${AWS_REGION_SECONDARY}" "${stack_cognito_secondary}" "${REPO_ROOT}/templates/cognito.yaml" "" "${EXEC_ROLE_ARN}" "" "${overrides[@]}"
}

# step_cognito ; two passes so the KMS ReplicaKey exists in AWS_REGION_SECONDARY before the UserPoolReplica is created:
#   pass 1  primary EnableReplica=false (pool, MRK, clients, SSM) -> secondary EnableReplica=false (ReplicaKey, alias)
#   pass 2  primary EnableReplica=true (UserPoolReplica)          -> secondary EnableReplica=true (attachment INACTIVE, SSM)
#   step 3b primary UserPoolDeletionProtection=ACTIVE (steady state, COG-04); destroy.sh lifts it again.
# Pass 1 is skipped when the primary stack already has EnableReplica=true: going back to false would delete the replica.
step_cognito() {
  local current
  current="$(stack_param "${AWS_REGION_PRIMARY}" "${stack_cognito_primary}" EnableReplica)"
  if [[ "${current}" == "true" ]]; then
    log "cognito: replica already enabled in ${stack_cognito_primary}; skipping pass 1 (EnableReplica=false would delete the replica)"
  else
    log "cognito pass 1: ${AWS_REGION_PRIMARY} EnableReplica=false, then ${AWS_REGION_SECONDARY} EnableReplica=false (KMS ReplicaKey)"
    cognito_primary false INACTIVE
    cognito_secondary false
  fi
  log "cognito pass 2: ${AWS_REGION_PRIMARY} EnableReplica=true (UserPoolReplica), then ${AWS_REGION_SECONDARY} EnableReplica=true (attachment, SSM)"
  cognito_primary true INACTIVE
  cognito_secondary true
  log "cognito step 3b: ${AWS_REGION_PRIMARY} UserPoolDeletionProtection=ACTIVE"
  cognito_primary true ACTIVE
}

# network_region REGION ; network stack of one data region (VPC, two private subnets, route tables). Independent of
# the database roles: safe to update after a drill.
network_region() {
  local region="$1" key="Primary"
  local -A params=()
  local -a overrides=()
  [[ "${region}" == "${AWS_REGION_SECONDARY}" ]] && key="Secondary"
  load_params params ".network.Common + .network.${key}"
  params_to_overrides params overrides
  cfn_deploy "${region}" "$(stack_name "${region}" network)" "${REPO_ROOT}/templates/network.yaml" "" "${EXEC_ROLE_ARN}" "" "${overrides[@]}"
}

step_network() {
  run_parallel "${AWS_REGION_PRIMARY}" network_region "${AWS_REGION_PRIMARY}" \
    "${AWS_REGION_SECONDARY}" network_region "${AWS_REGION_SECONDARY}"
}

# The edge stack (primary region) and the two network stacks have no dependency between them: all three at once.
step_edge_and_network() {
  run_parallel edge step_edge - \
    "${AWS_REGION_PRIMARY}" network_region "${AWS_REGION_PRIMARY}" \
    "${AWS_REGION_SECONDARY}" network_region "${AWS_REGION_SECONDARY}"
}

# True when the primary-region cluster is still the live writer (or nothing is deployed yet).
databases_safe_to_update() {
  if is_dry_run; then
    log "[dry-run] would verify that the cluster of ${AWS_REGION_PRIMARY} is still the global writer before touching the database stacks"
    return 0
  fi
  local global_id writer own
  if [[ -z "$(stack_status "${AWS_REGION_PRIMARY}" "${stack_primary_db}")" ]]; then
    return 0
  fi
  global_id="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_primary_db}" GlobalClusterIdentifier)"
  own="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_primary_db}" DbClusterArn)"
  writer="$(aws rds describe-global-clusters --region "${AWS_REGION_PRIMARY}" --global-cluster-identifier "${global_id}" \
    --query 'GlobalClusters[0].GlobalClusterMembers[?IsWriter].DBClusterArn | [0]' --output text 2>&1)" ||
    die "cannot read the global cluster ${global_id}: $(sanitize <<<"${writer}")"
  [[ "${writer}" == "${own}" ]]
}

# deploy_database primary|secondary true|false ; the second argument is EnableDataApi (primary only)
deploy_database() {
  local role="$1" data_api="$2" region key stack
  local -A params=()
  local -a overrides=()
  if [[ "${role}" == "primary" ]]; then
    region="${AWS_REGION_PRIMARY}"
    key="Primary"
    stack="${stack_primary_db}"
  else
    region="${AWS_REGION_SECONDARY}"
    key="Secondary"
    stack="${stack_secondary_db}"
  fi
  load_params params ".database.Common + .database.${key}"
  params[VpcId]="$(stack_output "${region}" "$(stack_name "${region}" network)" VpcId)"
  params[PrivateSubnetIds]="$(stack_output "${region}" "$(stack_name "${region}" network)" PrivateSubnetIds)"
  params[DbInstanceClass]="${db_class}"
  params[EngineVersion]="${engine_version}"
  params[EnableDataApi]="${data_api}"
  [[ -z "${ALERT_EMAIL:-}" ]] || params[AlertEmail]="${ALERT_EMAIL}"
  [[ -n "${params[KeyAdminRoleArn]:-}" ]] || params[KeyAdminRoleArn]="${EXEC_ROLE_ARN}"
  if [[ "${role}" == "secondary" ]]; then
    params[GlobalClusterIdentifier]="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_primary_db}" GlobalClusterIdentifier)"
    params[PrimaryClusterArn]="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_primary_db}" DbClusterArn)"
  fi
  params_to_overrides params overrides
  cfn_deploy "${region}" "${stack}" "${REPO_ROOT}/templates/database.yaml" "" "${EXEC_ROLE_ARN}" "" "${overrides[@]}"
}

step_db_primary() {
  if ! databases_safe_to_update; then
    warn "the cluster of ${AWS_REGION_PRIMARY} is not the global writer any more (a drill or failover swapped the roles): the database stacks are NOT updated (design C19)."
    return 0
  fi
  deploy_database primary false
}

step_db_secondary() {
  if ! databases_safe_to_update; then
    warn "roles are swapped: the secondary database stack is NOT updated (design C19)."
    return 0
  fi
  # The primary stack is complete here, so its instance exists (the secondary can only be created after it).
  deploy_database secondary false
}

step_schema() {
  local -a flag=()
  is_dry_run && flag=(--dry-run)
  if ! databases_safe_to_update; then
    warn "roles are swapped: the schema bootstrap targets the original primary and is skipped."
    return 0
  fi
  log "enabling the Data API on the primary cluster for the schema bootstrap only (it is closed again right after)"
  DATA_API_ENABLED=1
  deploy_database primary true
  "${SCRIPT_DIR}/init-schema.sh" "${flag[@]}"
  log "closing the Data API"
  deploy_database primary false
  DATA_API_ENABLED=0
  if ! is_dry_run; then
    local state
    state="$(aws rds describe-db-clusters --region "${AWS_REGION_PRIMARY}" --db-cluster-identifier "$(cluster_identifier "${AWS_REGION_PRIMARY}")" \
      --query 'DBClusters[0].HttpEndpointEnabled' --output text)"
    [[ "${state}" == "False" ]] || die "THE DATA API IS STILL ENABLED on the primary cluster (state: ${state}). Deploy ${stack_primary_db} with EnableDataApi=false now."
    log "verified: the Data API is disabled"
  fi
}

# sam_deploy_api REGION
sam_deploy_api() {
  local region="$1" db_stack stack boot_stack bucket
  local -A params=()
  local -a overrides=() shown=() args=()
  db_stack="$(stack_name "${region}" database)"
  stack="$(stack_name "${region}" api)"
  boot_stack="$(stack_name "${region}" bootstrap)"
  bucket="$(stack_output "${region}" "${boot_stack}" ArtifactBucketName)"
  load_params params '.api.Common'
  params[ApiDomainName]="${API_DOMAIN_NAME}"
  params[HostedZoneId]="${HOSTED_ZONE_ID}"
  # CORS allow-list (A16): the monitoring site first, then the banking site (outputs of the edge stack, task 4.3).
  local monitor_domain bank_domain
  monitor_domain="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_edge}" DistributionDomainName)"
  bank_domain="$(stack_output "${AWS_REGION_PRIMARY}" "${stack_edge}" BankDistributionDomainName)"
  [[ -n "${monitor_domain}" ]] || die "edge stack output DistributionDomainName is empty (is the edge stack deployed?)"
  [[ -n "${bank_domain}" ]] || die "edge stack output BankDistributionDomainName is empty: deploy the edge stack with the banking site first (task 4.3)"
  params[AllowedOrigins]="https://${monitor_domain},https://${bank_domain}"
  params[DbEndpoint]="$(stack_output "${region}" "${db_stack}" DbClusterEndpoint)"
  params[DbClusterResourceId]="$(stack_output "${region}" "${db_stack}" DbClusterResourceId)"
  params[PrivateSubnetIds]="$(stack_output "${region}" "$(stack_name "${region}" network)" PrivateSubnetIds)"
  params[LambdaSecurityGroupId]="$(stack_output "${region}" "${db_stack}" LambdaSecurityGroupId)"
  params[KmsKeyArn]="$(stack_output "${region}" "${db_stack}" KmsKeyArn)"
  params[AlertTopicArn]="$(stack_output "${region}" "${db_stack}" AlertTopicArn)"
  # Cognito parameters from SSM: each data region reads its own local parameters (us-east-2 primary pool, us-west-2 replica).
  local cognito_ssm_region="${region}"
  params[CognitoUserPoolArn]="$(read_cognito_ssm "${cognito_ssm_region}" UserPoolArn)"
  params[CognitoBankClientId]="$(read_cognito_ssm "${cognito_ssm_region}" BankClientId)"
  params[CognitoDashboardClientId]="$(read_cognito_ssm "${cognito_ssm_region}" DashboardClientId)"
  params_to_overrides params overrides
  local item index
  for index in "${!overrides[@]}"; do
    item="${overrides[${index}]}"
    # sam rejects "Key=" (empty value): it needs Key="" for an empty string.
    [[ "${item}" == *= ]] && overrides[index]="${item}\"\""
    shown+=("${overrides[${index}]}")
  done
  args=(deploy --config-env prod-api --stack-name "${stack}" --region "${region}" --s3-bucket "${bucket}"
    --role-arn "${EXEC_ROLE_ARN}" --tags "${TAGS[@]}" --parameter-overrides)
  assert_stack_deployable "${region}" "${stack}"
  log "deploy stack ${stack} (${region}) with sam"
  if is_dry_run; then
    log "[dry-run] sam ${args[*]} ${shown[*]}"
    return 0
  fi
  (cd "${REPO_ROOT}" && sam "${args[@]}" "${overrides[@]}" 2>&1 | sanitize_stream)
}

step_api() {
  local region
  for region in "${AWS_REGION_PRIMARY}" "${AWS_REGION_SECONDARY}"; do
    # The stage logs at ERROR level, which needs the account-level CloudWatch Logs role of the region (account baseline).
    if is_dry_run; then
      log "[dry-run] would check that the API Gateway account CloudWatch Logs role is set in ${region}"
    else
      local log_role
      log_role="$(aws apigateway get-account --region "${region}" --query cloudwatchRoleArn --output text 2>&1)" ||
        die "cannot read the API Gateway account settings of ${region}: $(sanitize <<<"${log_role}")"
      [[ -n "${log_role}" && "${log_role}" != "None" ]] ||
        die "the API Gateway account CloudWatch Logs role is not set in ${region}: deploy the account baseline (repository nubesegura-accounts-baseline, iam stack) in that region first"
      log "  ${region}: API Gateway account CloudWatch Logs role is set"
    fi
  done
  if [[ ! -f "${REPO_ROOT}/src/backend/certs/global-bundle.pem" ]]; then
    log "RDS CA bundle missing: downloading it (scripts/fetch-rds-ca.sh)"
    if is_dry_run; then
      log "[dry-run] would run scripts/fetch-rds-ca.sh"
    else
      "${SCRIPT_DIR}/fetch-rds-ca.sh"
    fi
  fi
  log "sam build (one build, deployed to both regions)"
  if is_dry_run; then
    log "[dry-run] sam build --config-env prod-api"
  else
    (cd "${REPO_ROOT}" && sam build --config-env prod-api)
  fi
  # Both regions at once. Their certificates share one DNS validation record (same name and value for the same domain
  # in the same account); CloudFormation writes it with an upsert of identical content, so the order does not matter.
  run_parallel "${AWS_REGION_PRIMARY}" sam_deploy_api "${AWS_REGION_PRIMARY}" \
    "${AWS_REGION_SECONDARY}" sam_deploy_api "${AWS_REGION_SECONDARY}"
}

step_dns() {
  local primary_api secondary_api primary_zone secondary_zone primary_weight secondary_weight
  primary_api="$(stack_name "${AWS_REGION_PRIMARY}" api)"
  secondary_api="$(stack_name "${AWS_REGION_SECONDARY}" api)"
  primary_weight="$(stack_param "${AWS_REGION_GLOBAL}" "${stack_dns}" PrimaryWeight)"
  secondary_weight="$(stack_param "${AWS_REGION_GLOBAL}" "${stack_dns}" SecondaryWeight)"
  if [[ -z "${primary_weight}" ]]; then
    primary_weight=255
    secondary_weight=0
    log "dns stack not deployed yet: weights ${AWS_REGION_PRIMARY}=255 ${AWS_REGION_SECONDARY}=0"
  else
    log "dns stack exists: keeping its weights (${AWS_REGION_PRIMARY}=${primary_weight} ${AWS_REGION_SECONDARY}=${secondary_weight}); use scripts/dr/30-switch-traffic.sh to move traffic"
  fi
  primary_zone="$(stack_output "${AWS_REGION_PRIMARY}" "${primary_api}" RegionalHostedZoneId)"
  secondary_zone="$(stack_output "${AWS_REGION_SECONDARY}" "${secondary_api}" RegionalHostedZoneId)"
  cfn_deploy "${AWS_REGION_GLOBAL}" "${stack_dns}" "${REPO_ROOT}/templates/dns.yaml" "" "${EXEC_ROLE_ARN}" "" \
    "ApiDomainName=${API_DOMAIN_NAME}" "HostedZoneId=${HOSTED_ZONE_ID}" \
    "PrimaryTargetDomainName=$(stack_output "${AWS_REGION_PRIMARY}" "${primary_api}" RegionalDomainName)" \
    "PrimaryTargetHostedZoneId=${primary_zone}" \
    "SecondaryTargetDomainName=$(stack_output "${AWS_REGION_SECONDARY}" "${secondary_api}" RegionalDomainName)" \
    "SecondaryTargetHostedZoneId=${secondary_zone}" \
    "PrimaryWeight=${primary_weight}" "SecondaryWeight=${secondary_weight}"
}

# publish_job SCRIPT ; runs one publisher (its own process, staging directory, bucket and distribution)
publish_job() {
  local -a flag=()
  is_dry_run && flag=(--dry-run)
  "${SCRIPT_DIR}/$1" "${flag[@]}"
}

# The step named dashboard publishes both static sites at once; each publisher prints its own address.
step_dashboard() {
  run_parallel dashboard publish_job publish-dashboard.sh bank publish_job publish-bank.sh
}

# ---------------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------------
trap on_exit EXIT

if want db-primary || want db-secondary; then
  log "pre-flight: engine version and instance class"
  check_engine_and_class
fi
load_global_context

want bootstrap && step_bootstrap
if want edge && want network; then
  step_edge_and_network
else
  want edge && step_edge
  want network && step_network
fi
want cognito && step_cognito
want db-primary && step_db_primary
want db-secondary && step_db_secondary
want schema && step_schema
want api && step_api
want dns && step_dns
want dashboard && step_dashboard

log "deploy finished (steps: $(for step in "${ALL_STEPS[@]}"; do want "${step}" && printf '%s ' "${step}"; done))"
log "nothing in this script verifies the deployed behaviour: run scripts/dr/40-validate.sh and open the monitoring dashboard and the banking app (addresses printed by the publish step)"
