import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as logic from "../logic.js";
import {
  initialState, reduce, detectRegionChange, detectRoleChange, sharePercent, regionRows, formatAmount,
  formatUtc, formatLocal, statusAnnouncement, historyRows, summarizeLatencies, consistencyView,
  secondsSince, unavailableView, HISTORY_LIMIT, TIMELINE_LIMIT, LATENCY_SAMPLES,
  initialAuthState, reduceAuth, buildInitiateAuthPayload, buildRefreshTokenPayload,
  parseJwtClaims, isTokenExpiring, buildAuthHeaders, purgeAuthTokens,
  canPoll, evaluateUnauthorized, TOKEN_EXPIRY_MARGIN_SECONDS,
  selectCognitoTarget, shouldSwitchAuthRegion, FAILOVER_AUTH_TIMEOUT_MS,
} from "../logic.js";

const stats = (region, role = "writer") => ({
  total_count: 4,
  counts_by_region: { "us-east-2": 3, "us-west-2": 1 },
  last_processed_at_by_region: { "us-east-2": "2026-01-01T00:00:03Z", "us-west-2": "2026-01-01T00:00:04Z" },
  latest_transactions: [],
  served_by_region: region,
  database_role: role,
  generated_at: "2026-01-01T00:00:05Z",
});

test("detectRegionChange ignores first sample and same region", () => {
  assert.equal(detectRegionChange(undefined, "us-east-2"), null);
  assert.equal(detectRegionChange("us-east-2", "us-east-2"), null);
  assert.deepEqual(detectRegionChange("us-east-2", "us-west-2"), { from: "us-east-2", to: "us-west-2" });
});

test("sharePercent handles zero and rounds to one decimal", () => {
  assert.equal(sharePercent(0, 0), 0);
  assert.equal(sharePercent(1, 3), 33.3);
  assert.equal(sharePercent(3, 3), 100);
});

test("regionRows lists every region sorted with share", () => {
  const rows = regionRows(stats("us-east-2"));
  assert.deepEqual(rows.map((r) => [r.region, r.count, r.share]), [["us-east-2", 3, 75], ["us-west-2", 1, 25]]);
});

test("regionRows always shows both demo regions with zero defaults", () => {
  const rows = regionRows({ total_count: 0, counts_by_region: {}, last_processed_at_by_region: {} });
  assert.deepEqual(rows.map((r) => [r.region, r.count, r.share, r.lastProcessedAt]), [["us-east-2", 0, 0, null], ["us-west-2", 0, 0, null]]);
  const partial = regionRows({ total_count: 2, counts_by_region: { "us-east-2": 2 }, last_processed_at_by_region: {} });
  assert.deepEqual(partial.map((r) => [r.region, r.count, r.share]), [["us-east-2", 2, 100], ["us-west-2", 0, 0]]);
});

test("formatAmount", () => {
  assert.equal(formatAmount("12.5"), "12.50");
  assert.equal(formatAmount(3), "3.00");
  assert.equal(formatAmount("abc"), "abc");
});

test("timestamps", () => {
  assert.equal(formatUtc("2026-01-01T10:20:30.123Z"), "2026-01-01 10:20:30 UTC");
  assert.equal(formatUtc("nope"), "unknown");
  assert.match(formatLocal("2026-01-01T10:20:30Z", "UTC"), /1 Jan 2026/);
  assert.equal(formatLocal(null, "UTC"), "unknown");
});

test("reducer: ok, then region switch is flagged and remembered", () => {
  let state = reduce(initialState, { type: "stats-ok", stats: stats("us-east-2"), at: "t1" });
  assert.equal(state.phase, "ok");
  assert.equal(state.freshChange, false);
  state = reduce(state, { type: "stats-ok", stats: stats("us-west-2"), at: "t2" });
  assert.equal(state.freshChange, true);
  assert.deepEqual(state.lastChange, { from: "us-east-2", to: "us-west-2", at: "t2" });
  state = reduce(state, { type: "stats-ok", stats: stats("us-west-2"), at: "t3" });
  assert.equal(state.freshChange, false);
  assert.equal(state.lastChange.at, "t2");
});

