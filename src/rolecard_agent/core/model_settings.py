"""Runtime-editable model configuration - the data layer behind the settings page.

Why a DB table instead of env-only: `MODEL_BACKENDS` (config.py) is a deploy-time contract.
The settings page needs an OPERATOR-time contract: add a SiliconFlow/OpenAI-compatible
endpoint, set the default, and have the next conversation turn use it WITHOUT restarting.
Env stays the bootstrap truth; the first settings save takes over (see `effective_settings`).

## 为什么是两层（2026-09-20 拆层）

一张表混装两层时，同一把 key 抄在每一行上（`siliconflow` + `siliconflow-vl` + …），于是
"改一次 key 要改 N 处"，漏一处就是"部分模型突然 401"（用户："为啥不用供应商和模型名组成
一个键"）。拆完之后：

  * `model_provider` = 凭据层：一组 = 一个 (供应商, base_url) 端点，**key 只有一个家**；
  * `model_backend`  = 模型层：一行 = 一个可调用模型，指向某个组。`name` 仍是主键，
    `session_thread.model_name` / `role_card.model_name` 都指着它 —— 拆层不能断这条链。

  * **用途也不在行上了**：一行服务谁 = `service_endpoint` 里有没有指向它的引用行（含
    category='chat'）。所以 `usage` 是**派生只读**值，唯一的写入口在「服务」页签。

读侧形状不变：`raw_backends` / `list_backends` / `effective_settings` 仍给出带
`provider`/`base_url`/`api_key`/`usage` 的"后端行"（JOIN + 派生补齐），所以内核、模型工厂、
rag 与 services 一行不改。变的是写入与迁移：这些字段不再有第二处副本。

两条规则值得单独说：

  * **API keys are write-only over the wire.** 读接口从不返回 key（只有 `has_key` + 掩码），
    浏览器会话因此永远读不到明文。`save` 把省略/None 的 `api_key` 理解为"保留该组已存的"、
    空串理解为"清除" —— 否则每次没重输 key 的保存都会把 key 抹掉。
  * **Plaintext at rest, stated rather than hidden.** Keys live in the local demo SQLite
    file, which never leaves the machine. Production would move to a secret manager - that
    is a v2 concern, and pretending otherwise in a demo would be worse than the limitation.
  * **一组凭据有主人（M2d）**：`model_provider.user_id` 是这一行的归属，所以本模块每个读写
    都要调用方交出 `user_id`。运行期"花谁的 key"只有一个答案 —— 交出这份 `Settings` 的那个人
    （`effective_settings(..., user_id=)` 是唯一咽喉）。刻意**留着不分身份**的只有两类，各自
    写明原因：分配主键（`_all_group_ids` / `_all_backend_names`，主键是全局的）、启动时的数据
    卫生清扫（`normalize_providers`）。`service_endpoint` 的 chat 引用行**也按人**
    （多租户 B1b，方案 A 收了 §4.1 的尾巴：对话默认/回退链花谁的 key 由谁定），能力端点
    （ocr/embedding/rerank）仍设备级、归 `core/services.py` 管。`model_backend` 因此**不另挂
    一列**：模型行的主人从它所属的组继承，按名改一行的那些 UPDATE 靠 JOIN 带上主人条件，
    而不是多存一份冗余归属。
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from rolecard_agent.config import (
    DEFAULT_SILICONFLOW_BASE_URL,
    MAX_FALLBACKS,
    ModelBackend,
    Settings,
)
from rolecard_agent.storage.db import SqlConnection, quote_ident


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


class ModelSettingsError(Exception):
    """A settings write that would produce an unusable configuration. Message is user-safe."""


def validate_base_url(value: str | None) -> str | None:
    """归一并校验 base_url：仅接受 http/https，允许 localhost/私网（自用场景 Ollama 需要）。

    拒绝 file:///gopher 等异常 scheme 与无 scheme 的裸串，避免后续 httpx 把内容当请求发走
    （M8：base_url 此前无校验，错误地址可作内网探测入口）。空值视作"留空/自动"（如 Ollama）。
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if "://" not in lowered:
        raise ModelSettingsError("base_url 必须包含协议（如 http:// 或 https://）。")
    parsed = urlparse(lowered)
    if parsed.scheme not in ("http", "https"):
        raise ModelSettingsError(f"base_url 仅支持 http/https，不支持 {parsed.scheme}://")
    if not parsed.hostname:
        raise ModelSettingsError("base_url 缺少主机名。")
    return text


# Backend names become keys in MODEL_BACKENDS-merged maps and UI list items: keep them tame.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# 供应商目录：设置页「模型」页签的下拉不再写死前端，改由后端提供（动态扩展）。
# id 是**供应商身份**（界面分组/展示用），style 才是运行时客户端风格：
#   native = Ollama 原生端点（base_url 不带 /v1）；openai = OpenAI 兼容（带 /v1）。
# 两者刻意分离：同一"OpenAI 兼容"风格下有很多厂商（SiliconFlow/DeepSeek/…），把厂商
# 写进 provider 才能让界面正确显示"硅基流动"，而不是一句无意义的"openai"。
MODEL_PROVIDERS: tuple[dict[str, object], ...] = (
    {"id": "ollama", "label": "本地 Ollama", "needs_key": False,
     "base_url_hint": "http://localhost:11434（可留空）", "style": "native",
     "default_base_url": "http://localhost:11434"},
    {"id": "openai", "label": "OpenAI 兼容", "needs_key": True,
     "base_url_hint": "https://api.openai.com/v1", "style": "openai",
     "default_base_url": "https://api.openai.com/v1"},
    {"id": "siliconflow", "label": "硅基流动", "needs_key": True,
     # 端点不写第二遍（`R28-14`）：这格曾经是该 URL 在 src 里的第二处字面量，而它就在
     # `default_base_url` 隔壁一行 —— 换端点时静静留下一句过期的占位。由 single-source
     # literals 那条门禁看着。
     "base_url_hint": DEFAULT_SILICONFLOW_BASE_URL, "style": "openai",
     "default_base_url": DEFAULT_SILICONFLOW_BASE_URL},
    {"id": "deepseek", "label": "DeepSeek", "needs_key": True,
     "base_url_hint": "https://api.deepseek.com/v1", "style": "openai",
     "default_base_url": "https://api.deepseek.com/v1"},
)

# 留空 base_url 的厂商 = 用它自己的默认端点。分组必须按**归一后的端点**算：否则
# "硅基流动 + 留空" 与 "硅基流动 + 显式 URL" 会成两个组，于是那把 key 又有了两个家 ——
# 正是拆层要消灭的东西（用户在旧界面加第二个模型时不重填 URL，这条路径天天会走到）。
_DEFAULT_BASE_URLS = {str(p["id"]): str(p["default_base_url"]) for p in MODEL_PROVIDERS}

# 历史 alias：旧数据/旧配置里的 "local" 一律视作 ollama（不再作为可选供应商出现）。
PROVIDER_ALIASES = {"local": "ollama"}

KEYLESS_PROVIDERS = frozenset({"ollama", "local"})

# 一行模型可以参与的服务。模型页**不再选用途**：它与其余三类一样是「服务」页签的一条引用
# 行，模型页只读回显 `used_by`（用途只有一个事实面）。
BACKEND_USAGES = frozenset({"chat", "embedding", "rerank", "ocr"})

# 「模型推理」在 service_endpoint 里的类别键。它刻意**不进** `SERVICE_CATEGORIES`（那三类各有
# 内置本地行、服务页可自由增删引用）：chat 引用只由本模块写，通用 REST 面
# （/api/services/{key}/...）因此碰不到它 —— 少一个能写出半套语义的入口。
CHAT_CATEGORY = "chat"

# `used_by` 的展示序：对话在前，其余按服务页的出现顺序。
USAGE_DISPLAY_ORDER = ("chat", "embedding", "rerank", "ocr")

# 没有被任何服务引用的一行模型。曾经它叫 "chat"（旧列的默认值），但那是撒谎：没人用它，
# 而消费方（对话页/角色页的后端下拉）会因为它是 "chat" 把它列进去。"还没配用途"是个真实
# 状态（刚添加的模型就是还没被任何服务引用），所以它需要自己的值。
UNASSIGNED_USAGE = "unassigned"


def _canonical(provider: str) -> str:
    p = (provider or "").strip().lower()
    return PROVIDER_ALIASES.get(p, p)


