#!/usr/bin/env bash
# Shared helpers of the delivery and DR scripts. Source it, do not execute it.
#
# Conventions:
#   - Every script sets `set -euo pipefail`, supports --dry-run (DRY_RUN=1) and prints timestamped actions.
#   - In dry-run mode no AWS API is called: read helpers return <placeholders>, write helpers only print.
#   - The account id is printed masked; secrets are never printed.
#   - Needs bash 4+ (associative arrays) and GNU date (Linux, Git Bash).
#
# Names follow <acronym>-<region without dashes>-drdemo-<descriptor>-prod (design constraints C01, C02).
# shellcheck shell=bash

if [[ -n "${DRDEMO_COMMON_LOADED:-}" ]]; then
  return 0
fi
DRDEMO_COMMON_LOADED=1

if [[ -z "${REPO_ROOT:-}" ]]; then
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  readonly REPO_ROOT
fi
if [[ -z "${ENV_TYPE:-}" ]]; then
  readonly ENV_TYPE="prod"
fi
readonly PARAMS_FILE="${REPO_ROOT}/parameters/prod.json"
# Mandatory tags (constitution section 4). Security tags live in the templates.
readonly -a TAGS=(
  "team-owner=nube-segura"
  "project-name=aws-multi-region-aurora-failover-active-passive"
  "app-name=dr-demo"
  "repo-name=aws-multi-region-aurora-failover-active-passive"
  "env-type=${ENV_TYPE}"
)
# The six functions of a region. The fence covers all six (a fence that skips a read function is not total).
# shellcheck disable=SC2034  # used by scripts/lib/dr.sh and scripts/destroy.sh
readonly -a FUNCTION_DESCRIPTORS=(create-transaction list-transactions get-stats get-health get-account get-consistency)

# Descriptors of the two static-site buckets of the edge stack: the monitoring dashboard and the banking app.
# shellcheck disable=SC2034  # used by scripts/destroy.sh
readonly -a SITE_BUCKET_DESCRIPTORS=(dashboard bank)

DRY_RUN="${DRY_RUN:-0}"
ACCOUNT_ID=""

# ---------------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------------
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s %s\n' "$(ts)" "$*"; }
warn() { printf '%s WARN  %s\n' "$(ts)" "$*" >&2; }
die() {
  printf '%s ERROR %s\n' "$(ts)" "$*" >&2
  exit 1
}
is_dry_run() { [[ "${DRY_RUN}" == "1" ]]; }

# Replace anything that looks like an account id (12 digits) so that errors can be shown safely.
sanitize() { sed -E 's/[0-9]{12}/************/g'; }
sanitize_stream() { sed -u -E 's/[0-9]{12}/************/g'; }

# ---------------------------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------------------------
require_cmds() {
  local name
  for name in "$@"; do
    command -v "${name}" >/dev/null 2>&1 || die "required command not found: ${name}"
  done
}

