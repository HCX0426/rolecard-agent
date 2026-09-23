"""提取精华 / 整理记忆（core/memory_distill.py）的用例：全部离线，假模型给固定文本。

四条必须钉住的性质（都是"改用户记忆"这类功能最容易做错的地方）：

  1. **只写标记不物理删**：整理与 UPDATE 都走 `invalidate_item(superseded_by=…)`，
     行还在库里 —— 整理坏了最坏是回滚一个标记，而不是丢记忆；
  2. **看不懂的行忽略并计数**，绝不猜它想说什么（猜出来的事实会永久留在用户记忆里）；
  3. **钉住的条目碰不得**：模型对 pinned 输出任何指令都整条作废；
  4. **模型调用失败 = 什么都没发生**，且错误可读（后台那条只留 trace，手动那条回 502）。
"""

from __future__ import annotations

from typing import Any

import pytest

from rolecard_agent.core import memory as mem
from rolecard_agent.core import memory_distill as distill
from rolecard_agent.storage.db import SqlConnection, _migrate, bootstrap, connect


class _Reply:
    """最小假模型响应：`text_of()` 读 `.content`，成本读 `.response_metadata`。"""

    def __init__(self, text: str, *, tokens: int | None = None) -> None:
        self.content = text
        self.response_metadata = {"token_usage": {"total_tokens": tokens}} if tokens else {}


class FakeModel:
    def __init__(self, text: str = "", *, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> _Reply:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return _Reply(self.text, tokens=123)


@pytest.fixture
def conn() -> Any:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=())
    return c


def _texts(c: SqlConnection, bucket: str) -> list[str]:
    return [str(i["text"]) for i in mem.ranked_active(c, bucket=bucket)]


def _thread_cols(c: SqlConnection) -> set[str]:
    return {str(r["name"]) for r in c.execute("PRAGMA table_info(session_thread)").fetchall()}


# ---------------------------------------------------------------- 提取


def test_extract_adds_facts_as_items(conn: SqlConnection) -> None:
    model = FakeModel("ADD 用户住在上海\nADD 用户每周五要交周报")
    out = distill.extract(
        conn,
        model=model,
        bucket="elysia",
        messages=[
            _Msg("human", "我搬到上海了"),
            _Msg("ai", "欢迎来到上海！"),
            _Msg("human", "对了每周五要交周报"),
        ],
    )
    assert out["ok"] is True
    assert out["report"]["added"] == 2
    assert set(_texts(conn, "elysia")) == {"用户住在上海", "用户每周五要交周报"}
    assert [i["source"] for i in mem.list_items(conn, bucket="elysia")] == ["extract", "extract"]
    # 成本可见化：模型报了 usage 就带进报告（没报就是 None，不编）
    assert out["report"]["tokens"] == 123
    # 已有条目会进 prompt —— 不然同一句事实每次提取都再 ADD 一遍
    assert "【已有条目】" in model.prompts[0]


def test_extract_books_the_call_on_the_backend_that_served_it(conn: SqlConnection) -> None:
    """提取也花真钱，所以要进同一本 token 账（审计 §12.8）。

    本地实测一次提取约 122 秒 / 248 token —— 自动提取降频到 5 轮之后，这笔开销是按轮次涨的，
    看不见它就等于"悄悄把成本加上去"。
    """
    distill.extract(
        conn,
        model=FakeModel("ADD 用户住在上海"),
        bucket="elysia",
        messages=[_Msg("human", "我搬到上海了")],
        backend="local-8b",
    )
    row = conn.execute("SELECT backend, calls, completion_tokens FROM token_usage_day").fetchone()
    assert (row["backend"], row["calls"], row["completion_tokens"]) == ("local-8b", 1, 123)


