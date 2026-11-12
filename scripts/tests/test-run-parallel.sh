#!/usr/bin/env bash
# test-run-parallel.sh - run_parallel (scripts/lib/common.sh): jobs run at the same time, every line is prefixed with
# the job label, a failed job is reported by label and the other job still finishes; nothing is retried.
#
# UNDO:    nothing to undo (works in a temporary directory that is removed on exit).
# Usage:   scripts/tests/test-run-parallel.sh
# Exit:    0 all cases behaved as expected; 1 at least one case failed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
export WORK="${work}"

failures=0
check() { # NAME CONDITION_RESULT DETAILS
  if [[ "$2" == "0" ]]; then
    echo "ok   $1"
  else
    echo "FAIL $1: $3"
    failures=$((failures + 1))
  fi
}

# run_jobs SCRIPT ; sources common.sh and runs the given bash snippet; prints its output, returns its exit code
run_jobs() {
  (
    cd "${REPO_ROOT}" &&
      bash -c '
        set -euo pipefail
        export AWS_REGION_PRIMARY=us-east-2 AWS_REGION_SECONDARY=us-west-2 AWS_REGION_GLOBAL=us-east-1
        # shellcheck source=/dev/null
        source scripts/lib/common.sh
        slow_ok() { sleep 2; echo "done $1"; touch "${WORK}/$1.done"; }
        fail_fast() { echo "failing $1"; die "job $1 failed on purpose"; }
        '"$1"'
      ' 2>&1
  )
}

# 1. Two 2-second jobs finish in well under 4 seconds (they ran at the same time) and every line carries its label.
start="$(date +%s)"
rc=0
output="$(run_jobs 'run_parallel east slow_ok a west slow_ok b')" || rc=$?
elapsed=$(($(date +%s) - start))
check "both jobs succeed" "${rc}" "exit ${rc}"
[[ ${elapsed} -lt 4 ]] && r=0 || r=1
check "jobs run at the same time (${elapsed} s for two 2 s jobs)" "${r}" "took ${elapsed} s"
grep -q '^\[east\] done a$' <<<"${output}" && grep -q '^\[west\] done b$' <<<"${output}" && r=0 || r=1
check "every line is prefixed with its label" "${r}" "${output}"

# 2. One job fails: the step fails, names the label, and the other job still completes.
rm -f "${work}"/*.done
rc=0
output="$(run_jobs 'run_parallel east slow_ok a west fail_fast b')" || rc=$?
[[ ${rc} -ne 0 ]] && r=0 || r=1
check "a failed job fails the step" "${r}" "exit ${rc}"
grep -q 'parallel step failed for: west' <<<"${output}" && r=0 || r=1
check "the failed job is named by its label" "${r}" "${output}"
[[ -f "${work}/a.done" ]] && r=0 || r=1
check "the other job still finished" "${r}" "a.done missing"

# 3. Wrong arguments are refused.
rc=0
output="$(run_jobs 'run_parallel east slow_ok')" || rc=$?
[[ ${rc} -ne 0 ]] && grep -q 'needs triplets' <<<"${output}" && r=0 || r=1
check "arguments must come in triplets" "${r}" "${output}"

if ((failures > 0)); then
  echo "${failures} case(s) failed"
  exit 1
fi
echo "all run_parallel cases passed"
