"""一致性尺子的遍历剪枝（`consistency/core._prune`）—— 尺子自己的地基，改之前全仓零用例。

2026-10-09 实测换来的：我在仓根建了个 `.venv-lock`（按锁装的对照 env，为量 ENGI-20② 那格），
`check_consistency` 立刻从 70/70 掉到 68/2 —— `audit citations` 多出 53 条"悬空"、
`audit index in sync` 行数差 +42。分离变量做了两发：env 挪出仓库 ⇒ 70/70；原地放一个**零拷贝
junction**（内容与 site-packages 一字不同也无所谓）⇒ 又 68/2。所以红的成因不是文件内容，而是
**遍历走进了仓内的虚拟环境树**。

根因是两份事实面：`.gitignore` 用前缀模式 `.venv*/`，而 `IGNORED_DIRS` 逐名点名
（`.venv`/`.venv-dev`/`.venv-ocr`）—— 名字不在那张表上的目录一律照走。这不只是"我做实验会踩"：
`src/.../core/tools/run.py` 里 `_TASK_VENV_ROOT = ROOT/".venv-tasks"` 是**产品自己**在仓内创建的
目录（角色跑命令时按任务建独立 venv），而它同样不在点名表上。

判据两臂都必须能红（本仓那条老规矩）：仓内多出一个 `.venv*` 目录 ⇒ 里面的文件不许进分母；
而**普通**目录里的同名后缀文件必须照常进 —— 只有后一臂，一个恒返回空的剪枝也能"通过"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from consistency import core  # noqa: E402


def _files_under(tmp_path: Path, *suffixes: str) -> list[Path]:
    """把尺子的世界临时换成 `tmp_path`（并清掉它的遍历缓存），问它看得见哪些文件。"""
    real_root = core.ROOT
    core.ROOT = tmp_path
    core._file_walk_cache.clear()
    try:
        return core.iter_files(*suffixes)
    finally:
        core.ROOT = real_root
        core._file_walk_cache.clear()


def test_仓内的虚拟环境目录不许进尺子分母(tmp_path: Path) -> None:
    """`.venv-lock` 这一族（前缀 `.venv` + 任意后缀）必须整棵剪掉。

    只钉住"剪枝生效"不够：还得钉住它按**前缀**生效 —— 逐名点名的写法对 `.venv-lock`、
    `.venv-tasks` 这些新名字全都失效，而那正是这次事故的成因。
    """
    for name in (".venv", ".venv-ocr", ".venv-lock", ".venv-tasks", ".venv-weird-x"):
        victim = tmp_path / name / "Lib" / "site-packages" / "pkg"
        victim.mkdir(parents=True)
        (victim / "mod.py").write_text("X = 1\n", encoding="utf-8")
        (victim / "README.md").write_text("# 第三方文档\n", encoding="utf-8")
    got = _files_under(tmp_path, ".py", ".md")
    leaked = [p for p in got if ".venv" in str(p)]
    assert not leaked, f"虚拟环境树被数进了尺子分母：{leaked[:4]}"


def test_普通目录里的文件必须照常进分母(tmp_path: Path) -> None:
    """**另一臂**：剪枝不是"什么都不看"。恒空的实现也能过上一格，所以这臂必须有。"""
    keep = tmp_path / "docs" / "sub"
    keep.mkdir(parents=True)
    (keep / "a.md").write_text("# 真文档\n", encoding="utf-8")
    (keep / "b.py").write_text("Y = 2\n", encoding="utf-8")
    got = _files_under(tmp_path, ".py", ".md")
    assert len(got) == 2, f"普通目录的文件被一起剪掉了（尺子在看一个空世界）：{got}"


def test_剪掉虚拟环境时项目本体照样全看得见(tmp_path: Path) -> None:
    """两臂同盘况：仓内既有 `.venv*` 又有项目文件时，前者不进、后者一个不少。

    这一格挡的是"为了排掉 venv 顺手把 src 也排除"那种修法 —— 少扫一族文件，尺子会绿得
    没有任何原因（本仓为"靠运气绿的判据"付过一整晚的账）。
    """
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "m.py").write_text("Z = 3\n", encoding="utf-8")
    (tmp_path / ".venv-tasks" / "demo").mkdir(parents=True)
    (tmp_path / ".venv-tasks" / "demo" / "noise.py").write_text("NO = 1\n", encoding="utf-8")
    got = _files_under(tmp_path, ".py")
    names = {p.name for p in got}
    assert "m.py" in names, f"项目本体被剪掉了：{names}"
    assert "noise.py" not in names, f"任务 venv 漏进分母：{names}"


def test_前缀规则与gitignore的那条同纪律() -> None:
    """结构臂：`.gitignore` 用 `.venv*/` 前缀，尺子也必须按前缀剪 —— 两份事实面早晚漂开。

    这条把"为什么不是往 IGNORED_DIRS 里再加一个名字"钉死：加名字只是把下一次事故推后。
    """
    import re

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^\.venv\*/\s*$", gitignore, re.M), (
        ".gitignore 里那条前缀模式不见了：那它就该一起改，别单改尺子"
    )
    src = (ROOT / "scripts" / "consistency" / "core.py").read_text(encoding="utf-8")
    assert "IGNORED_DIR_PREFIXES" in src and '(".venv",)' in src, (
        "剪枝退回逐名点名：`.venv-lock`/`.venv-tasks` 这些新名字又会照走"
    )
    assert ".venv-lock" not in src and ".venv-tasks" not in src.split("IGNORED_DIR_PREFIXES")[0], (
        "不许把具体 venv 名再加回点名表 —— 那是第二份事实面"
    )


def test_被剪掉的入库文件里不许藏着引用形状() -> None:
    r"""**把一次审计落成常驻断言**（2026-10-09 现读：704 个入库文件里有 10 个住在被剪的目录下
    —— 5 份角色设定 lore、4 个 `.gitkeep`、1 个 `shell/build/icon.ico`；其中含快照编号引用的
    **0 个**）。

    "现况无害"是巧合而不是设计：`docs/` 那 43 个入库文件在尺子视野内，而同为文档的
    `data/lore/*.md` 不在 —— 谁往 lore 里写一个能归位的快照编号（举一个不存在的号当例），
    `audit citations` 与 `audit index in sync` 就**静默看不见它**（剪枝按目录名一刀切，
    不看里面是不是文档）。
    这条把那个"暂时没人踩"的形状钉住：要么别在被剪的目录里写编号，要么把它挪进 `docs/`；
    出现引用又不处理，这里红。

    编号形状**复用尺子自己的 `_PID_RE`**，不自造第二份正则（替身必须跟真签名那条老规矩 ——
    我第一版顺手写了 `[PR]\d{1,3}-\d{1,3}|R102-\d+|ENGI-\d+`，那是"我以为尺子在用什么"；
    尺子真用的是 `\b([RP]\d+-\d+)\b`，口径不同，我那条自造的会多抓/少抓，红得就不是同一件事）。
    """
    import subprocess

    from consistency.checks_audit import _PID_RE  # 真尺子用的那一条

    # `-z` 不是风格：CI（Linux）的 `core.quotepath` 默认 true，`git ls-files` 会把非 ASCII
    # 路径输出成 `"data/lore/01-\345\237…"` —— 那个假名字切出来的第一段是 `"data`，匹配不上
    # `IGNORED_DIRS` 里的 `data` ⇒ **本档在 CI 上分母直接变 0**，被下面那句哨兵抓出来
    # （run 37860489135 实测：本地 5 支全绿、CI 红在这一格）。本机 Git-for-Windows 默认
    # false 所以永远复现不出来。`-z` 输出原始字节，两平台同形。
    raw = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=False,
    ).stdout
    tracked = [n.decode("utf-8", "replace") for n in raw.split(b"\x00") if n.strip()]
    if not tracked:
        pytest.fail("git ls-files 空：这条断言没有分母，不等于通过")

    scan = {".md", ".py", ".toml", ".txt"}
    offenders: list[tuple[str, list[str]]] = []
    buried_seen = 0
    for rel in tracked:
        parts = rel.split("/")
        buried = any(
            p in core.IGNORED_DIRS or p.startswith(core.IGNORED_DIR_PREFIXES)
            for p in parts[:-1]
        )
        if not buried or Path(rel).suffix not in scan:
            continue
        buried_seen += 1
        body = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        hits = sorted(set(_PID_RE.findall(body)))
        if hits:
            offenders.append((rel, hits))
    # 分母哨兵：现况是 10 个入库文件住在被剪的目录下（lore/.gitkeep/icon）。剪枝逻辑哪天
    # 缩到"什么都没剪"，这条会因 `buried_seen == 0` 当场红 —— 而不是空转"通过"：
    # 没有分母的绿灯与"量了说干净"长得一样（本仓为这一族立过三次尺子）。
    assert buried_seen > 0, (
        "被剪目录下没有任何入库文件可查：分母为 0，这条断言已经不再量任何东西"
    )
    assert not offenders, (
        f"被剪目录里出现了快照编号引用（{buried_seen} 个可查文件之中），两条审计尺子会静默"
        f"漏看它们：{offenders} —— 挪进 docs/，或者在 core._prune 里为它开一个**有主的**例外"
    )
