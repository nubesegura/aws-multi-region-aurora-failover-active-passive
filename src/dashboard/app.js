import {
  POLL_INTERVAL_MS, REQUEST_TIMEOUT_MS, HISTORY_LIMIT, initialState, reduce, regionRows, formatAmount,
  formatUtc, historyRows, summarizeLatencies, consistencyView, unavailableView, statusAnnouncement,
  initialAuthState, reduceAuth, buildInitiateAuthPayload, buildRefreshTokenPayload,
  isTokenExpiring, buildAuthHeaders, purgeAuthTokens, canPoll, evaluateUnauthorized,
  TOKEN_EXPIRY_MARGIN_SECONDS, selectCognitoTarget, shouldSwitchAuthRegion, FAILOVER_AUTH_TIMEOUT_MS,
} from "./logic.js";

const ID_TOKEN_STORAGE = "dr-demo-id-token";
const ACCESS_TOKEN_STORAGE = "dr-demo-access-token";
const REFRESH_TOKEN_STORAGE = "dr-demo-refresh-token";
const AUTH_REGION_STORAGE = "dr-demo-auth-region";
const COGNITO_TARGET_INITIATE_AUTH = "AWSCognitoIdentityProviderService.InitiateAuth";
const COGNITO_CONTENT_TYPE = "application/x-amz-json-1.1";
const REFRESH_CHECK_INTERVAL_MS = 30_000;

const $ = (id) => document.getElementById(id);

let state = initialState;
let authState = initialAuthState;
let config = null;
let pollTimer = null;
let refreshTimer = null;
let refreshPromise = null;
let lastAnnouncement = "";
let pollGeneration = 0;

/**
 * Module-private closure retaining active session credentials in memory only
 * for transparent failover re-authentication (Requirement 4.2, 6.2).
 * Never written to sessionStorage, localStorage, DOM, or console logs.
 */
const sessionCredentials = (() => {
  let creds = null;
  return {
    set(username, password) {
      creds = { username: String(username ?? "").trim(), password: String(password ?? "") };
    },
    get() {
      return creds ? { ...creds } : null;
    },
    clear() {
      creds = null;
    },
  };
})();

function getActiveCognitoTarget(regionPreference = null) {
  const targetKey = regionPreference ?? authState.activeRegion ?? authState.region ?? sessionStorage.getItem(AUTH_REGION_STORAGE) ?? "primary";
  return selectCognitoTarget(config, targetKey).active;
}

function saveTokensToStorage({ idToken, accessToken, refreshToken, region }) {
  if (idToken) {
    sessionStorage.setItem(ID_TOKEN_STORAGE, idToken);
  }
  if (accessToken) {
    sessionStorage.setItem(ACCESS_TOKEN_STORAGE, accessToken);
  }
  if (refreshToken) {
    sessionStorage.setItem(REFRESH_TOKEN_STORAGE, refreshToken);
  }
  if (region) {
    sessionStorage.setItem(AUTH_REGION_STORAGE, region);
  }
}

function restoreTokensFromStorage() {
  const idToken = sessionStorage.getItem(ID_TOKEN_STORAGE);
  const accessToken = sessionStorage.getItem(ACCESS_TOKEN_STORAGE);
  const refreshToken = sessionStorage.getItem(REFRESH_TOKEN_STORAGE);
  const region = sessionStorage.getItem(AUTH_REGION_STORAGE);
  if (idToken && !isTokenExpiring(idToken, 0)) {
    authState = reduceAuth(authState, {
      type: "auth-succeeded",
      idToken,
      accessToken,
      refreshToken,
      region,
    });
  } else if (idToken || refreshToken) {
    authState = purgeAuthTokens(sessionStorage);
  }
}

