import test from "node:test";
import assert from "node:assert/strict";
import {
  validateAmount, isRetryable, retrySchedule, summarizeLatencies, compareAcknowledged,
  describeMovementError, formatAmount, initialOperateState, reduceOperate, buildOperation,
  initialOperations, reduceOperations, RETRY_BUDGET_MS,
  initialAuthState, reduceAuth, buildInitiateAuthPayload, buildRefreshTokenPayload,
  parseJwtClaims, isTokenExpiring, buildAuthHeaders, purgeAuthTokens,
  selectCognitoTarget, shouldSwitchAuthRegion, FAILOVER_AUTH_TIMEOUT_MS,
} from "../logic.js";

test("validateAmount accepts whole and decimal amounts and normalizes to two decimals", () => {
  assert.deepEqual(validateAmount("12"), { ok: true, amount: "12.00" });
  assert.deepEqual(validateAmount(" 12.5 "), { ok: true, amount: "12.50" });
  assert.deepEqual(validateAmount("0.01"), { ok: true, amount: "0.01" });
  assert.deepEqual(validateAmount("1234.56"), { ok: true, amount: "1234.56" });
});

test("validateAmount rejects empty, zero, negative, over-precise and non-numeric text with a message", () => {
  for (const bad of ["", "   ", null, undefined, "0", "0.00", "-5", "-0.01", "1.234", "1e3", "abc", "1,5", "12.", ".5", "1 2"]) {
    const result = validateAmount(bad);
    assert.equal(result.ok, false, `expected rejection for ${JSON.stringify(bad)}`);
    assert.ok(result.message.length > 0);
  }
  assert.match(validateAmount("").message, /enter an amount/i);
  assert.match(validateAmount("0").message, /greater than zero/i);
  assert.match(validateAmount("-5").message, /greater than zero/i);
  assert.match(validateAmount("1.234").message, /2 decimals/i);
});

test("isRetryable: network errors and 429/500/502/503/504 retry; 400/422 and other client errors do not", () => {
  for (const status of [429, 500, 502, 503, 504]) {
    assert.equal(isRetryable(status), true, String(status));
  }
  for (const status of [400, 401, 403, 404, 422, 200, 201]) {
    assert.equal(isRetryable(status), false, String(status));
  }
  assert.equal(isRetryable(new TypeError("Failed to fetch")), true);
  assert.equal(isRetryable("network"), true);
  assert.equal(isRetryable(undefined), true);
});

test("retrySchedule grows and its waits never exceed the total budget", () => {
  const waits = retrySchedule();
  assert.ok(waits.length >= 4);
  for (let i = 1; i < waits.length; i += 1) {
    assert.ok(waits[i] >= waits[i - 1], "waits must not shrink");
  }
  assert.ok(waits[1] > waits[0], "waits must grow");
  const total = waits.reduce((sum, wait) => sum + wait, 0);
  assert.ok(total <= RETRY_BUDGET_MS);
  assert.equal(RETRY_BUDGET_MS, 60000);
  // One more wait of the same size would break the budget.
  assert.ok(total + waits[waits.length - 1] > RETRY_BUDGET_MS);
});

test("retrySchedule honours a custom budget and returns nothing when the first wait does not fit", () => {
  assert.deepEqual(retrySchedule({ budgetMs: 7000, firstWaitMs: 1000, factor: 2, maxWaitMs: 8000 }), [1000, 2000, 4000]);
  assert.deepEqual(retrySchedule({ budgetMs: 500, firstWaitMs: 1000 }), []);
});

test("summarizeLatencies: odd count, even count, single value and empty list", () => {
  assert.deepEqual(summarizeLatencies([30, 10, 20]), { count: 3, min: 10, median: 20, max: 30 });
  assert.deepEqual(summarizeLatencies([40, 10, 30, 20]), { count: 4, min: 10, median: 25, max: 40 });
  assert.deepEqual(summarizeLatencies([7]), { count: 1, min: 7, median: 7, max: 7 });
  assert.deepEqual(summarizeLatencies([]), { count: 0, min: null, median: null, max: null });
  assert.deepEqual(summarizeLatencies([5, Number.NaN, undefined, 15]), { count: 2, min: 5, median: 10, max: 15 });
});

