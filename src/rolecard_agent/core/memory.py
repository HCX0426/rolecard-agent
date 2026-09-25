"""跨会话记忆：条目表 `role_memory_item` + AI 可直接调用的写入工具。

## 形状：条目，不是一坨文本

以前是"一条大文本"（全局在 `kernel_meta:memory:facts`、每角色在 `role_memory.value`）。
一坨文本没法逐条管理，于是没有"这条过期了 / 这条被新事实取代 / 这条别再用"的概念 ——
记忆只会越长越浑，而错事实粘滞。现在每条事实是一行，带：

  * `source`（manual / chat / proactive / extract / seed）—— 从哪来的，出问题时能查；
  * `pinned` —— 钉住的不参与淘汰、不被整理覆盖；
  * `hit_count` + `last_hit_at` —— 被注入过几次、最近什么时候，退役排序的依据；
  * `importance`（0 随口 / 1 常规 / 2 要紧）—— 显著性，提取时由模型标、钳在 `clamp_importance`；
  * `invalidated_at` + `superseded_by` —— **失效不物理删**（可撤销、可调试、可回滚）。

旧的 blob 表原样留着不删列（迁移纪律），但它**不再是事实面**：注入、面板、工具都只认这张表。
老库里那些 blob 不迁移（用户 2026-09-20 明确"旧的记忆数据也可以不要了"）。

## 谁写记忆

  * `memory_save` 内核工具：AI 在对话中检测到**用户明确说出的、可复用的**事实时调用；
    同时写入全局桶与当前角色桶（跨角色的隔离铁律见架构总览 §5 不变式 8）。
  * 面板：逐条增删改与钉住，是人工的最终仲裁面。
  * 「提取精华 / 整理记忆」（见 `memory_distill.py`）把长对话压成条目 —— 那才是一次模型调用。

## 边界与不变式

  * 总开关 `MEMORY_ENABLED`：关掉后注入、工具、提取全停（面板仍可编辑，只存不用）。
  * **注入必须有上界**：条数上限（`MAX_ITEMS_PER_BUCKET`，超出即淘汰最弱的）+ 字符预算
    （`MAX_MEMORY_CHARS`）。记忆是 system prompt 的一部分，没有上界迟早挤掉对话本身。
  * "哪一轮该注入什么"只有 `memory_for_turn` 一处实现（对话与主动开口同源）。
  * 记忆区在 prompt 里自带"以用户最新说法为准"的降权声明：一条被提示注入污染的记忆
    不会压过用户当下的明确说法（软层规则，硬门仍是 core/guard.py）。
"""

from __future__ import annotations

import math
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import BaseTool, tool

from rolecard_agent.config import Settings
from rolecard_agent.storage.db import SqlConnection

# 当前对话角色（架构总览 §5）：execute_tools 每轮注入，memory_save 读取它把事实同时写入
# 该角色专属记忆。默认空串 = 无角色上下文，此时只写全局桶。与 role_knowledge_scopes_ctx
# 同一机制（ContextVar + copy_context 跨工具线程）。
current_role_id_ctx: ContextVar[str] = ContextVar("current_role_id", default="")

#: 全局（用户级）记忆用的桶名。空串而不是 NULL：NULL 在唯一约束与 WHERE 里都是麻烦源。
GLOBAL_BUCKET = ""

MAX_MEMORY_CHARS = 4000
# 每桶 active 条目上限。写死不做成配置：它是"记忆不会吃掉 prompt"这条保底约束的实现细节，
# 多一个旋钮就多一条要测、要解释、会被误配的路径。
MAX_ITEMS_PER_BUCKET = 200
# 单条事实的字符上限：一条"事实"该是一句话，超长的多半是没拆解的对话片段。
MAX_ITEM_CHARS = 200

_COLUMNS = (
    "id, role_id, text, source, pinned, hit_count, importance, last_hit_at, invalidated_at, "
    "superseded_by, created_at, updated_at"
)


def _parse_ts(raw: object) -> datetime | None:
    """sqlite 的 `CURRENT_TIMESTAMP` 是 UTC 文本；解析失败一律 None（不参与近因加权）。"""
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _row_to_item(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "role_id": str(row["role_id"]),
        "text": str(row["text"]),
        "source": str(row["source"]),
        "pinned": bool(row["pinned"]),
        "hit_count": int(row["hit_count"] or 0),
        "importance": int(row["importance"] if row["importance"] is not None else 1),
        "last_hit_at": row["last_hit_at"],
        "invalidated_at": row["invalidated_at"],
        "superseded_by": None if row["superseded_by"] is None else int(row["superseded_by"]),
        "created_at": row["created_at"],
    }


