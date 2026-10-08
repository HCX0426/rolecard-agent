"""README 可跑性探针的**取证那一半**（`_who_listens` / `_port_still_listening`）。

为什么单独一档：这条红线（"照 README 那条命令真起一次服务"）本身由门禁步骤端到端跑，
但它判红时打的那句"是谁还占着端口"是 2026-10-09 新加的，而**本机永远走不到那个分支**
（Windows 臂不红）。新加的取证代码一次没被执行过就推上 CI，正是本仓最忌讳的形状 ——
判据负责说"红"，现场负责说"为什么"，而**取证自己不许把这一步弄炸**（它一炸，"确证的负面"
与"取证崩了"两种完全不同的事实面就在日志里混成一样）。

行为臂与结构臂分开：
  * `_port_still_listening` 用真 socket 量（两平台都跑）—— 它是这条红线的**判据**，
    必须答对"还有没有在答"。
  * `_who_listens` 用替身量（工具在不在、输出形状各不同，不能拿别人的机器当被测环境）：
    三格 —— 命中点名 / 跑了但没 LISTEN 行 / 工具整个不可用。**后一格必须返回字符串而不是抛**：
    这是这一档最要紧的一条。
"""

from __future__ import annotations

import importlib.util
import pathlib
import socket
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "probe_readme_quickstart.py"


def _load():
    spec = importlib.util.spec_from_file_location("prq_under_test", str(_SCRIPT))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prq_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ 判据本体


def test_真在听的端口必须答_yes_关掉之后答_no() -> None:
    """`_port_still_listening` 是这条红线的判据本身：不许把"已收干净"读成"仍监听"，
    也不许反过来 —— 后者会把真留孤儿的那趟放成绿。"""
    prq = _load()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert prq._port_still_listening(port) is True, "真在听的没量出来 = 判据是摆设"
    finally:
        srv.close()
    assert prq._port_still_listening(port) is False, "关掉之后还答 True：那条红会是假的"


def test_没人听的端口不许被读成在听(tmp_path: pathlib.Path) -> None:
    """分母的另一半：拿一个**根本没开过**的端口问，必须答 False。

    只测"在听⇒True"的那半条尺子，与"只测 deny 一半的护栏救不了任何人"同族 ——
    一个恒答 True 的实现也能过上一格。
    """
    prq = _load()
    # 先抢一个空闲口再立刻关掉（确保这个号此刻没人听）
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert prq._port_still_listening(port) is False


# ------------------------------------------------------------------ 取证三格


class _FakeProc:
    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def test_命中时点名持有者(monkeypatch: pytest.MonkeyPatch) -> None:
    prq = _load()
    port = 51802
    out = (
        "Active Connections\n"
        f"  TCP    127.0.0.1:{port}   0.0.0.0:0   LISTENING   14992\n"
        "  TCP    127.0.0.1:55555   0.0.0.0:0   LISTENING   777\n"
    )
    monkeypatch.setattr(prq.subprocess, "run", lambda *_a, **_k: _FakeProc(0, out))
    got = prq._who_listens(port)
    assert str(port) in got and "14992" in got, f"持有者没点名：{got!r}"
    assert "55555" not in got and "777" not in got, "把无关端口的行也端上来了"


def test_工具跑成了但没有那一行时如实说没有(monkeypatch: pytest.MonkeyPatch) -> None:
    """这一格挡的是另一种假事实：日志里答"持有者是 X"而 X 其实是**别的端口**的主人。"""
    prq = _load()
    out = "  TCP    127.0.0.1:9999   0.0.0.0:0   LISTENING   12\n"
    monkeypatch.setattr(prq.subprocess, "run", lambda *_a, **_k: _FakeProc(0, out))
    got = prq._who_listens(51802)
    assert "9999" not in got, "把别人的端口当成持有者写进日志"
    assert "没有" in got, got


def test_取证的工具不在时报拿不到而不是抛(monkeypatch: pytest.MonkeyPatch) -> None:
    """**这一档最要紧的一格**：`ss`/`netstat` 缺失（或超时）时，判红那一句照常打得出来。

    取证代码把自己弄炸的后果不是"少一行信息"，而是把"确证的负面（留了孤儿）"变成
    "这一步崩了"——两种完全不同的事实面，而后者会把真凶一起埋掉。
    """
    prq = _load()

    def boom(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("这台机器上没有这个工具")

    monkeypatch.setattr(prq.subprocess, "run", boom)
    got = prq._who_listens(51802)
    assert isinstance(got, str) and got, "没返回可读的一句话"
    assert "拿不到" in got, got


def test_工具非零退出也换下一个而不是当持有者(monkeypatch: pytest.MonkeyPatch) -> None:
    """`returncode != 0` 的输出是**垃圾**（半截表头、权限报错），不许被当成持有者写进日志。"""
    prq = _load()
    calls: list[list[str]] = []

    def fake(cmd: list[str], **_k: object) -> _FakeProc:
        calls.append(list(cmd))
        return _FakeProc(1, "permission denied")

    monkeypatch.setattr(prq.subprocess, "run", fake)
    got = prq._who_listens(51802)
    assert "permission denied" not in got, got
    assert "拿不到" in got or "没有" in got, got


def test_超时不无限等(monkeypatch: pytest.MonkeyPatch) -> None:
    """取证也得有时间上界：`ss` 卡住不能把这条红线拖成新的挂死形状（本仓为同一件事
    刚付过一整晚的账）。"""
    prq = _load()
    seen: dict[str, object] = {}

    def fake(cmd: list[str], **kw: object) -> _FakeProc:
        seen.update(kw)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(str(kw.get("timeout")) or 0))

    monkeypatch.setattr(prq.subprocess, "run", fake)
    got = prq._who_listens(1)
    assert isinstance(seen.get("timeout"), (int, float)), "取证没带超时参数"
    assert "拿不到" in got, got


# ------------------------------------------------------------------ 形状钉


def test_判红那一句里带着持有者与外壳状态(tmp_path: pathlib.Path) -> None:
    """结构臂：宽限重试 + 点名持有者 + 外壳进程状态三件都在**判红那一条路径上**。

    这三样是 2026-10-09 那趟"红得说不出为什么"的直接答案；谁把它们改回一句裸打印，
    这里红 —— 行为臂本机走不到（Windows 上这一步不红），所以只能按源码形状钉。

    **宽限那一格钉的是循环形状而不是"宽限"这个词**（变异实测教的）：我第一版只查词，
    把 `while grace < 3 and _port_still_listening(port)` 那三行整个删掉，用例照绿 —— 因为
    那个词还留在打印句里，而那句此时写着"宽限 0s 后照旧"，**本身就是一条假话**。判据查词
    不查机制，就是这种"改掉了却量不出来"的形状。
    """
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "_who_listens(port)" in src, "判红那一句不再点名持有者"
    assert "while grace <" in src and "_port_still_listening(port)" in src, (
        "宽限重试的**循环**没了：内核回收滞后会被直接报成留孤儿（前一趟那个红多半就是这么来的）"
    )
    assert "time.sleep" in src[src.index("while grace <"):src.index("while grace <") + 200], (
        "重试没有间隔 = 三次连问同一个瞬间，等于没给滞后留时间"
    )
    assert "外壳进程状态" in src, "外壳还活着/已退这一格没了：分不清该杀谁"
