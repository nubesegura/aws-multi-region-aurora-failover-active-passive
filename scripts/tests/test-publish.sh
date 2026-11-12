#!/usr/bin/env bash
# test-publish.sh - publish_site (scripts/lib/publish.sh), the two publishers and destroy.sh --dry-run against a stub aws
# on the PATH (no network, no AWS call).
#
# Cases: staged file list of src/dashboard and src/bank (tests, package files, README and mock helpers left out; config.json
# rewritten from API_DOMAIN_NAME); no legacy API key parameter is ever read; an empty bucket or
# distribution output aborts before any upload or deletion; stale objects are deleted and kept ones are not (keys with
# spaces included); destroy.sh --dry-run lists both buckets.
#
# UNDO:    nothing to undo (works in a temporary directory that is removed on exit).
# Usage:   scripts/tests/test-publish.sh
# Needs:   bash 4+, jq
# Exit:    0 all cases behaved as expected; 1 at least one case failed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
mkdir -p "${work}/bin"

if ! command -v jq >/dev/null 2>&1; then
  cat >"${work}/bin/jq" <<'JQSTUB'
#!/usr/bin/env bash
py_bin="python3"
if ! command -v python3 >/dev/null 2>&1; then
  py_bin="python"
fi
"${py_bin}" -c '
import sys, json

args = sys.argv[1:]
if not args:
    sys.exit(0)

if args[0] == "-n":
    kwargs = {}
    i = 1
    while i < len(args):
        if args[i] == "--arg":
            kwargs[args[i+1]] = args[i+2]
            i += 3
        else:
            break
    if "cognito" in args[-1]:
        res = {
            "apiBaseUrl": kwargs.get("url"),
            "cognito": {
                "primary": {
                    "region": kwargs.get("p_region"),
                    "userPoolId": kwargs.get("p_pool"),
                    "clientId": kwargs.get("p_client"),
                    "endpoint": kwargs.get("p_endpoint")
                },
                "standby": {
                    "region": kwargs.get("s_region"),
                    "userPoolId": kwargs.get("s_pool"),
                    "clientId": kwargs.get("s_client"),
                    "endpoint": kwargs.get("s_endpoint")
                }
            }
        }
        print(json.dumps(res, indent=2))
    else:
        res = {"apiBaseUrl": kwargs.get("url")}
        print(json.dumps(res, indent=2))
    sys.exit(0)

if "-r" in args:
    args = [a for a in args if a != "-r"]

expr = args[0]
filename = args[1] if len(args) > 1 else None
if filename:
    with open(filename, "r", encoding="utf-8") as f:
        data = json.load(f)
else:
    data = json.load(sys.stdin)

if expr == "(.Contents // [])[] | .Key":
    contents = data.get("Contents") or []
    for item in contents:
        print(item.get("Key", ""))
elif expr == ".apiBaseUrl":
    print(data.get("apiBaseUrl", ""))
elif "cognito.primary" in expr:
    p = data.get("cognito", {}).get("primary", {})
    reg, pool, cli, ep = p.get("region"), p.get("userPoolId"), p.get("clientId"), p.get("endpoint")
    print(f"{reg}|{pool}|{cli}|{ep}")
elif "cognito.standby" in expr:
    s = data.get("cognito", {}).get("standby", {})
    reg, pool, cli, ep = s.get("region"), s.get("userPoolId"), s.get("clientId"), s.get("endpoint")
    print(f"{reg}|{pool}|{cli}|{ep}")
else:
    print(json.dumps(data))
' "$@"
JQSTUB
  chmod +x "${work}/bin/jq"
fi

# Stub aws: records every call, answers the few read calls the publishers make and copies uploaded files.
#   STUB_OUT_<Key>   value of the stack output <Key> (empty or unset: prints None, as the real CLI does)
#   STUB_LIST        JSON answer of s3api list-objects-v2
cat >"${work}/bin/aws" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${STUB_DIR}/calls.log"
case "$1 $2" in
  "sts get-caller-identity") printf '123456789012\n' ;;
  "cloudformation describe-stacks")
    key="$(grep -oE "OutputKey=='[A-Za-z]+'" <<<"$*" | head -n 1 | cut -d"'" -f2)"
    var="STUB_OUT_${key}"
    printf '%s\n' "${!var:-None}"
    ;;
  "s3 cp")
    mkdir -p "${STUB_DIR}/uploaded"
    cp "$3" "${STUB_DIR}/uploaded/$(basename "$4")"
    ;;
  "s3api list-objects-v2") printf '%s\n' "${STUB_LIST:-{\}}" ;;
  "s3 rm") printf '%s\n' "$3" >>"${STUB_DIR}/removed.log" ;;
  "cloudfront create-invalidation") printf 'I123\tInProgress\n' ;;
  "ssm get-parameter")
    if [[ "$*" == *"/BankClientId"* ]]; then
      printf '%s\n' "${STUB_SSM_BankClientId:-cgnc-bank-stub}"
    elif [[ "$*" == *"/DashboardClientId"* ]]; then
      printf '%s\n' "${STUB_SSM_DashboardClientId:-cgnc-dash-stub}"
    elif [[ "$*" == *"/UserPoolId"* ]]; then
      printf '%s\n' "${STUB_SSM_UserPoolId:-us-east-2_StubPool}"
    else
      echo "stub aws: unexpected ssm parameter: $*" >&2
      exit 3
    fi
    ;;
  *) echo "stub aws: unexpected call: $*" >&2; exit 3 ;;
