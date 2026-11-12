/** Pure functions of the banking app: no DOM, no network, no clock access. */

export const RETRY_BUDGET_MS = 60000;
const RETRYABLE_STATUSES = Object.freeze([429, 500, 502, 503, 504]);

/** Validates the amount text of the form. Returns {ok: true, amount: "12.50"} or {ok: false, message}. */
export function validateAmount(text) {
  const value = typeof text === "string" ? text.trim() : "";
  if (value === "") {
    return { ok: false, message: "Enter an amount." };
  }
  const isNegative = /^-\d+(\.\d+)?$/.test(value);
  const isZero = /^\d+(\.\d+)?$/.test(value) && Number(value) === 0;
  if (isNegative || isZero) {
    return { ok: false, message: "The amount must be greater than zero." };
  }
  if (/^\d+\.\d{3,}$/.test(value)) {
    return { ok: false, message: "The amount can have at most 2 decimals." };
  }
  if (!/^\d+(\.\d{1,2})?$/.test(value)) {
    return { ok: false, message: "Enter a valid amount such as 25.00." };
  }
  return { ok: true, amount: Number(value).toFixed(2) };
}

/** True for a network error (anything that is not an HTTP status number) and for 429, 500, 502, 503 and 504. */
export function isRetryable(statusOrError) {
  if (typeof statusOrError !== "number") {
    return true;
  }
  return RETRYABLE_STATUSES.includes(statusOrError);
}

/** Growing waits (ms) between attempts; their sum never exceeds `budgetMs`. */
export function retrySchedule({ budgetMs = RETRY_BUDGET_MS, firstWaitMs = 1000, factor = 2, maxWaitMs = 16000 } = {}) {
  const waits = [];
  let total = 0;
  let wait = firstWaitMs;
  while (total + wait <= budgetMs) {
    waits.push(wait);
    total += wait;
    wait = Math.min(Math.round(wait * factor), maxWaitMs);
  }
  return waits;
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

/** Acknowledged ids that the ledger does not contain (each once, in acknowledged order). */
export function compareAcknowledged(ackIds, ledgerIds) {
  const ledger = new Set(ledgerIds ?? []);
  return [...new Set(ackIds ?? [])].filter((id) => !ledger.has(id));
}

/** Fixed two-decimal amount; non-numeric input is shown as received. */
export function formatAmount(amount) {
  const value = Number(amount);
  return Number.isFinite(value) ? value.toFixed(2) : String(amount);
}

function hasValue(value) {
  return value !== undefined && value !== null && value !== "" && Number.isFinite(Number(value));
}

/** Maps a failed movement answer (HTTP status and parsed body, or a network error) to a message for the customer. */
export function describeMovementError(status, body) {
  const code = body?.error?.code;
  if (typeof status !== "number") {
    return "The bank could not reach the service. Check your connection and try again.";
  }
  if (status === 422 && code === "insufficient_funds") {
    const balance = body?.error?.balance ?? body?.balance;
    return hasValue(balance)
      ? `Insufficient funds. Your balance is ${formatAmount(balance)}. Nothing was changed.`
      : "Insufficient funds. Nothing was changed.";
  }
  if (status === 400) {
    const details = (body?.error?.fields ?? []).map((item) => `${item.field}: ${item.message}`);
    return `The operation was rejected: ${details.length ? details.join("; ") : (body?.error?.message ?? "validation error")}.`;
  }
  if (status === 429) {
    return "Too many requests. Wait a moment and try again.";
  }
  if (status === 401 || status === 403) {
    return "The API key was rejected. Check the key and try again.";
  }
  if (status === 503 && code === "database_read_only") {
    return "The database in the serving region is read-only (secondary not promoted yet). Nothing was stored.";
  }
  if (status === 503) {
    return "The service is unavailable right now. Nothing was stored.";
  }
  return `The operation could not be completed (HTTP ${status}${code ? `, ${code}` : ""}).`;
}

export const initialOperateState = Object.freeze({
  phase: "idle",
  attempt: 0,
  key: null,
  message: "",
  result: null,
});

/** Operate state: idle, sending, recovering (with attempt number), done, failed. A new send is ignored while busy. */
export function reduceOperate(state, action) {
  const busy = state.phase === "sending" || state.phase === "recovering";
  switch (action.type) {
    case "send-started":
      return busy ? state : { phase: "sending", attempt: 1, key: action.key, message: "", result: null };
    case "attempt-failed":
      return busy
        ? { ...state, phase: "recovering", attempt: action.attempt, message: `Recovering, attempt ${action.attempt}` }
        : state;
    case "send-ok":
      return busy ? { ...state, phase: "done", message: "", result: action.result } : state;
    case "send-failed":
      return busy ? { ...state, phase: "failed", message: action.message, result: null } : state;
    default:
      return state;
  }
}

/** One row of the session operations list from the 201 body and the client measurements. */
export function buildOperation(body, { roundTripMs, attempts }) {
  const movement = body.transaction;
  return {
    id: movement.id,
    type: movement.type,
    amount: movement.amount,
    balanceAfter: movement.balance_after,
    region: movement.processed_region,
    backend: body.served_by_region,
    roundTripMs,
    serverMs: body.server_ms,
    attempts,
    recovered: attempts > 1,
  };
}

export const initialOperations = Object.freeze([]);

/** Session operations list, newest first; an id already listed is kept as it was. */
export function reduceOperations(list, action) {
  switch (action.type) {
    case "operation-recorded":
      return list.some((item) => item.id === action.operation.id) ? list : [action.operation, ...list];
    case "operations-restored":
      return Array.isArray(action.operations) ? [...action.operations] : [];
    default:
      return list;
  }
}

export const TOKEN_EXPIRY_MARGIN_SECONDS = 60;

export const initialAuthState = Object.freeze({
  status: "unauthenticated",
  username: null,
  sub: null,
  email: null,
  idToken: null,
  accessToken: null,
  refreshToken: null,
  expiresAtMs: null,
  activeRegion: "primary",
  error: "",
});

function decodeBase64Url(segment) {
  const normalized = segment.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  if (typeof atob === "function") {
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
    return new TextDecoder().decode(bytes);
  }
  return Buffer.from(padded, "base64").toString("utf8");
}

/** Decode the payload of a Cognito JWT ID token without external libraries; returns null when malformed. */
export function parseJwtClaims(idToken) {
  if (typeof idToken !== "string" || !idToken.trim()) {
    return null;
  }
  const parts = idToken.split(".");
  if (parts.length !== 3 || !parts[1]) {
    return null;
  }
  try {
    const raw = JSON.parse(decodeBase64Url(parts[1]));
    if (!raw || typeof raw !== "object") {
      return null;
    }
    const username = raw["cognito:username"] ?? raw.username ?? null;
    return {
      sub: typeof raw.sub === "string" ? raw.sub : null,
      username: typeof username === "string" ? username : null,
      "cognito:username": typeof username === "string" ? username : null,
      email: typeof raw.email === "string" ? raw.email : null,
      exp: typeof raw.exp === "number" && Number.isFinite(raw.exp) ? raw.exp : null,
      aud: typeof raw.aud === "string" ? raw.aud : null,
      token_use: typeof raw.token_use === "string" ? raw.token_use : null,
    };
  } catch {
    return null;
  }
}

/** True when `idToken` is missing, malformed, or expires within `marginSeconds` from `nowMs`. */
export function isTokenExpiring(idToken, marginSeconds = TOKEN_EXPIRY_MARGIN_SECONDS, nowMs = Date.now()) {
  const claims = parseJwtClaims(idToken);
  if (!claims || typeof claims.exp !== "number") {
    return true;
  }
  const marginMs = Math.max(0, Number(marginSeconds) || 0) * 1000;
  return claims.exp * 1000 - nowMs <= marginMs;
}

/** Build the JSON request body for Cognito InitiateAuth (USER_PASSWORD_AUTH). */
export function buildInitiateAuthPayload(username, password, clientId) {
  return {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: String(clientId ?? ""),
    AuthParameters: {
      USERNAME: String(username ?? "").trim(),
      PASSWORD: String(password ?? ""),
    },
  };
}

/** Build the JSON request body for Cognito InitiateAuth (REFRESH_TOKEN_AUTH). */
export function buildRefreshTokenPayload(refreshToken, clientId) {
  return {
    AuthFlow: "REFRESH_TOKEN_AUTH",
    ClientId: String(clientId ?? ""),
    AuthParameters: {
      REFRESH_TOKEN: String(refreshToken ?? ""),
    },
  };
}

/** Build HTTP headers with `Authorization: Bearer <idToken>` when a token is present. */
export function buildAuthHeaders(idToken, extraHeaders = {}) {
  const token = typeof idToken === "string" ? idToken.trim() : "";
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...extraHeaders,
  };
}

