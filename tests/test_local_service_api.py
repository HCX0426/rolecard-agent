"""本地推理服务端点（api/routers/local_service.py）。  Traceability: US-5（可运营）。

钉四件事：

  1. **回环门禁是真的门禁**：`AUTH_MODE=off`（默认）时外部来源也必须 403 —— 这三个端点
     能影响主机（加载/释放 5.8 GB 显存），普通鉴权依赖在默认配置下等于没挂。
  2. **状态与失败都如实**：常驻（`keep_alive=-1`）与"用完自动退出"要能区分；加载/释放失败
     要 502，不能返回 200 让用户以为显存已经还回来了（或以为已经常驻了）。
  3. **"能不能常驻"取自当前默认后端**：云端后端没有 keep_alive，未配置默认也不能把
     "看一眼状态"变成 400。
  4. **常驻探针必须带后端配置的 num_ctx**：Ollama 在加载时定死窗口，漏传会把模型钉回 4096。

离线：Ollama 的四个原语全部 monkeypatch；配置用注入的假 `AppContext`，所以本文件既不碰
11434，也不依赖开发机上的 `.env` 里配了哪个后端。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.deps import get_context
from rolecard_agent.api.main import create_app
from rolecard_agent.api.routers import local_service as svc
from rolecard_agent.config import Settings

PINNED = "2318-12-30T16:41:33+08:00"  # keep_alive=-1 的真实形状（Ollama 推到几百年后）
SOON = "2026-09-19T18:00:00+08:00"

_LOCAL = Settings(
    model_backends={
        "local": {
            "model": "qwen3-vl:8b",
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434",
        }
    },
    model_default="local",
)
_CLOUD = Settings(
    model_backends={
        "cloud": {
            "model": "deepseek-chat",
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test",
        }
    },
    model_default="cloud",
)
# 默认后端指向一个不存在的名字：配置坏掉时状态卡仍须可读（用户要先能看到服务在不在跑）。
_BROKEN = Settings(
    model_backends={"local": {"model": "m", "provider": "ollama"}},
    model_default="gone",
)


@dataclass
class _Trail:
    """`base.audit.AuditTrail` 的替身：记下每一发留痕，不碰库。"""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def log(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@dataclass
class _Ctx:
    """端点只用到 `settings` 与 `audit.log` —— 注入假上下文，避免真装配依赖 .env。

    从前这个替身叫 `_Roles`、审计挂在它下面（与生产同形，`R102-07`），改名之后它长得
    就是它真正的样子：审计是审计，不是角色卡。
    """

    settings: Settings
    audit: _Trail = field(default_factory=_Trail)


def _client(
    tmp_path: Path, settings: Settings = _LOCAL, *, peer: str = "127.0.0.1"
) -> tuple[TestClient, _Ctx]:
    """`client=` 是 Starlette 提供的**测试侧**来源地址设置，不是生产后门。"""
    app = create_app(sqlite_path=tmp_path / "app.db")
    ctx = _Ctx(settings=settings)
    app.dependency_overrides[get_context] = lambda: ctx
    return TestClient(app, client=(peer, 50000), headers={"host": "testserver"}), ctx


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama 原语全部替身：本文件绝不碰 11434，也不管宿主机上服务在不在跑。"""
    monkeypatch.setattr(svc, "local_inference_base_url", lambda _settings: "http://127.0.0.1:9")
    monkeypatch.setattr(svc, "ollama_loaded", lambda _b: [])
    monkeypatch.setattr(svc, "ollama_reachable", lambda _b: False)
    monkeypatch.setattr(svc, "ollama_unload", lambda _b, _m: False)
    monkeypatch.setattr(svc, "ollama_keep", lambda _b, _m, _k=-1, **_kw: False)


# -- 状态 --------------------------------------------------------------------------


def test_status_reports_resident_models_as_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """常驻（几百年后才过期）必须标出来 —— 用户据此知道"等下去不会自己释放"。"""
    monkeypatch.setattr(
        svc,
        "ollama_loaded",
        lambda _b: [
            {"name": "qwen3-vl:8b", "size": 5795940924, "expires_at": PINNED, "processor": "GPU"}
        ],
    )
    c, _ = _client(tmp_path)
    body = c.get("/api/local-service").json()
    assert body["running"] is True
    assert body["resident"][0]["name"] == "qwen3-vl:8b"
    assert body["resident"][0]["pinned"] is True
    assert body["resident_bytes"] == 5795940924
    assert body["pinned"] is True


def test_status_distinguishes_idle_timer_from_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """会在几分钟后自己退出的模型不算"钉住"，界面不该显示"需要手动释放"。"""
    monkeypatch.setattr(
        svc, "ollama_loaded", lambda _b: [{"name": "m", "size": 100, "expires_at": SOON}]
    )
    c, _ = _client(tmp_path)
    body = c.get("/api/local-service").json()
    assert body["resident"][0]["pinned"] is False
    assert body["pinned"] is False


