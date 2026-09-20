"""StateGraph wiring: two nodes in a loop, one conditional edge, optional checkpointer.

The compiled graph is intentionally boring: `model -> (tools -> model)*  -> end`. Everything
interesting lives in the nodes, so the graph stays readable when someone new opens the repo.

`build_kernel` takes an already-constructed model. Tests inject a scripted fake; the app
passes `build_model(settings)`. Keeping construction out of the graph is what makes the whole
kernel testable without a running Ollama instance.
"""

# NOTE: no `from __future__ import annotations` here on purpose. LangGraph inspects the node's
# `config` parameter annotation and warns when it is a STRING (PEP 563 lazy form) instead of a
# real type object; under Python 3.13 every annotation in this file evaluates natively anyway.
from collections.abc import Callable
from functools import partial
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from rolecard_agent.config import Settings
from rolecard_agent.core.model_settings import client_style
from rolecard_agent.core.nodes import (
    ChatLike,
    KernelContext,
    call_model,
    execute_tools,
    route_after_model,
)
from rolecard_agent.core.observability import Tracer, make_tracer
from rolecard_agent.core.plugins import PluginService
from rolecard_agent.core.state import AgentState
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.service import RoleCardService

MODEL_NODE = "model"
TOOLS_NODE = "tools"

# 不配置时的步数上限（见 config.agent_max_steps 的说明）。留一个模块级常量是为了
# 让**不持有 Settings** 的调用方（脚本、测试）也能拿到同一个默认值。
DEFAULT_AGENT_MAX_STEPS = 25


def build_graph_config(
    thread_id: str,
    settings: Settings | None = None,
    *,
    agent_mode: bool = False,
) -> dict[str, Any]:
    """LangGraph 的运行配置：线程 id + **步数上限**。

    上限必须显式给：不设时 LangGraph 用默认 `recursion_limit=10007`，而本图是
    `model -> tools -> model` 的环 —— 模型只要持续返回 tool_calls（提示注入、工具反复
    报错被重试），这一轮就永远不会终止：云端后端等于数千次真实计费调用，SSE 长时间
    无响应且界面没有中断理由。`settings=None` 时用 `DEFAULT_AGENT_MAX_STEPS`。

    `agent_mode=True`（智能体模式）把上限**放大一倍**：多步自主任务需要更多次的
    "模型 → 工具 → 模型"，对话档的 25 步（≈12 轮工具）对完整任务经常不够；放大有界、
    不放开无界 —— 熔断的语义（工具循环必被截停）在两档下都成立（审查报告 P0-1 的
    配套，见 core/nodes.py 的 MAX_REPEATED_TOOL_CALLS 说明）。
    """
    limit = DEFAULT_AGENT_MAX_STEPS if settings is None else settings.agent_max_steps
    if agent_mode and limit > 0:
        limit *= 2
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if limit > 0:  # <=0 = 显式退回库默认（仅调试用）
        config["recursion_limit"] = limit
    return config


