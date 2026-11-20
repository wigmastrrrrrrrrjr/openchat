// openchat-auth — accounts and OAuth 2.0 for openchat.
// Runs on Cloudflare Workers with D1. No third-party runtime dependencies.

const SESSION_DAYS = 7;
const CODE_TTL = 300;
const ACCESS_TTL = 3600;
const REFRESH_TTL = 60 * 60 * 24 * 30;
const MAX_LOGIN_FAILURES = 8;
const FAILURE_WINDOW = 15 * 60;

const SCOPES = {
  "profile:read": "See your username and account id",
  "chat": "Use openchat thinking through the hosted model",
  "keys:read": "View your saved API keys (masked)",
  "keys:write": "Add and delete API keys and providers",
  "apps:manage": "Create and manage OAuth apps you own",
};

const SECURITY = {
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "referrer-policy": "same-origin",
  "strict-transport-security": "max-age=31536000; includeSubDomains",
  "content-security-policy": "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
};

export default {
  async fetch(request, env) {
    try {
      const response = await route(request, env);
      const headers = new Headers(response.headers);
      for (const [key, value] of Object.entries(SECURITY)) headers.set(key, value);
      return new Response(response.body, { status: response.status, headers });
    } catch (error) {
      console.error("openchat-auth error", error);
      return page(
        "Something went wrong",
        `<div class="card"><h1>Something went wrong</h1><p class="muted">${escapeHtml(
          String(error && error.message ? error.message : error)
        )}</p><p><a href="/">Back to openchat accounts</a></p></div>`,
        500
      );
    }
  },
};

async function route(request, env) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const method = request.method.toUpperCase();
  const session = await getSession(request, env);

  if (path === "/health") {
    return json({ ok: true, service: "openchat-auth", version: "1.0.0" });
  }
  if (path === "/favicon.ico") {
    return new Response(logoMarkSvg(), {
      headers: { "content-type": "image/svg+xml; charset=utf-8", "cache-control": "public, max-age=86400" },
    });
  }
  if (path === "/.well-known/oauth-authorization-server") {
    const origin = url.origin;
    return json({
      issuer: origin,
      authorization_endpoint: origin + "/authorize",
      token_endpoint: origin + "/token",
      userinfo_endpoint: origin + "/userinfo",
      revocation_endpoint: origin + "/revoke",
      scopes_supported: Object.keys(SCOPES),
      response_types_supported: ["code"],
      grant_types_supported: ["authorization_code", "refresh_token"],
      code_challenge_methods_supported: ["S256", "plain"],
      token_endpoint_auth_methods_supported: ["client_secret_post", "none"],
    });
  }

  if (path === "/") return landing(session, url);
  if (path === "/signup") return method === "POST" ? signup(request, env, url) : signupPage(url);
  if (path === "/login") return method === "POST" ? login(request, env, url) : loginPage(url);
  if (path === "/logout") return logout(request, env);

  if (path === "/account") {
    if (!session) return redirect("/login?next=%2Faccount");
    return method === "POST" ? accountAction(request, env, session, url) : accountPage(env, session, url);
  }
  if (path === "/apps") {
    if (!session) return redirect("/login?next=%2Fapps");
    return appsPage(env, session, url);
  }
  if (path === "/apps/new") {
    if (!session) return redirect("/login?next=%2Fapps%2Fnew");
    return method === "POST" ? appCreate(request, env, session, url) : appNewPage(session, url);
  }
  const appMatch = path.match(/^\/apps\/([A-Za-z0-9_-]{6,64})$/);
  if (appMatch) {
    if (!session) return redirect("/login?next=" + encodeURIComponent(path));
    return method === "POST"
      ? appAction(request, env, session, appMatch[1], url)
      : appDetail(env, session, appMatch[1], url);
  }

  if (path === "/keys") {
    if (!session) return redirect("/login?next=%2Fkeys");
    return method === "POST" ? keyCreate(request, env, session, url) : keysPage(env, session, url);
  }
  const keyMatch = path.match(/^\/keys\/([A-Za-z0-9_-]{6,64})\/delete$/);
  if (keyMatch && method === "POST") {
    if (!session) return redirect("/login?next=%2Fkeys");
    return keyDelete(request, env, session, keyMatch[1], url);
  }

  if (path === "/authorize") {
    return method === "POST" ? authorizeSubmit(request, env, session, url) : authorizePage(request, env, session, url);
  }
  if (path === "/token" && method === "POST") return tokenEndpoint(request, env);
  if (path === "/userinfo" && method === "GET") return userinfoEndpoint(request, env);
  if (path === "/revoke" && method === "POST") return revokeEndpoint(request, env);

  if (path === "/api/me") return apiMe(request, env);
  if (path === "/api/keys") return method === "POST" ? apiKeyCreate(request, env) : apiKeyList(request, env);
  const apiKeyMatch = path.match(/^\/api\/keys\/([A-Za-z0-9_-]{6,64})$/);
  if (apiKeyMatch && method === "DELETE") return apiKeyDelete(request, env, apiKeyMatch[1]);

  if (path === "/v1/chat" && method === "POST") return chatProxy(request, env);

  return page("Not found", `<div class="card"><h1>Not found</h1><p class="muted">That page does not exist.</p><p><a href="/">Back home</a></p></div>`, 404);
}

/* ------------------------------------------------------------------ */
/* crypto helpers                                                      */
/* ------------------------------------------------------------------ */

function now() {
  return Math.floor(Date.now() / 1000);
}

function randomToken(bytes = 32) {
  const raw = new Uint8Array(bytes);
  crypto.getRandomValues(raw);
  return base64url(raw);
}

function base64url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64url(value) {
  let text = String(value).replace(/-/g, "+").replace(/_/g, "/");
  while (text.length % 4) text += "=";
  const binary = atob(text);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(String(value)));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hashPassword(password, saltB64, iterations) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveBits"]
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: fromBase64url(saltB64), iterations, hash: "SHA-256" },
    key,
    256
  );
  return base64url(new Uint8Array(bits));
}

function timingSafeEqual(a, b) {
  const left = String(a || "");
  const right = String(b || "");
  if (left.length !== right.length) return false;
  let diff = 0;
  for (let i = 0; i < left.length; i += 1) diff |= left.charCodeAt(i) ^ right.charCodeAt(i);
  return diff === 0;
}

async function importVaultKey(env) {
  if (!env.VAULT_KEY) throw new Error("VAULT_KEY is not configured");
  const raw = fromBase64url(env.VAULT_KEY);
  if (raw.length !== 32) throw new Error("VAULT_KEY must decode to 32 bytes");
  return crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
}

async function encryptSecret(env, plaintext) {
  const key = await importVaultKey(env);
  const iv = new Uint8Array(12);
  crypto.getRandomValues(iv);
  const cipher = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    key,
    new TextEncoder().encode(String(plaintext))
  );
  return base64url(iv) + "." + base64url(new Uint8Array(cipher));
}

async function decryptSecret(env, payload) {
  const [ivPart, cipherPart] = String(payload).split(".");
  if (!ivPart || !cipherPart) throw new Error("bad secret payload");
  const key = await importVaultKey(env);
  const plain = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: fromBase64url(ivPart) },
    key,
    fromBase64url(cipherPart)
  );
  return new TextDecoder().decode(plain);
}

/* ------------------------------------------------------------------ */
/* request helpers                                                     */
/* ------------------------------------------------------------------ */

