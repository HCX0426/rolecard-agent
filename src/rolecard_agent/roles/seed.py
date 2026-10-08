"""出厂角色定义 - held in code, not in the database.

Why code rather than a SQL fixture:

  * `general_assistant`（唯一内置角色）不可删除（`role_card.is_builtin = 1`），种子在代码里
    才能让"出厂角色的改进随提交到达所有库"。
  * 域角色**不在本文件**：它们是域概念，随各域的 `DomainSpec.seed_roles` 出厂（2026-10-04
    审查快照的域机制条目把 `medical_archivist` 搬进了 `domains/health/seed.py`），由宿主
    聚合成 `domains.registry.domain_seed_roles()` 交装配根播种。类型是**自定义**
    （is_builtin = 0）—— 可编辑、可删除、重启不覆盖修改（用户 2026-09-17 反馈
    "健康档案管理员改成自定义"）。内核点名具体域的工具名与知识作用域就是概念泄漏，
    所以这份文件此后只装**与领域无关**的内置角色。

`GLOBAL_SAFETY_PROMPT` is NOT duplicated here. It lives in core/agent/prompts.py and is appended at
call time, after the role prompt, so no role - built-in or custom - can override it.

`scripts/init_db.py` / `create_app` upsert built-ins and insert domain seeds on every run;
`roles/service.py` refuses to delete any role whose `is_builtin` is set.
"""

from __future__ import annotations

from rolecard_agent.roles.models import RoleCardCreate

# The whitelist is written out explicitly even though `None` would mean "everything". Two
# reasons: it documents which capabilities this role is supposed to have, and it makes the
# permission boundary visible in the diff when someone widens it.
#
# 域角色的白名单（内核通用件 + 该域工具）同理写在**它自己域**里：`domains/<x>/seed.py`。

# 通用助手的基础内核工具（2026-09-18 用户反馈：默认角色不该是纯对话，要像市面上的
# AI agent 一样能用基础能力）。只含**与领域无关**的内核工具：联网、工作区文件读写、
# 多模型比对、部署信息。刻意不含 search_knowledge（那是"已授权知识作用域"的检索，
# 通用助手没声明作用域，配了也搜不到，还会让用户误以为它有档案知识）与任何域工具
# （需要领域能力请切对应的域角色）。
GENERAL_ASSISTANT_TOOLS = [
    "web_search",
    "web_fetch",
    "image_search",
    "fs_read",
    "fs_list",
    "fs_write",
    "compare_model_answers",
    "memory_save",
    "list_domains",
    "list_roles",
]

BUILTIN_ROLES: tuple[RoleCardCreate, ...] = (
    # 默认角色：基础内核工具（联网 / 工作区文件 / 多模型比对），不接领域工具与检索
    # 作用域（用户 2026-09-18：默认角色不该"纯对话"，要能编辑文档、查资料）。
    RoleCardCreate(
        role_id="general_assistant",
        role_name="通用助手",
        system_prompt=(
            "你是通用 AI 助手：答疑、写作、翻译、日常事务都可以正常聊。\n"
            "你已接入基础能力："
            "- 联网（web_search / web_fetch）：查实时信息、读网页正文；"
            "- 工作区文件（fs_read / fs_list / fs_write）：读写本系统工作区内的"
            "文本文件（如笔记、文档），用户说\"把 XX 存下来 / 帮我改一下\"时使用；"
            "- 多模型比对（compare_model_answers）：需要交叉确认时可用；\n"
            "- 跨会话记忆（memory_save）：用户明确说出希望长期记住的事实（称呼、偏好、"
            "身份背景等）时调用，之后所有角色都会记得。"
            "你**没有**接入健康档案、知识库检索与领域工具；如果用户想查询健康档案，"
            "请提示他切换到「健康档案管理员」角色后再问。"
        ),
        temperature=0.7,
        model_name=None,
        tool_whitelist=GENERAL_ASSISTANT_TOOLS,
        exemplars=[],
        knowledge_scopes=[],
        description=(
            "默认角色：通用对话 + 基础内核工具（联网 / 工作区文件 / 多模型比对），"
            "不接领域工具与检索。"
        ),
    ),
)
