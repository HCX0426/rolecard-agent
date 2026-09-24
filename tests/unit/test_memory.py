"""跨会话记忆（core/memory.py，条目表）的单元测试。

钉的是条目化换来的那几件事：注入有上界（条数与字符两重）、退役只写标记不删行、
命中计数只涨在真进 prompt 的那几条上、per-role 桶互不串、总开关能关死。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    set_pinned,
    top_active_item,
)


def _settings(enabled: bool = True) -> Settings:
    return Settings(memory_enabled=enabled)


def _seed(conn, *facts: str, bucket: str = GLOBAL_BUCKET) -> list[int]:
    return [int(add_item(conn, bucket=bucket, text=f)["id"]) for f in facts]


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
    assert list_items(conn, bucket=GLOBAL_BUCKET) == []  # 从没写过 = 空列表，不是缺表报错
    ids = _seed(conn, "用户住在上海", "用户每周五交周报")
    assert len(ids) == 2
    # 完全相同的事实不重复插入（模型执着地"再记一次"不该把桶撑满）
    again = add_item(conn, bucket=GLOBAL_BUCKET, text="用户住在上海")
    assert int(again["id"]) == ids[0]
    assert len(list_items(conn, bucket=GLOBAL_BUCKET)) == 2


def test_whitespace_and_overlong_fact_are_normalised(conn) -> None:
    from rolecard_agent.core.memory import MAX_ITEM_CHARS

    added = add_item(conn, bucket=GLOBAL_BUCKET, text="  用户   喜欢\n猫  ")
    assert added["text"] == "用户 喜欢 猫"
    long_one = add_item(conn, bucket=GLOBAL_BUCKET, text="字" * (MAX_ITEM_CHARS + 50))
    assert len(long_one["text"]) == MAX_ITEM_CHARS


def test_empty_fact_adds_nothing(conn) -> None:
    assert add_item(conn, bucket=GLOBAL_BUCKET, text="   ") is None
    assert list_items(conn, bucket=GLOBAL_BUCKET) == []


# ------------------------------------------------------------------ 注入（上界与顺序）


def test_injection_respects_char_budget_whole_items_only(conn) -> None:
    """超预算时**整条不进**，绝不塞半句 —— 半条事实会被模型当完整事实用。"""
    filler = "事" * 60
    for i in range(200):
        add_item(conn, bucket=GLOBAL_BUCKET, text=f"{i} {filler}")
    text, ids = render_memory(conn, bucket=GLOBAL_BUCKET)
    assert len(text) <= MAX_MEMORY_CHARS
    lines = text.splitlines()
    assert lines and all(ln.startswith("- ") for ln in lines)
    assert len(ids) == len(lines)
    assert all(len(ln) - 2 <= 200 for ln in lines)


def test_pinned_items_are_injected_first(conn) -> None:
    ids = _seed(conn, "次要的一条", "很重要的一条")
    set_pinned(conn, item_id=ids[1], pinned=True)
    first = ranked_active(conn, bucket=GLOBAL_BUCKET)[0]
    assert first["text"] == "很重要的一条"


def test_hit_count_and_recency_decide_the_order(conn) -> None:
    """近因×频次：被反复注入的排在一直没人用的前面；新条目也不会一进来就被淘汰。"""
    a, b = _seed(conn, "常被用到的一条", "没人理的一条")
    for _ in range(5):
        conn.execute("UPDATE role_memory_item SET hit_count = hit_count + 1 WHERE id = ?", (a,))
    conn.commit()
    ranked = ranked_active(conn, bucket=GLOBAL_BUCKET, now=datetime.now(UTC))
    assert ranked[0]["text"] == "常被用到的一条"
    assert ranked[1]["text"] == "没人理的一条"


def test_importance_outranks_a_fresh_throwaway_line(conn) -> None:
    """显著性这一维要顶得住近因：60 天没提的"青霉素过敏"该排在今天随口一句前面。

    旧的乘法（`近因 × log1p(hits)`）做不到 —— 同一个局面下两条是 0.402 vs 0.693（新闲聊赢）。
    所以这里同一份数据把档号拿掉再断言一次：**翻转它的是这一维，不是别的**。
    """
    allergy = int(add_item(conn, bucket=GLOBAL_BUCKET, text="用户青霉素过敏", importance=2)["id"])
    fresh = int(add_item(conn, bucket=GLOBAL_BUCKET, text="今天喝了燕麦奶")["id"])
    _backdate(conn, allergy, days=60, hits=3)
    _backdate(conn, fresh, days=0, hits=0)
    assert [i["text"] for i in ranked_active(conn, bucket=GLOBAL_BUCKET)] == [
        "用户青霉素过敏",
        "今天喝了燕麦奶",
    ]

    conn.execute("UPDATE role_memory_item SET importance = 1 WHERE id = ?", (allergy,))
    conn.commit()
    assert ranked_active(conn, bucket=GLOBAL_BUCKET)[0]["text"] == "今天喝了燕麦奶"


def test_frequency_saturates_and_cannot_buy_out_a_critical_fact(conn) -> None:
    """频次有饱和点（`HIT_CEIL`）：被提过几千次的旧口癖，压不过同龄、标要紧的事实。

    没有上限的话"次数"这一维可以无限涨，而它只说明"被提过"，不说明"更要紧"。
    """
    noisy = int(add_item(conn, bucket=GLOBAL_BUCKET, text="她爱用「今天」开头", importance=0)["id"])
    critical = int(
        add_item(conn, bucket=GLOBAL_BUCKET, text="用户在做胰岛素治疗", importance=2)["id"]
    )
    _backdate(conn, noisy, days=200, hits=5000)
    _backdate(conn, critical, days=200, hits=0)
    assert ranked_active(conn, bucket=GLOBAL_BUCKET)[0]["id"] == critical


def test_importance_is_clamped_and_only_ever_raised(conn) -> None:
    """档号来自模型，什么数都可能给：越界钳进 0..2，认不出的退回常规 1，**再提一次不降级**。"""
    assert clamp_importance(9) == 2 and clamp_importance(-4) == 0
    assert clamp_importance(None) == 1 and clamp_importance("很高") == 1

    item = add_item(conn, bucket=GLOBAL_BUCKET, text="用户住在苏州", importance=9)
    assert int(item["importance"]) == 2
    # 同一条再说一次（默认档 1）走的是判重合并那条路，它该保持 2：
    # 降级是「整理记忆」或用户做的判断，不是"再提一次"的副作用。
    again = add_item(conn, bucket=GLOBAL_BUCKET, text="用户住在苏州")
    assert int(again["importance"]) == 2 and int(again["id"]) == int(item["id"])
    low = add_item(conn, bucket=GLOBAL_BUCKET, text="另一条", importance=-4)
    assert int(low["importance"]) == 0


def test_the_critical_marker_appears_only_on_the_injection_side(conn) -> None:
    """【要紧】只挂在注入侧。面板那段文本会被原样填回编辑框、保存时整行覆写条目 ——
    标记进了面板文本就等于被写回存储层（时间标签当年犯的正是同一个错）。"""
    add_item(conn, bucket=GLOBAL_BUCKET, text="用户青霉素过敏", importance=2)
    add_item(conn, bucket=GLOBAL_BUCKET, text="用户住在苏州", importance=1)
    injected, _ids = render_memory(conn, bucket=GLOBAL_BUCKET, with_age_labels=True)
    assert "【要紧】用户青霉素过敏" in injected
    assert "要紧" not in render_memory(conn, bucket=GLOBAL_BUCKET)[0]


def test_memory_for_turn_marks_only_what_it_injected(conn) -> None:
    """命中计数只涨在**真进 prompt 的那几条**上 —— 它是退役排序里"频次"那一半的唯一来源。"""
    a, b = _seed(conn, "第一条", "第二条")
    memory_for_turn(conn, _settings(), None)
    assert int(get_item(conn, a)["hit_count"]) == 1
    assert int(get_item(conn, b)["hit_count"]) == 1  # 两条都在预算内，一起算命中
    assert get_item(conn, a)["last_hit_at"]
    # 总闸关掉时不注入，也就不该涨命中（不然退役排序会被"注入了但其实没注入"污染）
    memory_for_turn(conn, _settings(False), None)
    assert int(get_item(conn, a)["hit_count"]) == 1


def test_master_switch_stops_injection_and_write(conn) -> None:
    _seed(conn, "用户住在上海")
    assert memory_for_turn(conn, _settings(False), None) == ""
    tool = make_memory_tool(settings=_settings(False), conn=conn)
    assert "未启用" in tool.invoke({"fact": "用户喜欢猫"})


# ------------------------------------------------------------------ 桶隔离


def test_role_bucket_and_global_bucket_are_isolated(conn) -> None:
    _seed(conn, "全局：用户养了只猫")
    _seed(conn, "角色专属：她答应过帮他查资料", bucket="elsie")
    assert memory_for_turn(conn, _settings(), "elsie").startswith("- 今天 角色专属")
    # 该角色没有专属记忆时回退全局（全局存的是用户事实，不构成跨角色串扰）
    assert memory_for_turn(conn, _settings(), "nobody").startswith("- 今天 全局")
    assert "她答应过帮他查资料" not in memory_for_turn(conn, _settings(), "other")


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
    text = memory_for_turn(conn, _settings(), None)
    assert "- 今天 今天说的一条" in text
    assert f"- {long_ago.month}月{long_ago.day}日 四十天前说的一条" in text
    # 一周内用"几天前"，读起来才知道那件事有多近
    recent = _seed(conn, "三天前说的一条")[0]
    conn.execute(
        "UPDATE role_memory_item SET created_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), recent),
    )
    conn.commit()
    assert "- 3天前 三天前说的一条" in memory_for_turn(conn, _settings(), None)


def test_panel_text_has_no_age_label_but_injection_does(conn) -> None:
    """标签只能长在注入那一条路上。**设置面板把 `content` 原样填进编辑框、保存时又整段
    按行覆写条目**（`SettingsPage` 的 `setMemDraft(m.content)`）—— 面板那段一旦带标签，
    用户点一次保存「今天」就成了事实正文的一部分，再存一次叠一层。
    """
    _seed(conn, "用户住在上海")
    panel, _ = render_memory(conn, bucket=GLOBAL_BUCKET)
    assert panel == "- 用户住在上海", "面板文本必须等于原文，不加任何显示用的前后缀"
    assert memory_for_turn(conn, _settings(), None) == "- 今天 用户住在上海"
    # 覆写回来的文本按行解析，显示用的前缀不该变成事实的一部分（存三次仍然只有一层）
    for _ in range(3):
        replace_bucket_from_text(conn, bucket=GLOBAL_BUCKET, text=panel)
        assert [i["text"] for i in list_items(conn, bucket=GLOBAL_BUCKET)] == ["用户住在上海"]
    # 库里可能已经存着叠好的旧数据（这个 bug 上线过一次），剥多层也要成立
    replace_bucket_from_text(conn, bucket=GLOBAL_BUCKET, text="- - - 用户搬到了杭州")
    assert [i["text"] for i in list_items(conn, bucket=GLOBAL_BUCKET)][-1] == "用户搬到了杭州"


def test_injection_is_capped_by_count_not_only_by_chars(conn) -> None:
    """**条数**上界与字符预算是两重：4000 字塞得下几十条短句，而 LoCoMo 实测
    top-5 优于 top-50 —— 给多了不是更全，是给模型一堆互相竞争的事实。
    """
    facts = [f"短事实第{i}号" for i in range(40)]  # 每条十几个字，字符预算根本用不完
    _seed(conn, *facts)
    text, ids = render_memory(conn, bucket=GLOBAL_BUCKET)
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
    assert [i["text"] for i in list_items(conn, bucket="elsie")] == ["每周五要交周报"]
    assert [i["text"] for i in list_items(conn, bucket=GLOBAL_BUCKET)] == ["每周五要交周报"]


# ------------------------------------------------------------------ 退役与整理


def test_invalidate_keeps_the_row_and_hides_it_from_injection(conn) -> None:
    """**失效不物理删**：注入里消失，但仍查得到、可撤销 —— 整理错了要能回滚。"""
    (kept, gone) = _seed(conn, "还在用的", "被取代的")
    invalidate_item(conn, item_id=gone, superseded_by=kept)
    assert "被取代的" not in render_memory(conn, bucket=GLOBAL_BUCKET)[0]
    row = get_item(conn, gone)
    assert row["invalidated_at"] and row["superseded_by"] == kept
    assert len(list_items(conn, bucket=GLOBAL_BUCKET, include_invalidated=True)) == 2


def test_cap_retires_the_weakest_without_deleting(conn) -> None:
    """超限的保底淘汰发生在写入路径上（每次 add 都 enforce），所以桶**不会**越过上限。

    被挤掉的那些只是**失效**（行还在、可查可撤销），不是被 DELETE —— 整理错了要回得来。
    """
    ids = _seed(conn, *(f"事实 {i}" for i in range(MAX_ITEMS_PER_BUCKET)))
    stale = (datetime.now(UTC) - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE role_memory_item SET created_at = ? WHERE id IN ({})".format(
            ",".join("?" * 5)
        ),
        (stale, *ids[:5]),
    )
    conn.commit()

    add_item(conn, bucket=GLOBAL_BUCKET, text="最新的一条")

    active = list_items(conn, bucket=GLOBAL_BUCKET)
    assert len(active) == MAX_ITEMS_PER_BUCKET, "桶越过了上限"
    assert "最新的一条" in [i["text"] for i in active]
    still_there = {i["id"] for i in active}
    retired = [item_id for item_id in ids[:5] if item_id not in still_there]
    assert retired, "没有任何一条被淘汰，说明近因权重没参与排序"
    for item_id in retired:
        row = get_item(conn, item_id)
        assert row is not None, "被淘汰的条目被物理删了"
        assert row["invalidated_at"], "被淘汰却没写 invalidated_at"


def test_cap_never_evicts_pinned(conn) -> None:
    ids = _seed(conn, *(f"事实 {i}" for i in range(MAX_ITEMS_PER_BUCKET)))
    set_pinned(conn, item_id=ids[0], pinned=True)
    add_item(conn, bucket=GLOBAL_BUCKET, text="挤进来的一条")
    assert any(i["id"] == ids[0] for i in list_items(conn, bucket=GLOBAL_BUCKET))


def test_replace_from_text_preserves_pinned_and_rebuilds_the_rest(conn) -> None:
    """整段覆写的语义：一行一条；钉住的那条不动（它不该被一次整段保存抹掉）。"""
    (pinned, _gone) = _seed(conn, "钉住的要留着", "没钉的可以被覆盖")
    set_pinned(conn, item_id=pinned, pinned=True)
    replace_bucket_from_text(conn, bucket=GLOBAL_BUCKET, text="新的一行\n又一行")
    texts = [i["text"] for i in list_items(conn, bucket=GLOBAL_BUCKET)]
    assert "钉住的要留着" in texts
    assert "没钉的可以被覆盖" not in texts
    assert {"新的一行", "又一行"} <= set(texts)


def test_delete_is_physical_and_edit_rewrites_in_place(conn) -> None:
    (one, two) = _seed(conn, "要删掉的", "要改掉的")
    assert delete_item(conn, item_id=one) is True
    assert get_item(conn, one) is None
    assert delete_item(conn, item_id=one) is False  # 已经没了，如实说没删到
    edited = edit_item(conn, item_id=two, text="改过了的")
    assert edited["text"] == "改过了的" and int(edited["id"]) == two


def test_recall_material_is_the_top_active_item(conn) -> None:
    """E2 的闸门与素材同源：`top_active_item` 既决定能不能回忆，也决定回忆哪一条。"""
    assert top_active_item(conn, bucket="elsie") is None
    (only,) = _seed(conn, "答应帮他查资料", bucket="elsie")
    # 别的角色的条目不能当这个角色的素材
    assert top_active_item(conn, bucket="other") is None
    assert int(top_active_item(conn, bucket="elsie")["id"]) == only
