"""随包 OCR worker 的解析与状态文案（装机版有没有本地 OCR，判据只许有一处）。

背景（用户 10-03 报"服务页的 OCR 咋都不可用"）：`default_ocr_python()` 在冻结态必然返回 None，
而服务页的探活自己又写了一份"看 .venv-ocr 在不在"——于是**装机版永远报"未找到独立 OCR 解释器"**，
那句话在一个从来没装过 venv 的形态里根本不是原因。现在两条路：随包 `ocr-worker.exe`（新）、
独立 venv 的 python + `scripts/ocr_worker.py`（开发态），而 `available()` / `ocr()` / 服务页
三处问的是同一个 `_launcher()`。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from rolecard_agent.core.paths import IS_WINDOWS, bundled_ocr_worker
from rolecard_agent.core.services import check_availability
from rolecard_agent.rag import ocr as ocr_mod
from rolecard_agent.rag.ocr import LocalRapidOcrBackend
from rolecard_agent.rag.parser import OcrUnavailable


class _Settings:
    def __init__(self, ocr_python: str | None = None) -> None:
        self.ocr_python = ocr_python


def test_随包的_worker_优先且只用它自己(monkeypatch, tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "python.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        ocr_mod, "bundled_ocr_worker", lambda: tmp_path / "ocr-worker.exe", raising=True
    )
    backend = LocalRapidOcrBackend(exe=str(exe))
    assert backend.available()
    cmd, label = backend._launcher()  # noqa: SLF001 - 判据本身就是要问这一格
    assert cmd == [str(tmp_path / "ocr-worker.exe")], "包内 worker 在时不该再拖 venv python 进来"
    assert label == "ocr-worker.exe"


def test_没有包内产物时回退到独立_venv(monkeypatch, tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "python.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(ocr_mod, "bundled_ocr_worker", lambda: None, raising=True)
    backend = LocalRapidOcrBackend(exe=str(exe))
    got = backend._launcher()  # noqa: SLF001
    assert got is not None
    cmd, label = got
    assert cmd == [str(exe), str(ocr_mod._OCR_WORKER)], "开发态仍是 venv python + worker 脚本"  # noqa: SLF001
    assert label == "python.exe"


def test_两条路都没有时原因不许再说谎(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(ocr_mod, "bundled_ocr_worker", lambda: None, raising=True)
    backend = LocalRapidOcrBackend(exe=str(tmp_path / "没有这个.exe"))
    assert not backend.available()
    why = backend.readiness()
    # 旧文案只提 `.venv-ocr`，装机版读了会去查一个形态里根本不存在的东西。
    assert "ocr-worker" in why and ".venv-ocr" in why, why


def test_跑不动时抛的就是那句状态(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(ocr_mod, "bundled_ocr_worker", lambda: None, raising=True)
    # 显式给一条不存在的路径，不许传 None：`exe=None` 会落到 `default_ocr_python()` 的自动发现，
    # 而这台开发机上 `.venv-ocr` 是真的 —— 那样这一格量到的就是"真跑了一次 OCR"，
    # 报的是 ParseError（图片不存在）而不是它要判的 OcrUnavailable。
    backend = LocalRapidOcrBackend(exe=str(tmp_path / "没有这个.exe"))
    with pytest.raises(OcrUnavailable) as got:
        backend.ocr(tmp_path / "x.png")
    assert "ocr-worker" in str(got.value)


def test_服务页那一格与运行时同一份判定(monkeypatch, tmp_path: pathlib.Path) -> None:
    """`check_availability("rapidocr")` 不许再自己写一份判定。

    从前它自己看 `.venv-ocr` 在不在，于是装机版明明带着随包 worker 却报"未找到独立 OCR 解释器"
    —— 界面说不可用而运行时真会去试，就是这一族的症状（`R102-56` 同源）。
    """
    monkeypatch.setattr(
        ocr_mod, "bundled_ocr_worker", lambda: tmp_path / "ocr-worker.exe", raising=True
    )
    ok, reason = check_availability("rapidocr", _Settings(ocr_python=None))  # type: ignore[arg-type]
    assert ok is True
    assert "ocr-worker.exe" in reason, reason
    assert "未找到独立 OCR 解释器" not in reason


def test_冻结态才找包内产物_开发态返回_none(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr("rolecard_agent.core.paths.is_frozen", lambda: False, raising=True)
    assert bundled_ocr_worker() is None, "开发态不该去找 resources/ocr-worker（它有 .venv-ocr）"


def test_冻结态认得到包内那一格(monkeypatch, tmp_path: pathlib.Path) -> None:
    """上一条只判了"开发态不找"，这一条判"冻结态真找得到" —— 护栏两臂都得能量到（`R102-73` 同源）。

    装完的布局是 `resources/rolecard-backend/_internal` 与 `resources/ocr-worker/` 并排，
    所以 `sys._MEIPASS` 往上两层就是 `resources`。这一格算错的症状是"包里明明有 worker，
    运行时却说没带上"。
    """
    internal = tmp_path / "resources" / "rolecard-backend" / "_internal"
    internal.mkdir(parents=True)
    exe = tmp_path / "resources" / "ocr-worker" / ("ocr-worker.exe" if IS_WINDOWS else "ocr-worker")
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr("rolecard_agent.core.paths.is_frozen", lambda: True, raising=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    found = bundled_ocr_worker()
    assert found is not None
    assert found.name.startswith("ocr-worker"), str(found)
    # worker 不在位时必须是 None（"这一包没带 OCR" 是合法状态，不许抛、也不许指到空气）。
    exe.unlink()
    assert bundled_ocr_worker() is None
