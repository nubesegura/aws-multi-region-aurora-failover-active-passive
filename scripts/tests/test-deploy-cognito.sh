#!/usr/bin/env bash
# test-deploy-cognito.sh - Offline verification of Cognito multi-region deployment, API parameter passing,
# teardown sequence, and local workflow runners against stubbed AWS and SAM CLIs (no network, no AWS calls).
#
# Asserts (task 3.2):
#   1. scripts/deploy.sh step order includes cognito before api.
#   2. scripts/deploy.sh --steps cognito deploys in two passes: the primary stack in AWS_REGION_PRIMARY (us-east-2)
#      with EnableReplica=false, the replica-side stack in AWS_REGION_SECONDARY (us-west-2) with EnableReplica=false
#      (KMS ReplicaKey), the primary with EnableReplica=true, the replica side with EnableReplica=true, and finally the
#      primary with UserPoolDeletionProtection=ACTIVE; PrimaryKeyArn, PrimaryUserPoolId, BankClientId and
#      DashboardClientId come from the primary stack outputs.
#   3. scripts/deploy.sh --steps api reads /drdemo/prod/cognito/{UserPoolArn,BankClientId,DashboardClientId}
#      from SSM (us-east-2 for the us-east-2 API; us-west-2 for the us-west-2 API) and passes them to sam deploy,
#      and NEVER reads /drdemo/prod/api-key or passes ApiKeyValue.
#   4. scripts/destroy.sh --steps 3 deletes the API stacks before the Cognito stacks, lifts DeletionProtection on
#      the primary user pool, deactivates the replica attachment, deletes the replica Cognito stack before the
#      primary Cognito stack, and schedules KMS key deletion with a 7-day window.
#   5. .github/workflows-local/{deploy,destroy,simulate-outage,failover}.sh --help and --dry-run succeed cleanly.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
mkdir -p "${work}/bin"

if ! command -v jq >/dev/null 2>&1; then
  cat >"${work}/bin/jq" <<'JQSTUB'
#!/usr/bin/env bash
py_bin="python"
python --version >/dev/null 2>&1 || py_bin="python3"
null_input=0
expr=""
file=""
declare -a kv_args=()
while (($# > 0)); do
  case "$1" in
    -r | -c | -e) ;;
    -n) null_input=1 ;;
    --arg)
      kv_args+=("$2=$3")
      shift 2
      ;;
    *)
      if [[ -z "${expr}" ]]; then
        expr="$1"
      else
        file="$1"
      fi
      ;;
  esac
  shift
done
if ((null_input == 1)); then
  "${py_bin}" -c '
import json, sys
sys.stdout.reconfigure(newline="\n")
d = dict(a.split("=", 1) for a in sys.argv[1:])
print(json.dumps({"apiBaseUrl": d.get("url", "")}))
' "${kv_args[@]}"
elif [[ -n "${file}" ]]; then
  "${py_bin}" -c '
import json, re, sys
sys.stdout.reconfigure(newline="\n")
expr = sys.argv[1]
raw = sys.stdin.read()
data = json.loads(raw) if raw.strip() else {}
m = re.match(r"^\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s*\|\s*to_entries\[\]", expr)
if m:
    sub = data.get(m.group(1), {}).get(m.group(2), {})
    for k, v in sub.items():
        print(f"{k}={v}")
elif "GlobalClusters" in expr:
    print("")
else:
    print(json.dumps(data))
' "${expr}" <"${file}"
else
  "${py_bin}" -c '
import json, re, sys
sys.stdout.reconfigure(newline="\n")
expr = sys.argv[1]
raw = sys.stdin.read()
data = json.loads(raw) if raw.strip() else {}
if "GlobalClusters" in expr:
    print("")
else:
    print(json.dumps(data))
' "${expr}"
fi
JQSTUB
  chmod +x "${work}/bin/jq"
fi

