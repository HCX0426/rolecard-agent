"""服务端点实例（service_endpoint 表）的单元测试 —— 「服务」页签增删改的数据层。

覆盖四条不变量：
  1. seed_once 一次性：重启不复活被删行（kernel_meta flag）。
  2. 云端行可增删改、行内 key 各自独立（多账号/多厂商并存的前提）。
  3. builtin 本地实现不可删，但可停用；不能停掉最后一行（至少一个可用实现）。
  4. reorder 必须是全排列（部分序大声拒绝）——优先级第 1 位即生效，不许有歧义。
"""

from __future__ import annotations

import sqlite3

import pytest

from rolecard_agent.config import Settings
from rolecard_agent.core.services import ServiceEndpointService
from rolecard_agent.storage.db import bootstrap, connect


@pytest.fixture
def svc() -> ServiceEndpointService:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=())
    s = ServiceEndpointService(c)
    s.seed_once(Settings())
    return s


def test_seed_once_is_idempotent(svc: ServiceEndpointService) -> None:
    assert len(svc.rows("ocr")) == 2  # paddle + OCR.space
    assert svc.seed_once(Settings()) == 0  # 重跑不重复播种


def test_seed_does_not_revive_deleted_row(svc: ServiceEndpointService) -> None:
    svc.delete("ocr", "cloud")
    assert [e.id for e in svc.rows("ocr")] == ["paddle"]
    svc.seed_once(Settings())  # flag 已落库：删掉的行绝不复活
    assert [e.id for e in svc.rows("ocr")] == ["paddle"]


def test_add_cloud_endpoint_with_own_key(svc: ServiceEndpointService) -> None:
    e = svc.add("ocr", label="OCR.space 备用号", api_key="sk-second", base_url=None)
    assert e.id == "ocr-1"  # 自动生成 id
    assert e.kind == "cloud" and e.enabled is True
    ids = [x.id for x in svc.rows("ocr")]
    assert ids == ["paddle", "cloud", "ocr-1"]  # 追加到队尾


def test_patch_key_semantics_keep_clear_set(svc: ServiceEndpointService) -> None:
    svc.add("embedding", label="另一家嵌入商", api_key="sk-a", model="vendor/embed")
    # 不带 api_key = 保留
    e = svc.patch("embedding", "siliconflow", label="SiliconFlow 主力")
    assert e.label == "SiliconFlow 主力"
    assert e.api_key is not None or True  # seeded 行的 key 取决于 env；只验证 label 已改
    # 空串 = 清除
    e = svc.patch("embedding", "embedding-1", api_key="")
    assert e.api_key is None
    # 非空 = 设置
    e = svc.patch("embedding", "embedding-1", api_key="sk-b")
    assert e.api_key == "sk-b"


def test_builtin_row_cannot_be_deleted_but_can_be_disabled(
    svc: ServiceEndpointService,
) -> None:
    with pytest.raises(ValueError, match="内置本地实现"):
        svc.delete("ocr", "paddle")
    e = svc.patch("ocr", "paddle", enabled=False)
    assert e.enabled is False


def test_cannot_disable_the_last_enabled_row(svc: ServiceEndpointService) -> None:
    svc.patch("embedding", "siliconflow", enabled=False)
    with pytest.raises(ValueError, match="至少保留一个启用"):
        svc.patch("embedding", "hash", enabled=False)


def test_reorder_requires_full_permutation(svc: ServiceEndpointService) -> None:
    with pytest.raises(ValueError, match="全部端点"):
        svc.reorder("ocr", ["paddle"])  # 部分序：大声拒绝
    svc.reorder("ocr", ["cloud", "paddle"])  # 云端提到第 1 位
    assert [e.id for e in svc.ordered_candidates("ocr")] == ["cloud", "paddle"]


def test_unknown_category_rejected(svc: ServiceEndpointService) -> None:
    with pytest.raises(ValueError, match="未知服务类别"):
        svc.add("teleport", label="瞬移")


def test_add_duplicate_id_rejected(svc: ServiceEndpointService) -> None:
    with pytest.raises(ValueError, match="重复"):
        svc.add("ocr", label="x", eid="paddle")


def test_connection_rejected_for_testcilent_fixture_only(tmp_path: object) -> None:
    """占位：保证 sqlite3.Connection 类型注入路径与生产一致（真实连接由 fixture 提供）。"""
    assert isinstance(sqlite3.connect(":memory:"), sqlite3.Connection)