/** Pure reducer for banking authentication state transitions. */
export function reduceAuth(state = initialAuthState, action = {}) {
  switch (action.type) {
    case "auth-started":
      return { ...state, status: "authenticating", error: "" };
    case "auth-succeeded": {
      const claims = parseJwtClaims(action.idToken);
      return {
        ...state,
        status: "authenticated",
        username: action.username ?? claims?.username ?? state.username ?? null,
        sub: action.sub ?? claims?.sub ?? state.sub ?? null,
        email: action.email ?? claims?.email ?? state.email ?? null,
        idToken: action.idToken ?? null,
        accessToken: action.accessToken ?? null,
        refreshToken: action.refreshToken ?? state.refreshToken ?? null,
        expiresAtMs: action.expiresAtMs ?? (claims?.exp ? claims.exp * 1000 : null),
        activeRegion: action.activeRegion ?? state.activeRegion ?? "primary",
        error: "",
      };
    }
    case "token-refreshed":
    case "region-switched": {
      const claims = parseJwtClaims(action.idToken);
      return {
        ...state,
        status: "authenticated",
        username: action.username ?? claims?.username ?? state.username ?? null,
        sub: action.sub ?? claims?.sub ?? state.sub ?? null,
        email: action.email ?? claims?.email ?? state.email ?? null,
        idToken: action.idToken ?? state.idToken,
        accessToken: action.accessToken ?? state.accessToken,
        refreshToken: action.refreshToken ?? state.refreshToken,
        expiresAtMs: action.expiresAtMs ?? (claims?.exp ? claims.exp * 1000 : state.expiresAtMs),
        activeRegion: action.activeRegion ?? state.activeRegion ?? "primary",
        error: "",
      };
    }
    case "auth-failed":
      return {
        ...initialAuthState,
        activeRegion: state.activeRegion ?? "primary",
        error: action.message ?? "Invalid username or password",
      };
    case "signed-out":
      return {
        ...initialAuthState,
        error: action.message ?? "",
      };
    default:
      return state;
  }
}

/** Clear all stored token keys from `storage` (if provided) and return a clean unauthenticated state. */
export function purgeAuthTokens(storage, keys = []) {
  if (storage && typeof storage.removeItem === "function") {
    for (const key of keys) {
      try {
        storage.removeItem(key);
      } catch {
        // Ignore storage errors during sign-out cleanup.
      }
    }
  }
  return { ...initialAuthState };
}

export const FAILOVER_AUTH_TIMEOUT_MS = 4000;

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
