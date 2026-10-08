"""提取精华 / 整理记忆（core/memory/memory_distill.py）的用例：全部离线，假模型给固定文本。

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

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.core import memory as mem
from rolecard_agent.core.memory import memory_distill as distill
from rolecard_agent.core.storage.migrations import MIGRATION_PLAN
from rolecard_agent.storage.db import SqlConnection, bootstrap, connect


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


ME = DEFAULT_USER_ID  # 这台实例的主人在测试里的名字（M2b 之后每次读写都要说清为谁）

@pytest.fixture
def conn() -> Any:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=(), plan=MIGRATION_PLAN)
    return c


def _texts(c: SqlConnection, bucket: str) -> list[str]:
    return [str(i["text"]) for i in mem.ranked_active(c, user_id=ME, bucket=bucket)]


def _thread_cols(c: SqlConnection) -> set[str]:
    return {str(r["name"]) for r in c.execute("PRAGMA table_info(session_thread)").fetchall()}


# ---------------------------------------------------------------- 提取


def test_extract_adds_facts_as_items(conn: SqlConnection) -> None:
    model = FakeModel("ADD 用户住在上海\nADD 用户每周五要交周报")
    out = distill.extract(
        conn, user_id=ME,
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
    assert [i["source"] for i in mem.list_items(conn, user_id=ME,
        bucket="elysia")] == ["extract", "extract"]
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
        conn, user_id=ME,
        model=FakeModel("ADD 用户住在上海"),
        bucket="elysia",
        messages=[_Msg("human", "我搬到上海了")],
        backend="local-8b",
    )
    row = conn.execute("SELECT backend, calls, completion_tokens FROM token_usage_day").fetchone()
    assert (row["backend"], row["calls"], row["completion_tokens"]) == ("local-8b", 1, 123)


def test_extract_update_supersedes_instead_of_rewriting(conn: SqlConnection) -> None:
    """UPDATE 不是就地改文本：新增一条 + 把旧那条标失效（版本链是"它何时开始搞错"的证据）。"""
    old = mem.add_item(conn, user_id=ME, bucket="elysia", text="用户住在上海", source="manual")
    assert old is not None
    out = distill.extract(
        conn, user_id=ME,
        model=FakeModel(f"UPDATE {old['id']} 用户去年搬到北京了"),
        bucket="elysia",
        messages=[_Msg("human", "我现在在北京")],
    )
    assert out["report"]["updated"] == 1
    assert _texts(conn, "elysia") == ["用户去年搬到北京了"]
    stale = mem.get_item(conn, int(str(old["id"])), user_id=ME)
    assert stale is not None and stale["invalidated_at"] is not None
    fresh = mem.get_item(conn, int(str(stale["superseded_by"])), user_id=ME)
    assert fresh is not None and fresh["text"] == "用户去年搬到北京了"
    # 旧行**还在表里**（可回滚），只是不再注入
    assert len(mem.list_items(conn, user_id=ME, bucket="elysia", include_invalidated=True)) == 2


def test_extract_update_that_repeats_the_same_fact_loses_nothing(conn: SqlConnection) -> None:
    """模型说"这条过时了"却给了同一件事的另一种说法 ⇒ 不能把那条自己标为失效。

    这条路径不是想象出来的：`ADD`/`UPDATE` 给的文本与库里某条**逐字相同**时，`add_item`
    会返回那一条本身；照原流程往下 `invalidate_item(old, superseded_by=old)` 就会让
    幸存者自己取代自己 ⇒ 事实从注入里消失，而界面报的还是"更新 1 条"。
    """
    old = mem.add_item(conn, user_id=ME, bucket="elysia", text="用户住在上海", source="manual")
    assert old is not None
    out = distill.extract(
        conn, user_id=ME,
        model=FakeModel(f"UPDATE {old['id']} 用户住在上海"),
        bucket="elysia",
        messages=[_Msg("human", "我还是住在上海")],
    )
    assert out["report"]["updated"] == 0
    assert _texts(conn, "elysia") == ["用户住在上海"]  # 那条还在，也没被自己取代
    assert mem.get_item(conn, int(str(old["id"])), user_id=ME)["invalidated_at"] is None


def test_consolidate_keeps_the_survivor_of_a_merge(conn: SqlConnection) -> None:
    """MERGE 的结果如果就是被合并的某一条（文本逐字相同），那条是幸存者，不能把自己弄失效。"""
    a = mem.add_item(conn, user_id=ME, bucket="elysia", text="用户养了一只猫叫米")
    b = mem.add_item(conn, user_id=ME, bucket="elysia", text="用户的猫叫米")
    assert a is not None and b is not None
    out = distill.consolidate(
        conn, user_id=ME,
        model=FakeModel(f"MERGE {a['id']},{b['id']} 用户养了一只猫叫米"),
        bucket="elysia",
    )
    assert out["report"]["merged"] == 1
    survivor = mem.get_item(conn, int(str(a["id"])), user_id=ME)
    assert survivor is not None and survivor["invalidated_at"] is None, "幸存者被自己取代了"
    assert _texts(conn, "elysia") == ["用户养了一只猫叫米"]


def test_count_similar_only_counts_and_touches_nothing(conn: SqlConnection) -> None:
    """`count_similar` 是**提示**：它报数，不新增、不失效、不改写任何一行。"""
    mem.add_item(conn, user_id=ME, bucket="elysia", text="用户喜欢断舍离，清理衣物上瘾")
    mem.add_item(conn, user_id=ME, bucket="elysia", text="用户喜欢断舍离，清理衣柜上瘾")
    mem.add_item(conn, user_id=ME, bucket="elysia", text="用户住在上海")
    before = mem.list_items(conn, user_id=ME, bucket="elysia", include_invalidated=True)
    assert distill.count_similar(conn, user_id=ME, bucket="elysia") == 2
    assert mem.list_items(conn, user_id=ME, bucket="elysia", include_invalidated=True) == before


def test_extract_ignores_unparseable_lines_and_counts_them(conn: SqlConnection) -> None:
    out = distill.extract(
        conn, user_id=ME,
        model=FakeModel("我觉得这个人挺有意思\nADD 用户养了一只猫\n- 随便什么\nNOOP"),
        bucket="elysia",
        messages=[_Msg("human", "我家猫又踩键盘了")],
    )
    assert out["report"]["added"] == 1
    assert out["report"]["skipped"] >= 1
    assert _texts(conn, "elysia") == ["用户养了一只猫"]


def test_extract_noop_changes_nothing(conn: SqlConnection) -> None:
    out = distill.extract(
        conn, user_id=ME, model=FakeModel("NOOP"), bucket="elysia", messages=[_Msg("human",
            "今天天气不错")]
    )
    assert out["report"]["noop"] == 1 and out["report"]["added"] == 0
    assert _texts(conn, "elysia") == []


def test_extract_model_failure_writes_nothing(conn: SqlConnection) -> None:
    mem.add_item(conn, user_id=ME, bucket="elysia", text="原来就有的一条", source="manual")
    out = distill.extract(
        conn, user_id=ME,
        model=FakeModel(error=RuntimeError("boom")),
        bucket="elysia",
        messages=[_Msg("human", "随便说点什么")],
    )
    assert out["ok"] is False and "模型调用失败" in out["report"]["detail"]
    assert _texts(conn, "elysia") == ["原来就有的一条"]


def test_extract_without_text_is_not_a_model_call(conn: SqlConnection) -> None:
    """纯图片/空消息的会话不值得发一次调用（成本要花在有的可提的内容上）。"""
    model = FakeModel("ADD 不该被用到")
    out = distill.extract(conn, user_id=ME, model=model, bucket="elysia", messages=[_Msg("human",
        "")])
    assert out["ok"] is True and model.prompts == []


# ---------------------------------------------------------------- 整理


def test_consolidate_merges_synonyms_and_keeps_rows(conn: SqlConnection) -> None:
    a = mem.add_item(conn, user_id=ME, bucket="elysia", text="用户养了一只猫")
    b = mem.add_item(conn, user_id=ME, bucket="elysia", text="用户的猫叫米")
    assert a is not None and b is not None
    out = distill.consolidate(
        conn,
        user_id=ME,
        model=FakeModel(f"MERGE {a['id']},{b['id']} 用户养了一只叫米的猫"),
        bucket="elysia",
    )
    assert out["report"]["merged"] == 1
    assert _texts(conn, "elysia") == ["用户养了一只叫米的猫"]
    # 被合并的两条只是失效，指向合并出来的那条
    for item in (a, b):
        stale = mem.get_item(conn, int(str(item["id"])), user_id=ME)
        assert stale is not None and stale["invalidated_at"] is not None
        assert stale["superseded_by"] is not None
    assert out["report"]["before"] == 2 and out["report"]["after"] == 1


def test_extract_reads_the_importance_marker(conn: SqlConnection) -> None:
    """`ADD [2] …` 那个档号进库变成显著性；漏标与越界都落在常规档，**不是丢掉那条**。

    钳制与默认都发生在 `memory.clamp_importance` 那一处（写它的来源有三条）。
    """
    model = FakeModel(
        "ADD [2] 用户青霉素过敏\nADD 用户住在苏州\nADD [9] 越界的档号\nADD [很要紧] 不是数字"
    )
    out = distill.extract(
        conn, user_id=ME,
        model=model,
        bucket="medical_archivist",
        messages=[_Msg("human", "我青霉素过敏，住在苏州")],
    )
    assert out["report"]["added"] == 4
    items = mem.list_items(conn, user_id=ME, bucket="medical_archivist")
    tiers = {str(i["text"]): int(i["importance"]) for i in items}
    assert tiers == {
        "用户青霉素过敏": 2,
        "用户住在苏州": 1,
        "越界的档号": 2,
        "[很要紧] 不是数字": 1,
    }


def _thread(conn: SqlConnection, thread_id: str) -> None:
    """插一条真的会话行 —— 游标是 `session_thread` 上的列，没有行就没地方写。"""
    conn.executescript(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');"
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "  VALUES ('u1', 't1', 'demo');"
    )
    conn.execute(
        "INSERT OR IGNORE INTO session_thread (thread_id, user_id, current_role_id) "
        "VALUES (?, 'u1', 'elysia')",
        (thread_id,),
    )
    conn.commit()


def test_pending_messages_only_feeds_what_the_cursor_has_not_seen(conn: SqlConnection) -> None:
    """游标决定**喂什么**，不只是"要不要跑"：跑过一次之后，旧消息不该再进第二次的料。

    这条是 2026-09-24 那组读数逼出来的：固定八轮对话里自动提取跑了 3 次，每次喂的都是整段，
    于是弱模型把同一批事实换个说法重抽一遍 —— 桶里 **35 条 / 只有 8 个不同事实**。
    注入窗口一共 8 条，重复条目干的事就是**把不同的事实挤出去**。
    """
    msgs = [_Msg("human", f"第 {i} 句") for i in range(1, 9)]
    _thread(conn, "t1")
    assert distill.pending_messages(conn, thread_id="t1", messages=msgs) == msgs  # 游标 0 = 全量

    distill.mark_extracted(conn, thread_id="t1", message_count=6)
    left = distill.pending_messages(conn, thread_id="t1", messages=msgs)
    assert [str(m.content) for m in left] == ["第 7 句", "第 8 句"]

    distill.mark_extracted(conn, thread_id="t1", message_count=8)
    assert distill.pending_messages(conn, thread_id="t1", messages=msgs) == []


def test_pending_messages_falls_back_to_the_whole_history_when_the_cursor_lies(
    conn: SqlConnection
) -> None:
    """编辑/删除消息会让列表变短、游标落在长度之外：那种情况**退回整段**。

    宁可重抽一遍（顶多多几条，还能「整理记忆」收），也不能"看起来提取过、其实新消息一条没看"
    —— 后者是静默丢事实，代价不对等。
    """
    msgs = [_Msg("human", "只剩这一句")]
    _thread(conn, "t2")
    distill.mark_extracted(conn, thread_id="t2", message_count=9)
    assert distill.pending_messages(conn, thread_id="t2", messages=msgs) == msgs


def test_echo_adds_are_dropped_without_losing_information(conn: SqlConnection) -> None:
    """模型把【已有条目】抄回来当新事实时丢掉，但**只丢"字面已经在里面"的那种**。

    实测的形状（2026-09-24，本地 8B、固定八轮对话）：自动提取跑三轮，`added` 11 → 9 → 8，
    桶里 26 条而 `similar` 也涨到 26 —— 后两轮新增的基本是把清单换个长度重抄一遍。
    """
    mem.add_item(conn, user_id=ME, bucket="elysia", text="用户今天加班到十点才走", source="extract")
    out = distill.extract(
        conn, user_id=ME,
        model=FakeModel("ADD 今天加班到十点才走\nADD 用户中午和同事吵架了"),
        bucket="elysia",
        messages=[_Msg("human", "中午和同事吵架了")],
    )
    assert out["report"]["added"] == 1 and out["report"]["dup_echo"] == 1
    assert set(_texts(conn, "elysia")) == {
        "用户今天加班到十点才走",
        "用户中午和同事吵架了",
    }


def test_a_more_specific_fact_is_never_treated_as_an_echo(conn: SqlConnection) -> None:
    """反方向不能一起砍：「有结石」已在库里，而新说的是「结石直径 6 mm」—— 那是**更多信息**。

    抹掉它是吞真事实（§12.9 那次误并的同一种错），这种收敛留给「整理记忆」。
    """
    mem.add_item(
        conn, user_id=ME, bucket="medical_archivist", text="用户有肾结石", source="extract"
    )
    out = distill.extract(
        conn, user_id=ME,
        model=FakeModel("ADD 用户有肾结石，2026-03-12 复查直径 6 mm"),
        bucket="medical_archivist",
        messages=[_Msg("human", "复查说结石 6 毫米了")],
    )
    assert out["report"]["added"] == 1 and out["report"]["dup_echo"] == 0
    assert any("6 mm" in t for t in _texts(conn, "medical_archivist"))


def test_is_echo_needs_a_real_length_before_it_can_suppress(conn: SqlConnection) -> None:
    """短到几个字的句子到处是子串 —— 不够长就不许当回声丢掉。"""
    known = ["用户喜欢养猫，家里两只"]
    assert distill.is_echo("用户喜欢养猫", known)  # 够长（≥6 字）且确实在里面
    assert not distill.is_echo("喜欢养猫", known)  # 4 个字：短于下限，宁可留一条重复
    assert not distill.is_echo("用户住在苏州", known)
    # 条目里多余的空格不影响判断（写入侧本来就把空白折成一个空格）
    assert distill.is_echo("加班到十点才走", ["今天  加班到十点才走"])


def test_extract_prompt_forbids_background_not_said_in_the_dialogue(conn: SqlConnection) -> None:
    """提取指令里必须**点名**"不许补进对话里没有的背景"—— 2026-09-24 实测抓到的捏造形状。

    本地 8B 从一段固定八轮对话提出的条目里有「用户住在城市」「工作强度较高」
    （原话只有"今天加班到十点才走"）。这类东西一旦入库就会被当成用户的事实永久注入回去，
    而这条路径没有别的兜底 —— 「整理记忆」判的是"谁顶替谁"，看不出"这句没说过"。

    **这条约束第一版写过头了**（同一批实测）：把"写成用户说了什么"写进指令之后，本地改成
    每提一次就抄一条（同义对 0 → 2），云端产量从 15 掉到 5（八轮里约 8 个真事实）。
    所以钉的是**两头**：不许补没说的，也不许把明说的当琐碎漏掉。
    """
    joined = _extract_prompt_of(conn)
    assert "事实只从对方说过的话里取" in joined
    assert "没出现过的地点、职业、程度、原因、结论一律不补" in joined
    assert "同一件事在对话里被提到几次，只写一条" in joined
    assert "他明说的事就要记下来" in joined
    assert "不要加\"用户说\"" in joined
    # 第一版那两句"教它抄原话"的写法不能悄悄回来。
    assert "写成\"用户说了什么\"" not in joined
    assert "拿不准就少写一条" not in joined


def _extract_prompt_of(conn: SqlConnection) -> str:
    """跑一次提取，把发给模型的那段指令原样拿回来（只钉文本，不测模型行为）。"""
    model = FakeModel("NOOP")
    distill.extract(conn, user_id=ME, model=model, bucket="elysia", messages=[_Msg("human",
        "随便一句")])
    return model.prompts[0]


def test_consolidated_merge_inherits_the_highest_importance(conn: SqlConnection) -> None:
    """合并两条同义事实时，结果取源条目里**最高**的档：合并是加法性的整理，不是降级动作。

    顺手钉住另一件事：整理写出来的那条 `source` 必须是 `extract`（它是模型写的）——
    以前这一路漏传了参数，面板上"谁写的"那一列对整理出来的条目一直在撒谎。
    """
    a = mem.add_item(
        conn, user_id=ME, bucket="elysia", text="用户青霉素过敏", importance=2, source="extract"
    )
    b = mem.add_item(
        conn, user_id=ME, bucket="elysia", text="青霉素吃了会起疹子", importance=1, source="extract"
    )
    assert a is not None and b is not None
    out = distill.consolidate(
        conn, user_id=ME,
        model=FakeModel(f"MERGE {a['id']},{b['id']} 用户青霉素过敏会起疹子"),
        bucket="elysia",
    )
    assert out["report"]["merged"] == 1
    survivor = mem.list_items(conn, user_id=ME, bucket="elysia")[0]
    assert int(survivor["importance"]) == 2 and str(survivor["source"]) == "extract"


def test_consolidate_never_touches_pinned_items(conn: SqlConnection) -> None:
    """钉住 = 不参与淘汰，也不被模型改写（用户对某条事实特意钉过）。"""
    pinned = mem.add_item(conn, user_id=ME, bucket="elysia", text="用户的全名是张三")
    assert pinned is not None
    mem.set_pinned(conn, user_id=ME, item_id=int(str(pinned["id"])), pinned=True)
    other = mem.add_item(conn, user_id=ME, bucket="elysia", text="用户喜欢咖啡")
    assert other is not None
    lines = f"INVALID {pinned['id']} 不该动它\nMERGE {pinned['id']},{other['id']} 混在一起"
    out = distill.consolidate(
        conn, user_id=ME,
        model=FakeModel(lines),
        bucket="elysia",
    )
    assert out["report"]["invalidated"] == 0 and out["report"]["merged"] == 0
    assert out["report"]["skipped"] == 2
    kept = mem.get_item(conn, int(str(pinned["id"])), user_id=ME)
    assert kept is not None and kept["pinned"] is True and kept["invalidated_at"] is None


def test_consolidate_prompt_marks_pinned_rows(conn: SqlConnection) -> None:
    pinned = mem.add_item(conn, user_id=ME, bucket="elysia", text="钉住的那条")
    assert pinned is not None
    mem.set_pinned(conn, user_id=ME, item_id=int(str(pinned["id"])), pinned=True)
    mem.add_item(conn, user_id=ME, bucket="elysia", text="普通的一条")
    model = FakeModel("NOOP")
    distill.consolidate(conn, user_id=ME, model=model, bucket="elysia")
    prompt = model.prompts[0]
    assert f"{pinned['id']}*" in prompt
    assert "不要对它输出任何指令" in prompt


def test_consolidate_needs_at_least_two_items(conn: SqlConnection) -> None:
    """一条以下不发调用：没有可整理的东西，为什么要花一次模型调用。"""
    mem.add_item(conn, user_id=ME, bucket="elysia", text="只有一条")
    model = FakeModel("NOOP")
    out = distill.consolidate(conn, user_id=ME, model=model, bucket="elysia")
    assert out["ok"] is True and model.prompts == []


def test_consolidate_invalid_creates_the_replacement(conn: SqlConnection) -> None:
    stale = mem.add_item(conn, user_id=ME, bucket="elysia", text="用户在读研")
    assert stale is not None
    mem.add_item(conn, user_id=ME, bucket="elysia", text="用户已经工作了")
    out = distill.consolidate(
        conn, user_id=ME, model=FakeModel(f"INVALID {stale['id']} 用户已毕业工作"), bucket="elysia"
    )
    assert out["report"]["invalidated"] == 1
    assert "用户已毕业工作" in _texts(conn, "elysia")
    gone = mem.get_item(conn, int(str(stale["id"])), user_id=ME)
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

    def convo(humans: int, ai_per_turn: int = 1) -> list[_Msg]:
        msgs: list[_Msg] = []
        for i in range(humans):
            msgs.append(_Msg("human", f"问 {i}"))
            msgs.extend([_Msg("ai", f"答 {i}")] * ai_per_turn)
        return msgs

    # 每 12 轮：11 个用户轮不提，12 个才提（判据是"用户轮次"，不是墙钟也不是消息条数）
    assert distill.due_for_extract(conn, thread_id="t1", every=12, messages=convo(11)) is False
    assert distill.due_for_extract(conn, thread_id="t1", every=12, messages=convo(12)) is True
    distill.mark_extracted(conn, thread_id="t1", message_count=len(convo(12)))
    # 提取完之后重新计时，不会每轮都再提一次
    assert distill.due_for_extract(conn, thread_id="t1", every=12, messages=convo(18)) is False
    assert distill.due_for_extract(conn, thread_id="t1", every=12, messages=convo(24)) is True


def test_due_for_extract_is_not_inflated_by_tool_messages(conn: SqlConnection) -> None:
    """智能体模式一轮能产生 AI + 工具 + 汇总好几条消息 —— 那些不该把"每 12 轮"变成"每 5 轮"。

    实现以前数的是**消息条数除以 2**，而 docstring 承诺的是"用户轮次"。副本实测（固定八轮、
    节奏 5）因此跑了 3 次自动提取 + 1 次手动，一共 4 次真模型调用，每次都把已有清单重抄一遍。
    """
    _thread(conn)
    # 4 个用户轮，每轮带 3 条 AI/工具消息 = 16 条消息；按旧算法早该触发 every=5
    inflated: list[_Msg] = []
    for i in range(4):
        inflated.append(_Msg("human", f"问 {i}"))
        inflated.extend([_Msg("ai", "中段")] * 3)
    assert len(inflated) == 16
    assert distill.due_for_extract(conn, thread_id="t1", every=5, messages=inflated) is False
    assert distill.due_for_extract(conn, thread_id="t1", every=4, messages=inflated) is True


def test_every_zero_disables_the_auto_path(conn: SqlConnection) -> None:
    _thread(conn)
    msgs = [_Msg("human", "说很多")] * 999
    assert distill.due_for_extract(conn, thread_id="t1", every=0, messages=msgs) is False


def test_pre_turn_gate_agrees_with_the_post_turn_verdict(conn: SqlConnection) -> None:
    """轮前同义判断 ≡ 轮后判据：pending_humans(轮前) + 本轮那条 ≥ every ⇔ 轮后 due。

    chat 入口只有轮前的快照在手（省掉后台那次全量 get_state 的整个前提），所以
    "轮前数一遍 + 1"必须与"轮后真读一遍"给出同一个答案 —— 追加式流量下两条路
    数的是同一批消息。两边都贴着 every−1 / every 的边界各钉一格。
    """
    _thread(conn)

    def convo(humans: int) -> list[_Msg]:
        msgs: list[_Msg] = []
        for i in range(humans):
            msgs.append(_Msg("human", f"问 {i}"))
            msgs.append(_Msg("ai", f"答 {i}"))
        return msgs

    pre = convo(11)  # 轮前 11 个用户轮，全部在游标之后
    assert distill.pending_humans(conn, thread_id="t1", messages=pre) + 1 >= 12
    assert distill.due_for_extract(
        conn, thread_id="t1", every=12, messages=[*pre, _Msg("human", "第 12 问")]
    )

    pre = convo(10)  # 差一轮：轮前判据说"不提取"，轮后判据必须同样说"不"
    assert distill.pending_humans(conn, thread_id="t1", messages=pre) + 1 < 12
    assert not distill.due_for_extract(
        conn, thread_id="t1", every=12, messages=[*pre, _Msg("human", "第 11 问")]
    )


def test_cursor_migration_adds_the_column(conn: SqlConnection) -> None:
    """旧库（没有 distilled_at_seq）补列之后，游标判断照跑不误。

    在**已建好的库**上把列删掉来冒充旧库，而不是手搓一张只有 session_thread 的表：
    升级要读 role_card / service_endpoint / model_backend 的列，缺表直接炸。
    走 `bootstrap` 而不是直接调 `_migrate`（09-26 轮 R26-04 之后列级补齐归
    `reconcile_columns`，而它挂在 `bootstrap` 上）—— 这条要验的是"真启动路径能不能升上来"。
    """
    _thread(conn, "t9")
    conn.execute("ALTER TABLE session_thread DROP COLUMN distilled_at_seq")
    conn.commit()
    assert "distilled_at_seq" not in _thread_cols(conn)
    bootstrap(conn, enabled_domains=("health",), plan=MIGRATION_PLAN)
    assert "distilled_at_seq" in _thread_cols(conn)
    # 补列之后游标是 NULL ⇒ 全部消息都算"没提取过"，节奏判断照跑
    assert distill.due_for_extract(
        conn, thread_id="t9", every=1, messages=[_Msg("human", "一句话")]
    ) is True
    assert distill.pending_messages(
        conn, thread_id="t9", messages=[_Msg("human", "一句话")]
    ) != []


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


# ---------------------------------------------------------------- 提取的资源闸与 top-K


class _RecordingTracer:
    """只记录事件的假 tracer（after_turn 的推迟/跳过都靠事件点名）。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


