from __future__ import annotations

import re

from rolecard_agent.config import Settings
from rolecard_agent.core.model_settings.migration import _dedupe, _group_id, endpoint_key
from rolecard_agent.core.model_settings.read import _ReadMixin
from rolecard_agent.core.model_settings.rows import _OWNED_MODEL_ROWS, _opt_float
from rolecard_agent.core.model_settings.rules import (
    ModelSettingsError,
    client_style,
    is_keyless_provider,
    normalize_provider,
    validate_base_url,
)


class _WriteMixin(_ReadMixin):
    def set_num_ctx(self, name: str, num_ctx: int | None, *, user_id: str) -> None:
        """只改一行的上下文窗口（对话菜单悬浮面板用）—— 名称不存在抛 KeyError（404）。

        num_ctx 语义：None = 用引擎默认；给了必须 >= 512（太小的窗口等于把历史截没）。
        **别人的那一行也抛 KeyError**：按名改配置这条路上，"不是你的"与"不存在"是同一个回答
        （名字是可枚举的短串，回 403 等于告诉他"这行存在"）。
        """
        if num_ctx is not None and num_ctx < 512:
            raise ModelSettingsError("num_ctx 不得小于 512（tokens）")
        cur = self._conn.execute(
            f"UPDATE model_backend SET num_ctx = ? WHERE name = ? AND {_OWNED_MODEL_ROWS}",
            (num_ctx, name, user_id),
        )
        if cur.rowcount == 0:
            # **先结束事务再抛**：改到 0 行的 UPDATE 同样开了一个写事务，而这一路走不到
            # commit —— 留着它，这条线程就把 RESERVED 锁一直握着，别人等 5 秒一起报
            # `database is locked`（`R102-42`；领域服务里那两处早就按同一个写法收口了，
            # 漏的是这里 —— 所以本轮给它加了断言 `dangling write txn`）。
            self._conn.rollback()
            raise KeyError(name)
        self._conn.commit()

    #: 三栏惩罚的可接受区间。**故意不给"聪明"的默认值**：出厂全 NULL = 不传 = 引擎默认
    #: （Ollama 的 repeat_penalty 自带 1.1）。区间只挡"会把输出打成人话不成人话"的数：
    #: repeat 超过 2 实测就是断句复读，负数无意义；另两项 OpenAI 兼容体的定义域就是 −2..2。
    SAMPLING_RANGES: dict[str, tuple[float, float]] = {
        "repeat_penalty": (0.0, 2.0),
        "frequency_penalty": (-2.0, 2.0),
        "presence_penalty": (-2.0, 2.0),
    }

    def set_sampling(
        self, name: str, values: dict[str, float | None], *, user_id: str
    ) -> dict[str, float | None]:
        """改一行的采样惩罚（对话菜单那一栏）。给 None = 清回"不传"，不是传 0。

        **`repeat_penalty` 只对 native（Ollama）后端收**：OpenAI 兼容体里没有这个标准字段，
        存进去工厂也不会发出去 —— 让它写进去就是"界面显示已设、实际没生效"的第二个事实面。
        界面上那一栏对云端根本不出现，这里是同一件事的后端闸门。
        """
        unknown = sorted(set(values) - set(self.SAMPLING_RANGES))
        if unknown:
            raise ModelSettingsError(f"未知的采样参数：{', '.join(unknown)}")
        row = self._conn.execute(
            "SELECT p.provider FROM model_backend b JOIN model_provider p ON p.id = b.provider_id"
            " WHERE b.name = ? AND p.user_id = ?",
            (name, user_id),
        ).fetchone()
        if row is None:
            raise KeyError(name)
        native = client_style(str(row["provider"])) == "native"
        if not native and values.get("repeat_penalty") is not None:
            raise ModelSettingsError(
                "重复惩罚只对本地 Ollama 的模型有效（OpenAI 兼容体没这个字段）"
            )
        for field, (low, high) in self.SAMPLING_RANGES.items():
            raw = values.get(field)
            if raw is None:
                continue
            if not -1e9 < float(raw) < 1e9 or not low <= float(raw) <= high:
                raise ModelSettingsError(f"{field} 得在 {low}..{high} 之间（给了 {raw}）")
        for field in self.SAMPLING_RANGES:
            if field in values:
                self._conn.execute(
                    f"UPDATE model_backend SET {field} = ? "  # noqa: S608
                    f"WHERE name = ? AND {_OWNED_MODEL_ROWS}",
                    (values[field], name, user_id),
                )
        self._conn.commit()
        return self.sampling(name, user_id=user_id)

    def sampling(self, name: str, *, user_id: str) -> dict[str, float | None]:
        """一行的三栏惩罚现值（None = 没设）。写侧的回显走它，免得前端拿旧草稿。

        回显也按主人读：不然"我设了什么"会读到别人那一行的数（同名行在两个身份下可以各有一份）。
        """
        row = self._conn.execute(
            "SELECT b.repeat_penalty, b.frequency_penalty, b.presence_penalty "
            "FROM model_backend b JOIN model_provider p ON p.id = b.provider_id"
            " WHERE b.name = ? AND p.user_id = ?",
            (name, user_id),
        ).fetchone()
        if row is None:
            raise KeyError(name)
        return {
            "repeat_penalty": _opt_float(row["repeat_penalty"]),
            "frequency_penalty": _opt_float(row["frequency_penalty"]),
            "presence_penalty": _opt_float(row["presence_penalty"]),
        }

    def normalize_providers(self) -> int:
        """启动时一次性归一化历史组的 provider，并清掉无 key 供应商误存的 key。

        幂等：归一化后的值再跑一遍不再变化。修复两类历史脏数据 ——
        ① 云端种子把 SiliconFlow 写成 provider="openai"（只记了风格没记厂商），
           设置页因此显示"供应商：openai"这种错误身份；
        ② 无 key 供应商（Ollama）被早期测试写入了无意义的占位 key。

        **这一处刻意读全部身份的行**（`_all_provider_rows`）：它是启动时的数据卫生清扫，
        不是任何人的读写视图。按主人过滤反而漏 —— 库里躺着第二个身份的脏组就没人管了，
        而他下一次看见自己的供应商名仍然是错的。它不改归属，所以清扫不构成越权。
        """
        changed = 0
        for group in self._all_provider_rows():
            old = str(group["provider"])
            base = str(group["base_url"]) if group["base_url"] else None
            norm = normalize_provider(old, base)
            drop_key = is_keyless_provider(norm) and bool(group["api_key"])
            if norm != old or drop_key:
                self._conn.execute(
                    "UPDATE model_provider SET provider = ?, api_key = ? WHERE id = ?",
                    (norm, None if drop_key else group["api_key"], str(group["id"])),
                )
                changed += 1
        if changed:
            self._conn.commit()
        return changed

    # -- writes ----------------------------------------------------------------

    def remove_model(self, name: str, *, user_id: str) -> None:
        """删一行模型（名称不存在 → KeyError/404）。

        组里没别的模型了才连凭据一起删（key 不留成"看不见的凭据"）。其余服务类别的引用行
        **保持原样**并在服务页呈现「失效」—— 摘引用与删配置是两个动作，不能顺手合并；
        chat 引用则跟着这行走，并把它的位置让给序列里的下一个（默认不能悬空）。

        别人的那一行在这里同样是 KeyError —— 而这一处比 404 的口径更要紧：不带主人过滤的
        删除会连着 `DELETE FROM model_provider` 一起走，那是**删掉他的凭据**。
        """
        row = next((r for r in self._raw_backends(user_id=user_id) if str(r["name"]) == name), None)
        if row is None:
            raise KeyError(name)
        gid = str(row["provider_id"])
        pool = [n for n in self._chat_ref_names(user_id=user_id) if n != name]
        self._conn.execute(
            f"DELETE FROM model_backend WHERE name = ? AND {_OWNED_MODEL_ROWS}", (name, user_id)
        )
        left = self._conn.execute(
            "SELECT 1 FROM model_backend WHERE provider_id = ? LIMIT 1", (gid,)
        ).fetchone()
        if left is None:
            self._conn.execute(
                "DELETE FROM model_provider WHERE id = ? AND user_id = ?", (gid, user_id)
            )
        # 引用行按新序重编 sort_order，所以删掉的正是默认时，下一位自动顶上（默认不会悬空）。
        self._write_chat_refs(pool, user_id=user_id)
        self._conn.commit()

    def set_capabilities(
        self, name: str, capabilities: dict[str, bool | None], *, user_id: str
    ) -> None:
        """写回探测结论（三态）。字典里**出现**的键才写，缺席的键不动。

        为什么按"键在不在"而不是"值是不是 None"：`None` 在这三态里是一个**有内容的结论**
        ("没测过" → 界面 `?`)。把 None 当"没提交"，PATCH 就永远没法把 `✗` 改回 `?`。
        """
        if not self._has_backend(name, user_id=user_id):
            raise KeyError(name)
        unknown = set(capabilities) - {"supports_vision", "supports_tools"}
        if unknown:
            raise ModelSettingsError(f"未知能力位：{', '.join(sorted(unknown))}")
        if not capabilities:
            raise ModelSettingsError("没有要写的 capability。")
        columns = {
            field: None if value is None else int(bool(value))
            for field, value in capabilities.items()
        }
        # 参数化列名来自白名单（`unknown` 已经挡掉其它键），不是用户输入。
        assignments = ", ".join(f"{field} = ?" for field in columns)
        self._conn.execute(
            f"UPDATE model_backend SET {assignments} WHERE name = ? AND {_OWNED_MODEL_ROWS}",
            (*columns.values(), name, user_id),
        )
        self._conn.commit()

    def _has_backend(self, name: str, *, user_id: str) -> bool:
        """这一行**在这个人眼里**存在吗（别人的行 = 不存在，不是"存在但你不能碰"）。"""
        return (
            self._conn.execute(
                f"SELECT 1 FROM model_backend WHERE name = ? AND {_OWNED_MODEL_ROWS}",
                (name, user_id),
            ).fetchone()
            is not None
        )

    def _free_name(self, group_id: str, provider: str, model: str) -> str:
        """由 (供应商, 模型名) 生成一个合法且未占用的配置名，例如 `siliconflow-qwen3-vl-30b`。

        `taken` 是**全局**的（`_all_backend_names`）：主键全局，按人取会生成一个撞别人
        已占名字的键，症状是 INSERT 抛 IntegrityError —— 而这条路径是"用户没填名字"，
        他不该为一次看不见的主键冲突负责。
        """
        slug = re.sub(r"[^a-z0-9]+", "-", f"{provider}-{model}".lower()).strip("-")
        base = slug[:28].rstrip("-") or "model"
        taken = self._all_backend_names()
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    # -- merge -------------------------------------------------------------------
    def seed_from_env(self, env_settings: Settings, *, user_id: str) -> int:
        """First-boot migration: copy env backends into the tables ONCE, then env is out of
        the loop — the settings UI (these tables) is the single source of truth afterwards.

        The `model_backends_seeded` flag makes the migration one-way: a backend the operator
        deletes in the UI stays deleted even if env still provides it, and env edits after
        the first boot are deliberately ignored. 迁移是一次性的，这正是"以后都在界面配置"
        的含义。

        env 的后端按 (供应商, base_url) 归并成凭据组（同一端点的多个模型共用一把 key），
        `usage='chat'` 的行同时播 chat 引用，env 的默认后端排第 1 位。

        **种子有主人 = 这台实例的主人**（M2d）：env 里的 key 是"这个进程带着的凭据"，它不属于
        库里任何一个登录者。播种闸（`kernel_meta` 那个 flag）也因此是实例级的 —— 第二个身份
        来了不重播 env，他在界面上自己填 key。
        """
        flag = self._conn.execute(
            "SELECT value FROM kernel_meta WHERE key = ?", (self.MODEL_SEEDED_KEY,)
        ).fetchone()
        if flag is not None:
            return 0

        existing = {str(r["name"]) for r in self._raw_backends(user_id=user_id)}
        groups = {
            endpoint_key(str(g["provider"]), g["base_url"]): str(g["id"])
            for g in self._provider_rows(user_id=user_id)
        }
        taken = self._all_group_ids()
        inserted = 0
        chat_rows: list[str] = []
        for name, backend in env_settings.model_backends.items():
            if name in existing:
                continue
            catalog = normalize_provider(backend.provider, backend.base_url)
            base_url = validate_base_url(backend.base_url)
            endpoint = endpoint_key(catalog, base_url)
            if endpoint not in groups:
                gid = _group_id(taken, catalog)
                self._conn.execute(
                    "INSERT INTO model_provider "
                    "(id, user_id, provider, base_url, api_key, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        gid,
                        user_id,
                        catalog,
                        base_url,
                        None if is_keyless_provider(catalog) else backend.api_key,
                        len(taken) - 1,
                    ),
                )
                groups[endpoint] = gid
            self._conn.execute(
                "INSERT OR IGNORE INTO model_backend "
                "(name, provider_id, model, sort_order, num_ctx, supports_vision, "
                "supports_tools) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    groups[endpoint],
                    backend.model,
                    len(existing) + inserted,
                    backend.num_ctx,
                    int(backend.supports_vision),
                    int(backend.supports_tools),
                ),
            )
            if backend.usage == "chat":
                chat_rows.append(name)
            inserted += 1
        if chat_rows:
            ordered = _dedupe(
                [env_settings.model_default, *env_settings.model_fallbacks, *chat_rows],
                set(chat_rows),
            )
            self._write_chat_refs(ordered, user_id=user_id)
        self._conn.execute(
            "INSERT OR IGNORE INTO kernel_meta (key, value) VALUES (?, ?)",
            (self.MODEL_SEEDED_KEY, "1"),
        )
        self._conn.commit()
        return inserted
