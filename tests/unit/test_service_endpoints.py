"""服务端点引用（service_endpoint 表）的单元测试 —— 「服务」页签引用模型的数据层。

架构归一化：模型页（model_backend）是云端配置的唯一事实面；本表只存引用 + 优先级 + 启停。
覆盖五条不变量：
  1. seed_once 一次性：重启不复活被删行；默认引用只在对应后端已存在时创建。
  2. 新增 = 引用模型页已配置的后端；一后端一服务至多一条；未配置的后端大声拒绝。
  3. 删除引用行**绝不动** model_backend 配置；后端被删后引用行呈现「失效」。
  4. builtin 本地实现不可删，可停用；不能停掉最后一行。
  5. reorder 必须是全排列（部分序大声拒绝）——优先级第 1 位即生效。
"""

from __future__ import annotations

import pytest

from rolecard_agent.core.model_settings import ModelSettingsService
from rolecard_agent.core.services import ServiceEndpointService
from rolecard_agent.storage.db import bootstrap, connect


@pytest.fixture
def svc() -> ServiceEndpointService:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=())
    # 模型页先有一个云端配置（引用的前提）——唯一配置面的语义在测试里同样成立。
    ms = ModelSettingsService(c)
    ms.save(
        default="siliconflow",
        backends=[
            {
                "name": "siliconflow",
                "provider": "siliconflow",
                "model": "deepseek-ai/DeepSeek-V4-Flash",
                "api_key": "sk-x",
            },
            {"name": "local", "provider": "ollama", "model": "qwen2.5:7b"},
        ],
        fallbacks=[],
    )
    s = ServiceEndpointService(c)
    s.seed_once()
    return s


def test_seed_once_is_idempotent(svc: ServiceEndpointService) -> None:
    # 本地行必有；siliconflow 后端存在 → 嵌入/重排各一条默认引用
    assert [e.id for e in svc.rows("ocr")] == ["paddle"]
    assert [e.id for e in svc.rows("embedding")] == ["hash", "siliconflow"]
    assert svc.seed_once() == 0  # 重跑不重复播种


def test_seed_does_not_revive_deleted_row(svc: ServiceEndpointService) -> None:
    svc.delete("embedding", "siliconflow")
    assert [e.id for e in svc.rows("embedding")] == ["hash"]
    svc.seed_once()  # flag 已落库：删掉的行绝不复活
    assert [e.id for e in svc.rows("embedding")] == ["hash"]


def test_seed_without_backend_creates_no_reference() -> None:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=())
    s = ServiceEndpointService(c)
    s.seed_once()  # model_backend 表为空 → 只有本地行
    assert [e.id for e in svc_rows(s, "embedding")] == ["hash"]
    assert [e.id for e in svc_rows(s, "rerank")] == ["off"]


def svc_rows(s: ServiceEndpointService, key: str) -> list:
    return s.rows(key)


def test_add_reference_selects_from_model_page(svc: ServiceEndpointService) -> None:
    svc.add("ocr", ref_backend="siliconflow")  # OCR 引用对话后端 = 只借凭据
    ids = [e.id for e in svc.rows("ocr")]
    assert ids == ["paddle", "siliconflow"]
    row = next(e for e in svc.rows("ocr") if e.id == "siliconflow")
    assert row.api_key == "sk-x"  # 凭据来自被引用后端（唯一配置面）
    # 对话后端被嵌入/OCR 引用时，模型名回落到能力默认（OCR 无模型名）
    assert row.model is None


def test_add_unknown_backend_rejected(svc: ServiceEndpointService) -> None:
    with pytest.raises(ValueError, match="不在模型页配置里"):
        svc.add("ocr", ref_backend="ghost")


def test_add_duplicate_reference_rejected(svc: ServiceEndpointService) -> None:
    with pytest.raises(ValueError, match="已在本服务中"):
        svc.add("embedding", ref_backend="siliconflow")


def test_delete_reference_keeps_model_page_config(svc: ServiceEndpointService) -> None:
    svc.delete("embedding", "siliconflow")
    # 引用行没了，但模型页配置原样保留 —— 「删除只摘引用」的核心语义
    assert all(e.id != "siliconflow" for e in svc.rows("embedding"))
    ms = ModelSettingsService(svc._conn)  # noqa: SLF001 - 测试检视同一连接
    assert any(b["name"] == "siliconflow" for b in ms.list_backends())


def test_stale_reference_is_visible_not_silent(svc: ServiceEndpointService) -> None:
    # 模型页删掉 siliconflow（重存为只剩 local）→ 引用行失效但**可见**
    ms = ModelSettingsService(svc._conn)  # noqa: SLF001
    ms.save(
        default="local",
        backends=[{"name": "local", "provider": "ollama", "model": "qwen2.5:7b"}],
        fallbacks=[],
    )
    row = next(e for e in svc.rows("embedding") if e.id == "siliconflow")
    assert row.stale is True and row.api_key is None


def test_builtin_row_cannot_be_deleted_but_can_be_disabled(
    svc: ServiceEndpointService,
) -> None:
    with pytest.raises(ValueError, match="内置本地实现"):
        svc.delete("embedding", "hash")  # ocr 只有 paddle 一行（禁用会触发最后一行守卫）
    svc.patch("embedding", "hash", enabled=False)
    assert next(e for e in svc.rows("embedding") if e.id == "hash").enabled is False


def test_cannot_disable_the_last_enabled_row(svc: ServiceEndpointService) -> None:
    svc.patch("embedding", "siliconflow", enabled=False)
    with pytest.raises(ValueError, match="至少保留一个启用"):
        svc.patch("embedding", "hash", enabled=False)


def test_reorder_requires_full_permutation(svc: ServiceEndpointService) -> None:
    with pytest.raises(ValueError, match="全部端点"):
        svc.reorder("embedding", ["siliconflow"])  # 部分序：大声拒绝
    svc.reorder("embedding", ["siliconflow", "hash"])  # 云端提到第 1 位
    assert [e.id for e in svc.ordered_candidates("embedding")] == ["siliconflow", "hash"]


def test_unknown_category_rejected(svc: ServiceEndpointService) -> None:
    with pytest.raises(ValueError, match="未知服务类别"):
        svc.add("teleport", ref_backend="siliconflow")