def normalize_provider(provider: str, base_url: str | None = None) -> str:
    """把历史/风格性 provider 值归一到供应商目录 id。

    此前云端种子只记端点风格（SiliconFlow 存成 "openai"），界面因此显示错误的供应商。
    这里按 alias 折叠 + base_url 厂商特征推断；识别不出就原样保留（自定义网关仍算 openai）。
    """
    p = _canonical(provider)
    url = (base_url or "").lower()
    if p in ("", "openai"):
        if "siliconflow" in url:
            return "siliconflow"
        if "deepseek" in url:
            return "deepseek"
    return p or "openai"


def client_style(provider: str) -> str:
    """供应商 id → 运行时客户端风格：native 走 Ollama 原生，其余走 OpenAI 兼容。

    `init_chat_model` 只认 "ollama"/"openai" 两类 provider；目录化之后界面上的
    siliconflow/deepseek 都映射到 openai 兼容客户端（base_url 指向各自厂商）。
    """
    p = _canonical(provider)
    for entry in MODEL_PROVIDERS:
        if entry["id"] == p:
            return str(entry["style"])
    return "openai"


def is_keyless_provider(provider: str) -> bool:
    """本地类 provider（Ollama 及其别名 local）不需要 api_key。"""
    return _canonical(provider) in KEYLESS_PROVIDERS


def provider_catalog() -> list[dict[str, object]]:
    """返回供应商目录（前端下拉用）。新增供应商只改这里，无需动前端。

    值类型是 object：`needs_key` 是布尔（`R102-16`），与其余 str 字段同表。"""
    return [dict(p) for p in MODEL_PROVIDERS]


def provider_label(provider: str) -> str:
    """供应商 id 的展示名；目录外的自定义网关显示 id 本身（不编一个假名字）。"""
    p = _canonical(provider)
    for entry in MODEL_PROVIDERS:
        if entry["id"] == p:
            return str(entry["label"])
    return p


def mask_key(raw: str | None) -> str | None:
    """回读的**掩码**密钥（如 `sk-…abcd`）；不足 9 字符全打点。

    `raw[:3]…raw[-4:]` 对 7 字符的 key 等于把整个 key 拼回来，掩码就成了回明文
    （审查报告 L2）。与 `core/services._mask_key` 同一纪律（那里是引用行的快照）。
    """
    if not raw:
        return None
    text = str(raw)
    if len(text) <= 8:
        return "•" * len(text)
    return f"{text[:3]}…{text[-4:]}"


def _table_columns(conn: SqlConnection, table: str) -> set[str]:
    return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _group_id(taken: set[str], catalog: str) -> str:
    """给凭据组分配 id：目录 id 本身，重名加 `-2`/`-3` 后缀（同一协议的两个不同端点）。

    id 里带目录 id 是为了让审计日志与界面文案仍然认得出供应商。
    """
    if catalog not in taken:
        taken.add(catalog)
        return catalog
    n = 2
    while f"{catalog}-{n}" in taken:
        n += 1
    gid = f"{catalog}-{n}"
    taken.add(gid)
    return gid


def _dedupe(names: list[str], allowed: set[str]) -> list[str]:
    """按首次出现去重，丢掉不在 allowed 里的（env/历史默认值可能指向一个不存在的名字）。"""
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in allowed and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def endpoint_key(provider: str, base_url: object) -> tuple[str, str | None]:
    """凭据组的身份 = (供应商目录 id, **归一后的**端点)。

    留空的 base_url 折叠到该厂商的默认端点（运行时也是这么兜底的），所以"没填"与"填了
    默认值"是同一个组。自定义网关不在目录里，留空就自成一组（它本来也没有可兜底的端点）。
    """
    catalog = normalize_provider(provider, str(base_url) if base_url else None)
    base = str(base_url).strip() if base_url else ""
    return (catalog, base or _DEFAULT_BASE_URLS.get(catalog) or None)


# --------------------------------------------------------------------------- 搬层迁移


def migrate_to_provider_layers(conn: SqlConnection) -> int:
    """旧形态（每行自带 provider/base_url/api_key/usage）→ 两层 + chat 引用行。幂等。

    由 `storage/db.py::_migrate` 在建表之后调用（那一步已把历史缺列补齐）。判据是
    `model_backend.provider_id` 在不在：不在 = 旧库，搬；在 = 已搬过或本来就是新库。

    三件事必须同时做，否则配置会**变小**（用户最不能接受的一种错）：

      1. 同一 (供应商, base_url) 的行的 key 归并到一条 `model_provider`（取第一个非空 key
         —— 它们本就是同一把 key 的副本）；
      2. `usage='chat'` 的行换成 chat 引用行，**顺序照旧**（默认第 1 位，其后回退链），
         否则拆一次层对话默认就换了个模型；
      3. 删掉 kernel_meta 的 `model_default`/`model_fallbacks` —— 它们的事实面已经搬进
         引用行的顺序，留着就是第二个家（下次读谁？没有答案）。

    返回搬出的凭据组数（0 = 无需搬）。
    """
    cols = _table_columns(conn, "model_backend")
    if not cols or "provider_id" in cols:
        return 0
    rows = conn.execute(
        "SELECT name, provider, base_url, model, api_key, usage, sort_order, num_ctx, "
        "supports_vision, supports_tools FROM model_backend ORDER BY sort_order, name"
    ).fetchall()

    groups: dict[tuple[str, str | None], dict[str, object]] = {}
    taken: set[str] = set()
    group_of_name: dict[str, str] = {}
    for row in rows:
        base = str(row["base_url"]) if row["base_url"] else None
        catalog = normalize_provider(str(row["provider"]), base)
        key = endpoint_key(catalog, base)
        group = groups.get(key)
        if group is None:
            group = {
                "id": _group_id(taken, catalog),
                "provider": catalog,
                "base_url": base,
                "api_key": str(row["api_key"]) if row["api_key"] else None,
            }
            groups[key] = group
        else:
            if not group["api_key"] and row["api_key"]:
                group["api_key"] = str(row["api_key"])
            if not group["base_url"] and base:
                group["base_url"] = base  # 存显式端点，比"留空靠默认兜底"更少一层推断
        group_of_name[str(row["name"])] = str(group["id"])

    for order, group in enumerate(groups.values()):
        # 不写 `user_id`：这些是从**旧库**搬上来的行，旧库里没有归属这回事，所以让它们落进
        # 列默认值指向的那个身份（与补列器给老行回填的是同一个，见 storage/db.py）。
        conn.execute(
            "INSERT OR IGNORE INTO model_provider (id, provider, base_url, api_key, sort_order) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(group["id"]),
                str(group["provider"]),
                group["base_url"],
                group["api_key"],
                order,
            ),
        )

    # 前置 DROP（`R102-27`，与 db.py 同族三处的同一个理由 R28-15）：这一步死在半路，
    # 残留的暂存表会让下次启动的 CREATE 报 `already exists` —— 整个库再也打不开。
    # DROP IF EXISTS 让这一步可重放：INSERT 是单句原子，重跑从源表整表重灌，数据不丢。
    # 迁移事件落 stderr（`R102-64`）：搬层是唯一没有可追溯事件的迁移路径。
    print(
        "[schema-migrate] model_backend 搬层开始（provider 两层化，暂存表 __layers）",
        file=sys.stderr,
        flush=True,
    )
    conn.execute("DROP TABLE IF EXISTS model_backend__layers")
    conn.execute(
        "CREATE TABLE model_backend__layers ("
        " name TEXT PRIMARY KEY,"
        " provider_id TEXT NOT NULL REFERENCES model_provider(id),"
        " model TEXT NOT NULL,"
        " sort_order INTEGER NOT NULL DEFAULT 0,"
        " num_ctx INTEGER,"
        " supports_vision INTEGER,"
        " supports_tools INTEGER,"
        # 与 `core/schema.sql` 里那张表**必须同形**：搬完层读侧就按新形状查了。
        # （采样惩罚三栏 2026-09-24 加进来时，漏在这里的代价是旧库一搬层就
        #  "no such column: b.repeat_penalty"。）
        " repeat_penalty REAL,"
        " frequency_penalty REAL,"
        " presence_penalty REAL)"
    )
    conn.executemany(
        "INSERT INTO model_backend__layers "
        "(name, provider_id, model, sort_order, num_ctx, supports_vision, supports_tools) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                str(row["name"]),
                group_of_name[str(row["name"])],
                str(row["model"]),
                int(str(row["sort_order"])),
                row["num_ctx"],
                row["supports_vision"],
                row["supports_tools"],
            )
            for row in rows
        ],
    )
    conn.execute("DROP TABLE model_backend")
    conn.execute("ALTER TABLE model_backend__layers RENAME TO model_backend")

    # usage → 引用行：默认与回退链的顺序照搬，其余 chat 行按原序跟在后面。
    legacy_default = conn.execute(
        "SELECT value FROM kernel_meta WHERE key = 'model_default'"
    ).fetchone()
    legacy_chain = conn.execute(
        "SELECT value FROM kernel_meta WHERE key = 'model_fallbacks'"
    ).fetchone()
    chain: list[str] = []
    if legacy_chain and legacy_chain["value"]:
        with contextlib.suppress(ValueError):
            chain = [str(x) for x in json.loads(str(legacy_chain["value"]))]
    head = [str(legacy_default["value"])] if legacy_default and legacy_default["value"] else []
    chat_names = [str(row["name"]) for row in rows if str(row["usage"]) == "chat"]
    ordered = _dedupe([*head, *chain, *chat_names], set(chat_names))
    kinds = _kind_of_names(conn, ordered)
    for order, name in enumerate(ordered):
        # `user_id` 显式写本机主人（默认部署 = 'local-user'）：这些是从**旧库**搬上来的
        # chat 引用行，旧库里没有归属这回事 —— 让它们落进"列默认值指向的身份"（与搬进来的
        # model_provider 同一口径，见 storage/db.py 的补列回填）。能力类别的引用行这里
        # 不存在：搬层只写 chat（原 usage 列只有 chat 语义进对话序列）。
        conn.execute(
            "INSERT OR IGNORE INTO service_endpoint "
            "(category, id, kind, ref_backend, enabled, sort_order, builtin, user_id) "
            "VALUES (?, ?, ?, ?, 1, ?, 0, 'local-user')",
            (CHAT_CATEGORY, name, kinds.get(name, "cloud"), name, order),
        )
    conn.execute("DELETE FROM kernel_meta WHERE key IN ('model_default', 'model_fallbacks')")
    conn.commit()
    return len(groups)