cat >"${work}/bin/aws" <<'STUB'
#!/usr/bin/env bash
printf 'aws %s\n' "$*" >>"${STUB_DIR}/calls.log"
case "$1 $2" in
  "sts get-caller-identity")
    if [[ "$*" == *"Arn"* ]]; then
      printf 'arn:aws:iam::123456789012:role/stub\n'
    else
      printf '123456789012\n'
    fi
    ;;
  "apigateway get-account")
    printf 'arn:aws:iam::123456789012:role/apigw-cw-role\n'
    ;;
  "cloudformation describe-stacks")
    region=""
    stack=""
    args=("$@")
    for ((i=0; i<${#args[@]}; i++)); do
      [[ "${args[i]}" == "--region" ]] && region="${args[i+1]}"
      [[ "${args[i]}" == "--stack-name" ]] && stack="${args[i+1]}"
    done
    if [[ "$*" == *"StackStatus"* ]]; then
      printf 'CREATE_COMPLETE\n'
    elif [[ "$*" == *"OutputKey"* ]]; then
      key="$(grep -oE "OutputKey=='[A-Za-z0-9]+'" <<<"$*" | head -n 1 | cut -d"'" -f2)"
      case "${key}" in
        CfnExecRoleArn) printf 'arn:aws:iam::123456789012:role/iamr-useast1-drdemo-cfnexec-prod\n' ;;
        ArtifactBucketName) printf 'bckt-stub-artifacts\n' ;;
        DistributionDomainName) printf 'mon.example.net\n' ;;
        BankDistributionDomainName) printf 'bank.example.net\n' ;;
        DbClusterEndpoint) printf 'db.example.internal\n' ;;
        DbClusterResourceId) printf 'cluster-ABCDEFGHIJKLMN\n' ;;
        PrivateSubnetIds) printf 'subnet-11111111,subnet-22222222\n' ;;
        LambdaSecurityGroupId) printf 'sg-11111111\n' ;;
        KmsKeyArn) printf 'arn:aws:kms:us-east-2:123456789012:key/11111111-2222-3333-4444-555555555555\n' ;;
        ReplicaKmsKeyArn) printf 'arn:aws:kms:us-west-2:123456789012:key/11111111-2222-3333-4444-555555555555\n' ;;
        AlertTopicArn) printf 'arn:aws:sns:us-east-2:123456789012:sns-topic\n' ;;
        UserPoolId) printf 'us-east-2_StubPool123\n' ;;
        BankClientId) printf 'bankclientstub12345\n' ;;
        DashboardClientId) printf 'dashclientstub12345\n' ;;
        *) printf 'stub-output-%s\n' "${key}" ;;
      esac
    elif [[ "$*" == *"ParameterKey=='EnableReplica'"* ]]; then
      printf '%s\n' "${STUB_ENABLE_REPLICA:-None}"
    elif [[ "$*" == *"Stacks[0].Parameters"* ]]; then
      printf '[{"ParameterKey":"EnvType","ParameterValue":"prod"},{"ParameterKey":"PrimaryUserPoolId","ParameterValue":"us-east-2_StubPool123"},{"ParameterKey":"ReplicaAttachmentStatus","ParameterValue":"ACTIVE"}]\n'
    else
      printf '[]\n'
    fi
    ;;
  "cloudformation deploy" | "cloudformation delete-stack" | "cloudformation wait")
    exit 0
    ;;
  "ssm get-parameter")
    region=""
    name=""
    args=("$@")
    for ((i=0; i<${#args[@]}; i++)); do
      [[ "${args[i]}" == "--region" ]] && region="${args[i+1]}"
      [[ "${args[i]}" == "--name" ]] && name="${args[i+1]}"
    done
    case "${name}" in
      /drdemo/prod/cognito/UserPoolId) printf 'us-east-2_StubPool123\n' ;;
      /drdemo/prod/cognito/UserPoolArn)
        printf 'arn:aws:cognito-idp:%s:123456789012:userpool/us-east-2_StubPool123\n' "${region}"
        ;;
      /drdemo/prod/cognito/BankClientId) printf 'bankclientstub12345\n' ;;
      /drdemo/prod/cognito/DashboardClientId) printf 'dashclientstub12345\n' ;;
      *) echo "stub aws: unexpected ssm parameter ${name}" >&2; exit 254 ;;
    esac
    ;;
  "cognito-idp update-user-pool")
    exit 0
    ;;
  "kms describe-key")
    printf 'Enabled\n'
    ;;
  "kms schedule-key-deletion")
    exit 0
    ;;
  "rds describe-global-clusters")
    printf '{"GlobalClusters":[]}\n'
    ;;
  *)
    echo "stub aws: unexpected call: $*" >&2
    exit 3
    ;;
