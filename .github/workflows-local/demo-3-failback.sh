#!/usr/bin/env bash
# demo-3-failback.sh - Local equivalent of .github/workflows/demo-3-failback.yml. Demo step 3 of 3, no parameters.
#
# Planned failback to the PRIMARY region (return to the resting state): unfence the primary, move the database roles
# back, deactivate the Cognito replica, switch the weights back. It runs
#   failover.sh --direction to-primary --confirm failover
# after one y/N question (skipped with --yes). Diagram: docs/dr-failback-flow.png.
#
# Usage:
#   bash .github/workflows-local/demo-3-failback.sh [--yes] [--dry-run] [--force] [--skip-http-validation] [-h|--help]
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

confirm_yes "Demo 3: fail back from ${AWS_REGION_SECONDARY} to ${AWS_REGION_PRIMARY} (return to the resting state)?" "${assume_yes}"
exec bash "${WORKFLOWS_LOCAL_DIR}/failover.sh" --direction to-primary --confirm failover "${pass[@]}"