async function cognitoInitiateAuth(payload, target, timeoutMs = REQUEST_TIMEOUT_MS) {
  const response = await fetch(target.endpoint, {
    method: "POST",
    headers: {
      "Content-Type": COGNITO_CONTENT_TYPE,
      "X-Amz-Target": COGNITO_TARGET_INITIATE_AUTH,
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body?.AuthenticationResult?.IdToken) {
    const type = String(body?.__type ?? "");
    const isAuthError = response.status === 400 && (
      type.includes("NotAuthorizedException")
      || type.includes("UserNotFoundException")
      || type.includes("InvalidParameterException")
    );
    const error = new Error(isAuthError ? "Invalid username or password" : (body?.message || `Authentication failed (HTTP ${response.status})`));
    error.isAuthError = isAuthError;
    throw error;
  }
  return body.AuthenticationResult;
}

/**
 * Transparent background re-authentication against the alternate Cognito region
 * (`standby` on failover, `primary` on switchback) within the 5-second budget.
 */
async function failoverAuthenticate(explicitRegionKey = null) {
  const { alternate } = selectCognitoTarget(config, authState.activeRegion || authState.region || "primary");
  const target = explicitRegionKey ? getActiveCognitoTarget(explicitRegionKey) : alternate;
  if (!target.clientId) {
    return false;
  }
  const creds = sessionCredentials.get();
  if (creds) {
    try {
      const payload = buildInitiateAuthPayload(creds.username, creds.password, target.clientId);
      const result = await cognitoInitiateAuth(payload, target, FAILOVER_AUTH_TIMEOUT_MS);
      const nextRefresh = result.RefreshToken ?? authState.refreshToken;
      saveTokensToStorage({
        idToken: result.IdToken,
        accessToken: result.AccessToken,
        refreshToken: nextRefresh,
        region: target.region,
      });
      authState = reduceAuth(authState, {
        type: "region-switched",
        activeRegion: target.regionKey,
        region: target.region,
        idToken: result.IdToken,
        accessToken: result.AccessToken,
        refreshToken: nextRefresh,
      });
      render();
      return true;
    } catch {
      // Fall through to refresh-token attempt on alternate region.
    }
  }
  const refreshToken = authState.refreshToken ?? sessionStorage.getItem(REFRESH_TOKEN_STORAGE);
  if (refreshToken) {
    try {
      const payload = buildRefreshTokenPayload(refreshToken, target.clientId);
      const result = await cognitoInitiateAuth(payload, target, FAILOVER_AUTH_TIMEOUT_MS);
      const nextRefresh = result.RefreshToken ?? refreshToken;
      saveTokensToStorage({
        idToken: result.IdToken,
        accessToken: result.AccessToken,
        refreshToken: nextRefresh,
        region: target.region,
      });
      authState = reduceAuth(authState, {
        type: "region-switched",
        activeRegion: target.regionKey,
        region: target.region,
        idToken: result.IdToken,
        accessToken: result.AccessToken,
        refreshToken: nextRefresh,
      });
      render();
      return true;
    } catch {
      return false;
    }
  }
  return false;
}

async function refreshSession() {
  if (refreshPromise) {
    return refreshPromise;
  }
  refreshPromise = (async () => {
    const target = getActiveCognitoTarget();
    const refreshToken = authState.refreshToken ?? sessionStorage.getItem(REFRESH_TOKEN_STORAGE);
    if (refreshToken && target.clientId) {
      try {
        const payload = buildRefreshTokenPayload(refreshToken, target.clientId);
        const result = await cognitoInitiateAuth(payload, target, FAILOVER_AUTH_TIMEOUT_MS);
        const nextRefresh = result.RefreshToken ?? refreshToken;
        saveTokensToStorage({
          idToken: result.IdToken,
          accessToken: result.AccessToken,
          refreshToken: nextRefresh,
          region: target.region,
        });
        authState = reduceAuth(authState, {
          type: "token-refreshed",
          idToken: result.IdToken,
          accessToken: result.AccessToken,
          refreshToken: nextRefresh,
          region: target.region,
          activeRegion: target.regionKey,
        });
        render();
        return true;
      } catch {
        // Fall through to in-memory credentials or alternate region.
      }
    }
    const creds = sessionCredentials.get();
    if (creds && target.clientId) {
      try {
        const payload = buildInitiateAuthPayload(creds.username, creds.password, target.clientId);
        const result = await cognitoInitiateAuth(payload, target, FAILOVER_AUTH_TIMEOUT_MS);
        saveTokensToStorage({
          idToken: result.IdToken,
          accessToken: result.AccessToken,
          refreshToken: result.RefreshToken,
          region: target.region,
        });
        authState = reduceAuth(authState, {
          type: "auth-succeeded",
          idToken: result.IdToken,
          accessToken: result.AccessToken,
          refreshToken: result.RefreshToken,
          username: creds.username,
          region: target.region,
          activeRegion: target.regionKey,
        });
        render();
        return true;
      } catch {
        // Fall through to alternate-region failover re-authentication.
      }
    }
    return failoverAuthenticate();
  })();
  try {
    return await refreshPromise;
  } finally {
    refreshPromise = null;
  }
}

function stopTimers() {
  clearTimeout(pollTimer);
  pollTimer = null;
  clearTimeout(refreshTimer);
  refreshTimer = null;
}

function handleSessionExpired() {
  stopTimers();
  sessionCredentials.clear();
  purgeAuthTokens(sessionStorage);
  authState = reduceAuth(authState, {
    type: "auth-expired",
    error: "Session expired. Please sign in again.",
  });
  render();
}

function scheduleRefreshCheck() {
  clearTimeout(refreshTimer);
  if (authState.phase !== "authenticated") {
    return;
  }
  refreshTimer = setTimeout(async () => {
    if (authState.phase !== "authenticated") {
      return;
    }
    if (isTokenExpiring(authState.idToken, TOKEN_EXPIRY_MARGIN_SECONDS)) {
      const ok = await refreshSession();
      if (!ok) {
        handleSessionExpired();
        return;
      }
    }
    scheduleRefreshCheck();
  }, REFRESH_CHECK_INTERVAL_MS);
}

async function ensureFreshToken() {
  if (authState.phase !== "authenticated") {
    return false;
  }
  if (!isTokenExpiring(authState.idToken, TOKEN_EXPIRY_MARGIN_SECONDS)) {
    return true;
  }
  const ok = await refreshSession();
  if (!ok) {
    handleSessionExpired();
    return false;
  }
  return true;
}

async function loadConfig() {
  const response = await fetch("config.json", { cache: "no-store" });
  return response.json();
}

/** Read-only API client: GET requests only with Bearer ID token, automatic 401 refresh, and regional failover re-auth. */
async function apiGet(path, allowRefreshRetry = true) {
  const ready = await ensureFreshToken();
  if (!ready) {
    return { status: 401, ok: false, body: { error: { message: "Session expired" } } };
  }
  const baseUrl = getActiveCognitoTarget().apiBaseUrl || config.apiBaseUrl;
  let response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      headers: buildAuthHeaders(authState.idToken),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (error) {
    if (allowRefreshRetry && shouldSwitchAuthRegion({ status: null, isNetworkError: true, currentAuthRegion: authState.activeRegion })) {
      await failoverAuthenticate();
    }
    throw error;
  }
  if (response.status === 401 && allowRefreshRetry) {
    const switched = await failoverAuthenticate();
    if (switched) {
      return apiGet(path, false);
    }
    const decision = evaluateUnauthorized(authState);
    if (decision === "refresh" || sessionCredentials.get()) {
      const refreshed = await refreshSession();
      if (refreshed) {
        return apiGet(path, false);
      }
    }
    handleSessionExpired();
  }
  const body = await response.json().catch(() => null);
  if (
    allowRefreshRetry
    && shouldSwitchAuthRegion({
      status: response.status,
      isNetworkError: false,
      servedByRegion: body?.served_by_region,
      currentAuthRegion: authState.activeRegion,
    })
  ) {
    if (!response.ok) {
      await failoverAuthenticate();
    } else {
      void failoverAuthenticate();
    }
  }
  return { status: response.status, ok: response.ok, body };
}

function dispatch(action) {
  state = reduce(state, action);
  render();
}

function failureReason(error) {
  return error.name === "TimeoutError" ? "Request timed out" : error.message;
}

function transactionList(body) {
  if (Array.isArray(body)) {
    return body;
  }
  return body?.items ?? body?.transactions ?? [];
}

async function pollSecondary(generation) {
  const [account, history] = await Promise.allSettled([
    apiGet("/account"),
    apiGet(`/transactions?limit=${HISTORY_LIMIT}`),
  ]);
  if (generation !== pollGeneration || authState.phase !== "authenticated") {
    return;
  }
  if (account.status === "fulfilled" && account.value.ok && account.value.body) {
    dispatch({ type: "account-ok", account: account.value.body });
  }
  if (history.status === "fulfilled" && history.value.ok && history.value.body) {
    dispatch({ type: "history-ok", transactions: transactionList(history.value.body) });
  }
}

async function poll() {
  if (!canPoll(authState)) {
    if (authState.phase === "authenticated" && isTokenExpiring(authState.idToken, 0)) {
      const refreshed = await refreshSession();
      if (!refreshed) {
        handleSessionExpired();
        return;
      }
    } else {
      render();
      return;
    }
  }
  const generation = ++pollGeneration;
  const secondary = pollSecondary(generation);
  try {
    const started = performance.now();
    const { ok, status, body } = await apiGet("/stats");
    const roundTripMs = performance.now() - started;
    if (authState.phase !== "authenticated") {
      return;
    }
    if (!ok) {
      throw new Error(body?.error?.message ?? `HTTP ${status}`);
    }
    dispatch({ type: "stats-ok", stats: body, at: new Date().toISOString(), roundTripMs });
  } catch (error) {
    if (authState.phase === "authenticated") {
      dispatch({ type: "stats-failed", reason: failureReason(error) });
    }
  }
  await secondary;
  if (authState.phase === "authenticated") {
    schedulePoll();
  }
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (authState.phase === "authenticated") {
    pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
  }
}

async function verify() {
  if (authState.phase !== "authenticated") {
    return;
  }
  dispatch({ type: "consistency-started" });
  try {
    const { ok, status, body } = await apiGet("/consistency");
    if (!ok) {
      throw new Error(body?.error?.message ?? `HTTP ${status}`);
    }
    dispatch({ type: "consistency-ok", report: body, at: new Date().toISOString() });
  } catch (error) {
    dispatch({ type: "consistency-failed", reason: failureReason(error), at: new Date().toISOString() });
  }
}

async function onSignIn(event) {
  event.preventDefault();
  const usernameInput = $("login-username");
  const passwordInput = $("login-password");
  const username = usernameInput.value.trim();
  const password = passwordInput.value;

  if (!username || !password) {
    authState = reduceAuth(authState, {
      type: "auth-failed",
      error: "Invalid username or password",
    });
    passwordInput.value = "";
    render();
    (username ? passwordInput : usernameInput).focus();
    return;
  }

  authState = reduceAuth(authState, { type: "auth-started" });
  render();

  const target = getActiveCognitoTarget("primary");
  try {
    const payload = buildInitiateAuthPayload(username, password, target.clientId);
    const result = await cognitoInitiateAuth(payload, target);
    sessionCredentials.set(username, password);
    saveTokensToStorage({
      idToken: result.IdToken,
      accessToken: result.AccessToken,
      refreshToken: result.RefreshToken,
      region: target.region,
    });
    authState = reduceAuth(authState, {
      type: "auth-succeeded",
      idToken: result.IdToken,
      accessToken: result.AccessToken,
      refreshToken: result.RefreshToken,
      username,
      region: target.region,
    });
    passwordInput.value = "";
    scheduleRefreshCheck();
    render();
    await poll();
  } catch {
    sessionCredentials.clear();
    purgeAuthTokens(sessionStorage);
    authState = reduceAuth(authState, {
      type: "auth-failed",
      error: "Invalid username or password",
    });
    passwordInput.value = "";
    render();
    passwordInput.focus();
  }
}

function onSignOut() {
  stopTimers();
  pollGeneration += 1;
  sessionCredentials.clear();
  authState = purgeAuthTokens(sessionStorage);
  state = initialState;
  const passwordInput = $("login-password");
  if (passwordInput) {
    passwordInput.value = "";
  }
  render();
  $("login-username")?.focus();
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) {
    td.className = className;
  }
  return td;
}

function renderRegions(stats) {
  const items = regionRows(stats).map((row) => {
    const li = document.createElement("li");
    const head = document.createElement("div");
    head.className = "region-head";
    const name = document.createElement("span");
    name.textContent = row.region;
    const value = document.createElement("span");
    value.textContent = `${row.count} movements (${row.share}%)`;
    head.append(name, value);
    const meta = document.createElement("div");
    meta.className = "region-meta";
    meta.textContent = `Last movement: ${row.lastProcessedAt ? formatUtc(row.lastProcessedAt) : "never"}`;
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("div");
    fill.className = "bar-fill";
    fill.style.setProperty("--share", `${row.share}%`);
    bar.append(fill);
    li.append(head, bar, meta);
    return li;
  });
  $("region-list").replaceChildren(...items);
}

function renderHistory() {
  if (state.history === null) {
    return;
  }
  const rows = historyRows(state.history).map((row) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(row.time),
      cell(row.type),
      cell(row.amount, "num"),
      cell(row.balanceAfter, "num"),
      cell(row.region),
      cell(row.status),
    );
    return tr;
  });
  $("history-body").replaceChildren(...rows);
  $("empty-history").hidden = rows.length > 0;
}

