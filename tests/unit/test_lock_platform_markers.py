"""`lock platform markers` 的判据本体（2026-10-08 Linux 装锁事故那一格的尺子）。

这一族用例存在的理由就是事故本身：`pip-compile` 在 Windows 上解析时把传递依赖的上游
环境标记整个丢掉，裸的 `pywin32==312` 让 Linux 侧（CI 门禁臂、镜像、发布链）在
`pip install` 的**解析期**就退 1 —— 而锁是 10-07 落的、Linux 臂此前从没装过这把锁，
所以这条病在路上躺了一整天，首次 push 才照出来。判据不许再靠"下一次 push"发现它。

全部**离线**：喂假的声明图与假的 requires（`platform_pins` 的两个取数口都是参数），
不碰盘上的真环境 —— 这条判据在 Windows 开发机与 Linux runner 上必须问同一个世界，
而"世界"在这里是构造出来的，不是继承来的。

四臂各自钉一件事：
  * **漏钉**（裸的 Windows-only 二进制 pin）→ 点名那一行 —— 变异实测：把修好的锁剥回
    裸的就红；
  * **钉反**（方向错）→ 红；
  * **过度钉**（上游无条件声明却带标记）→ 红（那一侧会静默不装，比崩更难查）；
  * **uvloop 那一族**（声明了"另一侧才装"的成员而锁里整条没有）→ **只出声不拦**：
    实测过 Windows 解析根本不产出这一行，判红等于要求一台机器造不出来的东西。

2026-10-09 追加第五族（ENGI-18 第三路，用户拍板）：第四格从"锁里没有就响"扩成
"**哪儿都没有**才响" —— `extra_pins`（`constraints-linux.txt` 的 pin 集合）兜住就闭嘴，
外加三支结构/在场臂：三个 Linux 装配面必须挂 `-c`、约束文件本体必须可被 `parse_lock` 读。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from consistency.platform_pins import (  # noqa: E402
    CRASHES_ELSEWHERE,
    Edge,
    classify,
    expected_marker,
    lock_findings,
    parse_lock,
    platform_of,
    repair,
)

#: 假声明图：`mcp` 在两处 win32 门后声明 pywin32（真实上游的形状），`uvicorn` 在
#: standard extra 门后声明 uvloop（非 win32）。`altgraph` 无条件 —— 用来钉"过度钉"那臂。
FAKE_EDGES: dict[str, list[Edge]] = {
    "pywin32": [
        ("mcp", 'sys_platform == "win32" and python_version < "3.14"', ">=310"),
        ("mcp", 'sys_platform == "win32" and python_version >= "3.14"', ">=311"),
    ],
    "uvloop": [
        (
            "uvicorn",
            "(sys_platform != 'win32' and (sys_platform != 'cygwin'"
            " and platform_python_implementation != 'PyPy')) and extra == 'standard'",
            ">=0.15.1",
        ),
    ],
    "altgraph": [("pyinstaller", None, "")],
}

#: uvicorn 声明 uvloop（standard extra 门内）——requires 接缝喂的就是这一条。
def fake_requires(name: str, extras: Any = ()) -> list[tuple[str, str | None]]:
    if name == "uvicorn" and "standard" in {str(e) for e in extras}:
        return [("uvloop", FAKE_EDGES["uvloop"][0][1])]
    return []

_LOCK_PINNED = """\
mcp==1.30.0
    # via -r requirements-mcp.txt
pywin32==312 ; sys_platform == "win32"
    # via mcp
uvicorn[standard]==0.54.0
    # via -r requirements-api.txt
watchfiles==1.3.0
    # via uvicorn
