"""读数归属那一条的**三态**（10-03 由一次 CI 上的假阳逼出来）。

现场：README 与 `docs/gate-readings.json` 两边都是 1469，数一字未漂，而 `门禁 --ci` 那一臂红在
`README 数字收尾` —— 红的是「读数的 head 是不是现在这份代码」。判据要读 `HEAD~1` 才认得
"HEAD 是只含读数/README/审计索引的跟进提交"那一半形状，而 `actions/checkout@v4` 默认浅克隆
只有 1 层 ⇒ 父提交**读不到**。旧代码把"读不到"直接 `return False`，于是**环境的缺陷被说成
代码的陈旧**。

按本仓口径（只有确认负才拦）：`True` / `False` / `None`（问不出）三态，而"问不出"必须**出声**。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

import check_consistency as cc  # noqa: E402

HEAD = "c8a1565" + "0" * 33
PARENT = "0b1877a" + "0" * 33


class _Done:
    def __init__(self, out: str) -> None:
        self.stdout = out
        self.returncode = 0


def _fake_git(monkeypatch, *, parent: str | None, changed: list[str] | None) -> None:
    """把尺子模块里的 `subprocess.run` 换成一台只应答我给的三条查询的假 git。"""

    def run(cmd, *a, **kw):  # noqa: ANN001 - 假替身，形状照 subprocess.run 用得到的那几样
        joined = " ".join(cmd)
        if "rev-parse HEAD~1" in joined:
            if parent is None:
                raise subprocess.CalledProcessError(128, cmd, b"", b"unknown revision")
            return _Done(parent + "\n")
        if "diff --name-only" in joined:
            if changed is None:
                raise subprocess.CalledProcessError(128, cmd, b"", b"fatal: bad object")
            return _Done("\n".join(changed) + "\n")
        raise AssertionError(f"用例没预料到的 git 查询：{joined}")

    monkeypatch.setattr(cc.subprocess, "run", run)


def test_读数就在当前提交上_形状一为真(monkeypatch) -> None:
    _fake_git(monkeypatch, parent=None, changed=None)  # 形状①根本不该去问父提交
    assert cc._readings_head_is_current(HEAD, HEAD[:12]) is True


def test_父提交读不到时报unknown而不是红(monkeypatch) -> None:
    """这一条就是 10-03 CI 上那次假阳：浅克隆读不到 HEAD~1，旧代码 `return False` 判了红。"""
    _fake_git(monkeypatch, parent=None, changed=None)
    assert cc._readings_head_is_current(HEAD, PARENT[:12]) is None


def test_父提交对得上而HEAD只动读数_形状二为真(monkeypatch) -> None:
    _fake_git(
        monkeypatch,
        parent=PARENT,
        changed=["README.md", "docs/gate-readings.json", "docs/架构审计索引.md"],
    )
    assert cc._readings_head_is_current(HEAD, PARENT[:12]) is True


def test_父提交对得上而HEAD动了代码_就是红(monkeypatch) -> None:
    """另一臂：跟进提交里混进一处代码，这一条**必须**拦下来。"""
    _fake_git(monkeypatch, parent=PARENT, changed=["README.md", "src/rolecard_agent/rag/ocr.py"])
    assert cc._readings_head_is_current(HEAD, PARENT[:12]) is False


def test_父提交与读数根本不是同一个_就是红(monkeypatch) -> None:
    _fake_git(monkeypatch, parent="ffff" + "0" * 36, changed=["README.md"])
    assert cc._readings_head_is_current(HEAD, PARENT[:12]) is False


def test_diff问不出时也归unknown而不是红(monkeypatch) -> None:
    _fake_git(monkeypatch, parent=PARENT, changed=None)
    assert cc._readings_head_is_current(HEAD, PARENT[:12]) is None
