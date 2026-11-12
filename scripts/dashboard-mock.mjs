#!/usr/bin/env node
// Local mock of the banking API and of the two static sites, for manual checks. No dependencies, localhost only.
// Usage: node scripts/dashboard-mock.mjs [dashboardPort] [bankPort] [apiPort]
//   dashboard (src/dashboard) on dashboardPort (default 8080), bank app (src/bank) on bankPort (default dashboardPort+1),
//   API on apiPort (default dashboardPort+2). Three different origins, as in production.
//   Both sites get a generated /config.json that points to the mock API; the repository files are not touched.
// Modes: 1 normal, 2 secondary (reader, read-only), 3 promoted (secondary is writer), 4 outage.
// Controls (GET, on any of the three ports):
//   /__mode?set=normal|secondary|promoted|outage   switch the serving region and database role
//   /__fail?next=N                                 the next N API requests answer 503 (0 clears)
//   /__inconsistent?on=1|0                         make /consistency report a tampered ledger
//   /__state                                       show the current state
// An Authorization: Bearer <token> header is mandatory on every API route except /health (any non-empty token works).
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "src");
const DASHBOARD_PORT = Number(process.argv[2] ?? 8080);
const BANK_PORT = Number(process.argv[3] ?? DASHBOARD_PORT + 1);
const API_PORT = Number(process.argv[4] ?? DASHBOARD_PORT + 2);
const DASHBOARD_ORIGIN = `http://localhost:${DASHBOARD_PORT}`;
const BANK_ORIGIN = `http://localhost:${BANK_PORT}`;
const API_ORIGIN = `http://localhost:${API_PORT}`;
const ALLOWED_ORIGINS = new Set([DASHBOARD_ORIGIN, BANK_ORIGIN]);

const PRIMARY = "us-east-2";
const SECONDARY = "us-west-2";
const MODES = { 1: "normal", 2: "secondary", 3: "promoted", 4: "outage" };
const TYPES = {
  ".html": "text/html",
  ".js": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
};
// Same directives as the edge template; connect-src names the mock API origin.
const CSP =
  "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
  `connect-src 'self' ${API_ORIGIN}; base-uri 'none'; form-action 'none'`;

const MAX_CENTS = 999_999_999_999; // 9,999,999,999.99
const MAX_LIMIT = 100;
const DEFAULT_LIMIT = 20;
const LATEST_COUNT = 10;
const CONSISTENCY_LIMIT = 50_000;
const MAX_KEY_LENGTH = 64;

let mode = "normal";
let failNext = 0;
let inconsistent = false;
let balanceCents = 0;
const ledger = []; // oldest first: { id, type, cents, balanceAfterCents, key, region, processedAt }
const byKey = new Map();

const regionOf = () => (mode === "normal" ? PRIMARY : SECONDARY);
const roleOf = () => (mode === "secondary" ? "reader" : "writer");
const money = (cents) => (cents / 100).toFixed(2);

function movementJson(row) {
  return {
    id: row.id,
    type: row.type,
    amount: money(row.cents),
    balance_after: money(row.balanceAfterCents),
    processed_at: row.processedAt,
    processed_region: row.region,
  };
}

function stats() {
  const counts = {};
  const last = {};
  for (const row of ledger) {
    counts[row.region] = (counts[row.region] ?? 0) + 1;
    last[row.region] = row.processedAt;
  }
  return {
    total_count: ledger.length,
    counts_by_region: counts,
    last_processed_at_by_region: last,
    latest_transactions: ledger.slice(-LATEST_COUNT).reverse().map(movementJson),
    served_by_region: regionOf(),
    database_role: roleOf(),
    generated_at: new Date().toISOString(),
  };
}

