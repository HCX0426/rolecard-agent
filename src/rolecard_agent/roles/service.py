"""Role card CRUD + whitelist resolution.

Returns Pydantic models and raises typed errors - callers never see sqlite3.Row, and no SQL
leaks upward into the agent layer.

归属（09-27，架构总览 §4.1 的 M2a）：`role_card` 现在每一行都有主人，而**读它的唯一入口是
`RoleCardService.scoped(user_id)` 返回的那个视图**。这个类上刻意没有"不带主人就能读"的方法，
所以"忘了过滤"这种代码在这儿构不出来 —— 一条静默放宽的读路径不会报错，只会让 A 看见 B 的角色卡。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from rolecard_agent.base.audit import AuditTrail
from rolecard_agent.roles.models import RoleCard, RoleCardCreate, RoleCardUpdate
from rolecard_agent.roles.seed import BUILTIN_ROLES
from rolecard_agent.storage.db import SqlConnection
from rolecard_agent.storage.threads import set_current_role

_COLUMNS = (
    "role_id, user_id, role_name, system_prompt, temperature, model_name, "
    "tool_whitelist, exemplars, knowledge_scopes, description, "
    "is_builtin, reachout_enabled, recall_enabled, time_pattern_enabled, "
    "affinity_enabled, file_watch_enabled, reachout_keep, pet_pack, created_at, updated_at"
)

# Columns stored as JSON text. For every one of them `None` and `[]` mean different things,
# so the distinction has to survive the round trip.
#
# **公开名字**（10-02 轮 `R102-24`）：`core/sync.py` 交出卡的载荷时要把这三列从"行里的文本"
# 换成"模型要的list"，那边抄一份清单就是第二个真相 —— 而那条链坏过的样子恰好是
# "清单改了、抄的那份没改"（`R28-14` 那一族第七次）。
CARD_JSON_COLUMNS = ("tool_whitelist", "exemplars", "knowledge_scopes")
_JSON_COLUMNS = CARD_JSON_COLUMNS  # 本模块内的旧称，读起来更短


class RoleError(Exception):
    """Base for role-domain failures. Never carries a stack trace to the caller."""


class RoleNotFound(RoleError):
    pass


class RoleAlreadyExists(RoleError):
    pass


class BuiltinRoleProtected(RoleError):
    """Raised when a caller tries to delete a role whose `is_builtin` flag is set.

    This is a guardrail, not a policy: losing the last role would leave the system with
    nothing to run a conversation as.
    """


def _row_to_model(row: sqlite3.Row) -> RoleCard:
    payload = dict(row)
    for column in _JSON_COLUMNS:
        raw = payload.get(column)
        payload[column] = None if raw is None else json.loads(raw)
    payload["is_builtin"] = bool(payload.get("is_builtin"))
    payload["reachout_enabled"] = bool(payload.get("reachout_enabled"))
    payload["recall_enabled"] = bool(payload.get("recall_enabled"))
    payload["time_pattern_enabled"] = bool(payload.get("time_pattern_enabled"))
    payload["affinity_enabled"] = bool(payload.get("affinity_enabled"))
    payload["file_watch_enabled"] = bool(payload.get("file_watch_enabled"))
    return RoleCard(**payload)


def _dump_json(value: object | None) -> str | None:
    """Serialize a JSON column.

    `None` passes through untouched: for tool_whitelist `None` means "all" while `[]` means
    "none", and for knowledge_scopes `None` and `[]` both mean "no retrieval" but must still
    come back as what was stored.
    """
    if value is None:
        return None
    if isinstance(value, list):
        items = [item.model_dump() if hasattr(item, "model_dump") else item for item in value]
        return json.dumps(items, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


@dataclass(slots=True)
class RoleCards:
    """**某一个主人眼里**的角色卡集合 —— `role_card` 的唯一读路径。

    归属规则只有一条：`WHERE user_id = ?`，没有例外。内置卡今天**不算公共资产**：每台实例
    为自己的主人播种一份（就是 §4.1 那句"两份完整数据集"）。真要做"公共模板 + 每人一份覆盖"，
    第一步是把 `role_id` 的全局唯一主键改成 `(user_id, role_id)` —— 那是一次整表重建加会话、
    记忆、收件箱的引用改造，不是再加一列。

    看不见的卡一律 `RoleNotFound`（→ HTTP 404），**不回"这张存在但不是你的"**：那等于把别人的
    角色 id 泄露成一份可验证的清单，与 `api/deps.get_thread` 同一条理由。
    """

    conn: SqlConnection
    user_id: str

    # -- reads ---------------------------------------------------------------

    def list_roles(self) -> list[RoleCard]:
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM role_card WHERE user_id = ? ORDER BY is_builtin DESC, role_id",
            (self.user_id,),
        ).fetchall()
        return [_row_to_model(r) for r in rows]

    def get(self, role_id: str) -> RoleCard:
        row = self.conn.execute(
            f"SELECT {_COLUMNS} FROM role_card WHERE role_id = ? AND user_id = ?",
            (role_id, self.user_id),
        ).fetchone()
        if row is None:
            raise RoleNotFound(f"role not found: {role_id}")
        return _row_to_model(row)

    def exists(self, role_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM role_card WHERE role_id = ? AND user_id = ?",
            (role_id, self.user_id),
        ).fetchone()
        return row is not None

    # -- writes --------------------------------------------------------------

    def create(self, data: RoleCardCreate, *, is_builtin: bool = False) -> RoleCard:
        """建一张卡，主人就是这个视图绑的那个人 —— **参数里没有 user_id，也不该有**。

        归属能从请求体里进来是这条链最坏的一种洞：那等于谁都能指定"这张卡是谁的"。
        """
        if self.exists(data.role_id):
            raise RoleAlreadyExists(f"role already exists: {data.role_id}")
        self.conn.execute(
            "INSERT INTO role_card "
            "(role_id, user_id, role_name, system_prompt, temperature, model_name, "
            " tool_whitelist, exemplars, knowledge_scopes, description, is_builtin, "
            "reachout_enabled, recall_enabled, time_pattern_enabled, affinity_enabled, "
            "file_watch_enabled, "
            "reachout_keep, pet_pack) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.role_id,
                self.user_id,
                data.role_name,
                data.system_prompt,
                data.temperature,
                data.model_name,
                _dump_json(data.tool_whitelist),
                _dump_json(data.exemplars),
                _dump_json(data.knowledge_scopes),
                data.description,
                1 if is_builtin else 0,
                data.reachout_enabled,
                data.recall_enabled,
                data.time_pattern_enabled,
                data.affinity_enabled,
                data.file_watch_enabled,
                data.reachout_keep,
                data.pet_pack,
            ),
        )
        self.conn.commit()
        return self.get(data.role_id)

    def update(self, role_id: str, data: RoleCardUpdate) -> RoleCard:
        changes = data.changes()
        for column in _JSON_COLUMNS:
            if column in changes:
                changes[column] = _dump_json(getattr(data, column))
        if not changes:
            return self.get(role_id)  # nothing to do; still validate existence

        assignments = ", ".join(f"{name} = ?" for name in changes)
        params = [*changes.values(), role_id, self.user_id]
        cur = self.conn.execute(
            f"UPDATE role_card SET {assignments}, updated_at = CURRENT_TIMESTAMP "
            "WHERE role_id = ? AND user_id = ?",
            params,
        )
        if cur.rowcount == 0:
            # 改到 0 行的 UPDATE 也已经开了一个写事务，而这一路走不到 commit ——
            # 不回滚就把 RESERVED 锁留给这条线程（`R102-42`；领域服务里那两处早就写过
            # 这句理由，漏的是这里 —— 所以本轮给它加了一把尺子 `dangling write txn`）。
            self.conn.rollback()
            raise RoleNotFound(f"role not found: {role_id}")
        self.conn.commit()
        return self.get(role_id)

    def delete(self, role_id: str) -> None:
        """删角色卡，并**连她自己的那份状态一起删**。

        09-26 轮 R26-08：从前这里只删 `role_card` 一行，于是该角色的
        `role_memory_item` / `role_proactive_state` / `role_memory` 全部留着 —— 而
        `current_role_id` 没有外键，**同名重建一张卡就把旧记忆原样复活**。用户读到的是
        "我删掉的角色还记得我从没说过的事"。

        刻意**不动会话与消息**：那是用户的对话历史，不是这个角色的附属物；删一个角色
        不该顺手销毁用户聊过的东西（与卸载不删数据同一条理由）。
        """
        role = self.get(role_id)  # raises RoleNotFound if absent or not mine
        if role.is_builtin:
            raise BuiltinRoleProtected(f"built-in role cannot be deleted: {role_id}")
        for table in ("role_memory_item", "role_memory"):
            self.conn.execute(f"DELETE FROM {table} WHERE role_id = ?", (role_id,))
        # role_proactive_state 的主键是 (user_id, role_id)（多租户 B2）：删除范围必须带上
        # 主人，否则将来同名卡归别人时，删自己的这张会顺手清掉对方的状态。
        self.conn.execute(
            "DELETE FROM role_proactive_state WHERE role_id = ? AND user_id = ?",
            (role_id, self.user_id),
        )
        self.conn.execute(
            "DELETE FROM role_card WHERE role_id = ? AND user_id = ?",
            (role_id, self.user_id),
        )
        self.conn.commit()


class RoleCardService:
    """装配级的角色设施：播种、线程当前角色切换、审计。

    **数据读写一律走 `scoped(user_id)`** —— 这个类上刻意没有 `list_roles()` / `get()` 那种
    不带主人的读法。它自己留下的三件事都不需要"以谁的身份读"：播种是替这台实例的主人建行、
    `set_thread_role` 改的是会话行、审计写的是 `audit_log`。

    Role switching lives here rather than in `core/tools/builtin.py` on purpose: it must NOT
    be reachable by the model, so it is a service call that the API/UI layer invokes - never
    a bound tool (D2 / C5).
    """

    def __init__(self, conn: SqlConnection) -> None:
        self._conn = conn
        #: 审计咽喉是 `base/audit.AuditTrail`（`R102-07`）。从前这里躺着全仓第五份
        #: `INSERT INTO audit_log`，于是 api 侧 55 处写审计都得先取角色卡服务。
        self._audit = AuditTrail(conn)

    def scoped(self, user_id: str) -> RoleCards:
        """换一个主人：`ctx.current_user()`（这次请求）或 `runtime.identity`（这台实例）。"""
        return RoleCards(self._conn, user_id)

    # -- seeding -------------------------------------------------------------

    def seed_builtins(
        self, roles: Iterable[RoleCardCreate] = BUILTIN_ROLES, *, user_id: str
    ) -> int:
        """Upsert built-in roles **for one owner**. Idempotent, so `init_db.py` can boot-run.

        Upsert rather than create: a shipped change to a built-in role's prompt should reach
        existing databases without a migration step. 连 `user_id` 一起 upsert —— 出厂卡的主人
        就是这台实例的主人，重启不该把它变成无主行。
        """
        count = 0
        for role in roles:
            self._conn.execute(
                "INSERT INTO role_card "
                "(role_id, user_id, role_name, system_prompt, temperature, model_name, "
                " tool_whitelist, exemplars, knowledge_scopes, description, is_builtin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(role_id) DO UPDATE SET "
                "  user_id = excluded.user_id, "
                "  role_name = excluded.role_name, "
                "  system_prompt = excluded.system_prompt, "
                "  temperature = excluded.temperature, "
                "  model_name = excluded.model_name, "
                "  tool_whitelist = excluded.tool_whitelist, "
                "  exemplars = excluded.exemplars, "
                "  knowledge_scopes = excluded.knowledge_scopes, "
                "  description = excluded.description, "
                "  is_builtin = 1, "
                "  updated_at = CURRENT_TIMESTAMP "
                # **内容真变了才盖时刻**（`R102-25`）。从前这句无条件执行，于是每次开机
                # 都给没人动过的卡盖一个新的 `updated_at`，而卡类冲突的裁决是"新者胜且
                # 自动执行"（`core/sync.py` 的 `auto_moves`）—— 结构上"B 只是开了机"就能
                # 吃掉 A 的手改。`IS NOT` 是 SQLite 的空安全不等，与 `memory.restore_row`
                # 那条同一个写法（那一族的幂等判据早就长这样了）。
                "WHERE role_card.user_id IS NOT excluded.user_id "
                "  OR role_card.role_name IS NOT excluded.role_name "
                "  OR role_card.system_prompt IS NOT excluded.system_prompt "
                "  OR role_card.temperature IS NOT excluded.temperature "
                "  OR role_card.model_name IS NOT excluded.model_name "
                "  OR role_card.tool_whitelist IS NOT excluded.tool_whitelist "
                "  OR role_card.exemplars IS NOT excluded.exemplars "
                "  OR role_card.knowledge_scopes IS NOT excluded.knowledge_scopes "
                "  OR role_card.description IS NOT excluded.description "
                "  OR role_card.is_builtin IS NOT 1",
                (
                    role.role_id,
                    user_id,
                    role.role_name,
                    role.system_prompt,
                    role.temperature,
                    role.model_name,
                    _dump_json(role.tool_whitelist),
                    _dump_json(role.exemplars),
                    _dump_json(role.knowledge_scopes),
                    role.description,
                ),
            )
            count += 1
        self._conn.commit()
        return count

    def seed_domain_roles(
        self, roles: Iterable[RoleCardCreate], *, user_id: str
    ) -> int:
        """播种域角色：类型是**自定义**（is_builtin=0），已存在则一个字段都不覆盖。

        `roles` **没有默认值**：域角色清单不再住在本包里（2026-10-04 审查快照的域机制
        条目 —— 内核点名具体域的工具名与知识作用域就是概念泄漏），调用方从各域的
        `DomainSpec.seed_roles` 聚合后传进来（`domains.registry.domain_seed_roles()`；
        装配根拿宿主注入的那一份）。给个空默认值会让"忘了接"变成静默不播种，所以宁可
        让漏接在签名上就炸。

        与 `seed_builtins` 的全字段 upsert 刻意不同（用户 2026-09-17 反馈"健康档案管理员
        改成自定义"）：域角色是领域概念，不该由内核在每次重启时把操作员的改名/改提示词
        冲回出厂值。两条语义：缺失才插入；已存在的行只做一次幂等降级
        （is_builtin 纠正为 0，覆盖老库被误标内置的历史数据）。

        这里的 upsert **不动 `user_id`** —— 那张卡要是已经被改到别人名下，重启不该抢回来。
        """
        count = 0
        for role in roles:
            self._conn.execute(
                "INSERT INTO role_card "
                "(role_id, user_id, role_name, system_prompt, temperature, model_name, "
                " tool_whitelist, exemplars, knowledge_scopes, description, is_builtin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
                # 这一支只做一件事：把老库里被误标成内置的行降级。降级**真的发生**才盖时刻
                # （`R102-25` 的另一半 —— docstring 明写"已存在则一个字段都不覆盖"，
                # 而无条件的 `updated_at = CURRENT_TIMESTAMP` 每次都覆盖了一个字段：
                # 那张卡的"最后一次真改动"）。
                "ON CONFLICT(role_id) DO UPDATE SET is_builtin = 0, "
                "  updated_at = CURRENT_TIMESTAMP "
                "WHERE role_card.is_builtin IS NOT 0",
                (
                    role.role_id,
                    user_id,
                    role.role_name,
                    role.system_prompt,
                    role.temperature,
                    role.model_name,
                    _dump_json(role.tool_whitelist),
                    _dump_json(role.exemplars),
                    _dump_json(role.knowledge_scopes),
                    role.description,
                ),
            )
            count += 1
        self._conn.commit()
        return count

    # -- session-level role switch (operator action, not an LLM tool) --------

    def set_thread_role(
        self, thread_id: str, role_id: str, *, user_id: str, actor: str = "system"
    ) -> None:
        """Point a thread at a different role WITHOUT touching its message history.

        Only `current_role_id` changes. The next turn's system prompt is assembled from the
        new role, while the stored messages remain untouched - that is what "switching a role
        preserves context" actually means in this design.

        `user_id` 同时管两头：**目标卡必须在他看得见的范围里**（否则"把线程指向别人那张卡"
        就成了一条读别人提示词的路），而 UPDATE 也只允许改他自己那条线程
        （`WHERE thread_id = ? AND user_id = ?`）。
        """
        self.scoped(user_id).get(role_id)  # fail before writing if not his to use
        changed = set_current_role(
            self._conn, thread_id=thread_id, user_id=user_id, role_id=role_id
        )
        if changed == 0:
            # 同 `update`：0 行的 UPDATE 也开了写事务，抛之前先结束它（`R102-42`）。
            self._conn.rollback()
            raise RoleNotFound(f"thread not found: {thread_id}")
        self._audit.log(
            actor=actor, action="switch_role", target=thread_id, detail={"role_id": role_id}
        )
        self._conn.commit()

    def current_thread_role(self, thread_id: str) -> str:
        row = self._conn.execute(
            "SELECT current_role_id FROM session_thread WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise RoleNotFound(f"thread not found: {thread_id}")
        return str(row["current_role_id"])

    # `audit()` 这个名字从前住在这里，而它做的事与角色卡无关（`R102-07`）：
    # 咽喉搬到 `base/audit.py`，端点侧写审计走 `ctx.audit.log(...)`。
