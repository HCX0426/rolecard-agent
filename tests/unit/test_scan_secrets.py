"""密钥扫描的 fail-closed 出口（`scripts/tools/scan_secrets.py`）。

测试**不出网也不装二进制**：判据量的是"各种跑不成的形状都得退 2"这条纪律本身
（与依赖审计同一条：扫不成 ≠ 干净）。2026-10-09 补的是最后一种此前没有护垫的形状 ——
gitleaks 挂死：从前 `subprocess.run` 没有超时，吊着整条 CI job 直到顶穿被杀，而 GitHub
对 cancelled job 不传日志，现场直接消失。
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "tools" / "scan_secrets.py"


def _load():
    spec = importlib.util.spec_from_file_location("scan_secrets_under_test", str(_SCRIPT))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scan_secrets_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / ".gitleaks.toml").write_text("[allowlist]\n", encoding="utf-8")
    return tmp_path


def test_缺配置文件按红退2(tmp_path: pathlib.Path) -> None:
    scan = _load()
    assert scan.main(["--repo", str(tmp_path)]) == 2  # 没有 .gitleaks.toml


def test_装不到工具按红退2(tmp_path: pathlib.Path, monkeypatch) -> None:
    scan = _load()
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(scan, "_tool_path", lambda: None)
    monkeypatch.setattr(scan, "_install_to", lambda _c: None)
    assert scan.main(["--repo", str(repo)]) == 2


def test_挂死变一次干净的红而不是无限等(tmp_path: pathlib.Path, monkeypatch, capsys) -> None:
    """gitleaks 吊着不动：300s 到点必须退 2，而不是 traceback、更不是无限等。"""
    scan = _load()
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(scan, "_tool_path", lambda: repo / "gitleaks")

    def hang(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="gitleaks", timeout=300)

    monkeypatch.setattr(scan.subprocess, "run", hang)
    assert scan.main(["--repo", str(repo)]) == 2
    assert "300s" in capsys.readouterr().err


def test_真有发现退1_没有发现退0(tmp_path: pathlib.Path, monkeypatch) -> None:
    """退码约定不能被超时护垫带偏：0/1 两格照旧直通。"""

    class Proc:
        def __init__(self, code: int) -> None:
            self.returncode, self.stdout, self.stderr = code, "", ""

    scan = _load()
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(scan, "_tool_path", lambda: repo / "gitleaks")
    for code, want in ((0, 0), (1, 1), (137, 2)):
        # 循环变量要**当下绑住**（默认参数那份老写法）：直接 `lambda: Proc(code)` 让
        # ruff B023 拦住 —— 三次调用共享最后一个 code 时，这三格测的就不是三格了。
        fake = (lambda exit_code: (lambda _cmd, **_k: Proc(exit_code)))(code)
        monkeypatch.setattr(scan.subprocess, "run", fake, raising=True)
        assert scan.main(["--repo", str(repo)]) == want, f"gitleaks 退 {code} 应映射成 {want}"
