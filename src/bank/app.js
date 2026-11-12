import {
  initialOperateState, reduceOperate, initialOperations, reduceOperations, validateAmount, isRetryable,
  retrySchedule, summarizeLatencies, compareAcknowledged, formatAmount, describeMovementError, buildOperation,
  initialAuthState, reduceAuth, buildInitiateAuthPayload, buildRefreshTokenPayload,
  isTokenExpiring, buildAuthHeaders, purgeAuthTokens, TOKEN_EXPIRY_MARGIN_SECONDS,
  selectCognitoTarget, shouldSwitchAuthRegion, FAILOVER_AUTH_TIMEOUT_MS,
} from "./logic.js";

const ID_TOKEN_STORAGE = "dr-demo-bank-id-token";
const ACCESS_TOKEN_STORAGE = "dr-demo-bank-access-token";
const REFRESH_TOKEN_STORAGE = "dr-demo-bank-refresh-token";
const ACTIVE_REGION_STORAGE = "dr-demo-bank-auth-region";
const AUTH_STORAGE_KEYS = [
  ID_TOKEN_STORAGE,
  ACCESS_TOKEN_STORAGE,
  REFRESH_TOKEN_STORAGE,
  ACTIVE_REGION_STORAGE,
];
const OPS_STORAGE = "dr-demo-bank-operations";
const REQUEST_TIMEOUT_MS = 10000;
const VERIFY_LIMIT = 100;
const REFRESH_CHECK_INTERVAL_MS = 15000;
const COGNITO_TARGET = "AWSCognitoIdentityProviderService.InitiateAuth";
const COGNITO_CONTENT_TYPE = "application/x-amz-json-1.1";
const $ = (id) => document.getElementById(id);

let config = null;
let authState = initialAuthState;
let operate = initialOperateState;
let operations = initialOperations;
let balanceText = "-";
let lastStatus = "";
let refreshTimerId = null;

// Retained strictly in module-private memory for transparent failover re-authentication (never stored in DOM or Web Storage).
let sessionCredentials = null;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function isAuthenticated() {
  return authState.status === "authenticated" && Boolean(authState.idToken);
}

function getCognitoTarget(regionKey = authState.activeRegion || "primary") {
  return selectCognitoTarget(config, regionKey).active;
}

function saveAuthTokens(nextState) {
  try {
    if (nextState.idToken) {
      sessionStorage.setItem(ID_TOKEN_STORAGE, nextState.idToken);
    }
    if (nextState.accessToken) {
      sessionStorage.setItem(ACCESS_TOKEN_STORAGE, nextState.accessToken);
    }
    if (nextState.refreshToken) {
      sessionStorage.setItem(REFRESH_TOKEN_STORAGE, nextState.refreshToken);
    }
    if (nextState.activeRegion) {
      sessionStorage.setItem(ACTIVE_REGION_STORAGE, nextState.activeRegion);
    }
  } catch {
    // Storage unavailable: tokens remain in memory for the active page lifetime.
  }
}

function restoreAuthSession() {
  try {
    const idToken = sessionStorage.getItem(ID_TOKEN_STORAGE);
    const accessToken = sessionStorage.getItem(ACCESS_TOKEN_STORAGE);
    const refreshToken = sessionStorage.getItem(REFRESH_TOKEN_STORAGE);
    const activeRegion = sessionStorage.getItem(ACTIVE_REGION_STORAGE) || "primary";
    if (idToken && !isTokenExpiring(idToken, 5)) {
      authState = reduceAuth(authState, {
        type: "auth-succeeded",
        idToken,
        accessToken,
        refreshToken,
        activeRegion,
      });
    } else {
      authState = purgeAuthTokens(sessionStorage, AUTH_STORAGE_KEYS);
    }
  } catch {
    authState = initialAuthState;
  }
}

function wipeSessionCredentials() {
  if (sessionCredentials) {
    sessionCredentials.username = "";
    sessionCredentials.password = "";
    sessionCredentials = null;
  }
}

function saveOperations() {
  try {
    sessionStorage.setItem(OPS_STORAGE, JSON.stringify(operations));
  } catch {
    // Storage full or blocked: the list stays in memory only.
  }
}

function restoreOperations() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(OPS_STORAGE) ?? "[]");
    operations = reduceOperations(operations, { type: "operations-restored", operations: saved });
  } catch {
    operations = initialOperations;
  }
}

