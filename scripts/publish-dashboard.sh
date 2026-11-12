#!/usr/bin/env bash
# publish-dashboard.sh - publish the static monitoring dashboard to the edge bucket and invalidate CloudFront (requirements 4.2, 6.2, 8.2).
#
# Uploads the files of src/dashboard/ (tests and local helpers excluded) plus error.html (CloudFront error pages point
# to /error.html; a minimal one is generated when the source file is missing), and writes config.json from
# API_DOMAIN_NAME and the Cognito SSM parameters (/drdemo/<ENV_TYPE>/cognito/UserPoolId and DashboardClientId in
# primary and standby regions). Content-Type and Cache-Control are set as object metadata. Objects of an earlier publish
# that no longer exist locally are removed (the bucket is versioned, so earlier versions stay recoverable for 30 days).
# The work is done by publish_site in lib/publish.sh, shared with scripts/publish-bank.sh; it aborts when a stack output is empty.
#
# UNDO:    check out the previous dashboard files and run this script again (or restore a bucket version).
# Usage:   scripts/publish-dashboard.sh [--dry-run]
# Needs:   AWS_REGION_PRIMARY API_DOMAIN_NAME (optional AWS_REGION_SECONDARY, defaults to us-west-2);
#          credentials of the deploy role.
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
# shellcheck source=lib/publish.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/publish.sh"

eval "_base_$(declare -f publish_stage)"
publish_stage() {
  local source_dir="$1" stage="$2"
  local primary_region="${AWS_REGION_PRIMARY}"
  local standby_region="${AWS_REGION_SECONDARY:-us-west-2}"
  local primary_pool_id primary_client_id standby_pool_id standby_client_id
  _base_publish_stage "${source_dir}" "${stage}"
  primary_pool_id="${COGNITO_PRIMARY_USER_POOL_ID:-$(read_cognito_ssm "${primary_region}" UserPoolId)}"
  primary_client_id="${COGNITO_DASHBOARD_CLIENT_ID:-$(read_cognito_ssm "${primary_region}" DashboardClientId)}"
  standby_pool_id="${COGNITO_STANDBY_USER_POOL_ID:-$(read_cognito_ssm "${standby_region}" UserPoolId)}"
  standby_client_id="${COGNITO_STANDBY_DASHBOARD_CLIENT_ID:-$(read_cognito_ssm "${standby_region}" DashboardClientId)}"
  jq -n \
    --arg url "https://${API_DOMAIN_NAME}" \
    --arg p_region "${primary_region}" \
    --arg p_pool "${primary_pool_id}" \
    --arg p_client "${primary_client_id}" \
    --arg p_endpoint "https://cognito-idp.${primary_region}.amazonaws.com" \
    --arg s_region "${standby_region}" \
    --arg s_pool "${standby_pool_id}" \
    --arg s_client "${standby_client_id}" \
    --arg s_endpoint "https://cognito-idp.${standby_region}.amazonaws.com" \
    '{
      apiBaseUrl: $url,
      cognito: {
        primary: {
          region: $p_region,
          userPoolId: $p_pool,
          clientId: $p_client,
          endpoint: $p_endpoint,
          apiBaseUrl: $url
        },
        standby: {
          region: $s_region,
          userPoolId: $s_pool,
          clientId: $s_client,
          endpoint: $s_endpoint,
          apiBaseUrl: $url
        }
      }
    }' >"${stage}/config.json"
}

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
require_env AWS_REGION_PRIMARY API_DOMAIN_NAME
validate_region AWS_REGION_PRIMARY "${AWS_REGION_PRIMARY}"
validate_domain
print_context

publish_site "${REPO_ROOT}/src/dashboard" DashboardBucketName DistributionId
log "sign in with demo Cognito credentials; session tokens stay in sessionStorage only"
