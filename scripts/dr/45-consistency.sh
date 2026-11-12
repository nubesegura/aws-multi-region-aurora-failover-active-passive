#!/usr/bin/env bash
# 45-consistency.sh - ask the API to replay the whole ledger and prove that no movement was lost or duplicated
# (requirements 7.1 and 9.1; gate of the failover drill).
#
# Calls GET /consistency on https://<API_DOMAIN_NAME> and prints the report fields (movement count, total deposited,
# total withdrawn, replayed and stored balance, balance and chain checks, duplicated keys, counts per region, first
# offending movement), the region that served the call and the elapsed time. The call is read only.
# The Cognito ID token is read from COGNITO_ID_TOKEN. It is never printed or placed in the process list (it reaches curl
# through stdin, see api_call in lib/dr.sh).
#
# UNDO:    nothing to undo, the call only reads.
# Usage:   scripts/dr/45-consistency.sh [--dry-run]
# Needs:   API_DOMAIN_NAME, COGNITO_ID_TOKEN, curl, jq
# Exit:    0 the ledger is consistent (consistent is true); 1 inconsistent, no valid answer or missing configuration;
#          2 the check was truncated (more rows than the limit, nothing is claimed either way).
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

print_context soft

if is_dry_run; then
  log "[dry-run] would require API_DOMAIN_NAME, require COGNITO_ID_TOKEN and call GET /consistency on https://<API_DOMAIN_NAME>"
  log "[dry-run] would print: count, total_deposited, total_withdrawn, replayed_balance, stored_balance, balances_match,"
  log "[dry-run]   chain_ok, duplicate_keys, counts_by_region, consistent, first_offender, truncated, serving region, elapsed time"
  log "[dry-run] exit 0 when consistent is true, 1 when not consistent or no valid answer, 2 when truncated"
  exit 0
fi

# Checked here (exit 1) because exit 2 of require_env would be confused with "truncated".
[[ -n "${API_DOMAIN_NAME:-}" ]] || die "API_DOMAIN_NAME is required (GitHub variable of the prod environment)"
[[ -n "${COGNITO_ID_TOKEN:-}" ]] || die "COGNITO_ID_TOKEN is required (an ID token of a demo user from the primary or standby pool)"
api_init
trap api_cleanup EXIT

log "GET /consistency on ${API_DOMAIN_NAME}"
started_ms="$(date +%s%3N)"
code="$(api_call GET /consistency)"
elapsed_ms=$(($(date +%s%3N) - started_ms))

if [[ "${code}" != "200" ]]; then
  warn "GET /consistency returned HTTP ${code} after ${elapsed_ms} ms (000 means no connection; 503 means the serving region cannot read)"
  exit 1
fi
if ! jq -e 'type == "object"' "${API_BODY_FILE}" >/dev/null 2>&1; then
  warn "GET /consistency returned HTTP 200 but the body is not a JSON object"
  exit 1
fi

# The report may be the whole body or sit under "report"; the serving region comes from the header or the body.
report='(if (.report | type) == "object" then .report else . end)'
served="$(api_header X-Served-By-Region)"
[[ -n "${served}" ]] || served="$(jq -r '.served_by_region // "n/a"' "${API_BODY_FILE}")"

log "  serving region    : ${served}"
log "  elapsed           : ${elapsed_ms} ms"
for field in count total_deposited total_withdrawn replayed_balance stored_balance balances_match chain_ok duplicate_keys \
  consistent first_offender truncated; do
  log "  $(printf '%-17s' "${field}") : $(jq -r "${report} | .${field} | if . == null then \"n/a\" else tostring end" "${API_BODY_FILE}")"
done
log "  counts_by_region  : $(jq -c "${report} | .counts_by_region // {}" "${API_BODY_FILE}")"

truncated="$(jq -r "${report} | .truncated // false | tostring" "${API_BODY_FILE}")"
consistent="$(jq -r "${report} | .consistent | tostring" "${API_BODY_FILE}")"

if [[ "${truncated}" == "true" ]]; then
  warn "TRUNCATED: the ledger has more rows than the check limit, so consistency was not verified"
  exit 2
fi
if [[ "${consistent}" != "true" ]]; then
  warn "INCONSISTENT (consistent=${consistent}); first offending movement: $(jq -r "${report} | .first_offender // \"n/a\"" "${API_BODY_FILE}")"
  exit 1
fi
log "consistent: the replayed balance equals the stored balance, the chain is intact and no key is duplicated"