def list_items(
    conn: SqlConnection, *, bucket: str, include_invalidated: bool = False
) -> list[dict[str, Any]]:
    """某个桶的条目。默认只给 active —— 失效的留着是为了能查、能撤销，不是为了注入。"""
    where = "role_id = ?" if include_invalidated else "role_id = ? AND invalidated_at IS NULL"
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM role_memory_item WHERE {where} ORDER BY id DESC", (bucket,)
    ).fetchall()
    return [_row_to_item(r) for r in rows]


#: 半衰期（天）：比它短会让"低频但重要"的事实反复进出 prompt（模型表现会跳），比它长就退化成纯频次。
RECENCY_HALFLIFE_DAYS = 30.0
#: 频次那一维的饱和点：被注入过 24 次就算"常提"，再多不再加分。
#: 没有上限的话，一条被反复命中的口癖可以靠次数把"青霉素过敏"压下去 —— 而次数只说明
#: 它被提过，不说明它更要紧。
HIT_CEIL = 24
#: 显著性的档数（0/1/2 → 除完正好落在 0、0.5、1）。
IMPORTANCE_TIERS = 2
_W_RECENCY, _W_HITS, _W_IMPORTANCE = 0.35, 0.30, 0.35


def _score(item: dict[str, Any], *, now: datetime) -> float:
    """三维各归一到 0~1 再**加权相加**：近因 0.35 + 频次 0.30 + 显著性 0.35（设计稿 §8.5 的 P2）。

    为什么不再是 `近因 × log1p(hits)`（乘法）：乘法里任何一维趋零就把另两维一笔抹掉，
    于是"三年没被提起的青霉素过敏"和"三年没被提起的随口一句"分数一样 —— 缺的正是显著性，
    而它被乘进了零里。加法允许"某一维很低但另两维撑住"，这才是要的形状。

    为什么**固定上限**归一，而不是按桶内 min-max：后者会让"新加一条事实"把整桶的分数重标一遍
    —— 旧事实只应被新事实超过，不该因为别人来了而自己掉分。上限写死在常数里，
    一条事实的分数就只取决于它自己。
    """
    anchor = _parse_ts(item.get("last_hit_at")) or _parse_ts(item.get("created_at"))
    age_days = (now - anchor).total_seconds() / 86400.0 if anchor else 3650.0
    recency = 0.5 ** (max(0.0, age_days) / RECENCY_HALFLIFE_DAYS)
    hits = min(1.0, math.log1p(int(item.get("hit_count") or 0)) / math.log1p(HIT_CEIL))
    raw = item.get("importance")
    importance = 1.0 if raw is None else max(0.0, min(1.0, int(raw) / IMPORTANCE_TIERS))
    return _W_RECENCY * recency + _W_HITS * hits + _W_IMPORTANCE * importance


def ranked_active(
    conn: SqlConnection, *, bucket: str, now: datetime | None = None
) -> list[dict[str, Any]]:
    """注入顺序：钉住的在前，其余按 近因×频次。"""
    stamp = now or datetime.now(UTC)
    items = list_items(conn, bucket=bucket)
    return sorted(items, key=lambda i: (not i["pinned"], -_score(i, now=stamp), -i["id"]))


def clamp_importance(raw: object) -> int:
    """把任何来源写的显著性钳进 0..2。

    单点存在是因为写它的有三处（手动面板、提取模型的 `ADD [n]`、memory_save 工具），
    而提取模型给什么数字都不奇怪（给 5、给 "很高" 都有可能）。CHECK 约束挡不住旧库，
    钳在这里同时管住两条路，坏值一律退回中间档 1 —— 不替用户把要紧的事降级。
    """
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 1
    return max(0, min(IMPORTANCE_TIERS, value))


