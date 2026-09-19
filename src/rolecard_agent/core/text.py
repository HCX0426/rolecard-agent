"""消息 → 纯文本的**唯一**实现（架构审计报告 P1-8）。

为什么单独立一个模块而不是留在 `core/nodes.py` 里：它是全项目最底层的取值原语，
被内核（节点、guard、比对、主动开口）、域（抽取）与 api（SSE 帧化、历史回放）三层同时
消费。放在 `nodes.py` 里的实际后果是两处偏离：

  * `api/chat.py` / `api/deps.py` 只能 `from core.nodes import _text_of` —— 跨层 import 一个
    下划线私有函数；
  * 于是有人就地复刻：域的抽取模块抄了第二份（用空串拼接而非空格），另外四处干脆
    `str(msg.content)` 裸取 —— 而多模态/流式的 `content` 是**分块列表**，
    `str()` 得到的是 Python repr（`[{'type': 'text', ...}]`）。那份 repr 会进 guard、
    进用户收件箱、进"增强提示词"的输入框（审查报告 P1-8）。

对分块内容用空格连接：可读性优先。入参是 `Any` 而不是 `BaseMessage`：调用点既有真实消息
对象，也有 `getattr(msg, "content", msg)` 这类"可能是任意对象"的场景；宽容处理异常形状
的块，是因为它跑在 SSE 循环与历史回放里，一个怪块不该让整轮对话或整页渲染挂掉。

唯一性由 `scripts/check_consistency.py` 的 `single text extractor` 断言机器校验。
"""

from __future__ import annotations

from typing import Any


def text_of(message: Any) -> str:
    """把消息压成纯文本。`content` 可能是 str，也可能是分块列表（多模态 / 流式形态）。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return " ".join(parts)
    return str(content)