function timelineText(event) {
  const what = event.kind === "role" ? "Database role" : "Serving region";
  return `${formatUtc(event.at)}: ${what} changed from ${event.from} to ${event.to}.`;
}

function renderTimeline() {
  const items = state.timeline.map((event) => {
    const li = document.createElement("li");
    li.textContent = timelineText(event);
    return li;
  });
  $("timeline").replaceChildren(...items);
  $("empty-timeline").hidden = items.length > 0;
}

function renderLatency() {
  const summary = summarizeLatencies(state.latencies);
  const ms = (value) => `${Math.round(value)} ms`;
  $("latency-text").textContent = summary.count === 0
    ? "No samples yet."
    : `Samples ${summary.count}: min ${ms(summary.min)}, median ${ms(summary.median)}, max ${ms(summary.max)}.`;
}

function renderStats() {
  const { stats, phase, freshChange, lastChange, lastRoleChange } = state;
  if (!stats) {
    return;
  }
  $("active-region").textContent = stats.served_by_region;
  $("database-role").textContent = stats.database_role;
  $("total-count").textContent = String(stats.total_count);
  if (state.account) {
    $("balance").textContent = formatAmount(state.account.balance);
  }
  for (const card of document.querySelectorAll(".card")) {
    card.classList.toggle("stale", phase === "unavailable");
  }
  $("region-card").classList.toggle("changed", freshChange && lastChange?.at === state.lastSuccessAt);
  $("role-card").classList.toggle("changed", freshChange && lastRoleChange?.at === state.lastSuccessAt);
  const banner = $("change-banner");
  const latest = [lastChange && { kind: "region", ...lastChange }, lastRoleChange && { kind: "role", ...lastRoleChange }]
    .filter(Boolean)
    .sort((a, b) => b.at.localeCompare(a.at))[0];
  banner.hidden = !latest;
  if (latest) {
    banner.textContent = timelineText(latest);
  }
  renderRegions(stats);
}

