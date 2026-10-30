"""跨会话记忆（core/memory/，条目表）的单元测试。

钉的是条目化换来的那几件事：注入有上界（条数与字符两重）、退役只写标记不删行、
命中计数只涨在真进 prompt 的那几条上、per-role 桶互不串、总开关能关死。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.config import Settings
from rolecard_agent.core.memory import (
    GLOBAL_BUCKET,
    MAX_ITEMS_PER_BUCKET,
    MAX_ITEMS_PER_TURN,
    MAX_MEMORY_CHARS,
    add_item,
    clamp_importance,
    current_role_id_ctx,
    delete_item,
    edit_item,
    get_item,
    invalidate_item,
    list_items,
    make_memory_tool,
    memory_for_turn,
    ranked_active,
    render_memory,
    replace_bucket_from_text,
    set_importance,
    set_pinned,
    top_active_item,
)

ME = DEFAULT_USER_ID  # 这台实例的主人在测试里的名字（M2b 之后每次读写都要说清为谁）


def _settings(enabled: bool = True) -> Settings:
    return Settings(memory_enabled=enabled)


def _seed(conn, *facts: str, bucket: str = GLOBAL_BUCKET) -> list[int]:
    return [int(add_item(conn, user_id=ME, bucket=bucket, text=f)["id"]) for f in facts]


def _backdate(conn, item_id: int, *, days: int, hits: int = 0) -> None:
    """把一条挪到"多少天前"并设定命中数（没有 `last_hit_at` 时，近因的锚点就是 created_at）。

    直接写库而不是走参数：这两个值是排序的输入，测试要的是"摆出这个局面"，
    而不是给生产代码开一条只给测试用的后门。
    """
    stamp = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE role_memory_item SET created_at = ?, hit_count = ? WHERE id = ?",
        (stamp, hits, item_id),
    )
    conn.commit()


# ------------------------------------------------------------------ 基本读写


def test_add_list_roundtrip_and_dedup(conn) -> None:
    assert (
        list_items(conn, user_id=ME, bucket=GLOBAL_BUCKET) == []
    )  # 从没写过 = 空列表，不是缺表报错
    ids = _seed(conn, "用户住在上海", "用户每周五交周报")
    assert len(ids) == 2
    # 完全相同的事实不重复插入（模型执着地"再记一次"不该把桶撑满）
    again = add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="用户住在上海")
    assert int(again["id"]) == ids[0]
    assert len(list_items(conn, user_id=ME, bucket=GLOBAL_BUCKET)) == 2


def test_whitespace_and_overlong_fact_are_normalised(conn) -> None:
    from rolecard_agent.core.memory import MAX_ITEM_CHARS

    added = add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="  用户   喜欢\n猫  ")
    assert added["text"] == "用户 喜欢 猫"
    long_one = add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="字" * (MAX_ITEM_CHARS + 50))
    assert len(long_one["text"]) == MAX_ITEM_CHARS


def test_empty_fact_adds_nothing(conn) -> None:
    assert add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="   ") is None
    assert list_items(conn, user_id=ME, bucket=GLOBAL_BUCKET) == []


# ------------------------------------------------------------------ 注入（上界与顺序）


def test_injection_respects_char_budget_whole_items_only(conn) -> None:
    """超预算时**整条不进**，绝不塞半句 —— 半条事实会被模型当完整事实用。"""
    filler = "事" * 60
    for i in range(200):
        add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text=f"{i} {filler}")
    text, ids = render_memory(conn, user_id=ME, bucket=GLOBAL_BUCKET)
    assert len(text) <= MAX_MEMORY_CHARS
    lines = text.splitlines()
    assert lines and all(ln.startswith("- ") for ln in lines)
    assert len(ids) == len(lines)
    assert all(len(ln) - 2 <= 200 for ln in lines)


def test_pinned_items_are_injected_first(conn) -> None:
    ids = _seed(conn, "次要的一条", "很重要的一条")
    set_pinned(conn, user_id=ME, item_id=ids[1], pinned=True)
    first = ranked_active(conn, user_id=ME, bucket=GLOBAL_BUCKET)[0]
    assert first["text"] == "很重要的一条"


def test_hit_count_and_recency_decide_the_order(conn) -> None:
    """近因×频次：被反复注入的排在一直没人用的前面；新条目也不会一进来就被淘汰。"""
    a, b = _seed(conn, "常被用到的一条", "没人理的一条")
    for _ in range(5):
        conn.execute("UPDATE role_memory_item SET hit_count = hit_count + 1 WHERE id = ?", (a,))
    conn.commit()
    ranked = ranked_active(conn, user_id=ME, bucket=GLOBAL_BUCKET, now=datetime.now(UTC))
    assert ranked[0]["text"] == "常被用到的一条"
    assert ranked[1]["text"] == "没人理的一条"


def test_importance_outranks_a_fresh_throwaway_line(conn) -> None:
    """显著性这一维要顶得住近因：60 天没提的"青霉素过敏"该排在今天随口一句前面。

    旧的乘法（`近因 × log1p(hits)`）做不到 —— 同一个局面下两条是 0.402 vs 0.693（新闲聊赢）。
    所以这里同一份数据把档号拿掉再断言一次：**翻转它的是这一维，不是别的**。
    """
    allergy = int(
        add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="用户青霉素过敏", importance=2)["id"]
    )
    fresh = int(add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="今天喝了燕麦奶")["id"])
    _backdate(conn, allergy, days=60, hits=3)
    _backdate(conn, fresh, days=0, hits=0)
    assert [i["text"] for i in ranked_active(conn, user_id=ME, bucket=GLOBAL_BUCKET)] == [
        "用户青霉素过敏",
        "今天喝了燕麦奶",
    ]

    conn.execute("UPDATE role_memory_item SET importance = 1 WHERE id = ?", (allergy,))
    conn.commit()
    assert ranked_active(conn, user_id=ME, bucket=GLOBAL_BUCKET)[0]["text"] == "今天喝了燕麦奶"


def test_frequency_saturates_and_cannot_buy_out_a_critical_fact(conn) -> None:
    """频次有饱和点（`HIT_CEIL`）：被提过几千次的旧口癖，压不过同龄、标要紧的事实。

    没有上限的话"次数"这一维可以无限涨，而它只说明"被提过"，不说明"更要紧"。
    """
    noisy = int(
        add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="她爱用「今天」开头", importance=0)[
            "id"
        ]
    )
    critical = int(
        add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="用户在做胰岛素治疗", importance=2)[
            "id"
        ]
    )
    _backdate(conn, noisy, days=200, hits=5000)
    _backdate(conn, critical, days=200, hits=0)
    assert ranked_active(conn, user_id=ME, bucket=GLOBAL_BUCKET)[0]["id"] == critical


def test_importance_is_clamped_and_only_ever_raised(conn) -> None:
    """档号来自模型，什么数都可能给：越界钳进 0..2，认不出的退回常规 1，**再提一次不降级**。"""
    assert clamp_importance(9) == 2 and clamp_importance(-4) == 0
    assert clamp_importance(None) == 1 and clamp_importance("很高") == 1

    item = add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="用户住在苏州", importance=9)
    assert int(item["importance"]) == 2
    # 同一条再说一次（默认档 1）走的是判重合并那条路，它该保持 2：
    # 降级是「整理记忆」或用户做的判断，不是"再提一次"的副作用。
    again = add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="用户住在苏州")
    assert int(again["importance"]) == 2 and int(again["id"]) == int(item["id"])
    low = add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="另一条", importance=-4)
    assert int(low["importance"]) == 0


def test_set_importance_is_the_one_write_allowed_to_downgrade(conn) -> None:
    """`add_item` 永不降级（再提一次不是判断），但面板那一档**就是**用户的判断：能升也能降，
    包括降到 0（0 不是"没给"）。降完排序立刻跟着走，不然这个控件只是个存着好看的数。"""
    a = int(
        add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="用户青霉素过敏", importance=2)["id"]
    )
    b = int(add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="用户喜欢喝美式")["id"])
    assert [int(i["id"]) for i in ranked_active(conn, user_id=ME, bucket=GLOBAL_BUCKET)] == [a, b]

    assert int(set_importance(conn, user_id=ME, item_id=a, importance=0)["importance"]) == 0  # type: ignore[index]
    assert int(get_item(conn, b, user_id=ME)["importance"]) == 1  # 只动被点的那一条
    assert [int(i["id"]) for i in ranked_active(conn, user_id=ME, bucket=GLOBAL_BUCKET)] == [b, a]

    # 越界与坏值同一条路：钳进 0..2，认不出的退回 1，不抛。
    assert int(set_importance(conn, user_id=ME, item_id=a, importance=9)["importance"]) == 2  # type: ignore[index]
    assert int(set_importance(conn, user_id=ME, item_id=a, importance="很高")["importance"]) == 1  # type: ignore[index]
    assert set_importance(conn, user_id=ME, item_id=999999, importance=2) is None


def test_the_critical_marker_appears_only_on_the_injection_side(conn) -> None:
    """【要紧】只挂在注入侧。面板那段文本会被原样填回编辑框、保存时整行覆写条目 ——
    标记进了面板文本就等于被写回存储层（时间标签当年犯的正是同一个错）。"""
    add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="用户青霉素过敏", importance=2)
    add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="用户住在苏州", importance=1)
    injected, _ids = render_memory(conn, user_id=ME, bucket=GLOBAL_BUCKET, with_age_labels=True)
    assert "【要紧】用户青霉素过敏" in injected
    assert "要紧" not in render_memory(conn, user_id=ME, bucket=GLOBAL_BUCKET)[0]


def test_memory_for_turn_marks_only_what_it_injected(conn) -> None:
    """命中计数只涨在**真进 prompt 的那几条**上 —— 它是退役排序里"频次"那一半的唯一来源。"""
    a, b = _seed(conn, "第一条", "第二条")
    memory_for_turn(conn, _settings(), None, user_id=ME)
    assert int(get_item(conn, a, user_id=ME)["hit_count"]) == 1
    assert int(get_item(conn, b, user_id=ME)["hit_count"]) == 1  # 两条都在预算内，一起算命中
    assert get_item(conn, a, user_id=ME)["last_hit_at"]
    # 总闸关掉时不注入，也就不该涨命中（不然退役排序会被"注入了但其实没注入"污染）
    memory_for_turn(conn, _settings(False), None, user_id=ME)
    assert int(get_item(conn, a, user_id=ME)["hit_count"]) == 1


def test_master_switch_stops_injection_and_write(conn) -> None:
    _seed(conn, "用户住在上海")
    assert memory_for_turn(conn, _settings(False), None, user_id=ME) == ""
    tool = make_memory_tool(settings=_settings(False), conn=conn)
    assert "未启用" in tool.invoke({"fact": "用户喜欢猫"})


# ------------------------------------------------------------------ 桶隔离


def test_role_bucket_and_global_bucket_are_isolated(conn) -> None:
    _seed(conn, "全局：用户养了只猫")
    _seed(conn, "角色专属：她答应过帮他查资料", bucket="elsie")
    assert memory_for_turn(conn, _settings(), "elsie", user_id=ME).startswith("- 今天 角色专属")
    # 该角色没有专属记忆时回退全局（全局存的是用户事实，不构成跨角色串扰）
    assert memory_for_turn(conn, _settings(), "nobody", user_id=ME).startswith("- 今天 全局")
    assert "她答应过帮他查资料" not in memory_for_turn(conn, _settings(), "other", user_id=ME)


def test_injected_lines_carry_an_age_label(conn) -> None:
    """每条记忆前面带时间。**没有保质期的事实会被当成"此刻仍然如此"**来说，
    而事实会变（复查之后指标正常了、换工作了）。标签口径见 `_age_label`。
    """
    _seed(conn, "今天说的一条")
    old = _seed(conn, "四十天前说的一条")[0]
    long_ago = datetime.now(UTC) - timedelta(days=40)
    conn.execute(
        "UPDATE role_memory_item SET created_at = ? WHERE id = ?",
        (long_ago.strftime("%Y-%m-%d %H:%M:%S"), old),
    )
    conn.commit()
    text = memory_for_turn(conn, _settings(), None, user_id=ME)
    assert "- 今天 今天说的一条" in text
    assert f"- {long_ago.month}月{long_ago.day}日 四十天前说的一条" in text
    # 一周内用"几天前"，读起来才知道那件事有多近
    recent = _seed(conn, "三天前说的一条")[0]
    conn.execute(
        "UPDATE role_memory_item SET created_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), recent),
    )
    conn.commit()
    assert "- 3天前 三天前说的一条" in memory_for_turn(conn, _settings(), None, user_id=ME)


def test_panel_text_has_no_age_label_but_injection_does(conn) -> None:
    """标签只能长在注入那一条路上。**设置面板把 `content` 原样填进编辑框、保存时又整段
    按行覆写条目**（`SettingsPage` 的 `setMemDraft(m.content)`）—— 面板那段一旦带标签，
    用户点一次保存「今天」就成了事实正文的一部分，再存一次叠一层。
    """
    _seed(conn, "用户住在上海")
    panel, _ = render_memory(conn, user_id=ME, bucket=GLOBAL_BUCKET)
    assert panel == "- 用户住在上海", "面板文本必须等于原文，不加任何显示用的前后缀"
    assert memory_for_turn(conn, _settings(), None, user_id=ME) == "- 今天 用户住在上海"
    # 覆写回来的文本按行解析，显示用的前缀不该变成事实的一部分（存三次仍然只有一层）
    for _ in range(3):
        replace_bucket_from_text(conn, user_id=ME, bucket=GLOBAL_BUCKET, text=panel)
        assert [i["text"] for i in list_items(conn, user_id=ME, bucket=GLOBAL_BUCKET)] == [
            "用户住在上海"
        ]
    # 库里可能已经存着叠好的旧数据（这个 bug 上线过一次），剥多层也要成立
    replace_bucket_from_text(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="- - - 用户搬到了杭州")
    assert [i["text"] for i in list_items(conn, user_id=ME, bucket=GLOBAL_BUCKET)][
        -1
    ] == "用户搬到了杭州"


def test_injection_is_capped_by_count_not_only_by_chars(conn) -> None:
    """**条数**上界与字符预算是两重：4000 字塞得下几十条短句，而 LoCoMo 实测
    top-5 优于 top-50 —— 给多了不是更全，是给模型一堆互相竞争的事实。
    """
    facts = [f"短事实第{i}号" for i in range(40)]  # 每条十几个字，字符预算根本用不完
    _seed(conn, *facts)
    text, ids = render_memory(conn, user_id=ME, bucket=GLOBAL_BUCKET)
    assert len(text.splitlines()) == MAX_ITEMS_PER_TURN
    assert len(ids) == MAX_ITEMS_PER_TURN
    assert len(text) < MAX_MEMORY_CHARS, "这条要证的是「卡条数」，不是「卡字符」"
    # 留下的是打分最高的那几条，不是插入顺序的前几条（钉住的优先，其余按 近因×频次）
    assert all(f"短事实第{i}号" in text for i in range(40 - MAX_ITEMS_PER_TURN, 40))


def test_memory_save_tool_writes_both_buckets(conn) -> None:
    tool = make_memory_tool(settings=_settings(), conn=conn)
    token = current_role_id_ctx.set("elsie")
    try:
        out = tool.invoke({"fact": "每周五要交周报"})
    finally:
        current_role_id_ctx.reset(token)
    assert "已记住" in out
    assert [i["text"] for i in list_items(conn, user_id=ME, bucket="elsie")] == ["每周五要交周报"]
    assert [i["text"] for i in list_items(conn, user_id=ME, bucket=GLOBAL_BUCKET)] == [
        "每周五要交周报"
    ]


# ------------------------------------------------------------------ 退役与整理


def test_invalidate_keeps_the_row_and_hides_it_from_injection(conn) -> None:
    """**失效不物理删**：注入里消失，但仍查得到、可撤销 —— 整理错了要能回滚。"""
    (kept, gone) = _seed(conn, "还在用的", "被取代的")
    invalidate_item(conn, user_id=ME, item_id=gone, superseded_by=kept)
    assert "被取代的" not in render_memory(conn, user_id=ME, bucket=GLOBAL_BUCKET)[0]
    row = get_item(conn, gone, user_id=ME)
    assert row["invalidated_at"] and row["superseded_by"] == kept
    assert len(list_items(conn, user_id=ME, bucket=GLOBAL_BUCKET, include_invalidated=True)) == 2


def test_cap_retires_the_weakest_without_deleting(conn) -> None:
    """超限的保底淘汰发生在写入路径上（每次 add 都 enforce），所以桶**不会**越过上限。

    被挤掉的那些只是**失效**（行还在、可查可撤销），不是被 DELETE —— 整理错了要回得来。
    """
    ids = _seed(conn, *(f"事实 {i}" for i in range(MAX_ITEMS_PER_BUCKET)))
    stale = (datetime.now(UTC) - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE role_memory_item SET created_at = ? WHERE id IN ({})".format(",".join("?" * 5)),
        (stale, *ids[:5]),
    )
    conn.commit()

    add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="最新的一条")

    active = list_items(conn, user_id=ME, bucket=GLOBAL_BUCKET)
    assert len(active) == MAX_ITEMS_PER_BUCKET, "桶越过了上限"
    assert "最新的一条" in [i["text"] for i in active]
    still_there = {i["id"] for i in active}
    retired = [item_id for item_id in ids[:5] if item_id not in still_there]
    assert retired, "没有任何一条被淘汰，说明近因权重没参与排序"
    for item_id in retired:
        row = get_item(conn, item_id, user_id=ME)
        assert row is not None, "被淘汰的条目被物理删了"
        assert row["invalidated_at"], "被淘汰却没写 invalidated_at"


def test_cap_never_evicts_pinned(conn) -> None:
    ids = _seed(conn, *(f"事实 {i}" for i in range(MAX_ITEMS_PER_BUCKET)))
    set_pinned(conn, user_id=ME, item_id=ids[0], pinned=True)
    add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="挤进来的一条")
    assert any(i["id"] == ids[0] for i in list_items(conn, user_id=ME, bucket=GLOBAL_BUCKET))


def test_replace_from_text_preserves_pinned_and_rebuilds_the_rest(conn) -> None:
    """整段覆写的语义：一行一条；钉住的那条不动（它不该被一次整段保存抹掉）。"""
    (pinned, _gone) = _seed(conn, "钉住的要留着", "没钉的可以被覆盖")
    set_pinned(conn, user_id=ME, item_id=pinned, pinned=True)
    replace_bucket_from_text(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="新的一行\n又一行")
    texts = [i["text"] for i in list_items(conn, user_id=ME, bucket=GLOBAL_BUCKET)]
    assert "钉住的要留着" in texts
    assert "没钉的可以被覆盖" not in texts
    assert {"新的一行", "又一行"} <= set(texts)


def test_replace_from_text_carries_the_tiers_of_unchanged_lines(conn) -> None:
    """整段保存**不能把显著性抹回 1**：界面上没有那个控件，抹了就等于偷偷改了用户的记忆。

    这条洞是加 `importance` 那次自己挖的：`replace_bucket_from_text` 先 DELETE 再按行
    `add_item`，而 `add_item` 的默认档是 1 —— 于是用户在面板上编辑一次记忆文本，
    提取模型标好的「青霉素过敏 = 要紧」就悄悄降回常规，下一次它可能直接被挤出 prompt。
    """
    add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="用户青霉素过敏", importance=2)
    add_item(conn, user_id=ME, bucket=GLOBAL_BUCKET, text="用户住在苏州", importance=1)
    replace_bucket_from_text(
        conn,
        user_id=ME,
        bucket=GLOBAL_BUCKET,
        text="- 用户青霉素过敏\n- 用户住在苏州，最近换了公寓",
    )
    tiers = {
        str(i["text"]): int(i["importance"])
        for i in list_items(conn, user_id=ME, bucket=GLOBAL_BUCKET)
    }
    assert tiers == {"用户青霉素过敏": 2, "用户住在苏州，最近换了公寓": 1}
    # 逐字相同的那行保住档号；**改过的行是一句新话**，落回默认档（不是"猜它还想要 2"）


def test_delete_is_physical_and_edit_rewrites_in_place(conn) -> None:
    (one, two) = _seed(conn, "要删掉的", "要改掉的")
    assert delete_item(conn, user_id=ME, item_id=one) is True
    assert get_item(conn, one, user_id=ME) is None
    assert delete_item(conn, user_id=ME, item_id=one) is False  # 已经没了，如实说没删到
    edited = edit_item(conn, user_id=ME, item_id=two, text="改过了的")
    assert edited["text"] == "改过了的" and int(edited["id"]) == two


def test_deleting_the_survivor_clears_the_dangling_pointer(conn) -> None:
    """删掉"取代者"时，把指向它的 `superseded_by` 一起清掉（2026-10-30 引擎审计顺带项）。

    旧实现物理删了幸存者，旧行却还 `superseded_by=<不存在的 id>` —— 时间线把那条更正
    画成"更正为（空）"（`newer` 查不到 → text=""），而诚实的读法是"作废了一条事实"
    （`superseded_by` 为空那条既定分支）。清成 NULL 让两种读法不再混。
    """
    old = add_item(conn, user_id=ME, bucket="elsie", text="用户住在上海")
    fresh = add_item(conn, user_id=ME, bucket="elsie", text="用户搬到北京了")
    assert old is not None and fresh is not None
    invalidate_item(
        conn,
        user_id=ME,
        item_id=int(str(old["id"])),
        superseded_by=int(str(fresh["id"])),
    )
    assert delete_item(conn, user_id=ME, item_id=int(str(fresh["id"]))) is True
    survivor = get_item(conn, int(str(old["id"])), user_id=ME)
    assert survivor is not None
    assert survivor["superseded_by"] is None, (
        "取代者被删了而指针还悬着 —— 时间线会画成猜不透的'更正为（空）'"
    )


def test_recall_material_is_the_top_active_item(conn) -> None:
    """E2 的闸门与素材同源：`top_active_item` 既决定能不能回忆，也决定回忆哪一条。"""
    assert top_active_item(conn, user_id=ME, bucket="elsie") is None
    (only,) = _seed(conn, "答应帮他查资料", bucket="elsie")
    # 别的角色的条目不能当这个角色的素材
    assert top_active_item(conn, user_id=ME, bucket="other") is None
    assert int(top_active_item(conn, user_id=ME, bucket="elsie")["id"]) == only


# ------------------------------------------------------------------ 记忆分层
#
# 2026-10-30，用户：「每个角色的记忆分多板块，比如长期记忆、会话记忆、项目记忆」，
# 以及「留一些给它自己想记住啥就记住啥」。
#
# 下面钉的是**分层的语义**，不是字段存在：
#   - 分层是**一列**不是几张表（检索要能一次命中多层）；
#   - 每层有自己的注入配额（会话层变多不能把长期层整层挤出去）；
#   - 角色自记层是给"它自己想记住啥就记住啥"的。
# ---------------------------------------------------------------------------


def test_items_land_in_their_category_and_list_can_filter(conn) -> None:
    """写入落到指定层；读可以只看某一层。"""
    long_id = int(add_item(conn, user_id=ME, bucket="r1", text="用户住在上海")["id"])
    self_id = int(
        add_item(conn, user_id=ME, bucket="r1", text="我想起来了：周五要交报告", category="self")[
            "id"
        ]
    )
    assert get_item(conn, long_id, user_id=ME)["category"] == "longterm"
    assert get_item(conn, self_id, user_id=ME)["category"] == "self"

    only_self = list_items(conn, user_id=ME, bucket="r1", category="self")
    assert [i["id"] for i in only_self] == [self_id]
    # 不给 category = 全部层（这是默认读法，不能因为加了层就只看到一层）。
    assert len(list_items(conn, user_id=ME, bucket="r1")) == 2


def test_unknown_category_falls_back_to_longterm(conn) -> None:
    """不认识的层名归 longterm，不是不写进去 —— 归错了用户能自己改，归错了写不进去就麻烦了。"""
    item = add_item(conn, user_id=ME, bucket="r1", text="某条事实", category="不存在的层")
    assert item is not None
    assert item["category"] == "longterm"


def test_each_category_has_its_own_injection_quota(conn) -> None:
    """**每层各自限条数**，不是四层抢同一个总名额。

    变异：把 `memory_for_turn` 改成不分层（渲染全部再一起截断）⇒ 本用例红 ——
    会话层一变多，长期事实（"她记得我的称呼"）就被挤出去了。
    """
    # 会话层塞满（比长期层的配额多）
    for i in range(12):
        add_item(conn, user_id=ME, bucket="r1", text=f"会话摘要 {i}", category="session")
    # 长期层一条
    add_item(conn, user_id=ME, bucket="r1", text="用户住在上海", category="longterm")

    text = memory_for_turn(
        conn, _settings(), "r1", user_id=ME, inject_categories=("longterm", "session")
    )
    assert "用户住在上海" in text, "长期记忆被会话层挤出去了"
    # 会话层按自己的配额截断（12 条进 4 条）
    assert text.count("会话摘要") <= 4, text


def test_inject_categories_gate_which_layers_reach_the_prompt(conn) -> None:
    """只注入指定的层 —— 用户可以在面板上关掉某一层的注入。"""
    add_item(conn, user_id=ME, bucket="r1", text="长期的一条", category="longterm")
    add_item(conn, user_id=ME, bucket="r1", text="项目的一条", category="project")

    only_long = memory_for_turn(
        conn, _settings(), "r1", user_id=ME, inject_categories=("longterm",)
    )
    assert "长期的一条" in only_long
    assert "项目的一条" not in only_long

    only_project = memory_for_turn(
        conn, _settings(), "r1", user_id=ME, inject_categories=("project",)
    )
    assert "项目的一条" in only_project
    assert "长期的一条" not in only_project


def test_edit_can_move_an_item_between_categories(conn) -> None:
    """用户能在面板上把一条从"长期"挪到"项目"。"""
    item_id = int(add_item(conn, user_id=ME, bucket="r1", text="某条事实")["id"])
    updated = edit_item(conn, user_id=ME, item_id=item_id, text="", category="project")
    assert updated is not None
    assert updated["category"] == "project"
    assert updated["text"] == "某条事实", "改层不该动文本"


def test_resolve_inject_categories_parses_and_falls_back() -> None:
    """设置里那串层名的解析口径：认不出的丢掉，全空回落出厂默认。"""
    from rolecard_agent.core.memory import (
        DEFAULT_INJECT_CATEGORIES,
        resolve_inject_categories,
    )

    assert resolve_inject_categories("") == DEFAULT_INJECT_CATEGORIES
    assert resolve_inject_categories(None) == DEFAULT_INJECT_CATEGORIES
    assert resolve_inject_categories("longterm,project") == ("longterm", "project")
    # 不认识的层名丢掉，认识的留下
    assert resolve_inject_categories("longterm,胡说") == ("longterm",)
    # 全是乱写 → 回落默认（不是"注入零层"——那会让记忆看起来坏了）
    assert resolve_inject_categories("胡说八道") == DEFAULT_INJECT_CATEGORIES


def test_settings_drive_which_layers_are_injected(conn) -> None:
    """注入哪几层由 `settings.memory_inject_categories` 决定（用户在界面上的那个开关）。"""
    add_item(conn, user_id=ME, bucket="r1", text="长期的一条", category="longterm")
    add_item(conn, user_id=ME, bucket="r1", text="项目的一条", category="project")

    only_project = Settings(memory_enabled=True, memory_inject_categories="project")
    text = memory_for_turn(conn, only_project, "r1", user_id=ME)
    assert "项目的一条" in text
    assert "长期的一条" not in text


def test_replace_by_category_only_touches_that_layer(conn) -> None:
    """**按层覆写不能删掉别的层** —— 数据安全级的回归钉。

    界面按板块显示某一层的文本，用户在"项目记忆"板块里点保存。若覆写还按整桶删，
    他另外三层的条目全没了 —— 一个"保存"动作毁掉没看见的内容。

    变异：把 `replace_bucket_from_text` 的 category 参数摘掉（永远整桶删）⇒ 本用例红。
    """
    from rolecard_agent.core.memory import replace_bucket_from_text

    add_item(conn, user_id=ME, bucket="r1", text="长期的一条", category="longterm")
    add_item(conn, user_id=ME, bucket="r1", text="项目的一条", category="project")
    add_item(conn, user_id=ME, bucket="r1", text="自记的一条", category="self")

    replace_bucket_from_text(conn, user_id=ME, bucket="r1", text="项目改过了", category="project")

    texts = {i["text"] for i in list_items(conn, user_id=ME, bucket="r1")}
    assert "项目改过了" in texts
    assert "长期的一条" in texts, "按层覆写把长期层删了"
    assert "自记的一条" in texts, "按层覆写把角色自记删了"
    assert "项目的一条" not in texts, "旧的那条应该被替换掉"


def test_replace_without_category_still_replaces_the_whole_bucket(conn) -> None:
    """不带 category = 旧语义（整桶覆写）—— 那是"原文视图"那个老入口的行为，不能变。"""
    from rolecard_agent.core.memory import replace_bucket_from_text

    add_item(conn, user_id=ME, bucket="r1", text="长期的一条", category="longterm")
    add_item(conn, user_id=ME, bucket="r1", text="项目的一条", category="project")

    replace_bucket_from_text(conn, user_id=ME, bucket="r1", text="只剩这条")

    texts = {i["text"] for i in list_items(conn, user_id=ME, bucket="r1")}
    assert texts == {"只剩这条"}
