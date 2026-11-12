#!/usr/bin/env bash
# test-lag-gate.sh - lag_gate (scripts/lib/promote.sh) against a stub aws on the PATH (no network, no AWS call).
#
# Cases: a datapoint that appears after two empty polls passes; a lag above the limit fails; no datapoint for the whole
# wait fails closed with the "no replication lag datapoint" message; a failed CloudWatch query fails with the permission
# message; --force lets a missing datapoint pass with a warning.
#
# UNDO:    nothing to undo (works in a temporary directory that is removed on exit).
# Usage:   scripts/tests/test-lag-gate.sh
# Exit:    0 all cases behaved as expected; 1 at least one case failed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
mkdir -p "${work}/bin"

# Stub aws: each cloudwatch get-metric-statistics call consumes the next line of ${STUB_DIR}/answers:
#   none  -> prints None (no datapoint), error -> exits 255 (failed query), <number> -> prints it.
cat >"${work}/bin/aws" <<'STUB'
#!/usr/bin/env bash
case "$1 $2" in
  "cloudwatch get-metric-statistics")
    answer="$(head -n 1 "${STUB_DIR}/answers")"
    sed -i '1d' "${STUB_DIR}/answers"
    case "${answer:-none}" in
      none) printf 'None\n' ;;
      error) echo "AccessDenied" >&2; exit 255 ;;
      *) printf '%s\n' "${answer}" ;;
    esac
    ;;
  *) echo "stub aws: unexpected call: $*" >&2; exit 3 ;;
esac
STUB
chmod +x "${work}/bin/aws"
export STUB_DIR="${work}"
export PATH="${work}/bin:${PATH}"

failures=0

# run_case NAME EXPECTED_EXIT EXPECTED_TEXT FORCE ANSWERS...
run_case() {
  local name="$1" expected_exit="$2" expected_text="$3" force="$4" rc=0 output
  shift 4
  printf '%s\n' "$@" >"${work}/answers"
  output="$(
    cd "${REPO_ROOT}" &&
      bash -c '
        set -euo pipefail
        export AWS_REGION_PRIMARY=us-east-2 AWS_REGION_SECONDARY=us-west-2 AWS_REGION_GLOBAL=us-east-1
        # shellcheck source=/dev/null
        source scripts/lib/common.sh
        # shellcheck source=/dev/null
        source scripts/lib/dr.sh
        # shellcheck source=/dev/null
        source scripts/lib/promote.sh
        PROMOTE_MAX_LAG_MS=5000 PROMOTE_FORCE="$1" PROMOTE_LAG_WAIT_SECONDS=2 PROMOTE_LAG_POLL_SECONDS=1
        lag_gate us-east-2
      ' _ "${force}" 2>&1
  )" || rc=$?
  if [[ "${rc}" -ne "${expected_exit}" ]] || ! grep -qF -- "${expected_text}" <<<"${output}"; then
    echo "FAIL ${name}: exit ${rc} (expected ${expected_exit}), output:"
    while IFS= read -r line; do printf '  | %s\n' "${line}"; done <<<"${output}"
    failures=$((failures + 1))
  else
    echo "ok   ${name}"
  fi
}

run_case "datapoint appears after two empty polls" 0 "replication lag of the cluster in us-east-2: 938.0 ms" 0 none none 938.0
run_case "lag above the limit" 1 "is above 5000 ms" 0 84262.0
run_case "no datapoint for the whole wait" 1 "no replication lag datapoint for us-east-2 after waiting 2 s" 0 none none none none
run_case "failed CloudWatch query" 1 "the CloudWatch query failed" 0 error
run_case "--force lets a missing datapoint pass" 0 "continuing because --force was given" 1 none none none none

if ((failures > 0)); then
  echo "${failures} case(s) failed"
  exit 1
fi
echo "all lag gate cases passed"
