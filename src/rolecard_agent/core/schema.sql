-- ===========================================================================
-- Kernel schema. Owned by core/.
--
-- These tables model concepts the HARNESS itself needs - identity, session
-- ownership, plugin switches, audit. They are NOT domain concepts, which is
-- why user/tenant live here and not in any domain plugin (docs/07 A2/C6).
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
    -- 会话级对话模式（对话页「对话/智能体」切换）：NULL = 跟随全局默认
    -- （settings.agent_default_mode）；'chat' / 'agent' = 本会话显式覆盖。
    -- 旧库要跑 core/storage.db 的 _migrate() 补列（ALTER TABLE ADD COLUMN，幂等）。
    agent_mode       TEXT,
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

-- 角色主动开口（架构计划 B）：agent_reachout 是**收件箱**而非对话历史 ——
-- 角色主动的产出独立于 checkpoint 存储：天然支持未读/已读/清空，不污染对话提交历史，
-- 且页面关着时也能攒下来（web 端轮询读取，回来才看到）。
CREATE TABLE IF NOT EXISTS agent_reachout (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id     TEXT NOT NULL,             -- 发起主动的角色
    role_name   TEXT,                      -- 展示名冗余（角色被删后仍可读）
    text        TEXT NOT NULL,             -- 主动开口的内容
    state       TEXT NOT NULL DEFAULT 'unread',  -- unread / read
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP  -- UTC（CURRENT_TIMESTAMP）
);

CREATE INDEX IF NOT EXISTS idx_reachout_state_time ON agent_reachout(state, created_at DESC);

