"""静默策略：抑制层三道判据的唯一判定点（`R102-59` 从 `core/reachout.py` 拆出的四模块之一）。

间隔（退避×抖动）、静默时段、未读堆积 —— 三道的判据由 `quiet_gate` **一处**算出，并一次
带出全部读数：原因、下一次的时刻、退避与未读的计数。分成两处迟早对不上，而界面那句话的
全部意义是让人信。读数助手（`_last_speech_utc` 等）是这些判据的来源，也留在本模块。

纯搬层，行为与拆分前逐字一致。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from rolecard_agent.base.identity import resolve_instance_identity
from rolecard_agent.config import Settings
from rolecard_agent.core.common.thread_locks import thread_is_busy
from rolecard_agent.core.proactive.proactive_thread import proactive_thread_id
from rolecard_agent.roles.models import RoleCard
from rolecard_agent.storage.db import SqlConnection

# 静默时段（本地时间）：23:00–08:00 不主动打扰。
QUIET_HOURS_START = 23
QUIET_HOURS_END = 8
# 同一角色未读堆积上限：满了就不再开（防角色刷屏成轰炸）。
MAX_UNREAD_PER_ROLE = 2
# 退避倍率：每攒一条未读，下次要等的间隔乘一次这个数（未读=2 时本来就被上限闸住）。
BACKOFF_GROWTH = 2.0
# 间隔抖动幅度（±比例）：让"每天同一时刻"这件事不成立。依据见 `_quiet_minutes`。
JITTER_FRACTION = 0.12


# ----------------------------------------------------------- 度量与判定（可测，无线程依赖）


def _utc_from_db(raw: object) -> datetime | None:
    """DB 里的 UTC 时间串 → aware datetime。**两种精度都要吃**：`CURRENT_TIMESTAMP` 给秒级
    （`... %H:%M:%S`），而会话活动时刻走 `strftime('%Y-%m-%d %H:%M:%f','now')` 给毫秒级
    （`... %H:%M:%S.123`）。`fromisoformat` 两者都认，所以不必维护格式清单。
    读不出来就返回 None（= 不知道），而不是猜一个 —— 猜出来的时刻会直接变成"该不该开口"的判据。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError:
        return None


def _last_reachout_utc(conn: SqlConnection, role_id: str, *, user_id: str) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(created_at) AS at FROM agent_reachout"
        " WHERE role_id = ? AND user_id = ?",
        (role_id, user_id),
    ).fetchone()
    return _utc_from_db(None if row is None else row["at"])


def _lane_activity_utc(
    conn: SqlConnection, role_id: str, *, user_id: str
) -> datetime | None:
    """对方在那条主动会话里**最后一次有动静**的时刻（任何一轮都会推 `session_thread.updated_at`）。
    线程还不存在给 None（这条线从没建立过 = 无从判断他回没回）。
    """
    lane = conn.execute(
        "SELECT updated_at FROM session_thread WHERE thread_id = ?",
        (proactive_thread_id(role_id, user_id=user_id),),
    ).fetchone()
    return None if lane is None else _utc_from_db(lane["updated_at"])


def _last_speech_utc(
    conn: SqlConnection, role_id: str, *, user_id: str
) -> datetime | None:
    """她上一次**在这条线里说话**的时刻 —— 主动开口和**回答用户**都算，取较新那个。

    为什么必须算上回答（09-26 用户报的"我还没回话她就换了话题"）：间隔档原先只盯
    `agent_reachout`，于是这个场景是漏的 —— 她在对话里 13:52 刚回过一句（那轮的回复**不落
    reachout 表**），调度器看到的"上次开口"还是几小时前 ⇒ 间隔通过 ⇒ 一两分钟后又"主动"
    冒一句；而素材侧 `unanswered_lines` 拿她自己最后那句当分界（之后没有用户新话 = 空），
    于是这一句只能另找由头 ⇒ 用户看到的就是"不管刚才聊的是什么，她转头说起唱歌"。
    锚点改成"她最后一次说话"，间隔档的语义（同一角色两次冒话的最小间隔）才名副其实。

    会话那一侧读的是 `session_thread.updated_at`：**任何一轮写进去都会推它**，所以"用户刚发
    完、她还没答"那一段也在闸内（这正是 `thread_is_busy` 那道闸盖不住的间隙）。代价要说清 ——
    重命名、改会话级模型这类 PATCH 也会推它，于是一次重命名可能压掉她一个间隔的主动开口。
    这比"她刚答完就又冒一句"轻，接受。
    """
    reach = _last_reachout_utc(conn, role_id, user_id=user_id)
    chat = _lane_activity_utc(conn, role_id, user_id=user_id)
    if reach is None:
        return chat
    if chat is None:
        return reach
    return max(reach, chat)


