"""`probes.vision_capability` 的三态（P1-2 的探测器）。

这条探测存在的**全部价值**就在三态上：把"问不到"当成"不能看"，就会误杀能看图的模型
（老版本 Ollama 没 `capabilities` 字段、超时、模型没装、服务在重启）。所以这里一半的用例
在钉"什么时候**不**给 False"。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from rolecard_agent.core import probes


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture(autouse=True)
def _no_cache() -> Any:
    """每个用例都从空缓存开始（模块级 TTL 缓存会跨用例串味）。"""
    probes._CAP_CACHE.clear()
    yield
    probes._CAP_CACHE.clear()


def _install(monkeypatch: pytest.MonkeyPatch, responder: Any) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any] | None = None, **_kw: Any) -> _Response:
        seen.append({"url": url, "body": json or {}})
        return responder(url)

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def test_capabilities_listing_vision_reads_true(monkeypatch: pytest.MonkeyPatch) -> None:
    # 2026-09-20 本机 qwen3-vl:8b 的真实形状
    shapes = [
        {"capabilities": ["tools", "thinking", "completion", "vision"]},
        {"capabilities": ["VISION"]},  # 大小写不敏感
    ]
    for payload in shapes:
        probes._CAP_CACHE.clear()
        _install(monkeypatch, lambda _u, p=payload: _Response(p))
        assert probes.vision_capability("http://127.0.0.1:11434", "m") is True


def test_capabilities_without_vision_reads_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, lambda _u: _Response({"capabilities": ["completion", "tools"]}))
    assert probes.vision_capability("http://127.0.0.1:11434", "m") is False


def test_missing_field_is_unknown_not_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """老版本 Ollama 不返回 capabilities：这是"不知道"，绝不能报 False。"""
    _install(monkeypatch, lambda _u: _Response({"details": {"family": "llama"}}))
    assert probes.vision_capability("http://127.0.0.1:11434", "m") is None


def test_transport_failure_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_url: str, **_kw: Any) -> _Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", boom)
    assert probes.vision_capability("http://127.0.0.1:11434", "m") is None


def test_probe_is_cached_per_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """带 TTL 缓存：同一个模型问一次就够（每轮对话都问 = 每轮多一次 HTTP）。"""
    seen = _install(monkeypatch, lambda _u: _Response({"capabilities": ["vision"]}))
    base = "http://127.0.0.1:11434"
    assert probes.vision_capability(base, "a") is True
    assert probes.vision_capability(base, "a") is True
    assert len(seen) == 1
    assert probes.vision_capability(base, "b") is True  # 换模型 → 重新问
    assert len(seen) == 2
    assert probes.vision_capability(base, "b", use_cache=False) is True
    assert len(seen) == 3  # 强制重探（"刷新状态"类操作用）


def test_blank_base_url_falls_back_to_default_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install(monkeypatch, lambda _u: _Response({"capabilities": ["vision"]}))
    probes.vision_capability(None, "m")
    assert seen[0]["url"].startswith("http://127.0.0.1:11434")
