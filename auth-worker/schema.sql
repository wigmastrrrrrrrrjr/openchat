CREATE TABLE IF NOT EXISTS oca_users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  username_norm TEXT NOT NULL UNIQUE,
  pass_hash TEXT NOT NULL,
  pass_salt TEXT NOT NULL,
  pass_iter INTEGER NOT NULL,
  created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oca_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  csrf TEXT NOT NULL,
  expires INTEGER NOT NULL,
  created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oca_apps (
  id TEXT PRIMARY KEY,
  user_id TEXT,
  name TEXT NOT NULL,
  description TEXT,
  client_id TEXT NOT NULL UNIQUE,
  client_secret_hash TEXT,
  redirects TEXT NOT NULL,
  scopes TEXT NOT NULL,
  created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oca_codes (
  hash TEXT PRIMARY KEY,
  app_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  scope TEXT NOT NULL,
  challenge TEXT,
  method TEXT,
  expires INTEGER NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oca_tokens (
  hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  user_id TEXT NOT NULL,
  app_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  expires INTEGER NOT NULL,
  created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oca_keys (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  label TEXT NOT NULL,
  provider TEXT NOT NULL,
  base TEXT,
  secret TEXT NOT NULL,
  created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oca_attempts (
  username_norm TEXT NOT NULL,
  ts INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS oca_idx_attempts ON oca_attempts(username_norm, ts);
CREATE INDEX IF NOT EXISTS oca_idx_tokens_user ON oca_tokens(user_id);
CREATE INDEX IF NOT EXISTS oca_idx_keys_user ON oca_keys(user_id);
CREATE INDEX IF NOT EXISTS oca_idx_codes_exp ON oca_codes(expires);

-- Official public client used by the openchat CLI (PKCE, loopback redirects, no secret).
INSERT OR IGNORE INTO oca_apps
  (id, user_id, name, description, client_id, client_secret_hash, redirects, scopes, created)
VALUES
  ('app_openchatcli', NULL, 'openchat CLI', 'Official command-line client for openchat',
   'openchat-cli', NULL,
   '["http://127.0.0.1/callback","http://localhost/callback"]',
   '["profile:read","chat"]',
   CAST(strftime('%s','now') AS INTEGER));
