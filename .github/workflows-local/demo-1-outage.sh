#!/usr/bin/env bash
# demo-1-outage.sh - Local equivalent of .github/workflows/demo-1-outage.yml. Demo step 1 of 3, no parameters.
#
# Fences the PRIMARY region (AWS_REGION_PRIMARY of prod.env): reserved concurrency 0 on its six API functions. It runs
#   simulate-outage.sh --action fence --region <primary> --confirm <primary>
# after one y/N question (skipped with --yes). Diagram: docs/dr-outage-flow.png. Next step: demo-2-failover.sh.
#
# Usage:
#   bash .github/workflows-local/demo-1-outage.sh [--yes] [--dry-run] [--force] [-h|--help]
#     --yes      do not ask y/N
#     --dry-run  no AWS call
#     --force    run from a branch other than main
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

assume_yes=0
declare -a pass=()
while (($# > 0)); do
  case "$1" in
    --yes) assume_yes=1 ;;
    --dry-run)
      DRY_RUN=1
      pass+=(--dry-run)
      ;;
    --force) pass+=(--force) ;;
    -h | --help)
      sed -n '2,/^set -euo pipefail/{/^set -euo/!p;}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) fail "Unknown argument: $1 (this script takes no parameters; pass --help for usage)" ;;
  esac
  shift
done

export DRY_RUN
load_env
require_local_env AWS_REGION_PRIMARY

confirm_yes "Demo 1: fence ${AWS_REGION_PRIMARY} (simulated outage of the primary region)?" "${assume_yes}"
exec bash "${WORKFLOWS_LOCAL_DIR}/simulate-outage.sh" --action fence \
  --region "${AWS_REGION_PRIMARY}" --confirm "${AWS_REGION_PRIMARY}" "${pass[@]}"
