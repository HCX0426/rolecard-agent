-- ===========================================================================
-- Kernel schema. Owned by core/.
--
-- These tables model concepts the HARNESS itself needs - identity, session
-- ownership, plugin switches, audit. They are NOT domain concepts, which is
-- why user/tenant live here and not in domains/health/ (docs/07 A2/C6).
--
-- A domain plugin must never define its own user or tenant table.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS tenant (
    tenant_id     TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_user (
    user_id       TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL REFERENCES tenant(tenant_id),
    display_name  TEXT NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_app_user_tenant ON app_user(tenant_id);

-- LangGraph checkpoints are keyed by thread_id alone. This table is what maps a
-- thread to a logged-in user - without it there is no answer to "who is talking",
-- and therefore no memory isolation or permission check (docs/05 A2).
CREATE TABLE IF NOT EXISTS session_thread (
    thread_id        TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES app_user(user_id),
    current_role_id  TEXT NOT NULL,
    -- 会话级模型覆盖（对话页模型下拉）：NULL = 无覆盖（按 角色.model_name → 默认解析）。
    model_name       TEXT,
    -- Version stamp of the enabled tool set. Bumped whenever plugins are toggled.
    -- On resume, a checkpoint whose tool_epoch is older than the current one may
    -- reference tools that no longer exist; the executor must answer
    -- "this capability is offline" instead of raising (docs/07 C14).
    tool_epoch       INTEGER NOT NULL DEFAULT 1,
    title            TEXT,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_session_thread_user ON session_thread(user_id);

-- Data-driven plugin switches. Operator-only: this is deliberately NOT exposed
-- as an LLM-callable tool, otherwise the model could widen its own capability
-- set (docs/02 D2, docs/04 section 2).
CREATE TABLE IF NOT EXISTS plugin (
    plugin_id     TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 0,
    config_json   TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Audit trail for operator actions: role switches, plugin toggles, deletions.
-- Required by docs/06 US-3.
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    target       TEXT,
    detail_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);

-- Kernel bookkeeping: a single-row-per-key table for values the harness needs to persist.
-- `tool_epoch` lives here and is what makes "a plugin was toggled" detectable after a restart.
CREATE TABLE IF NOT EXISTS kernel_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Seeded so the invariant "a tool_epoch row always exists" holds immediately after bootstrap,
-- instead of every reader having to invent a default.
INSERT OR IGNORE INTO kernel_meta (key, value) VALUES ('tool_epoch', '1');

-- Runtime-editable model backends (settings page). Empty table = use env config as-is;
-- the first settings save takes over. API keys are stored PLAINTEXT in the local demo
-- database: this file never leaves the machine, and the GET endpoint never returns them
-- (only a has_key flag) - the round-trip rule lives in core/model_settings.py.
CREATE TABLE IF NOT EXISTS model_backend (
    name        TEXT PRIMARY KEY,
    provider    TEXT NOT NULL DEFAULT 'openai',
    base_url    TEXT,
    model       TEXT NOT NULL,
    api_key     TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- ===========================================================================
-- Document intake ledger.
--
-- WHY THIS IS A SEPARATE TABLE, and not a `status` column on the domain's report table:
--
--   1. Process vs fact. An intake can be retried three times; the resulting report is still
--      one report. Putting run state on the report row means either losing the attempt history
--      or adding attempts/last_error columns to a fact table - i.e. growing the ledger inside
--      the report.
--   2. Half-finished rows leaking. If a report row exists while processing is incomplete, every
--      reader downstream must remember to filter it out. One forgotten `WHERE status = ...`
--      and an unverified fragment reaches the user. In this project that is not an acceptable
--      failure mode, so an incomplete intake produces NO report row at all.
--   3. Cardinality. One file can yield several reports (a checkup covering multiple
--      departments). The relation is 1:N, and a 1:N relation cannot be a column.
--
-- RELATION DIRECTION: domains reference this table, never the reverse. `medical_report` carries
-- `ingestion_task_id`, so the kernel stays free of any domain knowledge.
--
-- UNIQUE (user_id, file_hash) is the idempotency key: re-uploading the same bytes returns the
-- existing task instead of creating a duplicate ledger entry. Re-processing is an explicit
-- action that resets that row, not a new one.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS ingestion_task (
    task_id      TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES app_user(user_id),
    source_file  TEXT,
    file_hash    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'parsed', 'extracted', 'indexed', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at  TIMESTAMP,
    UNIQUE (user_id, file_hash)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_status ON ingestion_task(status);
CREATE INDEX IF NOT EXISTS idx_ingestion_user ON ingestion_task(user_id);