test("reducer: failure keeps last stats and success time, recovery clears it", () => {
  let state = reduce(initialState, { type: "stats-ok", stats: stats("us-east-2"), at: "t1" });
  state = reduce(state, { type: "stats-failed", reason: "timeout", at: "t2" });
  assert.equal(state.phase, "unavailable");
  assert.equal(state.lastSuccessAt, "t1");
  assert.equal(state.stats.served_by_region, "us-east-2");
  state = reduce(state, { type: "stats-ok", stats: stats("us-east-2"), at: "t3" });
  assert.equal(state.phase, "ok");
  assert.equal(state.failureReason, null);
});

test("reducer: a region change across an outage is still detected", () => {
  let state = reduce(initialState, { type: "stats-ok", stats: stats("us-east-2"), at: "t1" });
  state = reduce(state, { type: "stats-failed", reason: "x" });
  state = reduce(state, { type: "stats-ok", stats: stats("us-west-2"), at: "t3" });
  assert.equal(state.freshChange, true);
});

const movement = (n) => ({
  id: `t${n}`, type: n % 2 ? "deposit" : "withdrawal", amount: "5", balance_after: "100.5",
  processed_at: "2026-01-01T10:20:30.123Z", processed_region: "us-west-2",
});

test("historyRows keeps the API order, caps at 50 and formats each row", () => {
  const rows = historyRows(Array.from({ length: 60 }, (_, i) => movement(i + 1)));
  assert.equal(HISTORY_LIMIT, 50);
  assert.equal(rows.length, 50);
  assert.deepEqual(rows[0], {
    id: "t1", time: "2026-01-01 10:20:30 UTC", type: "deposit", amount: "5.00",
    balanceAfter: "100.50", region: "us-west-2", status: "applied",
  });
  assert.equal(rows[49].id, "t50");
  assert.equal(historyRows(Array.from({ length: 5 }, (_, i) => movement(i)), 3).length, 3);
  assert.deepEqual(historyRows(null), []);
});

test("summarizeLatencies: odd, even, empty and non-numeric samples", () => {
  assert.deepEqual(summarizeLatencies([30, 10, 20]), { count: 3, min: 10, median: 20, max: 30 });
  assert.deepEqual(summarizeLatencies([40, 10, 30, 20]), { count: 4, min: 10, median: 25, max: 40 });
  assert.deepEqual(summarizeLatencies([]), { count: 0, min: null, median: null, max: null });
  assert.deepEqual(summarizeLatencies([5, Number.NaN, undefined, 15]), { count: 2, min: 5, median: 10, max: 15 });
});

test("reducer: poll round trips are collected (bounded) and ignored when missing", () => {
  let state = reduce(initialState, { type: "stats-ok", stats: stats("us-east-2"), at: "t1", roundTripMs: 120 });
  state = reduce(state, { type: "stats-ok", stats: stats("us-east-2"), at: "t2" });
  assert.deepEqual(state.latencies, [120]);
  for (let i = 0; i < LATENCY_SAMPLES + 5; i += 1) {
    state = reduce(state, { type: "stats-ok", stats: stats("us-east-2"), at: `t${i}`, roundTripMs: i });
  }
  assert.equal(state.latencies.length, LATENCY_SAMPLES);
  assert.equal(state.latencies.at(-1), LATENCY_SAMPLES + 4);
});

test("timeline records region and role changes newest first, nothing on the first sample or unchanged polls", () => {
  assert.deepEqual(detectRoleChange("writer", "reader"), { from: "writer", to: "reader" });
  let state = reduce(initialState, { type: "stats-ok", stats: stats("us-east-2", "writer"), at: "t1" });
  assert.deepEqual(state.timeline, []);
  state = reduce(state, { type: "stats-ok", stats: stats("us-east-2", "writer"), at: "t2" });
  assert.deepEqual(state.timeline, []);
  state = reduce(state, { type: "stats-ok", stats: stats("us-west-2", "writer"), at: "t3" });
  state = reduce(state, { type: "stats-ok", stats: stats("us-west-2", "reader"), at: "t4" });
  assert.deepEqual(state.timeline, [
    { at: "t4", kind: "role", from: "writer", to: "reader" },
    { at: "t3", kind: "region", from: "us-east-2", to: "us-west-2" },
  ]);
  assert.equal(state.freshChange, true);
  assert.deepEqual(state.lastRoleChange, { from: "writer", to: "reader", at: "t4" });
  const both = reduce(state, { type: "stats-ok", stats: stats("us-east-2", "writer"), at: "t5" });
  assert.deepEqual(both.timeline.slice(0, 2).map((e) => e.kind).sort(), ["region", "role"]);
  assert.match(statusAnnouncement(both), /region changed from us-west-2 to us-east-2.*role changed from reader to writer/);
});

