"""`openai_reasoning.ReasoningAwareChatOpenAI` 的**方法体**用例。

为什么单独一个文件（2026-11-01）：这个类的存在本身就是修 bug 的（用户 10-31 报"对话界面
上看不到思考过程了"），而 `test_model_kwargs.py` 那几条只验到"**构造**出了这个子类 +
请求体带 enable_thinking"—— 捕获那半（`_convert_chunk_to_generation_chunk`）**一条语句
都没跑过**。全量门禁的「覆盖率分模块地板」因此把它判红（33% < 85%）：一个"专门用来补
被丢掉的增量"的映射，自己从没被执行过一次，那跟没修一样。

三条判据对着类文档的承诺：
  ① `delta.reasoning_content` 有值 ⇒ 收进 `additional_kwargs["reasoning_content"]`
     （消费端 `core/agent/turn.py` 只认这一个形状）；
  ② 没有 reasoning 的响应 ⇒ **逐字等于 super()**（"行为是纯增量"这条承诺只有靠
     反向断言才守得住 —— 否则谁都能改成"顺手加工一下"）；
  ③ super() 返回 None 的分支 ⇒ 不炸、照旧 None（`{"type": "content.delta"}` 那条路径
     真实存在：beta.chat.completions.stream 的块）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessageChunk

from rolecard_agent.core.agent.openai_reasoning import ReasoningAwareChatOpenAI


def _client() -> ReasoningAwareChatOpenAI:
    # 只构造、不发请求：ChatOpenAI 的构造函数不碰网络。
    return ReasoningAwareChatOpenAI(model="some/thinking-model", api_key="sk-test")  # type: ignore[call-arg]


def _convert(chunk: dict[str, Any]) -> Any:
    return _client()._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)


def test_reasoning_content_lands_in_additional_kwargs() -> None:
    """① 主路径：增量被收进消费端认的那个形状。"""
    delta = {"reasoning_content": "先想清楚边界", "content": "答"}
    out = _convert({"choices": [{"delta": delta, "index": 0, "finish_reason": None}]})
    assert out is not None
    assert out.message.additional_kwargs["reasoning_content"] == "先想清楚边界"
    # 正文没被动过：捕获是**加**一个键，不是换内容
    assert out.message.content == "答"


def test_reasoning_is_appended_across_chunks_not_overwritten_by_emptyones() -> None:
    """①′ 没有 reasoning 的那些块不该把已经收到的键抹掉，也不该凭空造一个空键。

    真实的一轮流里只有前几块带 `reasoning_content`，后面全是正文块 —— 映射如果写成
    "每次都用 delta 里的值覆盖"，这里就会变成 `additional_kwargs["reasoning_content"] = ""`
    或直接消失，而消费端读的正是这个键。
    """
    with_it = _convert({"choices": [{"delta": {"reasoning_content": "内心戏"}, "index": 0}]})
    assert with_it is not None
    assert with_it.message.additional_kwargs["reasoning_content"] == "内心戏"

    without = _convert({"choices": [{"delta": {"content": "正"}, "index": 0}]})
    assert without is not None
    assert "reasoning_content" not in without.message.additional_kwargs

    empty_delta = _convert({"choices": [{"delta": {}, "index": 0}]})
    assert empty_delta is not None
    assert "reasoning_content" not in empty_delta.message.additional_kwargs


def test_plain_response_is_byte_for_byte_what_super_returns() -> None:
    """② 不认识 `reasoning_content` 的响应 ⇒ 与父类实现完全一致（纯增量那条承诺）。

    拿父类自己跑同一个块做对照，而不是照抄一份"我以为父类会给什么"的期望值 ——
    这个类对 langchain-openai 的 chunk 内部形状有依赖（类文档已经写明升级要复核），
    这条用例正好把"复核"变成机器判据：父类行为一变，这里就红。
    """
    from langchain_openai import ChatOpenAI

    chunk = {"choices": [{"delta": {"content": "只有正文"}, "index": 0, "finish_reason": None}]}
    mine = _convert(chunk)

    base = ChatOpenAI(model="some/plain-model", api_key="sk-test")  # type: ignore[call-arg]
    theirs = base._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)

    assert mine is not None and theirs is not None
    assert mine.message.content == theirs.message.content
    assert mine.message.additional_kwargs == theirs.message.additional_kwargs
    assert "reasoning_content" not in mine.message.additional_kwargs


def test_none_from_super_is_passed_through() -> None:
    """③ 父类判成 None 的块（beta 流式的 content.delta）⇒ 原样 None，不炸。"""
    assert _convert({"type": "content.delta", "content": "x", "index": 0}) is None


def test_chunk_without_choices_does_not_crash() -> None:
    """`choices` 整个缺失（usage-only 块）也不能抛 —— 那一条流炸掉整轮就没了。"""
    usage_only = {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    out = _convert({"choices": [], "usage": usage_only})
    assert out is not None
    assert "reasoning_content" not in out.message.additional_kwargs


def test_delta_reasoning_lands_in_the_same_key() -> None:
    """`delta.reasoning` 也要收 —— 这是 OpenAI 兼容端点的**第二个形状**（2026-11-08 实测）。

    命中现场：本地网关（127.0.0.1:18899）的 nemotron 在流式里发 `{"reasoning": "The"}`，
    而本类原先只认 `reasoning_content`。实测证据（同一条流两种读法）：
      * 上游**确实在想**：非流式 `usage.completion_tokens_details.reasoning_tokens = 357`，
        流式 delta 里出现过 `reasoning` 键、累计 412 字；
      * 而 `ChatOpenAI` 与 `ReasoningAwareChatOpenAI` 跑同一条真流，两者的
        `additional_kwargs` **都是空** —— 思考到了客户端就被丢掉，界面与检查点一起没有。
    所以这不是"名单没配"那种可归因于用户配置的形状，是捕获侧少认一个键。

    收进**同一个键**（不是新键）：消费端 `turn.py` 与回放 `api/message_view.py` 都只读
    `additional_kwargs["reasoning_content"]`，多一个形状就多一处要改的消费者。
    """
    out = _convert({"choices": [{"delta": {"reasoning": "先想清楚边界"}, "index": 0}]})
    assert out is not None
    assert out.message.additional_kwargs["reasoning_content"] == "先想清楚边界"


def test_both_reasoning_shapes_do_not_double_up() -> None:
    """两个键同时出现时**只取一次**，不能拼成"同一句话两遍"。

    逐块累加靠的是 langchain 的 `merge_dicts` 对这个键做字符串拼接（实测：两块各一段
    ⇒ 合并后是两段连写）。若这里把 `reasoning_content` 与 `reasoning` 都拼进去，同一段
    思考会在面板里出现两遍、落库也是两遍 —— 而重复正文正是套话清洗要洗掉的东西。
    """
    out = _convert(
        {"choices": [{"delta": {"reasoning_content": "同一段", "reasoning": "同一段"}, "index": 0}]}
    )
    assert out is not None
    assert out.message.additional_kwargs["reasoning_content"] == "同一段"


def test_non_string_reasoning_is_not_mapped() -> None:
    """`reasoning` 不是字符串（Responses API 那一族的对象形状）⇒ **不收**，别拼坏。

    这个键的累加语义是字符串拼接。塞一个 dict/list 进去，合并时要么 TypeError 炸掉整轮，
    要么在界面上渲染成 `{'summary': ...}` 那种没人看得懂的东西。宁可不显示。
    """
    for value in ({"summary": [{"text": "x"}]}, ["一段", "两段"], 7):
        out = _convert({"choices": [{"delta": {"reasoning": value}, "index": 0}]})
        assert out is not None, value
        assert "reasoning_content" not in out.message.additional_kwargs, value
