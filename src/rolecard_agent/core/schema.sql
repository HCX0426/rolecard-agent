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