test("timeline keeps only the newest events", () => {
  let state = reduce(initialState, { type: "stats-ok", stats: stats("us-east-2"), at: "t0" });
  for (let i = 1; i <= TIMELINE_LIMIT + 5; i += 1) {
    state = reduce(state, { type: "stats-ok", stats: stats(i % 2 ? "us-west-2" : "us-east-2"), at: `t${i}` });
  }
  assert.equal(state.timeline.length, TIMELINE_LIMIT);
  assert.equal(state.timeline[0].at, `t${TIMELINE_LIMIT + 5}`);
});

test("consistencyView for a consistent report lists the counts and the serving region", () => {
  const view = consistencyView({
    consistent: true, truncated: false, count: 3, total_deposited: "30", total_withdrawn: "10",
    replayed_balance: "20", stored_balance: "20", balances_match: true, chain_ok: true, duplicate_keys: 0,
    counts_by_region: { "us-west-2": 1, "us-east-2": 2 }, served_by_region: "us-west-2",
  });
  assert.equal(view.state, "consistent");
  assert.equal(view.servedBy, "us-west-2");
  assert.equal(view.firstOffender, null);
  assert.deepEqual(view.rows.slice(0, 3), [["Movements", "3"], ["Total deposited", "30.00"], ["Total withdrawn", "10.00"]]);
  assert.deepEqual(view.rows.slice(-2), [["Movements in us-east-2", "2"], ["Movements in us-west-2", "1"]]);
  assert.deepEqual(view.rows.find(([name]) => name === "Balances match"), ["Balances match", "yes"]);
});

test("consistencyView for an inconsistent report exposes the first offender and the failed checks", () => {
  const view = consistencyView({
    consistent: false, truncated: false, count: 2, replayed_balance: "20", stored_balance: "25",
    balances_match: false, chain_ok: false, duplicate_keys: 1, first_offender: "t2", counts_by_region: {},
  });
  assert.equal(view.state, "inconsistent");
  assert.equal(view.firstOffender, "t2");
  assert.deepEqual(view.rows.find(([name]) => name === "Balances match"), ["Balances match", "no"]);
  assert.deepEqual(view.rows.find(([name]) => name === "Duplicate keys"), ["Duplicate keys", "1"]);
});

test("consistencyView for a truncated report does not claim consistency; bad input is unknown", () => {
  const truncated = consistencyView({ consistent: null, truncated: true, count: 50001 });
  assert.equal(truncated.state, "truncated");
  assert.match(truncated.label, /Not checked/);
  assert.equal(truncated.firstOffender, null);
  assert.equal(consistencyView({ consistent: null, truncated: false }).state, "unknown");
  assert.equal(consistencyView(null).state, "unknown");
});

test("reducer: account, history and the on-demand consistency check", () => {
  let state = reduce(initialState, { type: "account-ok", account: { balance: "10.00" } });
  state = reduce(state, { type: "history-ok", transactions: [movement(1)] });
  assert.equal(state.account.balance, "10.00");
  assert.equal(state.history.length, 1);
  assert.equal(state.consistency.phase, "idle");
  state = reduce(state, { type: "consistency-started" });
  assert.equal(state.consistency.phase, "checking");
  state = reduce(state, { type: "consistency-ok", report: { consistent: true }, at: "t9" });
  assert.deepEqual(state.consistency, { phase: "done", report: { consistent: true }, at: "t9", reason: null });
  state = reduce(state, { type: "consistency-failed", reason: "HTTP 500", at: "t10" });
  assert.deepEqual(state.consistency, { phase: "failed", report: null, at: "t10", reason: "HTTP 500" });
  assert.equal(reduce(state, { type: "other" }), state);
});

