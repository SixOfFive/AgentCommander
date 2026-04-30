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
-- The endpoint and api_key live ONLY in this SQLite file, which is stored
-- in the OS user-data directory (XDG_DATA_HOME / %APPDATA% / Application Support)
-- and is gitignored so it never ships with the source tree.
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
  -- Configured num_ctx for the provider (Ollama "options.num_ctx", etc.).
  -- NULL = let the provider use its built-in default. Set by
  -- `/autoconfig --mincontext N` so the agent uses the chosen context size
  -- instead of whatever the runtime defaults to.
  context_window_tokens INTEGER,
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

-- ─────────────────────────────────────────────────────────────────────────
-- Scratchpad — the model-facing memory.
--
-- Every router/orchestrator decision, role call, and tool result becomes a
-- row here. The user-view comes from `messages`; the model-view (what gets
-- sent as the next prompt) is built from this table. Crucially:
--
--   * The user-view (`messages`) is NEVER touched by compaction. The user
--     can scroll back and see every turn in full fidelity.
--   * The model-view IS compactable: when the prompt would exceed num_ctx,
--     the engine writes a synthetic "compacted summary" entry whose
--     `replaced_message_ids` JSON-array points at the originals. The
--     originals stay in the DB (replay/inspection), but the engine filters
--     them out of the next prompt build.
--
-- `message_id` is the link between the two views: when an entry is the
-- user's input or the assistant's final reply, this column holds the
-- corresponding `messages.id`. For intermediate engine entries (router
-- classification, tool calls, role outputs that aren't the final reply)
-- it's NULL — those are model-view only.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scratchpad_entries (
  id TEXT PRIMARY KEY,                                -- UUID per entry
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  run_id TEXT,                                        -- which pipeline run; NULL for backfilled
  step INTEGER NOT NULL,
  role TEXT NOT NULL,                                 -- "router" / "orchestrator" / role.value / "tool" / "system"
  action TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '',
  output TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER,
  content TEXT,                                       -- write_file content blob, when applicable
  message_id TEXT,                                    -- FK to messages.id when this entry corresponds to a visible turn
  replaced_message_ids TEXT,                          -- JSON array of original entry ids this row stands for (compacted summary)
  is_replaced INTEGER NOT NULL DEFAULT 0,             -- 1 → hidden from prompt build (an earlier entry that compaction replaced)
  timestamp REAL NOT NULL,                            -- engine-side wall-clock (matches ScratchpadEntry.timestamp)
  created_at INTEGER NOT NULL                         -- DB insert time, ms epoch
);

CREATE INDEX IF NOT EXISTS idx_scratchpad_conv_time
  ON scratchpad_entries(conversation_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_scratchpad_message
  ON scratchpad_entries(message_id);

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

-- ─────────────────────────────────────────────────────────────────────────
-- Filesystem permission decisions — persisted "Always" choices.
-- "Yes once" decisions live only in memory for the current run.
-- decision is one of: 'allow' (yes from now on) | 'deny' (no from now on).
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fs_permissions (
  path TEXT NOT NULL,                -- absolute path
  operation TEXT NOT NULL,           -- 'read' | 'write' | 'delete'
  decision TEXT NOT NULL,            -- 'allow' | 'deny'
  scope TEXT NOT NULL DEFAULT 'exact', -- 'exact' | 'subtree' (subtree = path + all children)
  created_at INTEGER NOT NULL,
  PRIMARY KEY (path, operation)
);

CREATE INDEX IF NOT EXISTS idx_fs_permissions_path ON fs_permissions(path);

-- ─────────────────────────────────────────────────────────────────────────
-- Pipeline events — the live event stream of an in-flight run, persisted so
-- a read-only mirror process (`ac --mirror`) can replay everything the
-- primary is doing in close-to-real-time. Each engine event (role/start,
-- role/delta token chunk, role/end, tool/call, tool/result, guard/*, done)
-- becomes one row. Mirror polls `id > last_seen` every ~200ms and renders
-- in arrival order.
--
-- This table is INTENTIONALLY high-volume: token deltas can produce 10–60+
-- inserts/sec during streaming. WAL mode + synchronous=FULL absorb this;
-- the primary prunes rows older than ~1h on startup to keep the DB small.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pipeline_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT,
  run_id TEXT,
  event_type TEXT NOT NULL,           -- "role/start" | "role/delta" | "role/end" | "tool/call" | "tool/result" | "guard" | "done" | "system" | "error"
  payload TEXT NOT NULL,              -- JSON; shape depends on event_type
  created_at INTEGER NOT NULL         -- ms epoch
);

CREATE INDEX IF NOT EXISTS idx_pipeline_events_id ON pipeline_events(id);