def _unreplied_streak(
    conn: SqlConnection, role_id: str, *, user_id: str
) -> int:
    """她主动开口之后、对方**一个字都没回**的那几条 —— 退避指数用的就是它。

    为什么不用现成的 `unread`（09-26）：`mark_all_read` 的口径是用户 09-23 拍的"我点进对话
    界面了就算都看过"，于是他只要打开过一次控制台，未读数就归零、退避失效 ——
    "她连着冒了三条而我一条没回"这种最该退避的情形，在库里恰恰表现为 unread = 0。
    这一条不读状态列，只比时刻：`agent_reachout.created_at > 那条会话最后一次活动`。
    他回过一句话就会把 `updated_at` 推过那些开口 ⇒ 计数自然归零。
    （**封顶那一道仍然看 `unread`**，两种信号各有出口：划掉抽屉里的行能立刻解掉封顶，
    却解不掉"他没回话" —— 后者只把间隔拉长，不会把她永久关在门外。）

    **那条会话还没建立时给 0 而不是"全算"**：没有线就没有"他回没回"这回事，猜成"一条都没回"
    会把一个从没主动找过他的角色永久压在最长的退避上（同审计一贯的"不知道就放行"口径）。

    用 `julianday` 而不是直接比字符串：那两列虽然都是 ISO 形状，但 `TIMESTAMP` 声明在
    SQLite 里落进 NUMERIC 亲和，跨亲和的文本比较不是这里要的语义；换成数就只有一种读法。
    """
    lane = _lane_activity_utc(conn, role_id, user_id=user_id)
    if lane is None:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_reachout "
        "WHERE role_id = ? AND user_id = ? AND julianday(created_at) > julianday(?)",
        (role_id, user_id, lane.strftime("%Y-%m-%d %H:%M:%S.%f")),
    ).fetchone()
    return int(row["n"])


def _unread_for_role(conn: SqlConnection, role_id: str, *, user_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM agent_reachout"
        " WHERE role_id = ? AND user_id = ? AND state = 'unread'",
        (role_id, user_id),
    ).fetchone()
    return int(row["n"])


def _quiet_minutes(base_minutes: float, streak: int, seed: str) -> float:
    """这次该等多久（分钟）：先按"她说了而对方没回"的条数退避，再乘一个**确定性**的 ±12% 抖动。

    两件不同的事，各治一个实测症状：

      * **退避**（`base × BACKOFF_GROWTH ** streak`）—— 她说了你还没接，下一句就该等更久。
        N.E.K.O 用"级别"实现同一件事（120s 起步、按 1.09/1.55 收敛到 3600s 硬顶），我们不必
        再造一个级别字段：**那条会话里"她开口之后他没回过话"的条数就是现成的计数**
        （`_unreplied_streak` —— 09-26 从 `unread` 换过来，理由写在那一处的 docstring 里）。
      * **抖动**（±12%）—— 治"每天同一时刻说一句同样的话"。抄 N.E.K.O 的注释原话是
        "避免节奏过于机械"；它每次抽签，我们**改成按 (角色, 上次说话时刻) 派生的确定性抖动**：
        随机阈值会让"到底哪一秒够格"变成每 tick 重摇的抽签，既测不住也复现不了；种子只在
        她再次说话时才变，于是时间点自然逐日错开。
    """
    grown = base_minutes * BACKOFF_GROWTH ** max(0, streak)
    frac = random.Random(seed).uniform(-JITTER_FRACTION, JITTER_FRACTION)
    return grown * (1 + frac)


class Gate(NamedTuple):
    """一次抑制判定的全部读数。`why` 与 `next_ok_at` 与那两个计数**来自同一批查询**。

    为什么连 `streak` / `unread` 也要一起带出去（09-26 自己修自己）：界面上那句原因和旁边
    那两枚徽章若是各查一次，中间被人插一条就是
    两个数不一致 —— 而这一格存在的全部意义是让人信。同源不只指"话与时刻"，也指"话里的数"。
    """

    why: str | None
    next_ok_at: datetime | None
    streak: int
    unread: int


