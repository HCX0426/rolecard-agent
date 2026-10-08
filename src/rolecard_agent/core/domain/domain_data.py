"""通用领域数据的服务层（core 层）。

H4：把 `api/routers/domains.py` 里直写的 SQL 收拢到这里。路由只做"参数映射 + 异常→HTTP
状态码映射"——异常语义遵循项目约定：**找不到 = KeyError → 404**，**规则不允许 = ValueError → 400**，
服务层用异常类型承载区分，路由据此映射（不再自己拼 404/400 文案）。
"""

from __future__ import annotations

import re
import sqlite3
import uuid

from rolecard_agent.storage.db import SqlConnection

_DOMAIN_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_COLUMNS = "id, domain, label, value_text, value_num, unit, note, created_at"

# 可修改列的形状守卫（见 update_record 里的纵深防御）。
_COL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class DomainDataService:
    """通用领域记录（标签 + 数值/文本 + 单位 + 备注）的增删改查。"""

    def __init__(self, conn: SqlConnection) -> None:
        self._conn = conn

    @staticmethod
    def check_domain(domain: str) -> str:
        """校验域 id 合法；不合法抛 ValueError（路由映射为 400）。"""
        if not _DOMAIN_ID.match(domain):
            raise ValueError(f"非法域 id：{domain}")
        return domain

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["id"],
            "domain": row["domain"],
            "label": row["label"],
            "value_text": row["value_text"],
            "value_num": row["value_num"],
            "unit": row["unit"],
            "note": row["note"],
            "created_at": str(row["created_at"]),
        }

    def list_records(self, domain: str, user_id: str) -> list[dict[str, object]]:
        self.check_domain(domain)
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM domain_data "
            "WHERE domain = ? AND user_id = ? ORDER BY created_at DESC, id DESC",
            (domain, user_id),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def create_record(
        self,
        domain: str,
        user_id: str,
        *,
        label: str,
        value_text: str | None,
        value_num: float | None,
        unit: str | None,
        note: str | None,
    ) -> dict[str, object]:
        self.check_domain(domain)
        label = (label or "").strip()
        if not label:
            raise ValueError("label 不能为空")
        if value_text is None and value_num is None:
            raise ValueError("value_text 与 value_num 至少填一个")
        rid = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO domain_data (id, domain, user_id, label, value_text, "
            "value_num, unit, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, domain, user_id, label, value_text, value_num, unit, note),
        )
        self._conn.commit()
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM domain_data WHERE id = ?", (rid,)
        ).fetchone()
        return self._row_to_dict(row)

    def patch_record(
        self,
        domain: str,
        user_id: str,
        record_id: str,
        changes: dict[str, object],
    ) -> dict[str, object]:
        self.check_domain(domain)
        if not changes:
            raise ValueError("没有任何要更新的字段")
        row = self._conn.execute(
            "SELECT id FROM domain_data WHERE id = ? AND domain = ? AND user_id = ?",
            (record_id, domain, user_id),
        ).fetchone()
        if row is None:
            raise KeyError(record_id)
        # 纵深防御：调用方的 Pydantic 模型是 strict 的，但拼列名发生在本层 ——
        # 在这里再校验一次，调用方放宽模型时不会悄悄变成注入面（审查报告 P2）。
        for key in changes:
            if key in {"id", "domain", "user_id", "created_at"} or not _COL_NAME.match(key):
                raise ValueError(f"不可修改的字段：{key}")
        sets = ", ".join(f"{k} = ?" for k in changes)
        self._conn.execute(
            f"UPDATE domain_data SET {sets} WHERE id = ?",
            (*[changes[k] for k in changes], record_id),
        )
        self._conn.commit()
        return self._row_to_dict(
            self._conn.execute(
                f"SELECT {_COLUMNS} FROM domain_data WHERE id = ?", (record_id,)
            ).fetchone()
        )

    def delete_record(self, domain: str, user_id: str, record_id: str) -> None:
        self.check_domain(domain)
        row = self._conn.execute(
            "SELECT id FROM domain_data WHERE id = ? AND domain = ? AND user_id = ?",
            (record_id, domain, user_id),
        ).fetchone()
        if row is None:
            raise KeyError(record_id)
        self._conn.execute("DELETE FROM domain_data WHERE id = ?", (record_id,))
        self._conn.commit()