def build_kernel(
    *,
    model: ChatLike,
    registry: ToolRegistry,
    roles: RoleCardService,
    tracer: Tracer | None = None,
    settings: Settings | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    plugins: PluginService | None = None,
    model_resolver: Callable[..., ChatLike] | None = None,
    memory_provider: Callable[[str | None], str] | None = None,
    vision_probe: Callable[[str | None, str], bool | None] | None = None,
) -> Any:
    """Compile the kernel graph.

    `checkpointer` is optional but the app always passes one: without it, a conversation
    lives only as long as the process, which is exactly the failure this project exists to
    avoid.

    `plugins` is optional but the app always passes it: it is what makes "disable a domain and
    the change is visible on the very next turn" true, by feeding both `enabled_domains` and
    `tool_epoch` from the live `plugin` table rather than from graph-build time.

    `model_resolver` 是角色级路由（US-8 后半）的挂点：给定 `role_card.model_name`（后端名或
    None）返回该轮要用的模型。由宿主提供缓存与降级；不传 = 全部走默认模型。

    `memory_provider` 是跨会话记忆的读取器（每次调用实时取，**参数是本轮的角色 id**：
    给了就取该角色专属记忆、没有则回退全局），由宿主注入连接；缺省
    fail-closed（无记忆）。
    """
    ctx = KernelContext(
        model=model,
        registry=registry,
        roles=roles,
        tracer=tracer or make_tracer(settings or Settings()),
        settings=settings or Settings(),
    )
    if plugins is not None:
        # Bound methods: `ctx.enabled_domains()` / `ctx.tool_epoch()` now read the live table.
        ctx.enabled_domains = plugins.enabled_domains
        ctx.tool_epoch = plugins.tool_epoch
    ctx.model_resolver = model_resolver
    if memory_provider is not None:
        ctx.memory_provider = memory_provider
    # 视觉能力探测（P1-2）：不传 = "永远不知道" = 永远不拦（fail-open）。拦与不拦的口径
    # 写在 core/nodes._reject_unsupported_vision，这里只负责把宿主的探测器接进来。
    if vision_probe is not None:
        ctx.vision_probe = vision_probe
    # 历史预算与工具超时随宿主配置走（审查报告 H3 / M10）：内核不再无条件把全量历史塞进
    # prompt，也不再把工具执行交给"无限等待"。
    ctx.max_context_chars = ctx.settings.context_max_chars
    ctx.tool_timeout_seconds = ctx.settings.tool_timeout_seconds

    graph = StateGraph(AgentState)

    def model_node(state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:
        # A closure rather than `partial(call_model, ctx=ctx)`: LangGraph passes config as the
        # SECOND POSITIONAL argument to any node that accepts two. A partial with a bound
        # keyword would let that land in `ctx`, silently swapping the context for a config
        # dict. The RunnableConfig annotation is load-bearing too: LangGraph validates it and
        # warns if a node's config parameter is typed as anything else.
        return call_model(state, ctx=ctx, config=config)

    # LangGraph 的 `add_node` 泛型要求节点输入是 State 类型；这里的闭包刻意接
    # `dict[str, Any]`（节点只负责把 state 透传给 call_model）。运行期正确，
    # 类型变量表达不了这件事，因此显式忽略并留下理由。
    graph.add_node(MODEL_NODE, model_node)  # type: ignore[type-var]
    graph.add_node(TOOLS_NODE, partial(execute_tools, ctx=ctx))

    graph.add_edge(START, MODEL_NODE)
    graph.add_conditional_edges(MODEL_NODE, route_after_model, {TOOLS_NODE: TOOLS_NODE, "end": END})
    graph.add_edge(TOOLS_NODE, MODEL_NODE)

    return graph.compile(checkpointer=checkpointer)


def _thinking_model_list(raw: object) -> list[str]:
    """把 model_thinking_models 归一成 list[str]。字段声明是 list[str]，但运行环境覆盖经
    model_copy 绕过 pydantic 校验后运行时可能是逗号串——入参用 object 让 isinstance(str) 分支
    对 mypy 可达，两种形态都归到精确成员列表，杜绝 `x in str` 的子串误命中。"""
    if isinstance(raw, str):
        return [n.strip() for n in raw.split(",") if n.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(n).strip() for n in raw if str(n).strip()]
    return []


def _init_model(
    settings: Settings, backend_name: str | None, temperature: float | None = None
) -> ChatLike:
    """Instantiate one backend. Imported lazily so the kernel imports without a provider.

    超时是**显式带上**的：默认没有超时时，一个挂起的本地模型会让 SSE 对话与抽取无限等待
    ——不仅卡住请求，还会让 `with_fallbacks` 形同虚设（主模型既不返回也不失败，回退永远
    触发不了）。供应商目录化后 model_provider 只会是 "ollama"/"openai" 两类客户端，
    两者都接受 `timeout`，因此配置了就直接传。
    """
    from langchain.chat_models import init_chat_model

    backend = settings.backend(backend_name)
    kwargs: dict[str, Any] = {"model": backend.model}
    if backend.base_url:
        kwargs["base_url"] = backend.base_url
    if backend.api_key:
        kwargs["api_key"] = backend.api_key
    # provider 现在是**供应商 id**（ollama/siliconflow/deepseek/…），而 init_chat_model
    # 只认 "ollama"/"openai" 两类客户端 —— 厂商 → 客户端风格的映射统一走目录（client_style）。
    # 历史值 "local" 是 Ollama 的别名，同样按 native 处理。
    style = client_style(backend.provider)
    kwargs["model_provider"] = "ollama" if style == "native" else "openai"
    # 本地 Ollama 的实际上下文窗口：不显式传 num_ctx，引擎默认常只有 2048 tokens，
    # 我们裁剪到 24000 字符的历史会被静默截断（模型自带窗口形同虚设）。
    if style == "native" and backend.num_ctx:
        kwargs["num_ctx"] = backend.num_ctx
    # 思考（reasoning）模式只对**显式列出**的思考模型开启（MODEL_THINKING_MODELS），
    # 且受总开关 MODEL_THINKING=auto|off 管制（off = 名单内也不开，临时不想要思考
    # token 时用）：对不支持的模型传 reasoning=True 会直接 400（实测 qwen2.5:7b），
    # 且思考 token 会显著拉长首字延迟 —— 所以按模型名精确启用 + 总闸兜底。
    # 名单归一：字段声明是 list[str]，但运行环境覆盖路径经 model_copy 不过 pydantic 校验 →
    # 运行时可能是逗号串，直接 `x in str` 会退化成子串匹配（"qwen3" 命中 "qwen3-vl:8b"）。
    # 归一交给 _thinking_model_list（入参 object，绕开 mypy 对 isinstance(str) 的"不可达"误判）。
    thinking_models = _thinking_model_list(settings.model_thinking_models)
    if (
        style == "native"
        and settings.model_thinking != "off"
        and backend.model in thinking_models
    ):
        kwargs["reasoning"] = True
    # 超时的**传法因客户端而异**（实测，别再想当然）：
    #   - openai 兼容客户端认 `timeout` → 落到 request_timeout；
    #   - ChatOllama **不认** `timeout`，传了会被静默丢弃（实测 client_kwargs 为空、
    #     底层 httpx timeout=None）。本地模型挂起时 SSE 与 with_fallbacks 会一起永久
    #     等待，_CHAT_POOL 的 8 个线程被逐个占死 → 整个对话服务拖停。它只认 client_kwargs。
    if settings.model_timeout_seconds > 0:
        timeout = int(settings.model_timeout_seconds)
        if style == "native":
            kwargs["client_kwargs"] = {"timeout": timeout}
        else:
            kwargs["timeout"] = timeout
    # 角色卡的 temperature（构造期传入）：它对两类客户端都是**模型字段**，只能在
    # 实例化时设置。绝不能走 `.bind(temperature=...)` —— 实测 ChatOllama 会把调用期
    # kwargs 放进请求顶层而不是 `options`，Ollama 直接忽略（审查报告 P1-2）。
    if temperature is not None:
        kwargs["temperature"] = temperature
    return init_chat_model(**kwargs)


def build_model(
    settings: Settings, backend_name: str | None = None, temperature: float | None = None
) -> ChatLike:
    """Construct the chat model for a backend by name, wired to its fallback chain.

    Fallbacks matter here specifically because the primary is usually a LOCAL model: a laptop
    that is asleep, a model that was never pulled, and an Ollama that is not running all look
    identical from inside the process - a connection error mid-conversation.
    `with_fallbacks` turns that into "the cloud backend answered" instead of a dead turn.

    Two limits worth knowing before relying on it (实施计划.md §8.5):
      * When streaming, fallbacks only cover failures during *stream creation*. An error
        after the first chunk does not fall back - the caller needs its own retry affordance.
      * The chain is capped at two (`Settings.resolve_fallbacks`); longer chains make failures
        harder to localise and hide "the answer got worse after degrading".

    Not unit-tested: exercising it needs real provider packages. The part worth testing -
    which names end up in the chain and in what order - lives in `Settings.resolve_fallbacks`
    and is covered there.
    """
    primary = _init_model(settings, backend_name, temperature)
    chain = settings.resolve_fallbacks(backend_name)
    if not chain:
        return primary
    # 回退链用同一个温度：角色卡的采样参数描述的是"这个角色怎么说话"，
    # 与哪台后端接住无关（审查报告 P1-2）。
    fallbacks = [_init_model(settings, name, temperature) for name in chain]
    # `with_fallbacks` 是 Runnable 的方法，不在 `ChatLike` 这个**最小内核协议**里
    # （故意如此：测试用的假模型不该被迫实现它）。这里明确知道返回的是个可调用模型。
    return cast("ChatLike", primary.with_fallbacks(fallbacks))  # type: ignore[attr-defined]
