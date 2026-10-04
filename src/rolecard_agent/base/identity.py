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

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from rolecard_agent.config import Settings
from rolecard_agent.storage.db import SqlConnection

DEFAULT_TENANT_ID = "local"
DEFAULT_USER_ID = "local-user"

#: 这一轮"在为谁读" —— 由图节点在入口绑上（`core/nodes`），退出时复位。
_BOUND_USER: ContextVar[str | None] = ContextVar("rolecard_bound_user", default=None)

#: 「没绑过就回落」的**哨兵钩子**（住在 ContextVar 里 = 天然按执行上下文隔离）。默认
#: `None` = 不记，生产热路径零额外开销。测试装一个收集器（`capturing_identity_fallback`），
#: 就能把"这条路径到底有没有悄悄回落"变成**能断言**的事实（2026-10-04 审查快照
#: "身份显式化"那一格的"fallback 加哨兵"）。
#: 为什么是 ContextVar 而不是模块全局：全局会被同一进程里并发的另一条线程污染计数，而
#: `bound_user` 本身就是 ContextVar —— 钩子要跟它同一套隔离语义。
#: 为什么这里**不**再打一条日志：回落在生产里是**常态而不是异常**（后台调度器每个 tick 替
#: 实例主人冒话都走这一支，10-05 实测一轮 18 次触发全是这一类或测试自证），给它配日志等于
#: 每 30 秒刷一行；而"三条日志通道并存"是另一格（§5 第 18 条）正在收口的病 —— 这一格只加
#: 一个**测试可注入的钩子**，不新增一处日志出口。真要在生产里查回落，那个出口归收口那格定。
_FALLBACK_HOOK: ContextVar[Callable[[str], None] | None] = ContextVar(
    "rolecard_identity_fallback_hook", default=None
)


def active_user_id(fallback: str) -> str:
    """这一轮绑过的主人；没绑过就是 `fallback`（= 这台实例的主人）。"""
    bound = _BOUND_USER.get()
    if bound:
        return bound
    hook = _FALLBACK_HOOK.get()
    if hook is not None:
        hook(fallback)
    return fallback


@contextmanager
def capturing_identity_fallback() -> Iterator[list[str]]:
    """在这个块里每一次"未绑定→回落"都收进返回列表（供测试断言"这条路径身份是显式的"）。

    用法::

        with capturing_identity_fallback() as fell:
            ...  # 跑一条应当全程绑定了主人的同步路径
        assert fell == []        # 零回落 = 这条路径不靠静默兜底

    收的是 `fallback` 值而不是纯计数：测试要能说"回落到了谁"，而不只是"回落了几次"。
    复位在 `finally`：一次异常不许把这个上下文里的钩子留成脏值（与 `bound_user` 同纪律）。

    它能穿透线程池不是巧合：`core/nodes.execute_tools` 提交工具时走
    `contextvars.copy_context().run(...)`（`R102-03` 那条修复），复制的是**整个上下文**，
    所以这条钩子与 `bound_user` 一起被带进池线程 —— 一条没绑主人的工具调用照样会被抓到。
    """
    collected: list[str] = []
    token = _FALLBACK_HOOK.set(collected.append)
    try:
        yield collected
    finally:
        _FALLBACK_HOOK.reset(token)


@contextmanager
def bound_user(user_id: str | None) -> Iterator[None]:
    """把这一轮的主人绑到当前执行上下文里（图节点用）。

    为什么需要它：域工具的 `current_user` 是**装配时**定下的零参闭包（工具对模型必须看起来
    零参数，否则模型就能自己填"我是谁"），它拿不到 `state`。这一层让节点在入口替它回答，
    而工具的签名一个字都不用改。

    为什么是 `ContextVar` 而不是进程全局：同一台实例上两条线程可以属于两个主人，
    进程级会串。复位放在 `finally`，否则一次异常就把这个线程的主人留成了脏值。
    """
    token = _BOUND_USER.set(user_id or None)
    try:
        yield
    finally:
        _BOUND_USER.reset(token)


