"""domain 主题这一族判据（P3-9 按主题细分，从 checks.py 平移，正文一字未改）。

按主题拆出；执行顺序由 registry.CHECKS 唯一决定，本模块只回答"这一族住哪"。
行为等价由门禁实跑全部 CHECKS 证明。
"""

from __future__ import annotations

import ast
import pathlib
import re

from .core import ROOT, fails, out, strip_comments


def _sql_table_names(path: pathlib.Path) -> set[str]:
    """一个 schema 文件里声明的表名（小写）。"""
    if not path.exists():
        return set()
    return {
        m.lower()
        for m in re.findall(
            r"CREATE TABLE IF NOT EXISTS (\w+)",
            path.read_text(encoding="utf-8", errors="ignore"),
            flags=re.I,
        )
    }


def _domain_private_tokens() -> set[str]:
    """各域**自己建**的表名 = 内核源码里不该出现的专有名词
    （架构总览 §5 不变式 1 / 架构审计报告 P1-1）。

    过去这条检查只盯字面量 "health"，于是 `core/ingestion.py` 里一句
    `UPDATE medical_report …` 大摇大摆躲过了检查 —— 一个只会绿的检查比没有检查更糟，
    因为它给的是假信心。现在**从各域的 schema 推导**：新增一个域、改一个表名，检查自动
    跟上，不需要有人记得来改这个脚本。

    扣掉内核自己也声明的表名：万一某个域的表恰好叫 `settings` / `sessions`，那本来就是
    内核词汇，报出来只会让人学会给检查加豁免 —— 那等于没有检查。
    """
    src = ROOT / "src" / "rolecard_agent"
    kernel = _sql_table_names(src / "core" / "schema.sql") | _sql_table_names(
        src / "roles" / "schema.sql"
    )
    private: set[str] = set()
    for schema in sorted((src / "domains").glob("*/schema.sql")):
        private |= _sql_table_names(schema) - kernel
    return private


def check_core_no_domain_token() -> None:
    """L1：core/ 内不得出现具体域的专有名词（域目录名 + 域自己声明的表名，不区分大小写）。

    分层硬规则：core 是内核，domains/<x>/ 才是业务域。内核源码里出现某个域的专名，
    说明有人把域概念抄近道塞进了内核（历史事故：AppContext.health 把具体域硬编码进
    内核，M9 才解耦）。**注释与 SQL 注释一并禁止** —— 注释里的域词是概念泄漏的早期信号，
    等它长成代码就晚了；这与 check_domain_isolation 先剥注释的取向相反，因为那条
    查的是"结构违规"（建表），本条查的是"概念泄漏"（连提都不该提）。
    内核自己的 schema 里那句"域引用本表、反向不行"因此也必须写成中性表述。
    """
    tokens = {"health"} | _domain_private_tokens()
    core_dir = ROOT / "src" / "rolecard_agent" / "core"
    bad: list[str] = []
    for path in sorted(core_dir.rglob("*")):
        if path.suffix not in (".py", ".sql") or not path.is_file():
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        ):
            lowered = line.lower()
            hit = [t for t in tokens if t in lowered]
            if hit:
                bad.append(f"{path.relative_to(ROOT)}:{lineno} {hit}")
    detail = f"{len(tokens)} tokens clean" if not bad else f"found: {bad[:8]}"
    out("core/ no domain token", not bad, detail)
    if bad:
        fails.append(f"core/ mentions domain tokens: {bad}")


#: `domains` 包里**通用**的模块（域契约与注册表）。api 层 import 它们不算"耦合具体域" ——
#: 恰恰相反，它们就是"中心代码不点名任何域"的那一层（2026-10-04 域机制收口：`api/main.py`
#: 从此只读各域的 `SPEC`，那半边接缝随之消失）。
GENERIC_DOMAIN_MODULES = frozenset(
    {
        "rolecard_agent.domains.registry",
        "rolecard_agent.domains.spec",
    }
)