test("summarizeLatencies does not modify its input", () => {
  const input = [3, 1, 2];
  summarizeLatencies(input);
  assert.deepEqual(input, [3, 1, 2]);
});

test("compareAcknowledged returns the acknowledged ids that are not in the ledger, in order", () => {
  assert.deepEqual(compareAcknowledged(["a", "b", "c"], ["c", "a", "z"]), ["b"]);
  assert.deepEqual(compareAcknowledged(["a", "b"], ["b", "a"]), []);
  assert.deepEqual(compareAcknowledged([], ["a"]), []);
  assert.deepEqual(compareAcknowledged(["a", "b"], []), ["a", "b"]);
  assert.deepEqual(compareAcknowledged(["a", "a", "b"], ["x"]), ["a", "b"]);
});

test("formatAmount", () => {
  assert.equal(formatAmount("12.5"), "12.50");
  assert.equal(formatAmount("abc"), "abc");
});

test("describeMovementError: insufficient funds shows the balance", () => {
  const body = { error: { code: "insufficient_funds", balance: "40.5" } };
  assert.match(describeMovementError(422, body), /insufficient funds/i);
  assert.match(describeMovementError(422, body), /40\.50/);
  assert.match(describeMovementError(422, { error: { code: "insufficient_funds" }, balance: 12 }), /12\.00/);
  assert.doesNotMatch(describeMovementError(422, { error: { code: "insufficient_funds" } }), /undefined|NaN/);
});

test("describeMovementError: validation, throttling, outage, key, server and network errors in plain words", () => {
  const fields = { error: { code: "validation_error", fields: [{ field: "amount", message: "must be positive" }, { field: "type", message: "unknown" }] } };
  assert.match(describeMovementError(400, fields), /amount: must be positive; type: unknown/);
  assert.match(describeMovementError(400, { error: { message: "bad body" } }), /bad body/);
  assert.match(describeMovementError(429, null), /too many requests/i);
  assert.match(describeMovementError(503, { error: { code: "database_read_only" } }), /read-only/i);
  assert.match(describeMovementError(503, { error: { code: "database_unavailable" } }), /unavailable/i);
  assert.match(describeMovementError(401, undefined), /API key/);
  assert.match(describeMovementError(403, undefined), /API key/);
  assert.match(describeMovementError(500, { error: { code: "internal_error" } }), /HTTP 500, internal_error/);
  assert.match(describeMovementError("network", undefined), /could not reach/i);
  assert.match(describeMovementError(undefined, undefined), /could not reach/i);
});

test("operate reducer: idle to sending to done", () => {
  let state = reduceOperate(initialOperateState, { type: "send-started", key: "k1" });
  assert.deepEqual([state.phase, state.attempt, state.key], ["sending", 1, "k1"]);
  state = reduceOperate(state, { type: "send-ok", result: { id: "t1" } });
  assert.equal(state.phase, "done");
  assert.deepEqual(state.result, { id: "t1" });
  assert.equal(state.key, "k1");
});

test("operate reducer: retries show recovering with the attempt number, success after recovery is kept", () => {
  let state = reduceOperate(initialOperateState, { type: "send-started", key: "k1" });
  state = reduceOperate(state, { type: "attempt-failed", attempt: 2 });
  assert.deepEqual([state.phase, state.attempt], ["recovering", 2]);
  assert.match(state.message, /recovering, attempt 2/i);
  state = reduceOperate(state, { type: "attempt-failed", attempt: 3 });
  assert.equal(state.attempt, 3);
  state = reduceOperate(state, { type: "send-ok", result: { id: "t1" } });
  assert.deepEqual([state.phase, state.attempt], ["done", 3]);
});