esac
STUB

cat >"${work}/bin/sam" <<'STUB'
#!/usr/bin/env bash
printf 'sam %s\n' "$*" >>"${STUB_DIR}/calls.log"
exit 0
STUB

chmod +x "${work}/bin/aws" "${work}/bin/sam"

export STUB_DIR="${work}"
export PATH="${work}/bin:${PATH}"
export AWS_REGION_PRIMARY=us-east-2 AWS_REGION_SECONDARY=us-west-2 AWS_REGION_GLOBAL=us-east-1
export API_DOMAIN_NAME="api.example.com" HOSTED_ZONE_ID=Z0000000000000 GITHUB_REPOSITORY=example/aws-multi-region-aurora-failover-active-passive

failures=0
reset_stub() { rm -f "${work}/calls.log"; touch "${work}/calls.log"; }
pass() { printf 'ok   %s\n' "$1"; }
fail() {
  printf 'FAIL %s\n' "$1"
  failures=$((failures + 1))
}
check() {
  if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1 (expected '$2', got '$3')"; fi
}
expect_has() {
  if grep -qF -- "$2" <<<"$1"; then pass "$3"; else fail "$3 (missing: $2)"; fi
}

# Ensure global bundle exists or create temporary dummy if absent
bundle_created=0
if [[ ! -f "${REPO_ROOT}/src/backend/certs/global-bundle.pem" ]]; then
  mkdir -p "${REPO_ROOT}/src/backend/certs"
  printf 'stub-cert\n' >"${REPO_ROOT}/src/backend/certs/global-bundle.pem"
  bundle_created=1
fi
cleanup_all() {
  ((bundle_created == 1)) && rm -f "${REPO_ROOT}/src/backend/certs/global-bundle.pem"
  rm -rf "${REPO_ROOT}/.github/workflows-local/.runs" "${REPO_ROOT}/.github/workflows-local/.locks"
  rm -rf "${work}"
}
trap cleanup_all EXIT

# 1. deploy.sh --dry-run step order includes cognito before api and never references api-key
reset_stub
out="$(bash "${REPO_ROOT}/scripts/deploy.sh" --dry-run 2>&1)"
expect_has "${out}" "steps: bootstrap edge cognito network db-primary db-secondary schema api dns dashboard" \
  "deploy.sh default step order includes cognito before api"
if grep -vE '^\[(dashboard|bank)\]' <<<"${out}" | grep -qi "api-key\|ApiKeyValue"; then
  fail "deploy.sh --dry-run still references api-key or ApiKeyValue"
else
  pass "deploy.sh --dry-run does not reference api-key or ApiKeyValue"
fi

# 2. deploy.sh --steps cognito (live stubbed): two passes, then deletion protection ACTIVE
reset_stub
out="$(bash "${REPO_ROOT}/scripts/deploy.sh" --steps cognito 2>&1)"
calls="$(cat "${work}/calls.log")"
expect_has "${calls}" "aws cloudformation deploy --region us-east-2 --stack-name stk-useast2-drdemo-cognito-prod"   "cognito step deploys primary stack in us-east-2"
expect_has "${calls}" "aws cloudformation deploy --region us-west-2 --stack-name stk-uswest2-drdemo-cognito-prod"   "cognito step deploys replica stack in us-west-2"
expect_has "${calls}" "PrimaryUserPoolId=us-east-2_StubPool123"   "cognito replica stack receives PrimaryUserPoolId from primary stack"
expect_has "${calls}" "BankClientId=bankclientstub12345"   "cognito replica stack receives BankClientId from primary stack"
expect_has "${calls}" "DashboardClientId=dashclientstub12345"   "cognito replica stack receives DashboardClientId from primary stack"
expect_has "${calls}" "PrimaryRegion=us-east-2" "cognito stacks receive PrimaryRegion=us-east-2"