esac
STUB
chmod +x "${work}/bin/aws"

export STUB_DIR="${work}"
export PATH="${work}/bin:${PATH}"
export AWS_REGION_PRIMARY=us-east-2 AWS_REGION_SECONDARY=us-west-2 AWS_REGION_GLOBAL=us-east-1
export API_DOMAIN_NAME="api.example.com"
export STUB_OUT_DashboardBucketName="bckt-monitor" STUB_OUT_DistributionId="EDASHBOARD" STUB_OUT_DistributionDomainName="mon.example.net"
export STUB_OUT_BankBucketName="bckt-bank" STUB_OUT_BankDistributionId="EBANK" STUB_OUT_BankDistributionDomainName="bank.example.net"

failures=0
reset_stub() { rm -rf "${work}/uploaded" "${work}/calls.log" "${work}/removed.log"; touch "${work}/calls.log"; unset STUB_LIST; }
pass() { printf 'ok   %s\n' "$1"; }
fail() {
  printf 'FAIL %s\n' "$1"
  failures=$((failures + 1))
}
uploaded_list() { { find "${work}/uploaded" -maxdepth 1 -type f -printf '%f\n' 2>/dev/null || true; } | sort | tr '\n' ' '; }
check() { # name expected actual
  if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1 (expected '$2', got '$3')"; fi
}

# 1. staged file list of each site, config.json content, stale deletion, addresses
for site in dashboard bank; do
  reset_stub
  script="${REPO_ROOT}/scripts/publish-${site}.sh"
  expected="$(cd "${REPO_ROOT}/src/${site}" && for f in *; do
    [[ -f "${f}" ]] || continue
    case "${f}" in *.test.js | *.test.mjs | *-mock.mjs | package*.json | README*) continue ;; esac
    printf '%s\n' "${f}"
  done | { cat; echo config.json; echo error.html; } | sort -u | tr '\n' ' ')"
  [[ "${site}" == "dashboard" ]] && bucket="bckt-monitor" || bucket="bckt-bank"
  export STUB_LIST='{"Contents":[{"Key":"index.html"},{"Key":"old page.html"},{"Key":"legacy.js"}]}'
  if out="$(bash "${script}" 2>&1)"; then
    check "${site}: staged files equal the top-level source files" "${expected}" "$(uploaded_list)"
    expected_client="$([[ "${site}" == "dashboard" ]] && echo "cgnc-dash-stub" || echo "cgnc-bank-stub")"
    check "${site}: config.json apiBaseUrl" "https://api.example.com" "$(jq -r '.apiBaseUrl' "${work}/uploaded/config.json")"
    check "${site}: config.json cognito.primary" "us-east-2|us-east-2_StubPool|${expected_client}|https://cognito-idp.us-east-2.amazonaws.com" "$(jq -r '[.cognito.primary.region, .cognito.primary.userPoolId, .cognito.primary.clientId, .cognito.primary.endpoint] | join("|")' "${work}/uploaded/config.json")"
    check "${site}: config.json cognito.standby" "us-west-2|us-east-2_StubPool|${expected_client}|https://cognito-idp.us-west-2.amazonaws.com" "$(jq -r '[.cognito.standby.region, .cognito.standby.userPoolId, .cognito.standby.clientId, .cognito.standby.endpoint] | join("|")' "${work}/uploaded/config.json")"
    check "${site}: stale keys deleted, kept key left alone" "s3://${bucket}/old page.html s3://${bucket}/legacy.js " "$(tr '\n' ' ' <"${work}/removed.log" 2>/dev/null || true)"
    if grep -q "tests/\|logic.test.js" <<<"$(uploaded_list)"; then
      fail "${site}: a test file was staged"
    else
      pass "${site}: no test file staged"
    fi
    if grep -q "s3://${bucket}/" "${work}/calls.log" && ! grep -v "s3://${bucket}/" <(grep -E "^s3 (cp|rm)" "${work}/calls.log") | grep -q .; then
      pass "${site}: only its own bucket was written"
    else
      fail "${site}: wrote to a bucket other than ${bucket}"
    fi
    if grep -q "^cloudfront create-invalidation --distribution-id E$([[ ${site} == dashboard ]] && echo DASHBOARD || echo BANK)" "${work}/calls.log"; then
      pass "${site}: invalidation of its own distribution"
    else
      fail "${site}: wrong invalidation"
    fi
    if grep -q "https://$([[ ${site} == dashboard ]] && echo mon || echo bank).example.net/" <<<"${out}"; then
      pass "${site}: prints its address"
    else
      fail "${site}: address not printed"
    fi
    if grep -q "api-key" "${work}/calls.log"; then
      fail "${site}: the legacy api-key parameter was read"
    else
      pass "${site}: the legacy api-key parameter is never read"
    fi
  else
    fail "${site}: publish failed: ${out}"
  fi
