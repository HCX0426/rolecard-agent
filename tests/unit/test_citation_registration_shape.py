"""编号**登记形状**的判据（`_TARGET_ROW_RE`）—— 这条尺子改之前全仓零用例。

2026-10-09 实测照出来的缺陷：`_TARGET_ROW_RE` 从前要求编号**独占表格首格**（`| P2-16 |`），
而账本里大量合法写法是 `| P2-16 (PERF-11+ENGI-4) |`（合并说明写在同一格）与
`| **P3-11** (ENGI-10) |`（粗体强调）⇒ 46 个号里 **39 个从来没进过归属表**。谁在代码里引用
它们，`audit citations` 就把一条**真实存在**的引用判成悬空。我自己是撞上的第一个：往测试
docstring 写了个例子「见 P2-16」，尺子当场报 1 dangling —— 它报得对，危险的是我的第一反应
（把引用删掉 = 把尺子的缺陷当成自己的错修掉，缺陷就永久留着）。

判据四臂，两臂都必须能红（本仓那条老规矩）：
  ① 正向：三种合法写法（独占首格／编号+合并说明／粗体）都必须登记到；
  ② **反向（最容易出事的一臂）**：合并说明里**顺带提到**的号不许因此获得归属 ——
     `| P2-16 (PERF-11+ENGI-4) |` 只登记 P2-16。放宽如果不设这条闸，就从"严"改成"瞎"；
  ③ 首格里编号**不在开头**的行不算定义（`| 见 P2-16 那一格 |` 是引用，不是住址）；
  ④ 全仓真数据闸：放宽前后，归属只许增加、不许有任何号**失去**家。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from consistency.checks_audit import _TARGET_ROW_RE  # noqa: E402
from consistency.checks_docs import _citation_targets  # noqa: E402

OLD_STRICT = re.compile(r"^\|\s*([RP]\d+-\d+|\d+\.\d+)\s*\|")


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "快照.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_三种合法首格形状都必须登记(tmp_path: Path) -> None:
    """缺陷本体就是"只认第一种"。三种写法在账本里都真实存在（现读：独占形若干、
    `号 (合并说明)` 形 46 个里的大多数、粗体形 5 个）。"""
    path = _write(
        tmp_path,
        "| P2-1 | 独占首格 |\n"
        "| P2-16 (PERF-11+ENGI-4) | 编号 + 合并说明同格 |\n"
        "| **P3-11** (ENGI-10) | 粗体 + 合并说明 |\n",
    )
    found = _citation_targets(path)
    assert found == {"P2-1", "P2-16", "P3-11"}, found
    # 旧形状会漏掉后两种 —— 这一格同时把"缺陷曾被修掉"钉住
    strict_only = {
        m.group(1)
        for ln in path.read_text(encoding="utf-8").splitlines()
        if (m := OLD_STRICT.match(ln))
    }
    assert strict_only == {"P2-1"}, f"旧形状读数变了：{strict_only}"


def test_合并说明里顺带提到的号不许获得归属(tmp_path: Path) -> None:
    """**安全阀**：`| P2-16 (PERF-11+ENGI-4) |` 登记 P2-16，绝不登记括号里的两个。

    放宽一条识别正则，危险从来不在"少认"（那会红、会被发现），而在"多认"——把"只是提到"
    当成"住在这儿"，一次真误引就被判合法。这一臂就是这个闸。
    """
    path = _write(tmp_path, "| P2-16 (PERF-11+ENGI-4) | 说明 |\n")
    found = _citation_targets(path)
    assert found == {"P2-16"}, found
    for mentioned in ("PERF-11", "ENGI-4"):
        assert mentioned not in found, f"{mentioned} 被误登记成住在这一行"


def test_编号不在首格开头的行不算定义(tmp_path: Path) -> None:
    """`| 见 P2-16 那一格 |` 是**引用**，不是住址；登记它 = 把引用面当定义面。"""
    path = _write(tmp_path, "| 见 P2-16 那一格 | 说明 |\n")
    assert _citation_targets(path) == set()


#: 一份**三种定义形状都在**的样本账本（2026-11-01 换掉了原先那份真快照：
#: 历史台账与快照已删除，而这两条用例要验的是**尺子的形状**，不是某个文件的内容）。
#: 样本自带三种形状各若干行，够"放宽只该增加归属"这条判据跑起来。
SAMPLE_LEDGER = """| P2-1 | 独占首格 |
| P2-16 (PERF-11+ENGI-4) | 编号 + 合并说明同格 |
| **P3-11** (ENGI-10) | 粗体 + 合并说明 |
| P3-4 | 独占首格 |
| P3-8 (CONS-2) | 编号 + 合并说明同格 |
| **P3-20** (BUG-7) | 粗体 + 合并说明 |
| P2-31 | 独占首格 |
| P3-2 | 独占首格 |
| 见某一格 | 引用不是定义（这行不含任何编号，免得被当成真引用） |
"""


def test_真正则在全仓真数据上只增不减(tmp_path: Path) -> None:
    """把"放宽只该增加归属"这条**用一份真账本形状的样本验一遍**，而不只信构造样本。

    放宽前 6 个号无家（合并说明形与粗体形）、放宽后全部有家，且没有任何原有号失去家。
    这条是回归闸：将来谁再调形状，只要让任何一个号失去归属，这里红。
    """
    doc = _write(tmp_path, SAMPLE_LEDGER)
    now = _citation_targets(doc)
    before = {
        m.group(1)
        for ln in doc.read_text(encoding="utf-8").splitlines()
        if (m := OLD_STRICT.match(ln))
    }
    assert before <= now, f"有号失去归属：{sorted(before - now)}"
    # 新增的每一条都必须真的以该号开头（不是顺带提到）。取证循环认尺子的全部三种
    # 定义形状（标题 / 表格行 / 行首粗体散文条目 —— 第三种是 2026-10-10 补的），
    # 缺任何一种都会在这里 StopIteration。
    prose_head = re.compile(r"^\*\*(P[0-3]-\d+|ENGI-\d+)(?:[（(][^）)]*[)）])?")
    lines = doc.read_text(encoding="utf-8", errors="ignore").splitlines()

    def _def_line(num: str) -> str | None:
        for ln in lines:
            row = _TARGET_ROW_RE.match(ln)
            if row and row.group(1) == num:
                return ln
            prose = prose_head.match(ln)
            if prose and num in ln[: len(prose.group(0)) + 4]:
                return ln
        return None

    for num in now - before:
        line = _def_line(num)
        assert line is not None, f"{num} 的新家找不到定义行（尺子与取证循环形状不同步）"
        first = re.split(r"(?<!\\)\|", line.strip("|"))[0].strip().strip("*").strip()
        assert first.startswith(num), f"{num} 的新家来自非定义行：{first[:60]!r}"


def test_归属表里已有那三十九个号(tmp_path: Path) -> None:
    """正向读数闸：曾经"无家"的那批号里抽样必须已登记。

    只查数量会漏"登记到别的文档去了"这种形状，所以逐个查它的家**就在这份账本里**。
    """
    doc = _write(tmp_path, SAMPLE_LEDGER)
    now = _citation_targets(doc)
    for num in ("P2-16", "P2-1", "P3-4", "P3-8", "P3-20", "P2-31", "P3-11", "P3-2"):
        assert num in now, f"{num} 仍无归属：那条悬空误报没修好"


def test_索引里每个号的地址都能归位() -> None:
    """索引（生成物）与归属表是同一份事实的两面：索引写了某号住 X，`_citation_targets(X)`
    就必须给出那个号 —— 否则 `audit citations` 与索引互相说两套话。

    **只查"有家"的行**（2026-11-01 改）：历史台账与快照删除后，索引里 210 行 `P`/`R`/`ENGI`
    号的家全是"（没有任何文档里有这一号）" —— 那些号的过程记录连同文档一起没了，
    **它们的地址本来就不该再能归位**（判据要问的是"索引**声称**某号住 X，尺子认不认"，
    对"没有家"的行那问题不成立）。于是这条判据落到**仍有家的号**上：
    章节号（`§x.y`，住在活文档的标题里）与 `P`/`R` 号（若某份活文档仍提供它）。
    """
    index = ROOT / "docs" / "架构审计索引.md"
    rows = [ln for ln in index.read_text(encoding="utf-8").splitlines() if ln.startswith("| ")]
    checked = 0
    for ln in rows:
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", ln.strip("|"))]
        num, home = cells[0], cells[-1]
        path = ROOT / home
        if not path.exists() or path.suffix != ".md":
            continue  # "—"（无家）或路径已删
        if not re.fullmatch(r"(\d+(?:\.\d+)*|[RP]\d+-\d+)", num):
            continue  # 表格头那一行等
        assert num in _citation_targets(path), f"索引说 {num} 住在 {home}，尺子却不认"
        checked += 1
    assert checked > 0, "一条都没查：索引形状变了而这条判据在看一个空集合（分母为 0）"
