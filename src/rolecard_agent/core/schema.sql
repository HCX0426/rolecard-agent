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

-- Per-category service policy for the runtime service view (settings page,「服务」tab).
-- Covers the three categories whose candidates are NOT model rows (OCR / embedding / rerank);
-- model backends already have their own table + fallback chain above.
--   preferred  : candidate id this category should try FIRST (absent row = code default)
--   disabled   : JSON array of candidate ids excluded from automatic selection entirely
-- Candidates are defined IN CODE (core/services.py) — the DB only stores the operator's
-- ordering/enabling, never a catalogue. That keeps "what can exist" a commit, matching
-- domains/registry.py's explicit-registration philosophy.
CREATE TABLE IF NOT EXISTS service_policy (
    service_key TEXT PRIMARY KEY,
    preferred   TEXT,
    disabled    TEXT NOT NULL DEFAULT '[]',
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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

-- ===========================================================================
-- Generic domain data (settings page,「数据」tab for non-health domains).
--
-- A domain plugin that does not need health's rich report/indicator model can still
-- expose simple structured records here. Keyed by (domain, user_id) so each domain's
-- data is isolated; the frontend routes health to its own /api/records endpoints and
-- every other domain to /api/domains/{domain}/records.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS domain_data (
    id          TEXT PRIMARY KEY,
    domain      TEXT NOT NULL,
    user_id     TEXT NOT NULL REFERENCES app_user(user_id),
    label       TEXT NOT NULL,
    value_text  TEXT,
    value_num   REAL,
    unit        TEXT,
    note        TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_domain_data_domain ON domain_data(domain, user_id);

-- ===========================================================================
-- Service endpoints (settings page,「服务」tab) — candidate INSTANCES as data.
--
-- 哲学修正（架构归一化）：此前"候选在代码里定义，DB 只存排序与启停"——每类服务被
-- 钉死在 2 个候选上，操作员想加第 3 个云端条目（另一个 key 的 OCR / 另一家嵌入商）
-- 只能改代码。现在候选实例 = 行：云端行可增删改（各自 base_url / api_key / model），
-- 优先级 = sort_order（第 1 位即生效），启停 = enabled。本地实现（Paddle / Hash / off）
-- 是代码能力，行 builtin=1 不可删，但同样参与排序与启停。
-- 与 model_backend 同一密钥纪律：api_key 落盘明文（本地演示库不出机）、GET 只回掩码、
-- PATCH 不带 key = 保留、空串 = 清除。
-- ===========================================================================

CREATE TABLE IF NOT EXISTS service_endpoint (
    category   TEXT NOT NULL,
    id         TEXT NOT NULL,
    label      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'cloud' CHECK (kind IN ('local', 'cloud')),
    base_url   TEXT,
    api_key    TEXT,
    model      TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    builtin    INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP,
    PRIMARY KEY (category, id)
);

CREATE INDEX IF NOT EXISTS idx_service_endpoint_cat ON service_endpoint(category, sort_order);

