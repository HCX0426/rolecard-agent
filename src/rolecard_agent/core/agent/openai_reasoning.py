"""OpenAI 兼容端点的思考（reasoning）捕获 —— SiliconFlow 一族的 `enable_thinking` 开关
与 `reasoning_content` / `reasoning` 两种增量映射。

为什么要子类化（2026-10-31，用户："对话界面上看不到思考过程了"）：

  * **开关**：SiliconFlow 一族的思考不是 `reasoning=True`（那是 langchain-ollama 的参数），
    而是请求体里的 `enable_thinking`。实测 DeepSeek-V4-Flash 不传它 → `reasoning_tokens: 0`、
    delta 里根本没有 `reasoning_content`；传了才有。而 `_init_model` 的思考闸门从前只对
    `style == "native"` 生效 —— OpenAI 兼容路径从不发开关，思考静默关闭。
  * **捕获**：langchain-openai 1.6.7（锁定版本）的 chunk 转换**不映射** `reasoning_content`
    —— 增量到了客户端就被丢掉。消费端（`core/agent/turn.py`）只认
    `additional_kwargs["reasoning_content"]` 这一个形状（langchain-ollama 放这里），
    所以在这里补上，形状对齐，消费端不改。

**键有两个，只认一个就会静默失灵（2026-11-08 装机实测补的第二支）**：本机那个 OpenAI 兼容
网关（nemotron 一族）流式里发的是 `delta.reasoning`，不是 `delta.reasoning_content`。实测
证据是同一条流的三种读法：非流式 `usage.completion_tokens_details.reasoning_tokens = 357`
（上游**确实在想**）、流式 delta 里出现过 `reasoning` 键累计 412 字、而 `ChatOpenAI` 与
本类跑同一条真流两者的 `additional_kwargs` **都是空** —— 思考在客户端被丢掉，界面上没有
折叠面板，落库也没有（回放读的是同一个键）。这不是"模型没在想"，也不是"名单没配"，
是这里少认一个键。

**两支都收进同一个键**，不新造形状：`turn.py` 的 thinking 事件与 `api/message_view.py`
的回放都只读 `additional_kwargs["reasoning_content"]`，多一个键名就多两处消费者要改。
两键同时出现时只取一次 —— 逐块累加靠的是 langchain `merge_dicts` 对这个键做**字符串拼接**
（实测两块各一段 ⇒ 合并后连写），都拼就等于同一段思考在面板里出现两遍。

非字符串的 `reasoning`（Responses API 那一族的对象/数组形状）**不收**：这个键的累加语义
是拼接，塞结构进去要么 TypeError 炸掉整轮，要么在界面上渲染成没人看得懂的东西。宁可不显示。

**行为是纯增量**：不认识 `reasoning_content` / `reasoning` 的响应（非思考模型/没开开关）
走 super() 原样返回；多出来的映射从前是"被丢掉"，不是"被放别处" —— 不存在移位。
版本耦合要说清：本类对 langchain-openai 的 chunk 内部形状有依赖（锁定版本见
requirements-runtime.lock），升级它之后这里要复核。

**为什么住在 agent 包而不是 models**：`core/models.model_resolver` 本来就 import
`core.agent.nodes`（拿 ChatLike 协议）—— models→agent 这条边已存在；本类的唯一消费者
是本包的模型工厂（`graph._init_model`），agent 再反向 import models 就是包级环
（依赖方向门禁当场红过一次）。跟消费者走，落在这里。
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

#: 思考增量的两个键名，**按优先级**取第一个非空者（见模块文档：同一轮里两键同时出现只算一遍）。
_REASONING_KEYS = ("reasoning_content", "reasoning")


class ReasoningAwareChatOpenAI(ChatOpenAI):
    """把 `delta.reasoning_content` / `delta.reasoning` 收进 `additional_kwargs` 的 ChatOpenAI。

    只为 OpenAI 兼容端点构造；native（Ollama）路径继续走 `init_chat_model` ——
    那边的思考增量本来就在 `additional_kwargs` 里。
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> Any:
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is None:
            return None
        delta = ((chunk.get("choices") or [{}])[0].get("delta")) or {}
        for key in _REASONING_KEYS:
            reasoning = delta.get(key)
            # 只收字符串增量：非字符串（Responses 那族的对象/数组）拼进这个键会炸或渲染成乱码。
            if isinstance(reasoning, str) and reasoning:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
                break
        return generation


__all__ = ["ReasoningAwareChatOpenAI"]
