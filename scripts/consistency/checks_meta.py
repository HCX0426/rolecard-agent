"""meta 主题这一族判据（P3-9 按主题细分，从 checks.py 平移，正文一字未改）。

按主题拆出；执行顺序由 registry.CHECKS 唯一决定，本模块只回答"这一族住哪"。
行为等价由门禁实跑全部 CHECKS 证明。
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys
import tomllib

from .core import ROOT, fails, iter_files, out, warns


def check_stale_identifiers() -> None:
    """Renamed keys / old project names must not survive outside the archive."""
    stale = [
        "langgraph-health-agent",
        "langgraph==1.2.6",
        "langchain-ollama==0.2.0",
        "chromadb==0.6.0",
        "OBS_REDACT_TEXT",
        "只保留三个里程碑",
    ]
    allowed = {
        # 判据自己的名单就住在**本模块**（`checks_meta.py`）里 —— 自扫描会把这份名单
        # 当成命中，豁免它。名单从 checks.py 搬来时这一格必须跟着改：豁免指向的是
        # "名单住哪"，不是那个旧文件名，指错了就变成自己红自己（P3-9 细分时实测到）。
        ROOT / "scripts" / "consistency" / "checks_meta.py",
    }
    archive = ROOT / "docs" / "archive"
    suffixes = {".md", ".py", ".toml", ".sql", ".cfg", ".ini", ".example"}
    candidates = [*iter_files(*suffixes), ROOT / ".gitignore", ROOT / ".gitattributes"]
    for path in candidates:
        if not path.exists() or path in allowed or archive in path.parents:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in stale:
            if token in text:
                warns.append(f"{path.relative_to(ROOT)} still mentions {token!r}")
    out("stale identifiers", not warns, f"{len(warns)} hit(s)" if warns else "clean")
    fails.extend(warns)


def _imported_modules(code: str) -> list[str]:
    """从一段 `python -c` 的串里只取**被 import 的模块名**（走 AST，不是正则）。

    为什么走 AST（P3-9「解析器 AST/tomllib 化」）：正则版**两度把一条正确诊断说成两条**——
    `from rolecard_agent.config import DEFAULT_SILICONFLOW_BASE_URL` 里那个 `DEFAULT_…`
    是被导入的**符号**而不是模块，靠 `import` 后面那段模式去"绕开"它，绕法本身还要再
    猜一层（`;` `)` 换行、`as` 别名、逗号续行……每加一种写法就多一处猜错）。而 AST 里
    `ImportFrom.module` 与 `Import.names` 是**两种节点**，模块与符号在结构上是分开的，
    不需要猜：from-import 只认 `node.module`，裸 import 只认 `alias.name`。
    `from . import x` 这种（`node.module is None`、level>0）是相对导入，按"不给模块名"跳过。

    顺序仍不承载意义，去重排序与旧版一致 —— 免得调用方把"两个名字换了个位"读成行为变化。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # 调用方喂进来的是 **ci.yml 里 `-c` 后面的整段**，带着缩进与 shell/YAML 的外层
        # 引号括号（`… -c "import os")"`）—— 正则版能"透过"这些噪声匹配，AST 不行。
        # 所以先做一次**保守归一化**再解析：去缩进 + 剥掉最外层配对的引号/圆括号。
        normalized = code.strip()
        while len(normalized) >= 2 and normalized[0] in "\"'" and normalized[-1] in "\"')":
            # 只剥"确实成对"的那一层，且不剥到内容里自己的引号里去
            if normalized[-1] in "\"'" and normalized[0] == normalized[-1]:
                normalized = normalized[1:-1].strip()
            elif normalized[-1] == ")":
                normalized = normalized[:-1].strip()
            else:
                break
        try:
            tree = ast.parse(normalized)
        except SyntaxError:
            # **不能静默返回 []**：那等于"解析失败 ⇒ 判绿"，而这条尺子的存在意义就是
            # "宿主机 import 项目包必须红" —— 解析不出就当没看见，恰恰把它变成了摆设
            # （第一版 AST 化就是这么把一支必红的用例变绿的）。解析不了就**如实回一个
            # 让调用方判红的信号**：返回原始串里像模块名的那一段，交上层继续判。
            m = re.search(r"\bfrom\s+([A-Za-z_][\w.]*)\s+import\b", code)
            return [m.group(1)] if m else []
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:  # 相对导入（level>0 且 module 为空）没有可判的模块名
                mods.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                mods.append(alias.name)
    return sorted(set(mods))