# Fail with one clear message listing every missing variable.
require_env() {
  local -a missing=()
  local name
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      missing+=("${name}")
    fi
  done
  if ((${#missing[@]} > 0)); then
    {
      printf '%s ERROR missing required variable(s): %s\n' "$(ts)" "${missing[*]}"
      printf '  Define them in the GitHub Environment "prod" (variables or secrets, see the header of\n'
      printf '  .github/workflows/deploy.yml) or export them in your shell before running this script.\n'
    } >&2
    exit 2
  fi
}

validate_region() { # label value
  [[ "$2" =~ ^[a-z]{2}-[a-z]+-[0-9]$ ]] || die "$1 is not a valid region name: '$2'"
}

validate_regions() {
  validate_region AWS_REGION_PRIMARY "${AWS_REGION_PRIMARY}"
  validate_region AWS_REGION_SECONDARY "${AWS_REGION_SECONDARY}"
  if [[ -n "${AWS_REGION_GLOBAL:-}" ]]; then
    validate_region AWS_REGION_GLOBAL "${AWS_REGION_GLOBAL}"
    [[ "${AWS_REGION_GLOBAL}" != "${AWS_REGION_PRIMARY}" && "${AWS_REGION_GLOBAL}" != "${AWS_REGION_SECONDARY}" ]] ||
      die "AWS_REGION_GLOBAL must differ from the two data regions (stack names would collide)"
  fi
  [[ "${AWS_REGION_PRIMARY}" != "${AWS_REGION_SECONDARY}" ]] || die "AWS_REGION_PRIMARY and AWS_REGION_SECONDARY must differ"
}

validate_domain() {
  [[ "${API_DOMAIN_NAME}" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] ||
    die "API_DOMAIN_NAME must be a lowercase host name such as api.example.com"
}

validate_zone() {
  [[ "${HOSTED_ZONE_ID}" =~ ^Z[A-Z0-9]{5,31}$ ]] || die "HOSTED_ZONE_ID must be a Route 53 hosted zone id (starts with Z)"
}

# ---------------------------------------------------------------------------------------------
# Cognito parameters in SSM Parameter Store (spec 002)
# ---------------------------------------------------------------------------------------------
# read_cognito_ssm REGION PARAM_KEY
# Reads /drdemo/<ENV_TYPE>/cognito/<PARAM_KEY> in REGION. In dry-run mode returns a placeholder.
read_cognito_ssm() {
  local region="$1" key="$2" param_name out
  param_name="/drdemo/${ENV_TYPE}/cognito/${key}"
  if is_dry_run; then
    printf '<ssm:%s:%s>' "${region}" "${param_name}"
    return 0
  fi
  if out="$(aws ssm get-parameter --region "${region}" --name "${param_name}" --query Parameter.Value --output text 2>&1)"; then
    [[ -n "${out}" && "${out}" != "None" ]] || die "SSM parameter ${param_name} in ${region} is empty (deploy the cognito stack first)"
    printf '%s' "${out}"
  else
    die "cannot read SSM parameter ${param_name} in ${region} (deploy the cognito stack first): $(sanitize <<<"${out:0:300}")"
  fi
}

# ---------------------------------------------------------------------------------------------
# Account and names
# ---------------------------------------------------------------------------------------------
# ensure_account [soft] ; soft keeps going (account unknown) when no credentials are available.
ensure_account() {
  if [[ -n "${ACCOUNT_ID}" ]]; then
    return 0
  fi
  if is_dry_run; then
    ACCOUNT_ID="<account-id>"
    return 0
  fi
  local identity
  identity="$(aws sts get-caller-identity --query Account --output text 2>&1)" || true
  if [[ "${identity}" =~ ^[0-9]{12}$ ]]; then
    ACCOUNT_ID="${identity}"
  elif [[ "${1:-}" == "soft" ]]; then
    ACCOUNT_ID="unavailable"
  else
    die "cannot read the AWS identity (are credentials configured?): $(sanitize <<<"${identity}")"
  fi
}

mask_account() {
  if [[ "${ACCOUNT_ID}" =~ ^[0-9]{12}$ ]]; then
    printf '********%s' "${ACCOUNT_ID: -4}"
  else
    printf '%s' "${ACCOUNT_ID}"
  fi
}

# print_context [soft]
print_context() {
  ensure_account "${1:-}"
  if is_dry_run; then
    log "account: ************ (dry-run, not queried)"
    log "mode: DRY-RUN (no AWS API is called)"
  else
    log "account: $(mask_account)"
    log "mode: LIVE"
  fi
  log "regions: primary=${AWS_REGION_PRIMARY:-n/a} secondary=${AWS_REGION_SECONDARY:-n/a} global=${AWS_REGION_GLOBAL:-n/a}"
}

region_code() { printf '%s' "${1//-/}"; }
stack_name() { printf 'stk-%s-drdemo-%s-%s' "$(region_code "$1")" "$2" "${ENV_TYPE}"; } # region stack
cluster_identifier() { printf 'rdsc-%s-drdemo-%s' "$(region_code "$1")" "${ENV_TYPE}"; }
cluster_arn() { printf 'arn:aws:rds:%s:%s:cluster:%s' "$1" "${ACCOUNT_ID}" "$(cluster_identifier "$1")"; }
function_name() { printf 'fnc-%s-drdemo-%s-%s' "$(region_code "$1")" "$2" "${ENV_TYPE}"; }                   # region descriptor
bucket_name() { printf 'bckt-%s-drdemo-%s-%s-%s' "$(region_code "$1")" "$2" "${ACCOUNT_ID}" "${ENV_TYPE}"; } # region kind

# ---------------------------------------------------------------------------------------------
# AWS call wrappers
# ---------------------------------------------------------------------------------------------
# Mutating call: printed (account ids masked), streamed through sanitize, executed only outside dry-run.
aws_write() {
  if is_dry_run; then
    log "[dry-run] aws $(sanitize <<<"$*")"
    return 0
  fi
  log "aws $(sanitize <<<"$*")"
  aws "$@" 2>&1 | sanitize_stream
}

# CloudFormation stack helpers. Dry-run returns placeholders and never calls AWS.
stack_status() { # region stack ; prints the status, empty when the stack does not exist
  if is_dry_run; then
    printf ''
    return 0
  fi
  local out
  if out="$(aws cloudformation describe-stacks --region "$1" --stack-name "$2" --query 'Stacks[0].StackStatus' --output text 2>&1)"; then
    printf '%s' "${out}"
  elif [[ "${out}" == *"does not exist"* ]]; then
    printf ''
  else
    die "cannot describe stack $2 in $1: $(sanitize <<<"${out}")"
  fi
}

stack_output() { # region stack key
  if is_dry_run; then
    printf '<%s:%s>' "$2" "$3"
    return 0
  fi
  local out
  # The value is NOT sanitized (ARNs carry the account id and are passed on as parameters); only errors are masked.
  out="$(aws cloudformation describe-stacks --region "$1" --stack-name "$2" \
    --query "Stacks[0].Outputs[?OutputKey=='$3'].OutputValue | [0]" --output text 2>&1)" ||
    die "cannot read output $3 of stack $2 in $1: $(sanitize <<<"${out}")"
  [[ -n "${out}" && "${out}" != "None" ]] || die "stack $2 in $1 has no output $3 (is it deployed?)"
  printf '%s' "${out}"
}

stack_param() { # region stack key ; empty when unset
  if is_dry_run; then
    printf ''
    return 0
  fi
  local out
  out="$(aws cloudformation describe-stacks --region "$1" --stack-name "$2" \
    --query "Stacks[0].Parameters[?ParameterKey=='$3'].ParameterValue | [0]" --output text 2>/dev/null)" || out=""
  [[ "${out}" == "None" ]] && out=""
  printf '%s' "${out}"
}

# Refuse to touch a stack that is stuck or half-created: investigate with read-only calls first.
assert_stack_deployable() { # region stack
  local status
  status="$(stack_status "$1" "$2")"
  case "${status}" in
    # REVIEW_IN_PROGRESS: a create change set that failed or was never executed; the stack has no resources and both
    # 'aws cloudformation deploy' and 'sam deploy' treat it as a new stack.
    "" | CREATE_COMPLETE | UPDATE_COMPLETE | UPDATE_ROLLBACK_COMPLETE | IMPORT_COMPLETE | REVIEW_IN_PROGRESS) return 0 ;;
    *) die "stack $2 in $1 is in state ${status}. Investigate with 'aws cloudformation describe-stack-events' before retrying; this script never deletes or retries a stuck stack." ;;
  esac
}

# aws cloudformation deploy wrapper. Usage:
#   cfn_deploy REGION STACK TEMPLATE CAPABILITIES ROLE_ARN BUCKET [KEY=VALUE ...]
# Empty CAPABILITIES / ROLE_ARN / BUCKET are skipped.
cfn_deploy() {
  local region="$1" stack="$2" template="$3" caps="$4" role="$5" bucket="$6"
  shift 6
  local -a args=(cloudformation deploy --region "${region}" --stack-name "${stack}" --template-file "${template}" --no-fail-on-empty-changeset --tags "${TAGS[@]}")
  [[ -n "${caps}" ]] && args+=(--capabilities "${caps}")
  [[ -n "${role}" ]] && args+=(--role-arn "${role}")
  [[ -n "${bucket}" ]] && args+=(--s3-bucket "${bucket}" --s3-prefix "${stack}")
  # CloudFormation accepts at most 51,200 bytes inline; a larger template needs a bucket.
  if [[ -z "${bucket}" ]] && (($(wc -c <"${template}") > 51200)); then
    die "${template#"${REPO_ROOT}"/} is larger than 51,200 bytes and no artifact bucket is available for ${stack} in ${region}: deploy it with --s3-bucket (see the README)"
  fi
  if (($# > 0)); then
    args+=(--parameter-overrides "$@")
  fi
  assert_stack_deployable "${region}" "${stack}"
  log "deploy stack ${stack} (${region}) from ${template#"${REPO_ROOT}"/}"
  aws_write "${args[@]}"
}

# run_parallel LABEL FUNCTION ARG [LABEL FUNCTION ARG ...]
# Runs every "FUNCTION ARG" at the same time, each in its own subshell (a die stops only that job), with every line of
# its output prefixed by "[LABEL] ". Waits for all of them and stops with the list of failed jobs; nothing is retried.
# Use it only for jobs with no dependency between them. Variables set inside a job do not reach the caller.
run_parallel() {
  (($# > 0 && $# % 3 == 0)) || die "run_parallel needs triplets: LABEL FUNCTION ARG"
  local -a labels=() pids=() failed=()
  local index
  while (($# > 0)); do
    labels+=("$1")
    ("$2" "$3") > >(sed -u "s/^/[$1] /") 2>&1 &
    pids+=("$!")
    shift 3
  done
  for index in "${!pids[@]}"; do
    wait "${pids[${index}]}" || failed+=("${labels[${index}]}")
  done
  wait 2>/dev/null || true # let the prefixing filters flush
  ((${#failed[@]} == 0)) || die "parallel step failed for: ${failed[*]} (see the lines prefixed with those labels; nothing was retried)"
}

# ---------------------------------------------------------------------------------------------
# Parameters from parameters/prod.json (nested object by role)
# ---------------------------------------------------------------------------------------------
# load_params ASSOC_ARRAY_NAME JQ_EXPRESSION ; the expression must yield an object of strings.
load_params() {
  local -n _lp_target="$1"
  local key value
  while IFS='=' read -r key value; do
    [[ -n "${key}" ]] && _lp_target["${key}"]="${value}"
  done < <(jq -r "$2 | to_entries[] | \"\\(.key)=\\(.value)\"" "${PARAMS_FILE}")
}

# params_to_overrides ASSOC_ARRAY_NAME OUT_ARRAY_NAME
params_to_overrides() {
  local -n _po_src="$1"
  local -n _po_out="$2"
  local key
  _po_out=()
  while IFS= read -r key; do
    _po_out+=("${key}=${_po_src[${key}]}")
  done < <(printf '%s\n' "${!_po_src[@]}" | sort)
}

# ---------------------------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------------------------
# Delete every object version and delete marker, then the bucket is ready for stack deletion.
empty_bucket() { # region bucket
  local region="$1" bucket="$2" listing payload count total=0
  if is_dry_run; then
    log "[dry-run] empty all versions and delete markers of s3://${bucket}"
    return 0
  fi
  if ! aws s3api head-bucket --region "${region}" --bucket "${bucket}" >/dev/null 2>&1; then
    log "bucket ${bucket} not found or not accessible: nothing to empty"
    return 0
  fi
  local work
  work="$(mktemp -d)"
  while :; do
    listing="$(aws s3api list-object-versions --region "${region}" --bucket "${bucket}" --max-items 1000 --output json)"
    payload="$(jq -c '{Objects: ([(.Versions // [])[], (.DeleteMarkers // [])[]] | map({Key: .Key, VersionId: .VersionId})), Quiet: true}' <<<"${listing}")"
    count="$(jq '.Objects | length' <<<"${payload}")"
    ((count > 0)) || break
    printf '%s' "${payload}" >"${work}/delete.json"
    aws s3api delete-objects --region "${region}" --bucket "${bucket}" --delete "file://${work}/delete.json" >/dev/null
    total=$((total + count))
  done
  rm -rf "${work}"
  log "emptied s3://${bucket} (${total} versions and markers deleted)"
}

# ---------------------------------------------------------------------------------------------
# Confirmations
# ---------------------------------------------------------------------------------------------
# confirm_typed PHRASE PROMPT [PROVIDED]  (PROVIDED comes from a flag such as --confirm; empty means ask)
confirm_typed() {
  local phrase="$1" prompt="$2" provided="${3:-}" answer
  if is_dry_run; then
    log "[dry-run] would require the typed confirmation: ${phrase}"
    return 0
  fi
  if [[ -n "${provided}" ]]; then
    [[ "${provided}" == "${phrase}" ]] || die "confirmation phrase did not match; nothing was changed"
    return 0
  fi
  [[ -t 0 ]] || die "an interactive terminal is required for the typed confirmation '${phrase}' (or pass it as a flag)"
  printf '%s\nType "%s" to continue: ' "${prompt}" "${phrase}" >&2
  read -r answer
  [[ "${answer}" == "${phrase}" ]] || die "confirmation phrase did not match; nothing was changed"
}

# confirm_yes PROMPT [ASSUME_YES]
confirm_yes() {
  local prompt="$1" assume="${2:-0}" answer
  if is_dry_run || [[ "${assume}" == "1" ]]; then
    return 0
  fi
  [[ -t 0 ]] || die "an interactive terminal is required to confirm (or pass --yes)"
  printf '%s [y/N] ' "${prompt}" >&2
  read -r answer
  [[ "${answer}" == "y" || "${answer}" == "Y" ]] || die "not confirmed; nothing was changed"
}