def test_extract_update_supersedes_instead_of_rewriting(conn: SqlConnection) -> None:
    """UPDATE 不是就地改文本：新增一条 + 把旧那条标失效（版本链是"它何时开始搞错"的证据）。"""
    old = mem.add_item(conn, bucket="elysia", text="用户住在上海", source="manual")
    assert old is not None
    out = distill.extract(
        conn,
        model=FakeModel(f"UPDATE {old['id']} 用户去年搬到北京了"),
        bucket="elysia",
        messages=[_Msg("human", "我现在在北京")],
    )
    assert out["report"]["updated"] == 1
    assert _texts(conn, "elysia") == ["用户去年搬到北京了"]
    stale = mem.get_item(conn, int(str(old["id"])))
    assert stale is not None and stale["invalidated_at"] is not None
    fresh = mem.get_item(conn, int(str(stale["superseded_by"])))
    assert fresh is not None and fresh["text"] == "用户去年搬到北京了"
    # 旧行**还在表里**（可回滚），只是不再注入
    assert len(mem.list_items(conn, bucket="elysia", include_invalidated=True)) == 2


def test_extract_update_that_repeats_the_same_fact_loses_nothing(conn: SqlConnection) -> None:
    """模型说"这条过时了"却给了同一件事的另一种说法 ⇒ 不能把那条自己标为失效。

    这条路径不是想象出来的：`ADD`/`UPDATE` 给的文本与库里某条**逐字相同**时，`add_item`
    会返回那一条本身；照原流程往下 `invalidate_item(old, superseded_by=old)` 就会让
    幸存者自己取代自己 ⇒ 事实从注入里消失，而界面报的还是"更新 1 条"。
    """
    old = mem.add_item(conn, bucket="elysia", text="用户住在上海", source="manual")
    assert old is not None
    out = distill.extract(
        conn,
        model=FakeModel(f"UPDATE {old['id']} 用户住在上海"),
        bucket="elysia",
        messages=[_Msg("human", "我还是住在上海")],
    )
    assert out["report"]["updated"] == 0
    assert _texts(conn, "elysia") == ["用户住在上海"]  # 那条还在，也没被自己取代
    assert mem.get_item(conn, int(str(old["id"])))["invalidated_at"] is None


def test_consolidate_keeps_the_survivor_of_a_merge(conn: SqlConnection) -> None:
    """MERGE 的结果如果就是被合并的某一条（文本逐字相同），那条是幸存者，不能把自己弄失效。"""
    a = mem.add_item(conn, bucket="elysia", text="用户养了一只猫叫米")
    b = mem.add_item(conn, bucket="elysia", text="用户的猫叫米")
    assert a is not None and b is not None
    out = distill.consolidate(
        conn,
        model=FakeModel(f"MERGE {a['id']},{b['id']} 用户养了一只猫叫米"),
        bucket="elysia",
    )
    assert out["report"]["merged"] == 1
    survivor = mem.get_item(conn, int(str(a["id"])))
    assert survivor is not None and survivor["invalidated_at"] is None, "幸存者被自己取代了"
    assert _texts(conn, "elysia") == ["用户养了一只猫叫米"]


def test_count_similar_only_counts_and_touches_nothing(conn: SqlConnection) -> None:
    """`count_similar` 是**提示**：它报数，不新增、不失效、不改写任何一行。"""
    mem.add_item(conn, bucket="elysia", text="用户喜欢断舍离，清理衣物上瘾")
    mem.add_item(conn, bucket="elysia", text="用户喜欢断舍离，清理衣柜上瘾")
    mem.add_item(conn, bucket="elysia", text="用户住在上海")
    before = mem.list_items(conn, bucket="elysia", include_invalidated=True)
    assert distill.count_similar(conn, bucket="elysia") == 2
    assert mem.list_items(conn, bucket="elysia", include_invalidated=True) == before


def test_extract_ignores_unparseable_lines_and_counts_them(conn: SqlConnection) -> None:
    out = distill.extract(
        conn,
        model=FakeModel("我觉得这个人挺有意思\nADD 用户养了一只猫\n- 随便什么\nNOOP"),
        bucket="elysia",
        messages=[_Msg("human", "我家猫又踩键盘了")],
    )
    assert out["report"]["added"] == 1
    assert out["report"]["skipped"] >= 1
    assert _texts(conn, "elysia") == ["用户养了一只猫"]


