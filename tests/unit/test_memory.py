"""跨会话记忆（core/memory.py）的单元测试。

覆盖：读写往返 / 超限截断 / 清空 / memory_save 工具的追加去重与总闸。
存储是 kernel_meta 的一条文本 —— 正确性要求不高，但"追加不吞已有事实、超限有上界、
总闸能关死"这三点是安全相关的，值得钉住。
"""

from __future__ import annotations

from rolecard_agent.config import Settings
from rolecard_agent.core.memory import (
    MAX_MEMORY_CHARS,
    clear_memory_text,
    current_role_id_ctx,
    load_memory_text,
    load_role_memory_text,
    make_memory_tool,
    save_memory_text,
)


def test_save_load_roundtrip(conn) -> None:
    assert load_memory_text(conn) == ""  # 从未写过 = 空，不是缺行报错
    save_memory_text(conn, "用户住在上海。\n用户每周五交周报。")
    assert load_memory_text(conn) == "用户住在上海。\n用户每周五交周报。"


def test_save_overwrites_whole_text(conn) -> None:
    save_memory_text(conn, "旧记忆")
    save_memory_text(conn, "新记忆")  # 面板保存是整体覆写，不是追加
    assert load_memory_text(conn) == "新记忆"


def test_cap_truncates_long_text(conn) -> None:
    long_text = "x" * (MAX_MEMORY_CHARS + 500)
    save_memory_text(conn, long_text)
    assert len(load_memory_text(conn)) == MAX_MEMORY_CHARS


def test_clear_removes_row(conn) -> None:
    save_memory_text(conn, "要清掉的事")
    clear_memory_text(conn)
    assert load_memory_text(conn) == ""


def test_memory_save_appends_and_dedupes(conn) -> None:
    tool = make_memory_tool(settings=Settings(), conn=conn)
    assert "已记住" in tool.invoke({"fact": "用户住在上海"})
    assert "已记住" in tool.invoke({"fact": "用户每周五交周报"})
    # 同一条事实再保存：去重（不产生重复行）；重复项移到末尾 = 视为"最近确认过"，
    # 超限淘汰最旧行时它最后一个被丢（见 memory_save 的裁剪顺序）。
    tool.invoke({"fact": "用户住在上海"})
    lines = load_memory_text(conn).splitlines()
    assert lines == ["用户每周五交周报", "用户住在上海"]


def test_memory_save_rejects_empty_and_respects_master_switch(conn) -> None:
    tool = make_memory_tool(settings=Settings(), conn=conn)
    assert "为空" in tool.invoke({"fact": "  "})
    # 总闸关闭：拒写，且不落库
    off = make_memory_tool(settings=Settings(memory_enabled=False), conn=conn)
    assert "未启用" in off.invoke({"fact": "用户住在上海"})
    assert load_memory_text(conn) == ""


def test_memory_save_caps_total(conn) -> None:
    tool = make_memory_tool(settings=Settings(), conn=conn)
    tool.invoke({"fact": "前缀 " + "a" * (MAX_MEMORY_CHARS - 10)})
    # 新事实 + 旧事实一起超过上限：丢最旧，整体不超上限
    tool.invoke({"fact": "新的一条很长的" + "b" * 100})
    assert len(load_memory_text(conn)) <= MAX_MEMORY_CHARS


def test_memory_save_writes_role_memory_when_role_active(conn) -> None:
    """关系驱动主动开口（§5.2）：在角色对话上下文里，memory_save 把事实同时写入该角色专属记忆，
    使回忆触发有内容来源；且其它角色不会拿到这条（隔离铁律）。"""
    tool = make_memory_tool(settings=Settings(), conn=conn)
    token = current_role_id_ctx.set("cat_maid")
    try:
        assert "已记住" in tool.invoke({"fact": "用户喜欢蓝莓"})
    finally:
        current_role_id_ctx.reset(token)
    # 全局用户级记忆有
    assert "用户喜欢蓝莓" in load_memory_text(conn)
    # 该角色专属记忆有（回忆触发的内容来源）
    assert "用户喜欢蓝莓" in load_role_memory_text(conn, "cat_maid")
    # 其它角色桶为空（绝不串到别的角色）
    assert load_role_memory_text(conn, "other_role") == ""


def test_memory_save_skips_role_memory_without_role_context(conn) -> None:
    """无角色上下文（空串）时只写全局，不污染任何角色桶。"""
    tool = make_memory_tool(settings=Settings(), conn=conn)
    assert "已记住" in tool.invoke({"fact": "用户住在上海"})
    assert load_role_memory_text(conn, "any_role") == ""
