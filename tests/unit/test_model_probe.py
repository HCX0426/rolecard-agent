"""探测原语（core/probes/model_probe.py）的用例：全部走假 httpx，一次真请求都不发。

钉的是四条承诺：

  1. **默认不花钱、不出机**：`probe()` 不带 flag 时只打列表端点，`calls_used=0`，
     请求体里绝不出现 `image_url`（图片外发是红线，见 docs/archive/模型接入设计稿.md §3）；
  2. **三态是结论不是猜**：探测失败 → `None`（界面 `?`），不是 `False`（那会误杀能用的模型）；
  3. **凭据不上网络**：只给 `provider_id` 就能定位到已存组的 key（服务端进程内取用）；
  4. **临时端点也要过校验**：`file://` 之类的 base_url 直接拒（M8 的 SSRF 口径）。
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from typing import Any

import pytest

from rolecard_agent.core.model_settings import ModelSettingsError, ModelSettingsService
from rolecard_agent.core.probes import model_probe
from rolecard_agent.core.probes.model_probe import (
    ProbeTarget,
    list_models,
    probe,
    resolve_target,
)
from rolecard_agent.core.telemetry.probes import _CAP_CACHE  # noqa: PLC2701 - 探针缓存要用例自己清
from rolecard_agent.storage.db import bootstrap, connect

# 探测花的是谁的凭据（M2d）：主人不交出来就连自己的组也查不到。
OWNER = "local-user"


class _Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self) -> object:
        return self._payload


class _Calls:
    """记下每次请求，断言"发了什么"而不是只看返回值。"""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.gets: list[tuple[str, dict[str, str]]] = []
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def _reply(self, url: str) -> _Response:
        for key, value in self.replies.items():
            if key in url:
                return value if isinstance(value, _Response) else _Response(value)
        return _Response({"error": "unexpected url"}, status_code=404)

    # `**_` 是必须的：真 httpx 的 kwargs 会比这里列的多（例如 `trust_env=False`，
    # 见台账 R26-43）。假客户端跟着真签名走，才不会把"代码加了一个无害参数"变成一片红。
    def get(
        self, url: str, headers: dict[str, str] | None = None, timeout: float = 0, **_: object
    ) -> _Response:
        self.gets.append((url, headers or {}))
        return self._reply(url)

    def post(
        self,
        url: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 0,
        **_: object,
    ) -> _Response:
        self.posts.append((url, json or {}, headers or {}))
        return self._reply(url)


@pytest.fixture
def calls(request: pytest.FixtureRequest) -> Any:
    replies = getattr(request, "param", {})
    fake = _Calls(replies)
    import httpx

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(httpx, "post", fake.post)
    yield fake
    monkeypatch.undo()


def _cloud(**over: Any) -> ProbeTarget:
    base = {
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "sk-shared",
        "model": "Qwen/Qwen3-VL-30B-A3B-Instruct",
    }
    base.update(over)
    return ProbeTarget(**base)  # type: ignore[arg-type]


def _local(**over: Any) -> ProbeTarget:
    base = {"provider": "ollama", "base_url": "http://localhost:11434", "api_key": None,
            "model": "qwen3-vl:8b"}
    base.update(over)
    return ProbeTarget(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- 列表


def test_list_models_native_reads_api_tags(calls: Any) -> None:
    calls.replies = {"/api/tags": {"models": [{"name": "qwen3-vl:8b"}]}}
    names, error = list_models(_local())
    assert names == ["qwen3-vl:8b"] and error == ""
    assert calls.gets[0][0] == "http://localhost:11434/api/tags"
    # 本地端点即使组里存了 key 也不发（多一个 Authorization 头等于把凭据交给本地端口）
    assert calls.gets[0][1] == {}


def test_list_models_cloud_sends_bearer(calls: Any) -> None:
    calls.replies = {"/models": {"data": [{"id": "a"}, {"id": "b"}]}}
    names, error = list_models(_cloud())
    assert names == ["a", "b"] and error == ""
    assert calls.gets[0][0] == "https://api.siliconflow.cn/v1/models"
    assert calls.gets[0][1] == {"Authorization": "Bearer sk-shared"}


def test_list_models_reports_failure_as_text(calls: Any) -> None:
    calls.replies = {"/models": _Response({"detail": "bad key"}, status_code=401)}
    names, error = list_models(_cloud())
    assert names == [] and "401" in error


# ---------------------------------------------------------------------- probe：门禁


def test_probe_default_costs_nothing_and_sends_no_image(calls: Any) -> None:
    """不点名就不测工具、不测视觉：只打一次免费的列表端点。"""
    calls.replies = {"/models": {"data": [{"id": _cloud().model}]}}
    result = probe(_cloud())
    assert result.reachable is True and result.model_listed is True
    assert result.calls_used == 0
    assert result.tools is None and result.vision is None
    assert result.vision_source == "not-tested"
    assert calls.posts == []  # 一次推理请求都没发出去


def test_probe_tool_capability_reads_tool_calls(calls: Any) -> None:
    calls.replies = {
        "/models": {"data": [{"id": _cloud().model}]},
        "/chat/completions": {"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]},
    }
    result = probe(_cloud(), test_tools=True)
    assert result.tools is True and result.calls_used == 1
    url, body, headers = calls.posts[0]
    assert url == "https://api.siliconflow.cn/v1/chat/completions"
    assert body["tools"][0]["function"]["name"] == "probe_get_time"
    assert headers["Authorization"] == "Bearer sk-shared"


def test_probe_tool_verdict_is_a_tri_state_not_a_guess(calls: Any) -> None:
    """有内容但没调工具 = **不知道**（它完全可以只是直接回答）。

    这条是误伤防线：写回 `supports_tools=false` 会让 `call_model` 从此不给这个模型绑工具，
    一个健康模型被一次走运的探测误标成"不支持"，比不测更难查（与 P1-2 同一条纪律）。
    """
    calls.replies = {
        "/models": {"data": [{"id": _cloud().model}]},
        "/chat/completions": {"choices": [{"message": {"content": "现在下午三点。"}}]},
    }
    assert probe(_cloud(), test_tools=True).tools is None


def test_probe_tool_empty_reply_is_a_confident_no(calls: Any) -> None:
    """要探的那个真实故障：一附工具就**空返回**（既没内容也没 tool_calls）→ False。"""
    calls.replies = {
        "/models": {"data": [{"id": _cloud().model}]},
        "/chat/completions": {"choices": [{"message": {"content": ""}}]},
    }
    assert probe(_cloud(), test_tools=True).tools is False


def test_probe_vision_uploads_only_a_miniature_when_explicitly_asked(calls: Any) -> None:
    calls.replies = {
        "/models": {"data": [{"id": _cloud().model}]},
        "/chat/completions": {"choices": [{"message": {"content": "红色"}}]},
    }
    result = probe(_cloud(), test_vision=True)
    assert result.vision is True and result.vision_source == "uploaded-image"
    content = calls.posts[0][1]["messages"][0]["content"]
    data_url = next(p["image_url"]["url"] for p in content if p.get("type") == "image_url")
    assert data_url.startswith("data:image/png;base64,")
    # 外发的就是那张 16×16 纯色图，不是任何用户内容（几百字节，没有任何想象空间）。
    assert len(data_url) < 400


def test_probe_vision_rejection_is_a_confident_no(calls: Any) -> None:
    """端点**拒收**图片 = 确定的不支持（写回 `False` 会真的拦下生图请求）。"""
    calls.replies = {
        "/models": {"data": [{"id": _cloud().model}]},
        "/chat/completions": _Response({"error": "images not supported"}, status_code=400),
    }
    result = probe(_cloud(), test_vision=True)
    assert result.vision is None  # HTTP 失败按"不知道"处理，不当"不能"
    assert "视觉探测失败" in result.detail


def test_probe_local_vision_is_free_metadata(calls: Any) -> None:
    """Ollama 的视觉结论来自 /api/show，免费且不出机 —— 不要求 test_vision 也给结论。"""
    calls.replies = {
        "/api/tags": {"models": [{"name": "qwen3-vl:8b"}]},
        "/api/show": {"capabilities": ["tools", "vision"]},
    }
    _CAP_CACHE.clear()  # 探针有 10s TTL 缓存：用例之间必须各测各的
    result = probe(_local())
    assert result.vision is True and result.vision_source == "free-metadata"
    assert result.calls_used == 0
    assert any("/api/show" in url for url, _, _ in calls.posts)
    assert not any("chat/completions" in url for url, _, _ in calls.posts)


def test_probe_local_vision_unknown_stays_unknown(calls: Any) -> None:
    """老版本 Ollama 没有 capabilities 字段 → None（"不知道"），不是 False。"""
    calls.replies = {
        "/api/tags": {"models": [{"name": "m"}]},
        "/api/show": {"model_info": {}},
    }
    _CAP_CACHE.clear()
    result = probe(_local(model="m"))
    assert result.vision is None and result.vision_source == "free-metadata"


def test_probe_unreachable_endpoint_carries_the_reason(calls: Any) -> None:
    calls.replies = {"/models": _Response({"x": 1}, status_code=503)}
    result = probe(_cloud(), test_tools=True)
    assert result.reachable is False and "503" in result.detail
    assert result.calls_used == 0  # 连列表都没通，不该再花一次推理


def test_probe_says_so_when_the_model_is_not_listed(calls: Any) -> None:
    calls.replies = {"/models": {"data": [{"id": "someone-else"}]}}
    result = probe(_cloud())
    assert result.reachable is True and result.model_listed is False
    assert "中转站" in result.detail  # 建议性提示，不是否决：手填仍然可以加


# --------------------------------------------------------------------------- 凭据定位


def _service() -> ModelSettingsService:
    conn = connect(":memory:")
    bootstrap(conn, enabled_domains=())
    return ModelSettingsService(conn)


def test_resolve_target_uses_the_stored_group_key() -> None:
    """只给 provider_id：key 由服务端从组里取，从不经过请求体/查询串。"""
    svc = _service()
    svc.save(
        default="chat",
        backends=[{"name": "chat", "provider": "siliconflow", "model": "m",
                   "api_key": "sk-group", "usage": "chat"}], user_id=OWNER,
    )
    target = resolve_target(svc, provider_id="siliconflow", model="new-model", user_id=OWNER)
    assert target.api_key == "sk-group"
    assert target.base_url == "https://api.siliconflow.cn/v1"
    assert target.model == "new-model"


def test_resolve_target_body_key_wins_over_stored() -> None:
    svc = _service()
    svc.save(
        default="chat",
        backends=[{"name": "chat", "provider": "siliconflow", "model": "m",
                   "api_key": "sk-old", "usage": "chat"}], user_id=OWNER,
    )
    target = resolve_target(svc, provider="siliconflow", api_key="sk-new", model="m", user_id=OWNER)
    assert target.api_key == "sk-new"


def test_resolve_target_rejects_bad_scheme() -> None:
    svc = _service()
    with pytest.raises(ModelSettingsError, match="仅支持 http/https"):
        resolve_target(
            svc,
            provider="openai",
            base_url="file:///etc/passwd",
            model="m",
            user_id=OWNER,
        )
    with pytest.raises(ModelSettingsError, match="必须包含协议"):
        resolve_target(svc, provider="openai", base_url="10.0.0.7:1234", model="m", user_id=OWNER)


def test_resolve_target_unknown_group_is_a_400_not_a_silent_probe() -> None:
    svc = _service()
    with pytest.raises(ModelSettingsError, match="不存在"):
        resolve_target(svc, provider_id="ghost", model="m", user_id=OWNER)


# --------------------------------------------------------------------------- 那张测试图


def test_miniature_png_is_a_real_16x16_red_image() -> None:
    """手搓的 PNG 字节必须是合法图 —— 否则云端会把它当"图片格式错误"，探测结论全是假的。"""
    raw = base64.b64decode(model_probe._MINIATURE_PNG)  # noqa: SLF001
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    chunks: dict[bytes, bytes] = {}
    pos = 8
    while pos + 8 <= len(raw):
        (length,) = struct.unpack(">I", raw[pos : pos + 4])
        kind = raw[pos + 4 : pos + 8]
        chunks[kind] = raw[pos + 8 : pos + 8 + length]
        pos += 12 + length
    width, height, depth, color = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (width, height, depth, color) == (16, 16, 8, 2)  # 8bit RGB
    pixels = zlib.decompress(chunks[b"IDAT"])
    stride = 1 + width * 3
    rows = [pixels[i * stride : (i + 1) * stride] for i in range(height)]
    assert all(row[0] == 0 for row in rows)  # filter=None，逐行原样
    assert all(row[1:4] == b"\xff\x00\x00" for row in rows)  # 每行开头是纯红
