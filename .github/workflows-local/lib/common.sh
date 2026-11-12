#!/usr/bin/env bash
# Shared helper library for the local workflow runners (.github/workflows-local/).
# Mirrors the safety, locking, summary, and credential preflight checks of the GitHub Actions workflows
# so deployments, teardowns, outage simulations, and failover drills run locally without GitHub Actions minutes.
# shellcheck shell=bash

if [[ -n "${WORKFLOWS_LOCAL_COMMON_LOADED:-}" ]]; then
  return 0
fi
WORKFLOWS_LOCAL_COMMON_LOADED=1

WORKFLOWS_LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${WORKFLOWS_LOCAL_DIR}/../.." && pwd)"
readonly WORKFLOWS_LOCAL_DIR REPO_ROOT
readonly LOCKS_DIR="${WORKFLOWS_LOCAL_DIR}/.locks"
readonly RUNS_DIR="${WORKFLOWS_LOCAL_DIR}/.runs"

DRY_RUN="${DRY_RUN:-0}"
LOCK_HELD=""
RUN_SUMMARY_FILE=""
STEP_TIMINGS_FILE=""
RUN_STARTED_UTC=""

# ---------------------------------------------------------------------------------------------
# Logging and formatting
# ---------------------------------------------------------------------------------------------
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s [INFO]  %s\n' "$(ts)" "$*"; }
info() { printf '%s [INFO]  %s\n' "$(ts)" "$*"; }
warn() { printf '%s [WARN]  %s\n' "$(ts)" "$*" >&2; }
fail() {
  printf '%s [ERROR] %s\n' "$(ts)" "$*" >&2
  exit 1
}

is_dry_run() { [[ "${DRY_RUN}" == "1" ]]; }

# Redact 12-digit AWS account IDs from console output.
sanitize() { sed -E 's/[0-9]{12}/************/g'; }

# Strip Windows carriage returns (\r) when capturing command output on Git Bash / MSYS2.
capture() {
  local out
  out="$("$@" 2>&1 | tr -d '\r')"
  printf '%s' "${out}"
}

# Convert POSIX path to native Windows path when cygpath is available (Git Bash on Windows).
winpath() {
  local p="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "${p}"
  else
    printf '%s' "${p}"
  fi
}

