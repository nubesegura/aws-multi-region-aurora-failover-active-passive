#!/usr/bin/env bash
# test-45-consistency.sh - exit codes of scripts/dr/45-consistency.sh against a stub curl on the PATH (no network, no AWS).
#
# Cases: consistent (0), inconsistent (1), truncated (2), HTTP 503 (1), body that is not JSON (1), missing variables (1),
# --dry-run (0 and no curl call). It also asserts that the Cognito ID token never appears in the curl arguments or in the output.
#
# UNDO:    nothing to undo (works in a temporary directory that is removed on exit).
# Usage:   scripts/tests/test-45-consistency.sh
# Needs:   bash 4+, jq
# Exit:    0 all cases behaved as expected; 1 at least one case failed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/dr/45-consistency.sh"
command -v jq >/dev/null 2>&1 || {
  echo "jq is required" >&2
  exit 1
}

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
mkdir -p "${work}/bin"

# Stub curl: records its arguments, drains stdin (the --config - section carries the token), writes the body and the
# headers where the real curl would and prints the HTTP status as --write-out would.
cat >"${work}/bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$@" >>"${STUB_DIR}/curl-args.log"
cat >/dev/null
out="" hdr=""
while (($# > 0)); do
  case "$1" in
    --output) shift; out="$1" ;;
    --dump-header) shift; hdr="$1" ;;
  esac
  shift
done
[[ -n "${out}" ]] && cp "${STUB_DIR}/body.json" "${out}"
[[ -n "${hdr}" ]] && printf 'HTTP/2 %s\r\nX-Served-By-Region: us-west-2\r\n' "${STUB_CODE}" >"${hdr}"
printf '%s' "${STUB_CODE}"
STUB
chmod +x "${work}/bin/curl"

export STUB_DIR="${work}"
export API_DOMAIN_NAME="api.example.com"
export COGNITO_ID_TOKEN="stub-token-stub-token-stub-token-0000"
export PATH="${work}/bin:${PATH}"

failures=0
# run_case NAME EXPECTED_EXIT HTTP_CODE BODY [script args...]
run_case() {
  local name="$1" expected="$2" code="$3" body="$4" actual=0
  shift 4
  : >"${work}/curl-args.log"
  printf '%s' "${body}" >"${work}/body.json"
  STUB_CODE="${code}" bash "${SCRIPT}" "$@" >"${work}/out.log" 2>&1 || actual=$?
  if ((actual != expected)); then
    echo "FAIL ${name}: exit ${actual}, expected ${expected}"
    sed 's/^/    /' "${work}/out.log"
    failures=$((failures + 1))
    return
  fi
  if grep -q "${COGNITO_ID_TOKEN}" "${work}/curl-args.log" "${work}/out.log"; then
    echo "FAIL ${name}: the ID token appears in the curl arguments or in the output"
    failures=$((failures + 1))
    return
  fi
  echo "ok   ${name} (exit ${actual})"
}

consistent='{"count":3,"total_deposited":"30.00","total_withdrawn":"10.00","replayed_balance":"20.00","stored_balance":"20.00","balances_match":true,"chain_ok":true,"duplicate_keys":0,"counts_by_region":{"us-east-2":2,"us-west-2":1},"consistent":true,"first_offender":null,"truncated":false}'
inconsistent='{"count":3,"replayed_balance":"20.00","stored_balance":"25.00","balances_match":false,"chain_ok":true,"duplicate_keys":0,"counts_by_region":{},"consistent":false,"first_offender":"4f0c0000-0000-0000-0000-000000000003","truncated":false}'
truncated='{"count":50000,"balances_match":null,"chain_ok":true,"duplicate_keys":0,"counts_by_region":{},"consistent":null,"first_offender":null,"truncated":true}'

run_case "consistent" 0 200 "${consistent}"
run_case "consistent, report nested under report" 0 200 "{\"report\":${consistent},\"served_by_region\":\"us-east-2\"}"
run_case "inconsistent" 1 200 "${inconsistent}"
run_case "truncated" 2 200 "${truncated}"
run_case "HTTP 503" 1 503 '{"error":{"code":"database_unavailable"}}'
run_case "connection failure" 1 000 ''
run_case "body is not JSON" 1 200 'not json'

# Missing configuration must exit 1 (exit 2 is reserved for truncated).
: >"${work}/curl-args.log"
actual=0
env -u COGNITO_ID_TOKEN STUB_CODE=200 bash "${SCRIPT}" >"${work}/out.log" 2>&1 || actual=$?
if ((actual == 1)); then echo "ok   missing COGNITO_ID_TOKEN (exit 1)"; else
  echo "FAIL missing COGNITO_ID_TOKEN: exit ${actual}, expected 1"
  failures=$((failures + 1))
fi

# --dry-run calls nothing, even without the variables.
: >"${work}/curl-args.log"
actual=0
env -u COGNITO_ID_TOKEN -u API_DOMAIN_NAME bash "${SCRIPT}" --dry-run >"${work}/out.log" 2>&1 || actual=$?
if ((actual == 0)) && [[ ! -s "${work}/curl-args.log" ]]; then echo "ok   --dry-run (exit 0, no curl call)"; else
  echo "FAIL --dry-run: exit ${actual} or curl was called"
  failures=$((failures + 1))
fi

if ((failures > 0)); then
  echo "${failures} case(s) failed"
  exit 1
fi
echo "all cases passed"
