"""域清单与工具代数（tool_epoch）—— 域的运行时登记面。

2026-10-24 权限模型收口：**域常开**。从前 `plugin` 表存"操作员开关"（ENABLED vs
REGISTERED 两本账），角色卡的工具白名单之外还有一道域级闸；现在**白名单即唯一边界**
（操作员在角色卡上勾选，模型依旧不能改自己的白名单——自我扩权红线不变），这张表退化为
"注册了哪些域"的清单（`seed_plugin_rows` 播种、`enabled` 列退役不再读写）。

`kernel_meta.tool_epoch` 保留且更重要了：它是"工具集在会话存活期间变过没有"的唯一信号。
开关那一族写入消失后，唯一的变化来源是**热重建**（模型设置 / 服务端点保存 → 注册表整体
重建，MCP 服务器增删也走这条）——`assembler.rebuild` 在换装临界区内 bump 一次，C14 的
漂移检测（历史 tool_calls 降级为"未启用"而不是炸）因此继续有效。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from rolecard_agent.base.audit import AuditTrail
from rolecard_agent.storage.db import SqlConnection

TOOL_EPOCH_KEY = "tool_epoch"
DEFAULT_TOOL_EPOCH = 1


class PluginError(Exception):
    """Base for plugin-domain failures. Never carries a stack trace to the caller."""


class UnknownPlugin(PluginError):
    """Raised when a caller names a plugin that the code does not register.

    Checked only when `known_plugins` was supplied: the service cannot know the code-side
    registry by itself, and inventing one would re-create the two-lists problem (A3).
    """


def seed_plugin_rows(conn: SqlConnection, domains: Sequence[str]) -> None:
    """每个 REGISTERED 域一行 plugin 记录，默认开启（幂等）。

    `ON CONFLICT(plugin_id) DO NOTHING` —— 重跑绝不能把操作员关掉的插件重新打开：
    表一旦存在，`enabled` 就以它为唯一事实来源。`create_app` 与 `scripts/init_db.py`
    共用此函数（此前是两处镜像实现，靠注释提醒人工同步 —— 正是会漂移的那种重复）。
    """
    for domain in domains:
        conn.execute(
            "INSERT INTO plugin (plugin_id, display_name, enabled, sort_order) "
            "VALUES (?, ?, 1, 0) ON CONFLICT(plugin_id) DO NOTHING",
            (domain, domain),
        )
    conn.commit()


class PluginService:
    def __init__(self, conn: SqlConnection, *, known_plugins: Sequence[str] | None = None) -> None:
        self._conn = conn
        self._known = tuple(known_plugins) if known_plugins is not None else None
        #: 审计咽喉（`R102-07`）：这里不再自带一份 INSERT。
        self._trail = AuditTrail(conn)

    # -- version ---------------------------------------------------------------

    def tool_epoch(self) -> int:
        """Current tool-set version. Always readable: the schema seeds it at 1."""
        row = self._conn.execute(
            "SELECT value FROM kernel_meta WHERE key = ?", (TOOL_EPOCH_KEY,)
        ).fetchone()
        if row is None:
            return DEFAULT_TOOL_EPOCH
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return DEFAULT_TOOL_EPOCH

    def bump_tool_epoch(self) -> int:
        """Increment and return the new version. Caller owns the transaction.

        2026-10-24 起公共：开关写入退役后，热重建是工具集唯一的合法变化来源，
        `assembler.rebuild` 在换装临界区内调它（C14 的漂移检测因此对 MCP 增删仍有效）。
        """
        current = self.tool_epoch() + 1
        self._conn.execute(
            "INSERT INTO kernel_meta (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "  updated_at = CURRENT_TIMESTAMP",
            (TOOL_EPOCH_KEY, str(current)),
        )
        return current

    # -- reads -----------------------------------------------------------------

    def list_plugins(self) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT plugin_id, display_name, enabled, config_json, sort_order "
            "FROM plugin WHERE retired = 0 ORDER BY sort_order, plugin_id"
        ).fetchall()
        return [
            {
                "plugin_id": row["plugin_id"],
                "display_name": row["display_name"],
                "enabled": bool(row["enabled"]),
                "config": json.loads(row["config_json"]) if row["config_json"] else None,
            }
            for row in rows
        ]

    def is_enabled(self, plugin_id: str) -> bool:
        row = self._conn.execute(
            "SELECT enabled FROM plugin WHERE plugin_id = ?", (plugin_id,)
        ).fetchone()
        return bool(row["enabled"]) if row else False

    def enabled_domains(self) -> list[str]:
        """**在册的全部域** —— 域常开（2026-10-24 权限模型收口），但**摘掉的不算**
        （`retired=1` 是用户在资料页删掉的域，界面上与模型的可见集里都不该再出现）。

        从前这里是 `WHERE enabled = 1`：操作员开关 + 角色白名单双层过滤。现在
        **白名单即唯一边界**，`enabled` 那一格退役为播种/新建的默认值；新增的 `retired`
        不是"开关"而是"清单上还写不写这个名字" —— 它同时从 `list_plugins` 与这里滤掉，
        所以界面列表、`list_domains` 工具、工具可见性闸三处读的是同一张表的同一批行。

        仍按每次调用现读而非快照：`seed_plugin_rows` 播种的新域在下一轮即可见，语义与
        "热生效"一致；反过来说，用户删掉的域在下一轮就消失，不需要重启。
        """
        rows = self._conn.execute(
            "SELECT plugin_id FROM plugin WHERE retired = 0 ORDER BY plugin_id"
        ).fetchall()
        return [str(row["plugin_id"]) for row in rows]

    def retired_domain_ids(self) -> set[str]:
        """被用户删掉的域 id 集合 —— 装配根据此**不挂**它的专属路由。

        为什么必须单独给这一格：带专属路由的富域在 `discover_specs()` 里是目录枚举，
        装配点按它挂 `router_contrib` 交回的那族路由。用户在资料页把它删了，若路由照挂，
        就出现"清单里没有、端点却还在服务"的两套真相 —— 正是本仓最忌讳的形状。读同一张
        `plugin` 表（与 list_plugins/enabled_domains 同源），把"删了"这个事实贯彻到挂载
        这一层。开机时表已由 bootstrap 播种，此处读得到。
        """
        rows = self._conn.execute("SELECT plugin_id FROM plugin WHERE retired = 1").fetchall()
        return {str(row["plugin_id"]) for row in rows}

    # -- writes ----------------------------------------------------------------

    def register(
        self,
        plugin_id: str,
        *,
        display_name: str | None = None,
        enabled: bool = False,
        sort_order: int = 0,
    ) -> None:
        """Add a plugin row if absent. Does NOT change the state of an existing row.

        Deliberately not an upsert on `enabled`: re-running bootstrap must not switch a plugin
        back on after an operator turned it off.
        """
        self._check_known(plugin_id)
        self._conn.execute(
            "INSERT INTO plugin (plugin_id, display_name, enabled, sort_order) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(plugin_id) DO NOTHING",
            (plugin_id, display_name or plugin_id, 1 if enabled else 0, sort_order),
        )
        self._conn.commit()

    def create(self, plugin_id: str, *, display_name: str | None = None) -> None:
        """用户自己新建一个**数据型域**（资料页「＋ 新建域」），不要求代码侧有目录。

        与 `register` 的唯一不同：这里**不过** `_check_known` —— 那道闸是给开机播种用的
        （"注册了 id 却忘了接工厂"的静默失败防线），而用户建的是纯数据域：通用记录通路
        （`/api/domains/{domain}/records`）只按 `domain_data` 表 + `check_domain` 的 id 形状
        工作，**本就不需要 SPEC/工具工厂/路由**（代码里已有的数据型域就是这个形状，
        只是它写死在树里；内核照样不认识任何一个域名）。

        为什么是 upsert 而不是裸 INSERT：撞一个**已退役**的同名墓碑必须就地复活
        （`retired=0`），否则 INSERT 撞主键 → 500。而"撞活着的域"由路由的 409 先挡（它读
        `list_plugins`，只含未退役的行），到得了这里的冲突只可能是墓碑 —— 所以这一格
        `DO UPDATE ... retired=0` 只做"复活"，永远不会覆盖一个现役域的行。
        复活即 `enabled=1`：与新建/播种行一致（`enabled` 现为默认值格，读侧已不看它）。
        """
        self._conn.execute(
            "INSERT INTO plugin (plugin_id, display_name, enabled, retired, sort_order) "
            "VALUES (?, ?, 1, 0, 0) "
            "ON CONFLICT(plugin_id) DO UPDATE SET retired = 0, enabled = 1, "
            "  display_name = excluded.display_name, updated_at = CURRENT_TIMESTAMP",
            (plugin_id, display_name or plugin_id),
        )
        self._conn.commit()

    def retire(self, plugin_id: str) -> int:
        """把域从清单上摘掉（资料页「删除这个域」）：置 `retired=1`，**不删行**。

        为什么是标记而不是 DELETE —— 播种用的是 `ON CONFLICT(plugin_id) DO NOTHING`，
        所以这行留着就永远不会被 `seed_plugin_rows` 重新点亮（真删行的话，代码侧的域
        下一次开机又被播回来，用户会看到"删了又活"）；而 `DOMAINS` 只枚举代码目录，
        用户自己建的 id 本来就不在播种名单里，两种域在这一格上走同一条路。
        事实源仍是这张表：`list_domains`（界面 + `list_domains` 工具）与
        `enabled_domains()`（工具可见性的那道闸）都过滤 `retired=0`，删完立刻全线隐身。

        返回被改的行数（0 = 这行不存在；重复 retire 也是 0，调用方按"没这个域"报 404）。
        """
        cur = self._conn.execute(
            "UPDATE plugin SET retired = 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE plugin_id = ? AND retired = 0",
            (plugin_id,),
        )
        rowcount = cur.rowcount
        self._conn.commit()
        return rowcount

    # -- internals -------------------------------------------------------------

    def _check_known(self, plugin_id: str) -> None:
        if self._known is not None and plugin_id not in self._known:
            known = ", ".join(self._known) or "none"
            raise UnknownPlugin(f"plugin not registered in code: {plugin_id} (known: {known})")

    def _audit(
        self, actor: str, action: str, target: str, detail: dict[str, object] | None = None
    ) -> None:
        self._trail.log(actor=actor, action=action, target=target, detail=detail)