# ---------------------------------------------------------------------------------------------
# Environment file loader (KEY=VALUE parser without eval)
# ---------------------------------------------------------------------------------------------
# load_env [ENV_FILE]
# Reads KEY=VALUE lines safely without eval. Existing non-empty environment variables win unless
# OVERRIDE_ENV=1 is set.
load_env() {
  local env_file="${1:-${WORKFLOWS_LOCAL_DIR}/prod.env}"
  if [[ ! -f "${env_file}" ]]; then
    if [[ -f "${WORKFLOWS_LOCAL_DIR}/config.example.env" ]] && is_dry_run; then
      log "[dry-run] ${env_file#"${REPO_ROOT}"/} not found; falling back to config.example.env"
      env_file="${WORKFLOWS_LOCAL_DIR}/config.example.env"
    else
      fail "Environment file not found: ${env_file#"${REPO_ROOT}"/}. Copy .github/workflows-local/config.example.env to .github/workflows-local/prod.env and fill in the values."
    fi
  fi

  local line key val
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    # Skip comments and blank lines
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue
    [[ "${line}" =~ ^[[:space:]]*$ ]] && continue
    if [[ "${line}" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      # Strip optional surrounding single or double quotes
      if [[ "${val}" =~ ^\"(.*)\"$ ]]; then
        val="${BASH_REMATCH[1]}"
      elif [[ "${val}" =~ ^\'(.*)\'$ ]]; then
        val="${BASH_REMATCH[1]}"
      fi
      if [[ "${OVERRIDE_ENV:-0}" == "1" || -z "${!key:-}" ]]; then
        export "${key}=${val}"
      fi
    fi
  done <"${env_file}"

  export AWS_PROFILE="${AWS_PROFILE:-tf-local-admin-prod}"
  export SAM_CLI_TELEMETRY="0"
}

# Verify required variables are non-empty.
require_local_env() {
  local -a missing=()
  local name
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      missing+=("${name}")
    fi
  done
  if ((${#missing[@]} > 0)); then
    fail "Missing required configuration variable(s) in prod.env or environment: ${missing[*]}"
  fi
}

# ---------------------------------------------------------------------------------------------
# Git branch check
# ---------------------------------------------------------------------------------------------
# verify_branch [EXPECTED_BRANCH] [FORCE_FLAG]
verify_branch() {
  local expected="${1:-main}" force="${2:-0}" current
  current="$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null | tr -d '\r')" || current="unknown"
  if [[ "${current}" != "${expected}" ]]; then
    if [[ "${force}" == "1" ]] || is_dry_run; then
      warn "Current git branch is '${current}' (expected '${expected}'); continuing (--force or --dry-run)."
    else
      fail "Local workflow must run from branch '${expected}' (current: '${current}'). Pass --force to override."
    fi
  fi
}

# ---------------------------------------------------------------------------------------------
# Concurrency lock (.locks/<name>.lock directory)
# ---------------------------------------------------------------------------------------------
acquire_lock() {
  local name="${1:-drdemo-prod-infrastructure}"
  mkdir -p "${LOCKS_DIR}"
  local lock_path="${LOCKS_DIR}/${name}.lock"
  if ! mkdir "${lock_path}" 2>/dev/null; then
    local holder=""
    [[ -f "${lock_path}/pid" ]] && holder=" (PID $(cat "${lock_path}/pid" 2>/dev/null || echo unknown))"
    fail "Another local workflow holds the lock '${name}'${holder}. Wait for it to finish or remove ${lock_path#"${REPO_ROOT}"/} if stale."
  fi
  printf '%s\n' "$$" >"${lock_path}/pid"
  printf '%s\n' "$(ts)" >"${lock_path}/started_at"
  LOCK_HELD="${lock_path}"
}

release_lock() {
  if [[ -n "${LOCK_HELD}" && -d "${LOCK_HELD}" ]]; then
    rm -rf "${LOCK_HELD}"
    LOCK_HELD=""
  fi
}

# ---------------------------------------------------------------------------------------------
# Interactive confirmation helpers
# ---------------------------------------------------------------------------------------------
confirm_typed() {
  local phrase="$1" prompt="$2" provided="${3:-}" answer
  if is_dry_run; then
    log "[dry-run] would require typed confirmation: '${phrase}'"
    return 0
  fi
  if [[ -n "${provided}" ]]; then
    [[ "${provided}" == "${phrase}" ]] || fail "Confirmation phrase did not match '${phrase}'; aborting."
    return 0
  fi
  [[ -t 0 ]] || fail "Interactive terminal required for confirmation '${phrase}' (or pass --confirm ${phrase})."
  printf '%s\nType "%s" to continue: ' "${prompt}" "${phrase}" >&2
  read -r answer
  answer="${answer%$'\r'}"
  [[ "${answer}" == "${phrase}" ]] || fail "Confirmation phrase did not match; nothing was changed."
}

confirm_yes() {
  local prompt="$1" assume_yes="${2:-0}" answer
  if is_dry_run || [[ "${assume_yes}" == "1" ]]; then
    return 0
  fi
  [[ -t 0 ]] || fail "Interactive terminal required to confirm (or pass --yes)."
  printf '%s [y/N]: ' "${prompt}" >&2
  read -r answer
  answer="${answer%$'\r'}"
  [[ "${answer}" == "y" || "${answer}" == "Y" ]] || fail "Not confirmed; nothing was changed."
}

# ---------------------------------------------------------------------------------------------
# AWS Credential Preflight
# ---------------------------------------------------------------------------------------------
verify_aws_identity() {
  if is_dry_run; then
    log "[dry-run] would verify AWS identity for profile '${AWS_PROFILE:-default}' via sts get-caller-identity"
    return 0
  fi
  command -v aws >/dev/null 2>&1 || fail "AWS CLI ('aws') is not installed or not in PATH."
  local acct arn
  acct="$(aws sts get-caller-identity --query Account --output text 2>/dev/null | tr -d '\r')" || acct=""
  if [[ ! "${acct}" =~ ^[0-9]{12}$ ]]; then
    fail "AWS credentials for profile '${AWS_PROFILE:-default}' are not valid or expired. Run 'aws sts get-caller-identity' to diagnose."
  fi
  arn="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null | tr -d '\r' | sanitize)" || arn="unknown"
  log "AWS identity verified: profile=${AWS_PROFILE:-default} account=********${acct: -4} caller=${arn}"
}

# ---------------------------------------------------------------------------------------------
# Run summaries and step timers (.runs/)
# ---------------------------------------------------------------------------------------------
init_run_summary() {
  local workflow_name="$1"
  mkdir -p "${RUNS_DIR}"
  RUN_STARTED_UTC="$(ts)"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  RUN_SUMMARY_FILE="${RUNS_DIR}/${stamp}-${workflow_name}.md"
  STEP_TIMINGS_FILE="${RUNS_DIR}/.${stamp}-${workflow_name}.timings.tsv"
  : >"${STEP_TIMINGS_FILE}"
}

# timed_step LABEL COMMAND [ARGS...]
timed_step() {
  local label="$1" started rc=0 elapsed
  shift
  started="$(date +%s)"
  log ">>> Starting step: ${label}"
  "$@" || rc=$?
  elapsed="$(($(date +%s) - started))"
  if [[ -n "${STEP_TIMINGS_FILE}" ]]; then
    printf '%s\t%s\t%s\n' "${label}" "${elapsed}" "${rc}" >>"${STEP_TIMINGS_FILE}"
  fi
  if ((rc == 0)); then
    log "<<< Completed step: ${label} (${elapsed}s)"
  else
    warn "<<< Failed step: ${label} (${elapsed}s, exit ${rc})"
  fi
  return "${rc}"
}

# write_run_summary TITLE [KEY=VALUE ...]
write_run_summary() {
  local title="$1"
  shift
  [[ -n "${RUN_SUMMARY_FILE}" ]] || return 0
  {
    printf '# %s\n\n' "${title}"
    printf '| Field | Value |\n'
    printf '| --- | --- |\n'
    printf '| Started (UTC) | %s |\n' "${RUN_STARTED_UTC:-unknown}"
    printf '| Finished (UTC) | %s |\n' "$(ts)"
    printf '| Mode | %s |\n' "$(is_dry_run && echo DRY-RUN || echo LIVE)"
    printf '| AWS Profile | %s |\n' "${AWS_PROFILE:-default}"
    local kv
    for kv in "$@"; do
      printf '| %s | %s |\n' "${kv%%=*}" "$(sanitize <<<"${kv#*=}")"
    done
    printf '\n## Step Timings\n\n'
    printf '| Step | Seconds | Exit Code |\n'
    printf '| --- | --- | --- |\n'
    if [[ -n "${STEP_TIMINGS_FILE}" && -f "${STEP_TIMINGS_FILE}" ]]; then
      local label secs step_rc
      while IFS=$'\t' read -r label secs step_rc; do
        printf '| %s | %s | %s |\n' "${label}" "${secs}" "${step_rc}"
      done <"${STEP_TIMINGS_FILE}"
    fi
  } >"${RUN_SUMMARY_FILE}"
  rm -f "${STEP_TIMINGS_FILE}"
  log "Run summary written to ${RUN_SUMMARY_FILE#"${REPO_ROOT}"/}"
}
