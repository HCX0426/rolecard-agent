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

from rolecard_agent.config import Settings
from rolecard_agent.storage.db import SqlConnection

_PREFIX = "runtime:"


@dataclass(frozen=True)
class FieldSpec:
    """一个可编辑覆盖项：Settings 字段名、env 键、类型与可选枚举。

    `choices_from="models"`：下拉选项**动态**来自用户配置的模型名（model_backend 表），
    而不是写死的枚举 —— 用户加了新模型，选项自动跟上。
    `choices_from="backends"`：选项是**后端名**（`Settings.model_backends` 的键）。
    两者不是一回事，别混：`resolve_role_model(后端名)` 认的是键，而"模型列"可以重名
    （两个后端都叫 qwen3:8b）。填错在这里是**静默**回落默认模型 ⇒ 症状又变回
    "记忆悄悄提不出来"，所以这类字段必须给下拉，不能让人手打。
    """

    field: str
    env_key: str
    kind: str  # bool | secret | float | int | str
    choices: tuple[str, ...] | None = None
    choices_from: str | None = None


RUNTIME_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("web_search_enabled", "WEB_SEARCH_ENABLED", "bool"),
    FieldSpec("web_allowed_domains", "WEB_ALLOWED_DOMAINS", "str"),
    FieldSpec("web_search_backend", "WEB_SEARCH_BACKEND", "str", ("auto", "tavily", "ddgs", "off")),
    FieldSpec("tavily_api_key", "TAVILY_API_KEY", "secret"),
    FieldSpec("saucenao_api_key", "SAUCENAO_API_KEY", "secret"),
    # OCR / 嵌入 / 重排"用哪个后端"**不在此列**：那是「服务」页的端点序（运行期唯一事实面）。
    # 它们曾在这里各挂一条"可改"，而工厂里对应的 env 分支在生产上从不执行（内置行恒在），
    # 于是保存=什么都不发生 —— 随架构审计报告 P1-5 一并收口。
    FieldSpec("extract_backend", "EXTRACT_BACKEND", "str"),
    FieldSpec("extract_verify", "EXTRACT_VERIFY", "str"),
    # 「提取精华」用哪个后端（空 = 跟随会话/角色）。它单独一条是因为实测：本地 8B 在同一段
    # 对话上提得出 0 条而云端提得出 8 条 —— 让留在本地的角色"能记住"不必先把对话搬上云端
    # （审计 §12.5）。填了才出网，不填行为与今天一致。
    # 为什么是"backends"而不是"models"：这里存的是 `resolve_role_model()` 认的**后端名**
    # （`Settings.model_backends` 的键），而模型列可以重名 —— 手打错了不会报错，只会静默
    # 回落默认模型，症状正好是这次要修的"记忆悄悄提不出来"。
    FieldSpec(
        "memory_extract_backend", "MEMORY_EXTRACT_BACKEND", "str", choices_from="backends"
    ),
    FieldSpec("model_thinking", "MODEL_THINKING", "str", ("auto", "off")),
    FieldSpec("model_thinking_models", "MODEL_THINKING_MODELS", "str", choices_from="models"),
    FieldSpec("model_timeout_seconds", "MODEL_TIMEOUT_SECONDS", "float"),
    FieldSpec("tool_timeout_seconds", "TOOL_TIMEOUT_SECONDS", "float"),
    FieldSpec("context_max_chars", "CONTEXT_MAX_CHARS", "int"),
    # 多模型比对总闸：关 = `compare_model_answers` 一律返回关闭说明（一次 = N 次真调用，
    # 且同一问题会发给多个供应商；成本与隐私同一类，见 core/consensus.py）。
    FieldSpec("consensus_enabled", "CONSENSUS_ENABLED", "bool"),
    # 跨会话记忆总开关：「设置→通用」记忆面板的开关走这里保存（保存即热重建生效）。
    FieldSpec("memory_enabled", "MEMORY_ENABLED", "bool"),
    # 自动提取那条兜底路：一次提取 = 一次真模型调用，所以"要不要跑、多久跑一次"必须能在
    # 界面上改 —— 一个会自己花钱的开关不该只活在一个要重启才生效的 env 里。
    # 与 memory_enabled 同例：**写点在「记忆与任务目录」的记忆卡**，运行环境页不列这两行。
    FieldSpec("memory_extract_auto", "MEMORY_EXTRACT_AUTO", "bool"),
    FieldSpec("memory_extract_turns", "MEMORY_EXTRACT_TURNS", "int"),
    # Agent 模式全局默认：「运行环境」页保存即热重建；会话级切换覆盖它（对话页）。
    FieldSpec("agent_default_mode", "AGENT_DEFAULT_MODE", "str", ("chat", "agent")),
    # 角色主动开口全局总闸：「运行环境」页保存即热生效（调度每 tick 读当前值）。
    FieldSpec("reachout_enabled", "REACHOUT_ENABLED", "bool"),
    # 开口间隔（分钟）：调度每 tick 读当前值，所以在线可改。以前只能改 env + 重启，
    # 排查"它为什么不开口"时没法临时调小复现（架构计划 §5.2 的抑制层之一）。
    FieldSpec("reachout_interval_minutes", "REACHOUT_INTERVAL_MINUTES", "int"),
    # 收件箱一摞合并成一行覆盖几天。**只有三档**（决策点 A）：连续滑块会产出
    # "合并 5 天"这种没人能预判界面长什么样的取值，而 1/3/7 正好对应"今天/这周/最近一段"。
    # 写在 RUNTIME_FIELDS 是为了复用覆盖存储与热重建，**可写入口不在运行环境页**
    # （那里只读展示），单一写点在「记忆与任务目录」的主动开口卡 —— 同 reachout_enabled。
    FieldSpec("reachout_merge_days", "REACHOUT_MERGE_DAYS", "int", ("1", "3", "7")),
    # 文件事件触发（架构计划 C·§5.2）全局闸：开 = 每 tick 轮询任务目录做素材门控开口。
    FieldSpec("file_watch_enabled", "FILE_WATCH_ENABLED", "bool"),
    # 命令执行（架构计划 C·§6.2）：总闸 + 审批档都允许在线热切（工具每调用读 settings）。
    FieldSpec("run_tools_enabled", "RUN_TOOLS_ENABLED", "bool"),
    FieldSpec("run_approval", "RUN_APPROVAL", "str", ("manual", "auto")),
)

_FIELDS_BY_NAME = {f.field: f for f in RUNTIME_FIELDS}
# 前端「运行环境」页按 payload 的 `key`（= env 键）提交，测试/脚本按字段名提交，
# 两者都要收 → 建 env 键索引，save_overrides 统一解析到 FieldSpec 再按字段名落库。
_FIELDS_BY_ENV = {f.env_key: f for f in RUNTIME_FIELDS}


def spec_of(field: str) -> FieldSpec | None:
    """查字段的可编辑规格；不可在线修改的字段返回 None（界面据此渲染只读）。"""
    return _FIELDS_BY_NAME.get(field)


def _parse(spec: FieldSpec, raw: str) -> Any:
    """把界面提交的字符串解析成 Settings 字段值；不合法抛 ValueError（可读原因）。"""
    text = raw.strip()
    if spec.kind == "bool":
        lowered = text.lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
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
