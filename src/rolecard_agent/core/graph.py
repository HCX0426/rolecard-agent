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
from rolecard_agent.core.nodes import (
    ChatLike,
    KernelContext,
    call_model,
    execute_tools,
    route_after_model,
)
from rolecard_agent.core.observability import Tracer, make_tracer
from rolecard_agent.core.model_settings import client_style
from rolecard_agent.core.plugins import PluginService
from rolecard_agent.core.state import AgentState
from rolecard_agent.core.tools.registry import ToolRegistry
from rolecard_agent.roles.service import RoleCardService

MODEL_NODE = "model"
TOOLS_NODE = "tools"


def build_kernel(
    *,
    model: ChatLike,
    registry: ToolRegistry,
    roles: RoleCardService,
    tracer: Tracer | None = None,
    settings: Settings | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    plugins: PluginService | None = None,
    model_resolver: Callable[[str | None], ChatLike] | None = None,
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

    graph = StateGraph(AgentState)

    def model_node(state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:
        # A closure rather than `partial(call_model, ctx=ctx)`: LangGraph passes config as the
        # SECOND POSITIONAL argument to any node that accepts two. A partial with a bound
        # keyword would let that land in `ctx`, silently swapping the context for a config
        # dict. The RunnableConfig annotation is load-bearing too: LangGraph validates it and
        # warns if a node's config parameter is typed as anything else.
        return call_model(state, ctx=ctx, config=config)

    graph.add_node(MODEL_NODE, model_node)
    graph.add_node(TOOLS_NODE, partial(execute_tools, ctx=ctx))

    graph.add_edge(START, MODEL_NODE)
    graph.add_conditional_edges(MODEL_NODE, route_after_model, {TOOLS_NODE: TOOLS_NODE, "end": END})
    graph.add_edge(TOOLS_NODE, MODEL_NODE)

    return graph.compile(checkpointer=checkpointer)


def _init_model(settings: Settings, backend_name: str | None) -> ChatLike:
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
    # 两类客户端都接受 `timeout`（此前的 _TIMEOUT_PROVIDERS 两者都在列）：本地模型挂起
    # 会让 SSE 与 with_fallbacks 永久等待，所以只要配置了超时就显式带上。
    if settings.model_timeout_seconds > 0:
        kwargs["timeout"] = int(settings.model_timeout_seconds)
    return init_chat_model(**kwargs)


def build_model(settings: Settings, backend_name: str | None = None) -> ChatLike:
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
    primary = _init_model(settings, backend_name)
    chain = settings.resolve_fallbacks(backend_name)
    if not chain:
        return primary
    fallbacks = [_init_model(settings, name) for name in chain]
    return cast("ChatLike", primary.with_fallbacks(fallbacks))