// Same checks as check_consistency in the backend, computed from the in-memory ledger.
function consistency() {
  const truncated = ledger.length > CONSISTENCY_LIMIT;
  const rows = ledger.slice(0, CONSISTENCY_LIMIT);
  const storedCents = inconsistent ? balanceCents + 1 : balanceCents;
  let deposited = 0;
  let withdrawn = 0;
  let previous = 0;
  let chainOk = true;
  let offender = null;
  let duplicates = 0;
  const seen = new Set();
  const counts = {};
  for (const row of rows) {
    if (row.type === "deposit") deposited += row.cents;
    else withdrawn += row.cents;
    const expected = row.type === "deposit" ? previous + row.cents : previous - row.cents;
    const broken = row.balanceAfterCents !== expected || row.balanceAfterCents < 0;
    const repeated = row.key !== null && seen.has(row.key);
    if (row.key !== null) seen.add(row.key);
    if (repeated) duplicates += 1;
    if (broken) chainOk = false;
    if (offender === null && (broken || repeated)) offender = row.id;
    previous = row.balanceAfterCents;
    counts[row.region] = (counts[row.region] ?? 0) + 1;
  }
  const replayed = deposited - withdrawn;
  const balancesMatch = truncated ? null : replayed === storedCents;
  if (balancesMatch === false && offender === null && rows.length > 0) offender = rows.at(-1).id;
  return {
    count: rows.length,
    total_deposited: money(deposited),
    total_withdrawn: money(withdrawn),
    replayed_balance: money(replayed),
    stored_balance: money(storedCents),
    balances_match: balancesMatch,
    chain_ok: chainOk,
    duplicate_keys: duplicates,
    counts_by_region: counts,
    consistent: truncated ? null : chainOk && balancesMatch === true && duplicates === 0,
    first_offender: offender,
    truncated,
    served_by_region: regionOf(),
  };
}

function corsHeaders(req) {
  const origin = req.headers.origin;
  const headers = { Vary: "Origin" };
  if (origin && ALLOWED_ORIGINS.has(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Expose-Headers"] = "X-Served-By-Region, Retry-After";
  }
  return headers;
}

function sendApi(req, res, status, body, extra = {}) {
  res.writeHead(status, {
    "Content-Type": "application/json",
    "X-Served-By-Region": regionOf(),
    ...corsHeaders(req),
    ...extra,
  });
  res.end(JSON.stringify(body));
}

const apiError = (req, res, status, code, message, extra = {}, fields = []) =>
  sendApi(req, res, status, { error: { code, message, fields } }, extra);

function sendPlain(res, status, body) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

async function readBody(req) {
  let raw = "";
  for await (const chunk of req) {
    raw += chunk;
  }
  return raw;
}

// Returns { cents } or { error } following the domain rules of NewTransaction.
function validateMovement(payload, key) {
  const fields = [];
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return { fields: [{ field: "body", message: "must be a JSON object" }] };
  }
  for (const name of Object.keys(payload)) {
    if (name !== "type" && name !== "amount") fields.push({ field: name, message: "unknown field" });
  }
  if (payload.type !== "deposit" && payload.type !== "withdrawal") {
    fields.push({ field: "type", message: "must be one of: deposit, withdrawal" });
  }
  let cents = 0;
  const amount = payload.amount;
  if (typeof amount !== "number") {
    fields.push({ field: "amount", message: "must be a number" });
  } else if (!Number.isFinite(amount) || amount <= 0) {
    fields.push({ field: "amount", message: "must be a positive number" });
  } else if (Math.abs(amount * 100 - Math.round(amount * 100)) > 1e-6) {
    fields.push({ field: "amount", message: "must have at most 2 decimals" });
  } else if (Math.round(amount * 100) > MAX_CENTS) {
    fields.push({ field: "amount", message: "must not exceed 9999999999.99" });
  } else {
    cents = Math.round(amount * 100);
  }
  if (key !== undefined && (key.length < 1 || key.length > MAX_KEY_LENGTH)) {
    fields.push({ field: "Idempotency-Key", message: `must be 1 to ${MAX_KEY_LENGTH} characters` });
  }
  return fields.length > 0 ? { fields } : { cents };
}

