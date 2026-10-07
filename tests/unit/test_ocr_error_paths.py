"""OCR 三格的失败路径（覆盖率基线点名的靶子：`rag/ocr.py` 含分支口径 62%）。

低覆盖的文件恰好全是失败路径 —— 这不是巧合，是"用例好写 happy path、失败要造现场"的必然。
而 OCR 这一族的失败形状各有归处，混成一团就会把人引去查错的东西：

  * **超时** ≠ 启动失败（`R102-66`）：折叠成一句，用户会去重装 OCR，而真相是要换小图；
  * **非零退出** 要带 stderr 首行（退出码本身没信息量，原因在流里）；
  * **HTTP 非 200 / 空返回 / 配额报错**是三件事，云端那条还要把 `ErrorMessage` 透出来，
    而不是静默返回空字符串（静默空 = 报告"这张图没识别出来"，谁都不知道是配额没了）。

最后那组判的是选择器**顺延**的语义：一个候选不可用就试下一个，全部不可用才降级 ——
跳过与失败在界面上长得一样，只有用例能分开它们。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from rolecard_agent.config import Settings
from rolecard_agent.rag import ocr as ocr_mod
from rolecard_agent.rag.errors import OcrUnavailable, ParseError


@pytest.fixture
def png(tmp_path: Path) -> Path:
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return p


class _Client:
    """替掉 `_http()`：只回我要它回的东西（成功体、错误码、或直接抛）。"""

    def __init__(self, *, response: httpx.Response | None = None, error: Exception | None = None):
        self._response, self._error = response, error
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"url": url, **kw})
        if self._error is not None:
            raise self._error
        assert self._response is not None, "没给响应也没给异常，这条用例自己空转"
        return self._response


def _request(url: str = "http://x/api/chat", **kw: Any) -> httpx.Response:
    return httpx.Response(kw.pop("status", 200), request=httpx.Request("POST", url), **kw)


def _stub_http(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    monkeypatch.setattr(ocr_mod, "_http", lambda: client)


# ----------------------------------------------------------- 本地 RapidOCR（子进程那一族）


def test_子进程超时说清是超时而不是启动失败(monkeypatch, tmp_path: Path) -> None:
    """`R102-66`：折叠成一句会让人去查 OCR 安装，而真相是"换小图或加预算"。"""
    monkeypatch.setattr(
        ocr_mod,
        "bundled_ocr_worker",
        lambda: tmp_path / "ocr-worker.exe",
        raising=True,
    )

    def boom(*_a: Any, **_k: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="ocr-worker", timeout=120)

    monkeypatch.setattr(ocr_mod.subprocess, "run", boom, raising=True)
    backend = ocr_mod.LocalRapidOcrBackend(exe=None)
    with pytest.raises(ParseError) as got:
        backend.ocr(tmp_path / "x.png")
    message = str(got.value)
    assert "超时" in message and "更小的图" in message, message


def test_子进程启动失败带得出原因(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        ocr_mod, "bundled_ocr_worker", lambda: tmp_path / "ocr-worker.exe", raising=True
    )

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("系统找不到指定的文件。")

    monkeypatch.setattr(ocr_mod.subprocess, "run", boom, raising=True)
    backend = ocr_mod.LocalRapidOcrBackend(exe=None)
    with pytest.raises(ParseError) as got:
        backend.ocr(tmp_path / "x.png")
    assert "启动失败" in str(got.value) and "找不到" in str(got.value)


def test_非零退出把_stderr_首行带出来(monkeypatch, tmp_path: Path) -> None:
    """退出码本身没有信息量；原因是子进程写到流里的那句话。"""
    monkeypatch.setattr(
        ocr_mod, "bundled_ocr_worker", lambda: tmp_path / "ocr-worker.exe", raising=True
    )

    class _Proc:
        returncode = 3
        stdout = ""
        stderr = "Traceback...\nCUDA out of memory\n"

    def fake_run(*_a: Any, **_k: Any) -> _Proc:
        return _Proc()

    monkeypatch.setattr(ocr_mod.subprocess, "run", fake_run, raising=True)
    backend = ocr_mod.LocalRapidOcrBackend(exe=None)
    with pytest.raises(ParseError) as got:
        backend.ocr(tmp_path / "x.png")
    message = str(got.value)
    assert "退出码 3" in message, message
    assert "CUDA out of memory" in message, "长栈截前 300 字，但原因必须在里面"
    assert "Traceback" in message


# --------------------------------------------------------------- 本地视觉模型（VLM 那一格）


def test_视觉模型不可达算不可用而非失败(monkeypatch, png: Path) -> None:
    """连不上 = 顺延下一个候选（OcrUnavailable）；连上了但识别失败 = ParseError。"""
    _stub_http(monkeypatch, _Client(error=httpx.ConnectError("Connection refused")))
    backend = ocr_mod.VisionModelBackend(base_url="http://127.0.0.1:9", model="vl")
    with pytest.raises(OcrUnavailable) as got:
        backend.ocr(png)
    assert "不可达" in str(got.value)


def test_视觉模型非_200_与空返回是两种话(monkeypatch, png: Path) -> None:
    _stub_http(monkeypatch, _Client(response=_request(json={}, status=500)))
    backend = ocr_mod.VisionModelBackend(base_url="http://x", model="vl")
    with pytest.raises(ParseError) as got:
        backend.ocr(png)
    assert "HTTP 500" in str(got.value)

    _stub_http(monkeypatch, _Client(response=_request(json={"message": {"content": "  "}})))
    with pytest.raises(ParseError) as got2:
        backend.ocr(png)
    assert "没有返回任何内容" in str(got2.value)


def test_读图失败两种形状都不许是裸异常(monkeypatch, tmp_path: Path) -> None:
    """文件不在 ⇒ 三个后端都给 `ParseError`（"这张图读不出来"），不给调用方留裸异常。

    反面是"图片过大"那句必须**原样透出**：那句话是给人看的行动指引，被包一层就废了。
    两个后端（视觉/云端）以前在这里形状不一致 —— 云端包了、视觉没包，同一个失败一边 500
    一边可读；这一条把"一致"钉住，而不是钉住其中一种。
    """
    missing = tmp_path / "根本没有这个.png"
    _stub_http(monkeypatch, _Client(response=_request(json={"message": {"content": "x"}})))
    vl = ocr_mod.VisionModelBackend(base_url="http://x", model="vl")
    with pytest.raises(ParseError) as got:
        vl.ocr(missing)
    assert "读取图片失败" in str(got.value)
    with pytest.raises(ParseError):
        ocr_mod.CloudApiBackend(api_key="k").ocr(missing)

    big = tmp_path / "big.png"
    big.write_bytes(b"0" * 4096)
    monkeypatch.setattr(ocr_mod, "MAX_OCR_IMAGE_BYTES", 100, raising=True)  # 不写真 15MB
    with pytest.raises(ParseError) as got2:
        vl.ocr(big)
    assert "图片过大" in str(got2.value) and "压缩" in str(got2.value), "指引的话不许被包糊"


# ------------------------------------------------------------------- 云端 OCR（那一格）


def test_云端未配凭据既不该可用也不该被悄悄用(monkeypatch, png: Path) -> None:
    """隐私红线：available() 严格要求显式配了 key；直接调 ocr() 也得拒，不能拿空 key 去发图。"""
    assert ocr_mod.CloudApiBackend(api_key=None).available() is False
    assert ocr_mod.CloudApiBackend(api_key="k").available() is True
    with pytest.raises(OcrUnavailable) as got:
        ocr_mod.CloudApiBackend(api_key=None).ocr(png)
    assert "API Key" in str(got.value) and "模型页" in str(got.value)


def test_云端请求失败透出原因而不静默返空(monkeypatch, png: Path) -> None:
    _stub_http(monkeypatch, _Client(error=httpx.ConnectTimeout("timed out")))
    with pytest.raises(ParseError) as got:
        ocr_mod.CloudApiBackend(api_key="k").ocr(png)
    assert "请求失败" in str(got.value) and "timed out" in str(got.value)


def test_云端返回错误体时把_errormessage_说出去(monkeypatch, png: Path) -> None:
    """配额耗尽的现场就是这个形状：200 + 空 ParsedResults + 一句 ErrorMessage。

    静默返回空字符串的话，界面只会显示"这张图没识别出来"，而真相是配额没了。
    """
    _stub_http(
        monkeypatch,
        _Client(
            response=_request(
                json={"ParsedResults": [], "ErrorMessage": "Not enough free hits left"}
            )
        ),
    )
    with pytest.raises(ParseError) as got:
        ocr_mod.CloudApiBackend(api_key="k").ocr(png)
    assert "Not enough free hits left" in str(got.value)


def test_云端正常时逐段拼回文本(monkeypatch, png: Path) -> None:
    _stub_http(
        monkeypatch,
        _Client(
            response=_request(
                json={"ParsedResults": [{"ParsedText": " 第一行 "}, {"ParsedText": "第二行"}]}
            )
        ),
    )
    assert ocr_mod.CloudApiBackend(api_key="k").ocr(png) == "第一行\n第二行"


# ------------------------------------------------------------- 选择器的顺延与降级（那一组）


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def test_选择器跳过不可用候选并顺延到下一个(monkeypatch) -> None:
    """一个候选不可用就下一个；全不可用才 None（调用方降级为 pending）。"""
    monkeypatch.setattr(
        ocr_mod.LocalRapidOcrBackend, "available", lambda self: False, raising=True
    )
    monkeypatch.setattr(ocr_mod.VisionModelBackend, "available", lambda self: False, raising=True)
    endpoints = {
        "vl": _Row(kind="local", model="qwen3-vl", base_url="http://x", stale=False),
        "cloud": _Row(kind="cloud", model="", base_url="http://c", api_key="k", stale=False),
    }
    chosen = ocr_mod.select_ocr_backend(
        Settings(ocr_python=None), order=["rapidocr", "vl", "cloud"], endpoints=endpoints
    )
    assert isinstance(chosen, ocr_mod.CloudApiBackend), "本地两格都不该用却仍往下走了"


def test_选择器对失踪与过期行都跳过(monkeypatch) -> None:
    """端点序里点了名字但行不存在 / 行已过期 ⇒ 都不许被选中，也不许抛。"""
    endpoints = {
        "gone": None,
        "stale": _Row(kind="cloud", base_url="http://c", api_key="k", stale=True),
    }
    assert (
        ocr_mod.select_ocr_backend(
            Settings(ocr_python=None), order=["gone", "stale"], endpoints=endpoints
        )
        is None
    )


def test_空端点序返回_不抛(monkeypatch) -> None:
    """装配根之外没 `seed_once()` ⇒ order 为空 ⇒ 返回 None，**不许抛**。

    钉的是现有契约（调用方按 None 降级为 pending）。这一格"静默得像图没识别出来"是
    docstring 里自己承认的已知隐患 —— 它要的是另立一格（给运维出声），不是在这条里顺手加，
    所以这里只钉"不抛 + 返回 None"，不假装隐患已经解决。
    """
    assert ocr_mod.select_ocr_backend(Settings(ocr_python=None), order=[], endpoints={}) is None


def test_行在但没配凭据的云端候选不被选中(monkeypatch) -> None:
    """端点序引用了一条云端行、模型页却没填凭据 ⇒ 不发图、不选中（隐私红线的另一面）。"""
    endpoints = {"c": _Row(kind="cloud", base_url="http://c", api_key="", stale=False)}
    assert (
        ocr_mod.select_ocr_backend(
            Settings(ocr_python=None), order=["c"], endpoints=endpoints
        )
        is None
    )