-- 关系驱动主动开口的 per-role 状态（架构计划 §5.2）：按 role_id 隔离，不复用全局状态。
-- affinity = 关系数值（互动积累的成长值，到阈值即主动冒泡）；calibration_json = 主动度校准
-- （记录哪些主动被接受/驳回，用于"该不该现在打扰"的判断）；last_interaction_utc = 最近一次
-- 主动/被交互的 UTC 时间，用于关系数值随时间自然衰减。
CREATE TABLE IF NOT EXISTS role_proactive_state (
    role_id               TEXT PRIMARY KEY,
    affinity             REAL NOT NULL DEFAULT 0.0,
    last_interaction_utc TIMESTAMP,
    calibration_json     TEXT,
    updated_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 按角色卡隔离的长期记忆（架构计划 §5.2 实现前置）：与全局 kernel_meta 的 memory:facts 分离，
-- 角色 B 绝不直接读角色 A 的记忆。为空 = 该角色尚无专属记忆（生成回退到用户级全局记忆）。
CREATE TABLE IF NOT EXISTS role_memory (
    role_id     TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Runtime-editable model backends (settings page). Empty table = use env config as-is;
-- the first settings save takes over. API keys are stored PLAINTEXT in the local demo
-- database: this file never leaves the machine, and the GET endpoint never returns them
-- (only a has_key flag) - the round-trip rule lives in core/model_settings.py.
--
-- usage 标记该配置的用途（架构归一化：模型页是云端端点配置的**唯一事实面**）：
--   chat      对话/抽取推理（默认；对话菜单与角色路由只消费这类行）
--   embedding 语义嵌入凭据（服务页「语义嵌入」引用）
--   rerank    检索重排凭据（服务页「检索重排」引用）
--   ocr       云端 OCR 凭据（如 OCR.space 账号；服务页「OCR」引用）
-- 服务页的云端条目一律是对本表行的**引用**，不复制配置（见 service_endpoint）。
CREATE TABLE IF NOT EXISTS model_backend (
    name        TEXT PRIMARY KEY,
    provider    TEXT NOT NULL DEFAULT 'openai',
    base_url    TEXT,
    model       TEXT NOT NULL,
    api_key     TEXT,
    usage       TEXT NOT NULL DEFAULT 'chat',
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
-- RELATION DIRECTION: domains reference this table, never the reverse. A domain's own row
-- carries an `ingestion_task_id` column pointing here, so the kernel stays free of any domain
-- knowledge - it hands out task ids, it never names (or writes) the table that stores them.
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
-- Generic domain data (settings page,「数据」tab for domains without their own tables).
--
-- A domain plugin that does not need a rich report/indicator model can still
-- expose simple structured records here. Keyed by (domain, user_id) so each domain's
-- data is isolated; the frontend routes the rich-model domain to its own
-- /api/records endpoints and every other domain to /api/domains/{domain}/records.
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
-- Service endpoints (settings page,「服务」tab) — REFERENCES into model_backend.
--
-- 架构归一化（引用模型）：模型页是云端端点配置的唯一事实面；本表只存「哪些配置参与
-- 这类服务、以什么优先级、是否启用」—— 绝不复制 key/base_url/model。
--   本地行（builtin=1）：paddle / hash / off 等代码能力，id 固定、不可删、可排序停用；
--   引用行（builtin=0）：ref_backend → model_backend.name，id = ref_backend（每类服务
--   内一后端至多一条引用）。删除引用行**绝不**动模型页配置；后端被模型页删除时，
--   引用行在视图中呈现「失效」。
-- 优先级 = sort_order，第 1 位即生效；启停 = enabled。
-- ===========================================================================

CREATE TABLE IF NOT EXISTS service_endpoint (
    category    TEXT NOT NULL,
    id          TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'cloud' CHECK (kind IN ('local', 'cloud')),
    ref_backend TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    builtin     INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMP,
    PRIMARY KEY (category, id)
);

CREATE INDEX IF NOT EXISTS idx_service_endpoint_cat ON service_endpoint(category, sort_order);

-- ===========================================================================
-- 命令执行审批（架构计划 C·§6.2 run_command）。命令要"跑在授权目录里"这件事本身
-- 就是高危动作，所以模型提交的命令**默认要人批准**才执行：
--
--   状态机（v1）：pending → approved（后台执行中）→ done（result_json 已回填）
--                     ↘ rejected（终态：拒了不再自动重提）
--   批准判定（幂等复用）：同 command（规范化后）最近一条记录 ∈ {approved, done}
--   → 工具视为已获准（不再要求重复提交审批）；pending 表示"还在等"。
--
-- 区别于 agent_reachout：这里存的是**执行意图 + 结果**，不是给用户看的内容；
-- 命令、目录、退出码、耗时落 audit_log，输出只截断进 result_json（不上审计表）。
-- ===========================================================================

CREATE TABLE IF NOT EXISTS command_approval (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    command      TEXT NOT NULL,
    cwd          TEXT,
    role_id      TEXT,                     -- 提交审批的角色（角色被删后仍可读冗余名）
    role_name    TEXT,
    thread_id    TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'rejected', 'done')),
    result_json  TEXT,                     -- done 后：{exit_code, stdout, stderr, duration_ms, output_bytes}
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_command_approval_status ON command_approval(status, id DESC);

-- ===========================================================================
-- MCP 接入（架构计划 C·§6.1 的 operator 自助入口）。
--
-- 与 env 的 MCP_SERVERS 并存：生效集 = env ∪ 本表 enabled 行（同 id 本表覆盖 env）。
-- env 是"随部署烧进去的高级路径"，本表是"运行时可增删改的路径"，二者经 rebuild 合并加载。
--
-- 仅 http 传输（stdio 会 spawn 本地任意进程，安全面大、另议）。URL 由可信 operator 主动填
-- → 本机/内网/公网均可（"仅公网"的 SSRF 边界只用于模型给的 URL，如 web_fetch）。
-- headers 可能含鉴权密钥：GET 端点**永不回明文**（掩码，仿 model_backend.api_key 的
-- 只写不回读 + round-trip：PATCH 省略 headers = 保留原值，传 {} = 清空）。
-- 与 plugin 表同构：这是 operator 开关，**绝不暴露成 LLM 可调用工具**（自我扩权红线）。
-- ===========================================================================

CREATE TABLE IF NOT EXISTS mcp_server (
    id            TEXT PRIMARY KEY,          -- 稳定标识：工具名前缀 + 审计 target（非展示名）
    display_name  TEXT NOT NULL,
    transport     TEXT NOT NULL DEFAULT 'http' CHECK (transport IN ('http')),
    url           TEXT NOT NULL,             -- http(s) 端点；create/update 校验 scheme+主机（本机/内网/公网均可）
    headers_json  TEXT NOT NULL DEFAULT '{}',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

