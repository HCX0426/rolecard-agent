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

# 引用行解析凭据时的那个主人（M2d）= 这台实例默认那份。
OWNER = "local-user"


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
        fallbacks=[], user_id=OWNER,
    )
    s = ServiceEndpointService(c, owner=OWNER)
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
    s = ServiceEndpointService(c, owner=OWNER)
    s.seed_once()  # model_backend 表为空 → 只有本地行
    assert [e.id for e in svc_rows(s, "embedding")] == ["hash"]
    assert [e.id for e in svc_rows(s, "rerank")] == ["off"]


def svc_rows(s: ServiceEndpointService, key: str) -> list:
    return s.rows(key)


def test_add_reference_selects_from_model_page(svc: ServiceEndpointService) -> None:
    svc.add("ocr", ref_backend="siliconflow")  # OCR 引用对话后端 = 借凭据 + 用它的多模态模型
    ids = [e.id for e in svc.rows("ocr")]
    assert ids == ["paddle", "siliconflow"]
    row = next(e for e in svc.rows("ocr") if e.id == "siliconflow")
    assert row.api_key == "sk-x"  # 凭据来自被引用后端（唯一配置面）
    # OCR 没有能力默认模型：引用对话后端时**保留其模型名**（视觉 LLM 直读图片），
    # 一个后端同时服务对话与视觉 OCR，无需重复建行（用户 2026-09-17）。
    assert row.model == "deepseek-ai/DeepSeek-V4-Flash"


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
    assert any(b["name"] == "siliconflow" for b in ms.list_backends(user_id=OWNER))


def test_stale_reference_is_visible_not_silent(svc: ServiceEndpointService) -> None:
    # 模型页删掉 siliconflow（重存为只剩 local）→ 引用行失效但**可见**
    ms = ModelSettingsService(svc._conn)  # noqa: SLF001
    ms.save(
        default="local",
        backends=[{"name": "local", "provider": "ollama", "model": "qwen2.5:7b"}],
        fallbacks=[], user_id=OWNER,
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


def test_vision_reference_row_probes_the_model(
    svc: ServiceEndpointService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """视觉 OCR 引用行（usage=ocr 的后端引用）的可用性 = 探测模型是否在位。

    回归：探测原语此前只认识 paddle/hash/off，视觉行被判"未知本地实现/不可用"，
    而选择器实际会去试 —— 服务页状态与选择行为自相矛盾（用户 2026-09-17 反馈）。
    修复后判定共用 core/probes.vision_model_ready，这里 monkeypatch 它证明接线：
    探测 True → 行可用；False → 给出"模型未加载"的可读原因。
    """
    from rolecard_agent.core import probes
    from rolecard_agent.core.services import EndpointConfig, endpoint_available

    # 直接构造视觉引用行（不依赖 Ollama）：frozen dataclass，字段即 to_api 的来源。
    vision = EndpointConfig(
        id="local_vl", label="local_vl", kind="local", base_url="http://127.0.0.1:11434",
        api_key=None, model="qwen3-vl:8b", enabled=True, builtin=False,
        ref_backend="local_vl", stale=False,
    )
    monkeypatch.setattr(probes, "vision_model_ready", lambda *a, **k: True)
    ok, reason = endpoint_available(vision, _settings_stub())
    assert ok and "qwen3-vl:8b" in reason

    monkeypatch.setattr(probes, "vision_model_ready", lambda *a, **k: False)
    ok, reason = endpoint_available(vision, _settings_stub())
    assert not ok and "不可达或未加载" in reason


def _settings_stub():
    from rolecard_agent.config import Settings

    return Settings()


def test_unknown_category_rejected(svc: ServiceEndpointService) -> None:
    """未知类别抛 **KeyError**（接入层据此回 404），不是 ValueError。

    异常类型是契约的一部分：路由里 `except KeyError → 404` 与 `except ValueError → 400`
    两条分支必须各自可达，否则 404 那条就是死代码（代码审查报告（第二轮）补服务端点
    测试时发现它们此前永远走不到）。
    """
    with pytest.raises(KeyError, match="未知服务类别"):
        svc.add("teleport", ref_backend="siliconflow")
    with pytest.raises(KeyError, match="未知服务类别"):
        svc.patch("teleport", "hash", enabled=True)
    with pytest.raises(KeyError, match="未知服务类别"):
        svc.delete("teleport", "hash")
    with pytest.raises(KeyError, match="未知服务类别"):
        svc.reorder("teleport", ["hash"])


def test_unknown_endpoint_is_keyerror_but_rule_violations_are_valueerror(
    svc: ServiceEndpointService,
) -> None:
    """**找不到** vs **规则不允许**：前者 404、后者 400，两者不能混。"""
    with pytest.raises(KeyError, match="不存在端点"):
        svc.patch("embedding", "ghost", enabled=True)
    with pytest.raises(KeyError, match="不存在端点"):
        svc.delete("embedding", "ghost")
    # 规则不允许 → ValueError（400）。ocr 只有 paddle 一行，停掉它就等于该类服务没有实现。
    with pytest.raises(ValueError, match="至少保留一个"):
        svc.patch("ocr", "paddle", enabled=False)
    with pytest.raises(ValueError, match="不可删除"):
        svc.delete("embedding", "hash")
    with pytest.raises(ValueError, match="全部端点"):
        svc.reorder("embedding", ["hash"])  # 少了一个 → 不是全排列
