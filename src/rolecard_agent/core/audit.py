"""审计写入的**唯一咽喉**（2026-10-02 轮 `R102-07`）。

从前这条链上有**五份**逐字相同的 `INSERT INTO audit_log`：`roles/service.py`（api 侧 55 处
全借它，于是"审计的咽喉挂在一个名字只谈角色卡的服务上"）、`core/plugins.py`，以及
`core/tools/{files,mcp,run}.py` 三个工具模块。同一条留痕语句抄五遍的代价就是本仓那一族
事故的形状：改一处口径（actor 怎么填、detail 怎么编码、写完要不要 commit），另外四处静默
不动 —— 而 `detail` 的编码从前真的不一致（`mcp` 那份额外用 `default=str`，另外四份遇到
不可序列化的值就抛）。这里统一成一份：`default=str` 只对本来会抛的类型生效，JSON 原生的
载荷逐字节不变。

名字也归名字：端点侧写审计从此是 `ctx.audit.log(...)`，`RoleCardService` 不再兼任审计服务。

**动作词表**（`R102-14`）：一个动作一个名字，改名会把同一动作劈成两条历史，所以这里有一份
清单。门禁的 `audit action vocabulary` 拿 `src/` 里的字面量与它做**双向**差分 —— 正向
"用了没登记的词"即红，反向"登记了没人用"同样红（`R102-37` 的教训：只写一臂的判据是空转臂）。
带变量的那一族名字（`mcp:{server}__{tool}`）声明在 `DYNAMIC_ACTION_PREFIXES` 里，按前缀放行，
不进清单 —— 清单管得住的是"有名字的动作"，不是"运行时拼出来的字符串"。
"""

from __future__ import annotations

import json

from rolecard_agent.storage.db import SqlConnection

#: 审计动作词表的唯一出处。每一项都必须能在 `src/` 里被引用到（反向差集由门禁看着）。
AUDIT_ACTIONS: frozenset[str] = frozenset(
    {
        "add_mcp_server",
        "add_memory_item",
        "add_model",
        "add_service_endpoint",
        "approve_command",
        "cleanup_orphan_uploads",
        "clear_memory",
        "clear_role_memory",
        "clear_task_dir",
        "consolidate_memory",
        "create_domain_record",
        "create_report",
        "create_role",
        "create_session",
        "delete_domain_record",
        "delete_index",
        "delete_memory_item",
        "delete_model",
        "delete_report",
        "delete_role",
        "extract_memory",
        "extract_report",
        "extract_report_failed",
        "fs_list",
        "fs_read",
        "fs_write",
        "local_service_pin",
        "local_service_unload",
        "merge_memory_item",
        "open_proactive_session",
        "plugin_disable",
        "plugin_enable",
        "plugin_noop",
        "probe_model",
        "remove_mcp_server",
        "reject_command",
        "remove_service_endpoint",
        "reset_knowledge_scope",
        "run_command",
        "set_session_mode",
        "set_session_model",
        "set_task_dir",
        "stop_turn",
        "switch_role",
        "sync_apply",
        "sync_export",
        "sync_import",
        "sync_pull",
        "sync_reconcile",
        "test_mcp_server",
        "update_domain_record",
        "update_index",
        "update_mcp_server",
        "update_memory",
        "update_memory_item",
        "update_model_capabilities",
        "update_model_context",
        "update_model_sampling",
        "update_model_settings",
        "update_role",
        "update_role_memory",
        "update_runtime_settings",
        "update_service_endpoint",
        "update_service_order",
    }
)

#: 运行时拼出来的那一族动作名，按前缀放行（`mcp:{server_id}__{tool}`）。
DYNAMIC_ACTION_PREFIXES: frozenset[str] = frozenset({"mcp:"})

#: 模型触发的工具动作统一的 actor 名（与操作员的 `operator` 动作区分）。
AGENT_ACTOR = "agent"


def action_is_known(action: str) -> bool:
    """这个词在词表里吗（含动态前缀族）。"""
    return action in AUDIT_ACTIONS or action.startswith(tuple(DYNAMIC_ACTION_PREFIXES))


class AuditTrail:
    """`audit_log` 的唯一写入口。

    `conn=None` = 这个宿主没接审计（测试、或工具在没库的环境里跑）：跳过留痕，
    不影响权限判定 —— 与从前 `core/tools/*` 那三份 `if conn is None: return` 同语义。
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: SqlConnection | None = None) -> None:
        self._conn = conn

    def log(
        self,
        *,
        actor: str,
        action: str,
        target: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        """追加一行审计。写完立刻 commit：留痕不该跟着调用方的事务一起回滚。"""
        conn = self._conn
        if conn is None:
            return
        conn.execute(
            "INSERT INTO audit_log (actor, action, target, detail_json) VALUES (?, ?, ?, ?)",
            (
                actor,
                action,
                target,
                None if detail is None else json.dumps(detail, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()


def tool_audit(
    conn: SqlConnection | None,
    action: str,
    target: str | None,
    detail: dict[str, object] | None = None,
) -> None:
    """工具侧的一行写法：actor 恒为 `agent`，没有 conn 就跳过留痕。

    `core/tools/{files,run}.py` 从前各抄一份同样的 INSERT（连同 `if conn is None: return`
    那条判断），这里收一份 —— 两个模块 `import tool_audit as _audit`，调用点一字不动。
    """
    AuditTrail(conn).log(actor=AGENT_ACTOR, action=action, target=target, detail=detail)
