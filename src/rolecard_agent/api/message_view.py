"""消息回放与轮次展开的 **DTO**（checkpoint 消息 → 前端形状；选中 id → 整轮 id）。

为什么单独一格、不再挤在 `api/deps.py`（2026-10-04 审查快照"deps 混入 120 行消息序列化
DTO"那一格）：`deps.py` 的本职是**请求上下文与依赖注入**（`AppContext` / `get_thread` /
`get_actor` …），而这一百来行序列化与它一个字都不相干 —— 既不读 request，也不该跟着
"依赖注入"一起被翻。同住一个文件的代价是每次改回放形状都要在上下文里翻，以及
`deps.py` 的行预算（`report_line_budget`）被无关内容顶满。搬出来之后两边各自只有一个
动词：这里管**形状**，`deps` 管**注入**。

搬家不改行为：序列化的契约由 `tests/unit/test_serialize_message.py` 整支钉着，调用方
（`api/routers/sessions.py`）与测试只换 import 来源。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from rolecard_agent.base.text import text_of


def serialize_message(
    message: object, call_args: dict[str, dict[str, Any]] | None = None
) -> dict[str, object]:
    """Checkpoint message -> JSON shape for the frontend history replay.

    `id` 是前端**编辑 / 删除某条消息**时的寻址依据：LangGraph 的 `add_messages`
    按 id 去重与删除（`RemoveMessage(id=...)`），没有 id 就无法精确改动历史中的一条。
    `call_args`（tool_call_id → 入参）由调用方从消息序列里预先配对 —— 工具行回放时
    才能显示"搜了什么"（单条 ToolMessage 自己看不到入参）。
    """
    created_at = (getattr(message, "additional_kwargs", None) or {}).get("created_at")
    row: dict[str, object]
    if isinstance(message, HumanMessage):
        row = {
            "role": "user",
            "content": text_of(message),
            "id": message.id,
        }
        # 多模态传图（2026-09-18）：content 是 text + image_url 块时，把图透出给前端
        # 回放（用户气泡显示小图 + 点击放大）。只有文本时无此键。
        image = _image_of(message)
        if image:
            row["image"] = image
    elif isinstance(message, ToolMessage):
        trow: dict[str, object] = {
            "role": "tool",
            "name": message.name,
            "content": text_of(message),
            "id": message.id,
        }
        if args := (call_args or {}).get(message.tool_call_id):
            trow["args"] = args  # 历史工具卡显示"搜了什么"（用户 2026-09-17 反馈）
        if created_at:
            trow["ts"] = str(created_at)
        return trow
    elif isinstance(message, AIMessage):
        tools = [tc.get("name") for tc in (message.tool_calls or [])]
        row = {
            "role": "assistant",
            "content": text_of(message),
            "tools": tools,
            "id": message.id,
        }
        # 思考内容随消息一起回放。为什么不只在前台的 live 气泡里显示：一轮结束后前端会以
        # checkpoint 回放**整体替换**消息区（乐观气泡连同思考一起被销毁），用户就再也看不到
        # 推理过程了。把 reasoning 放进回放数据，思考过程才和回答一样是历史的一部分。
        reasoning = (message.additional_kwargs or {}).get("reasoning_content")
        if reasoning:
            row["reasoning"] = reasoning
        # "这一轮是被叫停的"随消息一起回放（R26-13 尾）：刷新前的提示挂在界面 state 上，
        # 一刷新就没了 —— 写进检查点的才是历史的一部分。缺键 = 正常收尾，旧消息一律没有。
        if (message.additional_kwargs or {}).get("stopped"):
            row["stopped"] = True
    else:
        row = {
            "role": "assistant",
            "content": text_of(message),
            "id": getattr(message, "id", None),
        }
    if created_at:
        row["ts"] = str(created_at)  # 旧消息没有该字段 → 不显示时间
    return row


def _image_of(message: object) -> str | None:
    """从多模态 content 块里取图片 data URL（有图才返回，否则 None）。

    与 `text_of` 同哲学：宽容处理形状意外的块 —— 回放循环里一个怪块不该让整页渲染挂掉。
    """
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return None
    for part in content:
        if not isinstance(part, dict):
            continue
        url = None
        iu = part.get("image_url")
        if isinstance(iu, str):
            url = iu
        elif isinstance(iu, dict) and isinstance(iu.get("url"), str):
            url = iu["url"]
        if url:
            return url
    return None


def group_turns(messages: Sequence[object]) -> list[list[int]]:
    """把消息序列切成"一轮问答"：`[用户消息, (工具消息…), 助手回答(可无)]`。

    为什么要有这个函数：删除一条消息时，只删用户消息会留下孤立的助手回答，只删助手回答
    会留下没有答案的提问，而**工具消息与发起它的 AI 消息必须同生共死**（切断配对会被
    供应商判为非法序列）。所以"选中一条 = 选中整轮"由后端统一执行，前端只传 id。

    规则：每遇到一条用户消息就开启新一轮；首条不是用户消息时（历史被删过），
    它自成一轮，保证删除不会漏掉孤儿消息。
    """
    turns: list[list[int]] = []
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage) or not turns:
            turns.append([index])
        else:
            turns[-1].append(index)
    return turns


def expand_to_turns(messages: Sequence[object], ids: Sequence[str]) -> list[str]:
    """把"用户选中的若干 id"扩展为**整轮的 id 集合**（含配对的助手回答与工具消息）。"""
    wanted = set(ids)
    turns = group_turns(messages)
    out: list[str] = []
    for turn in turns:
        indices = {getattr(messages[i], "id", None) for i in turn}
        if indices & wanted:
            out.extend(str(i) for i in indices if i is not None)
    # 保持原有顺序，便于按序删除
    order = {getattr(m, "id", None): n for n, m in enumerate(messages)}
    return sorted(set(out), key=lambda i: order.get(i, 0))
