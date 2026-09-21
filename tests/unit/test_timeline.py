"""事件簿那条轴（`core/timeline.py`）的用例。

钉的是四件容易做错的事：

  1. **顺序与游标**：三类源并进来按时间倒序，翻页**不重不漏** —— 尤其同一秒里的事件
     （收件箱那次已经踩过"时间戳打平"的坑），所以比较用的是 `(at, kind, ref_id)` 三元组
     而不是裸时间；
  2. **"更正"要有被划掉的那条**：`invalidated_at` + `superseded_by` 是 §3"失效不删"留下的
     版本链，轴是它第一次被用户看见的地方，链断了这里就看不出来；
  3. **不给死链**：主动会话不存在时 `thread_id` 必须是 None（与收件箱同一口径），
     否则点一下把人送进"加载历史失败"；
  4. **坏游标不 500**：宁可从头再给一页。
"""

from __future__ import annotations

from typing import Any

import pytest

from rolecard_agent.core import memory as mem
from rolecard_agent.core import timeline
from rolecard_agent.storage.db import SqlConnection, bootstrap, connect

ROLE = "elysia"


@pytest.fixture
def conn() -> Any:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=())
    # 轴本身不读用户，但 `session_thread.user_id` 是指向外键的：要有会话锚点就得先有人。
    c.executescript(
        "INSERT INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');"
        "INSERT INTO app_user (user_id, tenant_id, display_name) VALUES ('u1', 't1', 'u');"
    )
    c.commit()
    return c


def at(day: int, hhmmss: str) -> str:
    return f"2026-09-{day:02d} {hhmmss}"


def add_reachout(c: SqlConnection, text: str, when: str) -> int:
    cur = c.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text, state, created_at) "
        "VALUES (?, '爱莉希雅', ?, 'unread', ?)",
        (ROLE, text, when),
    )
    c.commit()
    return int(cur.lastrowid or 0)


def add_thread(
    c: SqlConnection, tid: str, created: str, updated: str, title: str, *, role: str = ROLE
) -> None:
    c.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, title,"
        " created_at, updated_at) VALUES (?, 'u1', ?, ?, ?, ?)",
        (tid, role, title, created, updated),
    )
    c.commit()


def kinds_of(page: dict[str, Any]) -> list[str]:
    return [str(i["kind"]) for i in page["items"]]


def texts_of(page: dict[str, Any]) -> list[str]:
    return [str(i["text"]) for i in page["items"]]


# ---------------------------------------------------------------- 合并与顺序


def test_merges_three_sources_newest_first(conn: SqlConnection) -> None:
    add_thread(conn, "s_a", at(10, "09:00:00"), at(12, "20:00:00"), "腰疼怎么缓解")
    add_reachout(conn, "今天腰还酸吗？", at(11, "08:30:00"))
    mem.add_item(conn, bucket=ROLE, text="用户每周三晚上练琴", source="extract")
    conn.execute(
        "UPDATE role_memory_item SET created_at = ? WHERE text = ?",
        (at(12, "10:00:00"), "用户每周三晚上练琴"),
    )
    conn.commit()

    page = timeline.build(conn, role_id=ROLE)
    # 会话的两个锚点 + 一条主动 + 一条记忆，按时间倒序。**轴给数据不给文案**：
    # "记下：/ 开始聊："这些中文动词是界面的事，后端替界面造句就等于把文案写进库。
    assert texts_of(page) == [
        "腰疼怎么缓解",
        "用户每周三晚上练琴",
        "今天腰还酸吗？",
        "腰疼怎么缓解",
    ]
    assert kinds_of(page) == ["thread", "memory", "reachout", "thread"]
    verbs = [i["verb"] for i in page["items"]]
    assert verbs == ["recent", None, None, "start"]


def test_same_second_events_keep_a_stable_order(conn: SqlConnection) -> None:
    """同一秒里按**种类正序**（主动 → 更正 → 记下 → 会话），且一条都不吞。

    排序用了一次 `reverse=True`，它会把每个分量一起翻过去 —— 种类不取负就会变成倒序，
    于是"同一秒"的表现随种类漂移，翻页时还会重不漏地翻车。这条就是钉住那个符号。
    """
    same = at(15, "10:00:00")
    add_thread(conn, "s_same", same, at(16, "10:00:00"), "同一秒的会话")
    add_reachout(conn, "同一秒的主动", same)
    item = mem.add_item(conn, bucket=ROLE, text="用户住在上海")
    assert item is not None
    conn.execute("UPDATE role_memory_item SET created_at = ? WHERE id = ?", (same, item["id"]))
    conn.commit()

    page = timeline.build(conn, role_id=ROLE)
    # 会话在轴上有两个锚点（16 号的"最近聊"排在最前，15 号同一秒里是"开始聊"排在最后）；
    # 同一秒内部按种类正序：主动 → 记下 → 会话。
    assert kinds_of(page) == ["thread", "reachout", "memory", "thread"]
    assert [i["verb"] for i in page["items"]] == ["recent", None, None, "start"]