# Sequence of the deploy calls: east(false) -> west(false) -> east(true) -> west(true) -> east(ACTIVE)
mapfile -t cog_calls < <(grep "cloudformation deploy .*drdemo-cognito-prod" "${work}/calls.log")
seq_ok=1
if ((${#cog_calls[@]} != 5)); then
  seq_ok=0
else
  [[ "${cog_calls[0]}" == *"--region us-east-2"* && "${cog_calls[0]}" == *"EnableReplica=false"* && "${cog_calls[0]}" == *"UserPoolDeletionProtection=INACTIVE"* ]] || seq_ok=0
  [[ "${cog_calls[1]}" == *"--region us-west-2"* && "${cog_calls[1]}" == *"EnableReplica=false"* ]] || seq_ok=0
  [[ "${cog_calls[2]}" == *"--region us-east-2"* && "${cog_calls[2]}" == *"EnableReplica=true"* && "${cog_calls[2]}" == *"UserPoolDeletionProtection=INACTIVE"* ]] || seq_ok=0
  [[ "${cog_calls[3]}" == *"--region us-west-2"* && "${cog_calls[3]}" == *"EnableReplica=true"* ]] || seq_ok=0
  [[ "${cog_calls[4]}" == *"--region us-east-2"* && "${cog_calls[4]}" == *"EnableReplica=true"* && "${cog_calls[4]}" == *"UserPoolDeletionProtection=ACTIVE"* ]] || seq_ok=0
fi
if ((seq_ok == 1)); then
  pass "cognito order: east(false) -> west(false, ReplicaKey) -> east(true, replica) -> west(true) -> east(deletion protection ACTIVE)"
else
  fail "cognito two-pass order wrong (${#cog_calls[@]} deploy calls)"
fi

# 2b. an existing primary stack with EnableReplica=true skips pass 1 (false would delete the replica)
reset_stub
STUB_ENABLE_REPLICA=true bash "${REPO_ROOT}/scripts/deploy.sh" --steps cognito >/dev/null 2>&1
mapfile -t cog_calls < <(grep "cloudformation deploy .*drdemo-cognito-prod" "${work}/calls.log")
if ((${#cog_calls[@]} == 3)) && ! grep "cloudformation deploy .*drdemo-cognito-prod" "${work}/calls.log" | grep -q "EnableReplica=false"; then
  pass "cognito step skips pass 1 when the replica is already enabled"
else
  fail "cognito step must not deploy EnableReplica=false over an enabled replica (${#cog_calls[@]} deploy calls)"
fi

# 3. deploy.sh --steps api reads regional Cognito SSM parameters and never reads /drdemo/prod/api-key
reset_stub
out="$(bash "${REPO_ROOT}/scripts/deploy.sh" --steps api 2>&1)"
calls="$(cat "${work}/calls.log")"
expect_has "${calls}" "ssm get-parameter --region us-east-2 --name /drdemo/prod/cognito/UserPoolArn" \
  "api step reads primary UserPoolArn from us-east-2 SSM for us-east-2 API"
expect_has "${calls}" "ssm get-parameter --region us-west-2 --name /drdemo/prod/cognito/UserPoolArn" \
  "api step reads replica UserPoolArn from us-west-2 SSM for us-west-2 API"
expect_has "${calls}" "CognitoUserPoolArn=arn:aws:cognito-idp:us-east-2:123456789012:userpool/us-east-2_StubPool123" \
  "us-east-2 API receives us-east-2 CognitoUserPoolArn"
expect_has "${calls}" "CognitoUserPoolArn=arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-east-2_StubPool123" \
  "us-west-2 API receives us-west-2 CognitoUserPoolArn"
expect_has "${calls}" "CognitoBankClientId=bankclientstub12345" \
  "API stacks receive CognitoBankClientId"
expect_has "${calls}" "CognitoDashboardClientId=dashclientstub12345" \
  "API stacks receive CognitoDashboardClientId"
if grep -q "api-key\|ApiKeyValue" "${work}/calls.log"; then
  fail "api step still queried api-key or passed ApiKeyValue"
else
  pass "api step never queries api-key or passes ApiKeyValue"
fi

# 4. destroy.sh --steps 3 deletes API stacks first, lifts DeletionProtection, deletes replica Cognito before primary, schedules KMS deletion
reset_stub
out="$(bash "${REPO_ROOT}/scripts/destroy.sh" --steps 3 --confirm destroy-aws-multi-region-aurora-failover-active-passive --confirm-deletion-protection disable-deletion-protection 2>&1)"
calls="$(cat "${work}/calls.log")"
expect_has "${calls}" "cognito-idp update-user-pool --region us-east-2 --user-pool-id us-east-2_StubPool123 --deletion-protection INACTIVE" \
  "destroy lifts DeletionProtection on primary Cognito User Pool"
expect_has "${calls}" "ReplicaAttachmentStatus=INACTIVE" \
  "destroy sets Cognito replica regional attachment to INACTIVE"
expect_has "${calls}" "cloudformation delete-stack --region us-west-2 --stack-name stk-uswest2-drdemo-cognito-prod" \
  "destroy deletes replica Cognito stack in us-west-2"
expect_has "${calls}" "cloudformation delete-stack --region us-east-2 --stack-name stk-useast2-drdemo-cognito-prod" \
  "destroy deletes primary Cognito stack in us-east-2"
expect_has "${calls}" "kms schedule-key-deletion --region us-west-2" \
  "destroy schedules replica KMS key deletion with 7-day window"
expect_has "${calls}" "kms schedule-key-deletion --region us-east-2" \
  "destroy schedules primary KMS key deletion with 7-day window"

del_api_line="$(grep -n "delete-stack.*drdemo-api-prod" "${work}/calls.log" | tail -n 1 | cut -d: -f1)"
del_cog_west="$(grep -n "delete-stack --region us-west-2 --stack-name stk-uswest2-drdemo-cognito-prod" "${work}/calls.log" | head -n 1 | cut -d: -f1)"
del_cog_east="$(grep -n "delete-stack --region us-east-2 --stack-name stk-useast2-drdemo-cognito-prod" "${work}/calls.log" | head -n 1 | cut -d: -f1)"
if [[ -n "${del_api_line}" && -n "${del_cog_west}" && -n "${del_cog_east}" ]] && ((del_api_line < del_cog_west && del_cog_west < del_cog_east)); then
  pass "destroy order: api stacks -> us-west-2 cognito replica stack -> us-east-2 cognito primary stack"
else
  fail "destroy order wrong (api=${del_api_line:-none}, cog_west=${del_cog_west:-none}, cog_east=${del_cog_east:-none})"
fi

# 5. Local workflow runners (--help and --dry-run)
for runner in deploy.sh destroy.sh simulate-outage.sh failover.sh; do
  if bash "${REPO_ROOT}/.github/workflows-local/${runner}" --help >/dev/null 2>&1; then
    pass ".github/workflows-local/${runner} --help"
  else
    fail ".github/workflows-local/${runner} --help failed"
  fi
done

if out="$(bash "${REPO_ROOT}/.github/workflows-local/failover.sh" --direction to-secondary --confirm failover --dry-run --force 2>&1)"; then
  expect_has "${out}" "ReplicaAttachmentStatus=ACTIVE" \
    "failover.sh to-secondary activates Cognito replica in us-west-2"
else
  fail "failover.sh to-secondary --dry-run failed: ${out}"
fi

if out="$(bash "${REPO_ROOT}/.github/workflows-local/failover.sh" --direction to-primary --confirm failover --dry-run --force 2>&1)"; then
  expect_has "${out}" "ReplicaAttachmentStatus=INACTIVE" \
    "failover.sh to-primary deactivates Cognito replica in us-west-2"
else
  fail "failover.sh to-primary --dry-run failed: ${out}"
fi

if ((failures > 0)); then
  echo "${failures} case(s) failed"
  exit 1
fi
echo "all cases passed"