function parseCookies(header) {
  const out = {};
  for (const chunk of String(header || "").split(";")) {
    const index = chunk.indexOf("=");
    if (index < 0) continue;
    out[chunk.slice(0, index).trim()] = decodeURIComponent(chunk.slice(index + 1).trim());
  }
  return out;
}

async function getSession(request, env) {
  const raw = parseCookies(request.headers.get("cookie") || "").oc_session;
  if (!raw) return null;
  const row = await env.DB.prepare(
    "SELECT s.id, s.csrf, s.expires, s.user_id, u.username FROM oca_sessions s JOIN oca_users u ON u.id = s.user_id WHERE s.id = ?"
  )
    .bind(await sha256Hex(raw))
    .first();
  if (!row || row.expires < now()) return null;
  return row;
}

async function createSession(env, userId) {
  const raw = randomToken(32);
  const csrf = randomToken(24);
  const current = now();
  await env.DB.prepare(
    "INSERT INTO oca_sessions (id, user_id, csrf, expires, created) VALUES (?, ?, ?, ?, ?)"
  )
    .bind(await sha256Hex(raw), userId, csrf, current + SESSION_DAYS * 86400, current)
    .run();
  return { raw, csrf };
}

function sessionCookie(raw, maxAge = SESSION_DAYS * 86400) {
  return `oc_session=${raw}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAge}`;
}

function clearCookie() {
  return "oc_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0";
}

async function formData(request) {
  const type = request.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    const data = await request.json().catch(() => ({}));
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(data)) params.set(key, String(value));
    return params;
  }
  return new URLSearchParams(await request.text());
}

async function csrfOk(request, session, params) {
  if (!session) return false;
  const supplied = params.get("csrf") || "";
  return timingSafeEqual(supplied, session.csrf);
}

function safeNext(value) {
  const next = String(value || "");
  if (next.startsWith("/") && !next.startsWith("//")) return next;
  return "/account";
}