test("operate reducer: failure keeps the key and message; a new send starts clean; send while busy is ignored", () => {
  let state = reduceOperate(initialOperateState, { type: "send-started", key: "k1" });
  assert.equal(reduceOperate(state, { type: "send-started", key: "k2" }), state);
  state = reduceOperate(state, { type: "attempt-failed", attempt: 2 });
  assert.equal(reduceOperate(state, { type: "send-started", key: "k2" }), state);
  state = reduceOperate(state, { type: "send-failed", message: "gave up" });
  assert.deepEqual([state.phase, state.message, state.key, state.result], ["failed", "gave up", "k1", null]);
  state = reduceOperate(state, { type: "send-started", key: "k2" });
  assert.deepEqual([state.phase, state.attempt, state.key, state.message, state.result], ["sending", 1, "k2", "", null]);
  assert.equal(reduceOperate(state, { type: "unknown" }), state);
});

const created = {
  transaction: { id: "t1", type: "deposit", amount: "10.00", balance_after: "110.00", processed_at: "2026-01-01T00:00:00.000Z", processed_region: "us-west-2" },
  balance: "110.00",
  served_by_region: "us-west-2",
  database_role: "writer",
  server_ms: 42,
};

test("buildOperation maps the 201 body and the client measurements; attempts above one mean recovered", () => {
  assert.deepEqual(buildOperation(created, { roundTripMs: 350.4, attempts: 1 }), {
    id: "t1", type: "deposit", amount: "10.00", balanceAfter: "110.00", region: "us-west-2",
    backend: "us-west-2", roundTripMs: 350.4, serverMs: 42, attempts: 1, recovered: false,
  });
  const recovered = buildOperation(created, { roundTripMs: 9000, attempts: 3 });
  assert.equal(recovered.recovered, true);
  assert.equal(recovered.attempts, 3);
});

test("operations reducer: newest first, same id is not listed twice, restore replaces the list", () => {
  const a = buildOperation(created, { roundTripMs: 100, attempts: 1 });
  const b = { ...a, id: "t2" };
  let list = reduceOperations(initialOperations, { type: "operation-recorded", operation: a });
  list = reduceOperations(list, { type: "operation-recorded", operation: b });
  assert.deepEqual(list.map((op) => op.id), ["t2", "t1"]);
  const again = reduceOperations(list, { type: "operation-recorded", operation: { ...a, roundTripMs: 5 } });
  assert.deepEqual(again.map((op) => op.id), ["t2", "t1"]);
  assert.equal(again.find((op) => op.id === "t1").roundTripMs, 100);
  assert.deepEqual(reduceOperations(list, { type: "operations-restored", operations: [a] }), [a]);
  assert.deepEqual(reduceOperations(list, { type: "operations-restored", operations: "garbage" }), []);
  assert.equal(reduceOperations(list, { type: "other" }), list);
});

function makeFakeJwt(payloadObj) {
  const header = Buffer.from(JSON.stringify({ alg: "RS256", typ: "JWT" })).toString("base64url");
  const payload = Buffer.from(JSON.stringify(payloadObj)).toString("base64url");
  return `${header}.${payload}.signature`;
}

test("buildInitiateAuthPayload and buildRefreshTokenPayload format Cognito JSON payloads", () => {
  assert.deepEqual(buildInitiateAuthPayload(" bank-operator ", "Secret#123456", "client-bank-1"), {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: "client-bank-1",
    AuthParameters: {
      USERNAME: "bank-operator",
      PASSWORD: "Secret#123456",
    },
  });

  assert.deepEqual(buildRefreshTokenPayload("refresh-tok-1", "client-bank-1"), {
    AuthFlow: "REFRESH_TOKEN_AUTH",
    ClientId: "client-bank-1",
    AuthParameters: {
      REFRESH_TOKEN: "refresh-tok-1",
    },
  });
});

