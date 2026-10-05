from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from rolecard_agent.config import ModelBackend
from rolecard_agent.core.model_settings.rules import (
    UNASSIGNED_USAGE,
    USAGE_DISPLAY_ORDER,
    ModelSettingsError,
    client_style,
)
from rolecard_agent.storage.db import SqlConnection


def _table_columns(conn: SqlConnection, table: str) -> set[str]:
    """这张表当前的列集合（现算，不缓存 —— 缓存就是第二份事实面）。"""
    return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


@dataclass(slots=True)
class _PreparedBackend:
    """`save()` 校验后的一行模型（还没落库）。

    存在理由是"校验一次、写两处"：同一份数据既要按端点归并成凭据组，又要逐行写进
    model_backend。用 dict 传的话每条读取都要 `str(...)`/`int(str(...))` 重申一遍类型，
    拼错键名只会静默拿到 None —— 那是给"存进去一个跑不通的配置"开门。
    """

    name: str
    endpoint: tuple[str, str | None]  # (供应商目录 id, base_url) = 凭据组的身份
    model: str
    # key 的三态：None = 这行没给（保留组里已存的）；"" = 清除；非空 = 设成它。
    api_key: str | None
    sort_order: int
    num_ctx: int | None
    supports_vision: int | None
    supports_tools: int | None


def declared_model_names(conn: SqlConnection) -> list[str]:
    """用户在模型页配过的**全部模型名**（去重、按名排序）。

    两个消费点：「运行环境」页那些动态下拉的选项源（思考名单等），以及保存时的合法性参照。
    从前这份 SELECT 长在 `api/routers/settings.py::_model_names` 里，还带一个
    `conn: object` + `# type: ignore` —— 因为路由不想 import 模型配置模块，于是把连接
    降级成 `object` 再让类型检查闭嘴：**那条注解是在藏一次越层，不是在描述形状**。
    归位到本模块之后签名是真的 `SqlConnection`，ignore 一起删掉。
    """
    rows = conn.execute(
        "SELECT DISTINCT model FROM model_backend WHERE model IS NOT NULL ORDER BY model"
    ).fetchall()
    return [str(row["model"]) for row in rows]


def _kind_of_names(conn: SqlConnection, names: list[str]) -> dict[str, str]:
    """引用行的 kind 跟随凭据组风格（native = 本地类，数据不出机）。"""
    rows = conn.execute(
        "SELECT b.name, p.provider FROM model_backend b JOIN model_provider p "
        "ON p.id = b.provider_id"
    ).fetchall()
    by_name = {str(r["name"]): str(r["provider"]) for r in rows}
    return {
        n: ("local" if client_style(by_name.get(n, "")) == "native" else "cloud") for n in names
    }


def backend_is_local(conn: SqlConnection, backend_name: str) -> bool:
    """这个后端是否跑在本机 GPU 上（provider 的客户端风格是 native）。

    2026-10-04 审查快照「提取与对话争抢本地 GPU」那条的资源闸要问的第一件事：
    "这一趟提取用谁、它吃不吃本机显存"。查不到（后端已删）= 不是本地，宁可让闸
    少拦 —— 推迟只是优化，误放行不该发生，误推迟也只是下一轮再来。
    """
    row = conn.execute(
        "SELECT p.provider FROM model_backend b JOIN model_provider p "
        "ON p.id = b.provider_id WHERE b.name = ?",
        (backend_name,),
    ).fetchone()
    return row is not None and client_style(str(row["provider"])) == "native"


# --------------------------------------------------------------------------- 服务层


#: `save()`（旧整表 `PUT /api/settings/models` 的原语）**只管**这几列。它写的是
#: `DELETE FROM model_backend` + 一份手写列清单的 INSERT —— 清单里没有的列不会被"保留"，
#: 而是跟着那行一起消失（09-26 轮 R26-01 的实测：`set_sampling` 得 repeat=1.25 freq=0.1，
#: 再 `save()` 同一行 → 两栏变 None，而同行 `num_ctx=8192` 活着）。
#: 所以其余各列一律按后端名从删之前的快照搬回来：以后再加任何一列，落进的都是
#: "未管理 ⇒ 原样保留"这条规则，而不是再补一次特例。
SAVE_MANAGED_COLUMNS = frozenset(
    {
        "name",
        "provider_id",
        "model",
        "sort_order",
        "num_ctx",
        "supports_vision",
        "supports_tools",
    }
)


