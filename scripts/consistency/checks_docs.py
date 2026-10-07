"""docs 主题这一族判据（P3-9 按主题细分，从 checks.py 平移，正文一字未改）。

按主题拆出；执行顺序由 registry.CHECKS 唯一决定，本模块只回答"这一族住哪"。
行为等价由门禁实跑全部 CHECKS 证明。
"""

from __future__ import annotations

import datetime
import pathlib
import re

# 跨族共享助手：唯一定义在别的族模块，按「谁在用谁 import」接线（不复制定义）。
from .checks_audit import (  # noqa: F401 - 共享助手，定义在那边
    _TARGET_HEAD_RE,
    _TARGET_ROW_RE,
    _git_ignored,
)
from .core import ROOT, fails, iter_files, out

_HEADLINE_ZONE_LINES = 35

_READINGS_LINK = "[docs/gate-readings.json](docs/gate-readings.json)"

def check_readme_headline_numbers() -> None:
    """README 首屏**不许再出现那三个数**，且必须给出读数文件的链接 —— 数字的家搬到了那边。

    这条尺子的形状被同一句话逼着换过两次："手抄的数不管测得多准都会再漂"。

      * 第一次（读数机之前）：数在散文里，漂了没人知道（那组数三周漂了 2.8 倍而无尺子）；
      * 第二次（对读时期）：数交给 `docs/gate-readings.json`、由这把尺子拿首屏去比 ——
        拦住了手抄，却养出一整条流水线：head 归属那一问、末尾的收尾步、以及**每轮一笔
        纯数字提交**（一个会话 13+ 笔，其中一半是"把一个还对的数改成另一个还对的数"）。
        对读治好了"漂"，没治"吵"；
      * 现在（拍板的"数字移出散文改 badge/引用"——**引用**半边；badge 半边依赖仓库公开
        状态、离线不可得，记在账本）：首屏连"要更新的字段"都不再有。数字只活在
        `docs/gate-readings.json`（门禁自己写：覆盖率家族入库，用例数与 head 落 gitignore
        的 scratch —— 快档每跑一趟都改它们，留在入库文件里就是每轮一笔纯数字提交）。
        判据随之**反过来**：

        ① 首屏 35 行内三个头条形状**不许出现**（有人抄回来就红 —— 这一条会拦住下一个
           把数写回去的人，包括我）；
        ② 链接**必须在**（连引用都丢了 = 数字的家没了门牌，读者找不到现值）。

    历史引文刻意只圈首屏：首屏之外那句"当年 457 漂成…"是记录，不是现值，给它设防
    等于让这把尺子去起诉历史。
    """
    readme = ROOT / "README.md"
    lines = readme.read_text(encoding="utf-8", errors="ignore").splitlines()
    zone = "\n".join(lines[:_HEADLINE_ZONE_LINES])
    problems: list[str] = []
    for pattern, label in (
        (r"\*\*\d+ 个后端测试", "后端测试数"),
        (r"\d+ 个前端测试", "前端测试数"),
        (r"覆盖率 \d+\.\d+%", "覆盖率值"),
    ):
        hit = re.search(pattern, zone)
        if hit:
            line_no = zone[: hit.start()].count("\n") + 1
            problems.append(
                f"首屏第 {line_no} 行又抄回了{label}（{hit.group(0)!r}）"
                " —— 这一格的家在 docs/gate-readings.json，散文里出现即漂"
            )
    if _READINGS_LINK not in zone:
        problems.append(
            f"首屏没有指向读数文件的链接（需要 {_READINGS_LINK} 这个形状）"
            " —— 数字的家不能没有门牌"
        )
    ok = not problems
    out(
        "README headline numbers",
        ok,
        "; ".join(problems[:3])
        if problems
        else (
            f"首屏 {_HEADLINE_ZONE_LINES} 行内零头条数字、引用在"
            "（现值见 docs/gate-readings.json，由门禁自己写）"
        ),
    )
    if problems:
        fails.append(f"README headline literals: {problems}")

