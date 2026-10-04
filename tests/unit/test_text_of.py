"""`base/text.py::text_of` —— 全项目唯一的消息取文本实现。  Traceability: US-4（对话渲染）, US-7。

钉的是"同一句话在各条路径上必须是同一个字符串"（架构审计报告 P1-8）：流式输出、历史回放、
结构化抽取、比对聚合、主动开口、"增强提示词"以前各有取法，其中几处把分块列表直接
字符串化 —— 用户与 guard 看到的是 Python repr。

唯一性由 `scripts/check_consistency.py` 的 `single text extractor` 断言机器校验；
这里钉语义。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk

from rolecard_agent.base.text import text_of


def test_plain_string_content_passes_through() -> None:
    assert text_of(AIMessage(content="你今天还好吗？")) == "你今天还好吗？"


def test_block_list_is_joined_with_spaces_not_repr() -> None:
    """分块列表用**空格**连接：既不是 repr，也不是旧副本那样粘连成一团。"""
    msg = AIMessage(
        content=[
            {"type": "text", "text": "结石直径"},
            {"type": "text", "text": "6.0 mm"},
        ]
    )
    out = text_of(msg)
    assert out == "结石直径 6.0 mm"
    assert "[" not in out and "'" not in out  # 数据结构的一个字符都不许漏出来


def test_non_text_blocks_and_stray_strings_are_tolerated() -> None:
    """跑在 SSE 循环与回放里：一个形状意外的块不该让整轮对话或整页渲染挂掉。

    这里不用 `HumanMessage(...)` 构造：pydantic 会先拒掉形状意外的块（那是消息层的职责），
    而本函数要兜住的是**流式/供应商返回的怪块**，所以给一个带 .content 的裸对象。
    """

    class _Odd:
        content: object = [
            "裸字符串块",
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "text"},  # 没有 text 字段的怪块
            None,
        ]

    assert text_of(_Odd()) == "裸字符串块"  # 图片块与怪块都不产出文本


def test_streaming_chunk_shape_is_handled() -> None:
    chunk = AIMessageChunk(content=[{"type": "text", "text": "部分"}])
    assert text_of(chunk) == "部分"


def test_object_without_content_falls_back_to_str() -> None:
    """入参可能是"任意对象"（调用点有 `getattr(msg, "content", msg)` 这一类）。"""

    class _Bare:
        def __str__(self) -> str:
            return "bare"

    assert text_of(_Bare()) == "bare"
    assert text_of("就是字符串") == "就是字符串"


def test_kernel_modules_reuse_the_single_implementation() -> None:
    """`core.nodes` 不再留私有副本，只有那一个函数被转发引用。

    私有副本曾从 `core.nodes` 被 api 层跨层 import，是"各自就地再写一份"的起点。
    """
    from rolecard_agent.base import text as text_module
    from rolecard_agent.core import nodes

    assert not hasattr(nodes, "_text_of")
    assert nodes.text_of is text_module.text_of
