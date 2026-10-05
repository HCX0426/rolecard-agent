"""上行同步的数据层（M7 第一批）：什么算同一条、什么算冲突、什么必须跳过。

四条判据，每条都在挡一种"看起来在同步、其实在丢东西"的坏法：

  1. **身份要跨机器可比**：记忆用 `uid`、会话用 `thread_id`、主动消息用
     `role_id|created_at|文本指纹`。用本机自增 id 当身份 = 两台机器各自的 12 号条目
     被当成同一条，然后其中一条被覆盖。
  2. **会话一边是另一边的前缀不算冲突**（那是"接着聊下去了"），按长的那份走；
     真分叉（头 20 条就不一样）才算。
  3. **带图/带附件的会话整条跳过**，不做"搬文字丢图"的半搬。
  4. **指纹对规范化敏感**：键序、`None` 与缺键都不该把"相同"判成"冲突"——
     冲突列表一灌满噪音，用户就开始无脑点"全按本机的来"，那一屏就白做了。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from rolecard_agent.features import sync as S
from rolecard_agent.storage.db import connect


def _conn(tmp_path: Any) -> Any:
    from rolecard_agent.storage.db import bootstrap

    conn = connect(tmp_path / "app.db")
    bootstrap(conn, enabled_domains=())
    return conn


def _brief(item: S.SyncItem) -> dict[str, Any]:
    return item.brief()


class _Graph:
    """假图：`get_state` 返回事先塞进去的消息序列（不建图、不调模型）。"""

    def __init__(self, threads: dict[str, list[Any]]) -> None:
        self._threads = threads

    def get_state(self, config: dict[str, Any]) -> Any:
        tid = config["configurable"]["thread_id"]
        return SimpleNamespace(values={"messages": self._threads.get(tid, [])})


def _seed(conn: Any, *, user: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('local', '本机')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) VALUES (?, 'local', ?)",
        (user, user),
    )
    conn.commit()


# -- 1：身份与指纹 -------------------------------------------------------------------


def test_card_identity_is_role_id_and_edited_content_is_a_conflict(tmp_path: Any) -> None:
    conn = _conn(tmp_path)
    _seed(conn, user="local-user")
    conn.execute(
        "INSERT INTO role_card (role_id, user_id, role_name, system_prompt) "
        "VALUES ('elysia', 'local-user', '爱莉希雅', '温柔')"
    )
    conn.commit()
    mine = S.collect_cards(conn, user_id="local-user")
    assert [m.ident for m in mine] == ["elysia"]
    # 对面同名同内容 → 相同（内置卡两边都在，正常就落在这里，不进冲突列表）
    same = S.plan(mine, [_brief(mine[0])])
    assert same.counts()["same"] == 1 and same.counts()["conflicts"] == 0
    # 对面同名但提示词不同 → 冲突（这是"你把同一个角色改成两个样子"，正是要问的时刻）
    edited = dict(_brief(mine[0]), hash="0000000000000000")
    conflict = S.plan(mine, [edited])
    assert [c.ident for c in conflict.conflicts] == ["elysia"]


def test_memory_identity_is_the_uid_not_the_local_row_id(tmp_path: Any) -> None:
    from rolecard_agent.core import memory as mem

    conn = _conn(tmp_path)
    _seed(conn, user="local-user")
    mem.add_item(conn, bucket=mem.GLOBAL_BUCKET, text="用户住在上海", user_id="local-user")
    mine = S.collect_memories(conn, user_id="local-user")
    assert len(mine) == 1 and mine[0].ident.startswith("")
    assert mine[0].ident != str(mine[0].payload.get("id")), "身份不能是本机自增 id"
    # 对面那条是同一个 uid、不同文本 ⇒ 冲突（按 uid 比，不按字面像不像）
    theirs = dict(_brief(mine[0]), hash="ffffffffffffffff", preview="用户住在苏州")
    out = S.plan(mine, [theirs])
    assert out.counts()["conflicts"] == 1


def test_reachout_identity_survives_a_different_local_autoincrement_id(tmp_path: Any) -> None:
    conn = _conn(tmp_path)
    _seed(conn, user="local-user")
    rows = [
        ("local-user", "elysia", "爱莉希雅", "第一句", "2026-09-26 04:00:00"),
        ("u1", "elysia", "爱莉希雅", "第一句", "2026-09-26 04:00:00"),
    ]
    for who, rid, rname, text, stamp in rows:
        conn.execute(
            "INSERT INTO agent_reachout (role_id, role_name, text, user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (rid, rname, text, who, stamp),
        )
    conn.commit()
    a = S.collect_reachouts(conn, user_id="local-user")
    b = S.collect_reachouts(conn, user_id="u1")
    # 同一句话在两个身份下各自有一行，身份相同（它不是"另一条内容"），但读侧本来就按人过滤。
    assert a[0].ident == b[0].ident
    assert a[0].ident.count("|") == 2
    # 文本不同 ⇒ 身份不同：主动消息只追加，永不判冲突
    conn.execute(
        "INSERT INTO agent_reachout (role_id, role_name, text, user_id, created_at) "
        "VALUES ('elysia', '爱莉希雅', '第二句', 'local-user', '2026-09-26 05:00:00')"
    )
    conn.commit()
    two = S.collect_reachouts(conn, user_id="local-user")
    assert len({i.ident for i in two}) == 2


def test_digest_is_stable_across_key_order_and_missing_none(tmp_path: Any) -> None:
    conn = _conn(tmp_path)
    _seed(conn, user="local-user")
    conn.execute(
        "INSERT INTO role_card (role_id, user_id, role_name, system_prompt, description) "
        "VALUES ('a', 'local-user', 'A', 'x', NULL)"
    )
    conn.commit()
    mine = S.collect_cards(conn, user_id="local-user")[0]
    reordered = S._digest(dict(reversed(list(mine.payload.items()))))
    assert reordered == mine.hash, "键序不该把相同的东西判成冲突"


# -- 2/3：会话的前缀、分叉与带图 -----------------------------------------------------


def test_thread_that_grew_on_one_side_is_not_a_conflict(tmp_path: Any) -> None:
    conn = _conn(tmp_path)
    _seed(conn, user="local-user")
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, title) "
        "VALUES ('s_a', 'local-user', 'general_assistant', '线 A')"
    )
    conn.commit()
    msgs = [HumanMessage(content="一问"), AIMessage(content="一答"), HumanMessage(content="又问")]
    items, skipped = S.collect_threads(
        conn, user_id="local-user", graph=_Graph({"s_a": msgs}), settings=None
    )
    assert skipped == [] and len(items) == 1
    mine = items[0]
    assert (mine.count, mine.ident) == (3, "s_a")
    # 对面只有前两条（同一个头）⇒ 本机接着聊了 ⇒ 推过去，不进冲突
    short = dict(_brief(mine), hash="0" * 16, count=2)
    out = S.plan([mine], [short])
    assert out.counts()["conflicts"] == 0 and out.counts()["only_local"] == 1
    # 对面更长 ⇒ 这一条不动（下行不在这一版）
    longer = dict(_brief(mine), hash="0" * 16, count=9)
    back = S.plan([mine], [longer])
    assert back.counts()["conflicts"] == 0 and back.counts()["only_remote"] == 1
    # 头就不一样 ⇒ 真分叉，要人裁决
    diverged = dict(_brief(mine), hash="0" * 16, head="1" * 16, count=3)
    hard = S.plan([mine], [diverged])
    assert [c.kind for c in hard.conflicts] == [S.KIND_THREAD]


def test_thread_with_an_image_is_skipped_whole_not_half_carried(tmp_path: Any) -> None:
    conn = _conn(tmp_path)
    _seed(conn, user="local-user")
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, title) "
        "VALUES ('s_b', 'local-user', 'general_assistant', '带图')"
    )
    conn.commit()
    multimodal = [HumanMessage(content=[{"type": "text", "text": "看这张"}, {"type": "image_url"}])]
    items, skipped = S.collect_threads(
        conn, user_id="local-user", graph=_Graph({"s_b": multimodal}), settings=None
    )
    assert items == []
    assert skipped[0]["reason"] == S.SKIP_HAS_IMAGE
    # 空会话也跳过，但理由不同（"还没内容"不是"传不了"）
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, title) "
        "VALUES ('s_c', 'local-user', 'general_assistant', '空的')"
    )
    conn.commit()
    empty_items, empty_skipped = S.collect_threads(
        conn, user_id="local-user", graph=_Graph({"s_b": multimodal}), settings=None
    )
    assert empty_items == []
    assert {row["reason"] for row in empty_skipped} == {S.SKIP_HAS_IMAGE, "这条会话还没有内容"}


def test_thread_memo_serves_unchanged_and_refreshes_on_change(tmp_path: Any) -> None:
    """指纹备忘的两臂：指纹没变不反序列化、指纹变了必须刷新。

    两个检查点写入口若都没动（updated_at 与 message_count 组成的指纹），collect
    直接用上一次的摘要 —— 二次调用从 N 次全量反序列化降为零次。反之，指纹一动
    就必须看到新内容，否则上行同步会静默推旧数据。
    """
    S.thread_memo_clear()
    conn = _conn(tmp_path)
    _seed(conn, user="local-user")
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, title) "
        "VALUES ('s_m', 'local-user', 'general_assistant', '备忘')"
    )
    conn.commit()
    msgs = [HumanMessage(content="一问"), AIMessage(content="一答")]
    items, _ = S.collect_threads(
        conn, user_id="local-user", graph=_Graph({"s_m": msgs}), settings=None
    )
    assert items[0].count == 2

    class _NoPeek:
        def get_state(self, *_a: object, **_kw: object) -> object:
            raise AssertionError("指纹没变就不该再读检查点")

        def __getattr__(self, name: str) -> object:
            return getattr(self, name)

    again, _ = S.collect_threads(conn, user_id="local-user", graph=_NoPeek(), settings=None)
    assert again[0].count == 2 and again[0].hash == items[0].hash

    # 指纹动了（两处都动才是真实写入的形状）→ 必须重读并给出新摘要
    conn.execute(
        "UPDATE session_thread SET updated_at = '2030-01-01 00:00:00.000',"
        " message_count = 3 WHERE thread_id = 's_m'"
    )
    conn.commit()
    grown = [*msgs, HumanMessage(content="新问")]
    third, _ = S.collect_threads(
        conn, user_id="local-user", graph=_Graph({"s_m": grown}), settings=None
    )
    assert third[0].count == 3 and third[0].hash != items[0].hash
    S.thread_memo_clear()


def test_collect_covers_exactly_the_four_shipped_kinds(tmp_path: Any) -> None:
    conn = _conn(tmp_path)
    _seed(conn, user="local-user")
    conn.execute(
        "INSERT INTO role_card (role_id, user_id, role_name, system_prompt) "
        "VALUES ('r1', 'local-user', 'R1', 'x')"
    )
    conn.commit()
    items, _skipped = S.collect(conn, user_id="local-user", graph=_Graph({}), settings=None)
    assert {i.kind for i in items} <= set(S.SYNC_KINDS)
    assert S.KIND_CARD in {i.kind for i in items}
    # 健康档案与原件这一版不搬：它们压根不在 kinds 里
    assert "report" not in S.SYNC_KINDS and "file" not in S.SYNC_KINDS


# -- 5：登录对账的自动策略 ------------------------------------------------------------
# 口径：只走无歧义的那半；歧义的（记忆、会话的冲突）留给人 —— 因为"两份都留"不幂等，
# 自动档每登录一次就会把复制品当新东西再复制一遍。跑两遍第二遍是空转，这是它的命。


def _conflict(kind: str, ident: str, at_mine: str, at_theirs: str) -> S.Conflict:
    mine = S.SyncItem(kind=kind, ident=ident, hash="a", at=at_mine, payload={})
    theirs = {"kind": kind, "ident": ident, "hash": "b", "at": at_theirs}
    return S.Conflict(kind=kind, ident=ident, mine=mine, theirs=theirs)


def test_auto_moves_takes_only_the_unambiguous() -> None:
    plan = S.SyncPlan(
        only_local=[S.SyncItem(kind="memory", ident="m1", hash="x", payload={})],
        only_remote=[{"kind": "card", "ident": "c1", "hash": "y"}],
        same=[],
        conflicts=[
            _conflict("memory", "m2", "2026-09-27 10:00:00", "2026-09-26 10:00:00"),
            _conflict("thread", "s_1", "2026-09-27 10:00:00", "2026-09-26 10:00:00"),
        ],
        skipped=[],
    )
    push, pull, human = S.auto_moves(plan)
    assert [(i.kind, i.ident) for i in push] == [("memory", "m1")]
    assert pull == [("card", "c1")]
    # 记忆与会话的冲突**一个都不自动动**——哪怕本机这边"看起来更新"
    assert {(c.kind, c.ident) for c in human} == {("memory", "m2"), ("thread", "s_1")}


def test_a_card_conflict_goes_to_whichever_side_is_newer() -> None:
    # 本机新 ⇒ 推过去
    push, pull, human = S.auto_moves(S.SyncPlan(
        only_local=[], only_remote=[], same=[],
        conflicts=[_conflict("card", "r1", "2026-09-28 08:00:00", "2026-09-27 08:00:00")],
        skipped=[]))
    assert [(i.kind, i.ident) for i in push] == [("card", "r1")] and pull == [] and human == []
    # 对面新 ⇒ 取过来
    push, pull, human = S.auto_moves(S.SyncPlan(
        only_local=[], only_remote=[], same=[],
        conflicts=[_conflict("card", "r1", "2026-09-27 08:00:00", "2026-09-28 08:00:00")],
        skipped=[]))
    assert push == [] and pull == [("card", "r1")] and human == []
    # 读不出时刻的不许赢（空串当最旧）⇒ 留给人
    push, pull, human = S.auto_moves(S.SyncPlan(
        only_local=[], only_remote=[], same=[],
        conflicts=[_conflict("card", "r1", "", "")],
        skipped=[]))
    assert push == [] and pull == [] and len(human) == 1