def resolve_instance_identity(settings: Settings) -> str:
    """**这台实例的主人是谁** —— 后台那条链（主动开口、图里的域工具）唯一能问的"谁"。

    为什么需要"实例级"这一层，而不是全都按请求解析：定下来的形态是**两份完整数据集、
    一台实例一个主人**（架构总览 §4.1，用户 09-27 拍的"默认用本机那份 + 可切云端那份"）。
    后台调度器没有"这次请求"可问 —— 它替她冒一句话时，服务对象是**这台机器的属主**，
    不是某个正连着的浏览器。所以身份有两层：

    - **实例级**（这个函数）：装配时从 `IDENTITY_USER_ID` 读，空 = `DEFAULT_USER_ID`。
      改它要重启：按 §4.1，"换主人"就是换一份数据集，不是热切一个过滤器。
    - **请求级**（`AppContext.current_user()`）：认证到的用户名在 `app_user` 里**确实有行**
      时才是另一个人，否则落回实例级这一层。

    这条分层也说明了为什么它必须排在"按身份过滤那 15 张表"前面：**一条读路径若不知道自己在
    为谁读，补上的那一列就只是假安慰** —— 看起来隔离了，实际谁都在读同一份。
    """
    return (settings.identity_user_id or "").strip() or DEFAULT_USER_ID


def resolve_identity(conn: SqlConnection, actor_id: str | None, *, fallback: str) -> str:
    """把"这次请求认证到的那个凭证"落到一张 `app_user` 行上；落不上就是这台实例那份。

    **今天它几乎总是回 `fallback`，而这不是摆设**。`AUTH_CREDENTIALS` 里的用户名是运维随手
    起的（`admin` 之类），压根不打算当 `user_id` 用；而 `app_user` 只有播种的那一行。这条判据
    要买的东西是：**"身份从哪来"从此只有这两处回答**（实例级 + 请求级），给那 15 张表补归属列、
    真出现第二个账号的那天，改这里就够了，不用回去动接入层那十几个调用点。

    认出来才算：只有 `actor_id` 在 `app_user` 里**确实有一行**时才算换到了另一个身份 ——
    否则一个陌生用户名会凭空造出一个身份，它的会话写进库里、却没有任何地方认识这个人。
    """
    if not actor_id:
        return fallback
    row = conn.execute("SELECT user_id FROM app_user WHERE user_id = ?", (actor_id,)).fetchone()
    return str(row[0]) if row is not None else fallback


def ensure_identity_row(conn: SqlConnection, user_id: str = DEFAULT_USER_ID) -> None:
    """让 `app_user` 里有这个人这一行 —— `session_thread.user_id` 的外键要有对象可指。

    为什么**实例主人**也要走这里，而不只是演示身份那一份：`IDENTITY_USER_ID` 指到第二个人
    （`alice`）时，这台实例的每条会话、每个摄取任务、每条域记录都写着 `alice`，而 `app_user`
    里只有播种的 `local-user` —— 外键当场断。2026-09-27 那支两实例端到端探针第一次真的把
    `IDENTITY_USER_ID` 设成第二个人，症状是 `POST /api/session` 直接 500 IntegrityError，
    不是同步才有的边角：**那台实例压根开不了新会话**。

    这与 `resolve_identity` 那句"陌生用户名不许凭空造身份"不矛盾，两件事分得很清：
      * 这里造身份的凭据是**运维改环境变量并重启**（`IDENTITY_USER_ID` 就是那句"这台实例
        归他"），不是任何一个敲对口令的浏览器；
      * 请求级那条仍然只在"确实有行"时才认，所以多出来的这一行不会让谁登录一下就换个身份。

    `INSERT OR IGNORE`：重复 bootstrap 不许复活任何东西，同一枚名字传两次也只是第二次没做事。
    """
    display = "本地用户" if user_id == DEFAULT_USER_ID else user_id
    conn.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES (?, '本地演示')",
        (DEFAULT_TENANT_ID,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) VALUES (?, ?, ?)",
        (user_id, DEFAULT_TENANT_ID, display),
    )
    conn.commit()
