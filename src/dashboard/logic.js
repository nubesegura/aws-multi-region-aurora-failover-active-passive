/** Pure functions of the dashboard: no DOM, no network, no clock access. */

export const POLL_INTERVAL_MS = 5000;
export const REQUEST_TIMEOUT_MS = 4000;
export const DEMO_REGIONS = Object.freeze(["us-east-2", "us-west-2"]);

export const initialState = Object.freeze({
  phase: "loading",
  stats: null,
  lastSuccessAt: null,
  lastChange: null,
  freshChange: false,
  failureReason: null,
  lastRoleChange: null,
  timeline: [],
  account: null,
  history: null,
  latencies: [],
  consistency: { phase: "idle", report: null, at: null, reason: null },
});

export const HISTORY_LIMIT = 50;
export const TIMELINE_LIMIT = 20;
export const LATENCY_SAMPLES = 100;
export const TOKEN_EXPIRY_MARGIN_SECONDS = 60;
export const FAILOVER_AUTH_TIMEOUT_MS = 4000;

export const initialAuthState = Object.freeze({
  phase: "unauthenticated",
  idToken: null,
  accessToken: null,
  refreshToken: null,
  username: null,
  sub: null,
  email: null,
  exp: null,
  region: null,
  activeRegion: "primary",
  error: null,
});

function decodeBase64Url(segment) {
  if (typeof segment !== "string" || !segment) {
    return null;
  }
  const normalized = segment.replace(/-/g, "+").replace(/_/g, "/");
  const padLength = (4 - (normalized.length % 4)) % 4;
  const padded = normalized + "=".repeat(padLength);
  try {
    if (typeof globalThis.atob === "function") {
      const binary = globalThis.atob(padded);
      const bytes = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
      return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    }
    if (typeof Buffer !== "undefined") {
      return Buffer.from(padded, "base64").toString("utf8");
    }
    return null;
  } catch {
    return null;
  }
}

/** Decode JWT payload claims (`sub`, `cognito:username`/`username`, `email`, `exp`). */
export function parseJwtClaims(idToken) {
  if (typeof idToken !== "string") {
    return null;
  }
  const parts = idToken.split(".");
  if (parts.length !== 3 || !parts[1]) {
    return null;
  }
  const decoded = decodeBase64Url(parts[1]);
  if (!decoded) {
    return null;
  }
  try {
    const payload = JSON.parse(decoded);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      return null;
    }
    const username = typeof payload["cognito:username"] === "string" && payload["cognito:username"].trim()
      ? payload["cognito:username"].trim()
      : (typeof payload.username === "string" && payload.username.trim() ? payload.username.trim() : null);
    const sub = typeof payload.sub === "string" && payload.sub.trim() ? payload.sub.trim() : null;
    const email = typeof payload.email === "string" && payload.email.trim() ? payload.email.trim() : null;
    const exp = typeof payload.exp === "number" && Number.isFinite(payload.exp) ? payload.exp : null;
    return {
      ...payload,
      sub,
      username,
      email,
      exp,
    };
  } catch {
    return null;
  }
}

/** Check if an ID token is missing, malformed, or expiring within `marginSeconds`. */
export function isTokenExpiring(idToken, marginSeconds = TOKEN_EXPIRY_MARGIN_SECONDS, nowMs = Date.now()) {
  const claims = parseJwtClaims(idToken);
  if (!claims || typeof claims.exp !== "number") {
    return true;
  }
  const margin = typeof marginSeconds === "number" && Number.isFinite(marginSeconds) ? marginSeconds : TOKEN_EXPIRY_MARGIN_SECONDS;
  const nowSeconds = Math.floor(nowMs / 1000);
  return claims.exp <= nowSeconds + margin;
}

