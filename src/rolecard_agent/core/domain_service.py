"""域查询服务抽象（core 层，向下无依赖）。

M9：解耦 `AppContext` 对具体域查询服务类的硬编码。`deps.py` / routers 只依赖本协议，
不直接 import 任何 `domains/<x>` 具体域模块（否则 api 层重新耦合到具体域，与
`registry.build_registry` 的"API 层绝不 import 具体域"约定相悖）。各域查询服务实现本协议
（结构化匹配，无需显式继承）即可被注入；内核只持本抽象。内核源码亦不出现具体域的
专有名词（L1：分层 token 卫生，一致性脚本机器校验）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class DomainQueryService(Protocol):
    """一个域查询服务对外暴露的读写契约（结构化匹配即可，无需显式继承）。"""

    def create_report(
        self,
        *,
        user_id: str,
        report_type: str,
        check_time: str,
        institution: str | None = None,
        note: str | None = None,
        indices: Sequence[dict[str, object]],
    ) -> str:
        """原子地插入一份报告及其指标行，返回报告 id。"""
        ...

    def list_records(self, user_id: str) -> list[dict[str, object]]:
        """列出某用户的全部报告（含指标行）。"""
        ...

    def get_record(self, *, user_id: str, report_id: str) -> dict[str, object] | None:
        """按 id 取单份报告；不存在返回 None。"""
        ...

    def update_index(
        self, *, user_id: str, index_id: str, changes: dict[str, object]
    ) -> dict[str, object]:
        """应用操作员对某条指标的修正，返回新状态。"""
        ...

    def delete_index(self, *, user_id: str, index_id: str) -> None:
        """删除一条指标行。"""
        ...

    def delete_report(self, *, user_id: str, report_id: str) -> None:
        """删除整份报告（含其指标行）。"""
        ...
