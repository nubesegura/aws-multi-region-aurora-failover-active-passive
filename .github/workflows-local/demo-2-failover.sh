#!/usr/bin/env bash
# demo-2-failover.sh - Local equivalent of .github/workflows/demo-2-failover.yml. Demo step 2 of 3, no parameters.
#
# Planned failover to the SECONDARY region. It runs
#   failover.sh --direction to-secondary --confirm failover
# after one y/N question (skipped with --yes). Run demo-1-outage.sh first: failover.sh stops unless the primary region
# is fenced. Diagram: docs/dr-failover-flow.png. Next step: demo-3-failback.sh.
#
# Usage:
#   bash .github/workflows-local/demo-2-failover.sh [--yes] [--dry-run] [--force] [--skip-http-validation] [-h|--help]
#     --yes                    do not ask y/N
#     --dry-run                no AWS call
#     --force                  run from a branch other than main
#     --skip-http-validation   skip 40-validate.sh and 45-consistency.sh
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
    --force | --skip-http-validation) pass+=("$1") ;;
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
require_local_env AWS_REGION_PRIMARY AWS_REGION_SECONDARY

confirm_yes "Demo 2: fail over from ${AWS_REGION_PRIMARY} to ${AWS_REGION_SECONDARY} (planned switchover)?" "${assume_yes}"
exec bash "${WORKFLOWS_LOCAL_DIR}/failover.sh" --direction to-secondary --confirm failover "${pass[@]}"
