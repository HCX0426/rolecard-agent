from __future__ import annotations

from rolecard_agent.config import ModelBackend, Settings
from rolecard_agent.core.model_settings.read import _ReadMixin
from rolecard_agent.core.model_settings.rows import _backend_from_row


class _EffectiveMixin(_ReadMixin):
    def effective_settings(self, env_settings: Settings, *, user_id: str) -> Settings:
        """DB rows are the single source of truth once seeded.

        **这一句 `user_id` 就是"谁的 key 被花出去"的唯一答案**（M2d，§4.1）：拼出来的是
        那个人名下的后端集，别人的组根本进不来，所以运行时不存在"要不要检查这把 key 是不是
        他的"这一问 —— 图与工厂拿到的 `Settings` 里压根没有别人的凭据。咽喉只在这一处，
        也就是 `core/agent/graph.py` 那句 `backend.api_key` 之上再没有第二道判断要写。
        本机单身份时这个参数恒等于"这台实例的主人"，形状与拆层前一致。

        H5 修复：表非空后**不再并入 env 后端**。此前 `merged = dict(env_settings.model_backends)`
        会把"UI 删掉、但 env 仍提供"的后端重新复活，与 `seed_from_env` 文档（首启后 env 出局、
        UI 删除的后端保持删除）直接矛盾。现在：表空 → 退回 env（首启前 bootstrap）；表非空 →
        仅以 DB 行为准，env 改动（首启后）一律忽略。
        """
        raw = self._raw_backends(user_id=user_id)
        if not raw:
            return env_settings
        # 值列不在这里抄清单（S-1）：`_backend_from_row` 按 `_COLUMN_READERS` 逐列读，
        # 加一列只改 schema 声明 + 补一个读取器。这里从前手写十一个字段，漏一个不会红。
        merged: dict[str, ModelBackend] = {str(row["name"]): _backend_from_row(row) for row in raw}
        # 默认/回退链按**这个人**的 chat 引用行取（多租户 B1b，方案 A）：这份配置是谁的 key
        # 谁说话，对话默认就该是那个人的第一条 chat 引用。别人名下的序列对这里不存在。
        default = self.default_backend(user_id=user_id)
        # 默认缺失/失效 → 退到 DB 第一个后端（首启种子已保证至少一个 chat 后端）。
        if default is None or default not in merged:
            default = next(iter(merged), env_settings.model_default)
        fallbacks = self.list_fallbacks(user_id=user_id)
        return env_settings.model_copy(
            update={
                "model_backends": merged,
                "model_default": default,
                "model_fallbacks": fallbacks
                if fallbacks is not None
                else env_settings.model_fallbacks,
            }
        )


# ------------------------------------------------------------------ 派生与三态小工具