# ---------------------------------------------------------------- 版本链


def test_superseded_item_becomes_a_correction_event(conn: SqlConnection) -> None:
    old = mem.add_item(conn, bucket=ROLE, text="用户住在上海", source="manual")
    assert old is not None
    fresh = mem.add_item(conn, bucket=ROLE, text="用户去年搬到北京了")
    assert fresh is not None
    mem.invalidate_item(conn, item_id=int(str(old["id"])), superseded_by=int(str(fresh["id"])))
    conn.execute(
        "UPDATE role_memory_item SET invalidated_at = ? WHERE id = ?",
        (at(20, "10:00:00"), old["id"]),
    )
    conn.commit()

    page = timeline.build(conn, role_id=ROLE)
    corrections = [i for i in page["items"] if i["kind"] == "memory_correct"]
    assert len(corrections) == 1
    assert corrections[0]["text"] == "用户去年搬到北京了"
    assert corrections[0]["from_text"] == "用户住在上海"  # 被划掉的那条要看得见


def test_invalidate_without_replacement_has_empty_new_text(conn: SqlConnection) -> None:
    """整理时"只作废不写替代"（INVALID 允许不带新事实）：那就显示作废了什么，不编一条"改成了"。"""
    item = mem.add_item(conn, bucket=ROLE, text="用户在读研")
    assert item is not None
    mem.invalidate_item(conn, item_id=int(str(item["id"])), superseded_by=None)
    page = timeline.build(conn, role_id=ROLE)
    correction = next(i for i in page["items"] if i["kind"] == "memory_correct")
    assert correction["text"] == "" and correction["from_text"] == "用户在读研"
    assert correction["verb"] == "correct"


# ---------------------------------------------------------------- 链接与过滤


def test_reachout_links_only_when_the_proactive_session_exists(conn: SqlConnection) -> None:
    add_reachout(conn, "有会话的那条", at(18, "10:00:00"))
    assert timeline.build(conn, role_id=ROLE)["items"][0]["thread_id"] is None

    add_thread(conn, f"s_proactive_{ROLE}", at(18, "09:00:00"), at(18, "09:30:00"), "与爱莉希雅")
    page = timeline.build(conn, role_id=ROLE)
    reachout = next(i for i in page["items"] if i["kind"] == "reachout")
    assert reachout["thread_id"] == f"s_proactive_{ROLE}"


def test_kinds_filter_keeps_corrections_with_memory(conn: SqlConnection) -> None:
    old = mem.add_item(conn, bucket=ROLE, text="用户住在上海")
    assert old is not None
    mem.invalidate_item(conn, item_id=int(str(old["id"])), superseded_by=None)
    add_reachout(conn, "别的一条", at(19, "10:00:00"))

    only_memory = timeline.build(conn, role_id=ROLE, kinds=("memory", "memory_correct"))
    assert set(kinds_of(only_memory)) == {"memory", "memory_correct"}
    assert "reachout" not in kinds_of(only_memory)


# ---------------------------------------------------------------- 翻页


def test_cursor_pages_without_duplicates_or_gaps(conn: SqlConnection) -> None:
    for day in range(1, 8):
        add_reachout(conn, f"第 {day} 天的主动", at(day, "12:00:00"))
    first = timeline.build(conn, role_id=ROLE, limit=3)
    assert len(first["items"]) == 3 and first["next_cursor"]
    second = timeline.build(conn, role_id=ROLE, limit=3, before=str(first["next_cursor"]))
    assert len(second["items"]) == 3
    third = timeline.build(conn, role_id=ROLE, limit=3, before=str(second["next_cursor"]))
    assert len(third["items"]) == 1 and third["next_cursor"] is None

    seen = [str(i["text"]) for page in (first, second, third) for i in page["items"]]
    assert len(seen) == 7 and len(set(seen)) == 7  # 不重不漏


def test_garbage_cursor_is_not_an_error(conn: SqlConnection) -> None:
    add_reachout(conn, "随便一条", at(3, "10:00:00"))
    for bad in ("", "not-a-cursor", "2026-09-03 10:00:00|nonsense|1"):
        page = timeline.build(conn, role_id=ROLE, before=bad)
        assert len(page["items"]) == 1  # 坏游标当"没有"：宁可重头给一页


def test_threads_of_other_roles_do_not_leak(conn: SqlConnection) -> None:
    add_thread(
        conn, "s_other", at(4, "10:00:00"), at(4, "11:00:00"), "别人的会话",
        role="general_assistant",
    )
    add_reachout(conn, "我的那条", at(5, "10:00:00"))
    page = timeline.build(conn, role_id=ROLE)
    assert all("别人的会话" not in str(i["text"]) for i in page["items"])
    assert kinds_of(page) == ["reachout"]
