"""Built-in role definitions - held in code, not in the database.

Why code rather than a SQL fixture:

  * `medical_archivist` is undeletable (`role_card.is_builtin = 1`) and its tool whitelist
    is a security boundary. Seed data in the DB would be editable by anything that can
    write to the DB, including a future admin endpoint.
  * The set of built-in roles should change with a commit, not with a row update.

`GLOBAL_SAFETY_PROMPT` is NOT duplicated here. It lives in core/prompts.py and is appended
at call time, after the role prompt, so no role - built-in or custom - can override it.

Filled in during M2. Shape:

    BUILTIN_ROLES: tuple[RoleCardCreate, ...] = (
        RoleCardCreate(
            role_id="medical_archivist",
            role_name="健康档案管理员",
            system_prompt="...",
            temperature=0.3,
            model_name=None,          # None = use config.MODEL_DEFAULT
            tool_whitelist=None,      # None = every tool of enabled plugins
            description="...",
            is_builtin=True,
        ),
    )

`scripts/init_db.py` upserts these on every run, and `roles/service.py` refuses to delete
any role whose `is_builtin` is set.
"""