async function loadConfig() {
  const response = await fetch("config.json", { cache: "no-store" });
  return response.json();
}

async function callCognitoInitiateAuth(
  payload,
  regionKey = authState.activeRegion || "primary",
  timeoutMs = REQUEST_TIMEOUT_MS,
) {
  const target = getCognitoTarget(regionKey);
  const response = await fetch(target.endpoint, {
    method: "POST",
    headers: {
      "Content-Type": COGNITO_CONTENT_TYPE,
      "X-Amz-Target": COGNITO_TARGET,
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const body = await response.json().catch(() => null);
  return { ok: response.ok, status: response.status, body, regionKey: target.regionKey };
}

/**
 * Transparent background re-authentication against the alternate Cognito region
 * (`standby` on failover, `primary` on switchback) within the 5-second budget.
 */
async function failoverAuthenticate(explicitRegionKey = null) {
  const { alternate } = selectCognitoTarget(config, authState.activeRegion || "primary");
  const targetKey = explicitRegionKey || alternate.regionKey;
  const target = getCognitoTarget(targetKey);
  if (!target.clientId) {
    return false;
  }
  if (sessionCredentials?.username && sessionCredentials?.password) {
    try {
      const payload = buildInitiateAuthPayload(
        sessionCredentials.username,
        sessionCredentials.password,
        target.clientId,
      );
      const { ok, body } = await callCognitoInitiateAuth(payload, targetKey, FAILOVER_AUTH_TIMEOUT_MS);
      const result = body?.AuthenticationResult;
      if (ok && result?.IdToken) {
        authState = reduceAuth(authState, {
          type: "region-switched",
          activeRegion: target.regionKey,
          idToken: result.IdToken,
          accessToken: result.AccessToken ?? authState.accessToken,
          refreshToken: result.RefreshToken ?? authState.refreshToken,
        });
        saveAuthTokens(authState);
        return true;
      }
    } catch {
      // Fall through to refresh-token attempt on alternate region.
    }
  }
  if (authState.refreshToken) {
    try {
      const payload = buildRefreshTokenPayload(authState.refreshToken, target.clientId);
      const { ok, body } = await callCognitoInitiateAuth(payload, targetKey, FAILOVER_AUTH_TIMEOUT_MS);
      const result = body?.AuthenticationResult;
      if (ok && result?.IdToken) {
        authState = reduceAuth(authState, {
          type: "region-switched",
          activeRegion: target.regionKey,
          idToken: result.IdToken,
          accessToken: result.AccessToken ?? authState.accessToken,
          refreshToken: result.RefreshToken ?? authState.refreshToken,
        });
        saveAuthTokens(authState);
        return true;
      }
    } catch {
      return false;
    }
  }
  return false;
}

async function refreshSessionToken() {
  const regionKey = authState.activeRegion || "primary";
  const target = getCognitoTarget(regionKey);
  if (authState.refreshToken && target.clientId) {
    try {
      const payload = buildRefreshTokenPayload(authState.refreshToken, target.clientId);
      const { ok, body } = await callCognitoInitiateAuth(payload, regionKey, FAILOVER_AUTH_TIMEOUT_MS);
      const result = body?.AuthenticationResult;
      if (ok && result?.IdToken) {
        authState = reduceAuth(authState, {
          type: "token-refreshed",
          idToken: result.IdToken,
          accessToken: result.AccessToken ?? authState.accessToken,
          refreshToken: result.RefreshToken ?? authState.refreshToken,
          activeRegion: regionKey,
        });
        saveAuthTokens(authState);
        return true;
      }
    } catch {
      // Fall through to credential or alternate-region recovery.
    }
  }
  if (sessionCredentials?.username && sessionCredentials?.password && target.clientId) {
    try {
      const payload = buildInitiateAuthPayload(
        sessionCredentials.username,
        sessionCredentials.password,
        target.clientId,
      );
      const { ok, body } = await callCognitoInitiateAuth(payload, regionKey, FAILOVER_AUTH_TIMEOUT_MS);
      const result = body?.AuthenticationResult;
      if (ok && result?.IdToken) {
        authState = reduceAuth(authState, {
          type: "auth-succeeded",
          idToken: result.IdToken,
          accessToken: result.AccessToken ?? null,
          refreshToken: result.RefreshToken ?? authState.refreshToken,
          activeRegion: regionKey,
        });
        saveAuthTokens(authState);
        return true;
      }
    } catch {
      // Fall through to alternate-region failover re-authentication.
    }
  }
  if (await failoverAuthenticate()) {
    return true;
  }
  handleExpiredSession();
  return false;
}

function handleExpiredSession() {
  stopRefreshTimer();
  wipeSessionCredentials();
  authState = purgeAuthTokens(sessionStorage, AUTH_STORAGE_KEYS);
  authState = reduceAuth(authState, {
    type: "signed-out",
    message: "Your session has expired. Please sign in again.",
  });
  balanceText = "-";
  setStatus("Your session has expired. Please sign in again.");
  render();
}

function startRefreshTimer() {
  stopRefreshTimer();
  refreshTimerId = setInterval(async () => {
    if (!isAuthenticated()) {
      return;
    }
    if (isTokenExpiring(authState.idToken, TOKEN_EXPIRY_MARGIN_SECONDS)) {
      await refreshSessionToken();
    }
  }, REFRESH_CHECK_INTERVAL_MS);
}

function stopRefreshTimer() {
  if (refreshTimerId !== null) {
    clearInterval(refreshTimerId);
    refreshTimerId = null;
  }
}

/** One HTTP call with automatic token expiry check, 401 refresh retry, and regional failover re-authentication. */
async function api(path, options = {}) {
  if (isAuthenticated() && isTokenExpiring(authState.idToken, TOKEN_EXPIRY_MARGIN_SECONDS)) {
    const refreshed = await refreshSessionToken();
    if (!refreshed) {
      return { status: 401, ok: false, body: null, error: null };
    }
  }
  const sendOnce = async () => {
    try {
      const baseUrl = getCognitoTarget().apiBaseUrl || config.apiBaseUrl;
      const response = await fetch(`${baseUrl}${path}`, {
        ...options,
        headers: buildAuthHeaders(authState.idToken, options.headers),
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
      const body = await response.json().catch(() => null);
      return { status: response.status, ok: response.ok, body, error: null };
    } catch (error) {
      return { status: null, ok: false, body: null, error };
    }
  };

  let result = await sendOnce();
  if (result.status === 401 && isAuthenticated()) {
    const switched = await failoverAuthenticate();
    if (switched) {
      result = await sendOnce();
    }
    if (result.status === 401 && isAuthenticated()) {
      const refreshed = await refreshSessionToken();
      if (refreshed) {
        result = await sendOnce();
      }
    }
  } else if (
    isAuthenticated()
    && shouldSwitchAuthRegion({
      status: result.status,
      isNetworkError: Boolean(result.error),
      servedByRegion: result.body?.served_by_region,
      currentAuthRegion: authState.activeRegion,
    })
  ) {
    if (!result.ok) {
      await failoverAuthenticate();
    } else {
      void failoverAuthenticate();
    }
  }
  return result;
}

function setStatus(text) {
  if (text !== lastStatus) {
    $("status").textContent = text;
    lastStatus = text;
  }
}

async function refreshBalance() {
  if (!isAuthenticated()) {
    return;
  }
  const { ok, body } = await api("/account");
  if (ok && body && body.balance !== undefined && body.balance !== null) {
    balanceText = formatAmount(body.balance);
    setStatus("Balance updated.");
  } else if (!ok && isAuthenticated()) {
    setStatus("The balance could not be loaded. The last known balance is shown.");
  }
  renderBalance();
}

async function execute(type, amountText) {
  const idempotencyKey = crypto.randomUUID();
  const waits = retrySchedule();
  const startedAt = performance.now();
  operate = reduceOperate(operate, { type: "send-started", key: idempotencyKey });
  render();
  const payload = JSON.stringify({ type, amount: Number(amountText) });
  let attempt = 1;
  for (;;) {
    const result = await api("/transactions", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: payload,
    });
    if (result.ok && result.body?.transaction) {
      const roundTripMs = Math.round(performance.now() - startedAt);
      const operation = buildOperation(result.body, { roundTripMs, attempts: attempt });
      operations = reduceOperations(operations, { type: "operation-recorded", operation });
      saveOperations();
      balanceText = formatAmount(result.body.balance);
      operate = reduceOperate(operate, {
        type: "send-ok",
        result: {
          operation,
          balance: result.body.balance,
          servedBy: result.body.served_by_region,
          role: result.body.database_role,
        },
      });
      setStatus(`Operation completed. Balance ${balanceText}.`);
      render();
      refreshBalance();
      return;
    }
    const failure = result.status ?? result.error;
    if (isRetryable(failure) && attempt <= waits.length) {
      attempt += 1;
      operate = reduceOperate(operate, { type: "attempt-failed", attempt });
      render();
      await sleep(waits[attempt - 2]);
      continue;
    }
    const message = describeMovementError(result.status ?? result.error, result.body);
    operate = reduceOperate(operate, { type: "send-failed", message });
    setStatus(message);
    render();
    return;
  }
}

async function onLoginSubmit(event) {
  event.preventDefault();
  if (authState.status === "authenticating") {
    return;
  }
  const usernameInput = $("login-username");
  const passwordInput = $("login-password");
  const username = usernameInput.value.trim();
  const password = passwordInput.value;

  if (!username || !password) {
    authState = reduceAuth(authState, {
      type: "auth-failed",
      message: "Invalid username or password",
    });
    passwordInput.value = "";
    renderAuth();
    (username ? passwordInput : usernameInput).focus();
    return;
  }

  authState = reduceAuth(authState, { type: "auth-started" });
  renderAuth();
  setStatus("Signing in.");

  try {
    const target = getCognitoTarget("primary");
    const payload = buildInitiateAuthPayload(username, password, target.clientId);
    const { ok, body } = await callCognitoInitiateAuth(payload, "primary");
    const authResult = body?.AuthenticationResult;
    if (!ok || !authResult?.IdToken) {
      authState = reduceAuth(authState, {
        type: "auth-failed",
        message: "Invalid username or password",
      });
      passwordInput.value = "";
      setStatus("Sign-in failed.");
      render();
      passwordInput.focus();
      return;
    }

    sessionCredentials = { username, password };
    passwordInput.value = "";
    authState = reduceAuth(authState, {
      type: "auth-succeeded",
      username,
      idToken: authResult.IdToken,
      accessToken: authResult.AccessToken ?? null,
      refreshToken: authResult.RefreshToken ?? null,
      activeRegion: "primary",
    });
    saveAuthTokens(authState);
    startRefreshTimer();
    setStatus("Loading balance.");
    render();
    await refreshBalance();
  } catch {
    authState = reduceAuth(authState, {
      type: "auth-failed",
      message: "Invalid username or password",
    });
    passwordInput.value = "";
    setStatus("Sign-in failed.");
    render();
    passwordInput.focus();
  }
}

function onSignOut() {
  stopRefreshTimer();
  wipeSessionCredentials();
  authState = purgeAuthTokens(sessionStorage, AUTH_STORAGE_KEYS);
  authState = reduceAuth(authState, { type: "signed-out" });
  operate = initialOperateState;
  balanceText = "-";
  $("login-password").value = "";
  setStatus("Signed out. Sign in to continue.");
  render();
  $("login-username").focus();
}

function onSubmit(event) {
  event.preventDefault();
  if (operate.phase === "sending" || operate.phase === "recovering") {
    return;
  }
  if (!isAuthenticated()) {
    render();
    $("login-username").focus();
    return;
  }
  const check = validateAmount($("move-amount").value);
  const error = $("amount-error");
  error.hidden = check.ok;
  error.textContent = check.ok ? "" : check.message;
  if (!check.ok) {
    $("move-amount").focus();
    return;
  }
  execute($("move-type").value, check.amount);
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) {
    td.className = className;
  }
  return td;
}

const ms = (value) => (value === null || value === undefined ? "-" : `${Math.round(value * 100) / 100} ms`);

function renderAuth() {
  const authenticated = isAuthenticated();
  const authenticating = authState.status === "authenticating";

  $("login-section").hidden = authenticated;
  $("authenticated-view").hidden = !authenticated;
  $("user-profile").hidden = !authenticated;
  $("sign-in-button").disabled = authenticating;

  if (authenticated) {
    $("user-display").textContent = authState.username || "bank-operator";
  }

  const loginError = $("login-error");
  const hasError = !authenticated && Boolean(authState.error);
  loginError.hidden = !hasError;
  loginError.textContent = hasError ? authState.error : "";
}

function renderBalance() {
  $("balance").textContent = balanceText;
}

function renderOperate() {
  const busy = operate.phase === "sending" || operate.phase === "recovering";
  $("execute-button").disabled = busy || !isAuthenticated();
  const message = $("operate-status");
  const text = operate.phase === "sending" ? "Sending." : operate.message;
  message.hidden = operate.phase === "idle" || operate.phase === "done" || text === "";
  message.textContent = text;
  message.dataset.phase = operate.phase;
  const result = $("result");
  result.hidden = !isAuthenticated() || operate.phase !== "done";
  if (isAuthenticated() && operate.phase === "done") {
    const { operation, balance, servedBy, role } = operate.result;
    $("r-result").textContent = `${operation.type === "deposit" ? "Deposit" : "Withdrawal"} of ${formatAmount(operation.amount)} stored`
      + (operation.recovered ? " (recovered after a retry, applied once)" : "");
    $("r-balance").textContent = formatAmount(balance);
    $("r-served").textContent = String(servedBy);
    $("r-processed").textContent = String(operation.region);
    $("r-role").textContent = String(role);
    $("r-round").textContent = ms(operation.roundTripMs);
    $("r-server").textContent = ms(operation.serverMs);
    $("r-attempts").textContent = String(operation.attempts);
  }
}

function renderOperations() {
  const rows = operations.map((op) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(op.id, "id"), cell(op.type), cell(formatAmount(op.amount), "num"), cell(formatAmount(op.balanceAfter), "num"),
      cell(String(op.region)), cell(String(op.backend)), cell(String(op.roundTripMs), "num"),
      cell(op.serverMs === undefined || op.serverMs === null ? "-" : String(op.serverMs), "num"),
      cell(op.recovered ? `${op.attempts} (recovered)` : String(op.attempts), "num"),
    );
    return tr;
  });
  $("ops-body").replaceChildren(...rows);
  const summary = summarizeLatencies(operations.map((op) => op.roundTripMs));
  $("latency-summary").textContent = summary.count === 0
    ? "No operations yet."
    : `Round trip over ${summary.count} operation${summary.count === 1 ? "" : "s"}: min ${ms(summary.min)}, median ${ms(summary.median)}, max ${ms(summary.max)}.`;
}

function render() {
  renderAuth();
  renderBalance();
  renderOperate();
  renderOperations();
}

async function verify() {
  const message = $("verify-message");
  const list = $("verify-missing");
  list.replaceChildren();
  list.hidden = true;
  message.hidden = false;
  message.removeAttribute("data-phase");
  if (!isAuthenticated()) {
    message.textContent = "Sign in first to verify your operations.";
    return;
  }
  if (operations.length === 0) {
    message.textContent = "There are no operations in this session to verify.";
    return;
  }
  $("verify-button").disabled = true;
  message.textContent = "Verifying.";
  const { ok, status, body } = await api(`/transactions?limit=${VERIFY_LIMIT}`);
  $("verify-button").disabled = false;
  if (!ok || !Array.isArray(body?.items)) {
    message.dataset.phase = "failed";
    message.textContent = `The ledger could not be read${status ? ` (HTTP ${status})` : ""}. Try again.`;
    return;
  }
  const missing = compareAcknowledged(operations.map((op) => op.id), body.items.map((item) => item.id));
  if (missing.length === 0) {
    message.dataset.phase = "done";
    message.textContent = `All ${operations.length} acknowledged operation${operations.length === 1 ? " is" : "s are"} in the ledger (newest ${VERIFY_LIMIT} movements checked).`;
    return;
  }
  message.dataset.phase = "failed";
  message.textContent = `${missing.length} acknowledged operation${missing.length === 1 ? " is" : "s are"} missing from the ledger:`;
  list.replaceChildren(...missing.map((id) => {
    const li = document.createElement("li");
    li.textContent = id;
    return li;
  }));
  list.hidden = false;
}

function bindControls() {
  $("login-form").addEventListener("submit", onLoginSubmit);
  $("sign-out-button").addEventListener("click", onSignOut);
  $("move-form").addEventListener("submit", onSubmit);
  $("verify-button").addEventListener("click", verify);
}

async function start() {
  config = await loadConfig();
  restoreOperations();
  restoreAuthSession();
  bindControls();
  render();
  if (isAuthenticated()) {
    startRefreshTimer();
    setStatus("Loading balance.");
    await refreshBalance();
  } else {
    setStatus("Sign in with your demo account to start.");
  }
}

start();
