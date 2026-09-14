"""Built-in role definitions - held in code, not in the database.

Why code rather than a SQL fixture:

  * `medical_archivist` is undeletable (`role_card.is_builtin = 1`) and its tool whitelist
    is a security boundary. Seed data in the DB would be editable by anything that can write
    to the DB, including a future admin endpoint.
  * The set of built-in roles should change with a commit, not with a row update.

`GLOBAL_SAFETY_PROMPT` is NOT duplicated here. It lives in core/prompts.py and is appended at
call time, after the role prompt, so no role - built-in or custom - can override it.

`scripts/init_db.py` upserts these on every run, and `roles/service.py` refuses to delete any
role whose `is_builtin` is set.
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

BUILTIN_ROLES: tuple[RoleCardCreate, ...] = (
    RoleCardCreate(
        role_id="medical_archivist",
        role_name="健康档案管理员",
        system_prompt=(
            "你是健康档案管理员，负责帮用户查询、汇总、对比他已经存入档案的报告与指标。\n"
            "你的能力边界：只能读取档案里已有的数据，并如实转述；不能做任何推断或延伸。\n"
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
                user="上次检查的结石直径是多少？",
                assistant=(
                    "你 2026-03-12 的报告里记录的是 6.0 mm，参考区间 0-5 mm。【未经人工校验】"
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