def quiet_gate(
    role: RoleCard,
    settings: Settings,
    conn: SqlConnection,
    *,
    now_utc: datetime,
    now_local: datetime,
    file_event: bool = False,
) -> Gate:
    """抑制层的唯一判定点。`next_ok_at=None` = 这不是"等一会儿就好"的事
    （未读封顶要他回话或划掉、正在对话要等这一轮跑完）。

    为什么是一个函数而不是"原因一段、时间另一段"：那句话与那个时刻**必须同源**，
    分两处算迟早对不上（"不足 66 分钟"配一个 40 分钟后的时刻，用户看一眼就再也不信这个界面）。
    """
    owner = resolve_instance_identity(settings)
    unread = _unread_for_role(conn, role.role_id, user_id=owner)
    streak = _unreplied_streak(conn, role.role_id, user_id=owner)
    last = _last_speech_utc(conn, role.role_id, user_id=owner)
    if last is not None and not file_event:
        need = _quiet_minutes(
            settings.reachout_interval_minutes, streak, f"{role.role_id}|{last.isoformat()}"
        )
        elapsed = now_utc - last
        if elapsed < timedelta(minutes=need):
            # 退避**不写进这句话**：界面上它是主句旁边一枚徽章（长句在 206px 的抽屉里会
            # 从中间折行，见 `R26-45`）。不写进句子 ≠ 不说 —— `Gate.streak` 就是那个数，
            # 句子与徽章同源一次算出，才不会"话里说连着 2 条、徽章写着 1"。
            return Gate(
                f"距上次说话不足 {need:.0f} 分钟",
                last + timedelta(minutes=need),
                streak,
                unread,
            )
    if now_local.hour >= QUIET_HOURS_START or now_local.hour < QUIET_HOURS_END:
        end = now_local.replace(hour=QUIET_HOURS_END, minute=0, second=0, microsecond=0)
        if end <= now_local:  # 23:00 之后那一段：要等到明天早上 8 点
            end += timedelta(days=1)
        return Gate("处于静默时段（23:00–08:00）", end.astimezone(UTC), streak, unread)
    if unread >= MAX_UNREAD_PER_ROLE:
        return Gate("未读堆积已达上限", None, streak, unread)
    # **正在聊就别插话**（审计 #12 的第二层）：这一轮用户的话还在图上跑，此时投递的那句
    # 会和它抢同一份检查点（`deliver_proactive` 那侧也有锁兜底，但"不打断"本来就是对的语义）。
    # 判据用锁的持有状态而不是"最后一条消息的时间"：后者在用户回完话、她还没答的间隙里是 False，
    # 而那恰好是最不该插嘴的一刻。
    if thread_is_busy(proactive_thread_id(role.role_id, user_id=owner)):
        return Gate("这条会话正在对话中", None, streak, unread)
    return Gate(None, None, streak, unread)


def quiet_status(
    roles: Sequence[RoleCard],
    settings: Settings,
    conn: SqlConnection,
    *,
    now_utc: datetime | None = None,
    now_local: datetime | None = None,
) -> list[dict[str, object]]:
    """每个"有资格主动"的角色此刻为什么静默（`S-8`）。空表 = 没有任何角色开了主动开口。

    口径要说清两件事：
    * 这里**不判全局总闸**（调用方的路由是使用者档，读不到 operator 才有的热切视图，
      而且"总闸关了"这件事界面上本来就看得见）；只按角色卡的 `reachout_enabled` 过滤，
      与调度器同一句谓词。
    * `why=None` 是"她现在随时能开口"，不是"坏了/没数据"。界面上这一格要写成肯定句，
      否则用户读成"系统没算出来"。
    """
    stamp_utc = now_utc or datetime.now(UTC)
    # **带时区的本地时刻**：只有 `.hour` 的比较不看 tz，但"下一次大约 08:00"要换算成 UTC 给
    # 界面显示， naive 的 `astimezone` 会按跑进程的机器猜一遍。
    stamp_local = now_local or datetime.now().astimezone()
    out: list[dict[str, object]] = []
    for role in roles:
        if not role.reachout_enabled:
            continue
        gate = quiet_gate(role, settings, conn, now_utc=stamp_utc, now_local=stamp_local)
        out.append(
            {
                "role_id": role.role_id,
                "role_name": role.role_name,
                "why": gate.why,
                "next_ok_at": None if gate.next_ok_at is None else gate.next_ok_at.isoformat(),
                # 直接取 `quiet_gate` 那次读的数：再查一遍就会出现"话里说连着 2 条、字段是 1"。
                "streak": gate.streak,
                "unread": gate.unread,
            }
        )
    return out