/** Pure reducer for dashboard authentication state lifecycle. */
export function reduceAuth(state = initialAuthState, action) {
  if (!action || typeof action !== "object") {
    return state;
  }
  switch (action.type) {
    case "auth-started":
      return { ...state, phase: "signing-in", error: null };
    case "auth-succeeded": {
      const claims = parseJwtClaims(action.idToken);
      return {
        phase: "authenticated",
        idToken: action.idToken ?? null,
        accessToken: action.accessToken ?? null,
        refreshToken: action.refreshToken ?? state.refreshToken ?? null,
        username: action.username ?? claims?.username ?? null,
        sub: claims?.sub ?? null,
        email: claims?.email ?? null,
        exp: claims?.exp ?? null,
        region: action.region ?? state.region ?? null,
        activeRegion: action.activeRegion ?? state.activeRegion ?? "primary",
        error: null,
      };
    }
    case "token-refreshed":
    case "region-switched": {
      const claims = parseJwtClaims(action.idToken);
      return {
        ...state,
        phase: "authenticated",
        idToken: action.idToken ?? state.idToken,
        accessToken: action.accessToken ?? state.accessToken,
        refreshToken: action.refreshToken ?? state.refreshToken,
        username: claims?.username ?? state.username,
        sub: claims?.sub ?? state.sub,
        email: claims?.email ?? state.email,
        exp: claims?.exp ?? state.exp,
        region: action.region ?? state.region,
        activeRegion: action.activeRegion ?? state.activeRegion ?? "primary",
        error: null,
      };
    }
    case "auth-expired":
      return {
        ...initialAuthState,
        phase: "auth-expired",
        error: action.error ?? "Session expired. Please sign in again.",
      };
    case "auth-failed":
      return {
        ...initialAuthState,
        phase: "unauthenticated",
        error: action.error ?? "Invalid username or password",
      };
    case "signed-out":
      return { ...initialAuthState };
    default:
      return state;
  }
}

/** Build Cognito InitiateAuth request body for USER_PASSWORD_AUTH. */
export function buildInitiateAuthPayload(username, password, clientId) {
  return {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: String(clientId ?? "").trim(),
    AuthParameters: {
      USERNAME: String(username ?? "").trim(),
      PASSWORD: String(password ?? ""),
    },
  };
}

/** Build Cognito InitiateAuth request body for REFRESH_TOKEN_AUTH. */
export function buildRefreshTokenPayload(refreshToken, clientId) {
  return {
    AuthFlow: "REFRESH_TOKEN_AUTH",
    ClientId: String(clientId ?? "").trim(),
    AuthParameters: {
      REFRESH_TOKEN: String(refreshToken ?? "").trim(),
    },
  };
}

/** Pure polling guard: returns true only when session is active and ID token is not expired. */
export function canPoll(authState, nowMs = Date.now()) {
  if (!authState || authState.phase !== "authenticated" || !authState.idToken) {
    return false;
  }
  return !isTokenExpiring(authState.idToken, 0, nowMs);
}

/** Evaluate 401 handling: returns "refresh" when a refresh token is present, else "expire". */
export function evaluateUnauthorized(authState) {
  if (authState && typeof authState.refreshToken === "string" && authState.refreshToken.trim() !== "") {
    return "refresh";
  }
  return "expire";
}

/** Build HTTP request headers with `Authorization: Bearer <idToken>`. */
export function buildAuthHeaders(idToken, extraHeaders = {}) {
  const headers = { ...extraHeaders };
  if (typeof idToken === "string" && idToken.trim() !== "") {
    headers.Authorization = `Bearer ${idToken.trim()}`;
  }
  return headers;
}

/** Clear all authentication tokens from storage and return reset auth state. */
export function purgeAuthTokens(storage = null) {
  const keys = [
    "dr-demo-id-token",
    "dr-demo-access-token",
    "dr-demo-refresh-token",
    "dr-demo-auth-region",
  ];
  if (storage && typeof storage.removeItem === "function") {
    for (const key of keys) {
      storage.removeItem(key);
    }
  }
  return { ...initialAuthState };
}

function formatCognitoBlock(config, regionKey) {
  const cognito = config?.cognito ?? {};
  const block = cognito[regionKey] ?? {};
  const defaultRegion = regionKey === "standby" ? "us-west-2" : "us-east-2";
  const region = block.region || defaultRegion;
  const endpoint = block.endpoint || `https://cognito-idp.${region}.amazonaws.com`;
  return {
    regionKey,
    region,
    userPoolId: block.userPoolId || "",
    clientId: block.clientId || "",
    endpoint,
    apiBaseUrl: block.apiBaseUrl || config?.apiBaseUrl || "",
  };
}

/** Resolve active and alternate Cognito targets (`primary` vs `standby`) for failover and switchback. */
export function selectCognitoTarget(config, activeAuthRegion = "primary") {
  const standbyRegion = config?.cognito?.standby?.region || "us-west-2";
  const isStandby = activeAuthRegion === "standby" || activeAuthRegion === standbyRegion;
  const activeKey = isStandby ? "standby" : "primary";
  const alternateKey = isStandby ? "primary" : "standby";
  return {
    active: formatCognitoBlock(config, activeKey),
    alternate: formatCognitoBlock(config, alternateKey),
  };
}