"""

_LOCK_BARE = _LOCK_PINNED.replace('pywin32==312 ; sys_platform == "win32"', "pywin32==312")


def test_修好的锁判不出问题_而漏钉点名那一行() -> None:
    problems, _noise = lock_findings(_LOCK_PINNED, FAKE_EDGES, "t.lock", fake_requires)
    assert problems == [], problems
    problems, _ = lock_findings(_LOCK_BARE, FAKE_EDGES, "t.lock", fake_requires)
    assert len(problems) == 1, problems
    assert "L3 pywin32==312" in problems[0], problems[0]
    assert "解析期就崩" in problems[0], problems[0]


def test_钉反方向判红() -> None:
    flipped = _LOCK_PINNED.replace(
        'pywin32==312 ; sys_platform == "win32"',
        'pywin32==312 ; sys_platform != "win32"',
    )
    problems, _ = lock_findings(flipped, FAKE_EDGES, "t.lock", fake_requires)
    assert len(problems) == 1, problems
    assert "真值不等于" in problems[0], problems[0]


def test_上游无条件声明却带标记_也判红() -> None:
    """altgraph 被 pyinstaller **无条件**声明，钉上 win32 就是"另一侧静默不装"。"""
    overpinned = _LOCK_PINNED + (
        'altgraph==0.17.5 ; sys_platform == "win32"\n    # via pyinstaller\n'
    )
    problems, _ = lock_findings(overpinned, FAKE_EDGES, "t.lock", fake_requires)
    assert any("无条件" in p and "altgraph" in p for p in problems), problems


def test_uvloop那一族只出声不拦_且点名缺的行() -> None:
    """锁里 uvicorn[standard] 声明了 uvloop 而整条没有 ⇒ warn（不红）。

    为什么不能红：2026-10-08 实测过两条制造这条路都不产出那一行 —— 手工进锁会被下一轮
    pip-compile 剥掉，带标记进输入会被"当前平台评估为假"直接不收。判红 = 要求这台机器
    造不出来的行（ENGI-18 立案的那格）。
    """
    problems, noise = lock_findings(_LOCK_PINNED, FAKE_EDGES, "t.lock", fake_requires)
    assert problems == []
    assert any("uvloop" in n for n in noise), noise
    # 带回正确的一行之后，那一声 warn 消失 —— 出声处只在该响的时候响。
    carried = _LOCK_PINNED.replace(
        "watchfiles==1.3.0",
        'uvloop==0.23.0 ; (sys_platform != "win32" and (sys_platform != "cygwin"'
        ' and platform_python_implementation != "PyPy"))\n    # via uvicorn\nwatchfiles==1.3.0',
    )
    _p2, noise2 = lock_findings(carried, FAKE_EDGES, "t.lock", fake_requires)
    assert not any("uvloop" in n for n in noise2), noise2


def test_extras门没被要求时不出声() -> None:
    """`uvicorn` 不带 [standard] 时，uvloop 那条边**不存在** —— 不该 warn。"""
    plain = _LOCK_PINNED.replace("uvicorn[standard]==0.54.0", "uvicorn==0.54.0")

    def requires_plain(name: str, extras: Any = ()) -> list[tuple[str, str | None]]:
        return []  # 没有 standard extra ⇒ uvloop 不在声明里

    _problems, noise = lock_findings(plain, FAKE_EDGES, "t.lock", requires_plain)
    assert noise == []


def test_repair补钉且幂等() -> None:
    """生成器侧的那半步：裸的钉上、钉完再跑一遍不许再动。

    只动 `CRASHES_ELSEWHERE`（会崩那族）——给装得上的 Windows-only 纯 Python 包顺手钉标记
    会删掉 Linux 现在实际拿到的东西，不归这一刀（判据 docstring 里那条纪律的用例面）。
    """
    fixed, notes = repair(_LOCK_BARE, FAKE_EDGES)
    assert notes, "没钉上任何东西"
    assert 'pywin32==312 ; sys_platform == "win32"' in fixed
    again, notes2 = repair(fixed, FAKE_EDGES)
    assert notes2 == [] and again == fixed, "repair 不幂等"


def test_名单与元数据冲突时点名要求人来核对() -> None:
    """`CRASHES_ELSEWHERE` 不靠元数据成立（它问的是"另一侧有没有货"）。

    所以**名单优先**：认得这条包名就先问"你钉对了吗"，判不出平台性时不闭嘴。
    哪天上游把 pywin32 改成无条件依赖（Linux 装得上了）、或本地看不见它的元数据，
    判据必须点名让人来核对这一行留不留，而不是安静地放行 —— 这条名单唯一的出声处。
    """
    assert "pywin32" in CRASHES_ELSEWHERE
    # 元数据里查无此包：钉好的行照样要被核对（判不出平台性 ⇒ 红，不是 OK）。
    problems, _ = lock_findings(_LOCK_PINNED, {}, "t.lock", lambda *_a: [])
    assert len(problems) == 1 and "判不出平台性" in problems[0], problems
    # 裸着的行 + 元数据**认得**它 ⇒ 走的是"崩在解析期"那一句（两格各自点名，别混）。
    problems, _ = lock_findings(_LOCK_BARE, FAKE_EDGES, "t.lock", lambda *_a: [])
    assert len(problems) == 1 and "解析期就崩" in problems[0], problems
    # 上游改口成无条件声明、而锁里还钉着 win32 ⇒ 红在"静默不装"那一格。
    conflicts = {"pywin32": [("mcp", None, "")]}
    problems, _ = lock_findings(_LOCK_PINNED, conflicts, "t.lock", lambda *_a: [])
    assert any("无条件" in p for p in problems), problems


def test_平台判定按锁的python而不是本机解释器() -> None:
    """`mcp` 对 pywin32 的分裂声明（<3.14 与 >=3.14 两条）按并集收拢。

    本机 python 是 3.13 或 3.14 都不该改变结论 —— 变了就说明判定继承了本机环境
    （这正是 10-08 构造判据时踩过的坑：点号键写错 ⇒ 两平台都读成"本机 Windows"）。
    """
    shape = classify("pywin32", FAKE_EDGES)
    assert shape == "win"
    assert expected_marker("pywin32", FAKE_EDGES) == 'sys_platform == "win32"'
    # uvloop 摘掉 extras 门之后是"仅非 Windows"；门没开时 inert。
    marker_uv = FAKE_EDGES["uvloop"][0][1]
    assert platform_of(marker_uv, ["standard"]) == "unix"
    assert platform_of(marker_uv, []) == "inert"


def test_parse_lock认pin行_不认注释与缩进() -> None:
    pins = parse_lock(_LOCK_PINNED)
    assert [p.name for p in pins] == ["mcp", "pywin32", "uvicorn", "watchfiles"]
    uv = next(p for p in pins if p.name == "uvicorn")
    assert uv.extras == ("standard",)
    py = next(p for p in pins if p.name == "pywin32")
    assert py.marker == 'sys_platform == "win32"'
    bare = parse_lock(_LOCK_BARE)
    assert next(p for p in bare if p.name == "pywin32").marker is None


# ---- ENGI-18 第三路（2026-10-09 拍板）：约束面是第四格的第二个读数面 ---------------------

def test_约束面兜住uvloop时第四格闭嘴_拿掉就回来() -> None:
    """`extra_pins` = `constraints-linux.txt` 的 pin 集合（ENGI-18 第三路）。

    判据语义从「锁里没有就响」变成「**哪儿都没有**才响」：约束在 → 闭嘴（出声处不再空响）；
    约束拿掉 → 同一发 noise 当场回来（谁删约束行都删不掉这条警告，绕不过去）。
    """
    _p0, noise0 = lock_findings(_LOCK_PINNED, FAKE_EDGES, "t.lock", fake_requires)
    assert any("uvloop" in n for n in noise0), "前提：无约束时必须响"
    _p1, noise1 = lock_findings(
        _LOCK_PINNED, FAKE_EDGES, "t.lock", fake_requires, extra_pins={"uvloop"}
    )
    assert not any("uvloop" in n for n in noise1), noise1
    # 名字形状不整齐也认（与锁里 pin 同一口径的归一：大小写折叠）
    _p2, noise2 = lock_findings(
        _LOCK_PINNED, FAKE_EDGES, "t.lock", fake_requires, extra_pins={"UVLOOP"}
    )
    assert not any("uvloop" in n for n in noise2), noise2
    # 约束兜的是"另一侧没锁版"那一格；判红三格不受影响（漏钉仍点名）
    problems, _ = lock_findings(
        _LOCK_BARE, FAKE_EDGES, "t.lock", fake_requires, extra_pins={"uvloop"}
    )
    assert len(problems) == 1 and "解析期就崩" in problems[0], problems


def test_三个Linux装配面挂约束_两个Windows面不挂() -> None:
    """结构臂：`-c constraints-linux.txt` 只挂 Linux 装配面（gate / full-gate / Dockerfile）。

    Windows 面（windows-test / windows-release）**刻意不挂**：win32 根本不请求 uvloop，
    挂了是 no-op；不挂是因为文件名与语义都写着 linux —— 这条断言钉的就是"别哪天顺手挂满"。

    2026-10-10 认两种安装器（`pip install` / `uv pip install`）：CI 四臂改用 uv 提速
    （冷装 303.9s → 27.8s，本机实测），这条断言原来逐字匹配 `pip install …`，当场红在
    "带 -c 的应为 2，实为 0"——**它红得对**（形状确实变了），改的是判据不是现实：现在按
    "哪一种安装器"都算，钉的仍是"哪几个面挂了约束"这个结构事实。
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # 归一成"去掉安装器前缀（含 uv 的 --system）"的命令体，两种安装器等价看待。
    def _body(line: str) -> str:
        s = line.strip()
        for verb in ("uv pip install --system ", "uv pip install ", "pip install "):
            if s.startswith(verb):
                return s[len(verb):]
        return ""

    guarded = [
        ln for ln in ci.splitlines()
        if _body(ln) == "-r requirements.lock -c constraints-linux.txt"
    ]
    unguarded = [
        ln for ln in ci.splitlines()
        if _body(ln) == "-r requirements.lock"
    ]
    assert len(guarded) == 2, f"带 -c 的锁安装应为 2 个 ubuntu job，实为 {len(guarded)}"
    assert len(unguarded) == 2, f"不带 -c 的应为 2 个 windows job，实为 {len(unguarded)}"
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY constraints-linux.txt ./" in docker, "镜像里没有约束文件可 -c"
    assert "-r requirements-runtime.lock -c constraints-linux.txt" in docker


def test_入库的约束文件只有一条uvloop_pin() -> None:
    """约束文件本体：parse_lock 认得、pin 就是 uvloop 一条、版本是实测数字。

    它**不是** pip-compile 的产物（头注释写明来源与刷新器）——这条断言钉住"文件在场且
    形状可被 `lock platform markers` 读"，空文件/被清成注释都会在这里红。
    """
    path = ROOT / "constraints-linux.txt"
    assert path.exists(), "约束文件不见了：Linux 装配面的 -c 会把 CI/镜像装红"
    pins = parse_lock(path.read_text(encoding="utf-8"))
    assert [p.name for p in pins] == ["uvloop"], [p.name for p in pins]
    assert pins[0].version[0].isdigit(), pins[0].version
    assert pins[0].marker is None, "这行不带标记：文件只挂 Linux 面，行内不必再判平台"