def check_promised_artifacts() -> None:
    promised = [
        "pyproject.toml",
        "LICENSE",
        "CONTRIBUTING.md",
        "README.md",
        ".gitattributes",
        ".gitignore",
        ".env.example",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-api.txt",
        "requirements-rag.txt",
        "requirements-cloud.txt",
        "requirements-ocr.txt",
        "docs/需求与验收标准.md",
        "docs/架构总览.md",
        "docs/前端设计.md",
        "docs/开发流程.md",
        "src/rolecard_agent/core/schema.sql",
        "src/rolecard_agent/core/guard.py",
        "src/rolecard_agent/base/text.py",
        "src/rolecard_agent/core/prompts.py",
        "src/rolecard_agent/core/tools/registry.py",
        "src/rolecard_agent/roles/models.py",
        "src/rolecard_agent/roles/seed.py",
        "src/rolecard_agent/roles/schema.sql",
        "src/rolecard_agent/domains/registry.py",
        "src/rolecard_agent/domains/health/schema.sql",
        "tests/unit",
        "tests/integration",
        "tests/eval",
        "tests/eval/cases",
        "tests/eval/cases/health.json",
        "frontend/package.json",
        "frontend/dist/index.html",
    ]
    absent = [p for p in promised if not (ROOT / p).exists()]
    detail = f"missing: {absent}" if absent else f"{len(promised)} present"
    out("promised artifacts", not absent, detail)
    if absent:
        fails.append(f"plan promises artifacts that do not exist: {absent}")

