"""StateGraph wiring: two nodes in a loop, one conditional edge, optional checkpointer.

The compiled graph is intentionally boring: `model -> (tools -> model)*  -> end`. Everything
interesting lives in the nodes, so the graph stays readable when someone new opens the repo.

`build_kernel` takes an already-constructed model. Tests inject a scripted fake; the app
passes `build_model(settings)`. Keeping construction out of the graph is what makes the whole
kernel testable without a running Ollama instance.
"""

from __future__ import annotations

from functools import partial
from typing import Any

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
) -> Any:
    """Compile the kernel graph.

    `checkpointer` is optional but the app always passes one: without it, a conversation
    lives only as long as the process, which is exactly the failure this project exists to
    avoid.
    """
    ctx = KernelContext(
        model=model,
        registry=registry,
        roles=roles,
        tracer=tracer or make_tracer(settings or Settings()),
        settings=settings or Settings(),
    )

    graph = StateGraph(AgentState)
    graph.add_node(MODEL_NODE, partial(call_model, ctx=ctx))
    graph.add_node(TOOLS_NODE, partial(execute_tools, ctx=ctx))

    graph.add_edge(START, MODEL_NODE)
    graph.add_conditional_edges(MODEL_NODE, route_after_model, {TOOLS_NODE: TOOLS_NODE, "end": END})
    graph.add_edge(TOOLS_NODE, MODEL_NODE)

    return graph.compile(checkpointer=checkpointer)


def build_model(settings: Settings, backend_name: str | None = None) -> ChatLike:
    """Construct the chat model for a backend by name.

    Not called by any test: tests inject a fake. Imported lazily so that importing the kernel
    does not require a provider package to be installed.
    """
    from langchain.chat_models import init_chat_model

    backend = settings.backend(backend_name)
    kwargs: dict[str, Any] = {"model": backend.model, "model_provider": backend.provider}
    if backend.base_url:
        kwargs["base_url"] = backend.base_url
    if backend.api_key:
        kwargs["api_key"] = backend.api_key
    return init_chat_model(**kwargs)
