"""图执行期注入到工具层的 ContextVar（零依赖件，base 层）。

为什么住 base 而不是 `core/nodes`：检索作用域与本轮图片来源是**内核节点注入、rag 工具层读取**
的共享状态（`retriever.make_search_tool` 从 `current_knowledge_scopes` 读）。把它留在 `core`
会让 `rag` 反向 import `core` 拿 ContextVar —— 而 base 层两方都合法依赖，环就消了
（依赖方向收口，2026-10-04 轮：把横向抓取改成只依赖 base+storage）。

注入端是 `core/nodes.execute_tools`（每轮绑定当前角色的 knowledge_scopes + 最近一张图）；
读取端是 rag 的 `search_knowledge` / image_search 工具。ContextVar 不自动跨线程传播，
`_invoke_tool` 用 `copy_context().run` 提交，所以工具线程能看到这里写入的值。
"""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar

# v2.1 RAG 的作用域注入：execute_tools 在调用工具前，把**当前角色已授权的知识作用域**
# 放进这里；search_knowledge 工具在调用瞬间读取。作用域从不出现在模型的参数里 ——
# 模型不能指定检索哪个集合（US-8：角色只声明，内核掌库）。
role_knowledge_scopes_ctx: ContextVar[Sequence[str]] = ContextVar(
    "role_knowledge_scopes", default=()
)


def current_knowledge_scopes() -> Sequence[str]:
    """工具层读取：本轮角色已授权的知识作用域（execute_tools 每轮注入）。"""
    return role_knowledge_scopes_ctx.get()


# 反向图搜（image_search）的图片来源注入：与 role_knowledge_scopes_ctx 同一机制。
# execute_tools 在跑工具前把**本轮最近一张图的 data URL** 放进这里；image_search 在调用
# 瞬间读取，模型无需（也不能）把巨大的 base64 塞进工具参数里。无图 → None，工具自降级。
turn_image_ctx: ContextVar[str | None] = ContextVar("turn_image", default=None)


def current_turn_image() -> str | None:
    """工具层读取：本轮最近一张图片的 data URL（execute_tools 每轮注入；无图为 None）。"""
    return turn_image_ctx.get()
