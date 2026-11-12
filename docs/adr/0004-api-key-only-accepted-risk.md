# ADR 0004: API key and usage plan only, no authorizer (accepted risk)

- Status: **superseded by [ADR 0017](0017-cognito-multi-region-replication.md)** (spec 002, deployed 2026-11-10). The API key, the usage plan,
  `scripts/init-api-key.sh` and the SSM parameter `/drdemo/prod/api-key` no longer exist in the code; the text below is kept as history.
  Originally: accepted (known and accepted risk of a non-production demo)
- Date: 2026-10-04

## Context

The API and the dashboard need some access control. A proper user authentication scheme (Cognito, a JWT or Lambda
authorizer, IAM authorization) adds components, cost and setup that are not the point of a DR demo. The data is synthetic.

## Decision

Every API method requires an API key (`x-api-key`) tied to a usage plan (20 requests per second, burst 40, 150,000 requests
per day per region). There is **no authorizer** and no WAF. The same key value is used in both regions. It is **not a GitHub
secret**: it is the SSM Parameter Store SecureString `/drdemo/prod/api-key` in the global region, created once by the owner
with `scripts/init-api-key.sh` (a generated value that is never printed; the script refuses to overwrite). The `api` step of
`scripts/deploy.sh` reads it with the deploy credentials, masks it and passes it to both API stacks as a `NoEcho` parameter
(the `ssm-secure` dynamic reference is not supported for the API key value, so the stack cannot read the parameter itself);
`40-validate.sh`, `45-consistency.sh` and the publish leak check read the same parameter when `API_KEY_VALUE` is not set.
The viewer types the key into the banking app or the dashboard, which keep it only in `sessionStorage` of that tab.

One exception to "every method": the `OPTIONS` methods that API Gateway creates for CORS do not require the key, because a
browser preflight request cannot carry it. They return only the static CORS headers and invoke nothing.

## Consequences (the accepted risk)

- Anyone holding the key can call every route. The key is typed in a browser, so cross-site scripting or shoulder
  surfing could expose it; it is also readable by principals with `apigateway:GET` and is stored in the Postman environment.
- Bounded by: throttling and daily quota, synthetic data only (no personal data may be entered), a strict Content
  Security Policy without inline code or third-party scripts, a private dashboard bucket, the default execute-api
  endpoint disabled, and a demo lifetime of hours.
- API Gateway rejects a missing or invalid key with `403` before any function runs.
- Rotation is manual: replace the SSM parameter (delete it, run `scripts/init-api-key.sh` again) and run the deploy workflow
  again (the `api` step); expect a short window of `403` responses while the two regions update one after the other (not measured).
- The value also exists in the two `NoEcho` stack parameters and in the API Gateway key; reading it to type it in a browser needs
  `aws ssm get-parameter --with-decryption` with the reader's own credentials.
- The API still needs a separate decision before any real use: an authorizer (small cost, moderate effort) and a WAF
  web ACL with a rate-based rule (roughly USD 6 to 10 per month each, unverified estimate).

## Alternatives considered

Lambda or IAM authorizer, Cognito, mutual TLS, an IP allow list through a resource policy, a WAF: all rejected for cost
and complexity in a demo.