def _run_after_turn(
    conn: SqlConnection, tracer: Any, model: Any, backend: str, *, local: bool
) -> None:
    """跑一遍 after_turn 状态机；包一层 close 为空的连接 ——
    生产里"用完即还"还的是池线程自己的连接槽，测试夹具的连接归夹具管。"""

    class _NoCloseConn:
        def execute(self, *a: Any, **k: Any) -> Any:
            return conn.execute(*a, **k)

        def commit(self) -> None:
            conn.commit()

        def rollback(self) -> None:
            conn.rollback()

        def close(self) -> None:
            pass  # 夹具连接不能真被关掉

    distill.after_turn(
        _NoCloseConn(),  # type: ignore[arg-type]
        thread_id="t1",
        role_id="general_assistant",
        extract_turns=1,
        tracer=tracer,
        load_context=lambda: ({"user_id": ME}, [_Msg("human", "问"), _Msg("ai", "答")]),
        resolve_model=lambda _thread: (model, backend),
        is_local_backend=(lambda _backend: local),
    )


def test_resource_gate_defers_extraction_while_any_turn_is_busy(conn: SqlConnection) -> None:
    """资源闸：本机后端 + 任何对话轮在飞 ⇒ 这一轮提取推迟。

    推迟的形状：不调模型、游标不动（下一轮兜底照常再问 —— 所以只断言"这轮没发生"）、
    留痕点名"GPU 忙"。对照臂：没有轮在飞时同一配置照常提取。
    """
    from rolecard_agent.core.common import thread_locks as locks

    _thread(conn, "t1")
    tracer = _RecordingTracer()

    def _busy() -> None:
        with locks.thread_write("别的窗口的会话"):
            _run_after_turn(conn, tracer, FakeModel(), "本地 8B", local=True)

    _busy()
    assert not _extract_invoked(tracer)
    assert any("GPU" in str(e.detail.get("skipped", "")) for e in tracer.events)
    cursor = conn.execute(
        "SELECT distilled_at_seq FROM session_thread WHERE thread_id = 't1'"
    ).fetchone()["distilled_at_seq"]
    assert cursor is None, "推迟不能动游标 —— 动了就真的丢了这一轮"

    # 对照臂：GPU 空闲 → 同一配置照常提取
    model = FakeModel("NOOP")
    _run_after_turn(conn, tracer, model, "本地 8B", local=True)
    assert model.prompts, "空闲时闸不该拦"