test("parseJwtClaims extracts sub, cognito:username, email and exp and rejects malformed tokens", () => {
  const token = makeFakeJwt({
    sub: "sub-123",
    "cognito:username": "bank-operator",
    email: "bank-operator@example.internal",
    exp: 1800000000,
    token_use: "id",
  });
  const claims = parseJwtClaims(token);
  assert.equal(claims.sub, "sub-123");
  assert.equal(claims.username, "bank-operator");
  assert.equal(claims["cognito:username"], "bank-operator");
  assert.equal(claims.email, "bank-operator@example.internal");
  assert.equal(claims.exp, 1800000000);

  for (const invalid of ["", null, undefined, "not-a-jwt", "a.b", "a.%%%bad%%%.c"]) {
    assert.equal(parseJwtClaims(invalid), null);
  }
});

test("isTokenExpiring detects tokens within margin or already expired", () => {
  const nowMs = 1_700_000_000_000;
  const freshToken = makeFakeJwt({ sub: "u1", exp: Math.floor(nowMs / 1000) + 300 });
  const expiringSoonToken = makeFakeJwt({ sub: "u1", exp: Math.floor(nowMs / 1000) + 30 });
  const expiredToken = makeFakeJwt({ sub: "u1", exp: Math.floor(nowMs / 1000) - 10 });

  assert.equal(isTokenExpiring(freshToken, 60, nowMs), false);
  assert.equal(isTokenExpiring(expiringSoonToken, 60, nowMs), true);
  assert.equal(isTokenExpiring(expiredToken, 60, nowMs), true);
  assert.equal(isTokenExpiring(null, 60, nowMs), true);
});

test("buildAuthHeaders formats Bearer Authorization header and merges extra headers", () => {
  assert.deepEqual(buildAuthHeaders("id-tok-123", { "Idempotency-Key": "k1" }), {
    Authorization: "Bearer id-tok-123",
    "Idempotency-Key": "k1",
  });
  assert.deepEqual(buildAuthHeaders("", { "Content-Type": "application/json" }), {
    "Content-Type": "application/json",
  });
});

test("reduceAuth transitions through login, refresh, failure and sign-out, and purgeAuthTokens clears storage", () => {
  const token1 = makeFakeJwt({
    sub: "sub-1",
    "cognito:username": "bank-operator",
    email: "bank-operator@example.internal",
    exp: 1800000900,
  });
  const token2 = makeFakeJwt({
    sub: "sub-1",
    "cognito:username": "bank-operator",
    email: "bank-operator@example.internal",
    exp: 1800001800,
  });

  let state = reduceAuth(initialAuthState, { type: "auth-started" });
  assert.equal(state.status, "authenticating");

  state = reduceAuth(state, {
    type: "auth-succeeded",
    idToken: token1,
    accessToken: "acc-1",
    refreshToken: "ref-1",
  });
  assert.equal(state.status, "authenticated");
  assert.equal(state.username, "bank-operator");
  assert.equal(state.sub, "sub-1");
  assert.equal(state.idToken, token1);
  assert.equal(state.refreshToken, "ref-1");

  state = reduceAuth(state, {
    type: "token-refreshed",
    idToken: token2,
    accessToken: "acc-2",
  });
  assert.equal(state.idToken, token2);
  assert.equal(state.accessToken, "acc-2");
  assert.equal(state.refreshToken, "ref-1");

  const failed = reduceAuth(state, { type: "auth-failed", message: "Invalid username or password" });
  assert.equal(failed.status, "unauthenticated");
  assert.equal(failed.idToken, null);
  assert.equal(failed.error, "Invalid username or password");

  const signedOut = reduceAuth(state, { type: "signed-out" });
  assert.equal(signedOut.status, "unauthenticated");
  assert.equal(signedOut.idToken, null);

  const removed = [];
  const fakeStorage = { removeItem: (k) => removed.push(k) };
  const purged = purgeAuthTokens(fakeStorage, ["k1", "k2"]);
  assert.deepEqual(removed, ["k1", "k2"]);
  assert.equal(purged.status, "unauthenticated");
});