def unmanaged_backend_columns(conn: SqlConnection) -> tuple[str, ...]:
    """`model_backend` 里 `save()` 不写的那些列。

    按**库里实际的形状**取（`PRAGMA table_info`）而不是抄一份清单：清单会漂，而漂了的清单
    正是这次要修的那种"漏一列不会红"的形状。
    """
    declared = {str(r["name"]) for r in conn.execute("PRAGMA table_info(model_backend)")}
    return tuple(sorted(declared - SAVE_MANAGED_COLUMNS))


def _opt_int(raw: object) -> int | None:
    """NULL / 读不出整数 → None（= 引擎默认），不猜一个数。"""
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw))
    except ValueError:
        return None


#: 两份 SELECT 里**按字面读**的列：`model` 既是 `ModelBackend` 的字段也是表列，
#: 但它就是一列字符串，不需要读取器（硬塞一个假 reader 只会多一处会骗人的地方）。
_LITERAL_COLUMNS = frozenset({"model"})

#: `_value_columns` 的判据边界（`R102-28` 的口径修正）：它管的是**声明进 `ModelBackend`
#: 的值列**（"表列 ∩ 字段"）必须有读取器。**只加在 schema.sql 的列不走这条** —— 它们是
#: `unmanaged_backend_columns` 的范围：`save()` 不认识、原样携带、不读不写，这是仓库
#: **有意支持**的形态（R26-04 的携带机制，同文件那条 `test_save_carries_over_any_column_…`
#: 就是钉它的）。从前那句"加列必须走 `_COLUMN_READERS`"比判据宽，红在宣称不在行为。


def _value_columns(conn: SqlConnection) -> tuple[str, ...]:
    """两份 SELECT 的列清单由这里算，不抄清单（抄了就会漂）。

    交集正好排掉两类不属于值列的东西：`name`/`provider_id` 是行身份与组引用（不在
    `ModelBackend` 里），`provider`/`base_url`/`api_key`/`usage` 来自凭据组或是派生值
    （不在 `model_backend` 表里）。判据只对这份交集成立（`R102-28`：宣称收窄到与判据同宽）
    —— 加进声明却漏读取器会在这里当场抛；**只在 schema.sql 里加的列不走这条**（未管理列）。
    """
    declared = set(ModelBackend.model_fields) - _LITERAL_COLUMNS
    table_columns = _table_columns(conn, "model_backend")
    columns = tuple(c for c in sorted(table_columns) if c in declared)
    missing = sorted(set(columns) - set(_COLUMN_READERS))
    if missing:
        raise ModelSettingsError(
            f"`ModelBackend` 声明了 {missing}，但 `_COLUMN_READERS` 里没写怎么读它。"
            " 声明为值列就必须同时补一个读取器 —— 否则那一列会一路静默读成 None"
            "（09-26 轮 S-1）。**只加在 schema.sql、不进 `ModelBackend` 的列不走这条**："
            "那是未管理列（`unmanaged_backend_columns`），原样携带、不读不写（`R102-28`）。"
        )
    return columns


#: 「这一行模型属于谁」的判定式：`model_backend` 不挂归属列（主人从它所属的凭据组继承，
#: 与那几张"主人跟着父行走"的表同一条纪律），所以按名改一行的 UPDATE 一律带上它。
#: 为什么写在 SQL 里而不是"先查一遍再决定改不改"：那两处代码会漂，而漂了的症状是
#: "按名改到了别人的行"（记忆那侧已经数过一次同样的错）。
_OWNED_MODEL_ROWS = "provider_id IN (SELECT id FROM model_provider WHERE user_id = ?)"


def _derived_usage(categories: list[str]) -> str:
    """引用类别 → 过渡期的 `usage` 值：chat 优先，其次优先级最高的那个类别。"""
    for candidate in USAGE_DISPLAY_ORDER:
        if candidate in categories:
            return candidate
    return UNASSIGNED_USAGE


