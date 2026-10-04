from __future__ import annotations

from rolecard_agent.core.model_settings.migration import endpoint_key
from rolecard_agent.core.model_settings.rows import (
    _derived_usage,
    _kind_of_names,
    _opt_float,
    _sorted_usages,
    _tools_of,
    _tri_state,
    _value_columns,
    _vision_of,
)
from rolecard_agent.core.model_settings.rules import (
    CHAT_CATEGORY,
    ModelSettingsError,
    client_style,
    is_keyless_provider,
    mask_key,
    provider_label,
)
from rolecard_agent.storage.db import SqlConnection


class _ReadMixin:
    #: env 后端播种进模型设置表的幂等标志（`seed_from_env` 用，见 core/bootstrap.py）。
    MODEL_SEEDED_KEY = "model_backends_seeded"

    def __init__(self, conn: SqlConnection) -> None:
        self._conn = conn

    # -- reads -----------------------------------------------------------------
    #
    # 每个读都要求调用方交出 `user_id`，因为这一层的行有主人（M2d，§4.1「key 跟人走」）。
    # 只有少数几处**刻意不分身份**，它们都在下面单独标了原因（主键分配、启动清扫）——
    # 那种地方必须是"另一个具名方法"，不能是同一个方法传个 None：一旦 None 表示"全部"，
    # "忘了过滤"就又变成一次普通的调用了。
    # `service_endpoint` 那一族不再例外（多租户 B1b，方案 A 收掉了 §4.1 的尾巴）：其中
    # `chat` 引用行的默认/回退序列**按人**（`default_backend` / `list_fallbacks` /
    # `_chat_ref_names` / `_usages` 的 chat 桶都要 `user_id`）；ocr/embedding/rerank 能力
    # 端点保持设备级（那些读在 `services.py`，不归本类）。

    def _provider_rows(self, *, user_id: str) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT id, user_id, provider, label, base_url, api_key, sort_order "
            "FROM model_provider WHERE user_id = ? ORDER BY sort_order, id",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _all_group_ids(self) -> set[str]:
        """**全部**身份的组键，只用来分配新键（`_group_id` 的那个 `taken` 集合）。

        为什么全局：`model_provider.id` 是全局主键，两个身份各建一个硅基流动组时，若各自
        从 `siliconflow` 起编号就是 INSERT 撞主键（一个 500，且第二次永远建不成）。
        代价是编号会跳过别人占掉的那几个 —— 而看不见别人的组，也就看不见那些编号。
        """
        return {
            str(r["id"]) for r in self._conn.execute("SELECT id FROM model_provider").fetchall()
        }

    def _all_provider_rows(self) -> list[dict[str, object]]:
        """全部身份的凭据组 —— 只有启动时那次数据卫生清扫用它（`normalize_providers`）。
        任何"给某人看"或"替某人改"的路径都不许走这里，它们走 `_provider_rows(user_id=)`。
        """
        rows = self._conn.execute(
            "SELECT id, user_id, provider, label, base_url, api_key, sort_order "
            "FROM model_provider ORDER BY sort_order, id"
        ).fetchall()
        return [dict(r) for r in rows]

    def _all_backend_names(self) -> set[str]:
        """同 `_all_group_ids`：`model_backend.name` 也是全局主键（session/角色卡引用它）。"""
        return {
            str(r["name"]) for r in self._conn.execute("SELECT name FROM model_backend").fetchall()
        }

    def _usages(self, *, user_id: str) -> dict[str, list[str]]:
        """后端名 → 引用它的服务类别（chat 在前，其余按优先级序）。

        这就是"用途"的唯一事实面：服务页写引用行，模型页只读这张映射。
        归属切分（多租户 B1b，方案 A）：**chat 引用行只在本人的那几条里找**（"这行用于
        对话"是"花谁的 key 由谁定"同族的事实）；能力类别（ocr/embedding/rerank）是
        设备级的，与 user_id 无关、一律计入。
        """
        rows = self._conn.execute(
            "SELECT ref_backend, category FROM service_endpoint "
            "WHERE builtin = 0 AND ref_backend IS NOT NULL "
            "AND (category != 'chat' OR user_id = ?) "
            "ORDER BY CASE WHEN category = 'chat' THEN 0 ELSE 1 END, sort_order, category",
            (user_id,),
        ).fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            buckets = out.setdefault(str(row["ref_backend"]), [])
            if str(row["category"]) not in buckets:
                buckets.append(str(row["category"]))
        return out

    def _raw_backends(self, *, user_id: str) -> list[dict[str, object]]:
        """两层 JOIN 出"后端行"视图 —— 内核与 services 消费的仍是拆层前那一形状。

        `provider`/`base_url`/`api_key` 来自凭据组，`usage`/`used_by` 派生自引用行。
        凭据组按主人过滤，所以**模型行也跟着主人**：一个身份看不见别人的模型，
        连"它叫什么"都拿不到（`model_backend` 没有自己的归属列，继承自所属的组）。
        """
        # 值列清单由 `_value_columns` 算（S-1）：从前这里抄一遍列名，加一列漏一处
        # 不会红，只是那一列永远读成 None。
        b_cols = ", ".join(f"b.{c}" for c in _value_columns(self._conn))
        rows = self._conn.execute(
            f"SELECT b.name, b.provider_id, b.model, b.sort_order, {b_cols}, "
            "p.user_id, p.provider, p.label, p.base_url, p.api_key "
            "FROM model_backend b JOIN model_provider p ON p.id = b.provider_id "
            "WHERE p.user_id = ? "
            "ORDER BY b.sort_order, b.name",
            (user_id,),
        ).fetchall()
        usages = self._usages(user_id=user_id)
        out: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            used = usages.get(str(item["name"]), [])
            item["used_by"] = used
            item["usage"] = _derived_usage(used)
            out.append(item)
        return out

    def raw_backends(self, *, user_id: str) -> list[dict[str, object]]:
        """进程内配置解析用（服务引用行取凭据、工厂实例化）。

        含 api_key 明文 —— 只允许在服务层/工厂内部消费，**绝不**直接进任何 API 响应
        （对外形状见 `list_backends`：只回 has_key + 掩码）。
        """
        return self._raw_backends(user_id=user_id)

    def list_backends(self, *, user_id: str) -> list[dict[str, object]]:
        """过渡形状（对话页 / 角色页 / 旧模型页仍在消费）：NO api_key ever leaves the service.

        `usage` 现在是派生只读值；`used_by` 是它的全集（一行可同时服务多种能力）。
        """
        usages = self._usages(user_id=user_id)
        return [
            {
                k: row[k]
                for k in (
                    "name",
                    "provider",
                    "base_url",
                    "model",
                    "usage",
                    "sort_order",
                    "num_ctx",
                    "provider_id",
                )
            }
            | {
                "supports_vision": _vision_of(row["supports_vision"]),
                "supports_tools": _tools_of(row["supports_tools"]),
                "has_key": bool(row["api_key"]),
                "key_masked": mask_key(str(row["api_key"]) if row["api_key"] else None),
                "used_by": _sorted_usages(usages.get(str(row["name"]), [])),
            }
            for row in self._raw_backends(user_id=user_id)
        ]

    def list_providers(self, *, user_id: str) -> list[dict[str, object]]:
        """「模型」页签的形状：按凭据组分层的卡片数据（key 只在组头出现一次）。

        能力位是**三态**（true / false / null=没测过）—— 把"没测过"显示成"不支持"是撒谎，
        而"不支持"会触发调用前拦截。运行时那侧仍按 bool 解释（`_vision_of`/`_tools_of`）。
        """
        usages = self._usages(user_id=user_id)
        default = self.default_backend(user_id=user_id)
        models_of_group: dict[str, list[dict[str, object]]] = {}
        value_cols = ", ".join(f"b.{c}" for c in _value_columns(self._conn))
        for row in self._conn.execute(
            f"SELECT b.name, b.provider_id, b.model, {value_cols} "
            "FROM model_backend b JOIN model_provider p ON p.id = b.provider_id "
            "WHERE p.user_id = ? ORDER BY b.sort_order, b.name",
            (user_id,),
        ).fetchall():
            name = str(row["name"])
            models_of_group.setdefault(str(row["provider_id"]), []).append(
                {
                    "name": name,
                    "model": str(row["model"]),
                    "num_ctx": row["num_ctx"],
                    "supports_vision": _tri_state(row["supports_vision"]),
                    "supports_tools": _tri_state(row["supports_tools"]),
                    # 采样惩罚现值（null = 没设 = 引擎默认）。对话页那一栏要回显它，
                    # 否则"我上次设了什么"在界面上看不见 —— 看不见的设置就是没人管的设置。
                    "repeat_penalty": _opt_float(row["repeat_penalty"]),
                    "frequency_penalty": _opt_float(row["frequency_penalty"]),
                    "presence_penalty": _opt_float(row["presence_penalty"]),
                    "used_by": _sorted_usages(usages.get(name, [])),
                    "is_default": name == default,
                }
            )
        out: list[dict[str, object]] = []
        for group in self._provider_rows(user_id=user_id):
            gid = str(group["id"])
            provider = str(group["provider"])
            out.append(
                {
                    "id": gid,
                    "provider": provider,
                    "label": str(group["label"] or provider_label(provider)),
                    "base_url": group["base_url"],
                    "style": client_style(provider),
                    "needs_key": not is_keyless_provider(provider),
                    "has_key": bool(group["api_key"]),
                    "key_masked": mask_key(str(group["api_key"]) if group["api_key"] else None),
                    "models": models_of_group.get(gid, []),
                }
            )
        return out

    def default_backend(self, *, user_id: str) -> str | None:
        """对话默认后端 = **这个人的** chat 引用行的第 1 位；None = 未配置（退回 env）。

        归属（多租户 B1b，方案 A）：默认/回退链回答"这次对话花谁的 key 由谁定"，按人过滤。
        别人名下的 chat 引用对这个人不存在 —— 界面的"当前默认"对得上实际跑的那台。
        """
        row = self._conn.execute(
            "SELECT id FROM service_endpoint WHERE category = ? AND user_id = ? "
            "ORDER BY sort_order, id LIMIT 1",
            (CHAT_CATEGORY, user_id),
        ).fetchone()
        return str(row["id"]) if row else None

    def stored_api_key(self, name: str, *, user_id: str) -> str | None:
        """已保存的 key（来自该行所属的凭据组；只在本进程内使用，绝不经 API 回传）。

        别人的那一行在这里就是**不存在**：返回 None 而不是他的 key。
        """
        for row in self._raw_backends(user_id=user_id):
            if str(row["name"]) == name:
                return row["api_key"]  # type: ignore[return-value]
        return None

    def stored_group_key(self, group_id: str, *, user_id: str) -> str | None:
        """按**组**取 key（拆层后 key 不再属于单行；添加抽屉与探测端点用）。"""
        for group in self._provider_rows(user_id=user_id):
            if str(group["id"]) == group_id:
                return group["api_key"]  # type: ignore[return-value]
        return None

    def has_key_for_endpoint(self, provider: str, base_url: str | None, *, user_id: str) -> bool:
        """这个 (供应商, 端点) 是否已经有 key —— 决定"新增一行模型"要不要重输凭据。

        归一化必须与写入路径同源（都走 `endpoint_key`），否则界面上一行"看起来同一个"的
        端点会因为留空/填了默认 URL 的差别被要求重填 key。
        只看本人的组：别人在同一端点上存过 key **不构成**"我也省一次输入"—— 那是他的凭据，
        让他替我的调用付费才是更糟的那种省。
        """
        target = endpoint_key(provider, base_url)
        return any(
            group["api_key"] and endpoint_key(str(group["provider"]), group["base_url"]) == target
            for group in self._provider_rows(user_id=user_id)
        )

    def list_fallbacks(self, *, user_id: str) -> list[str] | None:
        """Operator-configured fallback chain, or None = not configured (use env's).

        派生自**这个人的** chat 引用行：第 1 位是默认（不算回退），其后就是回退链。
        一条 chat 引用都没有 = 操作员没配过 = None（env 的 `MODEL_FALLBACKS` 仍然说话）。
        """
        names = self._chat_ref_names(user_id=user_id)
        return names[1:] if names else None

    # -- chat 引用行（"这行用于对话"这件事的事实面） ---------------------------------

    def _chat_ref_names(self, *, user_id: str) -> list[str]:
        """**这个人的** chat 引用序列（按优先级序）；能力行的 category 与它有别，天然隔开。"""
        rows = self._conn.execute(
            "SELECT id FROM service_endpoint WHERE category = ? AND user_id = ? "
            "ORDER BY sort_order, id",
            (CHAT_CATEGORY, user_id),
        ).fetchall()
        return [str(r["id"]) for r in rows]

    def _write_chat_refs(self, ordered: list[str], *, user_id: str) -> None:
        """整体重写**这个人的** chat 引用行（第 1 位 = 默认，其后 = 回退链）。

        删除必须带 `user_id` 范围、插入必须带 `user_id` 值：不加这两处，A 存一次对话序列
        就会把 B 的引用行一起抹掉/写成 A 的（多租户 B1b，方案 A 的"仅 chat 引用行按人"）。
        """
        self._conn.execute(
            "DELETE FROM service_endpoint WHERE category = ? AND user_id = ?",
            (CHAT_CATEGORY, user_id),
        )
        kinds = _kind_of_names(self._conn, ordered)
        for i, name in enumerate(ordered):
            self._conn.execute(
                "INSERT INTO service_endpoint "
                "(category, id, kind, ref_backend, enabled, sort_order, builtin, user_id) "
                "VALUES (?, ?, ?, ?, 1, ?, 0, ?)",
                (CHAT_CATEGORY, name, kinds.get(name, "cloud"), name, i, user_id),
            )

    def save_chat_pool(self, names: list[str], *, user_id: str) -> None:
        """「服务」页签模型推理序列的全量写入：第 1 位 = 对话默认，其后 = 回退顺序。

        这一条就是"哪些模型用于对话"的事实面 —— 写它即定义它：列进来的行从此是 chat
        用途，没列进来的不再是（引用被删）。所以候选**不能**只给"已经是 chat 的行"，
        否则第一次加入就没有入口（拆层前的死循环：usage=chat 才能进列表，进列表才能改 usage）。

        链长不再在这里拦："最多 2 级"是运行时的截断（`Settings.resolve_fallbacks`），
        序列里第 4 位以后不参与回退，但仍然记录在案 —— 因为拖动顺序本身就是意图，
        当场拒绝对用户没有意义（他改的是第 1 位，你却告诉他"链太长"）。

        校验只对**本人的**后端集：把别人的模型名塞进对话序列会写出一个他跑得起、你跑不起
        的配置（那一名字根本不在你的有效配置里），所以它对你是 400 而不是"成功"。
        """
        if not names:
            raise ModelSettingsError("对话优先级不能为空 —— 至少要留一个用于对话的模型。")
        if len(set(names)) != len(names):
            raise ModelSettingsError("对话序列里出现了重复的模型名。")
        known = {str(row["name"]) for row in self._raw_backends(user_id=user_id)}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise ModelSettingsError(
                f"以下模型不在模型页配置里：{', '.join(unknown[:3])}（请先在「模型」页签添加）。"
            )
        self._write_chat_refs(names, user_id=user_id)
        self._conn.commit()

