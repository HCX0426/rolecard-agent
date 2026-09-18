-- ===========================================================================
-- Role-card schema.
--
-- NOTE: the `plugin` table is NOT here - it lives in core/schema.sql, because
-- enable/disable is a kernel concern, not a role concern (docs/07 C6).
-- ===========================================================================

CREATE TABLE IF NOT EXISTS role_card (
    role_id         TEXT PRIMARY KEY,               -- lowercase ASCII, e.g. medical_archivist
    role_name       TEXT NOT NULL,
    system_prompt   TEXT NOT NULL,
    temperature     REAL NOT NULL DEFAULT 0.7
                    CHECK (temperature >= 0 AND temperature <= 1),
    model_name      TEXT,                           -- backend NAME from config.MODEL_BACKENDS
    tool_whitelist  TEXT,                           -- JSON array; NULL = all enabled tools, [] = none
    description     TEXT,

    -- Role reproducibility is not one channel but four; system_prompt alone only covers the
    -- first (rules). These two columns cover the other two that belong to the role:
    --
    --   exemplars        JSON [{"user": ..., "assistant": ...}] - a FEW high-quality samples
    --                    of how this role answers. Behaviour is shaped far more effectively
    --                    by examples than by writing longer rules.
    --   knowledge_scopes JSON ["health_reports"] - which retrieval scopes this role may read.
    --                    The role DECLARES scopes; it does not own a vector store. Owning one
    --                    would give N roles x M collections, duplicated indexes and no single
    --                    source of truth (docs/archive/技术评审与决策.md A1).
    --
    -- Exemplars are trusted content: writable only by an operator, never generated from
    -- conversation, otherwise a user could steer the persona through chat.
    exemplars         TEXT,
    knowledge_scopes  TEXT,

    is_builtin      INTEGER NOT NULL DEFAULT 0,     -- built-in roles cannot be deleted
    -- 主动开口（架构计划 B）：1 = 该角色会主动来找用户（还需全局 REACHOUT_ENABLED 开着）。
    -- 默认 0 = 出厂静默 —— 主动打扰是 opt-in。旧库经 storage/db._migrate 幂等补列。
    reachout_enabled INTEGER NOT NULL DEFAULT 0,
    -- 关系驱动主动开口（架构计划 §5.2）：两类关系驱动触发源的 per-role 开关。
    -- 默认 1 = 一旦该角色开启 reachout_enabled，关系驱动开口即生效（回忆 / 时段规律）。
    -- 用户可在「角色卡」里单独关掉任一类。旧库经 storage/db._migrate 幂等补列。
    recall_enabled       INTEGER NOT NULL DEFAULT 1,
    time_pattern_enabled INTEGER NOT NULL DEFAULT 1,
    -- 文件事件触发（架构计划 C·§5.2）per-role 闸门：该角色可否被目录变化触发。
    -- 默认 1 = 开了主动开口的角色自动关注目录变化；事件本身全局一份（任务目录是全局单值）。
    file_watch_enabled   INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- At least one role must always exist and must be built in, otherwise a delete
-- could leave the system with no usable role. Enforced in roles/service.py.
CREATE INDEX IF NOT EXISTS idx_role_card_builtin ON role_card(is_builtin);

-- GLOBAL_SAFETY_PROMPT is deliberately absent from this table: it lives in
-- core/prompts.py so that a role card can never override it.