test("secondsSince and unavailableView: elapsed time since the last good answer, last data kept", () => {
  assert.equal(secondsSince("2026-01-01T00:00:00Z", "2026-01-01T00:00:42Z"), 42);
  assert.equal(secondsSince("2026-01-01T00:00:10Z", "2026-01-01T00:00:00Z"), 0);
  assert.equal(secondsSince(null, "2026-01-01T00:00:00Z"), null);
  assert.equal(secondsSince("nope", "2026-01-01T00:00:00Z"), null);

  let state = reduce(initialState, { type: "stats-ok", stats: stats("us-east-2"), at: "2026-01-01T00:00:00Z" });
  assert.deepEqual(unavailableView(state, "2026-01-01T00:05:00Z"), { unavailable: false, seconds: null, text: "", keepsData: true });
  state = reduce(state, { type: "stats-failed", reason: "timeout" });
  const view = unavailableView(state, "2026-01-01T00:02:05Z");
  assert.deepEqual([view.unavailable, view.seconds, view.keepsData], [true, 125, true]);
  assert.match(view.text, /2 min 5 s ago/);
  assert.match(unavailableView(state, "2026-01-01T00:00:09Z").text, /9 s ago/);
  const never = unavailableView(reduce(initialState, { type: "stats-failed", reason: "x" }), "2026-01-01T00:00:09Z");
  assert.deepEqual([never.unavailable, never.keepsData], [true, false]);
  assert.match(never.text, /No successful update yet/);
});

