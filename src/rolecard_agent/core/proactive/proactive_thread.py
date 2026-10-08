"""「一个角色一条确定性的主动会话」—— 这条线程的**身份约定**。

为什么住在 core 而不是主动开口那个功能包里（2026-10-04 审查快照"core 装了产品功能"那一族）：
这条线程的 id 是**跨模块的事实面** —— 内核的主动开口投递（`core/proactive.py`）、会话时间线
（`core/proactive/timeline.py`）、接入层的收件箱跳转与桌宠落点（`api/routers/sessions.py`）都要认同一条
线。把 id 的算法放在其中任何一个消费方里，剩下几个就得反向 import 那个模块；放进 core，
方向就统一成"功能与接入层都往下认这份约定"。

**确定性而不是"首条时生成 uuid 存库"**：主动消息与它的会话是"一个角色一条对话"这一事实的
两面，用一个从 (主人, role_id) 推出来的 id，收件箱与写入侧天然指同一个地方，不必再加一列
去记"那个 id 是哪个"（也不会出现两处各存一份、改天不同步）。

身份这一维（多租户 B2）在**前缀**而不是后缀：`user_id` 可能是任意字面量，把它放中间、靠
`{prefix}{uid}_{role}` 的固定形状拼 id，从不解析回去 —— 只有"建"和"对着比"两种用法。
"""

from __future__ import annotations

from rolecard_agent.roles.models import RoleCard
from rolecard_agent.storage.db import SqlConnection
from rolecard_agent.storage.threads import ensure_thread

#: 每个角色一条固定的"主动会话"：角色开口时落进这里，用户回复走普通对话链路。
#: 线程 id 带身份（多租户 B2）：同一个 role_id 将来可以属于两个身份（role_card 主键会
#: 改成 (user_id, role_id)），不带身份就让两人的主动会话互相覆盖。
PROACTIVE_THREAD_PREFIX = "s_proactive_"


def proactive_thread_id(role_id: str, *, user_id: str) -> str:
    """该角色主动开口的会话线程 id（**确定性**：同角色同主人恒定，不做随机分配）。"""
    return f"{PROACTIVE_THREAD_PREFIX}{user_id}_{role_id}"


def proactive_thread_title(role_name: str) -> str:
    """会话列表里显示的名字 —— 一眼看出"这是角色主动找我的那条"，不是自己开的对话。"""
    return f"{role_name} · 主动找你"


def ensure_proactive_thread(
    conn: SqlConnection, *, role: RoleCard, user_id: str, tool_epoch: int
) -> str:
    """确保该角色的主动会话存在（幂等），返回线程 id。

    为什么单独一个函数：**这条线的创建只能有一处逻辑**。今天有三方需要它 —— 角色开口时投递
    （`ProactiveGateway.deliver`）、收件箱点进来（只读，不建）、桌宠面板要接着聊
    （设计稿 §7.2.1，用户从没被主动找过的角色也能先聊起来）。三处各写一条 INSERT 的话，
    `title` 或 `tool_epoch` 迟早有一处漏掉，而那条会话的行是收件箱跳转的判据。

    删了还会再长出来：用户把这条会话从列表里删掉，下一次角色开口（或桌宠上发消息）会重新
    建一行 —— 已提炼进角色记忆的事实不跟着走（那条边界有断言钉着）。
    """
    thread_id = proactive_thread_id(role.role_id, user_id=user_id)
    ensure_thread(
        conn,
        thread_id=thread_id,
        user_id=user_id,
        role_id=role.role_id,
        tool_epoch=tool_epoch,
        title=proactive_thread_title(role.role_name),
    )
    return thread_id


__all__ = [
    "PROACTIVE_THREAD_PREFIX",
    "ensure_proactive_thread",
    "proactive_thread_id",
    "proactive_thread_title",
]