/** Determine whether the client should switch its Cognito authentication target to the alternate region. */
export function shouldSwitchAuthRegion({
  status = null,
  isNetworkError = false,
  servedByRegion = null,
  currentAuthRegion = "primary",
} = {}) {
  if (isNetworkError || status === null) {
    return true;
  }
  if (typeof status === "number" && status >= 500 && status <= 599) {
    return true;
  }
  if (status === 401) {
    return true;
  }
  if (typeof servedByRegion === "string" && servedByRegion) {
    const isCurrentlyStandby = currentAuthRegion === "standby" || currentAuthRegion === "us-west-2";
    const servedByStandby = servedByRegion === "us-west-2";
    if (isCurrentlyStandby !== servedByStandby) {
      return true;
    }
  }
  return false;
}

/** Returns {from, to} when the serving region differs from the previous one, else null. */
export function detectRegionChange(previousRegion, currentRegion) {
  if (!previousRegion || !currentRegion || previousRegion === currentRegion) {
    return null;
  }
  return { from: previousRegion, to: currentRegion };
}

/** Percentage (0-100, one decimal) of `count` in `total`; 0 when there is nothing to share. */
export function sharePercent(count, total) {
  if (!total || total <= 0 || !count || count < 0) {
    return 0;
  }
  return Math.min(100, Math.round((count / total) * 1000) / 10);
}

/** One row per region (the demo regions always, plus any other reported; sorted) with count, share and last processed time. */
export function regionRows(stats) {
  const counts = stats.counts_by_region ?? {};
  const lastSeen = stats.last_processed_at_by_region ?? {};
  const names = [...new Set([...DEMO_REGIONS, ...Object.keys(counts), ...Object.keys(lastSeen)])].sort();
  return names.map((region) => ({
    region,
    count: counts[region] ?? 0,
    share: sharePercent(counts[region] ?? 0, stats.total_count),
    lastProcessedAt: lastSeen[region] ?? null,
  }));
}

/** Fixed two-decimal amount; non-numeric input is shown as received. */
export function formatAmount(amount) {
  const value = Number(amount);
  return Number.isFinite(value) ? value.toFixed(2) : String(amount);
}

function parseDate(iso) {
  if (!iso) {
    return null;
  }
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatUtc(iso) {
  const date = parseDate(iso);
  return date ? `${date.toISOString().slice(0, 19).replace("T", " ")} UTC` : "unknown";
}

export function formatLocal(iso, timeZone) {
  const date = parseDate(iso);
  if (!date) {
    return "unknown";
  }
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone,
  }).format(date);
}

/** Returns {from, to} when the database role differs from the previous one, else null. */
export function detectRoleChange(previousRole, currentRole) {
  return detectRegionChange(previousRole, currentRole);
}

/** Rows of the history table: the newest `limit` movements (the API already returns newest first), ready to display. */
export function historyRows(transactions, limit = HISTORY_LIMIT) {
  return (transactions ?? []).slice(0, limit).map((item) => ({
    id: item.id,
    time: formatUtc(item.processed_at),
    type: item.type,
    amount: formatAmount(item.amount),
    balanceAfter: formatAmount(item.balance_after),
    region: item.processed_region,
    status: "applied",
  }));
}

/** {count, min, median, max} of the finite numbers in `list`; the statistics are null when there are none. */
export function summarizeLatencies(list) {
  const values = (list ?? []).filter((item) => typeof item === "number" && Number.isFinite(item)).sort((a, b) => a - b);
  if (values.length === 0) {
    return { count: 0, min: null, median: null, max: null };
  }
  const middle = Math.floor(values.length / 2);
  const median = values.length % 2 === 1 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
  return { count: values.length, min: values[0], median, max: values[values.length - 1] };
}