function renderAuth() {
  const isAuthenticated = authState.phase === "authenticated";
  const isSigningIn = authState.phase === "signing-in";
  const isExpired = authState.phase === "auth-expired";

  $("login-section").hidden = isAuthenticated;
  $("authenticated-view").hidden = !isAuthenticated;
  $("user-profile").hidden = !isAuthenticated;
  $("sign-in-button").disabled = isSigningIn;
  $("login-username").disabled = isSigningIn;
  $("login-password").disabled = isSigningIn;

  const authBanner = $("auth-banner");
  authBanner.hidden = !isExpired;
  authBanner.textContent = isExpired ? (authState.error ?? "Session expired. Please sign in again.") : "";

  if (isAuthenticated) {
    $("user-display").textContent = authState.username ?? "dashboard-viewer";
    $("login-error").hidden = true;
    $("login-error").textContent = "";
    return;
  }

  const loginError = $("login-error");
  loginError.hidden = !authState.error;
  loginError.textContent = authState.error ?? "";
}

function renderStatus() {
  const isAuthenticated = authState.phase === "authenticated";
  const view = unavailableView(state, new Date().toISOString());
  $("unavailable").hidden = !view.unavailable || !isAuthenticated;
  if (view.unavailable) {
    const reason = state.failureReason ? ` ${state.failureReason.replace(/\.$/, "")}.` : "";
    $("unavailable-text").textContent = `${view.text}${reason} Retrying every ${POLL_INTERVAL_MS / 1000} seconds.`;
  }
  $("last-update").textContent = state.lastSuccessAt ? formatUtc(state.lastSuccessAt) : "never";
  const announcement = !isAuthenticated
    ? (authState.phase === "signing-in" ? "Signing in." : "Sign in to start polling.")
    : statusAnnouncement(state);
  if (announcement !== lastAnnouncement) {
    $("status").textContent = announcement;
    lastAnnouncement = announcement;
  }
}

