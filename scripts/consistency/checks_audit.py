"""audit 主题这一族判据（P3-9 按主题细分，从 checks.py 平移，正文一字未改）。

按主题拆出；执行顺序由 registry.CHECKS 唯一决定，本模块只回答"这一族住哪"。
行为等价由门禁实跑全部 CHECKS 证明。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import re
import subprocess

from .core import ROOT, fails, iter_files, out, warns


def check_audit_action_vocabulary() -> None:
    """审计的两侧都要有尺子（2026-10-02 轮 `R102-07` + `R102-14`）。

    **第一侧：一条 INSERT 只许有一份。** 审计从前有五个写入点 —— `roles/service.py`
    （api 侧 55 处全借它）、`core/plugins/`、`core/tools/{files,mcp,run}.py` —— 五份
    逐字相同的 `INSERT INTO audit_log`。改一处口径而另外四处不动，正是本仓那一族事故的
    形状（`detail` 的编码从前真的不一致：只有 `mcp` 那份额外用 `default=str`）。
    现在语句只住在 `base/audit.py`，出现次数必须 = 1。

    计数只数**代码里的字符串常量**，不数注释与 docstring 里的引文：这条判据本尊踩过这个
    坑 —— 它的 docstring 要解释"从前有五份"，按整文件文本计数时它把自己数成了第二份
    （`R102-37` 的同族：尺子把分母数错）。

    **第二侧：动作词表是一份清单，不是一个传说。** `R102-14` 记的是"56 处调用、55 个不同
    动作名，没有任何一处按名字读它们"，于是改一个名就是把同一动作劈成两条历史。清单在
    `base/audit.AUDIT_ACTIONS`；带变量的那一族（`mcp:{server}__{tool}`）按
    `DYNAMIC_ACTION_PREFIXES` 的前缀放行。差分**两个方向都要能红**（`R102-37` 的教训：
    只写一臂的判据是空转臂）—— 用了没登记的红，登记了没人用也红。
    """
    src = ROOT / "src" / "rolecard_agent"

    def stmt_copies() -> list[str]:
        copies: list[str] = []
        for path in sorted(src.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            n = sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith("INSERT INTO audit_log")
            )
            if n:
                copies.append(f"{path.relative_to(ROOT).as_posix()}×{n}")
        return copies

    copies = stmt_copies()
    one_throat = copies == ["src/rolecard_agent/base/audit.py×1"]

    try:
        from rolecard_agent.base.audit import (
            AUDIT_ACTIONS,
            DYNAMIC_ACTION_PREFIXES,
        )
    except Exception as exc:  # noqa: BLE001 - 清单读不到就是红，不许静默跳过
        out("audit action vocabulary", False, f"AUDIT_ACTIONS 不可导入：{exc}")
        fails.append(f"audit vocabulary registry unreadable: {exc}")
        return

    literal: set[str] = set()
    dynamic: set[str] = set()

    def take(node: ast.expr) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal.add(node.value)
        elif isinstance(node, ast.IfExp):
            take(node.body)
            take(node.orelse)
        elif isinstance(node, ast.JoinedStr) and node.values:
            head = node.values[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                dynamic.add(head.value)

    def harvest(node: ast.Call, name: str) -> None:
        """取这一发调用带的动作名。

        规则写死在这里：带 `action=` 关键字的按关键字取；`_audit(conn, "action", …)`
        那种位置写法取第 2 个实参，**但第 1 个实参是字符串常量时跳过** —— 那是 `mcp`
        那份额外的 `_audit(phase, detail)`，phase 不是动作名。
        """
        kw = next((k for k in node.keywords if k.arg == "action"), None)
        if kw is not None:
            take(kw.value)
        elif name in {"_audit", "tool_audit"} and len(node.args) >= 2:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return
            take(node.args[1])

    for path in src.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = (
                callee.attr
                if isinstance(callee, ast.Attribute)
                else (callee.id if isinstance(callee, ast.Name) else "")
            )
            if name in {"audit", "_audit", "log", "tool_audit"}:
                harvest(node, name)

    unregistered = sorted(literal - set(AUDIT_ACTIONS))
    stale = sorted(set(AUDIT_ACTIONS) - literal)
    bad_prefixes = sorted(dynamic - set(DYNAMIC_ACTION_PREFIXES))
    ok = one_throat and not unregistered and not stale and not bad_prefixes
    detail = (
        f"{len(literal)} 个动作用词全在 {len(AUDIT_ACTIONS)} 项清单内；"
        f"动态族 {sorted(dynamic) or '无'}；INSERT 一份"
        if ok
        else f"INSERT 份数={copies}；未登记动作={unregistered}；"
        f"清单里没人用的={stale}；未登记动态前缀={bad_prefixes}"
    )
    out("audit action vocabulary", ok, detail)
    if not one_throat:
        fails.append(f"audit_log INSERT must live in exactly one place: {copies}")
    if unregistered:
        fails.append(f"audit actions not in AUDIT_ACTIONS: {unregistered}")
    if stale:
        fails.append(f"AUDIT_ACTIONS entries with no call site (rename forks history): {stale}")
    if bad_prefixes:
        fails.append(
            f"dynamic audit prefixes not declared in DYNAMIC_ACTION_PREFIXES: {bad_prefixes}"
        )


#: 22 份归档散件合并后的汇编（2026-10-09 用户拍板：散件删除、内容逐字保留 + 来源头）。
_MERGED_ARCHIVE = ROOT / "docs" / "archive" / "审计台账汇编.md"


def _audit_ledger_path() -> pathlib.Path:
    """**最新一轮**审计台账住在哪儿：先扫 `docs/`（在写的），再扫 `docs/archive/`（已封存的）。

    为什么不是写死一个文件名：归档不该让尺子瞎掉 —— 本仓第九条踩过的那一发就是
    "台账全搬进 `docs/archive/` 之后，那把管台账形状的尺子再也没看见它"。
    按文件名里那个日期取最新的一轮，两个目录一起比；找不到就返回一个不存在的路径，
    让调用方按"台账不在"出声，而不是悄悄一步不进。
    """
    cands = [
        *sorted((ROOT / "docs").glob("架构审计（*轮）.md")),
        # 2026-10-09：22 份归档散件按用户拍板合并为一份汇编（散件删除、内容逐字保留 +
        # 来源头）。台账定位随之指向汇编 —— 编号（R26/R28/R102…）全部仍在盘上，
        # `audit citations` 的可达性不受影响。
        _MERGED_ARCHIVE,
    ]
    if not cands:
        return ROOT / "docs" / "架构审计（*轮）.md"  # 不存在 ⇒ 调用方判红

    def _date(p: pathlib.Path) -> tuple[int, int, int]:
        m = re.search(r"（(\d{4})-(\d{2})-(\d{2}) 轮）", p.name)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)

    return max(cands, key=_date)


def _ledger_h2_span(text: str, ruler: str) -> tuple[int, int] | None:
    """定位台账里 H2 那张表；结构不对就**干净判红**，不许抛 traceback（M26a 实测撞过）。

    会走到这里的最真实形状：`_audit_ledger_path()` 按日期取"最新一份"，而那份其实是上一代
    `P{n}-{m}` 结构的归档件（它没有 `## H2`）—— 这等于说当前轮的台账丢了或改了名，
    正该出声，而不是让整趟一致性崩在半路（崩掉的门禁只留下一串 traceback，谁也读不出少了什么）。
    """
    try:
        start = text.index("## H2 ")
        return start, text.index("## H3 ", start)
    except ValueError:
        out(
            ruler,
            False,
            "最新一份台账里没有 `## H2 … ## H3` 这张表 —— 解析器大概退回到了上一代的归档件，"
            "也就是当前这一轮的台账丢了或改了名",
        )
        fails.append(f"{ruler}: newest audit ledger has no H2..H3 table (不是本轮的 H* 结构)")
        return None


def check_audit_ledger_row_count() -> None:
    """**已退役（2026-11-01）**：这把尺子的对象没了。

    它判的是"台账 H2 标题里那个「（N 条）」必须由脚本现数"—— 也就是说，它量的是
    **一份历史台账对自己是否自洽**。历史台账（`docs/archive/审计台账汇编.md`）已按用户
    拍板删除（"以前归档的也准备删了"），这把尺子的**被审对象不存在了**，于是它从
    "一把尺子"退化成"一份对不存在文件的期待"—— 那正是它每次都报 `audit ledger missing`
    的原因。

    **退役不是"删掉不管"**：真正的防漂移能力已经搬到别处 ——
      * 编号的可达性 ⇒ `audit citations` + `audit index in sync`（两把都还在跑，
        判据是"代码里引的号在活文档里找得到"）；
      * "写死的数就是会漂的真相" ⇒ `readme headline numbers` 那把（README 的数由脚本现数）。
    这两把覆盖了它想防的那一类缺陷，且**判据的对象是活文档**（一直在写、一直在对账），
    比对着一份封存件自洽有意义得多。

    留一条空实现（而不是从 registry 里删掉函数）：让"这把尺子为什么退役"留在代码里，
    免得下一个人看到"编号会漂却没人管"又把同一把尺子重建一遍。
    """
    out(
        "audit ledger row count",
        True,
        "已退役（2026-11-01）：台账已删，防漂移改由 audit citations + readme headline numbers 承担",
    )


#: H2「状态」格里算"表过态"的字样（`R102-77` 的尺子在用）。
_LEDGER_STATUS_MARKERS = (
    "已收口",
    "未收口",
    "不修",
    "恒",
    "按设计",
    "取舍",
    "无需",
    "不动",
    "不改",
    "已处置",
    "已拍",
)


def _ledger_closure_traced(rid: str, h12: str, detail_rows: str) -> bool:
    """在 H12 的执行记录里找这一条的出处，**认简写**：「`R102-48`+`26`」「老 `31`」都算。

    也算详表那一格的出处：`R102-24`/`25`/`31` 的收口写在 H8 的开轮批详表行里（那里才是
    逐条证据的存放地），逼 H12 再抄一句只会造出第二处会漂的真相。
    方向按本仓"分不清就不拦"：裸数字可能把不相干的串认成出处（宽松一侧），
    而把确实收口的行冤枉成"查无出处"只会让下一批人不去补记 —— 尺子不是鞭子。
    """
    if rid in h12:
        return True
    nn = rid.split("-")[1]
    if re.search(rf"[+`]0*{nn}\b", h12) is not None:
        return True
    for line in detail_rows.splitlines():
        if line.startswith(f"| {rid} |") and "收口" in line:
            return True
    return False


def check_ledger_status_states_verdict() -> None:
    """**已退役（2026-11-01）**：这把尺子的对象没了。

    它判的是"台账 H2 每行的「状态」格必须表态；说「已收口」的行必须在 H12 里找得出处"
    —— 也就是**一份历史台账对自己是否自洽**。历史台账已按用户拍板删除，这把尺子的被审
    对象不存在了，于是它退化成"一份对不存在文件的期待"。

    **它想防的那一类缺陷没消失，判据换了地方**：沉默的"状态格"= 文档在一处说做了、
    另一处说没做 —— 现在由 `audit citations`（代码引的号在活文档里找得到）与
    `readme headline numbers`（写死的数由脚本现数）承担，两者判的都是**活文档**，
    一直在写、一直在对账。

    留一条空实现（而不是从 registry 里删掉函数）：让"这把尺子为什么退役"留在代码里，
    免得下一个人看到"编号的收口没出处却没人管"又把同一把尺子重建一遍。
    """
    out(
        "ledger status states a verdict",
        True,
        "已退役（2026-11-01）：台账已删，防「沉默收口」改由 "
        "audit citations + readme headline numbers 承担",
    )


def _git_ignored(refs: list[str]) -> tuple[set[str], bool]:
    """把"文档点名的路径"一次性问 git：哪些是**被忽略的**。返回（被忽略集合, 是否问成功）。

    为什么必须有这一步：`build/` 整个目录在 `.gitignore` 里，那里的一切都是"某台机器上
    某一刻跑出来的"。把它们和 `src/` 里的引用混在同一把尺子下，判据就从"这个引用是不是谎"
    悄悄变成"这台机器上有没有那个文件" —— 09-30 实测：`build/` 进受验前缀的第一趟**本机全绿**，
    推到 CI 红 10 处（`build/baseline.json` 这类"生成命令就写在同一行旁边"的暂存件，
    runner 上当然没有）。**一条检查如果在 CI 上与在本机结论不同，它的判据里就有 gitignore 的东西**，
    这一条现在是结构而不是靠人记。

    问不到（没有 git / 超时）时返回空集合：宁可让那 10 处照样红，也不"读不到就当没有引用"。
    """
    paths = sorted({str(r) for r in refs})
    if not paths:
        return set(), True
    try:
        proc = subprocess.run(
            # `-c core.quotepath=false`：**这条分区的立身之本是「送进去什么、回出来什么必须逐字
            # 相等」**，而 Linux 上 `core.quotepath` 默认 true 时 git 回的是带双引号的八进制转义名
            # （2026-10-09 用真仓库两种配置各跑一遍实测：true 下回 `"build/\344…"`，false 下回原名）
            # ⇒ 集合里的每个成员都匹配不上任何真引用 ⇒ 分区**静默退化成"一个都认不出"**，
            # 本机 Git-for-Windows 默认 false ⇒ 这台机器永远是绿的。与 ENGI-31 那族同根因，
            # 这是它的第 5 处（也是这处函数**第二次**栽在"回信对不上送信"上 —— 第一次是
            # `text=True` 把 \n 翻成 \r\n，见下一条注释）。
            ["git", "-c", "core.quotepath=false", "check-ignore", "--stdin"],
            # **必须送 bytes**：text 模式会把 "\n" 翻成 "\r\n"，而 `--stdin` 只剥换行不剥
            # 回车 —— git 于是收到一条带控制符的"路径"，回你一条加引号的转义名（`"a.json\r"`），
            # 匹配不上任何真引用，这条分区就静默失效（09-30 在干净 worktree 里实测到）。
            input="\n".join(paths).encode("utf-8"),
            capture_output=True,
            cwd=ROOT,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return set(), False
    if proc.returncode not in (0, 1):  # 1 = 一条都没被忽略，不是失败
        return set(), False
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    return {line.strip().replace("\\", "/") for line in lines if line.strip()}, True


# User stories that are explicitly NOT covered by automated tests yet. They are deferred to a
# later milestone, not forgotten - the traceability check still requires them to be *named* here
# so the gap stays visible (技术评审与决策.md 「实现对齐核查」D3).
# US-9 (console frontend) was covered once M5 landed -> removed from this set (2026-09-15).
# US-6 (quick reproduce: 3 commands / lock file / demo video) still has no *test*; the non-video
# parts are guarded by readme_quickstart / dependency_parity / python_pin, and the 60s demo
# video was dropped by decision (2026-09-15).
DEFERRED_US = {"US-6"}

#: 用户看得见的字面里不许再出现的词（`R28-45` 的事后闸）。判据分两半，各用**各自的精确信号**：
#:  * 前端扫 `frontend/dist` 的产物 —— 压缩后的 JS 里没有注释，出现在那里的字必然是用户看得见的
#:    （与 `bundled copy` 同一招，不需要一台 JSX 解析器去猜"这行是不是注释"）；
#:  * 后端扫 `src/**/*.py` 的**字符串常量**，docstring 与注释不算（那些是写给开发者看的，
#:    「后端」在那儿仍然精确指 `ModelBackend` 那一行）。
_BANNED_USER_VISIBLE = (
    "模型后端",  # L2 那一行在界面上叫「模型」
    "默认后端",  # 「默认」的家是「服务」页那条优先级，不是模型页
    "后端名",  # 字段名叫「模型名」
    "后端序列",  # 那条叫「对话优先级」
    "后端列表",
    "服务地址",  # L4 那个进程叫「本机程序」，它的地址是「程序地址」
    "本机后端",
    "本地推理服务",
    "Ollama 服务",
    "本地服务",  # 「本机程序」的落选备选（`R102-20`：同一旗标一张屏两个名字，
    # 术语词表提案 §6.2 定名「本机程序」—— 从前它不在词表里，尺子照绿）
)


def _required_user_stories() -> set[str]:
    """The authoritative US list is the `### US-N` headings in 需求与验收标准.md.

    Parsing it from the doc - rather than hard-coding - means a renamed or added story fails
    the check instead of silently drifting from what the tests claim to cover.
    """
    doc = (ROOT / "docs" / "需求与验收标准.md").read_text(encoding="utf-8")
    return set(re.findall(r"^###\s+(US-\d+)", doc, flags=re.M))


def _covered_user_stories() -> set[str]:
    """Every `US-N` token in the test tree counts as covered (unit / integration / eval)."""
    found: set[str] = set()
    for path in iter_files(".py", ".json"):
        if "tests" not in path.parts:
            continue
        found |= set(re.findall(r"US-\d+", path.read_text(encoding="utf-8", errors="ignore")))
    return found


def check_us_traceability() -> None:
    """Every shipped user story must be traceable to at least one test (技术评审与决策.md
    「实现对齐核查」D3).

    Before this check the chain was maintained by memory: only US-4 was named in a test, and a
    story could lose its only coverage with no signal. Now (a) a test citing a non-existent US
    fails, and (b) any non-deferred US with zero references fails.
    """
    required = _required_user_stories()
    covered = _covered_user_stories()

    stale = sorted(covered - required)
    if stale:
        fails.append(f"tests cite non-existent user stories: {stale}")
    must_cover = required - DEFERRED_US
    missing = sorted(must_cover - covered)
    if missing:
        fails.append(f"user stories with no test traceability: {missing}")

    ok = not stale and not missing
    detail = (
        f"required={len(required)} covered={len(required & covered)} "
        f"deferred={sorted(DEFERRED_US & required)}"
    )
    out("us traceability", ok, detail)


def report_line_budget() -> None:
    def count(suffix: str) -> int:
        return sum(
            len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
            for p in iter_files(suffix)
        )

    md, py = count(".md"), count(".py")
    print(f"\nmd={md} lines | py={py} lines | ratio={md / max(py, 1):.1f}:1")
    if md > py * 5:
        print("note: docs still outweigh code - expected during planning, watch it after M1")


#: 行里出现这个词就**优先**当它指这份文档（越具体越靠前）。匹配不到词时不做假设：
#: 直接去所有文档里找这个编号，找到谁就算谁。
#: 行内点了某份文档时，认的是**文件名**而不是完整路径（10-01 归档之后路径都变了，
#: 而"这句引用指的是哪份文档"问的是文档身份，不是存放地）。
#: 2026-11-01 改（历史台账与快照删除后）："审计"这把钥匙原先指向 `docs/archive/` 三份
#: （历史台账 + 两份旧快照）—— 那些文件删了之后，钥匙必须指向**还在盘上的**活文档。
#: 现在指向 `docs/架构总览.md`（§5 关键架构决策与不变式 = 历轮审计沉淀下来的**判据**，
#: 那里的 `§x.y` 号正是代码注释里引得最多的那族）与 `docs/需求与验收标准.md`。
#: 删掉历史台账不删**判据**：判据活在活文档里，历史台账只是过程。
_CITATION_DOC_KEYS: tuple[tuple[str, frozenset[str]], ...] = (
    ("设计稿", frozenset({"主动消息与记忆设计稿.md", "更新机制与OCR随包.md"})),
    ("总览", frozenset({"架构总览.md"})),
    ("需求", frozenset({"需求与验收标准.md"})),
    ("审计", frozenset({"架构总览.md", "需求与验收标准.md"})),
)
_SECTION_RE = re.compile(r"§\s*(\d+(?:\.\d+)*)")

# `[RP]\d+-\d+`（`R102-33`）：R 号命名空间（R26/R28/R102…）从来都在被引用，而旧正则只认
# P 号 —— 95 个文件 290 处引用处于盲区，"有这条检查看着"是假的。宽判据实验（内存里做）：
# R 号进来后被引用编号 92 个、悬空 2 个（见 _CITATION_PROSE_ONLY），洪峰可控。
# `ENGI-\d+`（2026-10-10）：审查快照（2026-10-04）的 ENGI 家族同理 —— 74 处代码引用完全
# 不受管（E 不在旧的 `[RP]` 字符类里），而快照第五节的 ENGI 行真实存在，编号有家可归。
_PID_RE = re.compile(r"\b(R\d+-\d+|P\d+-\d+|ENGI-\d+)\b")

#: 文档里可以当被引用目标的两种形状：标题编号（`### 12.19 …`）与台账行号（`| 12.4 |`）。
#: 只活在**散文**里、从来没有台账行锚点的历史编号（同一次宽判据实验量得的全部悬空）。
#: R28-44/45 描述的事件已并入「处置记录」的教训正文 —— 它们不是断链，是"正文吸收了条目"。
#: 这份名单是**豁免登记处**：新的悬空出现时先查是不是同类，是就登记理由，不是就修引用。
#: R28-45 在 2026-10-10 尺子认了快照行形后有了真家，从这份名单退役；R28-44 仍只在散文里。
#: ENGI-4/ENGI-10 是尺子自述与判据用例里举的**形状示例**（`| P2-16 (PERF-11+ENGI-4) |`），
#: 不是真引用 —— 与 ENGI-30 那条"注释里举的例子不能当引用"同一件事的登记处一侧。
_TARGET_HEAD_RE = re.compile(r"^#{2,5}\s+(\d+(?:\.\d+)*)\b")
# 表格行里"这一格就是这个号的定义"的形状。**从前要求编号独占首格**
# （`^\|\s*号\s*\|`），而账本大量合法写法是 `| P2-16 (PERF-11+ENGI-4) |`（合并说明写在同一格）
# 与 `| **P3-11** (ENGI-10) |`（粗体）—— 2026-10-09 实测：46 个号里 **39 个因此从来没进过归属表**，
# 谁在代码里引用它们就会被 `audit citations` 判成悬空（我自己就是撞上的第一个：往测试 docstring
# 写了个例「见 P2-16」，尺子当场报 1 dangling —— **它报得对，是我打算改错方向**：第一反应是
# 把引用删掉，那是把尺子的缺陷当自己的错修掉）。
# 现在认：可选粗体 + 编号 + 任意串括号说明 + 到格尾。捕获组只取**行首那一个**号，所以
# `| P2-1 (ARCH-5+7→见 P3-9) |` 登记的是 P2-1，合并说明里的 P3-9 不会被当成这行的定义 ——
# 放宽的方向只会**减少**悬空，不会把"只是提到"变成"住在这儿"。
# 2026-10-10 再补两格：① 编号家族加 `ENGI-\d+`（审查快照第五节的 ENGI 行，与 _PID_RE 同一批）；②
# 括号说明认**全角** `（…）`（快照的 ENGI 行全是 `| ENGI-20（2026-10-08 CI 量出…） |` 形状，
# 旧字符类只认半角 `(...)`，23 个 ENGI 号因此全部无家）。
_TARGET_ROW_RE = re.compile(
    r"^\|\s*\**([RP]\d+-\d+|ENGI-\d+|\d+\.\d+)\**\s*(?:[（(][^|）)]*[)）]\s*)*\|"
)

_CITATION_PROSE_ONLY = frozenset({"R28-44", "ENGI-4", "ENGI-10"})

#: **台账删除那一族的整体豁免**（2026-11-01）。
#:
#: 背景：11-01 删掉了历史审计台账与快照（用户拍板"判据留在活文档，过程不留档"）。台账里
#: 的**编号定义行**随之消失，而代码注释里那 1119 处 `R26-45` / `P1-5` / `ENGI-36` 式的
#: 引用还在 —— 它们指向的"那一节"已经不存在了。这不是有人写错引用，是**标的物被删了**。
#:
#: 为什么整族豁免而不是逐号登记：207 个号、1119 处引用，逐个改注释是把一次文档清理
#: 变成一次全仓代码改写（而且改错就改了判据）。仓库既有先例是 `R28-44`（"正文吸收了条目
#: —— 它们不是断链"），本族是它的放大版：**正文吸收或彻底消失，但引用还在**。
#:
#: 这条豁免**只豁免"号无家"这一半**：`audit citations` 仍会查"归因可疑"那半（点了某份
#: 文档、号却不在它里面），新出现的悬空（不在本族里的号）照红。
#:
#: 解锁条件：把代码注释里的裸编号改写成"那一号说的那件事"（仓库纪律，11-01 删档那条
#: 消息里定的方向）。改一处，从豁免名单里去掉一处；改完了这条豁免就整个退休。
#: 名单在 `config/citation-prose-only.json`（生成物：查当前还悬空的号，按号列出）。
_CITATION_DELETED_LEDGER = frozenset(
    json.loads(
        (ROOT / "config" / "citation-prose-only.json").read_text(encoding="utf-8")
        if (ROOT / "config" / "citation-prose-only.json").exists()
        else '{"numbers": []}'
    )["numbers"]
)


def _audit_index():
    """索引生成器（同一份扫描口径的唯一出处）。"""
    script = ROOT / "scripts" / "tools" / "build_audit_index.py"
    spec = importlib.util.spec_from_file_location("audit_index", str(script))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_citation_reachability() -> None:
    """代码/测试/壳里的每一处 `§x.y` 与 `P{n}-{m}` 引用都必须真的指得到东西。

    为什么单独立这条（2026-09-25，审计「「本地 / 云端」同时指两件事」）：`check_doc_links` 只查
    markdown 反引号里的
    **路径**，查不到 .py docstring 里"（架构审计报告 P1-5）"这种**散文引用** —— 而这类引用
    实测 29 个文件、444 处。后果不是难看，是**文档一动就静默断链**，而断掉的正好是
    "这个决策为什么长这样"的唯一线索（今天的误判就被一句过期的"P0-3 尚未闭环"带偏过一次）。

    判据在 10-01 改过一次，因为三份审计台账全部搬进了 `docs/archive/`。改之前搬一档的代价是
    `audit citations` 从 27 条黄跳到 124 条黄（09-25 实测），于是那档一直原地不动 ——
    根因是**判据把"存放地"当成了"身份"**：编号本身是稳定的，住哪个文件、归不归档是存放细节。
    现在的两档：
      * **红**：这个编号在任何一份文档里都不存在（写错了，或者那一节被删了）。
      * **黄**：行内点了某份文档（"见架构审计报告 P1-5"），而那个编号**不在它那里** —— 归因可疑。
        认的是**文件名**不是完整路径，所以搬档不会把这条变成噪音。
    "住在归档里"不再单独报警，只作为元信息上屏（多少条引用落在归档件里），
    并且由 `audit index in sync` 那条保证每条号都在索引里有地址。
    """
    index = _audit_index()
    _numbers, homes, _per_doc = index.scanned_citations()

    def _home(num: str) -> list[str]:
        return sorted(homes.get(num, set()))

    dangling: list[str] = []
    doubtful: list[str] = []
    in_archive = 0
    total = 0
    for path in iter_files(".py", ".ts", ".tsx", ".js"):
        rel_file = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in _SECTION_RE.finditer(line):
                total += 1
                num = match.group(1)
                found = _home(num)
                if not found:
                    if num not in _CITATION_DELETED_LEDGER:
                        # 台账删除那一族豁免（见该常量的理由与解锁条件）；
                        # § 形状的号不走 PID 名单，所以单独判一次。
                        dangling.append(f"{rel_file}:{lineno} §{num}")
                    continue
                if any("/archive/" in f"/{item}" for item in found):
                    in_archive += 1
                named = next(
                    (words for word, words in _CITATION_DOC_KEYS if word in line),
                    None,
                )
                if named and not any(item.rsplit("/", 1)[-1] in named for item in found):
                    doubtful.append(f"{rel_file}:{lineno} §{num} 点了那份文档，号却不在它里面")
            for num in _PID_RE.findall(line):
                total += 1
                if num in _CITATION_PROSE_ONLY:
                    continue  # 豁免登记处：只活在散文里的历史编号（见该常量的理由）
                if num in _CITATION_DELETED_LEDGER:
                    # 台账删除那一族：标的物没了，引用还在（见该常量的理由与解锁条件）。
                    # 只豁免"号无家"这一半 —— "归因可疑"那半在上面照查。
                    continue
                if not _home(num):
                    dangling.append(f"{rel_file}:{lineno} {num}")

    detail = (
        f"{total} citations; {len(dangling)} dangling, {len(doubtful)} 归因可疑"
        f"（{in_archive} 处落在归档件里，按设计：编号是身份、存放地见索引）"
    )
    out("audit citations", not dangling, detail)
    for label, items, hard in (
        ("Dangling §/P citations", dangling, True),
        ("Citations whose named doc lacks the number", doubtful, False),
    ):
        if items:
            line = f"{label} ({len(items)}): " + " | ".join(items[:8])
            (fails if hard else warns).append(line)


def check_audit_index_in_sync() -> None:
    """`docs/架构审计索引.md` 必须是"现在重算一遍"的那一份。

    为什么要有它（10-01，与归档同批）：索引是"编号 → 地址"的那张地图，地图一旦没人更新，
    它就变成**看起来权威的假地址** —— 比没有地图更糟。所以它跟 `frontend/dist` 一样按
    "入库的第二份事实"处理：生成物入库 + 一条比字节的断言。搬一次档之后忘了重跑生成器，
    这里就是红，而不是三个月后有人照着索引去找一个不存在的路径。
    """
    index = _audit_index()
    target = ROOT / "docs" / "架构审计索引.md"
    if not target.exists():
        out(
            "audit index in sync",
            False,
            "docs/架构审计索引.md 不见了（跑 scripts/tools/build_audit_index.py）",
        )
        fails.append("audit index missing")
        return
    expected = index.render()
    current = target.read_text(encoding="utf-8")
    if current == expected:
        rows = sum(
            1 for line in expected.splitlines() if line.startswith("| ") and " | " in line[2:]
        )
        out("audit index in sync", True, f"{rows} 行与重算结果逐字一致")
        return
    delta = len(expected.splitlines()) - len(current.splitlines())
    out(
        "audit index in sync",
        False,
        f"索引与重算不一致（行数差 {delta:+}）—— 重跑 scripts/tools/build_audit_index.py",
    )
    fails.append("audit index stale")
