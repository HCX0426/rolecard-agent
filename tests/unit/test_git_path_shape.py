"""git 路径形状：凡解析 `git ls-files`/`diff --name-only` 的文本输出，都必须关掉 quotepath。

根因（2026-10-09 由 CI 现场量出，run 37860489135 与 37852226595）：`core.quotepath` 在
**Linux 上默认 true**（Git for Windows 默认 false），true 时 git 把非 ASCII 路径输出成
**带双引号的八进制转义**：`"data/lore/01-\345\237\272…md"`。四处在解析这个文本，于是：

  * `scan_secrets.py` 的影子树拿假名字去 `is_file()` 必然假 ⇒ **本次要发的 37 个文件一个都没
    进密钥扫描，而那一步退 0、绿**（日志里只有一行「竞态/目录项」的小字，正是掩体）；
  * `gate.py` 的改动清单 `p.startswith("src/")` 对中文路径**永远判假** ⇒ 覆盖率被判"没改 src"
    而静默跳过 —— 那是**变弱**，正撞本函数注释里"绝不因探测失误而悄悄削弱安全网"那句；
  * `checks_runtime.py` 那条判据 `.exists()` 永远假 ⇒ 中文命名的 `.py`/`.toml` 会**静默不进
    扫描集合**（今天恰好 0 命中：本仓非 ASCII 路径全是 `.md`，不在它的扩展名列表里 —— 是巧合
    不是安全）；
  * 本仓自己那档剪枝用例的分母在 CI 上直接变 0（被它自己的分母哨兵抓成红）。

**为什么这档以结构臂为主**：`gate._git()` 与 `checks_runtime` 都以 `cwd=ROOT`（真仓库）跑，
行为臂要在本机造出"CI 的形状"只能靠环境变量注入 —— 那条路我手动跑过一遍（强制
`core.quotepath=true` 后四处全过），但写进用例会变成"赌本机 git 版本怎么处理这几个变量"。
所以这里钉**形状**（每个站点都带开关），真行为臂住在 `test_scan_secrets_path_scope.py`：
那里用 `tmp_path` 建真仓库 + `git config core.quotepath true`，把 CI 的形状搬进用例，
并做过撤 `-z` 的变异实测。最后那一支用真子进程问一遍本机，堵"源码里写了开关而实际没生效"。

**10-09 晚补（全仓 23 个 git 调用点盘完之后）**：`checks_audit._git_ignored` 与 `diff_coverage._git`
两处判定受影响的站点也进了本档。`_git_ignored` 那支是**真行为臂**（临时仓 + 仓内
`git config core.quotepath true` = CI 形状，不赌环境变量）：这条分区的立身之本是"送进去什么、
回出来什么逐字相等"，quotepath 开着就整体失效 —— 该函数已经栽过一次同款（`text=True` 的 `\r\n`），
这是第二次。`diff_coverage` 的判据方向是**静默变弱**（`+++ b/` 头认不出 ⇒ 文件从分母消失 ⇒
改动行覆盖率地板在它身上失效）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_gate的git单点关掉quotepath() -> None:
    """`_git()` 是 gate 全部 git 查询的**单点** —— 一处带开关，`_changed_paths()` 与
    `_src_changed()` 两条受害路径一起收口。"""
    src = _src("scripts/gate.py")
    assert '"core.quotepath=false"' in src, (
        "gate._git 又回到不锁 quotepath：Linux 上中文路径被转义 ⇒ "
        '改动清单里 `p.startswith("src/")` 判假 ⇒ 覆盖率静默跳过（变弱）'
    )
    # 每一条 git 查询都必须经过带开关的那一处：数一下 `["git"` 出现几次、带开关的几次
    total = src.count('["git"')
    guarded = src.count('["git", "-c", "core.quotepath=false"')
    assert total == guarded, (
        f"gate.py 里 {total} 处 subprocess 起 git，只有 {guarded} 处锁了形状：漏网的那些"
        "在 Linux 上会拿到转义过的路径"
    )


def test_一致性尺子的扫描集合也锁形状() -> None:
    src = _src("scripts/consistency/checks_runtime.py")
    assert '"core.quotepath=false"' in src, (
        "checks_runtime 的 ls-files 没锁形状：中文命名的 .py/.toml 会静默不进扫描集合"
    )


def test_影子树的名单走z分隔且missing必抛() -> None:
    """影子树这一处**两个都要**：`-z` 保证名字可用于 `is_file()`；`ScopeMismatch` 保证
    "名单里有、树里没有"不再是打印一声就退 0（那句「竞态/目录项」正是 37 个文件的掩体）。"""
    src = _src("scripts/tools/scan_secrets.py")
    assert '"-z"' in src, "_tracked_files 丢了 -z：CI 上又会静默少扫非 ASCII 路径那批文件"
    assert "ScopeMismatch" in src, "missing 不再抛 = 掩体装回来了（只 print 一声然后退 0）"


def test_本机真实仓在当前配置下确实拿得到原名() -> None:
    """分母哨兵：拿真仓库跑一次，确认名单里没有任何"带引号的八进制"形状。

    只查源码形状有个漏洞：万一某天 git 版本或本机配置变了，四行 `assert 字符串 in src`
    照样全绿，而真实输出已经是转义的 —— 这一发用真子进程问一遍。
    """
    raw = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "-z"],
        cwd=ROOT, capture_output=True, check=False,
    ).stdout
    names = [n.decode("utf-8", "replace") for n in raw.split(b"\x00") if n.strip()]
    assert len(names) > 500, f"名单才 {len(names)} 条：不像这份仓库，分母可疑"
    mangled = [n for n in names if n.startswith('"') or "\\3" in n]
    assert not mangled, f"名单里有转义形状（说明开关没生效）：{mangled[:3]}"


def test_diff_coverage的git也锁形状() -> None:
    """`diff_coverage._git` 是它全部 git 查询的单点；`git diff` 的 `+++ b/` 头在 quotepath
    开启时整体加引号+转义 ⇒ `parse_changed_lines` 认不出那条头 ⇒ **文件从分母里静默消失**，
    改动行覆盖率的地板（`diff_coverage_floor`）在它身上失效 —— 判据方向是"变弱"。"""
    src = _src("scripts/diff_coverage.py")
    total = src.count('["git"')
    guarded = src.count('["git", "-c", "core.quotepath=false"')
    assert total == guarded > 0, (
        f"diff_coverage 里 {total} 处起 git、{guarded} 处锁了形状："
        "漏网那处的 diff 头会在 Linux 上被转义，改动行静默少算"
    )


def test_git_ignored在quotepath开启时仍分区得动(tmp_path: Path, monkeypatch) -> None:
    """**真行为臂**：临时仓把 `core.quotepath` 设成 true（= CI 的 Linux 形状），把一个**中文命名
    的被忽略文件**点名给 `_git_ignored` —— 它必须仍然把这条从判据里分区出去。

    这条函数的立身之本就是"送进去什么、回出来什么逐字相等"（docstring 里已记过一次同款事故：
    `text=True` 的 `\r\n` 让 git 回 `"a.json\r"` ⇒ 分区静默失效、本机看不出来）。quotepath 是
    同一条病的第二个入口：命令行 `-c core.quotepath=false` 压过仓内 true，撤掉开关这发就红。
    """
    repo = tmp_path / "repo"
    (repo / "build").mkdir(parents=True)
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    (repo / "build" / "中文暂存件.json").write_text("{}", encoding="utf-8")

    def git(*a: str) -> None:
        subprocess.run(["git", "-C", str(repo), *a], capture_output=True, check=True)

    git("init", "-q")
    git("config", "user.email", "probe@example.invalid")
    git("config", "user.name", "probe")
    git("config", "core.quotepath", "true")  # CI 的 Linux 默认形状
    git("add", ".gitignore")
    git("commit", "-qm", "base")

    from consistency import checks_audit as audit

    monkeypatch.setattr(audit, "ROOT", repo)
    ignored, asked = audit._git_ignored(["build/中文暂存件.json", "src/real.py"])
    assert asked, "问 git 失败（这条哨兵自己先要有分母）"
    assert "build/中文暂存件.json" in ignored, (
        f"中文忽略路径没被分区出去（quotepath 又把回信转义了？）：{ignored}"
    )
    assert "src/real.py" not in ignored, f"没被忽略的路径混进来了：{ignored}"


def test_ci的paths忽略名单只许含散文类() -> None:
    """`paths-ignore` 是**静默生效**的：glob 写错 = 永远不触发 CI，最坏的静默失效形状。

    所以名单的内容要钉死 —— 只许"绝不影响被测物"的散文类（docs/** 与 **.md）；
    pyproject / 锁 / 工作流 / 源码 / 测试**绝不该进**名单（它们改一行就真能改行为，
    被这份名单吞掉的 push 连一个红都不会给你）。账本尺子（audit citations 等）在
    下一笔碰代码的 push 里照跑，漏不了。
    """
    import yaml

    d = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    push = d[True]["push"] if True in d else d["on"]["push"]
    ignore = push.get("paths-ignore") or []
    allowed = {"docs/**"}
    assert set(ignore) <= allowed, (
        f"paths-ignore 混进了非 docs 条目：{sorted(set(ignore) - allowed)} —— "
        "名单每加一行都在缩小 CI 的看守范围，只许显式过这一格"
    )
    # README.md **刻意不豁免**：它是门面文档，quickstart/里程碑/公网变量/首屏数字六条尺子
    # 把着它 —— 2026-10-09 首版把 `**.md` 放进来，README 重写那笔被整个吞掉（CI 一行没跑）。
    assert "**.md" not in ignore, "README.md 不能豁免：改它就是改被测物"
    # 反向：真正影响被测物的东西绝不该出现在忽略名单
    for forbidden in ("pyproject.toml", "requirements", "src/**", "tests/**", ".github/**"):
        assert not any(forbidden in e for e in ignore), f"{forbidden} 不许被忽略"