async function createTransaction(req, res) {
  const key = req.headers["idempotency-key"];
  let payload;
  try {
    payload = JSON.parse(await readBody(req));
  } catch {
    return apiError(req, res, 400, "validation_error", "The request is not valid.", {}, [
      { field: "body", message: "must be valid JSON" },
    ]);
  }
  const checked = validateMovement(payload, key);
  if (checked.fields) {
    return apiError(req, res, 400, "validation_error", "The request is not valid.", {}, checked.fields);
  }
  const known = key === undefined ? undefined : byKey.get(key);
  if (known) {
    return sendApi(req, res, 200, {
      transaction: movementJson(known),
      balance: money(known.balanceAfterCents),
      served_by_region: regionOf(),
      database_role: "writer",
      server_ms: 1,
    });
  }
  if (mode === "secondary") {
    return apiError(req, res, 503, "database_read_only", "The database in this region is read-only. Retry shortly.", {
      "Retry-After": "5",
    });
  }
  const started = performance.now();
  const { cents } = checked;
  if (payload.type === "withdrawal" && cents > balanceCents) {
    return sendApi(req, res, 422, {
      error: { code: "insufficient_funds", message: "The balance is not enough for this withdrawal.", balance: money(balanceCents) },
    });
  }
  const next = payload.type === "deposit" ? balanceCents + cents : balanceCents - cents;
  if (next > MAX_CENTS) {
    return apiError(req, res, 400, "validation_error", "The request is not valid.", {}, [
      { field: "amount", message: "balance would exceed the maximum" },
    ]);
  }
  const row = {
    id: randomUUID(),
    type: payload.type,
    cents,
    balanceAfterCents: next,
    key: key ?? null,
    region: regionOf(),
    processedAt: new Date().toISOString(),
  };
  balanceCents = next;
  ledger.push(row);
  if (key !== undefined) byKey.set(key, row);
  return sendApi(req, res, 201, {
    transaction: movementJson(row),
    balance: money(balanceCents),
    served_by_region: regionOf(),
    database_role: "writer",
    server_ms: Math.max(1, Math.round(performance.now() - started)),
  });
}

function listTransactions(req, res, url) {
  const raw = url.searchParams.get("limit");
  let limit = DEFAULT_LIMIT;
  if (raw !== null) {
    if (!/^[0-9]+$/.test(raw) || Number(raw) < 1 || Number(raw) > MAX_LIMIT) {
      return apiError(req, res, 400, "validation_error", "The request is not valid.", {}, [
        { field: "limit", message: `must be an integer between 1 and ${MAX_LIMIT}` },
      ]);
    }
    limit = Number(raw);
  }
  return sendApi(req, res, 200, { items: ledger.slice(-limit).reverse().map(movementJson) });
}

async function handleApi(req, res, url) {
  if (req.method === "OPTIONS") {
    // Preflight: answered before the token check, for the two sites only.
    const headers = { ...corsHeaders(req) };
    if (headers["Access-Control-Allow-Origin"]) {
      headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS";
      headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Idempotency-Key";
      headers["Access-Control-Max-Age"] = "600";
    }
    res.writeHead(204, headers);
    return res.end();
  }
  if (url.pathname !== "/health" && !/^Bearer\s+\S+/.test(req.headers["authorization"] ?? "")) {
    return apiError(req, res, 401, "unauthorized", "Missing bearer token.");
  }
  if (mode === "outage") {
    return apiError(req, res, 503, "service_unavailable", "Simulated regional outage.");
  }
  if (failNext > 0) {
    failNext -= 1;
    console.log(`injected 503 (${failNext} left)`);
    return apiError(req, res, 503, "service_unavailable", "Injected failure.");
  }
  const route = `${req.method} ${url.pathname}`;
  switch (route) {
    case "GET /health":
      return sendApi(req, res, 200, { status: "ok", region: regionOf(), database_role: roleOf() });
    case "GET /stats":
      return sendApi(req, res, 200, stats());
    case "GET /account":
      return sendApi(req, res, 200, {
        balance: money(balanceCents),
        movement_count: ledger.length,
        served_by_region: regionOf(),
        database_role: roleOf(),
      });
    case "GET /consistency":
      return sendApi(req, res, 200, consistency());
    case "GET /transactions":
      return listTransactions(req, res, url);
    case "POST /transactions":
      return createTransaction(req, res);
    default:
      return apiError(req, res, 404, "not_found", "Unknown route.");
  }
}