def check_ci_host_python_stdlib_only(
    ci_path: pathlib.Path | None = None, report: bool = True
) -> list[str]:
    """CI 里在 **runner 宿主机**上跑的 `python3 -c` 只许 import 标准库。

    为什么有这条（10-03 我自己写出来的红）：`R102-39` 把"别在 ci.yml 里抄第二份
    `DEFAULT_SILICONFLOW_BASE_URL`"改成"从 `config.py` 现读"，方向是对的，但落地写成了
    宿主机 `python3 -c "…from rolecard_agent.config import…"`，还在注释里断言"runner 的
    python3 只做标准库 import，装不装依赖无关" —— **那句是假的**：`config.py` 模块级
    `from pydantic import …`，宿主机上没有 pydantic ⇒ 镜像臂红在 `ModuleNotFoundError:
    No module named 'pydantic'`，而它前面四问全过，症状看着像"云端后端存不进去"。

    同族第三次（`R28-53` 镜像漏装依赖、`R28-61` 读不出就静默跳过），所以是尺子而不是第三句注释。
    判据的形状：**"唯一事实面"不许带"只有装了依赖的机器才成立"的前提** —— 要读项目的东西就在
    被测的那个容器里读（`docker exec`）。分母一并上屏（宿主 N 处 / 容器内 M 处），因为
    "一条都没扫到"与"扫到了都干净"长得一模一样（`R102` 轮那条分母为 0 的教训）。
    """
    problems: list[str] = []
    stdlib = set(sys.stdlib_module_names)
    ci = ci_path if ci_path is not None else ROOT / ".github" / "workflows" / "ci.yml"
    host_hits: list[tuple[int, str]] = []
    host_lines = 0
    container = 0
    if not ci.exists():
        problems.append("找不到 .github/workflows/ci.yml，这条没法判")
    else:
        for lineno, line in enumerate(
            ci.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if line.lstrip().startswith("#"):
                # 注释里可以**谈**这个写法（这条尺子自己的由来就写在 ci.yml 的注释里），
                # 它不是要执行的命令。第一版没跳过注释 ⇒ 把那句注释读成一次真 import，
                # 当场假阳 —— 判据读错方向比不读更坏。
                continue
            if "python -c" not in line and "python3 -c" not in line:
                continue
            if "docker exec" in line:
                container += 1
                continue
            host_lines += 1
            code = line.split("-c", 1)[1]
            for mod in _imported_modules(code):
                top = mod.split(".")[0]
                if top not in stdlib:
                    host_hits.append((lineno, top))
        if host_hits:
            problems.extend(
                f"ci.yml:{ln} 在宿主机上 import 了非标准库 `{mod}`" for ln, mod in host_hits
            )
        elif host_lines == 0 and container == 0:
            # 分母为 0：一个 `python -c` 都没扫到。这与"扫到了、都干净"长得一模一样，所以它自己出声
            # （`R102` 轮那条"恒绿尺子的分母是 0"的教训）。
            problems.append("ci.yml 里一个 python -c 都没扫到 ⇒ 这条没在量任何东西")

    if not report:
        return problems
    if problems:
        out("ci host python imports", False, "; ".join(problems)[:240])
        fails.append(f"ci.yml host-side python is not stdlib-only: {problems}")
    else:
        out(
            "ci host python imports",
            True,
            f"宿主机侧 {host_lines} 处引用全在标准库内；"
            f"容器内 {container} 处（那是被测环境，不计）",
        )
    return problems


# NOTE: check_python_pin was removed (2026-09-17) together with .python-version / uv.lock.
# The repo now manages dependencies with .venv + pip only; the Python floor lives solely in
# pyproject.toml's requires-python, and check_pyproject() already verifies it parses.
def check_version_parity() -> None:
    """整个仓库只有**一个**版本号，四处声明必须相等。

    从前这里比的是两对（`pyproject`↔`API_VERSION`、`shell`↔`frontend`），于是"0.3.0 与 0.1.0
    并存"看起来像设计。它确实是当时的设计（后端与壳各自发布），但**10-01 用户拍板合并**：
    "合并，归一化"。三条理由里最硬的一条是今天现学的 —— 那行明细印成
    `py=0.3.0 api=0.3.0 shell=0.1.0 frontend=0.1.0`，**同一轮取证里第三次被当成缺陷报上来**，
    一行需要读者先知道"这是两件事"才看得懂的输出，本身就是一处会反复产生误判的事实面。

    合并之后各处的含义：`pyproject` = `API_VERSION` = 两份 `package.json` 的 version，
    四处声明必须相等。
    安装包文件名与"下载桌面壳"那张卡读 `shell/package.json`，`/api/health` 与 OpenAPI 读
    `API_VERSION` —— 从这一版起这两个号**是同一个数**，所以"屏幕上的应用是哪一版"只有一个答案。
    代价如实记：以后只改壳（托盘、窗口行为）也要推后端那一格版本号，`/api/health` 里的号
    不再是"后端代码换没换"的信号 —— 那一问现在由 `build.sha` 回答（`R28-56` 那格构建指纹），
    恰好不需要版本线替它说话。
    """
    def grep_version(text: str, pattern: str) -> str | None:
        # re.M 是必需的：三条模式都锚在 `^` 上，没有 MULTILINE 时除了文件第一行什么都匹配不到
        # —— 写这条检查的人当场就被自己的正则骗过一次（pyproject 读成 None）。
        found = re.search(pattern, text, flags=re.M)
        return found.group(1) if found else None

    py = grep_version(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        r'^version = "([^"]+)"',
    )
    api = grep_version(
        (ROOT / "src/rolecard_agent/api/main.py").read_text(encoding="utf-8"),
        # 读的是 `API_VERSION` 那个常量（`R28-26`）：这里是 `version="x.y.z"`，
        # 恰好只盖住 FastAPI 那一处，而 `/api/health` 里还有一份手写副本在正则外面。
        # 两处现在合成一份，检查也跟着读那一份。
        r'^API_VERSION = "([^"]+)"',
    )
    shell = grep_version(
        (ROOT / "shell/package.json").read_text(encoding="utf-8"),
        r'"version":\s*"([^"]+)"',
    )
    front = grep_version(
        (ROOT / "frontend/package.json").read_text(encoding="utf-8"),
        r'"version":\s*"([^"]+)"',
    )
    bad: list[str] = []
    places = {
        "pyproject": py,
        "API_VERSION": api,
        "shell/package.json": shell,
        "frontend/package.json": front,
    }
    # 一格读不到也算不一致：从前 `grep_version` 返回 None 时两两比较会"两边都 None 所以相等"，
    # 于是把一个声明**删掉**能让这条绿 —— 结构与形状都变了却报"版本一致"。
    missing = sorted(name for name, value in places.items() if not value)
    if missing:
        bad.append(f"读不到版本声明：{missing}（读不到不算一致，None==None 会假绿）")
    distinct = sorted({value for value in places.values() if value})
    if len(distinct) > 1:
        detail_pairs = "、".join(f"{name}={value}" for name, value in places.items())
        bad.append(f"四处声明不是同一个号：{detail_pairs}（10-01 起合并成一条线）")

    # 安装脚本里**不许出现任何版本号字面量**（`R28-26` 的另一半）：它原来有两条硬编码的
    # `0.1.0`，其中"installer 进程退干净没有"那条匹配的是 `rolecard-agent-0.1.0*` ——
    # 升版本后它匹配不到任何东西，于是那个检查**永远通过**（而不是永远失败）。
    # 结构上盖不住的检查就要求文件里没有副本可漂，这比对齐两份更稳。
    ps1_raw = (ROOT / "scripts/install_package.ps1").read_text(encoding="utf-8", errors="ignore")
    # 只查**代码行**：PowerShell 的 `#` 注释里写"从前这里是 0.1.0"是这件东西存在的理由，
    # 不是可漂的副本。剥注释这件事本身就是这条检查的一半价值。
    ps1 = "\n".join(line for line in ps1_raw.splitlines() if not line.lstrip().startswith("#"))
    # `(?![\d.])` 是为了不把 `http://127.0.0.1:8000` 里的 "127.0.0" 当成版本号读出来 ——
    # 第一版就被它骗过一次（报的版本字面量里有 127.0.0）。
    literals = sorted({m.group(0) for m in re.finditer(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])", ps1)})
    if literals:
        bad.append(
            f"install_package.ps1 里出现了版本号字面量 {literals} —— 版本从 shell/package.json 现读"
        )

    # 明细只印**一个号**加"四处都读到了"这句话。从前它印四个数（`py=0.3.0 api=0.3.0
    # shell=0.1.0 frontend=0.1.0`），同一轮取证里被指着报过三次"版本在漂而门禁绿" ——
    # 一行需要读者先知道"这是两条线"才看得懂的输出，自己就是一处会反复产生误判的事实面。
    # 10-01 用户拍板合并成一条线，所以从这一版起它本来就该只印一个数。
    detail = (
        f"一个号 {distinct[0] if len(distinct) == 1 else '?'}，"
        f"四处声明（pyproject / API_VERSION / shell / frontend）全部读到且相等"
        "｜安装包名与下载卡读 shell/package.json，/api/health 读 API_VERSION —— 同一个数"
    )
    out("version parity", not bad, "; ".join(bad) if bad else detail)
    if bad:
        fails.append(f"version claims out of sync: {bad}")


def check_changelog() -> None:
    """当前版本必须在 `CHANGELOG.md` 里有一节 —— "tag 与 CHANGELOG 节共存"的本地代理。

    2026-10-04 快照「版本四处手抄、无 CHANGELOG」那一格的验收是"bump 0.4.0 后四处一致
    门禁绿；tag 与 CHANGELOG 节共存"。tag 那一半归发布链（本机既没有 tag 也没有可推的
    远端），能在门禁里判的是另一半：**改了号就得有那一节**。没有这条尺子，
    `bump_version.py` 生成的草稿只是"好心"，而好心拦不住"只改号、不写变更史" ——
    847 条提交零变更史就是这么长出来的。

    判据只问三件文件级事实（不猜语义、不检查文笔）：文件在、有 `## [Unreleased]`
    这个落点、有当前 `pyproject` 版本那一节。
    """
    path = ROOT / "CHANGELOG.md"
    problems: list[str] = []
    pyproject = ROOT / "pyproject.toml"
    version = None
    if pyproject.exists():
        # 走 tomllib 而不是 `re.search(r'^version = "…"')`（P3-9「tomllib 化」）：
        # 正则要求**行首零缩进**且恰好是双引号 —— 版本一旦缩进、换单引号、或别的表里
        # 先出现一个 `version = …`，它要么匹配错那一格，要么干脆匹配不到而**静默返回
        # None**（于是这一条"版本必须有 CHANGELOG 节"就变成了恒绿 —— 判据变摆设，
        # 与 `_imported_modules` AST 化那次是同一个坑）。按 TOML 结构读 `[project].version`
        # 才是在问"这一格的值"，而不是在文本里找一个长得像的行。
        try:
            parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            version = parsed.get("project", {}).get("version")
        except (tomllib.TOMLDecodeError, OSError):
            version = None
    if not path.exists():
        problems.append("CHANGELOG.md 不见了（版本改了却没有变更史）")
    else:
        text = path.read_text(encoding="utf-8")
        if not re.search(r"(?m)^## \[Unreleased\]", text):
            problems.append("`## [Unreleased]` 那一节不见了（新条目没有落点）")
        released = re.findall(r"(?m)^## \[([^\]]+)\]", text)
        if version and version not in released:
            problems.append(
                f"当前版本 {version} 在 CHANGELOG 里没有节（现有的：{released or '无'}）"
                " —— 跑 scripts/tools/bump_version.py 会连草稿一起生成"
            )
    ok = not problems
    out(
        "changelog",
        ok,
        "; ".join(problems[:3])
        if problems
        else f"当前版本 {version} 有节，`## [Unreleased]` 在（机械草稿 + 人工编辑）",
    )
    if not ok:
        fails.append(f"changelog missing: {problems}")


def check_coverage_threshold() -> None:
    """覆盖率阈值只有一处（pyproject 的 ``fail_under``），三份活文档抄它，读数不许低于它。

    覆盖率基线补完、阈值从 85 抬到 90 那一刀先盘点了这个数字住过几处家：配置、门禁步骤名、
    读数键、测试断言、散文 —— 抬一次要同步改七八处，漏一处就是静默漂。那一刀把身份与政策
    分了家（步骤名不再带数字：名字是身份，政策只归 ``fail_under``；断言由测试逼着改在配置
    那一处），剩三份散文由这条尺子管：

    ① README / 架构总览 / 开发流程里每个「阈值 N%」或「覆盖率≥N%」短语必须等于配置，
       **每个文件至少命中一处** —— 整个短语被删掉同样红：尺子失去主体，比数字错更难发现
       （形状上正是"缺一个键"与"这档本来不量"长得一样那一族）；
    ② 上一趟读数的 ``coverage_percent`` 不许低于阈值 —— "先落基线读数再定新阈值"这句
       规矩的机器化：线抬到读数之上，当场红，不必等下一次覆盖率实跑才发现判据立错了。

    两臂变异照红：只改散文的数字 → ①红；改配置到高于现读数的值 → ①②都红。
    读数文件不存在则②跳过（没跑过门禁不是缺陷，与"未知不拦"同一条纪律）。

    已知的假阳性面，记在这里而不是等人当缺陷报：README 若出现与覆盖率无关的「阈值 N%」
    （比如某个演示参数），这条也会要求它等于 ``fail_under`` —— 那时红是提醒来收窄这条
    规则，而不是悄悄放行。归档文档不扫：历史记录保持原样是原则。
    """
    problems: list[str] = []
    threshold: int | None = None
    pyproject = ROOT / "pyproject.toml"
    if pyproject.exists():
        cfg = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        threshold = cfg.get("tool", {}).get("coverage", {}).get("report", {}).get("fail_under")
    if threshold is None:
        # 配置里没有这一格是确认的负面：coverage.py 缺 fail_under 等于不设防，而命令行里
        # 那份副本早被"不许有第二份"的判据禁掉了 —— 所以这里必须响，不能当未知跳过。
        problems.append("pyproject 的 [tool.coverage.report] 没有 fail_under（阈值静默消失）")
    else:
        pattern = re.compile(r"阈值\s*(\d+)%|覆盖率≥\s*(\d+)%")
        for rel in ("README.md", "docs/架构总览.md", "docs/开发流程.md"):
            path = ROOT / rel
            if not path.exists():
                problems.append(f"{rel} 不见了（里面抄着覆盖率阈值）")
                continue
            hits = [
                g1 or g2
                for g1, g2 in pattern.findall(
                    path.read_text(encoding="utf-8", errors="ignore")
                )
            ]
            if not hits:
                problems.append(f"{rel} 里找不到阈值短语（尺子失去主体 —— 是短语变了形吗）")
            bad = sorted({h for h in hits if int(h) != int(threshold)})
            if bad:
                problems.append(
                    f"{rel} 写的阈值 {bad} 与 pyproject 的 fail_under {threshold} 不一致"
                )
        readings_path = ROOT / "docs" / "gate-readings.json"
        if readings_path.exists():
            try:
                readings = json.loads(readings_path.read_text(encoding="utf-8"))
            except ValueError:
                problems.append("gate-readings.json 读不出 JSON（产物坏了，另说）")
            else:
                cov = readings.get("coverage_percent")
                if cov is not None and float(cov) < float(threshold):
                    problems.append(
                        f"上一趟读数覆盖率 {cov}% 低于阈值 {threshold}%"
                        "（线抬到读数之上了 —— 先补用例把读数抬过线，或把线降回去）"
                    )
    ok = not problems
    out(
        "coverage threshold",
        ok,
        "; ".join(problems[:4])
        if problems
        else f"阈值 {threshold}% 单源于 pyproject，三份散文各至少一处且一致，读数高于它",
    )
    if not ok:
        fails.append(f"coverage threshold: {problems}")


#: 「这一轮在为谁」的**隐式读取点登记处**（2026-10-04 审查快照"身份显式化"那一格的尺子）。
#: 键 = `文件::所属函数`（**不写行号**：行号会随任何一次编辑漂，名字不会）。
#: 判据只认 `active_user_id(...)` 这一种调用形状 —— 它是那枚 ContextVar 唯一的读侧，
#: 而它的 `fallback` 实参就是"没绑过就悄悄用实例主人"这件事发生的地方。
#:
#: **从 5 条收到 3 条（快照验收"调用点 ≤3"达标）**，收掉的两条按"身份显式随 state 走"改：
#:   ① `features/proactive.py::chat_memory` → `memory_provider` 第三个参数显式收
#:      `state["user_id"]`（图从 state 现传，没传 = 直连门面/老线程，落实例主人 ——
#:      与从前回落语义逐字节相同）；
#:   ② `core/bootstrap.py::build_graph` 的 `settings_resolver` → 签名改
#:      `Callable[[str | None], Settings]`，owner 由 `_turn_backend` 从 state 取了现传。
#: 收拢时**尺子自己作证了它在工作**：代码改完、名单未改的那一刻跑一致性，stale 臂精确点名
#: 这两条（`build_graph` / `chat_memory`）而"未登记"臂是空的 —— 即搬动没有引入新的隐式读。
#:
#: 留下的 3 条都是**结构上挪不动**的（各写清为什么）：
#:   * `core/graph.py` 那两个 `bound_user(...)` 不算在内 —— 那是**绑**的一侧，不是读；
#:   * `api/main.py::_host_registry_factory`：域工具对模型必须看起来**零参数**（否则模型能
#:     自己填"我是谁"），所以工具的 `current_user` 只能是装配期定下的零参闭包，运行期现问；
#:   * `core/model_resolver.py::resolve_role_model`：凭据按本轮主人取（M2d），且它带
#:     `user_id` 显式入参给跨线程调用方 —— **只有没传且没绑**才走这里；
#:   * `core/memory.py::make_memory_tool.memory_save`：工具签名里刻意没有 user_id（同上那条
#:     零参纪律），调用方（tools 节点）手里有 state 却没法塞进工具入参 —— 这是①收拢后
#:     **唯一**留下的现问点，也是"为什么不直接改图入口就完事"的答案。
#:
#: 判据两臂都红：多一处即红（隐式读身份不许扩散），登记了而调用已不在也红（连"为什么"
#: 一并删，不许留死条目 —— 与 `api domain seams` 的"清单里没有死条目"同纪律）。
IDENTITY_IMPLICIT_READS: dict[str, str] = {
    "src/rolecard_agent/api/main.py::_host_registry_factory": "工具对模型零参",
    "src/rolecard_agent/core/model_resolver.py::resolve_role_model": "凭据按本轮主人取",
    "src/rolecard_agent/core/memory.py::make_memory_tool.memory_save": "工具签名里没有 user_id",
}


def _enclosing_func(tree: ast.Module) -> dict[int, str]:
    """每个节点的** enclosing 函数链**（`a.b` 形式，模块级记 `<module>`）。

    从节点往上走 parent 链，遇到函数就收名字，最后倒序拼接：嵌套函数（工具闭包、
    节点闭包）会带上外层，`make_memory_tool.memory_save` 这种就是靠它认出来的。
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    out_map: dict[int, str] = {}
    for node in ast.walk(tree):
        chain: list[str] = []
        cur: ast.AST = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
                chain.append(cur.name)
        out_map[id(node)] = ".".join(reversed(chain)) or "<module>"
    return out_map


def check_identity_implicit_reads_are_registered() -> None:
    """`active_user_id` 的每一处调用都得在登记名单里（身份不许继续隐式传播）。

    为什么立这把尺子：这一族缺陷的症状不是报错，是**悄悄换了一个人** —— 请求期漏了图入口
    的绑定，那条路径就读别人的记忆、花别人的 key，而它跑得很好。10-05 实测整套用例共
    触发回落 18 次，逐条归因后**没有一次是现行缺陷**（10 次是测试故意自证、6 次是后台
    调度器替实例主人冒话=设计如此、2 次是测试直接调工具函数不经图节点）；所以这一格的
    价值不在"修一个 bug"，在于**把扩散钉住**：以后谁再新加一处隐式读身份，必须来说清
    为什么这根管子只能这样接。

    判据用 AST 找**调用**（`ast.Call` 的函数名是 `active_user_id`），不是 grep 文本：
    本仓那几处 docstring/注释里提"active_user_id(实例主人)"是必要的说明，按文本数会把
    解释者数成越层者（`audit action vocabulary` 与 `api holds no sql` 都栽过一次，
    改法都是"读 AST 的常量/调用位置"）。
    两臂都要红：出现没登记的红；登记了而那处调用已经不在，也红。
    反向防空转：一条都没扫到 = 这个函数被改名或删了，尺子不该安静地绿。
    """
    src = ROOT / "src" / "rolecard_agent"
    found: dict[str, str] = {}
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        # 定义那一侧自己不算（`base/identity.py` 里的 def 与被包住的实现不是"读"）。
        if rel.endswith("base/identity.py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        funcs = _enclosing_func(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            else:
                name = ""
            if name != "active_user_id":
                continue
            found[f"{rel}::{funcs[id(node)]}"] = rel

    unregistered = sorted(k for k in found if k not in IDENTITY_IMPLICIT_READS)
    stale = sorted(k for k in IDENTITY_IMPLICIT_READS if k not in found)
    hollow = not found
    ok = not unregistered and not stale and not hollow
    if hollow:
        detail = "src 里一处 `active_user_id` 调用都没扫到（函数被改名/删除？判据在空转）"
    elif ok:
        detail = f"{len(found)} 处隐式读身份全在登记内"
    else:
        detail = f"未登记：{unregistered}；登记过时：{stale}"
    out("identity implicit reads registered", ok, detail)
    if unregistered:
        fails.append(
            "active_user_id() called outside the registered seams: "
            f"{unregistered}（登记处见 check_consistency.IDENTITY_IMPLICIT_READS；"
            "新加一处隐式读身份要先说清为什么不能显式传）"
        )
    if stale:
        fails.append(
            f"identity implicit-read registry is stale: {stale} —— 那几处调用已经不在，"
            "把登记与它的'为什么'一起删掉，别留死条目"
        )
    if hollow:
        fails.append(
            "identity-implicit-reads check is hollow: no active_user_id() call found in src"
        )


def check_session_thread_write_seam() -> None:
    """`session_thread` 的写 SQL 只许住在 storage 层（`R102-05` 第二步的尺子）。

    这条表从前有七个写入者：`api/routers/sessions.py`（5 处）、`core/sync.py`（2，该模块
    后迁 `features/`）、
    `core/reachout/inbox.py`（后迁 `features/`）、`core/memory_distill.py`、`roles/service.py`、
    `api/routers/sync.py` 各 1 —— 而它的每一条写都有道理（毫秒 `updated_at` 是为了侧栏同秒
    能分先后、`title` 的 `COALESCE` 是"只兜第一次"、`distilled_at_seq` 是游标不是计数）。
    道理散在七处，就等于哪一处都没有：`R102-62` 抄 7 遍的那条 SQL 是同一件事。

    判据只数**代码里的字符串常量**（与 `audit action vocabulary` 同一条纪律：注释里提到
    "从前有七处"不该把自己数成第八处）。允许的两份是 `storage/threads.py`（产品写链）与
    `storage/db.py`（形状迁移与身份重命名 —— 那是 schema 的事，不是会话的事）。
    反向还有一臂：repository 里必须**真的**躺着这些写点，不然"只许住在这里"会被掏空成
    一条永不说话的判据。
    """
    seam = {"src/rolecard_agent/storage/threads.py", "src/rolecard_agent/storage/db.py"}
    markers = ("INSERT INTO session_thread", "UPDATE session_thread", "DELETE FROM session_thread")
    offenders: list[str] = []
    in_seam = 0
    for path in sorted((ROOT / "src" / "rolecard_agent").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        found = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and any(marker in node.value for marker in markers)
        ]
        if not found:
            continue
        if rel in seam:
            in_seam += len(found)
        else:
            offenders.extend(f"{rel}:{line}" for line in found)

    hollow = in_seam < 5
    ok = not offenders and not hollow
    detail = (
        f"repository 里 {in_seam} 条写语句，登记接缝外 0 条"
        if ok
        else (
            f"越层写 session_thread：{offenders}"
            if offenders
            else f"接缝被掏空：repository 里只剩 {in_seam} 条写语句（<5）"
        )
    )
    out("session_thread write seam", ok, detail)
    if offenders:
        fails.append(
            "session_thread is written outside the storage repository: "
            f"{offenders}（写链唯一出处见 storage/threads.py）"
        )
    if hollow:
        fails.append(
            "session_thread write seam is hollow: storage/threads.py must hold the "
            "write statements it claims to own"
        )
