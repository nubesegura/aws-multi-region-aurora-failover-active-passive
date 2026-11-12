#!/usr/bin/env bash
# init-schema.sh - apply scripts/sql/schema.sql to the PRIMARY cluster through the RDS Data API (requirement 4.5).
#
# One-time bootstrap, idempotent (safe to repeat). The master credentials come from the RDS-managed secret
# (MasterSecretArn output of the primary database stack); the script never reads or prints the secret value: the
# Data API reads it on its side. It needs the Data API to be enabled on the primary cluster, which
# scripts/deploy.sh does only around this step (EnableDataApi=true, then back to false; threat model T-MASTER, D23).
# Creates: the ledger table transactions (with seq, type, balance_after) and its index on processed_at, the one-row
# table account (opened at balance 0, never reset by a repeat), users app_writer (INSERT on the ledger, SELECT and
# UPDATE on account) and app_reader (SELECT only), both with GRANT rds_iam.
#
# UNDO:    none needed (idempotent). To remove the objects, delete the stack; to close the Data API again run
#          scripts/deploy.sh --steps schema (or deploy the primary database stack with EnableDataApi=false).
# Usage:   scripts/init-schema.sh [--dry-run]
# Needs:   AWS_REGION_PRIMARY; credentials allowed to call rds-data:ExecuteStatement on the cluster and to read the secret.
# Fallback without the Data API: run scripts/sql/schema.sql with psql from an in-VPC session as the master user.
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

SQL_FILE="${REPO_ROOT}/scripts/sql/schema.sql"

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

require_cmds jq
is_dry_run || require_cmds aws
require_env AWS_REGION_PRIMARY
validate_region AWS_REGION_PRIMARY "${AWS_REGION_PRIMARY}"
print_context

declare -A common=()
load_params common '.database.Common'
db_name="${common[DbName]}"
declare -A api=()
load_params api '.api.Common'
[[ "${api[DbWriterUser]}" == "app_writer" && "${api[DbReaderUser]}" == "app_reader" ]] ||
  die "parameters/prod.json api.Common DbWriterUser/DbReaderUser differ from the user names in scripts/sql/schema.sql; change both together"

# Split the file into statements: a line "-- split" ends a statement; comment-only lines are dropped.
trim_leading() { printf '%s' "${1#"${1%%[![:space:]]*}"}"; }
statements=()
current=""
while IFS= read -r line || [[ -n "${line}" ]]; do
  if [[ "${line}" == "-- split" ]]; then
    [[ -n "${current//[[:space:]]/}" ]] && statements+=("$(trim_leading "${current}")")
    current=""
  elif [[ "${line}" =~ ^[[:space:]]*-- ]]; then
    continue
  else
    current+="${line}"$'\n'
  fi
done <"${SQL_FILE}"
[[ -n "${current//[[:space:]]/}" ]] && statements+=("$(trim_leading "${current}")")
log "schema.sql: ${#statements[@]} statements for database ${db_name}"

primary_stack="$(stack_name "${AWS_REGION_PRIMARY}" database)"
cluster_arn_value="$(stack_output "${AWS_REGION_PRIMARY}" "${primary_stack}" DbClusterArn)"
secret_arn="$(stack_output "${AWS_REGION_PRIMARY}" "${primary_stack}" MasterSecretArn)"

if is_dry_run; then
  index=0
  for statement in "${statements[@]}"; do
    index=$((index + 1))
    log "[dry-run] would execute statement ${index}/${#statements[@]} through the Data API: $(head -n 1 <<<"${statement}")"
  done
  log "[dry-run] would verify tables, roles and rds_iam membership with SELECT statements"
  exit 0
fi

endpoint_enabled="$(aws rds describe-db-clusters --region "${AWS_REGION_PRIMARY}" \
  --db-cluster-identifier "$(cluster_identifier "${AWS_REGION_PRIMARY}")" \
  --query 'DBClusters[0].HttpEndpointEnabled' --output text 2>&1)" || endpoint_enabled="error"
[[ "${endpoint_enabled}" == "True" ]] ||
  die "the Data API is not enabled on the primary cluster (state: $(sanitize <<<"${endpoint_enabled}")). Run scripts/deploy.sh --steps schema, which enables it only for this step."

# execute_sql SQL ; prints the JSON response. Retries while the Data API endpoint is still coming up.
execute_sql() {
  local attempt=1 output
  while :; do
    if output="$(aws rds-data execute-statement --region "${AWS_REGION_PRIMARY}" --resource-arn "${cluster_arn_value}" \
      --secret-arn "${secret_arn}" --database "${db_name}" --sql "$1" --output json 2>&1)"; then
      printf '%s' "${output}"
      return 0
    fi
    # The endpoint comes up asynchronously after the stack update: "HttpEndpoint is being enabled" (HttpEndpointNotEnabledException).
    if ((attempt >= 12)) || ! grep -qiE 'HttpEndpoint is (not|being) enabled|HttpEndpointNotEnabled|Communications link|timeout|Throttl|ServiceUnavailable|StatementTimeout' <<<"${output}"; then
      die "statement failed: $(sanitize <<<"${output}" | head -c 600)"
    fi
    log "  Data API not ready or busy (attempt ${attempt} of 12), waiting 10 s"
    attempt=$((attempt + 1))
    sleep 10
  done
}

index=0
for statement in "${statements[@]}"; do
  index=$((index + 1))
  log "statement ${index}/${#statements[@]}: $(head -n 1 <<<"${statement}")"
  execute_sql "${statement}" >/dev/null
done

log "verifying"
tables="$(execute_sql "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'transactions'" | jq -r '.records[0][0].longValue')"
roles="$(execute_sql "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member WHERE r.rolname = 'rds_iam' AND u.rolname IN ('app_writer', 'app_reader')" | jq -r '.records[0][0].longValue')"
index_count="$(execute_sql "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'idx_transactions_processed_at'" | jq -r '.records[0][0].longValue')"
log "  table transactions: ${tables} (expected 1); users with rds_iam: ${roles} (expected 2); index on processed_at: ${index_count} (expected 1)"
[[ "${tables}" == "1" && "${roles}" == "2" && "${index_count}" == "1" ]] || die "verification failed"
log "schema bootstrap finished"
