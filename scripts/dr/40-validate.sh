#!/usr/bin/env bash
# 40-validate.sh - validate the service on the custom domain after a promotion or a traffic switch (requirement 7.1).
#
# Calls GET /stats and GET /health?deep=true on https://<API_DOMAIN_NAME> and prints served_by_region and
# database_role of both, plus counts per region. Optionally waits for DNS to reach an expected region and posts
# one probe movement to prove that writes work (a deposit of 0.01; the balance of the demo account grows by one cent).
# The Cognito ID token is read from COGNITO_ID_TOKEN; it is never printed or placed in the process list.
#
# UNDO:    read-only, except --probe, which adds one deposit of 0.01 (there is no delete API by design; a later
#          withdrawal of 0.01 from the banking app compensates it).
# Usage:   scripts/dr/40-validate.sh [--expect-region <region>] [--wait <seconds>] [--probe] [--dry-run]
# Needs:   API_DOMAIN_NAME; COGNITO_ID_TOKEN for the authenticated calls (the call itself needs no AWS credentials)
# Exit:    0 all checks passed; 1 a check failed or the expected region was not reached in time.
set -euo pipefail

# shellcheck source=../lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
# shellcheck source=../lib/dr.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/dr.sh"

expect=""
wait_seconds=0
probe=0
while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --probe) probe=1 ;;
    --expect-region)
      shift
      expect="${1:-}"
      ;;
    --wait)
      shift
      wait_seconds="${1:-}"
      ;;
    -h | --help)
      sed -n '2,/^set -euo pipefail/{/^set -euo/!p;}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
[[ "${wait_seconds}" =~ ^[0-9]+$ ]] || die "--wait must be a number of seconds"
[[ -z "${expect}" ]] || validate_region --expect-region "${expect}"

print_context soft
failures=0

if is_dry_run; then
  log "[dry-run] would require API_DOMAIN_NAME, read COGNITO_ID_TOKEN (optional) and call, on https://<API_DOMAIN_NAME>:"
  log "[dry-run]   GET /stats           -> print served_by_region, database_role, total_count, counts_by_region"
  log "[dry-run]   GET /health?deep=true -> print region, status, database_role"
  [[ -z "${expect}" ]] || log "[dry-run]   poll /stats up to ${wait_seconds} s until served_by_region == ${expect}"
  ((probe == 0)) || log "[dry-run]   POST /transactions {type: deposit, amount: 0.01} with a unique Idempotency-Key (probe) and print processed_region"
  exit 0
fi

if [[ -n "${COGNITO_ID_TOKEN:-}" ]]; then
  api_init
else
  require_env API_DOMAIN_NAME
  validate_domain
  require_cmds curl jq
  dir="$(mktemp -d)"
  API_HEADERS_FILE="${dir}/headers"
  API_BODY_FILE="${dir}/body"
  : >"${API_HEADERS_FILE}"
  : >"${API_BODY_FILE}"
fi
trap api_cleanup EXIT

if [[ -n "${COGNITO_ID_TOKEN:-}" ]]; then
  log "step 1/3: GET /stats on ${API_DOMAIN_NAME}"
  deadline=$((SECONDS + wait_seconds))
  while :; do
    code="$(api_call GET /stats)"
    served=""
    if [[ "${code}" == "200" ]]; then
      served="$(jq -r '.served_by_region // ""' "${API_BODY_FILE}")"
    fi
    log "  HTTP ${code} served_by_region=${served:-n/a}"
    if [[ -z "${expect}" || "${served}" == "${expect}" ]]; then
      break
    fi
    if ((SECONDS >= deadline)); then
      warn "expected region ${expect} was not reached (DNS caches can last a few minutes; stale keep-alive connections ignore DNS)"
      failures=1
      break
    fi
    sleep 10
  done
  if [[ "${code}" == "200" ]]; then
    log "  served_by_region : $(jq -r '.served_by_region' "${API_BODY_FILE}")"
    log "  database_role    : $(jq -r '.database_role' "${API_BODY_FILE}")"
    log "  X-Served-By-Region header: $(api_header X-Served-By-Region)"
    log "  total_count      : $(jq -r '.total_count' "${API_BODY_FILE}")"
    log "  counts_by_region : $(jq -c '.counts_by_region' "${API_BODY_FILE}")"
    log "  last_processed_at_by_region: $(jq -c '.last_processed_at_by_region' "${API_BODY_FILE}")"
  else
    warn "GET /stats did not return 200"
    failures=1
  fi
else
  log "step 1/3: GET /stats skipped (no COGNITO_ID_TOKEN; verifying region via GET /health?deep=true)"
fi

log "step 2/3: GET /health?deep=true"
deadline=$((SECONDS + wait_seconds))
while :; do
  code="$(curl --silent --show-error --max-time 20 --proto '=https' --tlsv1.2 \
    --dump-header "${API_HEADERS_FILE}" --output "${API_BODY_FILE}" --write-out '%{http_code}' \
    "https://${API_DOMAIN_NAME}/health?deep=true" 2>/dev/null)" || code="000"
  served=""
  if [[ "${code}" == "200" ]]; then
    served="$(jq -r '.region // ""' "${API_BODY_FILE}")"
  fi
  if [[ -z "${COGNITO_ID_TOKEN:-}" ]]; then
    log "  HTTP ${code} region=${served:-n/a}"
    if [[ -z "${expect}" || "${served}" == "${expect}" ]]; then
      break
    fi
    if ((SECONDS >= deadline)); then
      warn "expected region ${expect} was not reached (DNS caches can last a few minutes; stale keep-alive connections ignore DNS)"
      failures=1
      break
    fi
    sleep 10
  else
    break
  fi
done
if [[ "${code}" == "200" ]]; then
  log "  HTTP 200 status=$(jq -r '.status' "${API_BODY_FILE}") region(served_by_region)=$(jq -r '.region' "${API_BODY_FILE}") database_role=$(jq -r '.database_role // "n/a"' "${API_BODY_FILE}")"
else
  warn "deep health returned HTTP ${code} (503 means the database is unreachable from that region)"
  failures=1
fi

log "step 3/3: probe movement (deposit of 0.01)"
if ((probe == 1)) && [[ -n "${COGNITO_ID_TOKEN:-}" ]]; then
  key="drill-probe-$(date -u +%Y%m%d%H%M%S)-${RANDOM}"
  body="$(jq -nc '{type: "deposit", amount: 0.01}')"
  code="$(api_call POST /transactions "${body}" "${key}")"
  case "${code}" in
    201) log "  HTTP 201 processed_region=$(jq -r '.transaction.processed_region' "${API_BODY_FILE}") id=$(jq -r '.transaction.id' "${API_BODY_FILE}") balance=$(jq -r '.balance' "${API_BODY_FILE}")" ;;
    503)
      warn "HTTP 503 $(jq -r '.error.code // "unknown"' "${API_BODY_FILE}"): the serving region cannot write (promote first?)"
      failures=1
      ;;
    *)
      warn "probe returned HTTP ${code}"
      failures=1
      ;;
  esac
elif ((probe == 1)); then
  log "  skipped (no COGNITO_ID_TOKEN provided for authenticated probe)"
else
  log "  skipped (add --probe to post one deposit of 0.01)"
fi

if ((failures > 0)); then
  warn "validation FAILED"
  exit 1
fi
log "validation passed"
