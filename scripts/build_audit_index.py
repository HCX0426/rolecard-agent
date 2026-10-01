"""生成 `docs/架构审计索引.md` —— 让"编号"当身份，文件位置只是存放地。

为什么要有这份索引（10-01，三份审计台账要全部归档时）：全仓代码/壳/测试里有 **444 处**
`§x.y` 与 `P{n}-{m}` 形式的引用，它们指向的是审计台账里某一节或某一行。归档前先试过一次
"把旧台账搬进 `docs/archive/`"，结果 `audit citations` 从 27 条黄跳到 124 条黄 —— 于是那份台账
一直原地留着，而"活文档"越堆越多。真正的问题是**引用把"存放地"当成了"身份"**：
编号本身是稳定的（`R28-56`、§12.19 都是），住哪个文件、在不在归档区，是存放细节。

这份索引就是那层地址：每条被引用的编号一行，写清它说的是什么、现在在哪个文件里。
搬一次档只需要重新生成它（一条命令），不需要动 444 处引用。

生成物**入库**，并由门禁的 `audit index in sync` 断言把着：`--check` 模式重算一遍比对字节，
不一致就红 —— 与 `check_dist_sync.py` 把 `frontend/dist` 钉成"第二份事实"是同一个形状。
所以它不会是又一份"写了没人更新"的目录：更新它是红的事，不更新才是。

跑法：
    .venv\\Scripts\\python.exe scripts/build_audit_index.py          # 写文件
    .venv\\Scripts\\python.exe scripts/build_audit_index.py --check   # 只问要不要重生成
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "架构审计索引.md"

#: 索引头部这段是给人读的规矩，不参与逐行比对之外的任何逻辑。
HEADER = """# 审计编号索引

> **这份文件由 `scripts/build_audit_index.py` 生成，不要手改。**
> 门禁的 `audit index in sync` 那条断言会重算一遍并与这份入库件比字节，不一致就红 ——
> 让索引成为一份会报警的产物，而不是一张"写了三个月就没人更新"的目录。

规矩：**编号是身份，文件位置只是存放地。** 代码里那 400 多处 `§x.y` / `P{n}-{m}` 引用指向的是
编号，不是某个路径下的某一节；三份审计台账搬进 `docs/archive/` 之后，引用照样有效，
因为地址在这一张表上。搬完档只需要重跑一次生成器。

| 编号 | 这一号说的是什么 | 现在住在哪个文件 |
|---|---|---|
"""


def _load_ruler():
    """借 `check_consistency.py` 的扫描器 —— 判据只能有一份，否则索引与断言会各自漂。"""
    script = ROOT / "scripts" / "check_consistency.py"
    spec = importlib.util.spec_from_file_location("cc_for_index", str(script))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _heading_text(path: pathlib.Path, number: str) -> str:
    """该文档里这一号那一行的文字（标题行取标题，表格行取第二格），压掉竖线与多余空白。"""
    from check_consistency import _TARGET_HEAD_RE, _TARGET_ROW_RE  # noqa: PLC0415

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        head = _TARGET_HEAD_RE.match(line)
        if head and head.group(1) == number:
            return line.lstrip("#").strip().removeprefix(number).strip() or "(无标题)"
        hit = _TARGET_ROW_RE.match(line)
        if hit and hit.group(1) == number:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            body = cells[1] if len(cells) > 1 else ""
            cleaned = body.replace("\\|", "｜").replace("**", "").replace("`", "").strip()
            return (cleaned[:70] + "…") if len(cleaned) > 71 else (cleaned or "(空)")
    return "(找不到那一行)"


def scanned_citations() -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    """(被引用的编号, 编号 → 住在哪些文档, 文档 → 它提供的编号)。

    **索引生成器与 `audit citations` 那条断言共用这一个函数** —— 两边各扫一遍就会有两套
    "什么算被引用"，而这两套迟早分叉（本仓为这件事立过三次尺子也犯过三次）。
    """
    ruler = _load_ruler()
    docs = sorted((ROOT / "docs").rglob("*.md"))
    targets = {path: ruler._citation_targets(path) for path in docs}
    numbers: set[str] = set()
    for path in ruler.iter_files(".py", ".ts", ".tsx", ".js"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            for match in ruler._SECTION_RE.finditer(line):
                numbers.add(match.group(1))
            numbers.update(ruler._PID_RE.findall(line))
    homes: dict[str, set[str]] = {}
    per_doc: dict[str, set[str]] = {
        path.relative_to(ROOT).as_posix(): pool for path, pool in targets.items()
    }
    for number in numbers:
        found = {rel for rel, pool in per_doc.items() if number in pool}
        homes[number] = found
    return numbers, homes, per_doc


def render() -> str:
    """整份索引的正文。确定性输出：编号排序、路径统一 posix。"""
    numbers, homes, _ = scanned_citations()
    rows: list[tuple[str, str, str]] = []
    for number in sorted(numbers, key=lambda item: (item.split("-")[0], item)):
        found = sorted(homes[number])
        if not found:
            # 查无此号：让它在索引里显形，而不是静默少一行 —— 少了哪一行，
            # `audit citations` 那条本来就红，这里再留一句"没有家"免得看着像生成器坏了。
            rows.append((number, "(没有任何文档里有这一号)", "—"))
            continue
        first = ROOT / found[0]
        title = _heading_text(first, number) if first.exists() else "(读不到)"
        rows.append((number, title, ", ".join(found)))

    out = [HEADER]
    for number, title, home in rows:
        out.append(f"| {number} | {title.replace('|', '｜')} | {home} |")
    out.append("")
    out.append(
        f"共 {len(rows)} 条编号。生成方式：`.venv\\\\Scripts\\\\python.exe "
        "scripts/build_audit_index.py`（`--check` 只问要不要重生成）。"
    )
    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成/校验审计编号索引")
    parser.add_argument("--check", action="store_true", help="只比对，不写文件")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "scripts"))
    text = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current == text:
            print("索引与重算结果逐字一致")
            return 0
        print(
            f"索引过期：入库 {len(current.splitlines())} 行 / 重算 {len(text.splitlines())} 行",
            file=sys.stderr,
        )
        print("  重跑：.venv\\Scripts\\python.exe scripts\\build_audit_index.py", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"已写 {OUT.relative_to(ROOT).as_posix()}（{len(text.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