def _kind_of_names(conn: SqlConnection, names: list[str]) -> dict[str, str]:
    """引用行的 kind 跟随凭据组风格（native = 本地类，数据不出机）。"""
    rows = conn.execute(
        "SELECT b.name, p.provider FROM model_backend b JOIN model_provider p "
        "ON p.id = b.provider_id"
    ).fetchall()
    by_name = {str(r["name"]): str(r["provider"]) for r in rows}
    return {
        n: ("local" if client_style(by_name.get(n, "")) == "native" else "cloud")
        for n in names
    }


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


def _value_columns(conn: SqlConnection) -> tuple[str, ...]:
    """两份 SELECT 的列清单由这里算，不抄清单（抄了就会漂）。

    交集正好排掉两类不属于值列的东西：`name`/`provider_id` 是行身份与组引用（不在
    `ModelBackend` 里），`provider`/`base_url`/`api_key`/`usage` 来自凭据组或是派生值
    （不在 `model_backend` 表里）。
    """
    declared = set(ModelBackend.model_fields) - _LITERAL_COLUMNS
    columns = tuple(c for c in sorted(_table_columns(conn, "model_backend")) if c in declared)
    missing = sorted(set(columns) - set(_COLUMN_READERS))
    if missing:
        raise ModelSettingsError(
            f"model_backend 有了新列 {missing}，但 `_COLUMN_READERS` 里没写怎么读它。"
            " 加一列只改 schema.sql 声明就够（补列器接管），读侧必须同时补一个读取器 ——"
            " 否则那一列会一路静默读成 None。（09-26 轮 S-1）"
        )
    return columns


#: 「这一行模型属于谁」的判定式：`model_backend` 不挂归属列（主人从它所属的凭据组继承，
#: 与那几张"主人跟着父行走"的表同一条纪律），所以按名改一行的 UPDATE 一律带上它。
#: 为什么写在 SQL 里而不是"先查一遍再决定改不改"：那两处代码会漂，而漂了的症状是
#: "按名改到了别人的行"（记忆那侧已经数过一次同样的错）。
_OWNED_MODEL_ROWS = "provider_id IN (SELECT id FROM model_provider WHERE user_id = ?)"