def test_resource_gate_never_gates_a_cloud_backend(conn: SqlConnection) -> None:
    """云端提取不占本机显存：轮次在飞也照常提取（闸只管本地后端）。"""
    from rolecard_agent.core.common import thread_locks as locks

    _thread(conn, "t1")
    tracer = _RecordingTracer()
    model = FakeModel("NOOP")
    with locks.thread_write("别的窗口的会话"):
        _run_after_turn(conn, tracer, model, "siliconflow", local=False)
    assert model.prompts, "云端后端不该被本地资源闸拦"


def _extract_invoked(tracer: _RecordingTracer) -> bool:
    """有没有真的走到模型调用：skip 事件全部点名了理由，没有 skip = 走到了。"""
    return not any(
        "skipped" in getattr(e, "detail", {}) for e in tracer.events
    )


def test_extract_prompt_carries_top_k_not_the_whole_bucket(conn: SqlConnection) -> None:
    """【已有条目】有固定上限：60 条活跃条目只带 ≤K 个候选，且相关的那条必须在场。

    上限就是"prompt 长度有界"的那道闸；相关性用与回声抑制同一口径的 4-gram Dice。
    回声判重仍看全量 —— 这里顺带钉住"喂得少"不改变"认得准"。
    """
    _thread(conn, "t1")
    for i in range(60):
        mem.add_item(
            conn,
            user_id=ME,
            bucket="general_assistant",
            text=f"无关记录{chr(0x4E00 + i)}号：关于杂项{chr(0x4E00 + i)}的备注",
            source="seed",
        )
    relevant = mem.add_item(
        conn,
        user_id=ME,
        bucket="general_assistant",
        text="用户的生日在十月",
        source="seed",
    )
    assert relevant is not None
    model = FakeModel("NOOP")
    out = distill.extract(
        conn,
        user_id=ME,
        model=model,
        bucket="general_assistant",
        messages=[_Msg("human", "我们聊聊生日在十月这件事吧")],
    )
    assert out["ok"]
    assert out["report"]["existing_items"] <= distill._EXTRACT_TOP_K
    assert "生日在十月" in model.prompts[0], "最相关的候选不该被截掉"
