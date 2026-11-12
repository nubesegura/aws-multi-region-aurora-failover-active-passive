#!/usr/bin/env bash
# Publishing of a static site to its edge bucket (requirements 3.6, 4.5, 4.7). Source it, do not execute it.
#
# One function serves both sites (the monitoring dashboard and the banking app):
#   publish_site <source dir> <bucket output name> <distribution output name>
# It needs lib/common.sh sourced first and AWS_REGION_PRIMARY and API_DOMAIN_NAME set (and validated by the caller).
# The stack outputs are read from the edge stack of the primary region. The domain output name is the distribution
# output with "Id" replaced by "DomainName" (DistributionId -> DistributionDomainName, BankDistributionId ->
# BankDistributionDomainName).
# shellcheck shell=bash

if [[ -n "${DRDEMO_PUBLISH_LOADED:-}" ]]; then
  return 0
fi
DRDEMO_PUBLISH_LOADED=1

PUBLISH_STAGES=()
_publish_cleanup() {
  local dir
  for dir in "${PUBLISH_STAGES[@]}"; do
    rm -rf "${dir}"
  done
}
trap _publish_cleanup EXIT

publish_content_type() {
  case "$1" in
    *.html) printf 'text/html; charset=utf-8' ;;
    *.js | *.mjs) printf 'text/javascript; charset=utf-8' ;;
    *.css) printf 'text/css; charset=utf-8' ;;
    *.json) printf 'application/json' ;;
    *.svg) printf 'image/svg+xml' ;;
    *.txt) printf 'text/plain; charset=utf-8' ;;
    *) printf 'application/octet-stream' ;;
  esac
}

# publish_stage SOURCE_DIR STAGE_DIR ; copy the top-level files that belong to the site (tests, package files, README and
# local mock helpers excluded), add a minimal error.html when missing and write config.json from API_DOMAIN_NAME.
publish_stage() {
  local source_dir="$1" stage="$2" file
  [[ -d "${source_dir}" ]] || die "source directory not found: ${source_dir}"
  for file in "${source_dir}"/*; do
    [[ -f "${file}" ]] || continue
    case "$(basename "${file}")" in
      *.test.js | *.test.mjs | *-mock.mjs | package*.json | README*) continue ;;
    esac
    cp "${file}" "${stage}/"
  done
  if [[ ! -f "${stage}/error.html" ]]; then
    log "${source_dir#"${REPO_ROOT}"/}/error.html is missing: generating a minimal error.html"
    cat >"${stage}/error.html" <<'HTML'
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Page not available</title>
  <link rel="stylesheet" href="/styles.css">
</head>
<body>
  <main>
    <h1>Page not available</h1>
    <p><a href="/">Back to the start page</a></p>
  </main>
</body>
</html>
HTML
  fi
  jq -n --arg url "https://${API_DOMAIN_NAME}" '{apiBaseUrl: $url}' >"${stage}/config.json"
  [[ -f "${stage}/index.html" ]] || die "${source_dir#"${REPO_ROOT}"/}/index.html not found"
}

publish_site() { # source_dir bucket_output distribution_output
  local source_dir="$1" bucket_output="$2" distribution_output="$3"
  local site edge_stack bucket distribution domain stage file name key remote
  local -a staged_keys=()
  local -A keep=()
  site="$(basename "${source_dir}")"
  edge_stack="$(stack_name "${AWS_REGION_PRIMARY}" edge)"
  bucket="$(stack_output "${AWS_REGION_PRIMARY}" "${edge_stack}" "${bucket_output}")"
  distribution="$(stack_output "${AWS_REGION_PRIMARY}" "${edge_stack}" "${distribution_output}")"
  domain="$(stack_output "${AWS_REGION_PRIMARY}" "${edge_stack}" "${distribution_output%Id}DomainName")"
  # A wrong or empty output must never reach the deletion of stale objects (it could empty another site).
  [[ -n "${bucket}" && "${bucket}" != "None" ]] || die "stack output ${bucket_output} is empty: nothing was published"
  [[ -n "${distribution}" && "${distribution}" != "None" ]] || die "stack output ${distribution_output} is empty: nothing was published"
  [[ -n "${domain}" && "${domain}" != "None" ]] || die "the distribution domain output of ${distribution_output} is empty: nothing was published"

  stage="$(mktemp -d)"
  PUBLISH_STAGES+=("${stage}")

  log "staging the ${site} files"
  publish_stage "${source_dir}" "${stage}"

  for file in "${stage}"/*; do
    name="$(basename "${file}")"
    staged_keys+=("${name}")
    keep["${name}"]=1
    log "upload ${name} ($(publish_content_type "${name}"), no-cache)"
    if is_dry_run; then
      log "[dry-run] aws s3 cp <stage>/${name} s3://${bucket}/${name} --content-type '$(publish_content_type "${name}")' --cache-control no-cache"
    else
      aws s3 cp "${file}" "s3://${bucket}/${name}" --region "${AWS_REGION_PRIMARY}" \
        --content-type "$(publish_content_type "${name}")" --cache-control "no-cache" --only-show-errors
    fi
  done

  log "removing objects that are no longer part of the ${site} site"
  if is_dry_run; then
    log "[dry-run] would list s3://${bucket} and delete keys that are not in: ${staged_keys[*]}"
  else
    remote="$(aws s3api list-objects-v2 --region "${AWS_REGION_PRIMARY}" --bucket "${bucket}" --output json)" ||
      die "cannot list s3://${bucket}: stale objects were not removed"
    if [[ -n "${remote}" ]]; then
      while IFS= read -r key; do
        [[ -n "${key}" ]] || continue
        if [[ -z "${keep[${key}]:-}" ]]; then
          log "delete stale object ${key}"
          aws s3 rm "s3://${bucket}/${key}" --region "${AWS_REGION_PRIMARY}" --only-show-errors
        fi
      done < <(jq -r '(.Contents // [])[] | .Key' <<<"${remote}")
    fi
  fi

  log "invalidate the CloudFront cache"
  aws_write cloudfront create-invalidation --distribution-id "${distribution}" --paths '/*' --query 'Invalidation.[Id,Status]' --output text
  log "${site} published: https://${domain}/"
}
