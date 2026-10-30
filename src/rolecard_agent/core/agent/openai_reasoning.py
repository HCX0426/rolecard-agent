"""OpenAI 兼容端点的思考（reasoning）捕获 —— SiliconFlow 一族的 `enable_thinking` 开关
与 `reasoning_content` 增量映射。

为什么要子类化（2026-10-31，用户："对话界面上看不到思考过程了"）：

  * **开关**：SiliconFlow 一族的思考不是 `reasoning=True`（那是 langchain-ollama 的参数），
    而是请求体里的 `enable_thinking`。实测 DeepSeek-V4-Flash 不传它 → `reasoning_tokens: 0`、
    delta 里根本没有 `reasoning_content`；传了才有。而 `_init_model` 的思考闸门从前只对
    `style == "native"` 生效 —— OpenAI 兼容路径从不发开关，思考静默关闭。
  * **捕获**：langchain-openai 1.6.7（锁定版本）的 chunk 转换**不映射** `reasoning_content`
    —— 增量到了客户端就被丢掉。消费端（`core/agent/turn.py`）只认
    `additional_kwargs["reasoning_content"]` 这一个形状（langchain-ollama 放这里），
    所以在这里补上，形状对齐，消费端不改。

**行为是纯增量**：不认识 `reasoning_content` 的响应（非思考模型/没开开关）走 super()
原样返回；多出来的映射从前是"被丢掉"，不是"被放别处" —— 不存在移位。
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


class ReasoningAwareChatOpenAI(ChatOpenAI):
    """把 `delta.reasoning_content` 收进 `additional_kwargs` 的 ChatOpenAI。

    只为 OpenAI 兼容端点（SiliconFlow 一族）构造；native（Ollama）路径继续走
    `init_chat_model` —— 那边的思考增量本来就在 `additional_kwargs` 里。
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
        reasoning = delta.get("reasoning_content")
        if reasoning:
            generation.message.additional_kwargs["reasoning_content"] = reasoning
        return generation


__all__ = ["ReasoningAwareChatOpenAI"]