def test_extract_noop_changes_nothing(conn: SqlConnection) -> None:
    out = distill.extract(
        conn, model=FakeModel("NOOP"), bucket="elysia", messages=[_Msg("human", "今天天气不错")]
    )
    assert out["report"]["noop"] == 1 and out["report"]["added"] == 0
    assert _texts(conn, "elysia") == []


def test_extract_model_failure_writes_nothing(conn: SqlConnection) -> None:
    mem.add_item(conn, bucket="elysia", text="原来就有的一条", source="manual")
    out = distill.extract(
        conn,
        model=FakeModel(error=RuntimeError("boom")),
        bucket="elysia",
        messages=[_Msg("human", "随便说点什么")],
    )
    assert out["ok"] is False and "模型调用失败" in out["report"]["detail"]
    assert _texts(conn, "elysia") == ["原来就有的一条"]


def test_extract_without_text_is_not_a_model_call(conn: SqlConnection) -> None:
    """纯图片/空消息的会话不值得发一次调用（成本要花在有的可提的内容上）。"""
    model = FakeModel("ADD 不该被用到")
    out = distill.extract(conn, model=model, bucket="elysia", messages=[_Msg("human", "")])
    assert out["ok"] is True and model.prompts == []


# ---------------------------------------------------------------- 整理


def test_consolidate_merges_synonyms_and_keeps_rows(conn: SqlConnection) -> None:
    a = mem.add_item(conn, bucket="elysia", text="用户养了一只猫")
    b = mem.add_item(conn, bucket="elysia", text="用户的猫叫米")
    assert a is not None and b is not None
    out = distill.consolidate(
        conn, model=FakeModel(f"MERGE {a['id']},{b['id']} 用户养了一只叫米的猫"), bucket="elysia"
    )
    assert out["report"]["merged"] == 1
    assert _texts(conn, "elysia") == ["用户养了一只叫米的猫"]
    # 被合并的两条只是失效，指向合并出来的那条
    for item in (a, b):
        stale = mem.get_item(conn, int(str(item["id"])))
        assert stale is not None and stale["invalidated_at"] is not None
        assert stale["superseded_by"] is not None
    assert out["report"]["before"] == 2 and out["report"]["after"] == 1


def test_consolidate_never_touches_pinned_items(conn: SqlConnection) -> None:
    """钉住 = 不参与淘汰，也不被模型改写（用户对某条事实特意钉过）。"""
    pinned = mem.add_item(conn, bucket="elysia", text="用户的全名是张三")
    assert pinned is not None
    mem.set_pinned(conn, item_id=int(str(pinned["id"])), pinned=True)
    other = mem.add_item(conn, bucket="elysia", text="用户喜欢咖啡")
    assert other is not None
    lines = f"INVALID {pinned['id']} 不该动它\nMERGE {pinned['id']},{other['id']} 混在一起"
    out = distill.consolidate(
        conn,
        model=FakeModel(lines),
        bucket="elysia",
    )
    assert out["report"]["invalidated"] == 0 and out["report"]["merged"] == 0
    assert out["report"]["skipped"] == 2
    kept = mem.get_item(conn, int(str(pinned["id"])))
    assert kept is not None and kept["pinned"] is True and kept["invalidated_at"] is None


def test_consolidate_prompt_marks_pinned_rows(conn: SqlConnection) -> None:
    pinned = mem.add_item(conn, bucket="elysia", text="钉住的那条")
    assert pinned is not None
    mem.set_pinned(conn, item_id=int(str(pinned["id"])), pinned=True)
    mem.add_item(conn, bucket="elysia", text="普通的一条")
    model = FakeModel("NOOP")
    distill.consolidate(conn, model=model, bucket="elysia")
    prompt = model.prompts[0]
    assert f"{pinned['id']}*" in prompt
    assert "不要对它输出任何指令" in prompt


