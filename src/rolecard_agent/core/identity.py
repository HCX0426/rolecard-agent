"""v1 单用户身份：常量、首启播种，与**"这次请求是谁"的唯一一处回答**。

先订正这段注释自己从前说的话：它以前写"schema 里每张表都带 `user_id` 列（多用户的接缝
早就留好了）"—— **那是假的**。2026-09-27 数过：本仓 SQL 声明的 21 张表里只有 4 张带
`user_id`（会话线程、摄取任务、域记录，再加健康域那张报告表 —— 域表名按各域自己声明，
内核不复述，见架构总览 §5 第 1 条），其余 15 张压根没有归属维度（角色卡、两张记忆表、
收件箱、主动状态、插件、审计、用量…）。所以"多人身份隔离"不是接一根线，一半是
**迁移**（见架构总览 §4.1 与本轮台账 `R26-28`）。

它在内核一侧是因为**两层都需要它**：装配根播种这两行（`session_thread.user_id` 有外键指向
它），域工具在执行时按 `current_user` 现取同一个 id。

曾经它在 `api/deps.py` 与 `api/main.py` 各定义一份（同为 `"local-user"`，靠巧合保持一致）
—— 两处定义同一个事实，就是两个事实面（架构审计报告 §5 的冗余族）。
"""

from __future__ import annotations

from rolecard_agent.storage.db import SqlConnection

DEFAULT_TENANT_ID = "local"
DEFAULT_USER_ID = "local-user"


def resolve_identity(conn: SqlConnection, actor_id: str | None) -> str:
    """把"这次请求认证到的那个凭证"落到一张 `app_user` 行上；落不上就是本机那份。

    **今天它几乎总是回 `DEFAULT_USER_ID`，而这不是摆设**。`AUTH_CREDENTIALS` 里的用户名是
    运维随手起的（`admin` 之类），压根不打算当 `user_id` 用；而 `app_user` 只有播种的那一行。
    这条判据要买的东西是：**"身份从哪来"从此只有一处回答**。给那 15 张表补归属列、真出现
    第二个账号的那天，改这一个函数就够了，不用回去动接入层那十几个调用点。

    认出来才算：只有 `actor_id` 在 `app_user` 里**确实有一行**时才算换到了另一个身份 ——
    否则一个陌生用户名会凭空造出一个身份，它的会话写进库里、却没有任何地方认识这个人。
    """
    if not actor_id:
        return DEFAULT_USER_ID
    row = conn.execute("SELECT user_id FROM app_user WHERE user_id = ?", (actor_id,)).fetchone()
    return str(row[0]) if row is not None else DEFAULT_USER_ID


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