def add_item(
    conn: SqlConnection,
    *,
    bucket: str,
    text: str,
    source: str = "manual",
    importance: int = 1,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """新增一条事实。**完全相同的文本不重复插入**（只刷新那一条的时间），并做超限淘汰。

    这里**刻意不做"同义判重"**（任务 #9 的结论，实测依据写在 `docs/架构审计.md` §12.7 末）：
    字面度量认不出"同一句式换个值"与"同一件事换个说法"的区别 —— 字符二元组 Jaccard ≥ 0.40
    会把「用户住在上海」与「用户住在苏州」并成一条（0.43）。误并是**吞掉一条真事实**，
    而漏并只是留着一眼看得见的重复，还有「整理记忆」按钮（模型判断 + 用户发起）能收拾。

    重复那条**只抬显著性、不压**（`max`）：第二次说"我青霉素过敏"带 `[2]` 时该升上去，
    而提取模型这次没标号（默认 1）不该把一条已经标成要紧的降回常规 —— 降级的判断该由
    「整理记忆」或用户来做，不该是"再提一次"的副作用。

    返回 None = 文本为空，什么都没做。
    """
    line = " ".join((text or "").split())[:MAX_ITEM_CHARS]
    if not line:
        return None
    tier = clamp_importance(importance)
    existing = conn.execute(
        "SELECT id FROM role_memory_item WHERE role_id = ? AND text = ? AND invalidated_at IS NULL",
        (bucket, line),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE role_memory_item SET last_hit_at = CURRENT_TIMESTAMP,"
            " updated_at = CURRENT_TIMESTAMP, importance = MAX(importance, ?) WHERE id = ?",
            (tier, existing["id"]),
        )
        conn.commit()
        return get_item(conn, int(existing["id"]))
    conn.execute(
        "INSERT INTO role_memory_item (role_id, text, source, importance) VALUES (?, ?, ?, ?)",
        (bucket, line, source, tier),
    )
    conn.commit()
    created = conn.execute(
        f"SELECT {_COLUMNS} FROM role_memory_item WHERE role_id = ? ORDER BY id DESC LIMIT 1",
        (bucket,),
    ).fetchone()
    enforce_cap(conn, bucket=bucket, now=now)
    return _row_to_item(created) if created else None


def get_item(conn: SqlConnection, item_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM role_memory_item WHERE id = ?", (item_id,)
    ).fetchone()
    return None if row is None else _row_to_item(row)


def edit_item(conn: SqlConnection, *, item_id: int, text: str) -> dict[str, Any] | None:
    """人工修正一条（面板上的"编辑"）。空文本 = 不改，交给删除去做那件事。"""
    line = " ".join((text or "").split())[:MAX_ITEM_CHARS]
    if not line:
        return get_item(conn, item_id)
    conn.execute(
        "UPDATE role_memory_item SET text = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (line, item_id),
    )
    conn.commit()
    return get_item(conn, item_id)


def set_pinned(conn: SqlConnection, *, item_id: int, pinned: bool) -> dict[str, Any] | None:
    conn.execute(
        "UPDATE role_memory_item SET pinned = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (1 if pinned else 0, item_id),
    )
    conn.commit()
    return get_item(conn, item_id)


def set_importance(
    conn: SqlConnection, *, item_id: int, importance: object
) -> dict[str, Any] | None:
    """面板上那一档「这条要紧」。与 `pinned` 是两件事：钉住 = 不进淘汰/整理池，
    显著性 = 还在池里时排多前。坏值经 `clamp_importance` 退回中间档，不报错。"""
    conn.execute(
        "UPDATE role_memory_item SET importance = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (clamp_importance(importance), item_id),
    )
    conn.commit()
    return get_item(conn, item_id)


def delete_item(conn: SqlConnection, *, item_id: int) -> bool:
    """用户明确删除 = **物理删**（他要它消失，留个"已删除"的行只是把隐私留在盘上）。

    与自动退役相反：那条走 `invalidate_item`，可撤销。
    """
    cur = conn.execute("DELETE FROM role_memory_item WHERE id = ?", (item_id,))
    conn.commit()
    return cur.rowcount > 0


def invalidate_item(
    conn: SqlConnection, *, item_id: int, superseded_by: int | None = None
) -> dict[str, Any] | None:
    """退役一条：只写标记，不删行 —— 整理错了能回滚，也留得下"谁取代了谁"。"""
    conn.execute(
        "UPDATE role_memory_item SET invalidated_at = CURRENT_TIMESTAMP, superseded_by = ?,"
        " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (superseded_by, item_id),
    )
    conn.commit()
    return get_item(conn, item_id)


def enforce_cap(
    conn: SqlConnection, *, bucket: str, now: datetime | None = None
) -> list[int]:
    """把 active 条数压回上限，返回被退役的 id。

    **这里刻意不做模型整理**：整理（合并/改写）要调模型，不能在写入路径上顺手发起 ——
    那会让"记一条事实"变成一次慢调用、且失败不可见。所以超限时只做保底淘汰（最弱的转失效）
    并在界面上提示"建议整理"，真正的合并是用户点「整理记忆」时发生的一次显式调用。
    """
    stamp = now or datetime.now(UTC)
    active = [i for i in list_items(conn, bucket=bucket) if not i["pinned"]]
    if len(active) <= MAX_ITEMS_PER_BUCKET:
        return []
    weakest = sorted(active, key=lambda i: (_score(i, now=stamp), i["id"]))
    retired: list[int] = []
    for item in weakest[: len(active) - MAX_ITEMS_PER_BUCKET]:
        invalidate_item(conn, item_id=item["id"])
        retired.append(item["id"])
    return retired


#: 一轮最多注入几条记忆。**这是"条数"上界，和 `MAX_MEMORY_CHARS`（字符预算）是两回事**：
#: 只卡字符，4000 字能塞进几十条短句，而 LoCoMo 那篇实测 **top-5 observations 优于 top-50**
#: （41.4 vs 37.8）—— 记忆给多了不是"更全"，是给模型一堆互相竞争的事实，它抓哪条都不稳。
#: 200 条/桶是"存"的上界，8 条是"用"的上界，两者不冲突。
MAX_ITEMS_PER_TURN = 8


def _age_label(raw: object, *, now: datetime) -> str:
    """条目后面的时间标签。为什么要带：注入文本里一句"用户膝盖不舒服"没有保质期，
    模型会当成"此刻仍然如此"来说 —— 而事实会变（搬家、换工作、复查之后指标正常了）。
    Zep 把 `valid_at/invalid_at` 直接写进检索结果，就是这个道理。
    """
    created = _parse_ts(raw)
    if created is None:
        return ""
    days = (now - created).days
    if days <= 0:
        return "今天 "
    if days == 1:
        return "昨天 "
    if days < 30:
        return f"{days}天前 "
    return f"{created.month}月{created.day}日 "


def render_memory(
    conn: SqlConnection,
    *,
    bucket: str,
    budget: int = MAX_MEMORY_CHARS,
    limit: int = MAX_ITEMS_PER_TURN,
    now: datetime | None = None,
    with_age_labels: bool = False,
) -> tuple[str, list[int]]:
    """渲染成文本，同时返回**真的进了文本**的那些条目 id（供命中计数）。

    截断按条目为单位：半句话被切掉，模型会把那半句当完整事实用。

    `with_age_labels` **只对注入侧开**（`memory_for_turn`），设置面板那一侧必须关着：
    面板把这段文本原样填进编辑框、保存时又整段按行覆写条目（`SettingsPage` 的
    `setMemDraft(m.content)`）。标签一旦进这段文本，用户点一次保存「今天」就成了事实正文
    的一部分，再存一次叠一层 —— 一个只读的显示字段被写回了存储层。
    """
    stamp = now or datetime.now(UTC)
    used: list[str] = []
    ids: list[int] = []
    total = 0
    for item in ranked_active(conn, bucket=bucket):
        if len(used) >= limit:
            break
        head = _age_label(item["created_at"], now=stamp) if with_age_labels else ""
        # 「要紧」只挂在注入侧（与时间标签同一条纪律）：面板那段文本会被原样填进编辑框、
        # 保存时整行覆写回条目，标记进了面板文本就等于被写回存储层。
        mark = ""
        if with_age_labels and int(item.get("importance") or 0) >= IMPORTANCE_TIERS:
            mark = "【要紧】"
        line = f"- {head}{mark}{item['text']}"
        if total + len(line) + 1 > budget:
            break
        used.append(line)
        ids.append(item["id"])
        total += len(line) + 1
    return "\n".join(used), ids


def mark_hit(conn: SqlConnection, *, item_id: int) -> None:
    conn.execute(
        "UPDATE role_memory_item SET hit_count = hit_count + 1, last_hit_at = CURRENT_TIMESTAMP"
        " WHERE id = ?",
        (item_id,),
    )
    conn.commit()


def memory_for_turn(conn: SqlConnection, settings: Settings, role_id: str | None) -> str:
    """以某个角色为锚点的一轮该注入什么记忆 —— **对话与主动开口共用这一份规则**。

    总开关关掉 → 空串；给了角色 → 先取该角色的条目，为空则回退用户级全局桶（全局存的是
    用户事实，不是别的角色的对话，所以回退不构成跨角色串扰）；没给角色 → 全局。

    顺手记命中：只有真进了 prompt 的条目才涨 hit_count —— 这就是退役排序里"频次"那一半的
    唯一来源，所以它必须长在注入这条路径上，而不是另开一个统计口。
    """
    if not settings.memory_enabled:
        return ""
    buckets = [role_id, GLOBAL_BUCKET] if role_id else [GLOBAL_BUCKET]
    for bucket in buckets:
        text, ids = render_memory(conn, bucket=bucket, with_age_labels=True)
        if text:
            for item_id in ids:
                mark_hit(conn, item_id=item_id)
            return text
    return ""


def top_active_item(conn: SqlConnection, *, bucket: str) -> dict[str, Any] | None:
    """recall 档的素材：此刻最该被提起的那一条。没有 = None（调用方就不该走回忆口吻）。"""
    ranked = ranked_active(conn, bucket=bucket)
    return ranked[0] if ranked else None


#: 面板文本每行前面那个列表符号。`render_memory` 吐 `- 事实`，而设置面板把这段文本**原样
#: 填进编辑框**、保存时又整段按行覆写条目 —— 于是解析侧必须把它当显示用的装饰剥掉，
#: 不然就是往返叠加：实测存三次变成 `- - - 用户住在上海`，而这串破折号会跟着进 prompt。
#: （剥 `[–—•*] ` 这几种是因为用户会手敲别的符号；剥多次是因为旧库里已经存着叠好的。）
_BULLET = re.compile(r"^\s*(?:[-–—•*]\s*)+")


def replace_bucket_from_text(
    conn: SqlConnection, *, bucket: str, text: str, source: str = "manual"
) -> list[dict[str, Any]]:
    """整段文本 → 该桶的非钉住条目（保留旧 PUT /api/settings/memory 的"覆写"语义）。

    一行一条；钉住的条目**不动** —— 用户特意钉的东西不该被一次整段保存抹掉。
    行首的列表符号会被剥掉，理由见 `_BULLET`：这段文本的**来源就是 `render_memory`**，
    不剥就是自己造的显示格式被自己当成正文反复吃进去。

    **显著性按"同一句话"带过去**：这一段先抄下 `文本 → 档号`，覆写时原样还回去。
    理由不是兼容，而是**这一段文本里根本没有档位信息** —— 它是 `render_memory` 的输出，
    档号只在【要紧】那个前缀里露一下，还只在带时间标签的注入渲染里露。用户在这里编辑的
    是"话"，不是"行"，一次保存不该顺手把逐条面板上标好的档号抹回 1（那条事实下次就可能
    被挤出 prompt）。改过的行（文本不再逐字相同）落回默认档：那确实是一句新话。
    """
    tiers = {
        " ".join(str(i["text"]).split()): clamp_importance(i.get("importance"))
        for i in list_items(conn, bucket=bucket)
        if not i["pinned"]
    }
    conn.execute(
        "DELETE FROM role_memory_item WHERE role_id = ? AND pinned = 0",
        (bucket,),
    )
    conn.commit()
    for line in (text or "").splitlines():
        body = _BULLET.sub("", line)
        add_item(
            conn,
            bucket=bucket,
            text=body,
            source=source,
            importance=tiers.get(" ".join(body.split()), 1),
        )
    return [i for i in list_items(conn, bucket=bucket) if i["pinned"]]


def make_memory_tool(*, settings: Settings, conn: SqlConnection) -> BaseTool:
    """构建 `memory_save` 内核工具（闭包持构建期连接与配置，与 fs 工具同一约定）。"""

    @tool("memory_save")
    def memory_save(fact: str) -> str:
        """把用户**明确说出**的、值得长期记住的可靠事实存入跨会话记忆（会永久记忆，直到用户在设置里删除）。

        只在对话里出现明确的长期事实时才用：比如用户的称呼、身份、居住地、固定偏好、
        常做事物的关键背景。一次性信息、当下即可完成的任务不要记。保存后向用户确认。
        """
        if not settings.memory_enabled:
            return "跨会话记忆功能未启用，无法写入。"
        line = " ".join((fact or "").split())
        if not line:
            return "没有可记住的内容：传入的 fact 为空。"
        added_global = add_item(conn, bucket=GLOBAL_BUCKET, text=line, source="chat")
        role_id = current_role_id_ctx.get()
        if role_id:
            add_item(conn, bucket=role_id, text=line, source="chat")
        count = len(list_items(conn, bucket=role_id or GLOBAL_BUCKET))
        return f"已记住：{line}（该桶现有 {count} 条事实）。" if added_global else "没有写入。"

    return memory_save


__all__ = [
    "GLOBAL_BUCKET",
    "MAX_ITEMS_PER_BUCKET",
    "MAX_ITEMS_PER_TURN",
    "MAX_ITEM_CHARS",
    "MAX_MEMORY_CHARS",
    "add_item",
    "current_role_id_ctx",
    "delete_item",
    "edit_item",
    "enforce_cap",
    "get_item",
    "invalidate_item",
    "list_items",
    "mark_hit",
    "memory_for_turn",
    "make_memory_tool",
    "ranked_active",
    "render_memory",
    "replace_bucket_from_text",
    "set_pinned",
    "top_active_item",
]