def test_consolidate_needs_at_least_two_items(conn: SqlConnection) -> None:
    """一条以下不发调用：没有可整理的东西，为什么要花一次模型调用。"""
    mem.add_item(conn, bucket="elysia", text="只有一条")
    model = FakeModel("NOOP")
    out = distill.consolidate(conn, model=model, bucket="elysia")
    assert out["ok"] is True and model.prompts == []


def test_consolidate_invalid_creates_the_replacement(conn: SqlConnection) -> None:
    stale = mem.add_item(conn, bucket="elysia", text="用户在读研")
    assert stale is not None
    mem.add_item(conn, bucket="elysia", text="用户已经工作了")
    out = distill.consolidate(
        conn, model=FakeModel(f"INVALID {stale['id']} 用户已毕业工作"), bucket="elysia"
    )
    assert out["report"]["invalidated"] == 1
    assert "用户已毕业工作" in _texts(conn, "elysia")
    gone = mem.get_item(conn, int(str(stale["id"])))
    assert gone is not None and gone["invalidated_at"] is not None


# ---------------------------------------------------------------- 自动兜底的节奏


def _thread(conn: SqlConnection, thread_id: str = "t1") -> None:
    """造一个能挂游标的会话（app_user 要 tenant、session_thread 要 user，都是外键）。"""
    conn.execute("INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo')")
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "VALUES ('u1', 't1', 'demo user')"
    )
    # 会话那条故意不用 OR IGNORE：外键没满足时报错，比后面 due 恒 False 好查。
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id) "
        "VALUES (?, 'u1', 'elysia')",
        (thread_id,),
    )
    conn.commit()


def test_due_for_extract_counts_turns_from_the_cursor(conn: SqlConnection) -> None:
    _thread(conn)
    # 每 12 轮 = 24 条消息；一条都没有时游标是 NULL，所以按 0 算
    assert distill.due_for_extract(conn, thread_id="t1", every=12, message_count=23) is False
    assert distill.due_for_extract(conn, thread_id="t1", every=12, message_count=24) is True
    distill.mark_extracted(conn, thread_id="t1", message_count=24)
    # 提取完之后重新计时，不会每轮都再提一次
    assert distill.due_for_extract(conn, thread_id="t1", every=12, message_count=40) is False
    assert distill.due_for_extract(conn, thread_id="t1", every=12, message_count=48) is True


def test_every_zero_disables_the_auto_path(conn: SqlConnection) -> None:
    _thread(conn)
    assert distill.due_for_extract(conn, thread_id="t1", every=0, message_count=999) is False


def test_cursor_migration_adds_the_column(conn: SqlConnection) -> None:
    """旧库（没有 distilled_at_seq）补列后，游标判断照跑不误。

    在**已建好的库**上把列删掉来冒充旧库，而不是手搓一张只有 session_thread 的表：
    `_migrate` 要读 role_card / service_endpoint / model_backend 的列，缺表直接炸。
    """
    _thread(conn, "t9")
    conn.execute("ALTER TABLE session_thread DROP COLUMN distilled_at_seq")
    conn.commit()
    assert "distilled_at_seq" not in _thread_cols(conn)
    _migrate(conn)
    assert "distilled_at_seq" in _thread_cols(conn)
    assert distill.due_for_extract(conn, thread_id="t9", every=1, message_count=4) is True


class _Msg:
    """够用的假消息：`text_of` 只读 `.content`，`_turn_lines` 只读 `.type`。"""

    def __init__(self, kind: str, content: str) -> None:
        self.type = kind
        self.content = content


def test_turn_lines_skips_tools_and_truncates() -> None:
    block = distill._turn_lines(
        [
            _Msg("human", "我住在上海" + "很长" * 200),
            _Msg("tool", "工具返回的一堆东西"),
            _Msg("ai", "知道了"),
            _Msg("human", ""),
        ]
    )
    assert "用户：我住在上海" in block and "角色：知道了" in block
    assert "工具返回" not in block
    assert len([ln for ln in block.splitlines()]) == 2
    assert max(len(ln) for ln in block.splitlines()) < 400
