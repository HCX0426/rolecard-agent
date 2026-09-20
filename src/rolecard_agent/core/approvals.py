"""命令执行审批（架构计划 C·§6.2）：run_command 的「先批准后执行」状态机。

## 一句话职责

模型提议跑的命令，是否已经人批准、批准过没、结果是什么 —— 全在这张 `command_approval`
表的状态机上。**执行本身不在本模块**：subprocess 在 `core/tools/run.py`
（`execute_command` / `run_approval_execution`），本模块只负责"审不审、批没批"这件事
的事实面，以及执行完成后的结果回填。

## 状态机（v1）

    pending -> approved（用户批准，后台执行中）-> done（result_json 已回填）
            \\-> rejected（终态：不再自动重提，避免模型换个说法刷屏）

* 同 command（规范化后）**最近一条** ∈ {approved, done} → 工具视为已获准，直接执行；
  pending → 还在等审批（不重复提交）；rejected → 向用户说明"已被拒绝"。
* 只有 `pending` 能 decide；对已决定的行再次 decide 是调用方 bug（ValueError）。
* 规范化（_normalise_cmd）：strip + 空白折叠成单空格 —— "python a.py " 和
  "python   a.py" 是同一条命令，不能靠多余空格骗过审批缓存。

## 并发/线程

`conn` 是 ThreadLocalConnection：decide / finish 可能发生在**后台执行线程**里
（run_approval_execution），它按线程分发真实连接，天然安全。
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
from typing import Any

from rolecard_agent.storage.db import SqlConnection

_PENDING = "pending"
_APPROVED = "approved"
_REJECTED = "rejected"
_DONE = "done"

# 同命令比较用的空白折叠：任意连续空白（含换行）替换为单个空格。
_WS_RE = re.compile(r"\s+")

# 决定令牌的有效期（秒）。为什么要有：令牌是"你刚才确实看到过这条待批"的凭据，而审批面板
# 可能开着就去干别的了；不设上限的话一次读取等于买断一条命令的批准权（页面挂一整天，
# 期间任何一次带该令牌的 POST 都能批）。30 分钟是"回来还能批，隔天就得重新看一遍"。
DECIDE_TOKEN_TTL_SECONDS = 30 * 60


class ApprovalNotFound(KeyError):
    """找不到审批记录（按 id / 命令查）。请求语义 = 404。"""


class ApprovalAlreadyDecided(ValueError):
    """对已决定（approved/rejected）的记录再次 decide。请求语义 = 400。"""


class ApprovalUnauthorised(PermissionError):
    """决定令牌缺失 / 不匹配 / 过期。请求语义 = 403。

    与 `ApprovalAlreadyDecided` 分开是有意的：那条说"你已经批过了"，这条说"你没法批" ——
    前者是重复提交，后者是缺凭据，用户看到的两句人话不能混。
    """


def normalise_cmd(command: str) -> str:
    """规范化命令字符串，让"同一命令"的比较不因多余空白漂移。"""
    return _WS_RE.sub(" ", (command or "").strip())


def _new_decide_token() -> str:
    return secrets.token_urlsafe(24)


class ApprovalService:
    """命令审批的事实面：提交 / 查询 / 决定 / 回填结果。不执行任何命令。"""

    def __init__(self, conn: SqlConnection) -> None:
        self._conn = conn

    # ---------------------------------------------------------------- 写

    def submit(
        self,
        command: str,
        *,
        cwd: str | None = None,
        role_id: str | None = None,
        role_name: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """提交一条待审批命令。返回**最近一条该命令的记录**。

        幂等语义：同命令已有 pending / approved / done 记录时**不重复插入**，直接返回
        那条记录（模型执着地重提同一命令不会刷审批队列）。rejected 是终态 —— 不自动
        重提（调用方应向用户转述拒绝原因，请模型换一种做法）。
        """
        cmd = normalise_cmd(command)
        latest = self.latest(cmd)
        if latest is not None and latest["status"] in (_PENDING, _APPROVED, _DONE, _REJECTED):
            return latest
        conn = self._conn
        conn.execute(
            "INSERT INTO command_approval (command, cwd, role_id, role_name, thread_id, "
            "decide_token) VALUES (?, ?, ?, ?, ?, ?)",
            (cmd, cwd, role_id, role_name, thread_id, _new_decide_token()),
        )
        conn.commit()
        created = self.latest(cmd)
        assert created is not None  # 刚插入，必然可查
        return created

    def decide(
        self, approval_id: int, decision: str, *, token: str | None = None
    ) -> dict[str, Any]:
        """pending → approved / rejected。对已决定的行抛 ApprovalAlreadyDecided。

        `decision` 是动词："approve" / "reject"（存进库的是状态值 approved / rejected）。
        只改状态，**不执行**：approve 后的实际执行由调用方（路由）安排到后台线程，
        完成后调 `finish` 把结果写回。这样"批准"这个 HTTP 请求自身永远快。

        `token` 是这条记录的一次性能力凭证（P0-3 第一步）：只有先从读侧看到过这条待批，
        才可能持有它 —— 于是"猜一个自增 id 就批准"与"浏览器里一段跨源 JS 盲 POST"都失效。
        决定之后令牌清空（同一份响应内容不能批第二次），过期同理要重新读一遍。
        """
        target = {"approve": _APPROVED, "reject": _REJECTED}.get((decision or "").strip().lower())
        if target is None:
            raise ValueError(f"decide 只接受 approve/reject，收到：{decision!r}")
        conn = self._conn
        row = conn.execute(
            "SELECT id, status, decide_token, "
            "(julianday('now') - julianday(created_at)) * 86400.0 AS age_s "
            "FROM command_approval WHERE id = ?",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise ApprovalNotFound(f"审批记录不存在：{approval_id}")
        if row["status"] != _PENDING:
            raise ApprovalAlreadyDecided(
                f"审批记录 {approval_id} 已是 {row['status']}，不能再次决定"
            )
        self._check_token(approval_id, row["decide_token"], token, float(row["age_s"] or 0.0))
        conn.execute(
            "UPDATE command_approval SET status = ?, decide_token = NULL, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (target, approval_id),
        )
        conn.commit()
        return self.get(approval_id)

    @staticmethod
    def _check_token(
        approval_id: int, expected: str | None, given: str | None, age_seconds: float
    ) -> None:
        """令牌的三件事：存在、相等、没过期。比对用常量时间比较（不比对长度）。"""
        if not expected:
            # 老库里补列之前的 pending 行没有令牌 —— 宁可让它重新提交，也不要"缺令牌就放行"。
            raise ApprovalUnauthorised(
                f"审批记录 {approval_id} 没有决定令牌（早于令牌机制建立），请让角色重新提交这条命令"
            )
        if age_seconds > DECIDE_TOKEN_TTL_SECONDS:
            raise ApprovalUnauthorised(
                f"审批记录 {approval_id} 的决定令牌已过期（超过 "
                f"{DECIDE_TOKEN_TTL_SECONDS // 60} 分钟），请重新查看待批列表后再批"
            )
        if not given or not hmac.compare_digest(expected, given):
            raise ApprovalUnauthorised(
                f"批准审批记录 {approval_id} 需要决定令牌（随待批列表下发），且必须一致"
            )

    def finish(self, approval_id: int, result: dict[str, Any]) -> None:
        """approved → done 并回填执行结果（后台线程在命令跑完后调用）。"""
        conn = self._conn
        conn.execute(
            "UPDATE command_approval SET status = ?, result_json = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (_DONE, json.dumps(result, ensure_ascii=False), approval_id),
        )
        conn.commit()

    # ---------------------------------------------------------------- 读

    def status_of(self, command: str) -> str | None:
        """该命令最近一条记录的状态；从没提交过 = None。"""
        latest = self.latest(command)
        return None if latest is None else latest["status"]

    def latest(self, command: str) -> dict[str, Any] | None:
        """某命令最近一条审批记录（按 id 倒序）。"""
        conn = self._conn
        row = conn.execute(
            "SELECT id, command, cwd, role_id, role_name, thread_id, status, "
            "result_json, decide_token, created_at, updated_at FROM command_approval "
            "WHERE command = ? ORDER BY id DESC LIMIT 1",
            (normalise_cmd(command),),
        ).fetchone()
        return None if row is None else self._row(row)

    def get(self, approval_id: int) -> dict[str, Any]:
        conn = self._conn
        row = conn.execute(
            "SELECT id, command, cwd, role_id, role_name, thread_id, status, "
            "result_json, decide_token, created_at, updated_at FROM command_approval WHERE id = ?",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise ApprovalNotFound(f"审批记录不存在：{approval_id}")
        return self._row(row)

    def list_rows(
        self, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """审批记录列表（默认按 id 倒序）。`status` 可按状态过滤（前端 pending / 全部）。"""
        conn = self._conn
        if status is None:
            rows = conn.execute(
                "SELECT id, command, cwd, role_id, role_name, thread_id, status, "
                "result_json, decide_token, created_at, updated_at FROM command_approval "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, command, cwd, role_id, role_name, thread_id, status, "
                "result_json, decide_token, created_at, updated_at FROM command_approval "
                "WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        raw = row["result_json"]
        return {
            "id": row["id"],
            "command": row["command"],
            "cwd": row["cwd"],
            "role_id": row["role_id"],
            "role_name": row["role_name"],
            "thread_id": row["thread_id"],
            "status": row["status"],
            "result": None if not raw else json.loads(raw),
            # 令牌随读侧下发：持有它 = "刚才确实看到过这条待批"。决定后为 None。
            "decide_token": row["decide_token"],
            "decide_token_ttl_seconds": DECIDE_TOKEN_TTL_SECONDS,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


__all__ = [
    "ApprovalAlreadyDecided",
    "ApprovalNotFound",
    "ApprovalService",
    "ApprovalUnauthorised",
    "DECIDE_TOKEN_TTL_SECONDS",
    "normalise_cmd",
]