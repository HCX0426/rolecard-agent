"""v1 单用户身份：常量与首启播种（内核概念，不属于任何域）。

schema 里每张表都带 `user_id` 列（多用户的接缝早就留好了），而 v1 有意不做登录页 ——
所以运行时有且只有一个共享身份。把它放在内核一侧是因为**两层都需要它**：装配根播种
这两行（`session_thread.user_id` 有外键指向它），域工具在执行时按 `current_user` 现取
同一个 id。

曾经它在 `api/deps.py` 与 `api/main.py` 各定义一份（同为 `"local-user"`，靠巧合保持一致）
—— 两处定义同一个事实，就是两个事实面（架构审计报告 §5 的冗余族）。
"""

from __future__ import annotations

from rolecard_agent.storage.db import SqlConnection

DEFAULT_TENANT_ID = "local"
DEFAULT_USER_ID = "local-user"


def seed_demo_identity(conn: SqlConnection) -> None:
    """播种 v1 演示身份。INSERT OR IGNORE：重复 bootstrap 不许复活任何东西，
    而 `session_thread.user_id` 的外键需要这一行存在。"""
    conn.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES (?, '本地演示')",
        (DEFAULT_TENANT_ID,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "VALUES (?, ?, '本地用户')",
        (DEFAULT_USER_ID, DEFAULT_TENANT_ID),
    )
    conn.commit()
