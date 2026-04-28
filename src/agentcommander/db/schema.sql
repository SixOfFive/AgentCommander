-- AgentCommander local SQLite schema.
-- Single-user, single-machine. No tenant_id / user_id columns anywhere.
-- All CREATE TABLE statements are idempotent (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS migrations (
  name TEXT PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

-- Key-value JSON config blob (working_directory, ui prefs, autoconfig flags, ...)
CREATE TABLE IF NOT EXISTS config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Provider config (Ollama, llama.cpp, OpenRouter, Anthropic, Google).
CREATE TABLE IF NOT EXISTS providers (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  name TEXT NOT NULL,
  endpoint TEXT,
  api_key TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);

-- Role -> (provider, model). Default flow: every role inherits the default
-- model the user picked. is_override=1 marks a per-role pin.
CREATE TABLE IF NOT EXISTS role_assignments (
  role TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  is_override INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  working_directory TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0,
  pinned INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS token_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT,
  role TEXT NOT NULL,
  provider_id TEXT,
  model TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  duration_ms INTEGER,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_token_usage_conv ON token_usage(conversation_id, created_at);

-- Local model hints — score adjustments accumulated per (model, role).
-- Mirrors EC's resources/model-hints-*.json but in SQLite.
-- ±0.1 per pipeline run, clamped ±100.
CREATE TABLE IF NOT EXISTS model_hints (
  model_id TEXT NOT NULL,
  role TEXT NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  runs INTEGER NOT NULL DEFAULT 0,
  last_bump_at INTEGER,
  PRIMARY KEY (model_id, role)
);

-- Audit log — local equivalent of ec_security_log. Every tool invocation,
-- every guard block, every role failure lands here.
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  details TEXT,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at DESC);

-- Pipeline replay/inspector data.
CREATE TABLE IF NOT EXISTS pipeline_runs (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  status TEXT NOT NULL,
  iterations INTEGER NOT NULL DEFAULT 0,
  category TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
  iteration INTEGER NOT NULL,
  step_type TEXT NOT NULL,
  name TEXT NOT NULL,
  input TEXT,
  output TEXT,
  duration_ms INTEGER,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pipeline_steps_run ON pipeline_steps(run_id, iteration);

-- Operational rules — feeds preflight + filled by postmortem.
CREATE TABLE IF NOT EXISTS operational_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint_version INTEGER NOT NULL,
  action_type TEXT NOT NULL,
  target_pattern TEXT,
  context_tags TEXT,             -- JSON array
  constraint_text TEXT NOT NULL,
  suggested_reorder TEXT,        -- JSON
  origin TEXT NOT NULL,          -- 'postmortem' | 'manual' | 'imported'
  confidence REAL NOT NULL DEFAULT 0.5,
  helped_count INTEGER NOT NULL DEFAULT 0,
  hurt_count INTEGER NOT NULL DEFAULT 0,
  example_run_id TEXT,
  created_at INTEGER NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_oprules_action ON operational_rules(action_type, archived);