class ModelSettingsService:
    MODEL_SEEDED_KEY = "model_backends_seeded"

    def __init__(self, conn: SqlConnection) -> None:
        self._conn = conn

    # -- reads -----------------------------------------------------------------
    #
    # 每个读都要求调用方交出 `user_id`，因为这一层的行有主人（M2d，§4.1「key 跟人走」）。
    # 只有少数几处**刻意不分身份**，它们都在下面单独标了原因（主键分配、启动清扫）——
    # 那种地方必须是"另一个具名方法"，不能是同一个方法传个 None：一旦 None 表示"全部"，
    # "忘了过滤"就又变成一次普通的调用了。
    # `service_endpoint` 那一族不再例外（多租户 B1b，方案 A 收掉了 §4.1 的尾巴）：其中
    # `chat` 引用行的默认/回退序列**按人**（`default_backend` / `list_fallbacks` /
    # `_chat_ref_names` / `_usages` 的 chat 桶都要 `user_id`）；ocr/embedding/rerank 能力
    # 端点保持设备级（那些读在 `services.py`，不归本类）。

    def _provider_rows(self, *, user_id: str) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT id, user_id, provider, label, base_url, api_key, sort_order "
            "FROM model_provider WHERE user_id = ? ORDER BY sort_order, id",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _all_group_ids(self) -> set[str]:
        """**全部**身份的组键，只用来分配新键（`_group_id` 的那个 `taken` 集合）。

        为什么全局：`model_provider.id` 是全局主键，两个身份各建一个硅基流动组时，若各自
        从 `siliconflow` 起编号就是 INSERT 撞主键（一个 500，且第二次永远建不成）。
        代价是编号会跳过别人占掉的那几个 —— 而看不见别人的组，也就看不见那些编号。
        """
        return {
            str(r["id"])
            for r in self._conn.execute("SELECT id FROM model_provider").fetchall()
        }

    def _all_provider_rows(self) -> list[dict[str, object]]:
        """全部身份的凭据组 —— 只有启动时那次数据卫生清扫用它（`normalize_providers`）。
        任何"给某人看"或"替某人改"的路径都不许走这里，它们走 `_provider_rows(user_id=)`。
        """
        rows = self._conn.execute(
            "SELECT id, user_id, provider, label, base_url, api_key, sort_order "
            "FROM model_provider ORDER BY sort_order, id"
        ).fetchall()
        return [dict(r) for r in rows]

    def _all_backend_names(self) -> set[str]:
        """同 `_all_group_ids`：`model_backend.name` 也是全局主键（session/角色卡引用它）。"""
        return {
            str(r["name"])
            for r in self._conn.execute("SELECT name FROM model_backend").fetchall()
        }

    def _usages(self, *, user_id: str) -> dict[str, list[str]]:
        """后端名 → 引用它的服务类别（chat 在前，其余按优先级序）。

        这就是"用途"的唯一事实面：服务页写引用行，模型页只读这张映射。
        归属切分（多租户 B1b，方案 A）：**chat 引用行只在本人的那几条里找**（"这行用于
        对话"是"花谁的 key 由谁定"同族的事实）；能力类别（ocr/embedding/rerank）是
        设备级的，与 user_id 无关、一律计入。
        """
        rows = self._conn.execute(
            "SELECT ref_backend, category FROM service_endpoint "
            "WHERE builtin = 0 AND ref_backend IS NOT NULL "
            "AND (category != 'chat' OR user_id = ?) "
            "ORDER BY CASE WHEN category = 'chat' THEN 0 ELSE 1 END, sort_order, category",
            (user_id,),
        ).fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            buckets = out.setdefault(str(row["ref_backend"]), [])
            if str(row["category"]) not in buckets:
                buckets.append(str(row["category"]))
        return out

    def _raw_backends(self, *, user_id: str) -> list[dict[str, object]]:
        """两层 JOIN 出"后端行"视图 —— 内核与 services 消费的仍是拆层前那一形状。

        `provider`/`base_url`/`api_key` 来自凭据组，`usage`/`used_by` 派生自引用行。
        凭据组按主人过滤，所以**模型行也跟着主人**：一个身份看不见别人的模型，
        连"它叫什么"都拿不到（`model_backend` 没有自己的归属列，继承自所属的组）。
        """
        # 值列清单由 `_value_columns` 算（S-1）：从前这里抄一遍列名，加一列漏一处
        # 不会红，只是那一列永远读成 None。
        b_cols = ", ".join(f"b.{c}" for c in _value_columns(self._conn))
        rows = self._conn.execute(
            f"SELECT b.name, b.provider_id, b.model, b.sort_order, {b_cols}, "
            "p.user_id, p.provider, p.label, p.base_url, p.api_key "
            "FROM model_backend b JOIN model_provider p ON p.id = b.provider_id "
            "WHERE p.user_id = ? "
            "ORDER BY b.sort_order, b.name",
            (user_id,),
        ).fetchall()
        usages = self._usages(user_id=user_id)
        out: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            used = usages.get(str(item["name"]), [])
            item["used_by"] = used
            item["usage"] = _derived_usage(used)
            out.append(item)
        return out

    def raw_backends(self, *, user_id: str) -> list[dict[str, object]]:
        """进程内配置解析用（服务引用行取凭据、工厂实例化）。

        含 api_key 明文 —— 只允许在服务层/工厂内部消费，**绝不**直接进任何 API 响应
        （对外形状见 `list_backends`：只回 has_key + 掩码）。
        """
        return self._raw_backends(user_id=user_id)

    def list_backends(self, *, user_id: str) -> list[dict[str, object]]:
        """过渡形状（对话页 / 角色页 / 旧模型页仍在消费）：NO api_key ever leaves the service.

        `usage` 现在是派生只读值；`used_by` 是它的全集（一行可同时服务多种能力）。
        """
        usages = self._usages(user_id=user_id)
        return [
            {
                k: row[k]
                for k in (
                    "name", "provider", "base_url", "model", "usage", "sort_order",
                    "num_ctx", "provider_id",
                )
            }
            | {
                "supports_vision": _vision_of(row["supports_vision"]),
                "supports_tools": _tools_of(row["supports_tools"]),
                "has_key": bool(row["api_key"]),
                "key_masked": mask_key(str(row["api_key"]) if row["api_key"] else None),
                "used_by": _sorted_usages(usages.get(str(row["name"]), [])),
            }
            for row in self._raw_backends(user_id=user_id)
        ]

    def list_providers(self, *, user_id: str) -> list[dict[str, object]]:
        """「模型」页签的形状：按凭据组分层的卡片数据（key 只在组头出现一次）。

        能力位是**三态**（true / false / null=没测过）—— 把"没测过"显示成"不支持"是撒谎，
        而"不支持"会触发调用前拦截。运行时那侧仍按 bool 解释（`_vision_of`/`_tools_of`）。
        """
        usages = self._usages(user_id=user_id)
        default = self.default_backend(user_id=user_id)
        models_of_group: dict[str, list[dict[str, object]]] = {}
        value_cols = ", ".join(f"b.{c}" for c in _value_columns(self._conn))
        for row in self._conn.execute(
            f"SELECT b.name, b.provider_id, b.model, {value_cols} "
            "FROM model_backend b JOIN model_provider p ON p.id = b.provider_id "
            "WHERE p.user_id = ? ORDER BY b.sort_order, b.name",
            (user_id,),
        ).fetchall():
            name = str(row["name"])
            models_of_group.setdefault(str(row["provider_id"]), []).append(
                {
                    "name": name,
                    "model": str(row["model"]),
                    "num_ctx": row["num_ctx"],
                    "supports_vision": _tri_state(row["supports_vision"]),
                    "supports_tools": _tri_state(row["supports_tools"]),
                    # 采样惩罚现值（null = 没设 = 引擎默认）。对话页那一栏要回显它，
                    # 否则"我上次设了什么"在界面上看不见 —— 看不见的设置就是没人管的设置。
                    "repeat_penalty": _opt_float(row["repeat_penalty"]),
                    "frequency_penalty": _opt_float(row["frequency_penalty"]),
                    "presence_penalty": _opt_float(row["presence_penalty"]),
                    "used_by": _sorted_usages(usages.get(name, [])),
                    "is_default": name == default,
                }
            )
        out: list[dict[str, object]] = []
        for group in self._provider_rows(user_id=user_id):
            gid = str(group["id"])
            provider = str(group["provider"])
            out.append(
                {
                    "id": gid,
                    "provider": provider,
                    "label": str(group["label"] or provider_label(provider)),
                    "base_url": group["base_url"],
                    "style": client_style(provider),
                    "needs_key": not is_keyless_provider(provider),
                    "has_key": bool(group["api_key"]),
                    "key_masked": mask_key(str(group["api_key"]) if group["api_key"] else None),
                    "models": models_of_group.get(gid, []),
                }
            )
        return out

    def default_backend(self, *, user_id: str) -> str | None:
        """对话默认后端 = **这个人的** chat 引用行的第 1 位；None = 未配置（退回 env）。

        归属（多租户 B1b，方案 A）：默认/回退链回答"这次对话花谁的 key 由谁定"，按人过滤。
        别人名下的 chat 引用对这个人不存在 —— 界面的"当前默认"对得上实际跑的那台。
        """
        row = self._conn.execute(
            "SELECT id FROM service_endpoint WHERE category = ? AND user_id = ? "
            "ORDER BY sort_order, id LIMIT 1",
            (CHAT_CATEGORY, user_id),
        ).fetchone()
        return str(row["id"]) if row else None

    def stored_api_key(self, name: str, *, user_id: str) -> str | None:
        """已保存的 key（来自该行所属的凭据组；只在本进程内使用，绝不经 API 回传）。

        别人的那一行在这里就是**不存在**：返回 None 而不是他的 key。
        """
        for row in self._raw_backends(user_id=user_id):
            if str(row["name"]) == name:
                return row["api_key"]  # type: ignore[return-value]
        return None

    def stored_group_key(self, group_id: str, *, user_id: str) -> str | None:
        """按**组**取 key（拆层后 key 不再属于单行；添加抽屉与探测端点用）。"""
        for group in self._provider_rows(user_id=user_id):
            if str(group["id"]) == group_id:
                return group["api_key"]  # type: ignore[return-value]
        return None

    def has_key_for_endpoint(
        self, provider: str, base_url: str | None, *, user_id: str
    ) -> bool:
        """这个 (供应商, 端点) 是否已经有 key —— 决定"新增一行模型"要不要重输凭据。

        归一化必须与写入路径同源（都走 `endpoint_key`），否则界面上一行"看起来同一个"的
        端点会因为留空/填了默认 URL 的差别被要求重填 key。
        只看本人的组：别人在同一端点上存过 key **不构成**"我也省一次输入"—— 那是他的凭据，
        让他替我的调用付费才是更糟的那种省。
        """
        target = endpoint_key(provider, base_url)
        return any(
            group["api_key"]
            and endpoint_key(str(group["provider"]), group["base_url"]) == target
            for group in self._provider_rows(user_id=user_id)
        )

    def list_fallbacks(self, *, user_id: str) -> list[str] | None:
        """Operator-configured fallback chain, or None = not configured (use env's).

        派生自**这个人的** chat 引用行：第 1 位是默认（不算回退），其后就是回退链。
        一条 chat 引用都没有 = 操作员没配过 = None（env 的 `MODEL_FALLBACKS` 仍然说话）。
        """
        names = self._chat_ref_names(user_id=user_id)
        return names[1:] if names else None

    # -- chat 引用行（"这行用于对话"这件事的事实面） ---------------------------------

    def _chat_ref_names(self, *, user_id: str) -> list[str]:
        """**这个人的** chat 引用序列（按优先级序）；能力行的 category 与它有别，天然隔开。"""
        rows = self._conn.execute(
            "SELECT id FROM service_endpoint WHERE category = ? AND user_id = ? "
            "ORDER BY sort_order, id",
            (CHAT_CATEGORY, user_id),
        ).fetchall()
        return [str(r["id"]) for r in rows]

    def _write_chat_refs(self, ordered: list[str], *, user_id: str) -> None:
        """整体重写**这个人的** chat 引用行（第 1 位 = 默认，其后 = 回退链）。

        删除必须带 `user_id` 范围、插入必须带 `user_id` 值：不加这两处，A 存一次对话序列
        就会把 B 的引用行一起抹掉/写成 A 的（多租户 B1b，方案 A 的"仅 chat 引用行按人"）。
        """
        self._conn.execute(
            "DELETE FROM service_endpoint WHERE category = ? AND user_id = ?",
            (CHAT_CATEGORY, user_id),
        )
        kinds = _kind_of_names(self._conn, ordered)
        for i, name in enumerate(ordered):
            self._conn.execute(
                "INSERT INTO service_endpoint "
                "(category, id, kind, ref_backend, enabled, sort_order, builtin, user_id) "
                "VALUES (?, ?, ?, ?, 1, ?, 0, ?)",
                (CHAT_CATEGORY, name, kinds.get(name, "cloud"), name, i, user_id),
            )

    def save_chat_pool(self, names: list[str], *, user_id: str) -> None:
        """「服务」页签模型推理序列的全量写入：第 1 位 = 对话默认，其后 = 回退顺序。

        这一条就是"哪些模型用于对话"的事实面 —— 写它即定义它：列进来的行从此是 chat
        用途，没列进来的不再是（引用被删）。所以候选**不能**只给"已经是 chat 的行"，
        否则第一次加入就没有入口（拆层前的死循环：usage=chat 才能进列表，进列表才能改 usage）。

        链长不再在这里拦："最多 2 级"是运行时的截断（`Settings.resolve_fallbacks`），
        序列里第 4 位以后不参与回退，但仍然记录在案 —— 因为拖动顺序本身就是意图，
        当场拒绝对用户没有意义（他改的是第 1 位，你却告诉他"链太长"）。

        校验只对**本人的**后端集：把别人的模型名塞进对话序列会写出一个他跑得起、你跑不起
        的配置（那一名字根本不在你的有效配置里），所以它对你是 400 而不是"成功"。
        """
        if not names:
            raise ModelSettingsError("对话优先级不能为空 —— 至少要留一个用于对话的模型。")
        if len(set(names)) != len(names):
            raise ModelSettingsError("对话序列里出现了重复的模型名。")
        known = {str(row["name"]) for row in self._raw_backends(user_id=user_id)}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise ModelSettingsError(
                f"以下模型不在模型页配置里：{', '.join(unknown[:3])}（请先在「模型」页签添加）。"
            )
        self._write_chat_refs(names, user_id=user_id)
        self._conn.commit()

    def seed_from_env(self, env_settings: Settings, *, user_id: str) -> int:
        """First-boot migration: copy env backends into the tables ONCE, then env is out of
        the loop — the settings UI (these tables) is the single source of truth afterwards.

        The `model_backends_seeded` flag makes the migration one-way: a backend the operator
        deletes in the UI stays deleted even if env still provides it, and env edits after
        the first boot are deliberately ignored. 迁移是一次性的，这正是"以后都在界面配置"
        的含义。

        env 的后端按 (供应商, base_url) 归并成凭据组（同一端点的多个模型共用一把 key），
        `usage='chat'` 的行同时播 chat 引用，env 的默认后端排第 1 位。

        **种子有主人 = 这台实例的主人**（M2d）：env 里的 key 是"这个进程带着的凭据"，它不属于
        库里任何一个登录者。播种闸（`kernel_meta` 那个 flag）也因此是实例级的 —— 第二个身份
        来了不重播 env，他在界面上自己填 key。
        """
        flag = self._conn.execute(
            "SELECT value FROM kernel_meta WHERE key = ?", (self.MODEL_SEEDED_KEY,)
        ).fetchone()
        if flag is not None:
            return 0

        existing = {str(r["name"]) for r in self._raw_backends(user_id=user_id)}
        groups = {
            endpoint_key(str(g["provider"]), g["base_url"]): str(g["id"])
            for g in self._provider_rows(user_id=user_id)
        }
        taken = self._all_group_ids()
        inserted = 0
        chat_rows: list[str] = []
        for name, backend in env_settings.model_backends.items():
            if name in existing:
                continue
            catalog = normalize_provider(backend.provider, backend.base_url)
            base_url = validate_base_url(backend.base_url)
            endpoint = endpoint_key(catalog, base_url)
            if endpoint not in groups:
                gid = _group_id(taken, catalog)
                self._conn.execute(
                    "INSERT INTO model_provider "
                    "(id, user_id, provider, base_url, api_key, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        gid,
                        user_id,
                        catalog,
                        base_url,
                        None if is_keyless_provider(catalog) else backend.api_key,
                        len(taken) - 1,
                    ),
                )
                groups[endpoint] = gid
            self._conn.execute(
                "INSERT OR IGNORE INTO model_backend "
                "(name, provider_id, model, sort_order, num_ctx, supports_vision, "
                "supports_tools) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    groups[endpoint],
                    backend.model,
                    len(existing) + inserted,
                    backend.num_ctx,
                    int(backend.supports_vision),
                    int(backend.supports_tools),
                ),
            )
            if backend.usage == "chat":
                chat_rows.append(name)
            inserted += 1
        if chat_rows:
            ordered = _dedupe(
                [env_settings.model_default, *env_settings.model_fallbacks, *chat_rows],
                set(chat_rows),
            )
            self._write_chat_refs(ordered, user_id=user_id)
        self._conn.execute(
            "INSERT OR IGNORE INTO kernel_meta (key, value) VALUES (?, ?)",
            (self.MODEL_SEEDED_KEY, "1"),
        )
        self._conn.commit()
        return inserted

    def set_num_ctx(self, name: str, num_ctx: int | None, *, user_id: str) -> None:
        """只改一行的上下文窗口（对话菜单悬浮面板用）—— 名称不存在抛 KeyError（404）。

        num_ctx 语义：None = 用引擎默认；给了必须 >= 512（太小的窗口等于把历史截没）。
        **别人的那一行也抛 KeyError**：按名改配置这条路上，"不是你的"与"不存在"是同一个回答
        （名字是可枚举的短串，回 403 等于告诉他"这行存在"）。
        """
        if num_ctx is not None and num_ctx < 512:
            raise ModelSettingsError("num_ctx 不得小于 512（tokens）")
        cur = self._conn.execute(
            f"UPDATE model_backend SET num_ctx = ? WHERE name = ? AND {_OWNED_MODEL_ROWS}",
            (num_ctx, name, user_id),
        )
        if cur.rowcount == 0:
            # **先结束事务再抛**：改到 0 行的 UPDATE 同样开了一个写事务，而这一路走不到
            # commit —— 留着它，这条线程就把 RESERVED 锁一直握着，别人等 5 秒一起报
            # `database is locked`（`R102-42`；领域服务里那两处早就按同一个写法收口了，
            # 漏的是这里 —— 所以本轮给它加了断言 `dangling write txn`）。
            self._conn.rollback()
            raise KeyError(name)
        self._conn.commit()

    #: 三栏惩罚的可接受区间。**故意不给"聪明"的默认值**：出厂全 NULL = 不传 = 引擎默认
    #: （Ollama 的 repeat_penalty 自带 1.1）。区间只挡"会把输出打成人话不成人话"的数：
    #: repeat 超过 2 实测就是断句复读，负数无意义；另两项 OpenAI 兼容体的定义域就是 −2..2。
    SAMPLING_RANGES: dict[str, tuple[float, float]] = {
        "repeat_penalty": (0.0, 2.0),
        "frequency_penalty": (-2.0, 2.0),
        "presence_penalty": (-2.0, 2.0),
    }

    def set_sampling(
        self, name: str, values: dict[str, float | None], *, user_id: str
    ) -> dict[str, float | None]:
        """改一行的采样惩罚（对话菜单那一栏）。给 None = 清回"不传"，不是传 0。

        **`repeat_penalty` 只对 native（Ollama）后端收**：OpenAI 兼容体里没有这个标准字段，
        存进去工厂也不会发出去 —— 让它写进去就是"界面显示已设、实际没生效"的第二个事实面。
        界面上那一栏对云端根本不出现，这里是同一件事的后端闸门。
        """
        unknown = sorted(set(values) - set(self.SAMPLING_RANGES))
        if unknown:
            raise ModelSettingsError(f"未知的采样参数：{', '.join(unknown)}")
        row = self._conn.execute(
            "SELECT p.provider FROM model_backend b JOIN model_provider p ON p.id = b.provider_id"
            " WHERE b.name = ? AND p.user_id = ?",
            (name, user_id),
        ).fetchone()
        if row is None:
            raise KeyError(name)
        native = client_style(str(row["provider"])) == "native"
        if not native and values.get("repeat_penalty") is not None:
            raise ModelSettingsError(
                "重复惩罚只对本地 Ollama 的模型有效（OpenAI 兼容体没这个字段）"
            )
        for field, (low, high) in self.SAMPLING_RANGES.items():
            raw = values.get(field)
            if raw is None:
                continue
            if not -1e9 < float(raw) < 1e9 or not low <= float(raw) <= high:
                raise ModelSettingsError(f"{field} 得在 {low}..{high} 之间（给了 {raw}）")
        for field in self.SAMPLING_RANGES:
            if field in values:
                self._conn.execute(
                    f"UPDATE model_backend SET {field} = ? "  # noqa: S608
                    f"WHERE name = ? AND {_OWNED_MODEL_ROWS}",
                    (values[field], name, user_id),
                )
        self._conn.commit()
        return self.sampling(name, user_id=user_id)

    def sampling(self, name: str, *, user_id: str) -> dict[str, float | None]:
        """一行的三栏惩罚现值（None = 没设）。写侧的回显走它，免得前端拿旧草稿。

        回显也按主人读：不然"我设了什么"会读到别人那一行的数（同名行在两个身份下可以各有一份）。
        """
        row = self._conn.execute(
            "SELECT b.repeat_penalty, b.frequency_penalty, b.presence_penalty "
            "FROM model_backend b JOIN model_provider p ON p.id = b.provider_id"
            " WHERE b.name = ? AND p.user_id = ?",
            (name, user_id),
        ).fetchone()
        if row is None:
            raise KeyError(name)
        return {
            "repeat_penalty": _opt_float(row["repeat_penalty"]),
            "frequency_penalty": _opt_float(row["frequency_penalty"]),
            "presence_penalty": _opt_float(row["presence_penalty"]),
        }

    def normalize_providers(self) -> int:
        """启动时一次性归一化历史组的 provider，并清掉无 key 供应商误存的 key。

        幂等：归一化后的值再跑一遍不再变化。修复两类历史脏数据 ——
        ① 云端种子把 SiliconFlow 写成 provider="openai"（只记了风格没记厂商），
           设置页因此显示"供应商：openai"这种错误身份；
        ② 无 key 供应商（Ollama）被早期测试写入了无意义的占位 key。

        **这一处刻意读全部身份的行**（`_all_provider_rows`）：它是启动时的数据卫生清扫，
        不是任何人的读写视图。按主人过滤反而漏 —— 库里躺着第二个身份的脏组就没人管了，
        而他下一次看见自己的供应商名仍然是错的。它不改归属，所以清扫不构成越权。
        """
        changed = 0
        for group in self._all_provider_rows():
            old = str(group["provider"])
            base = str(group["base_url"]) if group["base_url"] else None
            norm = normalize_provider(old, base)
            drop_key = is_keyless_provider(norm) and bool(group["api_key"])
            if norm != old or drop_key:
                self._conn.execute(
                    "UPDATE model_provider SET provider = ?, api_key = ? WHERE id = ?",
                    (norm, None if drop_key else group["api_key"], str(group["id"])),
                )
                changed += 1
        if changed:
            self._conn.commit()
        return changed

    # -- writes ----------------------------------------------------------------

    def save(
        self,
        *,
        user_id: str,
        default: str,
        backends: list[dict[str, object]],
        fallbacks: list[str] | None = None,
    ) -> None:
        """Replace the whole backend set in one transaction（过渡期：旧模型页的整表保存）。

        **`user_id` 是"整表"的范围**：这里的"全量替换"替换的是**这个人**的那一集，不是库里的
        全部。旧语义在没有归属列之前是同一件事（整个库里只有一族配置），有了主人之后它就成了
        最危险的一处 —— 不加过滤，A 存一次盘就把 B 的模型行与 key 抹了（`DELETE FROM
        model_backend` 无 WHERE + 末尾那句按 id 列表删组，都是全表）。

        入参仍是**旧形状**：每行带 provider/base_url/api_key/usage。内部按 (供应商, base_url)
        归并成凭据组，模型行只留模型名 + 能力位 + num_ctx；`usage='chat'` 翻译成 chat 引用行
        （第 1 位 = `default`，其后 = `fallbacks`）。旧界面下线后本方法随
        `PUT /api/settings/models` 一起退役，新界面走逐条增删改的端点。

        `api_key` 语义（组内聚合后落到组上）：任一行给了非空串 = 设成它；给了空串且无人给
        新值 = 清除；所有行都省略 = 保留组里已存的。无 key 供应商（Ollama）一律不存 key。
        """
        if not backends:
            raise ModelSettingsError("至少需要保留一个模型。")
        stored_rows = {str(row["name"]): row for row in self._raw_backends(user_id=user_id)}
        existing_groups = {str(g["id"]): g for g in self._provider_rows(user_id=user_id)}
        stored_gid_of_endpoint = {
            endpoint_key(str(g["provider"]), g["base_url"]): str(g["id"])
            for g in existing_groups.values()
        }
        names: list[str] = []
        usage_by_name: dict[str, str] = {}
        prepared: list[_PreparedBackend] = []
        for i, item in enumerate(backends):
            name = str(item.get("name") or "").strip()
            provider = str(item.get("provider") or "").strip()
            model = str(item.get("model") or "").strip()
            usage = str(item.get("usage") or "chat").strip().lower() or "chat"
            if not _NAME_RE.match(name):
                raise ModelSettingsError(
                    f"模型名 {name!r} 不合法：小写字母开头，只含小写字母/数字/下划线/连字符。"
                )
            if name in names:
                raise ModelSettingsError(f"模型名重复：{name}")
            if not provider:
                raise ModelSettingsError(f"模型 {name} 缺少 provider（从供应商目录选择）。")
            if not model:
                raise ModelSettingsError(f"模型 {name} 还没有填模型名。")
            if usage not in BACKEND_USAGES:
                raise ModelSettingsError(
                    f"模型 {name} 的用途 {usage!r} 不合法（chat/embedding/rerank/ocr）。"
                )
            # num_ctx（本地 Ollama 上下文窗口）：None 允许；给了必须是不小于 512 的整数
            # ——太小的窗口等于把历史截没，宁可大声拒绝。
            raw_ctx = item.get("num_ctx")
            num_ctx: int | None = None
            if raw_ctx not in (None, ""):
                try:
                    num_ctx = int(str(raw_ctx))
                except (TypeError, ValueError) as exc:
                    raise ModelSettingsError(
                        f"模型 {name} 的 num_ctx 必须是整数（tokens）"
                    ) from exc
                if num_ctx < 512:
                    raise ModelSettingsError(f"模型 {name} 的 num_ctx 不得小于 512（tokens）")
            raw_base = item.get("base_url")
            # 写入即归一：目录外的风格值（如历史 "openai"+硅基流动 URL）折叠成厂商 id。
            provider = normalize_provider(provider, str(raw_base) if raw_base else None)
            # M8：base_url 落库前校验 scheme/host，拒绝异常协议与裸 host（允许 localhost/私网）。
            base_url = validate_base_url(str(raw_base) if raw_base else None)
            names.append(name)
            usage_by_name[name] = usage
            raw_key = item.get("api_key")
            prepared.append(
                _PreparedBackend(
                    name=name,
                    endpoint=endpoint_key(provider, base_url),
                    model=model,
                    # 三态：省略/None = 这行没给（保留组里已存的）；空串 = 清除；非空 = 设值。
                    api_key=None if raw_key is None else str(raw_key).strip(),
                    sort_order=i,
                    num_ctx=num_ctx,
                    # 能力位三态：省略 = 保留库里已存的（包括"没测过"）；显式给了才写。
                    supports_vision=_capability_of(
                        item, "supports_vision", stored_rows.get(name), default=False
                    ),
                    supports_tools=_capability_of(
                        item, "supports_tools", stored_rows.get(name), default=True
                    ),
                )
            )

        if default not in names:
            raise ModelSettingsError(f"默认模型 {default!r} 不在列表里。")
        # M2：对话默认后端必须是 chat 用途（对话/抽取）；embedding/rerank/ocr 不能当默认。
        if usage_by_name.get(default) != "chat":
            raise ModelSettingsError(
                f"默认模型 {default!r} 必须是 chat 用途（对话/抽取），"
                f"不能是 {usage_by_name.get(default)}。"
            )

        # 回退链：显式给链 → 原样校验（操作员手滑必须大声拒绝）；缺省（=保留当前值）→
        # **修剪掉引用已删后端的项** —— 后端集缩小时旧链可能指向已删行，此时拒绝会让
        # 一次普通的缩容保存永远卡死；运行时 `resolve_fallbacks` 本就丢弃未知名字，
        # 保存时对齐这一语义（smoke：缩容保存 200，链被清空）。
        kept = self.list_fallbacks(user_id=user_id) or []
        chain = (
            list(fallbacks) if fallbacks is not None else [n for n in kept if n in names]
        )
        if len(chain) > MAX_FALLBACKS:
            raise ModelSettingsError(f"回退链最多 {MAX_FALLBACKS} 级（过长只会掩盖降级质量）。")
        if len(set(chain)) != len(chain):
            raise ModelSettingsError("回退链里出现了重复的模型名。")
        for name in chain:
            if name not in names:
                raise ModelSettingsError(f"回退用的模型 {name!r} 不在已配置的模型列表里。")
            if usage_by_name[name] != "chat":
                raise ModelSettingsError(
                    f"回退用的模型 {name!r} 不是 chat 用途（对话/抽取），不能进回退链。"
                )

        # 组 key：先按端点聚合各行信号（给了新值 > 显式清除 > 保留已存的）。
        orders: dict[tuple[str, str | None], int] = {}
        signals: dict[tuple[str, str | None], str] = {}
        keyless: dict[tuple[str, str | None], bool] = {}
        for row in prepared:
            endpoint = row.endpoint
            orders.setdefault(endpoint, row.sort_order)
            keyless[endpoint] = is_keyless_provider(endpoint[0])
            if keyless[endpoint] or row.api_key is None:
                continue
            if row.api_key:
                signals[endpoint] = row.api_key
            else:
                signals.setdefault(endpoint, "")

        # 删之前先按后端名留住"这个端点不管理"的那些列（见 `SAVE_MANAGED_COLUMNS`）。
        # 快照与删除同范围（都只碰本人的行）：全表快照会把别人的行读进来，而全表删除会
        # 把别人的行删掉 —— 两边不一致时，症状是"我保存一次，他的配置变小了"。
        carried = unmanaged_backend_columns(self._conn)
        carried_values = {
            str(r["name"]): {c: r[c] for c in carried}
            for r in self._conn.execute(
                "SELECT b.* FROM model_backend b JOIN model_provider p ON p.id = b.provider_id"
                " WHERE p.user_id = ?",
                (user_id,),
            )
        }
        self._conn.execute(
            f"DELETE FROM model_backend WHERE {_OWNED_MODEL_ROWS}", (user_id,)
        )
        used_ids: set[str] = set()
        gid_of_endpoint: dict[tuple[str, str | None], str] = {}
        for endpoint, order in orders.items():
            stored_gid = stored_gid_of_endpoint.get(endpoint)
            if keyless[endpoint]:
                api_key = None  # 本地类供应商不存 key（历史脏数据也在这次保存里被清掉）
            elif endpoint in signals:
                api_key = signals[endpoint] or None
            elif stored_gid is not None:
                api_key = str(existing_groups[stored_gid]["api_key"] or "") or None
            else:
                api_key = None
            if stored_gid is not None:
                gid = stored_gid
                self._conn.execute(
                    "UPDATE model_provider SET base_url = ?, api_key = ?, sort_order = ? "
                    "WHERE id = ? AND user_id = ?",
                    (endpoint[1], api_key, order, gid, user_id),
                )
            else:
                # `taken` 是**全局**的（主键是全局的，见 `_all_group_ids`）；而 gid 一旦发就
                # 只写进本人名下。同一个 (供应商, 端点) 被两个人各配一次 = 两组各带一把 key，
                # 而不是共享 A 的那一把 —— 这正是"key 跟人走"要的形状。
                gid = _group_id(self._all_group_ids() | used_ids, endpoint[0])
                self._conn.execute(
                    "INSERT INTO model_provider "
                    "(id, user_id, provider, base_url, api_key, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (gid, user_id, endpoint[0], endpoint[1], api_key, order),
                )
            gid_of_endpoint[endpoint] = gid
            used_ids.add(gid)
        insert_cols = [
            "name",
            "provider_id",
            "model",
            "sort_order",
            "num_ctx",
            "supports_vision",
            "supports_tools",
            *carried,
        ]
        insert_sql = (
            f"INSERT INTO model_backend ({', '.join(quote_ident(c) for c in insert_cols)}) "
            f"VALUES ({', '.join('?' * len(insert_cols))})"
        )
        for row in prepared:
            keep = carried_values.get(row.name, {})
            self._conn.execute(
                insert_sql,
                (
                    row.name,
                    gid_of_endpoint[row.endpoint],
                    row.model,
                    row.sort_order,
                    row.num_ctx,
                    row.supports_vision,
                    row.supports_tools,
                    *(keep.get(c) for c in carried),
                ),
            )
        # 组里最后一个模型被删掉 = 这个端点不再存在。key 随组一起消失，不是"留着备用"：
        # 界面上已经没有它，留在盘上就是一处看不见的凭据。
        # `user_id = ?` 是这一句的范围：`used_ids` 只装了本次涉及的组，不加过滤就是
        # "A 存一次盘，把 B 的凭据组全删了"（这是本方法最要命的那一条，也是它进验收用例的原因）。
        self._conn.execute(
            f"DELETE FROM model_provider WHERE user_id = ? "
            f"AND id NOT IN ({','.join('?' * len(used_ids))})",
            (user_id, *used_ids),
        )
        # 用途（chat 引用行）：默认永远第 1 位，其后依次是回退链，再后面是其余对话后端。
        chat_names = [n for n in names if usage_by_name[n] == "chat"]
        self._write_chat_refs(
            _dedupe([default, *chain, *chat_names], set(chat_names)), user_id=user_id
        )
        self._conn.commit()

    # -- 逐条写入（新模型页的添加抽屉 / 删除 / 探测写回） -----------------------------

    def group_for(
        self,
        *,
        user_id: str,
        group_id: str | None = None,
        provider: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, object] | None:
        """按组 id 或按 (供应商, 端点) 找那条凭据组（含 api_key 明文，**只在进程内用**）。

        端点走 `endpoint_key` 归一，所以"没填 URL 的硅基流动"能命中"填了默认 URL 的那一组"。
        只在这个人的组里找 —— 别人的组在这里就是不存在（探测/添加因此花不到他的 key）。
        """
        for group in self._provider_rows(user_id=user_id):
            if group_id is not None:
                if str(group["id"]) == group_id:
                    return group
            elif provider is not None and endpoint_key(
                provider, base_url
            ) == endpoint_key(str(group["provider"]), group["base_url"]):
                return group
        return None

    def add_model(
        self,
        *,
        user_id: str,
        model: str,
        provider: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        group_id: str | None = None,
        name: str | None = None,
        num_ctx: int | None = None,
        supports_vision: bool | None = None,
        supports_tools: bool | None = None,
    ) -> dict[str, str]:
        """加一行模型（添加抽屉测连通过后走这里）；组不存在就顺手建。

        与整表 `save()` 的区别是它**只动这一行**：不重排别的行、不覆写回退链、不要求前端
        持有全部配置（写放大与并发互相覆盖都少了）。新行进对话序列的尾部（能加进来就是要
        能聊），序列本身仍是"哪些模型用于对话"的唯一事实面。

        `name` 是这行的身份键（session/角色卡引用它）。缺省时由 (供应商, 模型名) 生成一个
        可读的短键，冲突就加后缀 —— 让用户少填一格，同时名字仍然说得出它是谁。
        返回 `{"name":…, "provider_id":…}`。
        """
        model = (model or "").strip()
        if not model:
            raise ModelSettingsError("缺少模型名。")
        group = self.group_for(user_id=user_id, group_id=group_id) if group_id else None
        if group is None and provider:
            group = self.group_for(user_id=user_id, provider=provider, base_url=base_url)
        if group is None:
            if not provider:
                raise ModelSettingsError("要么选一个已配置的供应商，要么填 provider。")
            catalog = normalize_provider(provider, base_url)
            pinned = validate_base_url(base_url)
            if is_keyless_provider(catalog):
                api_key = None
            gid = _group_id(self._all_group_ids(), catalog)
            self._conn.execute(
                "INSERT INTO model_provider "
                "(id, user_id, provider, base_url, api_key, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    gid,
                    user_id,
                    catalog,
                    pinned,
                    api_key.strip() or None if api_key else None,
                    len(self._provider_rows(user_id=user_id)),
                ),
            )
            group = {"id": gid, "provider": catalog, "base_url": pinned, "api_key": api_key}
        gid = str(group["id"])
        if name:
            key = name.strip()
            if not _NAME_RE.match(key):
                raise ModelSettingsError(
                    f"模型名 {key!r} 不合法：小写字母开头，只含小写字母/数字/下划线/连字符。"
                )
            # 冲突判定是**全局**的，不是按人的：`model_backend.name` 是全局主键，按人过滤
            # 只会把"撞主键"变成一个 500。这里的取舍是"宁可报一次占用，也不静默改名"
            # （改名会让用户下次找不到自己那行）。
            if self._conn.execute(
                "SELECT 1 FROM model_backend WHERE name = ?", (key,)
            ).fetchone():
                raise ModelSettingsError(
                    f"这个配置名已经存在：{key}（换一个，或直接编辑原来那行）。"
                )
        else:
            key = self._free_name(gid, str(group["provider"]), model)
        # 能力位：探测结果原样写（None 保持"没测过"），不让一次添加把未知说成已知。
        order = len(self._raw_backends(user_id=user_id))
        self._conn.execute(
            "INSERT INTO model_backend "
            "(name, provider_id, model, sort_order, num_ctx, supports_vision, supports_tools) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                gid,
                model,
                order,
                num_ctx,
                None if supports_vision is None else int(supports_vision),
                None if supports_tools is None else int(supports_tools),
            ),
        )
        # 新行默认进对话序列的尾部（拆层前的行为：加一个模型就是为了能跟它说话）。
        # 不想让它参与对话 → 在「服务」页的模型推理序列里把它摘掉；只服务嵌入的那行
        # 也是在那里加回来（批次③ 补这个入口）。
        self._write_chat_refs([*self._chat_ref_names(user_id=user_id), key], user_id=user_id)
        self._conn.commit()
        return {"name": key, "provider_id": gid}

    def remove_model(self, name: str, *, user_id: str) -> None:
        """删一行模型（名称不存在 → KeyError/404）。

        组里没别的模型了才连凭据一起删（key 不留成"看不见的凭据"）。其余服务类别的引用行
        **保持原样**并在服务页呈现「失效」—— 摘引用与删配置是两个动作，不能顺手合并；
        chat 引用则跟着这行走，并把它的位置让给序列里的下一个（默认不能悬空）。

        别人的那一行在这里同样是 KeyError —— 而这一处比 404 的口径更要紧：不带主人过滤的
        删除会连着 `DELETE FROM model_provider` 一起走，那是**删掉他的凭据**。
        """
        row = next((r for r in self._raw_backends(user_id=user_id) if str(r["name"]) == name), None)
        if row is None:
            raise KeyError(name)
        gid = str(row["provider_id"])
        pool = [n for n in self._chat_ref_names(user_id=user_id) if n != name]
        self._conn.execute(
            f"DELETE FROM model_backend WHERE name = ? AND {_OWNED_MODEL_ROWS}", (name, user_id)
        )
        left = self._conn.execute(
            "SELECT 1 FROM model_backend WHERE provider_id = ? LIMIT 1", (gid,)
        ).fetchone()
        if left is None:
            self._conn.execute(
                "DELETE FROM model_provider WHERE id = ? AND user_id = ?", (gid, user_id)
            )
        # 引用行按新序重编 sort_order，所以删掉的正是默认时，下一位自动顶上（默认不会悬空）。
        self._write_chat_refs(pool, user_id=user_id)
        self._conn.commit()

    def set_capabilities(
        self, name: str, capabilities: dict[str, bool | None], *, user_id: str
    ) -> None:
        """写回探测结论（三态）。字典里**出现**的键才写，缺席的键不动。

        为什么按"键在不在"而不是"值是不是 None"：`None` 在这三态里是一个**有内容的结论**
        ("没测过" → 界面 `?`)。把 None 当"没提交"，PATCH 就永远没法把 `✗` 改回 `?`。
        """
        if not self._has_backend(name, user_id=user_id):
            raise KeyError(name)
        unknown = set(capabilities) - {"supports_vision", "supports_tools"}
        if unknown:
            raise ModelSettingsError(f"未知能力位：{', '.join(sorted(unknown))}")
        if not capabilities:
            raise ModelSettingsError("没有要写的 capability。")
        columns = {
            field: None if value is None else int(bool(value))
            for field, value in capabilities.items()
        }
        # 参数化列名来自白名单（`unknown` 已经挡掉其它键），不是用户输入。
        assignments = ", ".join(f"{field} = ?" for field in columns)
        self._conn.execute(
            f"UPDATE model_backend SET {assignments} WHERE name = ? AND {_OWNED_MODEL_ROWS}",
            (*columns.values(), name, user_id),
        )
        self._conn.commit()

    def _has_backend(self, name: str, *, user_id: str) -> bool:
        """这一行**在这个人眼里**存在吗（别人的行 = 不存在，不是"存在但你不能碰"）。"""
        return (
            self._conn.execute(
                f"SELECT 1 FROM model_backend WHERE name = ? AND {_OWNED_MODEL_ROWS}",
                (name, user_id),
            ).fetchone()
            is not None
        )

    def _free_name(self, group_id: str, provider: str, model: str) -> str:
        """由 (供应商, 模型名) 生成一个合法且未占用的配置名，例如 `siliconflow-qwen3-vl-30b`。

        `taken` 是**全局**的（`_all_backend_names`）：主键全局，按人取会生成一个撞别人
        已占名字的键，症状是 INSERT 抛 IntegrityError —— 而这条路径是"用户没填名字"，
        他不该为一次看不见的主键冲突负责。
        """
        slug = re.sub(r"[^a-z0-9]+", "-", f"{provider}-{model}".lower()).strip("-")
        base = slug[:28].rstrip("-") or "model"
        taken = self._all_backend_names()
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    # -- merge -------------------------------------------------------------------

    def effective_settings(self, env_settings: Settings, *, user_id: str) -> Settings:
        """DB rows are the single source of truth once seeded.

        **这一句 `user_id` 就是"谁的 key 被花出去"的唯一答案**（M2d，§4.1）：拼出来的是
        那个人名下的后端集，别人的组根本进不来，所以运行时不存在"要不要检查这把 key 是不是
        他的"这一问 —— 图与工厂拿到的 `Settings` 里压根没有别人的凭据。咽喉只在这一处，
        也就是 `core/graph.py` 那句 `backend.api_key` 之上再没有第二道判断要写。
        本机单身份时这个参数恒等于"这台实例的主人"，形状与拆层前一致。

        H5 修复：表非空后**不再并入 env 后端**。此前 `merged = dict(env_settings.model_backends)`
        会把"UI 删掉、但 env 仍提供"的后端重新复活，与 `seed_from_env` 文档（首启后 env 出局、
        UI 删除的后端保持删除）直接矛盾。现在：表空 → 退回 env（首启前 bootstrap）；表非空 →
        仅以 DB 行为准，env 改动（首启后）一律忽略。
        """
        raw = self._raw_backends(user_id=user_id)
        if not raw:
            return env_settings
        # 值列不在这里抄清单（S-1）：`_backend_from_row` 按 `_COLUMN_READERS` 逐列读，
        # 加一列只改 schema 声明 + 补一个读取器。这里从前手写十一个字段，漏一个不会红。
        merged: dict[str, ModelBackend] = {
            str(row["name"]): _backend_from_row(row) for row in raw
        }
        # 默认/回退链按**这个人**的 chat 引用行取（多租户 B1b，方案 A）：这份配置是谁的 key
        # 谁说话，对话默认就该是那个人的第一条 chat 引用。别人名下的序列对这里不存在。
        default = self.default_backend(user_id=user_id)
        # 默认缺失/失效 → 退到 DB 第一个后端（首启种子已保证至少一个 chat 后端）。
        if default is None or default not in merged:
            default = next(iter(merged), env_settings.model_default)
        fallbacks = self.list_fallbacks(user_id=user_id)
        return env_settings.model_copy(
            update={
                "model_backends": merged,
                "model_default": default,
                "model_fallbacks": fallbacks
                if fallbacks is not None
                else env_settings.model_fallbacks,
            }
        )


# ------------------------------------------------------------------ 派生与三态小工具


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


__all__ = [
    "BACKEND_USAGES",
    "CHAT_CATEGORY",
    "KEYLESS_PROVIDERS",
    "MODEL_PROVIDERS",
    "ModelSettingsError",
    "ModelSettingsService",
    "PROVIDER_ALIASES",
    "UNASSIGNED_USAGE",
    "USAGE_DISPLAY_ORDER",
    "client_style",
    "endpoint_key",
    "is_keyless_provider",
    "mask_key",
    "migrate_to_provider_layers",
    "normalize_provider",
    "provider_catalog",
    "provider_label",
    "validate_base_url",
]
