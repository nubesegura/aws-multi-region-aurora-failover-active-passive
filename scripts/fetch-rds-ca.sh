#!/usr/bin/env bash
# Download the Amazon RDS global CA bundle into src/backend/certs/ so that `sam build` packages it
# and the Lambda functions can verify the Aurora server certificate (requirement 4.4).
# Undo: delete src/backend/certs/ (it is git-ignored). Needs no AWS credentials.
set -euo pipefail

readonly BUNDLE_URL="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_dir="${repo_root}/src/backend/certs"
target="${target_dir}/global-bundle.pem"

mkdir -p "${target_dir}"
tmp="$(mktemp "${target_dir}/global-bundle.XXXXXX")"
trap 'rm -f "${tmp}"' EXIT

curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  --output "${tmp}" "${BUNDLE_URL}"

if ! grep -q -- "-----BEGIN CERTIFICATE-----" "${tmp}"; then
  echo "error: downloaded file is not a PEM certificate bundle" >&2
  exit 1
fi

mv "${tmp}" "${target}"
chmod 644 "${target}"
trap - EXIT

certificates="$(grep -c -- "-----BEGIN CERTIFICATE-----" "${target}")"
echo "RDS CA bundle written: src/backend/certs/global-bundle.pem (${certificates} certificates)"
echo "sha256: $(sha256sum "${target}" | cut -d' ' -f1)"