test("selectCognitoTarget resolves active and alternate regional Cognito targets for failover and switchback", () => {
  const cfg = {
    apiBaseUrl: "https://api.example.invalid",
    cognito: {
      primary: {
        region: "us-east-2",
        userPoolId: "us-east-2_Pool1",
        clientId: "client-bank-1",
        endpoint: "https://cognito-idp.us-east-2.amazonaws.com",
        apiBaseUrl: "https://api.example.invalid",
      },
      standby: {
        region: "us-west-2",
        userPoolId: "us-east-2_Pool1",
        clientId: "client-bank-1",
        endpoint: "https://cognito-idp.us-west-2.amazonaws.com",
        apiBaseUrl: "https://api.example.invalid",
      },
    },
  };

  const fromPrimary = selectCognitoTarget(cfg, "primary");
  assert.equal(fromPrimary.active.regionKey, "primary");
  assert.equal(fromPrimary.active.region, "us-east-2");
  assert.equal(fromPrimary.active.endpoint, "https://cognito-idp.us-east-2.amazonaws.com");
  assert.equal(fromPrimary.alternate.regionKey, "standby");
  assert.equal(fromPrimary.alternate.region, "us-west-2");
  assert.equal(fromPrimary.alternate.endpoint, "https://cognito-idp.us-west-2.amazonaws.com");

  const fromStandby = selectCognitoTarget(cfg, "us-west-2");
  assert.equal(fromStandby.active.regionKey, "standby");
  assert.equal(fromStandby.active.region, "us-west-2");
  assert.equal(fromStandby.alternate.regionKey, "primary");
  assert.equal(fromStandby.alternate.region, "us-east-2");
  assert.ok(FAILOVER_AUTH_TIMEOUT_MS < 5000);
});

test("shouldSwitchAuthRegion detects network outages, 5xx gateway errors, 401 regional rejection, and region drift", () => {
  assert.equal(shouldSwitchAuthRegion({ status: null, isNetworkError: true, currentAuthRegion: "primary" }), true);
  assert.equal(shouldSwitchAuthRegion({ status: 503, isNetworkError: false, currentAuthRegion: "primary" }), true);
  assert.equal(shouldSwitchAuthRegion({ status: 401, isNetworkError: false, currentAuthRegion: "primary" }), true);
  assert.equal(shouldSwitchAuthRegion({ status: 200, servedByRegion: "us-west-2", currentAuthRegion: "primary" }), true);
  assert.equal(shouldSwitchAuthRegion({ status: 200, servedByRegion: "us-east-2", currentAuthRegion: "standby" }), true);
  assert.equal(shouldSwitchAuthRegion({ status: 200, servedByRegion: "us-east-2", currentAuthRegion: "primary" }), false);
  assert.equal(shouldSwitchAuthRegion({ status: 422, isNetworkError: false, currentAuthRegion: "primary" }), false);
});

test("reduceAuth region-switched updates activeRegion and tokens without interrupting authenticated status", () => {
  const primaryJwt = makeFakeJwt({ sub: "sub-1", "cognito:username": "bank-operator", exp: 1800000900 });
  const standbyJwt = makeFakeJwt({ sub: "sub-1", "cognito:username": "bank-operator", exp: 1800001800 });

  let state = reduceAuth(initialAuthState, {
    type: "auth-succeeded",
    idToken: primaryJwt,
    accessToken: "acc-east",
    refreshToken: "ref-east",
    activeRegion: "primary",
  });
  assert.equal(state.activeRegion, "primary");

  state = reduceAuth(state, {
    type: "region-switched",
    activeRegion: "standby",
    idToken: standbyJwt,
    accessToken: "acc-west",
    refreshToken: "ref-west",
  });
  assert.equal(state.status, "authenticated");
  assert.equal(state.activeRegion, "standby");
  assert.equal(state.idToken, standbyJwt);
  assert.equal(state.accessToken, "acc-west");
  assert.equal(state.refreshToken, "ref-west");
  assert.equal(state.username, "bank-operator");
});
