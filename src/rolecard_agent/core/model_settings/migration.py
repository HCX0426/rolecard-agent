from __future__ import annotations

import contextlib
import json

from rolecard_agent.base.observability import logline
from rolecard_agent.core.model_settings.rows import _kind_of_names, _table_columns
from rolecard_agent.core.model_settings.rules import (
    _DEFAULT_BASE_URLS,
    CHAT_CATEGORY,
    normalize_provider,
)
from rolecard_agent.storage.db import SqlConnection


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
    # 迁移事件走 `notice` 档落 stderr（`R102-64` 的旧约定由这一档承接，见 `logline`）。
    logline(
        "notice",
        "schema-migrate",
        "model_backend 搬层开始（provider 两层化，暂存表 __layers）",
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