function redirect(location, headers = {}) {
  return new Response(null, { status: 302, headers: { location, ...headers } });
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function page(title, body, status = 200, headers = {}) {
  return new Response(
    `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)} · openchat</title>
<style>
:root { color-scheme: dark; --bg:#0b1117; --panel:#101923; --panel2:#0d141c; --line:#21303d; --text:#e6edf3; --muted:#8b9bab; --accent:#22d3ee; --accent2:#3b82f6; --ok:#34d399; --warn:#fbbf24; --err:#f87171; }
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; background:radial-gradient(1000px 500px at 15% -10%, #103242 0%, var(--bg) 55%); color:var(--text); font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
.wrap { max-width:720px; margin:0 auto; padding:34px 20px 70px; }
.brand { display:flex; align-items:center; gap:11px; margin-bottom:26px; }
.mark { width:30px; height:30px; border-radius:9px; display:grid; place-items:center; font-weight:800; font-size:17px; color:#04121a; background:linear-gradient(135deg,var(--accent),var(--accent2)); box-shadow:0 0 22px rgba(34,211,238,.25); }
.brand .name { font-weight:700; letter-spacing:.2px; } .brand .tag { color:var(--muted); font-size:12px; margin-left:2px; }
.card { background:linear-gradient(180deg,var(--panel),var(--panel2)); border:1px solid var(--line); border-radius:14px; padding:22px; margin:0 0 16px; }
h1 { font-size:22px; margin:0 0 6px; letter-spacing:-.2px; } h2 { font-size:15px; margin:22px 0 8px; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; }
p { margin:8px 0; } .muted { color:var(--muted); } .small { font-size:12.5px; }
label { display:block; font-size:12.5px; color:var(--muted); margin:12px 0 5px; }
input[type=text], input[type=password], input[type=url], textarea { width:100%; background:#0a1118; color:var(--text); border:1px solid var(--line); border-radius:9px; padding:10px 12px; font:inherit; }
input:focus, textarea:focus { outline:none; border-color:#2c6076; box-shadow:0 0 0 3px rgba(34,211,238,.10); }
textarea { min-height:84px; resize:vertical; }
button, .btn { display:inline-block; border:1px solid #2c6076; background:#123039; color:#c8f4fb; border-radius:9px; padding:9px 14px; font:inherit; font-weight:600; cursor:pointer; }
button:hover, .btn:hover { background:#15414d; text-decoration:none; }
button.primary { background:linear-gradient(135deg,#0891b2,#2563eb); border-color:#2563eb; color:#f2fbff; }
button.ghost { background:transparent; border-color:var(--line); color:var(--muted); }
button.danger { border-color:#7f2a2a; background:#2a1111; color:#ffc7c7; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.spread { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }
.pill { border:1px solid var(--line); color:var(--muted); border-radius:999px; padding:2px 9px; font-size:12px; }
.scope { border:1px solid var(--line); border-radius:10px; padding:10px 12px; margin:8px 0; }
.scope b { color:var(--text); } .scope span { color:var(--muted); }
.notice { border:1px solid #2c6076; background:#0d222b; border-radius:10px; padding:10px 12px; margin:12px 0; }
.error { border:1px solid #7f2a2a; background:#241012; color:#ffc7c7; border-radius:10px; padding:10px 12px; margin:12px 0; }
.ok { border:1px solid #1f5f4a; background:#0c211b; color:#b9f4de; border-radius:10px; padding:10px 12px; margin:12px 0; }
table { width:100%; border-collapse:collapse; } th, td { text-align:left; padding:9px 8px; border-bottom:1px solid var(--line); font-size:14px; vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.05em; }
code, .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; background:#0a1118; border:1px solid var(--line); border-radius:6px; padding:2px 6px; word-break:break-all; }
h1 + p { color:var(--muted); }
hr { border:0; border-top:1px solid var(--line); margin:18px 0; }
footer { color:var(--muted); font-size:12px; margin-top:26px; }
</style>
</head>
<body>
<div class="wrap">
  <a class="brand" href="/"><div class="mark">C</div><div><div class="name">openchat</div><div class="tag">accounts · oauth</div></div></a>
  ${body}
  <footer>openchat accounts · <a href="/">home</a> · OAuth 2.0</footer>
</div>
</body>
</html>`,
    { status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", ...headers } }
  );
}

function logoMarkSvg() {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#3b82f6"/></linearGradient></defs><rect width="64" height="64" rx="18" fill="url(#g)"/><path d="M44 22a15 15 0 1 0 0 20" fill="none" stroke="#04121a" stroke-width="7" stroke-linecap="round"/></svg>`;
}

function layout(session, title, body, message) {
  const banner = session
    ? `<div class="spread small muted" style="margin-bottom:14px"><div>signed in as <b style="color:var(--text)">@${escapeHtml(session.username)}</b></div><form method="post" action="/logout" style="margin:0"><button class="ghost small" type="submit">sign out</button></form></div>`
    : "";
  const flash = message ? `<div class="notice">${escapeHtml(message)}</div>` : "";
  return page(title, banner + flash + body);
}

function scopeLabel(scope) {
  return SCOPES[scope] || "Unknown permission";
}

/* ------------------------------------------------------------------ */
/* pages                                                               */
/* ------------------------------------------------------------------ */

function landing(session, url) {
  const msg = url.searchParams.get("msg");
  const actions = session
    ? `<div class="row"><a class="btn" href="/account">Your account</a><a class="btn" href="/apps">OAuth apps</a><a class="btn" href="/keys">API keys</a></div>`
    : `<div class="row"><a class="btn primary" href="/signup">Create an account</a><a class="btn" href="/login">Sign in</a></div>`;
  return layout(
    session,
    "openchat accounts",
    `<div class="card">
      <h1>One account for openchat.</h1>
      <p class="muted">Sign in, keep your provider keys in one encrypted place, and let apps ask for access with OAuth.</p>
      ${actions}
      <hr>
      <div class="row small muted"><span class="pill">OAuth 2.0</span><span class="pill">authorization code</span><span class="pill">PKCE</span><span class="pill">refresh tokens</span></div>
    </div>
    <div class="card small muted">
      <b style="color:var(--text)">For app developers</b>
      <p>Create an app to get a client id and secret, choose permissions, and register callback URLs. Users see exactly what your app asks for before they approve it.</p>
    </div>`,
    msg
  );
}

function authShell(title, heading, fields, bottom, error = "", notice = "") {
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${escapeHtml(title)}</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { min-height:100vh; background:#505050; font-family:Arial,Helvetica,sans-serif; display:flex; justify-content:center; align-items:center; color:#fff; padding:28px 0; }
.container { width:min(590px,90%); text-align:center; }
.brand { width:44px; height:44px; margin:0 auto 26px; border-radius:14px; display:grid; place-items:center; font-weight:800; font-size:24px; color:#04121a; background:linear-gradient(135deg,#00e5ff,#0a84ff); box-shadow:0 0 26px rgba(0,229,255,.35); }
h1 { font-size:80px; font-weight:400; margin-bottom:62px; letter-spacing:-2px; }
form { display:flex; flex-direction:column; gap:28px; }
input { width:100%; height:109px; border:none; outline:none; border-radius:34px; background:#929292; color:#333; font-size:21px; text-align:center; padding:20px; transition:background .2s, box-shadow .2s; font-family:inherit; }
input::placeholder { color:#444; opacity:1; }
input:focus { background:#a0a0a0; box-shadow:0 0 0 2px #00dfff; }
button { width:100%; height:76px; border:none; border-radius:34px; background:linear-gradient(135deg,#00e5ff,#0a84ff); color:#04121a; font:700 20px Arial,Helvetica,sans-serif; cursor:pointer; transition:filter .2s, transform .1s; }
button:hover { filter:brightness(1.08); }
button:active { transform:scale(.995); }
.error, .notice { border-radius:22px; padding:15px 18px; font-size:15px; margin-bottom:22px; text-align:center; }
.error { background:rgba(220,60,60,.22); border:1px solid rgba(255,140,140,.55); color:#ffe1e1; }
.notice { background:rgba(0,229,255,.14); border:1px solid rgba(0,229,255,.45); color:#d7fbff; }
.bottom-text { margin-top:27px; font-size:17px; }
.bottom-text a { color:#00e5ff; text-decoration:none; }
.bottom-text a:hover { text-decoration:underline; }
.small { margin-top:18px; font-size:13px; color:#cfcfcf; }
@media (max-width:600px) {
  h1 { font-size:64px; margin-bottom:48px; }
  input { height:80px; border-radius:27px; font-size:18px; }
  form { gap:22px; }
  button { height:64px; font-size:18px; }
}
</style>
</head>
<body>
<div class="container">
  <div class="brand">C</div>
  <h1>${escapeHtml(heading)}</h1>
  ${error ? `<div class="error">${escapeHtml(error)}</div>` : ""}
  ${notice ? `<div class="notice">${escapeHtml(notice)}</div>` : ""}
  <form method="post">
    ${fields}
  </form>
  <p class="bottom-text">${bottom}</p>
</div>
</body>
</html>`;
  return new Response(html, { status: 200, headers: { "content-type": "text/html; charset=utf-8" } });
}

function signupPage(url, error = "", values = {}) {
  const msg = url.searchParams.get("msg") || "";
  const fields = `
    <input type="hidden" name="next" value="${escapeHtml(values.next || "")}">
    <input type="text" name="username" autocomplete="username" aria-label="username" placeholder="username" value="${escapeHtml(values.username || "")}" maxlength="32" required>
    <input type="password" name="password" autocomplete="new-password" aria-label="password" placeholder="password" required>
    <input type="password" name="confirm" autocomplete="new-password" aria-label="confirm password" placeholder="confirm password" required>
    <button type="submit">create account</button>`;
  return authShell(
    "signup",
    "signup",
    fields,
    `already have one? <a href="/login${url.searchParams.get("next") ? "?next=" + encodeURIComponent(url.searchParams.get("next")) : ""}">go here</a>`,
    error,
    msg
  );
}

function loginPage(url, error = "", username = "") {
  const next = url.searchParams.get("next") || "";
  const msg = url.searchParams.get("msg") || "";
  const fields = `
    <input type="hidden" name="next" value="${escapeHtml(next)}">
    <input type="text" name="username" autocomplete="username" aria-label="username" placeholder="username" value="${escapeHtml(username)}" required>
    <input type="password" name="password" autocomplete="current-password" aria-label="password" placeholder="password" required>
    <button type="submit">login</button>`;
  return authShell(
    "login",
    "login",
    fields,
    `need an account? <a href="/signup${next ? "?next=" + encodeURIComponent(next) : ""}">sign up</a>`,
    error,
    msg
  );
}

async function accountPage(env, session, url) {
  const user = await env.DB.prepare("SELECT id, username, created FROM oca_users WHERE id = ?").bind(session.user_id).first();
  const apps = await env.DB.prepare("SELECT COUNT(*) AS n FROM oca_apps WHERE user_id = ?").bind(session.user_id).first();
  const keys = await env.DB.prepare("SELECT COUNT(*) AS n FROM oca_keys WHERE user_id = ?").bind(session.user_id).first();
  const msg = url.searchParams.get("msg") || "";
  return layout(
    session,
    "Account",
    `<div class="card">
      <h1>@${escapeHtml(user.username)}</h1>
      <p class="muted small">member since ${new Date(user.created * 1000).toISOString().slice(0, 10)} · id <code>${escapeHtml(user.id)}</code></p>
      <div class="row">
        <a class="btn" href="/apps">${apps.n} OAuth app${apps.n === 1 ? "" : "s"}</a>
        <a class="btn" href="/keys">${keys.n} API key${keys.n === 1 ? "" : "s"}</a>
      </div>
    </div>
    <div class="card">
      <h2 style="margin-top:0">Change password</h2>
      <form method="post" action="/account">
        <input type="hidden" name="csrf" value="${escapeHtml(session.csrf)}">
        <input type="hidden" name="action" value="password">
        <label for="current">current password</label>
        <input id="current" type="password" name="current" autocomplete="current-password" required>
        <label for="next">new password</label>
        <input id="next" type="password" name="next" autocomplete="new-password" required>
        <div class="row" style="margin-top:16px"><button class="primary" type="submit">Update password</button></div>
      </form>
    </div>`,
    msg
  );
}

async function appsPage(env, session, url) {
  const rows = await env.DB.prepare(
    "SELECT id, name, client_id, scopes, created FROM oca_apps WHERE user_id = ? ORDER BY created DESC"
  )
    .bind(session.user_id)
    .all();
  const apps = rows.results || [];
  const sidebar = apps.length
    ? apps
        .map(
          (app) =>
            `<a class="app" href="/apps/${escapeHtml(app.id)}"><b>${escapeHtml(app.name)}</b><span>${escapeHtml(app.client_id)}</span></a>`
        )
        .join("")
    : `<p class="empty">no auth apps yet..</p>`;
  const msg = url.searchParams.get("msg") || "";
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OAuth Apps</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { height:100vh; background:#505050; font-family:Arial,Helvetica,sans-serif; overflow:hidden; display:flex; }
.sidebar { width:53.7%; height:100vh; background:#929292; border-radius:0 30px 0 0; padding:98px 48px 40px; overflow-y:auto; }
.sidebar .empty { font-size:48px; font-weight:400; color:#626262; white-space:nowrap; }
.sidebar .app { display:block; text-decoration:none; padding:16px 0; border-bottom:1px solid rgba(0,0,0,.12); }
.sidebar .app b { display:block; font-size:30px; font-weight:400; color:#3e3e3e; }
.sidebar .app span { font:13px ui-monospace,SFMono-Regular,Menlo,monospace; color:#626262; }
.sidebar .app:hover b { color:#1d1d1d; }
.main { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:54px; padding:30px; }
h1 { font-size:48px; font-weight:400; color:#3e3e3e; }
.msg { color:#e8f9ff; background:rgba(0,229,255,.14); border:1px solid rgba(0,229,255,.4); border-radius:20px; padding:10px 18px; font-size:14px; }
.actions { display:flex; flex-direction:column; align-items:center; gap:18px; }
.btn { width:230px; height:82px; border:none; border-radius:26px; background:#929292; color:#626262; font-size:28px; font-weight:400; cursor:pointer; transition:background .2s; text-decoration:none; display:grid; place-items:center; }
.btn:hover { background:#a5a5a5; }
.btn.primary { background:#00dfff; color:#04121a; }
.btn.primary:hover { background:#57ecff; }
@media (max-width:700px) {
  body { flex-direction:column; overflow:auto; height:auto; }
  .sidebar { width:100%; height:45vh; min-height:250px; border-radius:0 0 30px 30px; padding:65px 28px 24px; }
  .sidebar .empty { font-size:32px; }
  .sidebar .app b { font-size:24px; }
  .main { min-height:50vh; gap:36px; }
  h1 { font-size:36px; }
  .btn { width:205px; height:72px; border-radius:22px; font-size:24px; }
}
</style>
</head>
<body>
  <aside class="sidebar">${sidebar}</aside>
  <main class="main">
    <h1>oauth apps</h1>
    ${msg ? `<div class="msg">${escapeHtml(msg)}</div>` : ""}
    <div class="actions">
      <a class="btn primary" href="/apps/new">add app</a>
      <a class="btn" href="/account">back</a>
    </div>
  </main>
</body>
</html>`;
  return new Response(html, { status: 200, headers: { "content-type": "text/html; charset=utf-8" } });
}

function appNewPage(session, url) {
  const msg = url.searchParams.get("msg") || "";
  const scopes = Object.entries(SCOPES)
    .map(
      ([scope, text]) =>
        `<label class="scope" style="display:flex;gap:9px;align-items:flex-start;margin:0 0 8px">
          <input type="checkbox" name="scopes" value="${escapeHtml(scope)}" style="width:auto;margin-top:4px">
          <span><b>${escapeHtml(scope)}</b><br><span>${escapeHtml(text)}</span></span>
        </label>`
    )
    .join("");
  return layout(
    session,
    "Add an app",
    `<div class="card">
      <div class="spread"><div><h1>Add an app</h1><p class="muted small">Register a client to get a client id, a secret, permissions, and callback URLs.</p></div><a class="btn ghost" href="/apps">All apps</a></div>
      ${msg ? `<div class="notice">${escapeHtml(msg)}</div>` : ""}
      <form method="post" action="/apps/new">
        <input type="hidden" name="csrf" value="${escapeHtml(session.csrf)}">
        <label for="name">app name</label>
        <input id="name" type="text" name="name" maxlength="60" placeholder="My app" required>
        <label for="description">description</label>
        <textarea id="description" name="description" maxlength="400" placeholder="What does this app do?"></textarea>
        <label for="redirects">callback URLs — one per line</label>
        <textarea id="redirects" name="redirects" placeholder="https://example.com/callback" required></textarea>
        <h2>Permissions</h2>
        ${scopes}
        <div class="row" style="margin-top:16px"><button class="primary" type="submit">Create app</button><a class="btn ghost" href="/apps">Cancel</a></div>
      </form>
    </div>`
  );
}

async function appDetail(env, session, id, url, extra = "", newSecret = null) {
  const app = await env.DB.prepare("SELECT * FROM oca_apps WHERE id = ? AND user_id = ?").bind(id, session.user_id).first();
  if (!app) return page("Not found", `<div class="card"><h1>App not found</h1><p><a href="/apps">Back to apps</a></p></div>`, 404);
  const redirects = JSON.parse(app.redirects || "[]");
  const scopes = JSON.parse(app.scopes || "[]");
  const secretBox = newSecret
    ? `<div class="ok"><b>Client secret (shown once)</b><p class="mono">${escapeHtml(newSecret)}</p><p class="small">Copy it now. It is stored only as a hash.</p></div>`
    : "";
  return layout(
    session,
    app.name,
    `<div class="card">
      <div class="spread"><div><h1>${escapeHtml(app.name)}</h1><p class="muted small">Created by @${escapeHtml(session.username)} · ${new Date(app.created * 1000).toISOString().slice(0, 10)}</p></div><a class="btn ghost" href="/apps">All apps</a></div>
      ${extra}
      ${secretBox}
      <h2>Client id</h2>
      <p class="mono">${escapeHtml(app.client_id)}</p>
      <h2>Permissions</h2>
      <p>${scopes.map((s) => `<span class="pill">${escapeHtml(s)}</span>`).join(" ")}</p>
      <h2>Callback URLs</h2>
      <p class="mono small">${redirects.map((r) => escapeHtml(r)).join("<br>")}</p>
      <hr>
      <h2>Edit settings</h2>
      <form method="post" action="/apps/${escapeHtml(app.id)}">
        <input type="hidden" name="csrf" value="${escapeHtml(session.csrf)}">
        <input type="hidden" name="action" value="update">
        <label for="name">app name</label>
        <input id="name" type="text" name="name" value="${escapeHtml(app.name)}" maxlength="60" required>
        <label for="description">description</label>
        <textarea id="description" name="description" maxlength="400">${escapeHtml(app.description || "")}</textarea>
        <label for="redirects">callback URLs — one per line</label>
        <textarea id="redirects" name="redirects" required>${escapeHtml(redirects.join("\n"))}</textarea>
        <h2>Permissions</h2>
        ${Object.entries(SCOPES)
          .map(
            ([scope, text]) => `<label class="scope" style="display:flex;gap:9px;align-items:flex-start;margin:0 0 8px">
              <input type="checkbox" name="scopes" value="${escapeHtml(scope)}" ${scopes.includes(scope) ? "checked" : ""} style="width:auto;margin-top:4px">
              <span><b>${escapeHtml(scope)}</b><br><span>${escapeHtml(text)}</span></span>
            </label>`
          )
          .join("")}
        <div class="row" style="margin-top:16px"><button class="primary" type="submit">Save changes</button></div>
      </form>
      <hr>
      <div class="row">
        <form method="post" action="/apps/${escapeHtml(app.id)}" onsubmit="return confirm('Rotate the client secret? Existing apps using it will break.')">
          <input type="hidden" name="csrf" value="${escapeHtml(session.csrf)}">
          <input type="hidden" name="action" value="rotate">
          <button type="submit">Rotate secret</button>
        </form>
        <form method="post" action="/apps/${escapeHtml(app.id)}" onsubmit="return confirm('Delete this app and revoke its tokens?')">
          <input type="hidden" name="csrf" value="${escapeHtml(session.csrf)}">
          <input type="hidden" name="action" value="delete">
          <button class="danger" type="submit">Delete app</button>
        </form>
      </div>
    </div>`
  );
}

async function keysPage(env, session, url) {
  const rows = await env.DB.prepare(
    "SELECT id, label, provider, base, secret, created FROM oca_keys WHERE user_id = ? ORDER BY created DESC"
  )
    .bind(session.user_id)
    .all();
  const items = [];
  for (const key of rows.results || []) {
    let masked = "unreadable";
    try {
      const plain = await decryptSecret(env, key.secret);
      masked = plain.length > 8 ? plain.slice(0, 4) + "…" + plain.slice(-4) : "set";
    } catch (_) {
      masked = "unreadable";
    }
    items.push({ ...key, masked });
  }
  const list = items
    .map(
      (key) => `<tr>
        <td><b>${escapeHtml(key.label)}</b><div class="muted small mono">${escapeHtml(key.masked)}</div></td>
        <td class="small muted">${escapeHtml(key.provider)}${key.base ? `<div class="mono small">${escapeHtml(key.base)}</div>` : ""}</td>
        <td class="small muted">${new Date(key.created * 1000).toISOString().slice(0, 10)}</td>
        <td><form method="post" action="/keys/${escapeHtml(key.id)}/delete" onsubmit="return confirm('Delete this key?')"><input type="hidden" name="csrf" value="${escapeHtml(session.csrf)}"><button class="danger small" type="submit">delete</button></form></td>
      </tr>`
    )
    .join("");
  return layout(
    session,
    "API keys",
    `<div class="card">
      <h1>API keys and providers</h1>
      <p class="muted small">Stored encrypted with AES-GCM. Only the last characters are shown again. Apps can request access to these with OAuth scopes.</p>
      ${
        list
          ? `<table><thead><tr><th>label</th><th>provider</th><th>added</th><th></th></tr></thead><tbody>${list}</tbody></table>`
          : `<p class="muted">No keys yet.</p>`
      }
    </div>
    <div class="card">
      <h2 style="margin-top:0">Add a provider key</h2>
      <form method="post" action="/keys">
        <input type="hidden" name="csrf" value="${escapeHtml(session.csrf)}">
        <label for="label">label</label>
        <input id="label" type="text" name="label" maxlength="60" placeholder="personal novita" required>
        <label for="provider">provider</label>
        <input id="provider" type="text" name="provider" maxlength="40" placeholder="novita" required>
        <label for="base">base URL (optional)</label>
        <input id="base" type="url" name="base" placeholder="https://api.example.com/v1">
        <label for="key">API key</label>
        <input id="key" type="password" name="key" autocomplete="off" required>
        <div class="row" style="margin-top:16px"><button class="primary" type="submit">Save key</button></div>
      </form>
    </div>`,
    url.searchParams.get("msg") || ""
  );
}

/* ------------------------------------------------------------------ */
/* accounts                                                            */
/* ------------------------------------------------------------------ */

async function signup(request, env, url) {
  const params = await formData(request);
  const username = String(params.get("username") || "").trim();
  const password = String(params.get("password") || "");
  const confirm = String(params.get("confirm") || "");
  const next = safeNext(params.get("next") || url.searchParams.get("next"));
  const problem = (message) => signupPage(url, message, { username, next });

  if (!/^[A-Za-z0-9_-]{3,32}$/.test(username)) return problem("Username must be 3-32 letters, numbers, _ or -.");
  if (password.length < 8) return problem("Password must be at least 8 characters.");
  if (password !== confirm) return problem("Passwords do not match.");

  const norm = username.toLowerCase();
  const existing = await env.DB.prepare("SELECT id FROM oca_users WHERE username_norm = ?").bind(norm).first();
  if (existing) return problem("That username is taken.");

  const salt = randomToken(16);
  const iterations = 100000;
  const hash = await hashPassword(password, salt, iterations);
  const id = "usr_" + randomToken(12);
  const current = now();
  await env.DB.prepare(
    "INSERT INTO oca_users (id, username, username_norm, pass_hash, pass_salt, pass_iter, created) VALUES (?, ?, ?, ?, ?, ?, ?)"
  )
    .bind(id, username, norm, hash, salt, iterations, current)
    .run();
  const session = await createSession(env, id);
  await cleanup(env);
  return redirect(next, { "set-cookie": sessionCookie(session.raw) });
}

async function login(request, env, url) {
  const params = await formData(request);
  const username = String(params.get("username") || "").trim();
  const password = String(params.get("password") || "");
  const next = safeNext(params.get("next") || url.searchParams.get("next"));
  const norm = username.toLowerCase();
  const current = now();

  const failures = await env.DB.prepare(
    "SELECT COUNT(*) AS n FROM oca_attempts WHERE username_norm = ? AND ts > ?"
  )
    .bind(norm, current - FAILURE_WINDOW)
    .first();
  if ((failures.n || 0) >= MAX_LOGIN_FAILURES) {
    return loginPage(url, "Too many attempts. Wait 15 minutes and try again.", username);
  }

  const user = await env.DB.prepare("SELECT * FROM oca_users WHERE username_norm = ?").bind(norm).first();
  const failed = async () => {
    await env.DB.prepare("INSERT INTO oca_attempts (username_norm, ts) VALUES (?, ?)").bind(norm, current).run();
    return loginPage(url, "Wrong username or password.", username);
  };
  if (!user) return failed();
  const hash = await hashPassword(password, user.pass_salt, user.pass_iter);
  if (!timingSafeEqual(hash, user.pass_hash)) return failed();

  await env.DB.prepare("DELETE FROM oca_attempts WHERE username_norm = ?").bind(norm).run();
  const session = await createSession(env, user.id);
  await cleanup(env);
  return redirect(next, { "set-cookie": sessionCookie(session.raw) });
}

async function logout(request, env) {
  if (request.method !== "POST") return redirect("/");
  const raw = parseCookies(request.headers.get("cookie") || "").oc_session;
  if (raw) {
    await env.DB.prepare("DELETE FROM oca_sessions WHERE id = ?").bind(await sha256Hex(raw)).run();
  }
  return redirect("/", { "set-cookie": clearCookie() });
}

async function accountAction(request, env, session, url) {
  const params = await formData(request);
  if (!(await csrfOk(request, session, params))) return page("Bad request", `<div class="card"><h1>Bad request</h1><p>Reload and try again.</p></div>`, 400);
  if (params.get("action") !== "password") return redirect("/account");
  const currentPassword = String(params.get("current") || "");
  const nextPassword = String(params.get("next") || "");
  if (nextPassword.length < 8) return redirect("/account?msg=" + encodeURIComponent("New password must be at least 8 characters."));
  const user = await env.DB.prepare("SELECT * FROM oca_users WHERE id = ?").bind(session.user_id).first();
  const hash = await hashPassword(currentPassword, user.pass_salt, user.pass_iter);
  if (!timingSafeEqual(hash, user.pass_hash)) return redirect("/account?msg=" + encodeURIComponent("Current password is wrong."));
  const salt = randomToken(16);
  const iterations = 100000;
  const newHash = await hashPassword(nextPassword, salt, iterations);
  await env.DB.prepare("UPDATE oca_users SET pass_hash = ?, pass_salt = ?, pass_iter = ? WHERE id = ?")
    .bind(newHash, salt, iterations, user.id)
    .run();
  await env.DB.prepare("DELETE FROM oca_sessions WHERE user_id = ? AND id <> ?").bind(user.id, session.id).run();
  return redirect("/account?msg=" + encodeURIComponent("Password updated."));
}

async function cleanup(env) {
  const current = now();
  await env.DB.batch([
    env.DB.prepare("DELETE FROM oca_sessions WHERE expires < ?").bind(current),
    env.DB.prepare("DELETE FROM oca_codes WHERE expires < ?").bind(current),
    env.DB.prepare("DELETE FROM oca_tokens WHERE expires < ?").bind(current),
    env.DB.prepare("DELETE FROM oca_attempts WHERE ts < ?").bind(current - 86400),
  ]);
}

/* ------------------------------------------------------------------ */
/* apps                                                                */
/* ------------------------------------------------------------------ */

function parseRedirects(raw) {
  return String(raw || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function validRedirect(uri) {
  try {
    const url = new URL(uri);
    if (!["http:", "https:"].includes(url.protocol)) return false;
    if (url.hash) return false;
    return true;
  } catch (_) {
    return false;
  }
}

async function appCreate(request, env, session, url) {
  const params = await formData(request);
  if (!(await csrfOk(request, session, params))) return page("Bad request", `<div class="card"><h1>Bad request</h1></div>`, 400);
  const name = String(params.get("name") || "").trim();
  const description = String(params.get("description") || "").trim().slice(0, 400);
  const redirects = parseRedirects(params.get("redirects"));
  const scopes = params.getAll("scopes").filter((scope) => scope in SCOPES);

  const error = (message) =>
    layout(session, "Add an app", `<div class="card"><h1>Add an app</h1><div class="error">${escapeHtml(message)}</div><p><a href="/apps/new">Try again</a></p></div>`);
  if (!name) return error("Give the app a name.");
  if (!redirects.length) return error("Add at least one callback URL.");
  if (!redirects.every(validRedirect)) return error("Every callback URL must be an absolute http(s) URL without a fragment.");
  if (!scopes.length) return error("Choose at least one permission.");

  const id = "app_" + randomToken(10);
  const clientId = "oc_" + randomToken(18);
  const clientSecret = "ocs_" + randomToken(32);
  const current = now();
  await env.DB.prepare(
    "INSERT INTO oca_apps (id, user_id, name, description, client_id, client_secret_hash, redirects, scopes, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
  )
    .bind(id, session.user_id, name, description, clientId, await sha256Hex(clientSecret), JSON.stringify(redirects), JSON.stringify(scopes), current)
    .run();
  const html = await appDetail(env, session, id, url, "", clientSecret);
  return html;
}

async function appAction(request, env, session, id, url) {
  const params = await formData(request);
  if (!(await csrfOk(request, session, params))) return page("Bad request", `<div class="card"><h1>Bad request</h1></div>`, 400);
  const app = await env.DB.prepare("SELECT * FROM oca_apps WHERE id = ? AND user_id = ?").bind(id, session.user_id).first();
  if (!app) return page("Not found", `<div class="card"><h1>App not found</h1></div>`, 404);
  const action = params.get("action");

  if (action === "delete") {
    await env.DB.batch([
      env.DB.prepare("DELETE FROM oca_tokens WHERE app_id = ?").bind(id),
      env.DB.prepare("DELETE FROM oca_codes WHERE app_id = ?").bind(id),
      env.DB.prepare("DELETE FROM oca_apps WHERE id = ?").bind(id),
    ]);
    return redirect("/apps?msg=" + encodeURIComponent("App deleted."));
  }

  if (action === "rotate") {
    const secret = "ocs_" + randomToken(32);
    await env.DB.prepare("UPDATE oca_apps SET client_secret_hash = ? WHERE id = ?").bind(await sha256Hex(secret), id).run();
    return appDetail(env, session, id, url, "", secret);
  }

  if (action === "update") {
    const name = String(params.get("name") || "").trim();
    const description = String(params.get("description") || "").trim().slice(0, 400);
    const redirects = parseRedirects(params.get("redirects"));
    const scopes = params.getAll("scopes").filter((scope) => scope in SCOPES);
    const fail = (message) =>
      appDetail(env, session, id, url, `<div class="error">${escapeHtml(message)}</div>`);
    if (!name) return fail("Give the app a name.");
    if (!redirects.length || !redirects.every(validRedirect)) return fail("Callback URLs must be absolute http(s) URLs without fragments.");
    if (!scopes.length) return fail("Keep at least one permission.");
    await env.DB.prepare("UPDATE oca_apps SET name = ?, description = ?, redirects = ?, scopes = ? WHERE id = ?")
      .bind(name, description, JSON.stringify(redirects), JSON.stringify(scopes), id)
      .run();
    return redirect(`/apps/${id}?msg=` + encodeURIComponent("Saved."));
  }
  return redirect(`/apps/${id}`);
}

/* ------------------------------------------------------------------ */
/* keys                                                                */
/* ------------------------------------------------------------------ */

async function keyCreate(request, env, session, url) {
  const params = await formData(request);
  if (!(await csrfOk(request, session, params))) return page("Bad request", `<div class="card"><h1>Bad request</h1></div>`, 400);
  const label = String(params.get("label") || "").trim().slice(0, 60);
  const provider = String(params.get("provider") || "").trim().slice(0, 40);
  const base = String(params.get("base") || "").trim().slice(0, 300);
  const secret = String(params.get("key") || "").trim();
  if (!label || !provider || !secret) return redirect("/keys?msg=" + encodeURIComponent("Label, provider and key are required."));
  const encrypted = await encryptSecret(env, secret);
  await env.DB.prepare("INSERT INTO oca_keys (id, user_id, label, provider, base, secret, created) VALUES (?, ?, ?, ?, ?, ?, ?)")
    .bind("key_" + randomToken(10), session.user_id, label, provider, base, encrypted, now())
    .run();
  return redirect("/keys?msg=" + encodeURIComponent("Key saved."));
}

async function keyDelete(request, env, session, id, url) {
  const params = await formData(request);
  if (!(await csrfOk(request, session, params))) return page("Bad request", `<div class="card"><h1>Bad request</h1></div>`, 400);
  await env.DB.prepare("DELETE FROM oca_keys WHERE id = ? AND user_id = ?").bind(id, session.user_id).run();
  return redirect("/keys?msg=" + encodeURIComponent("Key deleted."));
}

function maskKey(plain) {
  if (!plain) return "set";
  return plain.length > 8 ? plain.slice(0, 4) + "…" + plain.slice(-4) : "set";
}

async function apiMe(request, env) {
  const token = await bearer(request, env, "profile:read");
  if (!token) return json({ error: "invalid_token" }, 401);
  const user = await env.DB.prepare("SELECT id, username, created FROM oca_users WHERE id = ?").bind(token.user_id).first();
  return json({ id: user.id, username: user.username, scope: token.scope, created: user.created });
}

async function apiKeyList(request, env) {
  const token = await bearer(request, env, "keys:read");
  if (!token) return json({ error: "insufficient_scope" }, 403);
  const rows = await env.DB.prepare("SELECT id, label, provider, base, secret, created FROM oca_keys WHERE user_id = ? ORDER BY created DESC")
    .bind(token.user_id)
    .all();
  const items = [];
  for (const key of rows.results || []) {
    let masked = "unreadable";
    try {
      masked = maskKey(await decryptSecret(env, key.secret));
    } catch (_) {}
    items.push({ id: key.id, label: key.label, provider: key.provider, base: key.base, masked, created: key.created });
  }
  return json({ keys: items });
}

async function apiKeyCreate(request, env) {
  const token = await bearer(request, env, "keys:write");
  if (!token) return json({ error: "insufficient_scope" }, 403);
  const data = await request.json().catch(() => null);
  if (!data) return json({ error: "invalid_request", error_description: "JSON body required" }, 400);
  const label = String(data.label || "").trim().slice(0, 60);
  const provider = String(data.provider || "").trim().slice(0, 40);
  const base = String(data.base || "").trim().slice(0, 300);
  const secret = String(data.key || data.api_key || "").trim();
  if (!label || !provider || !secret) return json({ error: "invalid_request", error_description: "label, provider and key are required" }, 400);
  const id = "key_" + randomToken(10);
  await env.DB.prepare("INSERT INTO oca_keys (id, user_id, label, provider, base, secret, created) VALUES (?, ?, ?, ?, ?, ?, ?)")
    .bind(id, token.user_id, label, provider, base, await encryptSecret(env, secret), now())
    .run();
  return json({ id, label, provider, base, masked: maskKey(secret) }, 201);
}

async function apiKeyDelete(request, env, id) {
  const token = await bearer(request, env, "keys:write");
  if (!token) return json({ error: "insufficient_scope" }, 403);
  const result = await env.DB.prepare("DELETE FROM oca_keys WHERE id = ? AND user_id = ?").bind(id, token.user_id).run();
  if (!result.meta || result.meta.changes === 0) return json({ error: "not_found" }, 404);
  return json({ ok: true });
}

/* ------------------------------------------------------------------ */
/* oauth authorize                                                     */
/* ------------------------------------------------------------------ */

function loopback(uri) {
  try {
    const url = new URL(uri);
    return url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]", "::1"].includes(url.hostname);
  } catch (_) {
    return false;
  }
}

function redirectMatches(registered, requested) {
  if (registered === requested) return true;
  try {
    const a = new URL(registered);
    const b = new URL(requested);
    if (loopback(registered) && loopback(requested)) {
      return a.protocol === b.protocol && a.hostname === b.hostname && a.pathname === b.pathname && !b.search && !b.hash;
    }
  } catch (_) {
    return false;
  }
  return false;
}

function requestedScopes(params, app) {
  const allowed = JSON.parse(app.scopes || "[]");
  const raw = String(params.get("scope") || "").trim();
  const requested = raw ? raw.split(/\s+/) : allowed;
  const valid = requested.filter((scope) => scope in SCOPES);
  const unallowed = valid.filter((scope) => !allowed.includes(scope));
  return { scopes: valid, unallowed };
}

async function authorizePage(request, env, session, url) {
  const params = url.searchParams;
  const clientId = params.get("client_id") || "";
  const redirectUri = params.get("redirect_uri") || "";
  const responseType = params.get("response_type") || "";
  const state = params.get("state") || "";
  const app = await env.DB.prepare("SELECT * FROM oca_apps WHERE client_id = ?").bind(clientId).first();

  if (!app) return page("Unknown app", `<div class="card"><h1>Unknown app</h1><p class="muted">No OAuth app matches this client id.</p></div>`, 400);
  const redirects = JSON.parse(app.redirects || "[]");
  if (!redirects.some((registered) => redirectMatches(registered, redirectUri))) {
    return page("Bad redirect", `<div class="card"><h1>Bad redirect URL</h1><p class="muted">The callback URL is not registered for this app.</p></div>`, 400);
  }
  const { scopes, unallowed } = requestedScopes(params, app);
  if (!scopes.length || unallowed.length) {
    return page("Bad scope", `<div class="card"><h1>Bad permissions</h1><p class="muted">Requested: ${escapeHtml(unallowed.join(" ") || "none")}</p></div>`, 400);
  }
  if (responseType !== "code") {
    const target = new URL(redirectUri);
    target.searchParams.set("error", "unsupported_response_type");
    if (state) target.searchParams.set("state", state);
    return redirect(target.toString());
  }
  if (!session) {
    return redirect("/login?next=" + encodeURIComponent(url.pathname + url.search));
  }

  const owner = app.user_id
    ? (await env.DB.prepare("SELECT username FROM oca_users WHERE id = ?").bind(app.user_id).first())?.username || "unknown"
    : "openchat";
  const scopeHtml = scopes
    .map(
      (scope) =>
        `<div class="scope"><b>${escapeHtml(scopeLabel(scope))}</b><br><span class="small">${escapeHtml(scope)} — access to ${
          scope === "keys:write" ? "add a provider, add API keys and delete API keys" : escapeHtml(scopeLabel(scope).toLowerCase())
        }</span></div>`
    )
    .join("");
  const hidden = ["client_id", "redirect_uri", "response_type", "scope", "state", "code_challenge", "code_challenge_method"]
    .filter((name) => params.get(name))
    .map((name) => `<input type="hidden" name="${name}" value="${escapeHtml(params.get(name))}">`)
    .join("");
  return layout(
    session,
    "Authorize " + app.name,
    `<div class="card">
      <div class="mark" style="margin-bottom:14px">C</div>
      <h1>Are you sure you want to authenticate “${escapeHtml(app.name)}”?</h1>
      <p class="muted">This app was created by <b style="color:var(--text)">@${escapeHtml(owner)}</b>. If you approve, it will have access to:</p>
      ${scopeHtml}
      <p class="muted small">You are signed in as <b style="color:var(--text)">@${escapeHtml(session.username)}</b>. Approving creates a one-time code that expires in 5 minutes.</p>
      <form method="post" action="/authorize">
        <input type="hidden" name="csrf" value="${escapeHtml(session.csrf)}">
        ${hidden}
        <div class="row" style="margin-top:16px">
          <button class="primary" type="submit" name="decision" value="approve">Yes, authenticate</button>
          <button class="danger" type="submit" name="decision" value="deny">No, go back</button>
        </div>
      </form>
    </div>`
  );
}

async function authorizeSubmit(request, env, session, url) {
  const params = await formData(request);
  if (!session) return redirect("/login?next=" + encodeURIComponent("/authorize"));
  if (!(await csrfOk(request, session, params))) return page("Bad request", `<div class="card"><h1>Bad request</h1><p>Reload and approve again.</p></div>`, 400);

  const clientId = String(params.get("client_id") || "");
  const redirectUri = String(params.get("redirect_uri") || "");
  const state = String(params.get("state") || "");
  const app = await env.DB.prepare("SELECT * FROM oca_apps WHERE client_id = ?").bind(clientId).first();
  const errorPage = (message) => page("Bad request", `<div class="card"><h1>Bad request</h1><p>${escapeHtml(message)}</p></div>`, 400);
  if (!app) return errorPage("Unknown app.");
  if (!JSON.parse(app.redirects || "[]").some((registered) => redirectMatches(registered, redirectUri))) {
    return errorPage("Callback URL is not registered.");
  }
  const { scopes, unallowed } = requestedScopes(params, app);
  if (!scopes.length || unallowed.length) return errorPage("Invalid permissions.");

  const target = new URL(redirectUri);
  if (state) target.searchParams.set("state", state);

  if (String(params.get("decision")) !== "approve") {
    target.searchParams.set("error", "access_denied");
    target.searchParams.set("error_description", "The user declined the request.");
    return redirect(target.toString());
  }

  const code = "oc_code_" + randomToken(48);
  const challenge = String(params.get("code_challenge") || "");
  const method = String(params.get("code_challenge_method") || (challenge ? "plain" : ""));
  const current = now();
  await env.DB.prepare(
    "INSERT INTO oca_codes (hash, app_id, user_id, client_id, redirect_uri, scope, challenge, method, expires, used) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)"
  )
    .bind(await sha256Hex(code), app.id, session.user_id, clientId, redirectUri, scopes.join(" "), challenge, method, current + CODE_TTL)
    .run();
  target.searchParams.set("code", code);
  return redirect(target.toString());
}

/* ------------------------------------------------------------------ */
/* oauth token / userinfo / revoke                                     */
/* ------------------------------------------------------------------ */

function verifyPkce(verifier, challenge, method) {
  if (!challenge) return true;
  if (!verifier) return false;
  if (method === "S256") {
    const digest = crypto.subtle
      .digest("SHA-256", new TextEncoder().encode(verifier))
      .then((bits) => base64url(new Uint8Array(bits)));
    return digest;
  }
  return Promise.resolve(verifier === challenge);
}

async function issueTokens(env, app, userId, scope) {
  const access = "oc_at_" + randomToken(32);
  const refresh = "oc_rt_" + randomToken(32);
  const current = now();
  const accessHash = await sha256Hex(access);
  const refreshHash = await sha256Hex(refresh);
  await env.DB.batch([
    env.DB.prepare("INSERT INTO oca_tokens (hash, kind, user_id, app_id, scope, expires, created) VALUES (?, 'access', ?, ?, ?, ?, ?)").bind(
      accessHash, userId, app.id, scope, current + ACCESS_TTL, current
    ),
    env.DB.prepare("INSERT INTO oca_tokens (hash, kind, user_id, app_id, scope, expires, created) VALUES (?, 'refresh', ?, ?, ?, ?, ?)").bind(
      refreshHash, userId, app.id, scope, current + REFRESH_TTL, current
    ),
  ]);
  return {
    access_token: access,
    token_type: "Bearer",
    expires_in: ACCESS_TTL,
    refresh_token: refresh,
    scope,
  };
}

async function tokenEndpoint(request, env) {
  const params = await formData(request);
  const grant = String(params.get("grant_type") || "");
  let clientId = String(params.get("client_id") || "");
  const app = await env.DB.prepare("SELECT * FROM oca_apps WHERE client_id = ?").bind(clientId).first();
  if (!app) return json({ error: "invalid_client" }, 401);

  let clientSecret = String(params.get("client_secret") || "");
  if (!clientSecret) {
    const header = request.headers.get("authorization") || "";
    const basic = header.match(/^Basic\s+(.+)$/i);
    if (basic) {
      const decoded = atob(basic[1]);
      const index = decoded.indexOf(":");
      if (index >= 0) {
        if (!clientId) clientId = decoded.slice(0, index);
        clientSecret = decoded.slice(index + 1);
      }
    }
  }
  if (app.client_secret_hash) {
    if (!clientSecret) return json({ error: "invalid_client", error_description: "client_secret required" }, 401);
    if (!timingSafeEqual(await sha256Hex(clientSecret), app.client_secret_hash)) {
      return json({ error: "invalid_client" }, 401);
    }
  }

  if (grant === "authorization_code") {
    const code = String(params.get("code") || "");
    const redirectUri = String(params.get("redirect_uri") || "");
    const verifier = String(params.get("code_verifier") || "");
    if (!code) return json({ error: "invalid_request", error_description: "code is required" }, 400);
    const row = await env.DB.prepare("SELECT * FROM oca_codes WHERE hash = ?").bind(await sha256Hex(code)).first();
    if (!row || row.used || row.expires < now()) return json({ error: "invalid_grant" }, 400);
    if (row.client_id !== clientId || row.app_id !== app.id) return json({ error: "invalid_grant" }, 400);
    if (!redirectMatches(row.redirect_uri, redirectUri)) return json({ error: "invalid_grant", error_description: "redirect_uri mismatch" }, 400);
    const pkce = await verifyPkce(verifier, row.challenge, row.method);
    if (!pkce) return json({ error: "invalid_grant", error_description: "PKCE check failed" }, 400);
    await env.DB.prepare("UPDATE oca_codes SET used = 1 WHERE hash = ?").bind(row.hash).run();
    return json(await issueTokens(env, app, row.user_id, row.scope));
  }

  if (grant === "refresh_token") {
    const token = String(params.get("refresh_token") || "");
    if (!token) return json({ error: "invalid_request", error_description: "refresh_token is required" }, 400);
    const hash = await sha256Hex(token);
    const row = await env.DB.prepare("SELECT * FROM oca_tokens WHERE hash = ? AND kind = 'refresh'").bind(hash).first();
    if (!row || row.expires < now() || row.app_id !== app.id) return json({ error: "invalid_grant" }, 400);
    await env.DB.prepare("DELETE FROM oca_tokens WHERE hash = ?").bind(hash).run();
    return json(await issueTokens(env, app, row.user_id, row.scope));
  }

  return json({ error: "unsupported_grant_type" }, 400);
}

async function bearer(request, env, scope) {
  const header = request.headers.get("authorization") || "";
  const match = header.match(/^Bearer\s+(.+)$/i);
  if (!match) return null;
  const row = await env.DB.prepare("SELECT * FROM oca_tokens WHERE hash = ? AND kind = 'access'").bind(await sha256Hex(match[1].trim())).first();
  if (!row || row.expires < now()) return null;
  if (scope && !String(row.scope).split(/\s+/).includes(scope)) return null;
  return row;
}

async function userinfoEndpoint(request, env) {
  const token = await bearer(request, env, "");
  if (!token) return json({ error: "invalid_token" }, 401);
  const user = await env.DB.prepare("SELECT id, username FROM oca_users WHERE id = ?").bind(token.user_id).first();
  return json({ sub: user.id, username: user.username, scope: token.scope });
}

async function revokeEndpoint(request, env) {
  const params = await formData(request);
  const token = String(params.get("token") || "");
  if (token) {
    await env.DB.prepare("DELETE FROM oca_tokens WHERE hash = ?").bind(await sha256Hex(token)).run();
  }
  return new Response("", { status: 200 });
}

/* ------------------------------------------------------------------ */
/* chat                                                                */
/* ------------------------------------------------------------------ */

async function chatProxy(request, env) {
  const token = await bearer(request, env, "chat");
  if (!token) return json({ error: "invalid_token", error_description: "sign in with openchat login" }, 401);
  if (!env.BOT_TOKEN) return json({ error: "server_misconfigured" }, 500);
  const body = await request.text();
  try {
    JSON.parse(body);
  } catch (_) {
    return json({ error: "invalid_request", error_description: "JSON body required" }, 400);
  }
  const init = {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${env.BOT_TOKEN}`,
      "user-agent": "openchat-auth/1.0",
    },
    body,
  };
  let response;
  try {
    response = env.BOT
      ? await env.BOT.fetch("https://discord-ai-bot/api/openchat", init)
      : await fetch(env.BOT_URL.replace(/\/+$/, "") + "/api/openchat", init);
  } catch (error) {
    return json({ error: "upstream_unreachable" }, 502);
  }
  const text = await response.text();
  return new Response(text, {
    status: response.status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}
