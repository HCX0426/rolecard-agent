"""Ollama 控制与探活的失败形状（覆盖率基线点名的 `base/probes.py` 65%）。

这一族不追"每行都跑到"，追的是**四个调用各自的返回契约在异常下站得住**：

  * 驻留/卸载只认 200（非 200 与异常都 False）—— 半成功在这里就是失败：没钉住的模型
    过 5 分钟自己掉出显存，界面上那句"已常驻"就成了谎；
  * `num_ctx` **必须跟着驻留请求走**：缺它 Ollama 把窗口钉在 4096，第一条真对话又触发
    一次冷加载 —— 预热反而制造冷加载（2026-09-19 实测），所以值得单独钉；
  * 可达性是布尔、"已常驻"是清单 —— 拿不到清单要回 `[]` 而不是 None/抛（调用方按【没有】渲染）；
  * 探活带 TTL 缓存：一次服务页渲染对同一 (base, model) 问好几遍，Ollama 没在跑时每遍
    都等满 3s（用户 2026-09-18 那句"服务页签转圈"）。而 `use_cache=False` 是"刷新状态"
    那颗钮的后门，它必须**真的重新出网** —— 否则那颗钮永远显示上次的结果。
"""

from __future__ import annotations

import pytest

from rolecard_agent.base import outbound, probes


class _Resp:
    def __init__(self, status: int = 200, json_body: object = None) -> None:
        self.status_code = status
        self._json: object = json_body if json_body is not None else {}

    def json(self) -> object:
        return self._json


class _Spy:
    """替掉 outbound：记下每次调用的形状，让"payload 里有没有 num_ctx"这种判据能落地。"""

    def __init__(self, *, resp: _Resp | None = None, error: Exception | None = None) -> None:
        self._resp, self._error = resp, error
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kw: object) -> _Resp:
        self.calls.append({"url": url, **kw})
        if self._error is not None:
            raise self._error
        assert self._resp is not None, "没给响应也没给异常：这条用例自己在空转"
        return self._resp

    def get(self, url: str, **kw: object) -> _Resp:
        self.calls.append({"url": url, **kw})
        if self._error is not None:
            raise self._error
        assert self._resp is not None, "没给响应也没给异常：这条用例自己在空转"
        return self._resp


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    # 两份缓存都是模块级的：不清就会跨用例互相顶掉（同 (base, model) 第二次直接命中缓存）
    probes._PROBE_CACHE.clear()  # noqa: SLF001
    probes._CAP_CACHE.clear()  # noqa: SLF001