#: api 层允许 import 具体域的**登记接缝**（`R102-10`：集单调最严——除登记处外即红）。
#: 每条的"为什么"就写在这里；登记过时（那个文件不再 import 具体域了）也红 ——
#: 与 `route access` 的"清单里没有死条目"同一条纪律。
#:
#: **现在是空的，这正是目标态**（2026-10-04 域机制收口，快照 P1-5）：域专属路由（记录补录 /
#: 抽取 / 修正 / 删除那一族）搬回域内，由 `DomainSpec.router_contrib` 交回宿主挂载，api 层
#: 连最后一条具体域 import 也归零。空名单**不是这条尺子退休**：谁再往 api 里 import 一个
#: 具体域，照样红，那时必须来这里登记并写清"这道接缝为什么是刻意的"。
API_DOMAIN_SEAMS: dict[str, str] = {}


def check_api_domain_seams() -> None:
    """api 层 import 具体域只许发生在登记接缝上（`R102-10` 那把迟到的尺子）。

    `deps.py` 从前自述"api 层不 import 具体域"，而 `main.py` 与 `records.py` 就在
    import —— 分叉处正好在尺子的覆盖面外（`core no domain token` 只管 core）。修法不是
    把那两句改没，而是立这把尺子把接缝摆到台面上：新增一个 import 具体域的 api 文件 =
    红，必须登记并写理由。

    收口过程（快照 P1-5，两步走）：① 域接线改读各域 `SPEC`（`main.py` 除名）；② 域专属
    路由改由 `router_contrib` 交回宿主挂载（`routers/records.py` 搬进域内，名单就此**清空**）。
    `api/main.py` 与其余 api 文件 import 的 `domains.registry` / `domains.spec` 属于
    **通用**模块，不计入（见 `GENERIC_DOMAIN_MODULES` —— 把它们算成"具体域"会让这把尺子
    天天喊狼来了，喊多了就没人看）。
    """
    api_dir = ROOT / "src" / "rolecard_agent" / "api"
    hits: dict[str, set[str]] = {}
    for path in sorted(api_dir.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
                and node.module.startswith("rolecard_agent.domains.")
                and node.module not in GENERIC_DOMAIN_MODULES
            ):
                hits.setdefault(rel, set()).add(node.module)
    unregistered = sorted(rel for rel in hits if rel not in API_DOMAIN_SEAMS)
    stale = sorted(rel for rel in API_DOMAIN_SEAMS if rel not in hits)
    ok = not unregistered and not stale
    if not ok:
        detail = f"未登记：{unregistered}；登记过时（已不再 import 具体域）：{stale}"
    elif not hits:
        detail = "api 层零处 import 具体域（目标态；登记名单为空）"
    else:
        detail = f"{len(hits)} 处接缝全在登记内（{', '.join(sorted(hits))}）"
    out("api domain seams", ok, detail)
    if unregistered:
        fails.append(
            "api imports concrete domains outside registered seams: "
            f"{unregistered}（登记处见 check_consistency.API_DOMAIN_SEAMS）"
        )
    if stale:
        fails.append(f"api domain seams registry is stale: {stale}")


def check_domain_isolation() -> None:
    """users / tenants are kernel concepts - a domain plugin must not define them.

    Comments are stripped first: a note saying "there is no user table here" is not a
    violation, and treating it as one made this check fire on its own documentation.
    """
    text = strip_comments(
        "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in (ROOT / "src" / "rolecard_agent" / "domains").rglob("*")
            if p.is_file()
        )
    )
    bad = [
        kw
        for kw in (
            "CREATE TABLE IF NOT EXISTS app_user",
            "CREATE TABLE IF NOT EXISTS tenant",
            "user_base",
            "UserBase",
        )
        if kw in text
    ]
    out("domain isolation", not bad, str(bad) if bad else "no identity concepts inside domains/")
    if bad:
        fails.append(f"domain layer references kernel identity concepts: {bad}")
