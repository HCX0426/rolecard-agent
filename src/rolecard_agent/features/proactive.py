"""主动开口的**投递与会话上下文读法**（`ProactiveGateway`）—— 一件产品功能，宿主接线。

这一族零件（`thread_rows` / `recent_lines` / `recent_window` / `deliver` / `chat_memory`）
全是"主动开口"这件事的半边：读那条主动会话的检查点、把她说的那句落回同一条线程、在别的
线程里给她补回声。内核只要用这四下，所以 `core/bootstrap.py` 里定的是**形状**
（`ProactiveGatewayLike`）而不是这个类 —— 实例由宿主经 `build_runtime(proactive_factory=…)`
交进来，方向保持 features→core 单向（2026-10-04 审查快照"core 装了产品功能"那一格）。

四件依赖的形状值得单独说，它们是这一族全部的正确性所在：

  * **会话的唯一真相是 checkpoint**：三个读法共用 `thread_rows` 一处（不另存一份历史），
    而"取哪一截"才是各自措辞的事；读不到一律给空表 —— 少一段上下文，比不开口更不该出事。
  * **投递不跑图**：那句是角色"已经出口"的话，用 `graph.update_state` 写进检查点；跑图
    会变成替用户自言自语。
  * **拿不到写锁就这次不投**（审计 #12）：用户那一轮可能正在图上跑，两边分叉同一个父节点
    时后写会盖掉先写。不投 ≠ 丢 —— 收件箱按 `delivered_at IS NULL` 认出来，下一 tick
    补投（`R26-40` ②）。
  * **本轮主人显式传**（R28-04 的收拢，2026-10-04 审查快照"身份显式化"）：`chat_memory`
    的 `user_id` 由图从 `state["user_id"]` 现传 —— 身份随 graph state 走，这里不再自己问
    ContextVar（没传 = 直连门面/老线程，落实例主人）。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from rolecard_agent.base.observability import TraceEvent, Tracer
from rolecard_agent.base.text import text_of
from rolecard_agent.config import Settings
from rolecard_agent.core.bootstrap import GatewayContext
from rolecard_agent.core.graph import build_graph_config
from rolecard_agent.core.memory import memory_for_turn
from rolecard_agent.core.plugins import PluginService
from rolecard_agent.core.proactive_thread import (
    ensure_proactive_thread,
    proactive_thread_id,
)
from rolecard_agent.core.state import now_ts
from rolecard_agent.core.thread_locks import release_thread, try_thread_write
from rolecard_agent.core.thread_transcript import (
    CHAT_ECHO_LIMIT,
    format_recent_window,
    format_thread_lines,
    format_unreplied_lines,
    unanswered_lines,
    unreplied_lines,
)
from rolecard_agent.features.reachout import recent_reachout_lines
from rolecard_agent.roles.models import RoleCard
from rolecard_agent.storage import threads as threads_store
from rolecard_agent.storage.db import ThreadLocalConnection


@dataclass(slots=True)
class ProactiveGateway:
    """主动开口：把那句投进"该角色的主动会话"，并给调度器/记忆注入提供那条线程的上下文。

    `state` 是 Runtime 那份**共享**的可变槽位（`graph` / `effective`）：热重建换装之后这里
    读到的就是新的一份 —— 网关不再自己持一份图或配置，免得出现"新图配旧配置"的中间态。
    `identity` 收的是闭包：这台实例主人的解析只留 `Runtime.identity` 那一处。
    """

    conn: ThreadLocalConnection
    tracer: Tracer
    plugins: PluginService
    state: dict[str, Any]
    identity: Callable[[], str]

    # -- 读法（三个入口共用一份 rows）------------------------------------------

    def thread_rows(self, role_id: str) -> list[tuple[str, str]]:
        """该角色主动会话里的 `(说话人, 原文)` 序列（按时间正序，读检查点，不另存一份）。

        两个读法（`recent_lines` / `recent_window`）共用这一处：会话的唯一真相就是
        checkpoint（与桌宠面板读历史同源），而"取哪一截"是**措辞**的事、不该连带把读取
        逻辑抄两遍。读不到（图没建 / 线程不存在 / 反序列化出问题）一律给空表：少一段
        上下文，比不开口更不该出事。

        `ToolMessage` 不进"你说过的话"：工具结果是内核读到的东西，把它标成"她说过"
        等于让她以为自己对一段 JSON 说出口过。
        """
        graph = self.state.get("graph")
        if graph is None:
            return []
        rows: list[tuple[str, str]] = []
        with contextlib.suppress(Exception):
            snap = graph.get_state(
                build_graph_config(
                    proactive_thread_id(role_id, user_id=self.identity()),
                    self.state["effective"],
                )
            )
            for m in (snap.values or {}).get("messages") or []:
                if isinstance(m, ToolMessage):
                    continue
                text = text_of(m).strip()
                if text:
                    rows.append(("用户" if isinstance(m, HumanMessage) else "你", text))
        return rows

    def recent_lines(self, role_id: str, *, limit: int = 6) -> str:
        """那条主动会话里**悬着没了结**的那一截，拼成开口指令里的上下文。

        两刀互补，只看"最后说话的是谁"：
        最后说的是他 ⇒ `unanswered_lines` 非空 ⇒ "有几句你还没接"；
        最后说的是她 ⇒ `unreplied_lines` 非空 ⇒ "你说过这几句而他没回"。
        以前只有前一刀，于是后一种情形在她眼里是**空白** —— 她只会另找一个由头，
        用户读到的就是"我还没回话呢，她转头说起唱歌"（09-26）。返回空串只在线程本身
        为空时发生，那同样是正确答案。
        """
        rows = self.thread_rows(role_id)
        asked = unanswered_lines(rows)
        if asked:
            return format_thread_lines(asked, limit=limit)
        return format_unreplied_lines(unreplied_lines(rows), limit=limit)

    def recent_window(self, role_id: str, *, limit: int = 8) -> str:
        """**最近这一窗**对话原样交给「未收尾话题」的扫描用（09-26 轮 R26-03 的修法）。

        为什么不能复用 `recent_lines`：那一截只看"最后说话的人是谁"那一侧，于是
        **她接住过的话一律不在里面** —— 可这一源要找的恰恰是"说到一半没了下文"，那件事
        往往正是她接住过、只是没落地的那件。拿"还没了结"当输入，判据与素材是反的，
        实测下来扫描一次都不会发生。所以要的是"最近聊到什么"，不是"还有什么没接"。
        """
        return format_recent_window(self.thread_rows(role_id), limit=limit)

    def chat_memory(
        self, role_id: str | None, thread_id: str | None, user_id: str | None = None
    ) -> str:
        """这一轮对话她该看见什么：`memory_for_turn` 那份记忆 + 她最近**主动**说过的原话。

        为什么要补那一截（用户 2026-09-26 拍的"并进来"）：主动开口的话只落进
        `s_proactive_<uid>_<role>` 那一条线程（B2 起带身份），而控制台的「新建对话」另开一条 ——
        那条里她看不见自己刚问过什么，"我记得我提醒过你鞋带"这种话就接不上。

        在**那条主动会话里**不补：同一句话本来就在她的历史里，再抄一遍进 system 等于把
        复读喂回给模型（`nodes._scrub_own_repeats` 治的就是这个），白花 token 还添病。

        **本轮主人由调用方传**（R28-04 的收拢，"身份显式随 state 走"）：以前这三处写死
        实例主人，后来改问 ContextVar，现在由图从 `state["user_id"]` 显式传进来 ——
        第二个身份的对话读的是她自己的记忆、回声，hit_count 也记到她账上。
        没传（直连门面、老线程 state 缺这一项）落实例主人，与从前的回落语义逐字节相同。
        settings 仍取实例那份是刻意的：这里只用到 `memory_enabled`，而它是运行环境级的
        （覆盖存在 `kernel_meta` 的 `runtime:<key>`，不分人）。
        """
        owner = user_id or self.identity()
        settings: Settings = self.state["effective"]
        text = memory_for_turn(self.conn, settings, role_id, user_id=owner)
        if not role_id or thread_id == proactive_thread_id(role_id, user_id=owner):
            return text
        echo = recent_reachout_lines(self.conn, role_id, user_id=owner, limit=CHAT_ECHO_LIMIT)
        if not echo:
            return text
        return f"{text}\n\n{echo}" if text else echo

    # -- 投递 ------------------------------------------------------------------

    def deliver(self, role: RoleCard, text: str) -> str | None:
        """把角色主动说的那句落进"该角色的主动会话"，返回线程 id（图还没建 → None）。

        为什么需要这一步：主动消息原先只进 `agent_reachout`，于是"角色找我，我却回不了、
        也点不开历史"（用户 2026-09-19）。落进会话后，回复与历史都直接复用既有对话链路，
        不需要再造一套消息通道；角色下一次生成时也能在自己的历史里看到说过什么。

        写检查点用 `graph.update_state`（与上传说明、图片注入同一路数）而**不跑图**：
        这句是角色"已经出口"的话，不是让它接着想 —— 跑图会变成替用户自言自语。
        """
        thread_id = ensure_proactive_thread(
            self.conn,
            role=role,
            user_id=self.identity(),
            tool_epoch=self.plugins.tool_epoch(),
        )
        graph = self.state.get("graph")
        if graph is None:  # 还没有图（纯内核装配 / 装配失败）：收件箱那条照样有效
            return None
        # 占住这条会话的写入再改检查点（审计 #12）：用户那一轮可能正在图上跑，而
        # `update_state` 读的是"它此刻认为的最新检查点"—— 两边分叉同一个父节点时后写的盖掉
        # 先写的，用户发的那条就被吞了。拿不到锁就**这次不投递**，而不是阻塞调度线程去等
        # 一轮对话跑完（那会拖住别的角色的开口时机）。
        # **不投不等于丢**：调度器把没投成的那些按 `delivered_at IS NULL` 认出来，下一 tick
        # 补投（`R26-40` ②，`undelivered_reachouts`）。
        if not try_thread_write(thread_id, timeout=0.0):
            self.tracer.emit(
                TraceEvent(
                    event="reachout_skipped",
                    node="reachout",
                    thread_id=thread_id,
                    role_id=role.role_id,
                    detail={"why": "这条会话正在对话中"},
                )
            )
            return None
        try:
            graph.update_state(
                build_graph_config(thread_id, self.state["effective"]),
                # created_at 与内核节点那条同一口径（`additional_kwargs`）：这句是"她已经
                # 说出口"的消息，没带时间就会在回放里悬着不知排在哪一轮 —— 而她什么时候说的
                # 恰恰是要判断的东西（2026-09-22 那次"一句话回三遍"的取证只能靠 id 前缀
                # 区分主动投递与图内回复）。
                {"messages": [AIMessage(content=text, additional_kwargs={"created_at": now_ts()})]},
            )
            # 冗余计数与检查点改动同锁同批维护（镜像探针靠它免掉全量反序列化）；
            # updated_at 同步动 —— 侧栏"刚刚"与上行同步的指纹都指着它。
            threads_store.bump_message_count(self.conn, thread_id, 1)
            threads_store.touch_thread(self.conn, thread_id)
            self.conn.commit()
        finally:
            release_thread(thread_id)
        return thread_id


__all__ = ["ProactiveGateway", "build_gateway"]


def build_gateway(ctx: GatewayContext) -> ProactiveGateway:
    """宿主接线工厂：装配根交出 `GatewayContext`，这里建出网关实例。

    它是 `core.bootstrap.build_runtime(proactive_factory=…)` 的实参 —— 内核只认
    `ProactiveGatewayLike` 那个形状，不认识这个类（features→core 单向，快照那一格的
    终态）。与调度器走 `register_background` 是同一条纪律的两个方向用法。
    """
    return ProactiveGateway(
        conn=ctx.conn,
        tracer=ctx.tracer,
        plugins=ctx.plugins,
        state=ctx.state,
        identity=ctx.identity,
    )