/** Display model of a GET /consistency report: state is consistent, inconsistent, truncated or unknown. */
export function consistencyView(report) {
  if (!report || typeof report !== "object") {
    return { state: "unknown", label: "No result", firstOffender: null, rows: [], servedBy: null };
  }
  const state = report.truncated ? "truncated" : report.consistent === true ? "consistent" : report.consistent === false ? "inconsistent" : "unknown";
  const label = {
    consistent: "Consistent",
    inconsistent: "Inconsistent",
    truncated: "Not checked: the ledger is larger than the check limit",
    unknown: "No result",
  }[state];
  const byRegion = Object.entries(report.counts_by_region ?? {}).sort(([a], [b]) => a.localeCompare(b));
  return {
    state,
    label,
    firstOffender: state === "inconsistent" ? (report.first_offender ?? null) : null,
    servedBy: report.served_by_region ?? null,
    rows: [
      ["Movements", String(report.count ?? 0)],
      ["Total deposited", formatAmount(report.total_deposited ?? 0)],
      ["Total withdrawn", formatAmount(report.total_withdrawn ?? 0)],
      ["Replayed balance", formatAmount(report.replayed_balance ?? 0)],
      ["Stored balance", formatAmount(report.stored_balance ?? 0)],
      ["Balances match", report.balances_match ? "yes" : "no"],
      ["Balance chain intact", report.chain_ok ? "yes" : "no"],
      ["Duplicate keys", String(report.duplicate_keys ?? 0)],
      ...byRegion.map(([region, count]) => [`Movements in ${region}`, String(count)]),
    ],
  };
}

/** Whole seconds between two ISO timestamps, never negative; null when either is missing or invalid. */
export function secondsSince(thenIso, nowIso) {
  const then = parseDate(thenIso);
  const now = parseDate(nowIso);
  return then && now ? Math.max(0, Math.round((now.getTime() - then.getTime()) / 1000)) : null;
}

/** Unavailable state for the screen: the last data stays, with the time since the last good answer. */
export function unavailableView(state, nowIso) {
  if (state.phase !== "unavailable") {
    return { unavailable: false, seconds: null, text: "", keepsData: state.stats !== null };
  }
  const seconds = secondsSince(state.lastSuccessAt, nowIso);
  const text = seconds === null
    ? "Service unavailable. No successful update yet."
    : `Service unavailable. Last successful update ${seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`} ago.`;
  return { unavailable: true, seconds, text, keepsData: state.stats !== null };
}

export function reduce(state, action) {
  switch (action.type) {
    case "stats-ok": {
      const change = detectRegionChange(state.stats?.served_by_region, action.stats.served_by_region);
      const roleChange = detectRoleChange(state.stats?.database_role, action.stats.database_role);
      const events = [
        ...(change ? [{ at: action.at, kind: "region", ...change }] : []),
        ...(roleChange ? [{ at: action.at, kind: "role", ...roleChange }] : []),
      ];
      return {
        ...state,
        phase: "ok",
        stats: action.stats,
        lastSuccessAt: action.at,
        failureReason: null,
        freshChange: events.length > 0,
        lastChange: change ? { ...change, at: action.at } : state.lastChange,
        lastRoleChange: roleChange ? { ...roleChange, at: action.at } : state.lastRoleChange,
        timeline: [...events.reverse(), ...state.timeline].slice(0, TIMELINE_LIMIT),
        latencies: addLatency(state.latencies, action.roundTripMs),
      };
    }
    case "stats-failed":
      return { ...state, phase: "unavailable", failureReason: action.reason, freshChange: false };
    case "account-ok":
      return { ...state, account: action.account };
    case "history-ok":
      return { ...state, history: action.transactions };
    case "consistency-started":
      return { ...state, consistency: { phase: "checking", report: null, at: null, reason: null } };
    case "consistency-ok":
      return { ...state, consistency: { phase: "done", report: action.report, at: action.at, reason: null } };
    case "consistency-failed":
      return { ...state, consistency: { phase: "failed", report: null, at: action.at ?? null, reason: action.reason } };
    default:
      return state;
  }
}

function addLatency(latencies, roundTripMs) {
  if (typeof roundTripMs !== "number" || !Number.isFinite(roundTripMs)) {
    return latencies;
  }
  return [...latencies, roundTripMs].slice(-LATENCY_SAMPLES);
}

/** Short text for the polite live region; changes only when the situation changes. */
export function statusAnnouncement(state) {
  if (state.phase === "unavailable") {
    const last = state.lastSuccessAt ? formatUtc(state.lastSuccessAt) : "never";
    return `Service unavailable. Last successful update: ${last}. Retrying.`;
  }
  if (state.phase === "ok") {
    const change = state.lastChange && state.lastChange.at === state.lastSuccessAt
      ? ` Active region changed from ${state.lastChange.from} to ${state.lastChange.to}.`
      : "";
    const role = state.lastRoleChange && state.lastRoleChange.at === state.lastSuccessAt
      ? ` Database role changed from ${state.lastRoleChange.from} to ${state.lastRoleChange.to}.`
      : "";
    return `Active region ${state.stats.served_by_region}, database ${state.stats.database_role}.${change}${role}`;
  }
  return "Loading.";
}
