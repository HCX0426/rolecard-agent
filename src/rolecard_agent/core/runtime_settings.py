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
    """

    field: str
    env_key: str
    kind: str  # bool | str | secret | float | int
    choices: tuple[str, ...] | None = None
    choices_from: str | None = None


RUNTIME_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("web_search_enabled", "WEB_SEARCH_ENABLED", "bool"),
    FieldSpec("web_allowed_domains", "WEB_ALLOWED_DOMAINS", "str"),
    FieldSpec("web_search_backend", "WEB_SEARCH_BACKEND", "str", ("auto", "tavily", "ddgs", "off")),
    FieldSpec("tavily_api_key", "TAVILY_API_KEY", "secret"),
    FieldSpec("ocr_backend", "OCR_BACKEND", "str", ("auto", "paddle", "cloud")),
    FieldSpec("ocr_api_key", "OCR_API_KEY", "secret"),
    FieldSpec("ocr_api_url", "OCR_API_URL", "str"),
    FieldSpec(
        "embedding_backend", "RAG_EMBEDDING", "str",
        ("auto", "siliconflow", "chroma_default", "hash"),
    ),
    FieldSpec("rag_rerank", "RAG_RERANK", "str", ("auto", "off", "siliconflow")),
    FieldSpec("extract_backend", "EXTRACT_BACKEND", "str"),
    FieldSpec("extract_verify", "EXTRACT_VERIFY", "str"),
    FieldSpec("model_thinking", "MODEL_THINKING", "str", ("auto", "off")),
    FieldSpec("model_thinking_models", "MODEL_THINKING_MODELS", "str", choices_from="models"),
    FieldSpec("model_timeout_seconds", "MODEL_TIMEOUT_SECONDS", "float"),
    FieldSpec("tool_timeout_seconds", "TOOL_TIMEOUT_SECONDS", "float"),
    FieldSpec("context_max_chars", "CONTEXT_MAX_CHARS", "int"),
    # 跨会话记忆总开关：「设置→通用」记忆面板的开关走这里保存（保存即热重建生效）。
    FieldSpec("memory_enabled", "MEMORY_ENABLED", "bool"),
)

_FIELDS_BY_NAME = {f.field: f for f in RUNTIME_FIELDS}


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
    for field, raw in values.items():
        spec = _FIELDS_BY_NAME.get(field)
        if spec is None:
            raise ValueError(f"不支持在线修改的配置项：{field}")
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