function state() {
  return { mode, fail_next: failNext, inconsistent, balance: money(balanceCents), movements: ledger.length };
}

function handleControl(res, url) {
  if (url.pathname === "/__mode") {
    const requested = url.searchParams.get("set");
    if (Object.values(MODES).includes(requested)) setMode(requested);
  } else if (url.pathname === "/__fail") {
    const next = Number(url.searchParams.get("next") ?? 0);
    if (Number.isInteger(next) && next >= 0) {
      failNext = next;
      console.log(`fail next: ${failNext}`);
    }
  } else if (url.pathname === "/__inconsistent") {
    inconsistent = ["1", "true", "on"].includes(url.searchParams.get("on") ?? "");
    console.log(`inconsistent report: ${inconsistent}`);
  } else if (url.pathname !== "/__state") {
    return sendPlain(res, 404, { error: "unknown control" });
  }
  return sendPlain(res, 200, state());
}

async function serveStatic(res, url, root) {
  const name = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname).replace(/^[/\\]+/, "");
  try {
    let body;
    if (name === "config.json") {
      body = JSON.stringify({ apiBaseUrl: API_ORIGIN });
    } else {
      const file = resolve(root, name);
      if (!file.startsWith(root + sep)) throw new Error("outside root");
      body = await readFile(file);
    }
    const ext = name.slice(name.lastIndexOf("."));
    res.writeHead(200, {
      "Content-Type": `${TYPES[ext] ?? "application/octet-stream"}; charset=utf-8`,
      "Content-Security-Policy": CSP,
      "Cache-Control": "no-store",
    });
    res.end(body);
  } catch {
    res.writeHead(404).end("Not found");
  }
}

function setMode(next) {
  mode = next;
  console.log(`mode: ${mode}`);
}

function makeServer(handler) {
  return createServer(async (req, res) => {
    const url = new URL(req.url, "http://localhost");
    try {
      if (url.pathname.startsWith("/__")) return handleControl(res, url);
      return await handler(req, res, url);
    } catch {
      if (!res.headersSent) sendApi(req, res, 500, { error: { code: "internal_error", message: "Mock failure.", fields: [] } });
      else res.end();
    }
  });
}

const dashboardRoot = resolve(SRC, "dashboard");
const bankRoot = resolve(SRC, "bank");
makeServer((req, res, url) => serveStatic(res, url, dashboardRoot)).listen(DASHBOARD_PORT, "127.0.0.1");
makeServer((req, res, url) => serveStatic(res, url, bankRoot)).listen(BANK_PORT, "127.0.0.1");
makeServer(handleApi).listen(API_PORT, "127.0.0.1", () => {
  console.log(`monitoring dashboard  ${DASHBOARD_ORIGIN}`);
  console.log(`banking app           ${BANK_ORIGIN}`);
  console.log(`mock API              ${API_ORIGIN}  (Authorization: Bearer required, any non-empty token)`);
  console.log("controls: /__mode?set=normal|secondary|promoted|outage, /__fail?next=N, /__inconsistent?on=1, /__state");
  console.log("keys: 1 normal, 2 secondary read-only, 3 promoted secondary, 4 outage, q quit");
});

if (process.stdin.isTTY) {
  process.stdin.setRawMode(true);
  process.stdin.on("data", (key) => {
    const char = String(key);
    if (char === "q" || char === "\u0003") {
      process.exit(0);
    }
    if (MODES[char]) {
      setMode(MODES[char]);
    }
  });
}