done

# 2. temporary site used by the library cases below (tests, README, package files and mock helpers are excluded)
site_dir="${work}/site"
mkdir -p "${site_dir}/tests"
printf '<!doctype html><title>x</title>\n' >"${site_dir}/index.html"
printf 'x\n' >"${site_dir}/README.md"
printf '{}\n' >"${site_dir}/package.json"
printf 'x\n' >"${site_dir}/thing.test.js"
printf 'x\n' >"${site_dir}/local-mock.mjs"
printf 'x\n' >"${site_dir}/tests/t.js"
printf 'const k = 1;\n' >"${site_dir}/app.js"
run_lib() { # source_dir bucket_output distribution_output
  bash -c '
    set -euo pipefail
    export DRY_RUN=0
    source "$1/scripts/lib/common.sh"
    source "$1/scripts/lib/publish.sh"
    publish_site "$2" "$3" "$4"
  ' _ "${REPO_ROOT}" "$@"
}

# 3. staging of the temporary site: only the site files
reset_stub
if run_lib "${site_dir}" DashboardBucketName DistributionId >/dev/null 2>&1; then
  check "excluded files (tests, README, package.json, *.test.js, *-mock.mjs) are not staged" "app.js config.json error.html index.html " "$(uploaded_list)"
else
  fail "publish of the temporary site failed"
fi

# 4. empty outputs abort before any upload or deletion
for case_name in "bucket:STUB_OUT_DashboardBucketName" "distribution:STUB_OUT_DistributionId" "domain:STUB_OUT_DistributionDomainName"; do
  reset_stub
  var="${case_name#*:}"
  if out="$(env "${var}=" bash "${REPO_ROOT}/scripts/publish-dashboard.sh" 2>&1)"; then
    fail "empty ${case_name%%:*} output did not abort"
  else
    pass "empty ${case_name%%:*} output aborts"
  fi
  if grep -qE "^s3 (cp|rm)|^cloudfront" "${work}/calls.log"; then
    fail "empty ${case_name%%:*}: a write happened"
  else
    pass "empty ${case_name%%:*}: no upload, deletion or invalidation"
  fi
done
# the guard inside publish_site itself (stack_output replaced by a function that returns an empty value)
reset_stub
if out="$(bash -c '
    set -euo pipefail
    export DRY_RUN=0
    source "$1/scripts/lib/common.sh"
    source "$1/scripts/lib/publish.sh"
    stack_output() { printf ""; }
    publish_site "$2" DashboardBucketName DistributionId
  ' _ "${REPO_ROOT}" "${site_dir}" 2>&1)"; then
  fail "publish_site with an empty output did not abort"
else
  if grep -q "is empty: nothing was published" <<<"${out}"; then
    pass "publish_site guard on an empty output"
  else
    fail "guard message missing: ${out}"
  fi
fi

# 5. dry-run of both publishers makes no AWS call
reset_stub
for site in dashboard bank; do
  if bash "${REPO_ROOT}/scripts/publish-${site}.sh" --dry-run >/dev/null 2>&1; then
    pass "publish-${site}.sh --dry-run"
  else
    fail "publish-${site}.sh --dry-run failed"
  fi
done
if [[ ! -s "${work}/calls.log" ]]; then
  pass "dry-run made no aws call"
else
  fail "dry-run called aws: $(cat "${work}/calls.log")"
fi

# 6. destroy.sh --dry-run lists both buckets
reset_stub
export HOSTED_ZONE_ID=Z0000000000000 GITHUB_REPOSITORY=example/aws-multi-region-aurora-failover-active-passive
if out="$(bash "${REPO_ROOT}/scripts/destroy.sh" --dry-run 2>&1)"; then
  if grep -q "empty the dashboard and banking buckets" <<<"${out}"; then
    pass "destroy plan mentions both buckets"
  else
    fail "destroy plan text lacks both buckets"
  fi
  if grep -q "all versions and delete markers of s3://bckt-useast2-drdemo-dashboard-" <<<"${out}"; then
    pass "destroy empties the dashboard bucket"
  else
    fail "destroy does not empty the dashboard bucket"
  fi
  if grep -q "all versions and delete markers of s3://bckt-useast2-drdemo-bank-" <<<"${out}"; then
    pass "destroy empties the banking bucket"
  else
    fail "destroy does not empty the banking bucket"
  fi
  if grep -q "dashboard and banking buckets of us-east-2" <<<"${out}"; then
    pass "destroy leftover listing mentions both"
  else
    fail "leftover text lacks both buckets"
  fi
else
  fail "destroy.sh --dry-run failed: ${out}"
fi
if [[ ! -s "${work}/calls.log" ]]; then
  pass "destroy dry-run made no aws call"
else
  fail "destroy dry-run called aws"
fi

if ((failures > 0)); then
  echo "${failures} case(s) failed"
  exit 1
fi
echo "all cases passed"