def test_status_says_not_running_when_nothing_answers(tmp_path: Path) -> None:
    """没在跑 ≠ 跑了但没驻留模型：两者的可操作动作完全不同。"""
    c, _ = _client(tmp_path)
    res = c.get("/api/local-service")
    assert res.status_code == 200
    body = res.json()
    assert body["running"] is False and body["resident"] == []


def test_malformed_size_does_not_break_the_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游字段形状变化只能让数字变 0，不能把状态卡打成 500。"""
    monkeypatch.setattr(
        svc, "ollama_loaded", lambda _b: [{"name": "m", "size": None, "expires_at": None}]
    )
    c, _ = _client(tmp_path)
    body = c.get("/api/local-service").json()
    assert body["resident"][0]["size_bytes"] == 0
    assert body["resident"][0]["pinned"] is False


def test_status_marks_only_a_native_default_as_pinnable(tmp_path: Path) -> None:
    """`is_local` 决定界面给不给"预热/常驻"：云端后端没有 keep_alive，按钮点了也是错。"""
    local, _ = _client(tmp_path, _LOCAL)
    assert local.get("/api/local-service").json()["is_local"] is True

    cloud, _ = _client(tmp_path, _CLOUD)
    body = cloud.get("/api/local-service").json()
    assert body["is_local"] is False
    # 云端默认时本地服务照样可以看（驻留的模型还占着显存），只是不能常驻它。
    assert body["base_url"] == "http://127.0.0.1:9"


def test_status_survives_a_broken_default_backend(tmp_path: Path) -> None:
    """默认后端配坏了也要能看状态 —— "先把显存腾出来"往往正是修配置之前的那一步。"""
    c, _ = _client(tmp_path, _BROKEN)
    res = c.get("/api/local-service")
    assert res.status_code == 200
    assert res.json()["is_local"] is False and res.json()["model"] is None


# -- 回环门禁 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/local-service"),
        ("POST", "/api/local-service/pin"),
        ("POST", "/api/local-service/unload"),
    ],
)
@pytest.mark.parametrize("peer", ["8.8.8.8", "10.0.0.5"])
def test_non_loopback_peer_is_rejected_even_with_auth_off(
    tmp_path: Path, peer: str, method: str, path: str
) -> None:
    """AUTH_MODE 默认 off，普通鉴权依赖等于没挂 —— 这些端点靠回环判定守。

    `pin` 尤其要紧：它会让进程去加载 5.8 GB 模型，任何人都能远程按一次就等于一台机器
    被反复撑满显存。三个端点逐一过（用各自的动词，否则会先撞上 405/404 而测不到门禁）。
    """
    c, _ = _client(tmp_path, peer=peer)
    assert c.request(method, path, json={}).status_code == 403


def test_ipv6_loopback_is_allowed(tmp_path: Path) -> None:
    c, _ = _client(tmp_path, peer="::1")
    assert c.get("/api/local-service").status_code == 200


# -- 常驻 / 预热 -------------------------------------------------------------------


def test_pin_passes_the_configured_num_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """常驻必须把后端配置的 num_ctx 透传给 Ollama。

    漏传的后果不是"小一点"而是**多一次冷加载**：预热把模型钉在默认 4096，第一条真实
    对话带着 8192 又来 → 重新加载一遍（2026-09-19 的真实 bug）。
    """
    seen: dict[str, Any] = {}

    def _keep(base: str, model: str, keep_alive: int = -1, **kw: Any) -> bool:
        seen.update(base=base, model=model, keep_alive=keep_alive, **kw)
        return True

    monkeypatch.setattr(svc, "ollama_keep", _keep)
    c, ctx = _client(tmp_path, _LOCAL)
    res = c.post("/api/local-service/pin", json={"keep_alive": -1})
    assert res.status_code == 200, res.text
    assert seen["model"] == "qwen3-vl:8b" and seen["keep_alive"] == -1
    assert seen["num_ctx"] is None  # 后端没配窗口就别硬塞一个数
    assert res.json()["model"] == "qwen3-vl:8b"
    assert ctx.audit.calls[-1]["action"] == "local_service_pin"

    # 后端把窗口抬到 8192 后，探针必须跟着改口径。
    with_num_ctx = _LOCAL.model_copy(deep=True)
    with_num_ctx.model_backends["local"].num_ctx = 8192
    c2, ctx2 = _client(tmp_path, with_num_ctx)
    assert c2.post("/api/local-service/pin", json={}).status_code == 200
    assert seen["num_ctx"] == 8192
    assert ctx2.audit.calls[-1]["detail"]["num_ctx"] == 8192


def test_pin_names_the_model_it_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "ollama_keep", lambda _b, _m, _k=-1, **_kw: True)
    c, _ = _client(tmp_path)
    res = c.post("/api/local-service/pin", json={"model": "other:tag"})
    assert res.status_code == 200 and res.json()["model"] == "other:tag"


def test_pin_failure_is_a_502_not_a_quiet_success(tmp_path: Path) -> None:
    """加载失败（`_offline` 里的替身恒返 False）不能返回 200 说"已常驻"。"""
    c, ctx = _client(tmp_path)
    res = c.post("/api/local-service/pin", json={})
    assert res.status_code == 502, res.text
    assert "常驻失败" in res.json()["detail"]
    assert ctx.audit.calls == []  # 没发生的动作不进审计


def test_pin_is_refused_for_a_cloud_default_backend(tmp_path: Path) -> None:
    c, _ = _client(tmp_path, _CLOUD)
    res = c.post("/api/local-service/pin", json={})
    assert res.status_code == 400
    assert "云端" in res.json()["detail"]


def test_pin_reports_unconfigured_default_readable(tmp_path: Path) -> None:
    """配置不全时说"先去模型页设默认"，而不是 KeyError 甩到界面上。"""
    c, _ = _client(tmp_path, _BROKEN)
    res = c.post("/api/local-service/pin", json={})
    assert res.status_code == 400
    assert "还没配置" in res.json()["detail"]


# -- 释放 --------------------------------------------------------------------------


def test_unload_without_model_releases_every_resident_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "把显存还回来"是默认诉求：不点名就卸掉全部驻留模型。"""
    monkeypatch.setattr(
        svc,
        "ollama_loaded",
        lambda _b: [
            {"name": "a:1", "size": 1, "expires_at": PINNED},
            {"name": "b:2", "size": 1, "expires_at": PINNED},
        ],
    )
    seen: list[str] = []
    monkeypatch.setattr(svc, "ollama_unload", lambda _b, m: seen.append(m) or True)
    c, ctx = _client(tmp_path)
    res = c.post("/api/local-service/unload", json={})
    assert res.status_code == 200, res.text
    assert res.json()["unloaded"] == ["a:1", "b:2"] and res.json()["skipped"] == []
    assert seen == ["a:1", "b:2"]
    assert ctx.audit.calls[-1]["action"] == "local_service_unload"  # 影响主机的动作必须留痕


