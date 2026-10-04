"""上行同步的数据层（M7 第一批）：把"本机这份"与"对面那份"比出一个**计划**。

这一版搬四类：**角色卡 / 会话 / 记忆 / 主动消息**。健康档案与上传原件**不搬**
（用户 09-27：「第二批先不需要传吧」），界面上也不给它们一个勾 —— 没有实现的复选框
比没有复选框更坏。模型凭据（api_key）同样不上行：到对面自己填，本站不代付 token。

## 三件必须先想清楚的事

**① 什么算"同一条"。** 跨机器可比的身份只有三个来源：`role_memory_item.uid`（M2b 就是
为这件事埋的）、`session_thread.thread_id`（`s_<12 hex>`，uuid）、以及 `role_card.role_id`。
`agent_reachout.id` 是本机自增整数，两台机器会各自长出相同 id 的不同行 —— 所以它的身份是
`role_id|created_at|文本指纹` 三元组，不是那个 id。

**② 什么算冲突。** 同身份**且内容指纹不同**才算。两条特例：

  * **会话：一边是另一边的前缀**不算冲突，那是"其中一台接着聊下去了" —— 按长的那份走。
    判据是头 20 条的指纹相同而条数不同。不做这条，任何"两台都聊过"的会话都会变成冲突，
    而正确答案从来是"取更长的那份"。
  * **内置角色卡两边同名**通常直接落进"相同"（出厂内容一样）。用户真改过的那张会算冲突 ——
    这不是噪音，那是"你在两台机器上把同一个角色改成了两个样子"，正是要问的时刻。

**③ 谁发起、凭据走哪。** 用户拍的是"本机后端发起"：本机收集、本机比对，对面只暴露
一个清单端点与一个导入端点。所以**对面的地址与凭据是每次请求带进来的**（它们存在浏览器
那一侧的 `dataSource` 里，本机后端自己不存）。代价说清楚：那枚凭据会经过
浏览器 → 本机后端（回环）→ 对面，而**绝不进审计、日志、异常文本**（与 api_key 同一条纪律）。

"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from rolecard_agent.core.memory import restore_row as restore_memory
from rolecard_agent.core.thread_locks import thread_write
from rolecard_agent.features.reachout.inbox import restore_row as restore_reachout
from rolecard_agent.storage.db import SqlConnection
from rolecard_agent.storage.threads import (
    insert_imported_thread,
    update_imported_thread,
)

KIND_CARD = "card"
KIND_THREAD = "thread"
KIND_MEMORY = "memory"
KIND_REACHOUT = "reachout"
#: 这一版真的搬的四类。加一类要同时动 `collect` 与对面的导入端点，两边一起红才有意义。
SYNC_KINDS: tuple[str, ...] = (KIND_CARD, KIND_THREAD, KIND_MEMORY, KIND_REACHOUT)

#: 会话"头多少条"用来判前缀：20 条足够分开两个真实分叉的对话，又不至于把整段历史
#: 塞进清单端点的响应里。
HEAD_MESSAGES = 20

#: 一条会话里出现这些就**整条跳过**（不搬、不猜、不删图）：附件这一版不过去。
SKIP_HAS_IMAGE = "这条会话里有附图或文件，附件这一版不传"


def _digest(parts: object) -> str:
    """规范化 JSON → 16 位十六进制指纹。

    规范化是必须的：`dict` 的键序、`None` 与缺键、数字与字符串数字，任何一处不一致
    都会让"内容相同"判成冲突，而冲突列表一多，用户就会开始无脑点"全按本机的来"。
    """
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _preview(text: object, limit: int = 90) -> str:
    body = " ".join(str(text or "").split())
    return body[:limit]


@dataclass(slots=True)
class SyncItem:
    """一条要上行的东西：身份 + 指纹 + 对面写回去要用的完整载荷。"""

    kind: str
    ident: str
    hash: str
    at: str = ""
    preview: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    #: 只有会话用：头 20 条的指纹（判"一边是另一边的前缀"）与条数。
    head: str = ""
    count: int = 0

    def brief(self) -> dict[str, Any]:
        """清单端点的形状：**不含 payload**（那是导入时才给的东西）。"""
        return {
            "kind": self.kind,
            "ident": self.ident,
            "hash": self.hash,
            "at": self.at,
            "preview": self.preview,
            "head": self.head,
            "count": self.count,
        }


@dataclass(slots=True)
class Conflict:
    kind: str
    ident: str
    mine: SyncItem
    theirs: dict[str, Any]


@dataclass(slots=True)
class SyncPlan:
    only_local: list[SyncItem]
    only_remote: list[dict[str, Any]]
    same: list[dict[str, Any]]
    conflicts: list[Conflict]
    skipped: list[dict[str, Any]]
    #: 对面有、本机没有的条数按类分（"不动"那一格要说清是什么在不动）。
    remote_counts: dict[str, int] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "only_local": len(self.only_local),
            "only_remote": len(self.only_remote),
            "same": len(self.same),
            "conflicts": len(self.conflicts),
            "skipped": len(self.skipped),
        }

    def by_kind(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {k: {} for k in SYNC_KINDS}

        def bump(kind: str, key: str) -> None:
            bucket = out.setdefault(kind, {})
            bucket[key] = bucket.get(key, 0) + 1

        for item in self.only_local:
            bump(item.kind, "only_local")
        for row in self.same:
            bump(str(row["kind"]), "same")
        for row in self.only_remote:
            bump(str(row["kind"]), "only_remote")
        for row in self.skipped:
            bump(str(row["kind"]), "skipped")
        for conflict in self.conflicts:
            bump(conflict.kind, "conflicts")
        return {k: v for k, v in out.items() if v}


# ------------------------------------------------------------------ 本机这一侧的收集


#: 一张卡能搬的那些列。**指纹、交出去的载荷、写入端那句"变了没有"比的是同一个形状**，
#: 所以这份清单与下面 `_card_body` 只能有一处（`R102-24` 的修法把翻译放进 `_card_body`，
#: 而 `R102-25` 要问的"内容真变了吗"也必须读同一个函数，否则两处迟早分叉）。
_CARD_COLUMNS = (
    "role_id, role_name, system_prompt, temperature, model_name, tool_whitelist,"
    " exemplars, knowledge_scopes, description, reachout_enabled, recall_enabled,"
    " time_pattern_enabled, affinity_enabled, file_watch_enabled, reachout_keep,"
    " pet_pack, is_builtin, updated_at"
)


def _card_body(row: Any) -> dict[str, Any]:
    """一行 `role_card` → 那张卡的可搬运体。

    两处翻译都在这一个函数里：
    ① 那三列在库里是 **JSON 文本**，而对面收下载荷的 `RoleCardUpdate` 要的是 **list** ——
      从前没人翻译，于是每张带白名单的卡在 import 端必炸（`R102-24`：3/3 张
      `ValidationError: tool_whitelist Input should be a valid list`、`written` 里 card
      一格都没有、而 HTTP 200）。翻译放在**这里**而不是放在写入端，是因为指纹比的就是
      这份 body —— 载荷与指纹必须描述同一个东西，否则"我交出去的那张"与"我宣称它长这样"分叉。
    ② `updated_at` 与 `is_builtin` 不进指纹也不进载荷：两边都是内置卡时它是事实，
      不是用户写的东西；时刻则由下面 `at=` 单独带着走裁决。
    """
    # 延迟导入与 `_write_card` 同一处理：`roles/` 不进 `core/` 的顶层依赖，
    # 而这一列清单的唯一出处在角色服务那边（`CARD_JSON_COLUMNS`）。
    from rolecard_agent.roles.service import CARD_JSON_COLUMNS  # noqa: PLC0415

    # `dict(row)` 而不是 `{k: row[k] for k in row}`：sqlite3.Row 直接迭代给的是
    # **下标**不是列名，那种写法会拿 `row[0]` 去查列名而 IndexError。
    body = dict(row)
    body.pop("updated_at", None)
    body.pop("is_builtin", None)
    for column in CARD_JSON_COLUMNS:
        raw = body.get(column)
        body[column] = None if raw is None else json.loads(raw)
    return body


def collect_cards(conn: SqlConnection, *, user_id: str) -> list[SyncItem]:
    rows = conn.execute(
        f"SELECT {_CARD_COLUMNS} FROM role_card WHERE user_id = ? ORDER BY role_id",
        (user_id,),
    ).fetchall()
    out: list[SyncItem] = []
    for row in rows:
        body = _card_body(row)
        out.append(
            SyncItem(
                kind=KIND_CARD,
                ident=str(row["role_id"]),
                hash=_digest(body),
                at=str(row["updated_at"] or ""),
                preview=_preview(
                    f"{row['role_name']} · {row['description'] or row['system_prompt']}"
                ),
                payload=body,
            )
        )
    return out


def collect_memories(conn: SqlConnection, *, user_id: str) -> list[SyncItem]:
    rows = conn.execute(
        "SELECT id, uid, role_id, text, source, pinned, importance, invalidated_at, created_at"
        " FROM role_memory_item WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    out: list[SyncItem] = []
    for row in rows:
        uid = str(row["uid"] or "")
        body = {
            "text": str(row["text"]),
            "role_id": str(row["role_id"] or ""),
            "source": str(row["source"] or "manual"),
            "pinned": bool(row["pinned"]),
            "importance": int(row["importance"] or 1),
            "invalidated": bool(row["invalidated_at"]),
        }
        out.append(
            SyncItem(
                kind=KIND_MEMORY,
                ident=uid or f"legacy-{row['id']}",
                # 没有 uid 的老行（理论上不该存在，M2b 之后出生即带）退回本机 id + 文本指纹：
                # 它在对面永远配不上，只会成为"本机独有"——那是安全的一侧（不会误判成冲突）。
                hash=_digest(body),
                at=str(row["created_at"] or ""),
                preview=_preview(row["text"]),
                payload=body,
            )
        )
    return out


def collect_reachouts(conn: SqlConnection, *, user_id: str) -> list[SyncItem]:
    """主动开口的投递记录：**只追加，永不判冲突**（身份含文本，改一个字就是另一条）。"""
    rows = conn.execute(
        "SELECT id, role_id, role_name, text, fired_by, state, created_at, seen_at, dismissed_at"
        " FROM agent_reachout WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    out: list[SyncItem] = []
    for row in rows:
        stamp = str(row["created_at"] or "")
        text = str(row["text"] or "")
        ident = f"{row['role_id']}|{stamp}|{_digest(text)}"
        body = {
            "role_id": str(row["role_id"]),
            "role_name": str(row["role_name"] or ""),
            "text": text,
            "fired_by": row["fired_by"],
            "state": str(row["state"] or "unread"),
            "created_at": stamp,
        }
        out.append(
            SyncItem(
                kind=KIND_REACHOUT,
                ident=ident,
                hash=_digest(body),
                at=stamp,
                preview=_preview(text),
                payload=body,
            )
        )
    return out


def collect_threads(
    conn: SqlConnection, *, user_id: str, graph: Any, settings: Any
) -> tuple[list[SyncItem], list[dict[str, Any]]]:
    """会话 = 行 + 检查点里的消息序列。返回 (可搬的, 跳过的)。

    跳过只有一种理由：**这条里有附图/附件**（原件这一版不传）。不做"搬文字丢图"那种
    半搬 —— 症状是"她记得那张单子，你这边却没有"，比不搬更难解释。
    """
    from rolecard_agent.core.graph import build_graph_config

    rows = conn.execute(
        "SELECT thread_id, title, current_role_id, model_name, agent_mode, updated_at"
        " FROM session_thread WHERE user_id = ? ORDER BY thread_id",
        (user_id,),
    ).fetchall()
    out: list[SyncItem] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        tid = str(row["thread_id"])
        try:
            snapshot = graph.get_state(build_graph_config(tid, settings))
        except Exception as exc:  # noqa: BLE001 - 读不到检查点就不搬这条，不拖垮整份计划
            skipped.append({"kind": KIND_THREAD, "ident": tid, "reason": f"读不到历史：{exc}"})
            continue
        messages = list((snapshot.values or {}).get("messages") or [])
        pairs: list[dict[str, Any]] = []
        has_media = False
        for message in messages:
            kind = getattr(message, "type", None)
            if kind not in ("human", "ai"):
                continue  # 工具消息与中间轮不搬：对面的图会自己重跑
            raw = getattr(message, "content", "")
            if isinstance(raw, list):
                has_media = True
                break
            text = str(raw or "")
            if "data:image" in text or "data:application" in text:
                has_media = True
                break
            pairs.append({"role": "user" if kind == "human" else "assistant", "text": text})
        if has_media:
            skipped.append({"kind": KIND_THREAD, "ident": tid, "reason": SKIP_HAS_IMAGE,
                            "preview": _preview(row["title"] or tid)})
            continue
        if not pairs:
            skipped.append({"kind": KIND_THREAD, "ident": tid, "reason": "这条会话还没有内容",
                            "preview": _preview(row["title"] or tid)})
            continue
        out.append(
            SyncItem(
                kind=KIND_THREAD,
                ident=tid,
                hash=_digest(pairs),
                head=_digest(pairs[:HEAD_MESSAGES]),
                count=len(pairs),
                at=str(row["updated_at"] or ""),
                preview=_preview(row["title"] or pairs[0]["text"]),
                payload={
                    "thread_id": tid,
                    "title": row["title"],
                    "current_role_id": row["current_role_id"],
                    "model_name": row["model_name"],
                    "agent_mode": row["agent_mode"],
                    "messages": pairs,
                },
            )
        )
    return out, skipped


def collect(
    conn: SqlConnection, *, user_id: str, graph: Any = None, settings: Any = None
) -> tuple[list[SyncItem], list[dict[str, Any]]]:
    """这一版本机这份的全部可搬条目（+ 跳过的会话）。"""
    items: list[SyncItem] = []
    items += collect_cards(conn, user_id=user_id)
    items += collect_memories(conn, user_id=user_id)
    items += collect_reachouts(conn, user_id=user_id)
    skipped: list[dict[str, Any]] = []
    if graph is not None:
        threads, skipped = collect_threads(conn, user_id=user_id, graph=graph, settings=settings)
        items += threads
    return items, skipped


# ------------------------------------------------------------------ 比对（只在本机跑）


def plan(
    mine: list[SyncItem],
    theirs: list[dict[str, Any]],
    *,
    skipped: list[dict[str, Any]] | None = None,
) -> SyncPlan:
    """把两边清单比成一个计划。**对面独有的东西一律不动。**"""
    by_ident = {(str(r.get("kind")), str(r.get("ident"))): r for r in theirs}
    mine_idents = {(item.kind, item.ident) for item in mine}
    result = SyncPlan(
        only_local=[], only_remote=[], same=[], conflicts=[], skipped=list(skipped or [])
    )
    for item in mine:
        other = by_ident.get((item.kind, item.ident))
        if other is None:
            result.only_local.append(item)
            continue
        if str(other.get("hash")) == item.hash:
            result.same.append(item.brief())
            continue
        # 会话：头一段相同而条数不同 = 一边接着聊下去了，按长的走，不占冲突列表。
        if (
            item.kind == KIND_THREAD
            and str(other.get("head")) == item.head
            and int(other.get("count") or 0) != item.count
        ):
            if item.count > int(other.get("count") or 0):
                result.only_local.append(item)  # 本机更长 ⇒ 推过去
            else:
                result.only_remote.append(other)  # 对面更长 ⇒ 这一条不动（下行不在这一版）
            continue
        result.conflicts.append(Conflict(kind=item.kind, ident=item.ident, mine=item, theirs=other))
    for key, row in by_ident.items():
        if key not in mine_idents:
            result.only_remote.append(row)
    result.remote_counts = {}
    for row in theirs:
        kind = str(row.get("kind"))
        result.remote_counts[kind] = result.remote_counts.get(kind, 0) + 1
    return result


__all__ = [
    "HEAD_MESSAGES",
    "KIND_CARD",
    "KIND_MEMORY",
    "KIND_REACHOUT",
    "KIND_THREAD",
    "SKIP_HAS_IMAGE",
    "SYNC_KINDS",
    "Conflict",
    "SyncItem",
    "SyncPlan",
    "auto_moves",
    "collect",
    "collect_cards",
    "collect_memories",
    "collect_reachouts",
    "collect_threads",
    "plan",
]


# ------------------------------------------------------------------ 登录对账的自动策略


def _newer(a: str, b: str) -> bool:
    """`a` 比 `b` 新吗。两边都是同一份 SQLite 产出的时间串，按字典序比就够；
    空串当最旧（读不到时刻的东西不该赢）。"""
    return bool(a) and a > b


def auto_moves(
    result: SyncPlan,
) -> tuple[list[SyncItem], list[tuple[str, str]], list[Conflict]]:
    """登录对账那一轮的**自动策略**：只走无歧义的那半，其余留给人。

    为什么"两份都留"不进自动策略：它**不幂等** —— 同一条记忆两边各改过一版，"都留"会让
    下一轮对账把复制品当成新东西再复制一遍，每登录一次长出两条。自动档只允许
    "跑两遍第二遍是空转"的那种移动，所以歧义的（记忆、会话的冲突）一律跳过，
    原地等人在向导里裁决 —— 冲突本该是可数的少数。

    卡按 `updated_at` 新者胜：卡是配置，不是回忆，后写入的赢是这一类的行业常规；
    两边都是同一份代码写的时间串，字典序即可。会话不用比 —— 前缀规则已经把
    "一边接着聊"判掉了，剩下的都是真分叉，不猜。

    返回 (本机→对面要推的, 要从对面取的 (kind, ident), 留给人的冲突)。
    """
    push: list[SyncItem] = list(result.only_local)
    pull: list[tuple[str, str]] = [
        (str(row.get("kind")), str(row.get("ident"))) for row in result.only_remote
    ]
    human: list[Conflict] = []
    for c in result.conflicts:
        if c.kind == KIND_CARD:
            if _newer(c.mine.at, str(c.theirs.get("at") or "")):
                push.append(c.mine)
            elif _newer(str(c.theirs.get("at") or ""), c.mine.at):
                pull.append((c.kind, c.ident))
            else:
                human.append(c)
        else:
            human.append(c)
    return push, pull, human


# ------------------------------------------------------------------ 对面那一侧的写入


def _write_card(conn: SqlConnection, *, user_id: str, payload: dict[str, Any]) -> str:
    """建或改一张卡。**归属由视图绑定**，载荷里没有 user_id 的容身之处。

    已存在且**内容一字未变** ⇒ 什么都不写、回 `skipped`。这一格不是可选的礼貌：
    `RoleCards.update` 会盖 `updated_at`，而卡类冲突的裁决是"新者胜且自动执行"，
    于是重复导入会把对面那张的时刻顶到"刚刚"，两边在随后的每次对账里互相盖个没完
    （`R102-25` 的乒乓形态）。判据与 `_card_body` 同源 ⇒ 指纹说"相同"与写入端说"没东西要动"
    永远是同一句话。
    """
    from rolecard_agent.roles.models import RoleCardCreate, RoleCardUpdate
    from rolecard_agent.roles.service import RoleCards

    cards = RoleCards(conn, user_id)
    body = {k: v for k, v in payload.items() if k != "role_id"}
    role_id = str(payload["role_id"])
    if cards.exists(role_id):
        have = conn.execute(
            f"SELECT {_CARD_COLUMNS} FROM role_card WHERE role_id = ? AND user_id = ?",
            (role_id, user_id),
        ).fetchone()
        if have is not None and _card_body(have) == {**body, "role_id": role_id}:
            return "skipped"
        cards.update(role_id, RoleCardUpdate(**body))
        return "updated"
    cards.create(RoleCardCreate(role_id=role_id, **body))
    return "created"


def _write_thread(
    conn: SqlConnection,
    *,
    user_id: str,
    graph: Any,
    settings: Any,
    payload: dict[str, Any],
) -> str:
    """会话 = 行 + 检查点里的消息。整段替换成推过来的那一份。

    消息用 `HumanMessage` / `AIMessage` 重建，**工具调用与中间轮不重建**：对面的图会按
    它自己的插件与工具集重新走一遍，把那边的执行结果硬塞进历史才是错的（那些工具在
    对面可能压根不存在）。
    """
    from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    from rolecard_agent.core.graph import build_graph_config

    tid = str(payload["thread_id"])
    row = conn.execute(
        "SELECT user_id FROM session_thread WHERE thread_id = ?", (tid,)
    ).fetchone()
    if row is not None and str(row["user_id"]) != user_id:
        return "foreign"
    if row is None:
        insert_imported_thread(conn, thread_id=tid, user_id=user_id, payload=payload)
    else:
        update_imported_thread(conn, thread_id=tid, user_id=user_id, payload=payload)
    messages: list[Any] = []
    for item in payload.get("messages") or []:
        role = str(item.get("role") or "")
        text = str(item.get("text") or "")
        if role == "user":
            messages.append(HumanMessage(content=text))
        elif role == "assistant":
            messages.append(AIMessage(content=text))
    config = build_graph_config(tid, settings)
    # 整段替换是"先把这一会话的检查点清空、再把对面那份写进来"，它必须在锁里做完整（R28-03）：
    # 中间插进用户那一轮的 `stream`，两边会各自基于同一个父检查点分叉，后写的把先写的盖掉
    # —— 而这一条链路盖掉的是一整段历史，不是单条消息。
    with thread_write(tid):
        graph.update_state(
            config, {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]}
        )
    return "created" if row is None else "updated"


#: 写入顺序 = 依赖顺序：会话行引用角色卡，所以卡必须先到。
_IMPORT_ORDER = (KIND_CARD, KIND_MEMORY, KIND_REACHOUT, KIND_THREAD)


def apply_import(
    conn: SqlConnection,
    *,
    user_id: str,
    graph: Any,
    settings: Any,
    items: list[dict[str, Any]],
    commit: bool = True,
) -> dict[str, Any]:
    """把一批选中的条目写进**这台机器**（对面那一侧调的就是它）。

    归属只认一个来源：`user_id` 由调用方从**这次请求解析出的身份**给（见 routers/sync.py），
    载荷里的 `user_id` 一概不读 —— 那等于让发送方指定"这些数据属于谁"。

    `commit=False`：不在结尾提交，由调用方把本批写入与其事务里的其它写（整份替换的
    行类清空）一起收口 —— 2026-10-04 审查快照的数据丢失条目：replace 档"清了不导"
    要能整批回滚，清空与导入必须共事务。
    """
    written: dict[str, int] = {}
    skipped: dict[str, int] = {}
    errors: list[dict[str, str]] = []

    def bump(table: dict[str, int], kind: str) -> None:
        table[kind] = table.get(kind, 0) + 1

    def _rank(row: dict[str, Any]) -> int:
        # 未知 kind 排到最后：排序本身不许再炸（`R102-48`）—— 入口的入域校验拒掉之后，
        # 直连本函数的调用方（测试/内部）遇到的未知 kind 由下面 else 里的 raise 兜成
        # per-item error，而不是一行 ValueError 让整批半途而废。
        item_kind = str(row.get("kind") or "")
        return _IMPORT_ORDER.index(item_kind) if item_kind in _IMPORT_ORDER else len(_IMPORT_ORDER)

    ordered = sorted(items, key=_rank)
    for row in ordered:
        kind = str(row.get("kind") or "")
        payload = dict(row.get("payload") or {})
        ident = str(row.get("ident") or "")
        try:
            if kind == KIND_CARD:
                outcome = _write_card(conn, user_id=user_id, payload=payload)
            elif kind == KIND_MEMORY:
                outcome = restore_memory(conn, user_id=user_id, payload=payload, uid=ident)
            elif kind == KIND_REACHOUT:
                outcome = restore_reachout(conn, user_id=user_id, payload=payload)
            elif kind == KIND_THREAD:
                outcome = _write_thread(
                    conn, user_id=user_id, graph=graph, settings=settings, payload=payload
                )
            else:
                # 未知 kind 不是"跳过"——静默吞掉会让发送方以为写进去了（本仓判据纪律：
                # 不把未知报成正常）。抛给上面的 per-item except，折进 errors 里回来。
                raise ValueError(f"未知的同步 kind：{kind!r}")
        except Exception as exc:  # noqa: BLE001 - 一条坏的不该让整批回滚成"什么都没发生"
            errors.append(
                {"kind": kind, "ident": ident, "error": f"{type(exc).__name__}: {exc}"[:200]}
            )
            continue
        if outcome in {"created", "updated"}:
            bump(written, kind)
        else:
            bump(skipped, kind)
            if outcome == "foreign":
                errors.append({"kind": kind, "ident": ident, "error": "这条身份已经属于别人"})
    if commit:
        conn.commit()
    return {"written": written, "skipped": skipped, "errors": errors}