function renderConsistency() {
  const { phase, report, at, reason } = state.consistency;
  const result = $("verify-result");
  const rows = $("verify-rows");
  $("verify-button").disabled = phase === "checking";
  result.hidden = phase === "idle";
  rows.hidden = true;
  rows.replaceChildren();
  result.dataset.state = phase;
  if (phase === "checking") {
    result.textContent = "Checking the ledger.";
    return;
  }
  if (phase === "failed") {
    result.dataset.state = "inconsistent";
    result.textContent = `The check could not run: ${String(reason).replace(/\.$/, "")}.`;
    return;
  }
  if (phase !== "done") {
    return;
  }
  const view = consistencyView(report);
  result.dataset.state = view.state;
  const offender = view.firstOffender ? ` First offending movement: ${view.firstOffender}.` : "";
  const served = view.servedBy ? ` Answered by ${view.servedBy}.` : "";
  result.textContent = `${view.label}.${offender}${served} Checked at ${formatUtc(at)}.`;
  const nodes = view.rows.flatMap(([label, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    return [dt, dd];
  });
  rows.append(...nodes);
  rows.hidden = nodes.length === 0;
}

function render() {
  renderAuth();
  renderStatus();
  if (authState.phase === "authenticated") {
    renderStats();
    renderTimeline();
    renderLatency();
    renderHistory();
    renderConsistency();
  }
}

function bindControls() {
  $("login-form").addEventListener("submit", onSignIn);
  $("sign-out-button").addEventListener("click", onSignOut);
  $("verify-button").addEventListener("click", verify);
}

async function start() {
  config = await loadConfig();
  restoreTokensFromStorage();
  bindControls();
  render();
  if (canPoll(authState)) {
    scheduleRefreshCheck();
    await poll();
  }
}

start();