def _sorted_usages(categories: list[str]) -> list[str]:
    return [c for c in USAGE_DISPLAY_ORDER if c in categories] + [
        c for c in sorted(categories) if c not in USAGE_DISPLAY_ORDER
    ]


def _tri_state(raw: object) -> bool | None:
    """列值 → 三态：NULL 保持 null（"没测过"），0/1 → 明确 bool。不猜：猜就是第二个事实面。"""
    if raw is None or raw == "":
        return None
    return bool(raw)


def _opt_float(raw: object) -> float | None:
    """采样惩罚那一列 → `float | None`。认不出的一律当"没设"（None）而不是 0。

    0 在这三栏里是一个**有语义的值**（frequency_penalty=0 = 明确关掉惩罚），所以坏值不能
    退成 0 —— 退成 None 才是"不传这个参数、听引擎的"。
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _vision_of(raw: object) -> bool:
    """运行时语义：未探测 = 不支持视觉（拆层前的列默认值，行为不变）。"""
    return bool(raw) if raw is not None else False


def _tools_of(raw: object) -> bool:
    """运行时语义：未探测 = 支持工具（与 P1-2"只拦确定的否"同一条纪律）。"""
    return bool(raw) if raw is not None else True


#: "值由 `ModelBackend` 声明、又真的存在 `model_backend` 表里"的那些列**怎么读成运行时值**。
#: 读取器**缺一个就当场抛**（`_value_columns`），而不是让那一列静默变成 None —— 这就是
#: S-1 的全部目的：从前加一列要手写 7 处，漏在读侧的那一处不会红，症状是"设了但看不见"。
#: 这份清单**有两个消费点**：`_value_columns` 拿它校验"有没有漏"，`_backend_from_row` 拿它
#: 真的读值。两份语义刻意分开的另一半是界面的**三态**（`list_providers` 显式调 `_tri_state`
#: 把"没测过"显示成 `?`）：这里给的是运行时二态，合并的话"没测过"会被当成"不支持"，
#: 而"不支持"会触发调用前拦截。
_COLUMN_READERS: dict[str, Callable[[Any], Any]] = {
    "num_ctx": _opt_int,
    "supports_vision": _vision_of,
    "supports_tools": _tools_of,
    "repeat_penalty": _opt_float,
    "frequency_penalty": _opt_float,
    "presence_penalty": _opt_float,
}


def _backend_from_row(row: Mapping[str, object]) -> ModelBackend:
    """一行（`_raw_backends` 那份 JOIN 视图）→ `ModelBackend`（S-1）。

    值列**不抄清单**：加一列只改 `schema.sql` 声明 + 在 `_COLUMN_READERS` 里补一个读取器，
    这里自动跟上。从前这段手写十个字段，漏一个的症状是"设了但跑起来看不见"，而且不会红
    （`_value_columns` 只管 SELECT 那一侧，管不到这个构造）。
    """
    fields: dict[str, object] = {
        "model": str(row["model"]),
        "base_url": row.get("base_url"),
        "api_key": row.get("api_key"),
        "provider": str(row["provider"]),
        "usage": str(row["usage"]),
    }
    for column, read in _COLUMN_READERS.items():
        fields[column] = read(row.get(column))
    return ModelBackend(**fields)  # type: ignore[arg-type]


def _capability_of(
    item: dict[str, object], field: str, stored: dict[str, object] | None, *, default: bool
) -> int | None:
    """能力位的三态写入：没提交这一列时，**库里是什么就还是什么**。

    库里是 NULL（没测过）→ 继续 NULL。把"没测过"在一次无关的保存里悄悄写成"不支持"，
    界面上就从 `?` 变成 `✗`，而 `✗` 会触发调用前拦截 —— 那不是"保留原状"，是改了行为。
    只有库里根本没有这行（新增模型）才落到出厂默认（视觉不支持 / 工具支持）。
    """
    if field in item and item[field] is not None:
        return int(bool(item[field]))
    if stored is None:
        return int(default)
    raw = stored.get(field)
    return None if raw is None else int(bool(raw))