test("the dashboard is read-only: no send state, no transaction-building function, no POST in the logic", () => {
  assert.equal("send" in initialState, false);
  for (const name of Object.keys(logic)) {
    assert.doesNotMatch(name, /send|post|transaction/i, name);
  }
  assert.equal(reduce(initialState, { type: "send-started" }), initialState);
  const source = readFileSync(new URL("../logic.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /POST|method\s*:|fetch\(|XMLHttpRequest/);
});

test("statusAnnouncement", () => {
  assert.equal(statusAnnouncement(initialState), "Loading.");
  let state = reduce(initialState, { type: "stats-ok", stats: stats("us-east-2"), at: "2026-01-01T00:00:00Z" });
  assert.equal(statusAnnouncement(state), "Active region us-east-2, database writer.");
  const failed = reduce(state, { type: "stats-failed", reason: "x" });
  assert.match(statusAnnouncement(failed), /unavailable.*2026-01-01 00:00:00 UTC/);
  assert.match(statusAnnouncement(reduce(initialState, { type: "stats-failed", reason: "x" })), /never/);
  state = reduce(state, { type: "stats-ok", stats: stats("us-west-2", "reader"), at: "t2" });
  assert.match(statusAnnouncement(state), /changed from us-east-2 to us-west-2/);
});

function makeFakeJwt(payloadObj) {
  const header = Buffer.from(JSON.stringify({ alg: "RS256", typ: "JWT" })).toString("base64url");
  const payload = Buffer.from(JSON.stringify(payloadObj)).toString("base64url");
  return `${header}.${payload}.signature`;
}

test("reduceAuth transitions through auth-started, auth-succeeded, token-refreshed, auth-expired, auth-failed, signed-out", () => {
  assert.equal(initialAuthState.phase, "unauthenticated");
  assert.equal(initialAuthState.idToken, null);

  let s = reduceAuth(initialAuthState, { type: "auth-started" });
  assert.equal(s.phase, "signing-in");
  assert.equal(s.error, null);

  const idToken = makeFakeJwt({
    sub: "sub-viewer-1",
    "cognito:username": "dashboard-viewer",
    email: "dashboard-viewer@example.internal",
    exp: 1900000000,
  });

  s = reduceAuth(s, {
    type: "auth-succeeded",
    idToken,
    accessToken: "acc-1",
    refreshToken: "ref-1",
    region: "us-east-2",
  });
  assert.equal(s.phase, "authenticated");
  assert.equal(s.username, "dashboard-viewer");
  assert.equal(s.sub, "sub-viewer-1");
  assert.equal(s.email, "dashboard-viewer@example.internal");
  assert.equal(s.exp, 1900000000);
  assert.equal(s.refreshToken, "ref-1");
  assert.equal(s.region, "us-east-2");

  const refreshedToken = makeFakeJwt({
    sub: "sub-viewer-1",
    "cognito:username": "dashboard-viewer",
    exp: 1900000900,
  });
  s = reduceAuth(s, {
    type: "token-refreshed",
    idToken: refreshedToken,
    accessToken: "acc-2",
  });
  assert.equal(s.phase, "authenticated");
  assert.equal(s.idToken, refreshedToken);
  assert.equal(s.accessToken, "acc-2");
  assert.equal(s.refreshToken, "ref-1");
  assert.equal(s.exp, 1900000900);

  const expired = reduceAuth(s, { type: "auth-expired", error: "Session expired. Please sign in again." });
  assert.equal(expired.phase, "auth-expired");
  assert.equal(expired.idToken, null);
  assert.equal(expired.accessToken, null);
  assert.equal(expired.refreshToken, null);
  assert.equal(expired.error, "Session expired. Please sign in again.");

  const failed = reduceAuth(s, { type: "auth-failed", error: "Invalid username or password" });
  assert.equal(failed.phase, "unauthenticated");
  assert.equal(failed.idToken, null);
  assert.equal(failed.error, "Invalid username or password");

  const signedOut = reduceAuth(s, { type: "signed-out" });
  assert.deepEqual(signedOut, initialAuthState);
  assert.equal(reduceAuth(s, { type: "unknown" }), s);
});

test("buildInitiateAuthPayload and buildRefreshTokenPayload construct Cognito payloads", () => {
  const loginPayload = buildInitiateAuthPayload("  dashboard-viewer ", "Secret#123456", "client-dash");
  assert.deepEqual(loginPayload, {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: "client-dash",
    AuthParameters: {
      USERNAME: "dashboard-viewer",
      PASSWORD: "Secret#123456",
    },
  });

  const refreshPayload = buildRefreshTokenPayload("refresh-xyz", "client-dash");
  assert.deepEqual(refreshPayload, {
    AuthFlow: "REFRESH_TOKEN_AUTH",
    ClientId: "client-dash",
    AuthParameters: {
      REFRESH_TOKEN: "refresh-xyz",
    },
  });
});

test("parseJwtClaims and isTokenExpiring evaluate ID token expiration", () => {
  const nowMs = 1_700_000_000_000;
  const freshToken = makeFakeJwt({ sub: "u1", "cognito:username": "dashboard-viewer", exp: 1_700_000_000 + 300 });
  const soonToken = makeFakeJwt({ sub: "u1", username: "dashboard-viewer", exp: 1_700_000_000 + 30 });
  assert.equal(TOKEN_EXPIRY_MARGIN_SECONDS, 60);
  assert.equal(parseJwtClaims(freshToken).username, "dashboard-viewer");
  assert.equal(parseJwtClaims("malformed"), null);
  assert.equal(isTokenExpiring(freshToken, 60, nowMs), false);
  assert.equal(isTokenExpiring(soonToken, 60, nowMs), true);
  assert.equal(isTokenExpiring(null, 60, nowMs), true);
});

test("canPoll allows polling only when authenticated with a non-expired ID token", () => {
  const nowMs = 1_700_000_000_000;
  const validToken = makeFakeJwt({ sub: "u1", "cognito:username": "dashboard-viewer", exp: 1_700_000_000 + 600 });
  const expiredToken = makeFakeJwt({ sub: "u1", "cognito:username": "dashboard-viewer", exp: 1_700_000_000 - 5 });

  assert.equal(canPoll(initialAuthState, nowMs), false);
  assert.equal(canPoll(null, nowMs), false);

  const active = reduceAuth(initialAuthState, {
    type: "auth-succeeded",
    idToken: validToken,
    accessToken: "acc",
    refreshToken: "ref",
  });
  assert.equal(canPoll(active, nowMs), true);

  const expiredState = reduceAuth(initialAuthState, {
    type: "auth-succeeded",
    idToken: expiredToken,
    accessToken: "acc",
    refreshToken: "ref",
  });
  assert.equal(canPoll(expiredState, nowMs), false);
});

test("evaluateUnauthorized chooses refresh when refreshToken exists and expire otherwise", () => {
  assert.equal(evaluateUnauthorized(initialAuthState), "expire");
  assert.equal(evaluateUnauthorized({ phase: "authenticated", refreshToken: "ref-token" }), "refresh");
  assert.equal(evaluateUnauthorized({ phase: "authenticated", refreshToken: "" }), "expire");
  assert.equal(evaluateUnauthorized(null), "expire");
});

test("buildAuthHeaders and purgeAuthTokens handle Bearer headers and storage cleanup", () => {
  assert.deepEqual(buildAuthHeaders("jwt-123"), { Authorization: "Bearer jwt-123" });
  assert.deepEqual(buildAuthHeaders(""), {});
  const store = new Map([
    ["dr-demo-id-token", "id"],
    ["dr-demo-access-token", "acc"],
    ["dr-demo-refresh-token", "ref"],
    ["dr-demo-auth-region", "us-east-2"],
  ]);
  const storageMock = { removeItem: (k) => store.delete(k) };
  const reset = purgeAuthTokens(storageMock);
  assert.deepEqual(reset, initialAuthState);
  assert.equal(store.size, 0);
});

test("selectCognitoTarget and shouldSwitchAuthRegion support dashboard failover and switchback", () => {
  const cfg = {
    apiBaseUrl: "https://api.example.invalid",
    cognito: {
      primary: {
        region: "us-east-2",
        userPoolId: "us-east-2_Pool1",
        clientId: "client-dash-1",
        endpoint: "https://cognito-idp.us-east-2.amazonaws.com",
        apiBaseUrl: "https://api.example.invalid",
      },
      standby: {
        region: "us-west-2",
        userPoolId: "us-east-2_Pool1",
        clientId: "client-dash-1",
        endpoint: "https://cognito-idp.us-west-2.amazonaws.com",
        apiBaseUrl: "https://api.example.invalid",
      },
    },
  };

  const primarySelection = selectCognitoTarget(cfg, "primary");
  assert.equal(primarySelection.active.regionKey, "primary");
  assert.equal(primarySelection.active.region, "us-east-2");
  assert.equal(primarySelection.alternate.regionKey, "standby");
  assert.equal(primarySelection.alternate.region, "us-west-2");

  const standbySelection = selectCognitoTarget(cfg, "us-west-2");
  assert.equal(standbySelection.active.regionKey, "standby");
  assert.equal(standbySelection.alternate.regionKey, "primary");
  assert.ok(FAILOVER_AUTH_TIMEOUT_MS < 5000);

  assert.equal(shouldSwitchAuthRegion({ status: null, isNetworkError: true, currentAuthRegion: "primary" }), true);
  assert.equal(shouldSwitchAuthRegion({ status: 503, isNetworkError: false, currentAuthRegion: "primary" }), true);
  assert.equal(shouldSwitchAuthRegion({ status: 401, isNetworkError: false, currentAuthRegion: "primary" }), true);
  assert.equal(shouldSwitchAuthRegion({ status: 200, servedByRegion: "us-west-2", currentAuthRegion: "primary" }), true);
  assert.equal(shouldSwitchAuthRegion({ status: 200, servedByRegion: "us-east-2", currentAuthRegion: "standby" }), true);
  assert.equal(shouldSwitchAuthRegion({ status: 200, servedByRegion: "us-east-2", currentAuthRegion: "primary" }), false);
});

test("reduceAuth region-switched updates activeRegion and tokens seamlessly during failover", () => {
  const eastJwt = makeFakeJwt({ sub: "sub-v1", "cognito:username": "dashboard-viewer", exp: 1900000000 });
  const westJwt = makeFakeJwt({ sub: "sub-v1", "cognito:username": "dashboard-viewer", exp: 1900000900 });

  let s = reduceAuth(initialAuthState, {
    type: "auth-succeeded",
    idToken: eastJwt,
    accessToken: "acc-east",
    refreshToken: "ref-east",
    region: "us-east-2",
    activeRegion: "primary",
  });
  assert.equal(s.activeRegion, "primary");
  assert.equal(s.region, "us-east-2");

  s = reduceAuth(s, {
    type: "region-switched",
    activeRegion: "standby",
    region: "us-west-2",
    idToken: westJwt,
    accessToken: "acc-west",
    refreshToken: "ref-west",
  });
  assert.equal(s.phase, "authenticated");
  assert.equal(s.activeRegion, "standby");
  assert.equal(s.region, "us-west-2");
  assert.equal(s.idToken, westJwt);
  assert.equal(s.accessToken, "acc-west");
  assert.equal(s.refreshToken, "ref-west");
});
