"""运行时设置覆盖（Runtime overrides）—— 让「运行环境」页签的配置可改、保存即热生效。

两层契约（与模型页同一哲学，用户 2026-09-17："我想在软件里可以改"）：

  * **env 仍是部署期引导**：`.env` 决定初始值；
  * **DB 覆盖是操作员时刻**：本模块把操作员在「运行环境」页签的修改写进 kernel_meta
    （键 `runtime:<field>`），`apply_overrides` 在每次 Settings 生效（启动 / 热重建）时
    把覆盖叠到 env 之上 —— **后写者胜**，且删除覆盖即回落 env。

不是所有显示项都可编辑：认证中间件 / tracer / 数据路径随进程构建，改了也只能重启才生效，
保留只读（界面上如实标注），避免"改了没生效"的困惑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rolecard_agent.config import (
    SECRET_FIELD_NAMES,
    Settings,
    env_falsy,
    env_truthy,
)
from rolecard_agent.storage.db import SqlConnection

_PREFIX = "runtime:"


@dataclass(frozen=True)
class FieldSpec:
    """一个运行环境项：Settings 字段名、env 键、类型、**展示元数据**与分组。

    `choices_from="models"`：下拉选项**动态**来自用户配置的模型名（model_backend 表），
    而不是写死的枚举 —— 用户加了新模型，选项自动跟上。
    `choices_from="backends"`：选项是**后端名**（`Settings.model_backends` 的键）。
    两者不是一回事，别混：`resolve_role_model(后端名)` 认的是键，而"模型列"可以重名
    （两个后端都叫 qwen3:8b）。填错在这里是**静默**回落默认模型 ⇒ 症状又变回
    "记忆悄悄提不出来"，所以这类字段必须给下拉，不能让人手打。

    **展示元数据住这里（单注册表）**：`label` / `note` / `group` / `show` 与
    `kind == "ro"` 让「运行环境」页的分组与行**由注册表派生**。从前页面的行清单在
    `api/routers/settings.py` 手抄了第三遍（字段、env 键、分组标签各一份），新增一个
    配置项要改三处、漏改哪一处都只表现为"页面缺一行"而没有任何信号。

    * `kind == "ro"` = 只读展示行（随进程构建，改了要重启）：进注册表只为让视图有据可派；
      **保存/加载的两个索引显式过滤它** —— PUT 提交只读键照旧报"不支持在线修改"（400），
      行为与"只读行不进注册表"的旧结构逐字一致；
    * `show = False` = 可保存但不进这一页（写点在别的卡片，如记忆卡的三个开关）；
    * `note` 里的 `{ocr_python}` 是唯一一处动态文案占位，视图填当前值，其余一律字面量。
    """

    field: str
    env_key: str
    kind: str  # bool | secret | float | int | str | ro（只读展示行）
    choices: tuple[str, ...] | None = None
    choices_from: str | None = None
    label: str = ""
    note: str = ""
    group: str = ""
    show: bool = True


#: 「运行环境」页的分组顺序与标题（顺序即渲染顺序，改序是界面变更）。
RUNTIME_GROUPS: tuple[tuple[str, str], ...] = (
    ("web", "联网"),
    ("ocr", "OCR"),
    ("rag", "检索与抽取"),
    ("limit", "超时与预算"),
    ("think", "思考模式"),
    ("agent", "对话模式"),
    ("reachout", "主动开口"),
    ("run", "命令执行"),
    ("auth", "访问控制"),
    ("egress", "数据出口"),
    ("obs", "观测"),
)

#: 注册表按**组主序**排列：组内顺序 = 页面行序（金样快照守着逐字节不变）。
#: 原注释（各条的理由与沿革）随条目一起搬过来 —— 注释是条目的说明书，搬家不丢。
RUNTIME_FIELDS: tuple[FieldSpec, ...] = (
    # -- web 联网 ------------------------------------------------------------------
    FieldSpec("web_search_enabled", "WEB_SEARCH_ENABLED", "bool",
              label="联网总闸", note="0 = web_search / web_fetch 一律返回关闭说明", group="web"),
    FieldSpec("web_allowed_domains", "WEB_ALLOWED_DOMAINS", "str",
              label="域名白名单",
              note="web_fetch 只允许名单内域名（子域匹配），空 = 不限", group="web"),
    FieldSpec("web_search_backend", "WEB_SEARCH_BACKEND", "str", ("auto", "tavily", "ddgs", "off"),
              label="搜索",
              note="auto=配了 Tavily Key 走云端搜索，否则本地 ddgs", group="web"),
    FieldSpec("tavily_api_key", "TAVILY_API_KEY", "secret",
              label="Tavily 云端搜索 Key",
              note="本地搜索超时时配它（当前搜索走哪条路看上一行）", group="web"),
    FieldSpec("saucenao_api_key", "SAUCENAO_API_KEY", "secret",
              label="SauceNAO 反向图搜 Key",
              note="image_search 工具用（识别动漫/插画角色）；不配则该工具返回未配置提示",
              group="web"),
    # -- ocr OCR -------------------------------------------------------------------
    # OCR"用哪个后端"**不在此列**：那是「服务」页的端点序（运行期唯一事实面）。曾有的
    # OCR_BACKEND / OCR_API_KEY / OCR_API_URL 三项在生产上从不被读（内置行恒在 ⇒ 工厂的
    # env 分支不可达），留着就是三个假开关 —— 随 P1-5 一并收口。这里只剩解释器路径（只读）。
    FieldSpec("ocr_python", "OCR_PYTHON", "ro",
              label="RapidOCR 解释器", note="RapidOCR 独立解释器：{ocr_python}", group="ocr"),
    # -- rag 检索与抽取 --------------------------------------------------------------
    # 嵌入/重排的**选型**同样不在此列：「服务」页的端点序是唯一事实面。这里只留一个真正
    # 被读的部署值 —— 云端行未填 base_url 时的兜底端点（随进程构建 ⇒ 只读）。
    # 凭据不出现在任何运行环境行里：模型页的 api_key 只写不回读（`has_key` 掩码）。
    FieldSpec("siliconflow_base_url", "SILICONFLOW_BASE_URL", "ro",
              label="云端嵌入/重排兜底端点",
              note="行内未填 base_url 的云端端点用它兜底；改它需重启（随进程构建）", group="rag"),
    FieldSpec("extract_backend", "EXTRACT_BACKEND", "str",
              label="抽取用的模型", group="rag"),
    FieldSpec("extract_verify", "EXTRACT_VERIFY", "str",
              label="抽取校对", group="rag"),
    # 「提取精华 / 整理记忆」用哪个后端（空 = 跟随会话/角色）。单独一条是为了把"对话用谁"与
    # "记忆用谁"分开 —— 角色留在本地陪聊，不代表它的长期记忆也该由 8B 来判（审计 §12.5）。
    # 依据在 2026-09-24 更正两次：本地并非提不出；修完捏造口子与提取自我叠加后两边各 9 / 10 条。
    # 所以它是延迟与判断力的取舍，不是"有没有记忆"。填了才出网，不填行为与今天一致。
    # 为什么是"backends"而不是"models"：这里存的是 `resolve_role_model()` 认的**后端名**
    # （`Settings.model_backends` 的键），而模型列可以重名 —— 手打错了不会报错，只会静默
    # 回落默认模型，症状正好是这次要修的"记忆悄悄提不出来"。
    FieldSpec("memory_extract_backend", "MEMORY_EXTRACT_BACKEND", "str",
              choices_from="backends", label="记忆用的模型（提取精华 / 整理记忆）",
              note="空 = 跟随这条会话/角色用的模型。填上一个模型名 = 只把「提取精华」和「整理记忆」"
              "这两步交给它。实测两边都提得出（同一段八轮对话各 9 / 10 条），所以这不是"
              '"有没有记忆"的开关，而是取舍：本地一次约 122 秒、云端 10–20 秒，而「整理记忆」'
              '那个"谁顶替谁"的判断更吃模型强度。**填了才出网**，清空即回到今天的行为。',
              group="rag"),
    # -- limit 超时与预算 ------------------------------------------------------------
    FieldSpec("model_timeout_seconds", "MODEL_TIMEOUT_SECONDS", "float",
              label="模型调用超时（秒）", note="0=不限", group="limit"),
    FieldSpec("tool_timeout_seconds", "TOOL_TIMEOUT_SECONDS", "float",
              label="工具执行上限（秒）", note="单次工具总时长", group="limit"),
    FieldSpec("context_max_chars", "CONTEXT_MAX_CHARS", "int",
              label="历史字符预算", note="送模型的历史上限", group="limit"),
    # 多模型比对总闸：关 = `compare_model_answers` 一律返回关闭说明（一次 = N 次真调用，
    # 且同一问题会发给多个供应商；成本与隐私同一类，见 core/models/consensus.py）。
    FieldSpec("consensus_enabled", "CONSENSUS_ENABLED", "bool",
              label="多模型比对总闸",
              note="0 = compare_model_answers 返回关闭说明（一次≈N 次调用，且发给多个供应商）",
              group="limit"),
    # -- think 思考模式 --------------------------------------------------------------
    FieldSpec(
        "model_thinking", "MODEL_THINKING", "str", ("auto", "off"),
        label="思考总开关",
        note="auto=按名单显示 / off=名单内也不显示（只是藏起来：思考照旧发生、"
        "那几十秒照旧花）",
        group="think",
    ),
    FieldSpec(
        "model_thinking_models", "MODEL_THINKING_MODELS", "str", choices_from="models",
        label="思考模型名单",
        note="名单内模型以 reasoning=True 调用 ⇒ 思考显示在折叠面板；"
        "不列名它照样想，只是看不见",
        group="think",
    ),
    # -- agent 对话模式 --------------------------------------------------------------
    # Agent 模式全局默认：「运行环境」页保存即热重建；会话级切换覆盖它（对话页）。
    FieldSpec(
        "agent_default_mode", "AGENT_DEFAULT_MODE", "str", ("chat", "agent"),
        label="全局默认模式",
        note="chat=一问一答 / agent=多步自主任务（步数上限放大、注入规划指令）",
        group="agent",
    ),
    FieldSpec("agent_max_steps", "AGENT_MAX_STEPS", "ro",
              label="步数上限（对话档）",
              note="单轮允许的图步数；agent 模式自动翻倍，0=库默认", group="agent"),
    # -- reachout 主动开口 -----------------------------------------------------------
    # 角色主动开口全局总闸：「运行环境」页保存即热生效（调度每 tick 读当前值）。
    FieldSpec("reachout_enabled", "REACHOUT_ENABLED", "bool",
              label="全局总闸",
              note="角色主动找你的总开关；谁真有资格主动看各角色卡的开关", group="reachout"),
    # 文件事件触发（架构总览 §5）全局闸：开 = 每 tick 轮询任务目录做素材门控开口。
    FieldSpec("file_watch_enabled", "FILE_WATCH_ENABLED", "bool",
              label="文件事件触发",
              note="开 = 轮询任务目录，有变化时该次开口先说变化（绕间隔一次，静默时段不放松）",
              group="reachout"),
    # 开口间隔（分钟）：调度每 tick 读当前值，所以在线可改。以前只能改 env + 重启，
    # 排查"它为什么不开口"时没法临时调小复现（架构总览 §5 的抑制层之一）。
    FieldSpec("reachout_interval_minutes", "REACHOUT_INTERVAL_MINUTES", "int",
              label="开口间隔（分钟）",
              note="同一角色两次「冒话」的最小间隔（回答和主动开口都算）；保存即热生效，"
              "排查时可临时调小", group="reachout"),
    # 收件箱一摞合并成一行覆盖几天。**只有三档**（决策点 A）：连续滑块会产出
    # "合并 5 天"这种没人能预判界面长什么样的取值，而 1/3/7 正好对应"今天/这周/最近一段"。
    # 写在 RUNTIME_FIELDS 是为了复用覆盖存储与热重建，**可写入口不在运行环境页**
    # （页面只是展示），单一写点在「记忆与任务目录」的主动开口卡 —— 同 reachout_enabled。
    FieldSpec("reachout_merge_days", "REACHOUT_MERGE_DAYS", "int", ("1", "3", "7"),
              label="收件箱合并窗口（天）",
              note="1/3/7 天：同一角色在一个窗口里的开口折成一行（未读数上角标）。"
              "**改它在「记忆与任务目录」的主动开口卡**，这里只读——一个设置只有一个写点。",
              group="reachout"),
    # -- run 命令执行 ----------------------------------------------------------------
    # 命令执行（架构计划 C·§6.2）：总闸 + 审批档都允许在线热切（工具每调用读 settings）。
    FieldSpec("run_tools_enabled", "RUN_TOOLS_ENABLED", "bool",
              label="命令执行总闸",
              note="关掉 = run_command 一律返回关闭说明（1=开，0=关）", group="run"),
    FieldSpec("run_approval", "RUN_APPROVAL", "str", ("manual", "auto"),
              label="审批模式",
              note="manual=命令要人批准才跑（推荐）；auto=无审批直接跑（仅自研/可信目录用）",
              group="run"),
    # -- auth 访问控制（只读：随进程构建，改了要重启） ------------------------------------
    FieldSpec("auth_mode", "AUTH_MODE", "ro", label="认证模式", note="off / auto / on",
              group="auth"),
    FieldSpec("auth_credentials", "AUTH_CREDENTIALS", "ro", label="Basic 凭据", group="auth"),
    FieldSpec("auth_api_keys", "AUTH_API_KEYS", "ro", label="API Key 列表", group="auth"),
    # -- egress 数据出口 --------------------------------------------------------------
    # 单独一组而不是塞进「访问控制」：那一组是**只读**的（随进程构建），而这一条是**可改**
    # 的（判据随请求读当前 Settings）。混在一组里，同一个组标题下一半能改一半不能，
    # 比多一组难读（R102-58）。
    # 出站目标允许清单：语义就是"新增一个数据出口要 operator 确认一次" —— 只活在 .env 里
    # 等于每次都要改文件加重启。判据随请求读当前 Settings，保存即生效。
    FieldSpec("sync_allowed_hosts", "SYNC_ALLOWED_HOSTS", "str",
              label="同步目标允许清单",
              note="上行同步（/api/sync/*）能推到哪些主机，逗号分隔；空 = 只允许本机回环。"
              "那几条端点属使用者档，开启认证后不在此清单、也不是回环的目的地址一律 403"
              "（AUTH_MODE=off 时不分档，单机形态逐字不变）",
              group="egress"),
    # 回退去向策略（ENGI-36 C）：`local_only` 时本地档失败**不回退到云端**（这一轮明确失败）。
    # **只读**而不是在线可改：回退链在模型**构造期**烧进实例（`with_fallbacks`），而解析器的
    # 缓存键是 (主人, 后端, 温度) —— 在线改了策略，已建的那份模型链纹丝不动，标成可改就是
    # "界面显示新值、跑的是旧链"那种谎（R28-05 同族）。改 env + 重启生效（随进程构建）。
    FieldSpec("model_fallback_policy", "MODEL_FALLBACK_POLICY", "ro",
              label="回退去向（env 改后重启）",
              note="allow_cloud=本地失败可静默降级到云端（默认）/ local_only=只回退到本地档，"
              "本地不可达就让这一轮明说失败，绝不悄悄把对话送出去",
              group="egress"),
    # -- obs 观测（只读） ---------------------------------------------------------------
    # 标签里就把"今天只有 local"写出来（R28-10）：这一组是只读展示行，但把 "LangSmith Key"
    # 摆在那儿又什么都不说，等于邀请人填一个永远不生效的东西。
    FieldSpec("obs_backend", "OBS_BACKEND", "ro",
              label="观测（只实现 local）", group="obs"),
    FieldSpec("obs_emit_raw_text", "OBS_EMIT_RAW_TEXT", "ro", label="记录原文", group="obs"),
    FieldSpec("langsmith_project", "LANGSMITH_PROJECT", "ro",
              label="LangSmith 项目（未实现）", group="obs"),
    FieldSpec("langsmith_api_key", "LANGSMITH_API_KEY", "ro",
              label="LangSmith Key（未实现）", group="obs"),
    # -- 不进「运行环境」页的可编辑项（show=False；写点在别的卡片） ------------------------
    # 跨会话记忆总开关与自动提取：写点在「记忆与任务目录」的记忆卡（保存即热重建生效）。
    # 一次提取 = 一次真模型调用，所以"要不要跑、多久跑一次"必须能在界面上改 ——
    # 一个会自己花钱的开关不该只活在一个要重启才生效的 env 里。
    FieldSpec("memory_enabled", "MEMORY_ENABLED", "bool", group="memory", show=False),
    FieldSpec("memory_extract_auto", "MEMORY_EXTRACT_AUTO", "bool", group="memory", show=False),
    FieldSpec("memory_extract_turns", "MEMORY_EXTRACT_TURNS", "int", group="memory", show=False),
)

#: 保存/加载侧只认**可在线修改**的项：只读展示行（kind="ro"）显式过滤 —— 否则 PUT 提交
#: 只读键会被当合法字段收下（旧结构里它们压根不在注册表，400 是靠"查不到"得到的）。
#: 展示走 RUNTIME_FIELDS 全量，写走这两份索引：一份数据，两种视图，边界在这里。
_EDITABLE_FIELDS = tuple(f for f in RUNTIME_FIELDS if f.kind != "ro")
_FIELDS_BY_NAME = {f.field: f for f in _EDITABLE_FIELDS}
# 前端「运行环境」页按 payload 的 `key`（= env 键）提交，测试/脚本按字段名提交，
# 两者都要收 → 建 env 键索引，save_overrides 统一解析到 FieldSpec 再按字段名落库。
_FIELDS_BY_ENV = {f.env_key: f for f in _EDITABLE_FIELDS}

# **注册表自己校对那份「哪些是密钥」的名单**（`R28-14` 的②）：标成 `kind="secret"` 却没在
# `config.SECRET_FIELD_NAMES` 里登记的字段，会在**导入时**抛，而不是等某次改动把它的明文
# 送回浏览器。这一族失败一直是静默的 —— 掩码那一侧读的是另一份清单，两边都能各自自洽。
# 写成函数而不是就地把那行推导挂在这里，是为了让用例能喂一份"故意标错"的注册表进去验它真的抛。
def misdeclared_secrets(fields: tuple[FieldSpec, ...]) -> list[str]:
    """被标成密钥、却没在唯一名单里登记的字段名（正常应为空表）。"""
    return [
        spec.field
        for spec in fields
        if spec.kind == "secret" and spec.field not in SECRET_FIELD_NAMES
    ]


def _assert_secret_registry_agrees(fields: tuple[FieldSpec, ...] = RUNTIME_FIELDS) -> None:
    offenders = misdeclared_secrets(fields)
    if offenders:
        raise RuntimeError(
            "runtime_settings 把这几个字段当密钥处理，却没在 config.SECRET_FIELD_NAMES 里登记："
            f"{offenders}。两处名单必须同源 —— 加一处忘一处就是明文出进程。"
        )


_assert_secret_registry_agrees()


def _parse(spec: FieldSpec, raw: str) -> Any:
    """把界面提交的字符串解析成 Settings 字段值；不合法抛 ValueError（可读原因）。"""
    text = raw.strip()
    if spec.kind == "bool":
        if env_truthy(text):
            return True
        if env_falsy(text):
            return False
        raise ValueError(f"{spec.env_key} 只接受 开/关（1/0、true/false）")
    if spec.kind == "float":
        try:
            v = float(text)
        except ValueError as exc:
            raise ValueError(f"{spec.env_key} 必须是数字（{raw!r}）") from exc
        if v < 0:
            raise ValueError(f"{spec.env_key} 不能为负（0 = 不限）")
        return v
    if spec.kind == "int":
        try:
            v = int(text)
        except ValueError as exc:
            raise ValueError(f"{spec.env_key} 必须是整数（{raw!r}）") from exc
        if v < 0:
            raise ValueError(f"{spec.env_key} 不能为负（0 = 不裁剪）")
        # int 也能带 choices（如收件箱合并窗口只有 1/3/7）。以前 choices 只在字符串分支
        # 校验，于是"带枚举的整数字段"能写进任意值 —— 界面会显示一个没人处理得好的窗口。
        if spec.choices and str(v) not in spec.choices:
            raise ValueError(f"{spec.env_key} 只支持：{' / '.join(spec.choices)}")
        return v
    if spec.choices and text.lower() not in spec.choices:
        raise ValueError(f"{spec.env_key} 只支持：{' / '.join(spec.choices)}")
    return text.lower() if spec.choices else text


def load_overrides(conn: SqlConnection) -> dict[str, Any]:
    """读 kernel_meta 里的覆盖并按字段类型解析成 Settings 可用的值。"""
    rows = conn.execute(
        "SELECT key, value FROM kernel_meta WHERE key LIKE ?", (_PREFIX + "%",)
    ).fetchall()
    out: dict[str, Any] = {}
    for row in rows:
        field = str(row["key"])[len(_PREFIX) :]
        spec = _FIELDS_BY_NAME.get(field)
        if spec is None:
            continue  # 未知覆盖（字段已下线）：忽略而不是炸启动
        try:
            out[field] = _parse(spec, str(row["value"] or ""))
        except ValueError:
            continue  # 脏覆盖不阻断启动：回落 env，等下次保存修正
    return out


def apply_overrides(base: Settings, overrides: dict[str, Any]) -> Settings:
    """把覆盖叠到 env 之上（覆盖存在才进 update；env 值不受影响）。"""
    return base.model_copy(update=overrides) if overrides else base


def save_overrides(conn: SqlConnection, values: dict[str, str | None]) -> list[str]:
    """校验并写入覆盖。值 None/空串 = **清除**该覆盖（回落 env）。返回实际写入的字段名。

    一个 field 一行 upsert；校验失败整体抛 ValueError（调用方回 400），**不落任何一行**
    —— 半套配置比旧配置更危险。
    """
    parsed: dict[str, Any] = {}
    for key, raw in values.items():
        # 键名兼容：env 键（前端提交的 payload.key）或字段名（测试/脚本）都收，
        # 统一解析到 FieldSpec、按**字段名**落库（存储与 load_overrides 都以 field 为准）。
        spec = _FIELDS_BY_NAME.get(key) or _FIELDS_BY_ENV.get(key)
        if spec is None:
            raise ValueError(f"不支持在线修改的配置项：{key}")
        field = spec.field
        if raw is None or str(raw).strip() == "":
            parsed[field] = None  # 清除覆盖
            continue
        parsed[field] = _parse(spec, str(raw))

    for field, value in parsed.items():
        key = _PREFIX + field
        if value is None:
            conn.execute("DELETE FROM kernel_meta WHERE key = ?", (key,))
        else:
            stored = value if isinstance(value, (int, float)) else str(value)
            conn.execute(
                "INSERT INTO kernel_meta (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "  updated_at = CURRENT_TIMESTAMP",
                (key, str(stored)),
            )
    conn.commit()
    return [f for f, v in parsed.items() if v is not None]