def test_unload_names_only_the_requested_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(svc, "ollama_unload", lambda _b, m: seen.append(m) or True)
    c, _ = _client(tmp_path)
    res = c.post("/api/local-service/unload", json={"model": "only:this"})
    assert res.status_code == 200 and res.json()["unloaded"] == ["only:this"]
    assert seen == ["only:this"]


def test_unload_failure_is_a_502_not_a_fake_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """释放失败必须 502：返回 200 等于告诉用户"显存已经还回来了"，而它没有。"""
    monkeypatch.setattr(
        svc, "ollama_loaded", lambda _b: [{"name": "a:1", "size": 1, "expires_at": PINNED}]
    )
    c, _ = _client(tmp_path)
    res = c.post("/api/local-service/unload", json={})
    assert res.status_code == 502, res.text
    assert "释放失败" in res.json()["detail"]


def test_unload_is_a_noop_when_nothing_is_resident(tmp_path: Path) -> None:
    """没有驻留模型不是错误，也不该被说成"释放了 0 个"当成成功动作。"""
    c, ctx = _client(tmp_path)
    res = c.post("/api/local-service/unload", json={})
    assert res.status_code == 200 and res.json()["unloaded"] == []
    assert ctx.audit.calls == []


def test_unload_reports_the_models_that_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部分成功必须点名剩下的：用户据此知道显存只回来了一部分。"""
    monkeypatch.setattr(
        svc,
        "ollama_loaded",
        lambda _b: [
            {"name": "ok:1", "size": 1, "expires_at": PINNED},
            {"name": "stuck:2", "size": 1, "expires_at": PINNED},
        ],
    )
    monkeypatch.setattr(svc, "ollama_unload", lambda _b, m: m == "ok:1")
    c, _ = _client(tmp_path)
    res = c.post("/api/local-service/unload", json={})
    assert res.status_code == 200
    assert res.json() == {"unloaded": ["ok:1"], "skipped": ["stuck:2"]}


# -- 地址解析 ----------------------------------------------------------------------


def test_local_endpoint_follows_the_configured_native_backend() -> None:
    """服务地址取**默认后端**配的那一个；默认是云端/没配时回落出厂本地地址。

    为什么单独钉：把 Ollama 装在别的端口上是真实用法，而"卸哪个服务的显存"问错地址就是
    一次静默失败（连不上 → 报"释放失败"，用户以为是模型的问题）。
    """
    from rolecard_agent.core.telemetry.probes import local_inference_base_url

    native = Settings(
        model_backends={
            "local": {
                "model": "qwen3-vl:8b",
                "provider": "ollama",
                "base_url": "http://127.0.0.1:12345",
            }
        }
    )
    assert local_inference_base_url(native) == "http://127.0.0.1:12345"
    assert local_inference_base_url(_CLOUD) == "http://127.0.0.1:11434"
    assert local_inference_base_url(_BROKEN) == "http://127.0.0.1:11434"