def check_readme_quickstart() -> None:
    """The plan's P4 exit criterion is "clone and run in 3 commands" - the README must deliver it.

    Regression guard: this section was lost once during a rewrite, while both docs/实施计划.md and
    docs/需求与验收标准.md still claimed the criterion was met (C21).
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = [
        "## 快速开始",
        "scripts/init_db.py",
        "scripts/check_consistency.py",
        "requirements.txt",
    ]
    absent = [r for r in required if r not in readme]
    out("readme quickstart", not absent, f"missing: {absent}" if absent else "present")
    if absent:
        fails.append(f"README is missing quickstart pieces: {absent}")

def check_doc_references() -> None:
    """Docs are referenced by semantic filename now, not by a number.

    Numbers were dropped because merging two docs silently invalidates every numbered link
    (which is exactly what happened: three separate files all ended up as `docs/02`).
    This asserts the old numbered form never creeps back in.
    """
    pattern = re.compile(r"docs/0[1-9]")
    offenders: list[str] = []
    for path in iter_files(".md", ".py", ".toml", ".txt"):
        # This file explains why the numbered form was dropped, so it necessarily
        # contains an example of it. （拆包后判据住 consistency/ 包，连同包装器一起豁免。）
        if path.is_relative_to(ROOT / "scripts" / "consistency"):
            continue
        if path == ROOT / "scripts" / "check_consistency.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    out("doc references", not offenders, str(offenders) if offenders else "semantic names only")
    if offenders:
        fails.append(f"numbered docs references found: {offenders}")

def check_doc_links() -> None:
    """Backticked repo paths in the docs must point at files that actually exist.

    After the docs were consolidated (7 files -> 4, numbers replaced by semantic names),
    nothing verified that the remaining cross-references still resolved. A dead link in a
    README is the first thing a reviewer hits.
    """
    # Documented before they exist, on purpose.
    # `tests/eval/report.json` 与 `tests/eval/harness/` 是**脚本按需生成**的产物（跑批/链路
    # 自检），全新 clone 里本来就不存在 —— 它们是"跑出来的"而不是"仓库里的"，因此不参与
    # "文档里的路径必须存在"这条校验。
    not_yet = {
        "requirements.lock",
        ".env",
        "data/sqlite/app.db",
        "tests/eval/report.json",
    }
    bare = {"pyproject.toml", "README.md", "CONTRIBUTING.md", "LICENSE"}
    # Only repo-relative references are validated. Docs also use in-package shorthand such
    # as `core/prompts.py`, which is not a path from the repo root - validating those
    # produced nothing but noise.
    # `build/` 是 09-30 加进来的（`R28-47`）：文档里按文件名点名的**证据**多数长在那儿
    # （`build/backup-liveroot-*.zip` 之类的装包前备份）。决策 4 清盘之后那些名字就悬空了，
    # 而这条检查当时看不见它们 —— "数字仍在档里，但复跑不回来"没人报。
    # **但那个前缀只有在配合下面的 gitignore 分区之后才成立**：`build/` 整个是被忽略的暂存区，
    # 直接按"存在吗"判，得到的结论只在这台机器上成立（本机全绿、CI 红 10 处，见 `_git_ignored`）。
    prefixes = ("docs/", "src/", "scripts/", "tests/", "data/", "build/")
    # 字符类必须含中文：**整个中文文件名文档树原本是这条检查的盲区**。09-26 轮 R26-20 实测：
    # 把 CJK 放进来之后立刻抓到 7 处 living docs 指着已经搬进 archive/ 的《技术评审与决策》
    # 《实施计划》，而在此之前这条检查报的是 "all resolve"。
    # markdown 链接的目标 `](a.md)` 也一起看 —— 那是真链接，不是包内简写，误报面为零。
    cjk = "".join(chr(c) for c in range(0x4E00, 0xA000)) + "\uff08\uff09\u3001\u00b7\u2014"
    # 半角空格进字符类（`R102-34`）：三份台账的文件名全带空格（`架构审计（2026-09-28 轮）.md`），
    # 从前 `name_cls` 不收空格 ⇒ 引用它们的 6 处**整段吃不到** —— 把《…轮》删掉这条断言照打
    # "all resolve"。结尾字符类不能带空格（文件名不以空格结尾），分隔符继续点横线。
    name_cls = f"{cjk}A-Za-z0-9_ "
    pattern = re.compile(
        rf"`([{name_cls}][{name_cls}.\-/]*\.(?:md|py|toml|txt|sql|json|cfg|ini|zip))`"
    )
    # `]()` 形态同样收空格（`R102-34`）：`[^)\s#]` → `[^)#]`，锚点 `#` 与右括号仍是界。
    link_pattern = re.compile(r"\]\(([^)#]+?\.(?:md|png|jpg|json))\)")

    def resolvable(md_path: pathlib.Path, ref: str) -> bool:
        """仓库相对路径按仓库根解；裸文件名（含 `../` 形式）按本文件所在目录解。"""
        if (ROOT / ref).exists():
            return True
        return (md_path.parent / ref).exists()

    broken: list[str] = []
    unresolved: list[tuple[str, str]] = []
    # 一条刻意不参与：
    #  * `docs/archive/` 是**封存件** —— 里面的路径是"写它的那天"的事实，按 R26-19 的同一个
    #    决定（引用可达性进门禁，但归档档里的编号与路径原地不动）不去追修它们。
    def out_of_scope(rel: pathlib.Path) -> bool:
        return "archive" in set(rel.parts)

    for path in iter_files(".md"):
        rel = path.relative_to(ROOT)
        if out_of_scope(rel):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            for ref in pattern.findall(line) + link_pattern.findall(line):
                if "*" in ref or ref in not_yet:
                    continue
                is_prefixed = ref.startswith(prefixes)
                is_bare_md = "/" not in ref and ref.endswith(".md")
                if not (is_prefixed or is_bare_md or ref in bare):
                    continue
                if not resolvable(path, ref):
                    unresolved.append((f"{rel}:{lineno}", ref))
    # 判据不许落在 gitignore 的暂存区上：分不出这一半，同一条检查就会本机绿、CI 红，
    # 而那个"红"里混着"这台机器上有过那个文件"这种不可复跑的事实 —— 比没有尺子更坏。
    ignored, asked = _git_ignored([ref for _, ref in unresolved])
    skipped = 0
    for where, ref in unresolved:
        if ref in ignored:
            skipped += 1
            continue
        broken.append(f"{where} -> {ref}")
    if broken:
        detail = "; ".join(broken[:4])
    else:
        detail = "all resolve"
        if skipped:
            detail += f"（另有 {skipped} 处点名 gitignore 暂存区里的产物，按设计不参与判据）"
        if not asked:
            detail += " [警告：未能询问 git，暂存区那一半没分区]"
    out("doc links", not broken, detail)
    if broken:
        fails.append(f"broken doc references: {broken}")

def _unescaped_pipes(text: str) -> list[int]:
    """每个未转义 `|` 的下标。"""
    return [m.start() for m in re.finditer(r"(?<!\\)\|", text)]

def _is_table_delim(line: str) -> bool:
    body = line.strip()
    return (
        body.startswith("|")
        and body.endswith("|")
        and bool(body)
        and set(body.replace("|", "").strip()) <= set("-: ")
    )

def check_markdown_table_shape() -> None:
    """GFM 表格每一行的列数不能超过表头，且行必须自己收尾。

    为什么要有这条（09-26 轮，修完 12 处之后）：**一个没转义的竖线会静悄悄地把那一格劈成
    两格**，整行右移一列 —— 渲染出来字数对、内容看着也在，但"证据"那一列里装着"复验"的话。
    另一种是**长行折成几个物理行**：表格在那一行就终止了，后面的续行掉成散段落，紧随其后的
    那些正常行还会变成"没有表头的第二张表"。这两种都不报错，而《架构审计》这种**按列读**的
    台账恰恰全靠列位对齐。

    **归档件照问**（10-01 改；从前这里跟着 `check_doc_links` 豁免 `docs/archive/`）：`R28-63`
    把三本台账全部搬进归档的那一刻，这条尺子就**从此看不见台账本身**了 —— 它防的那一类缺陷
    住在按列读的长行里，而那种行只有台账才有。实测代价是当场照出两处：归档件里一处存量窄行，
    以及我自己 `R28-64` 那一行被物理折成 9 行、在"45 条全绿"里过了一趟门禁。
    封存的意思是"内容不再改写"，不是"格式不再需要成立"。
    """
    offenders: list[str] = []
    for path in iter_files(".md"):
        rel = path.relative_to(ROOT)
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        i = 0
        while i < len(lines):
            if not lines[i].lstrip().startswith("|") or i + 1 >= len(lines):
                i += 1
                continue
            if not _is_table_delim(lines[i + 1]):
                i += 1
                continue
            header = len(_unescaped_pipes(lines[i])) - 1
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                row = lines[i]
                cells = len(_unescaped_pipes(row)) - 1
                if cells > header:
                    offenders.append(f"{rel}:{i + 1} 行 {cells} 列 > 表头 {header} 列")
                elif cells < header:
                    # 少一格同样要报：GFM 会在**末尾**补一个空格子让它"看起来没事"，
                    # 而按列读的台账里这意味着某一格的内容坐进了别的列。
                    # 10-01 之前这条只判 `>` 不判 `<`，于是三行 09-26 的 S 系列条目
                    # 与我自己刚写的 `R28-60` 都错位了而检查全绿（"all rows match their header"
                    # 那句话当时是假的）。空格子要**显式写出来**（同表里 `S-4′` 就是那么写的）。
                    offenders.append(
                        f"{rel}:{i + 1} 行 {cells} 列 < 表头 {header} 列"
                        "（少一格 = 某一格的内容坐进了别的列；不填的那格要写成空的 `| |`）"
                    )
                if not row.rstrip().endswith("|"):
                    offenders.append(f"{rel}:{i + 1} 这一行没有收尾的 `|`（表格在此被折断）")
                i += 1
            # 表格后面紧贴着一条不是表格的行 = 上一行其实是折行的续文
            if i < len(lines) and lines[i].strip() and not lines[i].lstrip().startswith(
                ("|", "#", ">", "-", "*", "`", "!", "[")
            ):
                offenders.append(f"{rel}:{i + 1} 表后紧跟游离行（多半是折断的续文）")
    out("markdown table shape", not offenders, "all rows match their header"
        if not offenders else f"{len(offenders)} 处")
    fails.extend(offenders)

def _citation_targets(path: pathlib.Path) -> set[str]:
    """一份文档里所有"能被指到"的编号。"""
    if not path.exists():
        return set()
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        for regex in (_TARGET_HEAD_RE, _TARGET_ROW_RE):
            hit = regex.match(line)
            if hit:
                found.add(hit.group(1))
    return found

_HEADER_DATE = re.compile(r"最后更新[：:]\s*(\d{4})-(\d{1,2})-(\d{1,2})")
_FULL_DATE = re.compile(r"(?<![\d-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d-])")
_BARE_DATE = re.compile(r"(?<![\d-])(\d{1,2})-(\d{1,2})(?![\d-])")

def _dates_in(text: str, today: datetime.date) -> list[datetime.date]:
    """正文里出现过的**过去的**日期（含今天）。未来的那些是计划，不该拿去要求头部。

    裸 `MM-DD` 按本年解释（本仓的散文就是这么写日期的：「09-28 那次」）。
    `R28-17` 这类编号不会被误认：`28-17` 前面是字母 `R`，`\\b` 在那里不成边界。
    """
    found: list[datetime.date] = []
    for year, month, day in _FULL_DATE.findall(text):
        try:
            d = datetime.date(int(year), int(month), int(day))
        except ValueError:
            continue
        if d <= today:
            found.append(d)
    for month, day in _BARE_DATE.findall(text):
        try:
            d = datetime.date(today.year, int(month), int(day))
        except ValueError:
            continue
        if d <= today:
            found.append(d)
    return found

def check_doc_freshness() -> None:
    """活文档头部那句「最后更新」不许比它自己正文里出现过的日期更旧。

    为什么立它（10-01，`R28-41` 复核时当场抓出来的一处新漂）：`docs/前端设计.md` 与
    `docs/架构总览.md` 头部都还写「2026-09-28」，而正文里躺着 09-29 的组名、09-30 的换形象与
    Live2D、10-01 的一整批 —— 也就是说**这句话正在对每一个读者撒谎**，而它撒谎的方式恰好是
    "让人以为后面的内容不用再看"。这与体积、用例数是同一族"没有尺子的数"：
    `R28-47` 说"数会漂、物会没"，日期是第三种：**日期会过期**。
    归档件（`docs/archive/`）照旧不问 —— 那里封的是"不再改写"，头部日期本来就是当时的快照。
    """
    today = datetime.date.today()
    offenders: list[str] = []
    checked = 0
    for path in iter_files(".md"):
        rel = path.relative_to(ROOT)
        if "archive" in rel.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        header = _HEADER_DATE.search(text)
        if not header:
            continue
        checked += 1
        try:
            declared = datetime.date(*(int(g) for g in header.groups()))
        except ValueError:
            offenders.append(f"{rel} 头部那句「最后更新」不是个合法日期")
            continue
        later = [d for d in _dates_in(text, today) if d > declared]
        if later:
            newest = max(later)
            offenders.append(
                f"{rel} 头部写「最后更新 {declared.isoformat()}」，而正文里出现过 "
                f"{newest.isoformat()}（共 {len(later)} 处比它晚）—— 这句话会让读者跳过新内容"
            )
    out(
        "doc freshness",
        not offenders,
        f"{checked} 份带「最后更新」的活文档都比自己正文里最近的日期新"
        if not offenders
        else "; ".join(offenders[:4]),
    )
    if offenders:
        fails.append(f"doc freshness: {offenders}")