def test_驻留只认_200_非零码与连接失败都算失败(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = _Spy(resp=_Resp(200))
    monkeypatch.setattr(outbound, "post", ok.post, raising=True)
    assert probes.ollama_keep("http://x", "m", keep_alive=-1) is True
    assert ok.calls[0]["url"] == "http://x/api/generate"

    half = _Spy(resp=_Resp(500))
    monkeypatch.setattr(outbound, "post", half.post, raising=True)
    assert probes.ollama_keep("http://x", "m") is False, "500 也算没钉住"

    dead = _Spy(error=OSError("Connection refused"))
    monkeypatch.setattr(outbound, "post", dead.post, raising=True)
    assert probes.ollama_keep("http://x", "m") is False


def test_驻留必须带上_num_ctx_否则预热反而制造冷加载(monkeypatch: pytest.MonkeyPatch) -> None:
    """`options.num_ctx` 是**加载时**定死的：不带就被钉在 4096。"""
    spy = _Spy(resp=_Resp(200))
    monkeypatch.setattr(outbound, "post", spy.post, raising=True)
    probes.ollama_keep("http://x", "m", num_ctx=32768)
    body = spy.calls[0]["json"]
    assert body == {
        "model": "m",
        "prompt": "",
        "keep_alive": -1,
        "options": {"num_ctx": 32768},
    }, body

    # 没给 num_ctx 时不许凭空造一个（那会覆盖用户在配置里定的窗口）
    spy2 = _Spy(resp=_Resp(200))
    monkeypatch.setattr(outbound, "post", spy2.post, raising=True)
    probes.ollama_keep("http://x", "m", num_ctx=None)
    body2 = spy2.calls[0]["json"]
    assert isinstance(body2, dict) and "options" not in body2, body2


def test_卸载用即时短超时而不是驻留那条长等待(monkeypatch: pytest.MonkeyPatch) -> None:
    """卸载是即时动作：等久了说明服务本来就没跑，该 False 而不是把请求线程挂住。"""
    spy = _Spy(resp=_Resp(200))
    monkeypatch.setattr(outbound, "post", spy.post, raising=True)
    assert probes.ollama_unload("http://x", "m") is True
    assert spy.calls[0]["json"] == {"model": "m", "prompt": "", "keep_alive": 0}
    assert spy.calls[0]["timeout"] == 10.0
    # 驻留那条走的是 120s：两件事的超时预算不同，混成一个数就要么挂死要么不够
    keep = _Spy(resp=_Resp(200))
    monkeypatch.setattr(outbound, "post", keep.post, raising=True)
    probes.ollama_keep("http://x", "m")
    assert keep.calls[0]["timeout"] == 120.0

    dead = _Spy(error=OSError("boom"))
    monkeypatch.setattr(outbound, "post", dead.post, raising=True)
    assert probes.ollama_unload("http://x", "m") is False


def test_可达性区分没起与起了但没驻留(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/tags：200=在跑（哪怕没驻留模型），异常=没在跑。两件事不许混。"""
    up = _Spy(resp=_Resp(200))
    monkeypatch.setattr(outbound, "get", up.get, raising=True)
    assert probes.ollama_reachable("http://x") is True
    assert up.calls[0]["timeout"] == 3.0

    down = _Spy(error=OSError("refused"))
    monkeypatch.setattr(outbound, "get", down.get, raising=True)
    assert probes.ollama_reachable("http://x") is False
    # 同一个"异常=不在跑"的判定，探模型那一格也要给 False（不是抛）：选择器与探针
    # 两处共用一份答案，混进异常就会把"服务页问一遍"变成"服务页 500"。
    assert probes.vision_model_ready("http://x", "m") is False


def test_已常驻清单拿不到时回空表不抛(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _Spy(
        resp=_Resp(
            200,
            {"models": [{"name": "m:latest", "size": 12, "expires_at": "later", "cpu": 1}]},
        )
    )
    monkeypatch.setattr(outbound, "get", good.get, raising=True)
    rows = probes.ollama_loaded("http://x")
    assert rows[0]["name"] == "m:latest" and rows[0]["size"] == 12, rows
    # 界面那一格只认 name/size/expires_at/processor 四件，多余的键不该漏进契约
    assert set(rows[0]) == {"name", "size", "expires_at", "processor"}, rows[0]

    bad = _Spy(error=OSError("refused"))
    monkeypatch.setattr(outbound, "get", bad.get, raising=True)
    assert probes.ollama_loaded("http://x") == [], "失败要回空清单：调用方按【没有】渲染，不许抛"


def test_探活缓存续住同一页的多次问话而刷新钮不吃缓存(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一次服务页渲染对同一 (base, model) 问好几遍；没在跑时每遍都等满 3s。"""
    calls: list[str] = []

    def fake_get(url: str, **_kw: object) -> _Resp:
        calls.append(url)
        return _Resp(200, {"models": [{"name": "m:latest"}]})

    monkeypatch.setattr(outbound, "get", fake_get, raising=True)
    assert probes.vision_model_ready("http://x", "m") is True
    assert probes.vision_model_ready("http://x", "m") is True
    assert len(calls) == 1, "第二次该吃缓存（否则服务页转圈那一格又回来了）"

    # "刷新状态"那颗钮的后门：不许继续吃缓存，否则它永远显示上次的结果
    assert probes.vision_model_ready("http://x", "m", use_cache=False) is True
    assert len(calls) == 2


def test_缓存会过期而不是永久续住(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTL 存在的理由是"状态最多晚 10s 被看见"，不是"第一次结果永远显示下去"。

    过期这条支路不测的话，`_PROBE_CACHE` 变成一个只进不出的表：Ollama 重启、模型装卸完，
    服务页仍然报旧状态，而没人会想到去查缓存 —— 只进不出的缓存比没缓存更难查。
    """
    now = [1000.0]
    monkeypatch.setattr(probes.time, "monotonic", lambda: now[0], raising=True)
    calls: list[str] = []

    def fake_get(url: str, **_kw: object) -> _Resp:
        calls.append(url)
        return _Resp(200, {"models": [{"name": "m:latest"}]})

    monkeypatch.setattr(outbound, "get", fake_get, raising=True)
    assert probes.vision_model_ready("http://x", "m") is True
    now[0] += probes.PROBE_TTL - 1  # TTL 内：吃缓存
    assert probes.vision_model_ready("http://x", "m") is True
    assert len(calls) == 1
    now[0] += 2  # 跨过 TTL：必须重新出网
    assert probes.vision_model_ready("http://x", "m") is True
    assert len(calls) == 2, "过期不出网 = 状态永远停在第一次的结果"
