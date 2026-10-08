"""通用领域数据服务层（core/domain/domain_data.py，H4）的单元测试。

验证路由下沉到服务层后的契约：校验/增删改查 + 异常类型语义
（找不到 = KeyError → 404；规则不允许 = ValueError → 400）。
"""

from __future__ import annotations

import pytest

from rolecard_agent.core.domain.domain_data import DomainDataService
from rolecard_agent.storage.db import bootstrap, connect


@pytest.fixture
def svc() -> DomainDataService:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=())
    # domain_data.user_id → app_user → tenant 外键链；测试里种一个 demo 租户/用户。
    c.executescript(
        "INSERT INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');"
        "INSERT INTO app_user (user_id, tenant_id, display_name) "
        "VALUES ('u1', 't1', 'demo user');"
    )
    c.commit()
    return DomainDataService(c)


def test_check_domain_rejects_illegal_id(svc: DomainDataService) -> None:
    with pytest.raises(ValueError, match="非法域 id"):
        svc.check_domain("1bad")
    with pytest.raises(ValueError):
        svc.check_domain("../x")
    assert svc.check_domain("finance") == "finance"


def test_create_and_list(svc: DomainDataService) -> None:
    row = svc.create_record(
        "finance", "u1", label="收入", value_text=None,
        value_num=1000.5, unit="元", note=None,
    )
    assert row["id"]
    assert row["label"] == "收入"
    assert row["value_num"] == 1000.5
    assert row["unit"] == "元"
    assert row["created_at"]
    listed = svc.list_records("finance", "u1")
    assert [r["id"] for r in listed] == [row["id"]]


def test_create_requires_label_and_value(svc: DomainDataService) -> None:
    with pytest.raises(ValueError, match="label 不能为空"):
        svc.create_record(
            "finance", "u1", label="  ", value_text=None, value_num=1,
            unit=None, note=None,
        )
    with pytest.raises(ValueError, match="至少填一个"):
        svc.create_record(
            "finance", "u1", label="x", value_text=None,
            value_num=None, unit=None, note=None,
        )


def test_patch_and_delete(svc: DomainDataService) -> None:
    row = svc.create_record(
        "finance", "u1", label="a", value_text=None, value_num=1,
        unit=None, note=None,
    )
    rid = row["id"]
    patched = svc.patch_record("finance", "u1", rid, {"label": "b", "value_num": 2})
    assert patched["label"] == "b"
    assert patched["value_num"] == 2
    svc.delete_record("finance", "u1", rid)
    assert svc.list_records("finance", "u1") == []


def test_missing_record_is_keyerror(svc: DomainDataService) -> None:
    # 找不到 → KeyError（路由映射 404），不是 ValueError/400。
    with pytest.raises(KeyError):
        svc.patch_record("finance", "u1", "nope", {"label": "x"})
    with pytest.raises(KeyError):
        svc.delete_record("finance", "u1", "nope")


def test_user_isolation(svc: DomainDataService) -> None:
    svc.create_record(
        "finance", "u1", label="a", value_text=None, value_num=1,
        unit=None, note=None,
    )
    assert svc.list_records("finance", "u2") == []
