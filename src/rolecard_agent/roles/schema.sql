-- ===========================================================================
-- Role-card schema.
--
-- NOTE: the `plugin` table is NOT here - it lives in core/schema.sql, because
-- enable/disable is a kernel concern, not a role concern (docs/07 C6).
-- ===========================================================================

CREATE TABLE IF NOT EXISTS role_card (
    role_id         TEXT PRIMARY KEY,               -- lowercase ASCII, e.g. medical_archivist
    -- 归属：这张卡是"谁的"。09-27 起每台实例有自己的主人（`IDENTITY_USER_ID`，架构总览 §4.1），
    -- 所有读路径都按它过滤 —— 包括播种出来的那几张：**内置卡今天不算公共资产**。
    -- 为什么不留一个"公共卡"的概念：那需要"模板 + 每人一份覆盖"的语义（改一张卡到底改了几份），
    -- 是另一件产品事；今天先按"换身份 = 换一份完整数据集"办。
    -- 为什么默认值是字面量而没有 REFERENCES：SQLite 不允许"带非常量默认值的 ADD COLUMN"再挂外键，
    -- 而老库升上来走的正是 ADD COLUMN。默认值与 `core/identity.DEFAULT_USER_ID` 的一致性由
    -- `tests/unit/test_identity_scoping.py` 钉住（这两个数一旦漂，旧库读法就会静默变空）。
    user_id         TEXT NOT NULL DEFAULT 'local-user',
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
    -- 默认 0 = 出厂静默 —— 主动打扰是 opt-in。旧库由 reconcile_columns 按这行声明补列。
    reachout_enabled INTEGER NOT NULL DEFAULT 0,
    -- 关系驱动主动开口（架构计划 §5.2）：两类关系驱动触发源的 per-role 开关。
    -- 默认 1 = 一旦该角色开启 reachout_enabled，关系驱动开口即生效（回忆 / 时段规律）。
    -- 用户可在「角色卡」里单独关掉任一类。旧库由 reconcile_columns 按这行声明补列。
    recall_enabled       INTEGER NOT NULL DEFAULT 1,
    time_pattern_enabled INTEGER NOT NULL DEFAULT 1,
    -- 「关系够近了所以想找你」这一档的 per-role 开关（09-26 轮 R26-23 补上的第四把）。
    -- 为什么需要它：触发源那条 `or` 链是**排他**的，而 affinity 每次成功开口 +0.2、封顶 5.0，
    -- 一旦触顶就永久命中这一档 ⇒ 排在它后面的时段规律 / 回忆 / 定时三档**再也读不到**。
    -- 默认 1 = 与今天的实际行为完全一致；关掉它才谈得上让其余几档轮得到。
    affinity_enabled     INTEGER NOT NULL DEFAULT 1,
    -- 文件事件触发（架构计划 C·§5.2）per-role 闸门：该角色可否被目录变化触发。
    -- 默认 1 = 开了主动开口的角色自动关注目录变化；事件本身全局一份（任务目录是全局单值）。
    file_watch_enabled   INTEGER NOT NULL DEFAULT 1,
    -- 收件箱自动保留条数（用户 2026-09-23："抽屉没删除功能，越堆越多"）：
    -- 0 = 不自动删（默认，行为与今天一致）；N>0 = 每次她主动开口后，该角色的收件箱
    -- 只留最近 N 条。删的是**投递记录**，不是她说出口的那句话（那句在主动会话里，
    -- 是她下次开口的依据 —— 边界见 docs/主动消息与记忆设计稿.md）。旧库由 reconcile_columns 补。
    reachout_keep        INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- At least one role must always exist and must be built in, otherwise a delete
-- could leave the system with no usable role. Enforced in roles/service.py.
CREATE INDEX IF NOT EXISTS idx_role_card_builtin ON role_card(is_builtin);

-- GLOBAL_SAFETY_PROMPT is deliberately absent from this table: it lives in
-- core/prompts.py so that a role card can never override it.
