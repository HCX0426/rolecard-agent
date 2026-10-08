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

from rolecard_agent.base import paths as paths_mod
from rolecard_agent.base.paths import (
    IS_WINDOWS,
    RUNTIME_FORM_ENV,
    bundled_ocr_worker,
    runtime_form,
)
from rolecard_agent.core.models.services import check_availability
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
    monkeypatch.setattr("rolecard_agent.base.paths.is_frozen", lambda: False, raising=True)
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
    monkeypatch.setattr("rolecard_agent.base.paths.is_frozen", lambda: True, raising=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    found = bundled_ocr_worker()
    assert found is not None
    assert found.name.startswith("ocr-worker"), str(found)
    # worker 不在位时必须是 None（"这一包没带 OCR" 是合法状态，不许抛、也不许指到空气）。
    exe.unlink()
    assert bundled_ocr_worker() is None


def test_容器态那句话指云端兜底而不是让人去装_venv(monkeypatch, tmp_path, capsys) -> None:
    """容器里"本地 OCR 不可用"是**设计**，可操作的路只有云端兜底。

    旧文案给的两条路（装 `.venv-ocr` / 重打这一包）在镜像里都做不到 —— 容器里的人读了
    只会去查一个镜像里根本不存在的东西。这一格就是快照里"容器形态静默缺本地 OCR"。
    """
    monkeypatch.setattr(ocr_mod, "bundled_ocr_worker", lambda: None, raising=True)
    monkeypatch.setattr(ocr_mod, "_container_hint_emitted", False, raising=True)
    monkeypatch.setenv(RUNTIME_FORM_ENV, "container")
    backend = LocalRapidOcrBackend(exe=str(tmp_path / "没有这个.exe"))
    why = backend.readiness()
    assert "云端" in why and "capability-matrix.json" in why, why
    assert "装 .venv-ocr" not in why, "容器里做不到的建议不许再出现在这一支里"
    # 服务页那格是给人看的，日志里那条是给排障的人看的（人读日志的唯一出口）。
    assert "[warning] [ocr.container_no_local]" in capsys.readouterr().err


def test_容器提示只打一次(monkeypatch, tmp_path, capsys) -> None:
    """探活会反复问 `readiness()`：提示是"让人读到"，不是"每次都往 stderr 写一遍"。"""
    monkeypatch.setattr(ocr_mod, "bundled_ocr_worker", lambda: None, raising=True)
    monkeypatch.setattr(ocr_mod, "_container_hint_emitted", False, raising=True)
    monkeypatch.setenv(RUNTIME_FORM_ENV, "container")
    backend = LocalRapidOcrBackend(exe=str(tmp_path / "没有这个.exe"))
    first = backend.readiness()
    capsys.readouterr()
    assert backend.readiness() == first == backend.readiness(), "文案每次都一样"
    assert "ocr.container_no_local" not in capsys.readouterr().err


def test_非容器形态的文案一个字不改(monkeypatch, tmp_path) -> None:
    """自报标记不在时照旧 —— 容器那一支不许把开发态/装机版也接过去（护栏两臂都要量到）。"""
    monkeypatch.setattr(ocr_mod, "bundled_ocr_worker", lambda: None, raising=True)
    monkeypatch.delenv(RUNTIME_FORM_ENV, raising=False)
    backend = LocalRapidOcrBackend(exe=str(tmp_path / "没有这个.exe"))
    why = backend.readiness()
    assert "云端" not in why and "ocr-worker" in why, why


def test_形态判据是自报而不是探测(monkeypatch) -> None:
    """判据只认自报标记：**不许**因为"这台机器看起来在容器里"就换答案。

    这条是给后来的人看的护栏：加 `/.dockerenv` 那类探测，会让门禁自己的 CI job（若在
    容器里跑）把开发态用例判成容器态 —— 一个换台机器就换答案的判据写不出可复现的用例。
    """
    monkeypatch.delenv(RUNTIME_FORM_ENV, raising=False)
    monkeypatch.setattr(paths_mod, "is_frozen", lambda: False, raising=True)
    assert runtime_form() == "source"
    monkeypatch.setattr(paths_mod, "is_frozen", lambda: True, raising=True)
    assert runtime_form() == "desktop", "冻结态不是容器态：装机版没有自报标记"
    monkeypatch.setenv(RUNTIME_FORM_ENV, " Container ")  # 大小写与空格宽松（它毕竟是个 env）
    assert runtime_form() == "container"
