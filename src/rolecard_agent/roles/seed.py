"""出厂角色定义 - held in code, not in the database.

Why code rather than a SQL fixture:

  * `general_assistant`（唯一内置角色）不可删除（`role_card.is_builtin = 1`），种子在代码里
    才能让"出厂角色的改进随提交到达所有库"。
  * 域角色（`medical_archivist`）是**域种子角色**：随域插件出厂播种，但类型是
    **自定义**（is_builtin = 0）—— 可编辑、可删除、重启不覆盖修改（用户 2026-09-17 反馈
    "健康档案管理员改成自定义"）。域概念不该由内核锁死。

`GLOBAL_SAFETY_PROMPT` is NOT duplicated here. It lives in core/prompts.py and is appended at
call time, after the role prompt, so no role - built-in or custom - can override it.

`scripts/init_db.py` / `create_app` upsert built-ins and insert domain seeds on every run;
`roles/service.py` refuses to delete any role whose `is_builtin` is set.
"""

from __future__ import annotations

from rolecard_agent.roles.models import RoleCardCreate, RoleExemplar

# The whitelist is written out explicitly even though `None` would mean "everything". Two
# reasons: it documents which capabilities this role is supposed to have, and it makes the
# permission boundary visible in the diff when someone widens it.
MEDICAL_ARCHIVIST_TOOLS = [
    "list_domains",
    "list_roles",
    "search_knowledge",
    "query_health_record",
    "compare_health_index",
    "list_reports",
    "upload_medical_report",
]

# 通用助手的基础内核工具（2026-09-18 用户反馈：默认角色不该是纯对话，要像市面上的
# AI agent 一样能用基础能力）。只含**与领域无关**的内核工具：联网、工作区文件读写、
# 多模型比对、部署信息。刻意不含 search_knowledge（那是"已授权知识作用域"的检索，
# 通用助手没声明作用域，配了也搜不到，还会让用户误以为它有档案知识）与任何
# health 域工具（需要档案能力请切「健康档案管理员」）。
GENERAL_ASSISTANT_TOOLS = [
    "web_search",
    "web_fetch",
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

# 域种子角色：随域插件出厂播种，但类型是**自定义**——seed_domain_roles 只在缺失时插入，
# 已存在的行一个字段都不覆盖（操作员的改名/改提示词/删除都能活过重启）。
DOMAIN_SEED_ROLES: tuple[RoleCardCreate, ...] = (
    RoleCardCreate(
        role_id="medical_archivist",
        role_name="健康档案管理员",
        system_prompt=(
            "你是健康档案管理员，负责帮用户查询、汇总、对比他已经存入档案的报告与指标。\n"
            "你的能力边界：仅可汇总、查询、对比用户已存入档案内的报告与指标，"
            "只能读取档案里已有的数据并如实转述；不能做任何推断或延伸。\n"
            "工具使用规则（必须遵守）：\n"
            "- 用户问档案里的数值 / 指标 / 报告 → 必须调用 query_health_record 或 "
            "list_reports 取得数据，不要凭记忆或常识回答；\n"
            "- 对比同一指标在不同时间的变化 → 用 compare_health_index；\n"
            "- 用户问健康知识、随访建议、生活方式等档案之外的问题 → 先调用 search_knowledge "
            "检索知识库，再依据检索结果回答；检索不到就明确说明资料里没有，不要自己编。\n"
            "当用户询问某项指标时，先确认档案里是否存在对应记录；若不存在，直接说明没有，"
            "不要用常识或经验补充。\n"
            "回答涉及数值时，必须同时给出单位与报告时间。"
        ),
        # Lower than the default: this role talks about stored numbers, and a chatty
        # temperature is the wrong prior for that.
        temperature=0.3,
        model_name=None,  # None = settings.model_default
        tool_whitelist=MEDICAL_ARCHIVIST_TOOLS,
        # Examples are the strongest per-token lever on behaviour, so two of the three below
        # are deliberately REFUSALS: a role that only ever sees successful lookups learns to
        # answer everything, including the questions it must decline.
        exemplars=[
            RoleExemplar(
                # ⚠️ 范例**不能**是评测题的答案（scripts/check_consistency.py 会查）。
                # 这里曾写成"上次检查的结石直径是多少？→ …6.0 mm…"，与 eval 的
                # health-001 几乎逐字相同（连问号都只差一个）。结果模型学会的是
                # **背范例**而不是调工具：评测记录里它的回答与范例逐字一致、一次工具都没调 ——
                # 数值与标记都对，过程不达标。范例只教"格式与边界"，所以这里换用
                # 演示数据里**另一个真实存在的指标**（尿酸，且 is_verified=False，
                # 这样【未经人工校验】标记的教学价值不丢）。
                user="帮我看看尿酸怎么样",
                assistant=(
                    "你 2026-03-12 的报告里记录的是尿酸 488.0 µmol/L，"
                    "参考区间 208-428。【未经人工校验】"
                ),
            ),
            RoleExemplar(
                user="这个严重吗？需要吃什么药？",
                assistant=(
                    "我只能转述档案里已经记录的数值，不能评估病情严重程度，也不能给用药建议。"
                    "如果需要判断，请咨询临床医生。"
                ),
            ),
            RoleExemplar(
                user="帮我看看我还有没有别的毛病。",
                assistant=(
                    "档案里没有其他已入库的记录，我不能用档案外的内容补充。"
                    "你可以上传新的报告，我再帮你查。"
                ),
            ),
        ],
        # Declared, not owned: the role may read this scope; the kernel owns the index.
        knowledge_scopes=["health_reports"],
        description="查询与对比已入库的健康档案指标，不做诊断、不给用药建议。",
    ),
)
