"""锁的**平台标记**：判据与补钉的唯一实现（2026-10-08「Linux 装不上锁」事故的收口）。

事故形状：两把锁由 `pip-compile` 在 **Windows** 上解析（P1-12 拍板「Windows-first，锁在
生成机平台解析」），而 pip-compile 写传递依赖时**把上游的环境标记整个丢掉**。同一把锁被
Linux 消费（CI 门禁、镜像、发布链）时第一步就撞墙，CI 日志原文：

    ERROR: Could not find a version that satisfies the requirement pywin32==312
    ERROR: No matching distribution found for pywin32==312   (from versions: none)

**另一半也实测过**：手工把标记加回锁里、再跑一轮 `pip-compile`，标记被重新剥掉
（`pywin32==312 ; sys_platform == "win32"` 回到裸的 `pywin32==312`）。所以只改锁本体不够 ——
周更那一步必须把钉补回来（`scripts/tools/recompile_locks.py` 调这里的 `repair`），判据必须
盯着钉没钉歪，否则下一周的锁差异 PR 会把这次的修复**静默 revert**。

为什么不是一张手抄名单：名单朝两个方向烂。上游哪天把某依赖改成跨平台而名单还在钉标记，
那一侧该装的就被钉没了；上游新增一个 Windows-only 的二进制依赖而名单没它，Linux 又炸。
所以"谁在什么平台装"读**已安装分包的元数据**（`importlib.metadata` 的 `Requires-Dist`），
名单本身不存在。

只有一处需要实测结论而不是读元数据：**"漏钉会不会让另一侧装不上"**。纯 Python 的
Windows-only 包（`pefile` / `pywin32-ctypes` / `colorama` / `tzdata`）在 Linux 照样装得上，
漏钉只是多装一个用不上的东西；只有二进制那族会当场崩。这一族按本轮实测给（见
`CRASHES_ELSEWHERE` 的理由）。给装得上的那些顺手钉标记会**删掉 Linux 现在实际拿到的东西**
—— 那是没量过的运行环境改动，不归这一刀。

平台判定一律按**锁的目标 Python**（3.13）评估，不取当前解释器：在 3.14 的机器上跑判据
也量出同一个世界。`extra` 门与平台问**分开问**（`uvloop` 住在 `uvicorn[standard]` 下面），
否则"平台中立 + 可选"的边会被读成平台性证据，判据会咬自己的修复。

测试接缝：取数只有两个口（`installed_edges` / `installed_requires`），判据与补钉都收参数化的
图，单测喂假图、不碰盘上的真环境（"测试全离线"铁律）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from importlib import metadata
from typing import Any

from packaging.markers import InvalidMarker, Marker, default_environment

#: 锁的目标 Python（pip-compile 横幅里那行 `with Python 3.13`）。
LOCK_PYTHON_VERSION = "3.13"

#: 两个对照环境的**差异位**。键名必须是 packaging 认的下划线名（`sys_platform`，不是
#: `sys.platform`）：`Marker.evaluate(env)` 把传入字典**覆盖**在真实机器默认环境之上，
#: 写错的键等于没写。实测踩过这一格 —— 拿点号键构造，两个平台都继承了本机 Windows，
#: `pywin32` 被判成"两平台都装"，判据当场成了摆设（失效形状与它要防的病一模一样）。
_ENV_WIN: dict[str, str] = {
    "os_name": "nt",
    "sys_platform": "win32",
    "platform_version": "10.0",
    "platform_machine": "AMD64",
    "platform_system": "Windows",
    "platform_release": "10",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "python_version": LOCK_PYTHON_VERSION,
    "python_full_version": f"{LOCK_PYTHON_VERSION}.0",
}
_ENV_LNX: dict[str, str] = {
    "os_name": "posix",
    "sys_platform": "linux",
    "platform_version": "#1 SMP",
    "platform_machine": "x86_64",
    "platform_system": "Linux",
    "platform_release": "6.8.0",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "python_version": LOCK_PYTHON_VERSION,
    "python_full_version": f"{LOCK_PYTHON_VERSION}.0",
    # PEP 599 的 libc 三兄弟在 Windows 的默认环境里**不存在**（键缺失 ⇒ 那一侧不装），
    # Linux 才有；所以只给 Linux 侧填值。
    "platform_libc": "glibc",
    "platform_libc_release": "",
    "platform_libc_version": "2.39",
}

def _env(overrides: Mapping[str, str]) -> dict[str, Any]:
    """从本机默认环境起，只覆盖平台那几位。

    起点用 `default_environment()` 而不是手写全表：packaging 随版本增减变量，手写表漏一个
    新键就从"两平台同值"变成"从本机继承"。上面的覆盖项含所有能区分 win / 非 win 的键；
    单测喂的假图两侧都显式给值，不依赖本机。
    """
    base = dict(default_environment())
    base["extra"] = ""
    base.update(overrides)
    return base

ENV_WIN: dict[str, Any] = _env(_ENV_WIN)
ENV_LNX: dict[str, Any] = _env(_ENV_LNX)

#: 钉 Windows-only 的 pin 用的规范写法（判据核**语义**，不核字面）。
WIN_ONLY_MARKER = 'sys_platform == "win32"'

#: 漏钉会让**另一侧装不上**（不是"多装一个用不着的"）的那一族。
#:
#: 2026-10-08 用 `pip download --platform manylinux2014_x86_64 --only-binary=:all:` 逐条实测
#: 同一批 pin：`pywin32==312` 取不到任何发行物（`from versions: none` ⇒ pip 在解析期退 1），
#: 而 `pefile==2024.8.26` / `pywin32-ctypes==0.2.3` / `colorama==0.4.6` / `tzdata==2026.5` 四个
#: 都取到了 wheel。差别是纯 Python 与 Windows 二进制。判据的红线只钉会崩的那一族。
#:
#: 这一族**不靠读元数据成立**（元数据只说"谁声明"，不说"另一侧有没有货"），所以它有可能
#: 与元数据冲突：那时判据点名要求人来核对，而不是闭嘴（见 `lock_problems` 里那一格）。
CRASHES_ELSEWHERE: frozenset[str] = frozenset({"pywin32"})

#: `extra` 子句。两种引号都要认：PEP 508 允许单双，而 `Requires-Dist` 从 wheel 元数据读出来
#: 是**单引号**（`extra == 'standard'`），手写需求文件是双引号。只认双引号的版本会把
#: uvloop / pygments 那一族读成"两平台都不装"（实测踩过：判据把自己补进去的行判红）。
#: 捕获组里就是带引号的值。
_EXTRA_CLAUSE = re.compile(r"""extra\s*(?:==|!=|not\s+in|in)\s*('[^']*'|"[^"]*")""")

def norm(name: str) -> str:
    """PEP 503 口径的包名归一（锁、元数据、镜像三方用的同一把尺）。"""
    return name.lower().replace("_", "-")

def _extra_names(marker_text: str) -> set[str]:
    """标记里 `extra` 门控的那些名字（`extra in "a b"` 展开成两个）。"""
    names: set[str] = set()
    for quoted in _EXTRA_CLAUSE.findall(marker_text):
        names.update(quoted.strip("'\"").split())
    return names

def _strip_extra_clauses(marker_text: str) -> str:
    """摘掉标记里的 `extra` 子句（补钉只钉平台，extras 由被钉方自己选）。

    **不动外层括号**：uvloop 的声明是 `(sys_platform != "win32" and (...)) and
    extra == "standard"`，摘掉 extra 之后那对括号是配平的、marker 语法合法；拿
    `strip("()")` 去括会把嵌套层的右括号吃掉，留下解析不成的半截式子。
    """
    text = re.sub(rf"\s*and\s+(?:{_EXTRA_CLAUSE.pattern})", "", marker_text)
    text = re.sub(rf"^(?:{_EXTRA_CLAUSE.pattern})\s*and\s*", "", text)
    text = re.sub(rf"^(?:{_EXTRA_CLAUSE.pattern})\s*", "", text)
    return text.strip()

def _installs(marker: Marker, env: Mapping[str, Any]) -> bool:
    try:
        return marker.evaluate(dict(env))
    except Exception:
        return False

SHAPE_WIN = "win"
SHAPE_UNIX = "unix"
SHAPE_BOTH = "both"
SHAPE_NEVER = "never"
SHAPE_INERT = "inert"

def platform_of(marker_text: str, requested_extras: Iterable[str] = ()) -> str:
    """一条声明在我们这两个平台下的形状。

    `extra` 门先单独问，再问平台：声明方**没被要求**那个 extra 时这条边不存在
    （`inert`）；要求了才看平台。摘完只剩 extras 条件（平台中立）也是 `inert`。
    """
    gates = _extra_names(marker_text)
    if gates and not gates & {norm(e) for e in requested_extras}:
        return SHAPE_INERT
    stripped = _strip_extra_clauses(marker_text)
    if not stripped:
        return SHAPE_INERT
    try:
        marker = Marker(stripped)
    except InvalidMarker:
        return SHAPE_NEVER
    win = _installs(marker, ENV_WIN)
    lnx = _installs(marker, ENV_LNX)
    if win and not lnx:
        return SHAPE_WIN
    if lnx and not win:
        return SHAPE_UNIX
    if win and lnx:
        return SHAPE_BOTH
    return SHAPE_NEVER

Edge = tuple[str, str | None, str]

def classify(name: str, edges: Mapping[str, list[Edge]]) -> str:
    """按上游声明图给包名定性：`required` / `win` / `unix` / `both` / `never` / `unknown`。

    * `required` —— 至少一处**无条件**声明：两平台都必须装，锁里不许带标记；
    * `win` / `unix` —— 只在一侧装（pywin32 / uvloop 两族的形状）；
    * `both` —— 声明了但两平台都装（纯 `python_version` 那类条件）：不该钉；
    * `never` —— 所有声明在两平台都为假：锁里不该有它；
    * `unknown` —— 图里没有这个名字，或只有 `inert` 的边（我们直接点名装的那批落这档）：不判。

    `inert` 的边**不参与**定性：一个包只被"某个没被要求的 extra"带进来时，说它属于哪个平台
    都是猜。
    """
    declared = edges.get(norm(name))
    if not declared:
        return "unknown"
    win = False
    lnx = False
    saw_claim = False
    for _parent, marker_text, _spec in declared:
        if marker_text is None:
            return "required"
        shape = platform_of(marker_text)
        if shape == SHAPE_INERT:
            # 只被"没被要求的 extra"带进来：说它属于哪个平台都是猜 ⇒ 不出结论。
            # （这一族由 `lock_findings` 在**行**一级问：那条 pin 自己请求了哪些 extras。）
            continue
        saw_claim = True
        if shape in (SHAPE_WIN, SHAPE_BOTH):
            win = True
        if shape in (SHAPE_UNIX, SHAPE_BOTH):
            lnx = True
    if not saw_claim:
        return "unknown"
    if win and not lnx:
        return SHAPE_WIN
    if lnx and not win:
        return SHAPE_UNIX
    if win and lnx:
        return SHAPE_BOTH
    return SHAPE_NEVER

def expected_marker(name: str, edges: Mapping[str, list[Edge]]) -> str | None:
    """该包在锁里**应当**带的标记；不该带（其余档位）返回 None。

    `win` 用规范短写法（`mcp` 那两处 `<3.14` / `>=3.14` 的分裂声明按并集收拢成一句 ——
    钉两条中的任一条都会在另一个 Python 上钉错）；`unix` 尽量复用声明方自己的话。
    """
    cls = classify(name, edges)
    if cls == SHAPE_WIN:
        return WIN_ONLY_MARKER
    if cls != SHAPE_UNIX:
        return None
    for _parent, marker_text, _spec in edges.get(norm(name), []):
        if not marker_text:
            continue
        stripped = _strip_extra_clauses(marker_text)
        if stripped and platform_of(stripped) == SHAPE_UNIX:
            return stripped
    return 'sys_platform != "win32"'

_EDGES: dict[str, list[Edge]] | None = None

def installed_edges() -> dict[str, list[Edge]]:
    """被声明包名 → [(声明者, 声明标记原文|None, 版本约束原文)]。

    读自已安装分包的元数据，单次进程内缓存（一致性整趟几十个包名问的是同一份图）。
    标记留原文 —— 补钉要把上游自己的话写回锁里，不该由判据重新格式化。
    """
    global _EDGES
    if _EDGES is not None:
        return _EDGES
    edges: dict[str, list[Edge]] = {}
    for dist in metadata.distributions():
        parent = norm(dist.metadata["Name"] or "")
        if not parent:
            continue
        for name, marker_text, spec in _requires_of(dist):
            edges.setdefault(name, []).append((parent, marker_text, spec))
    _EDGES = edges
    return edges

def reset_edges_cache() -> None:
    """接缝：单测喂假图之前清掉进程内缓存。"""
    global _EDGES
    _EDGES = None

def _requires_of(dist: metadata.Distribution) -> list[tuple[str, str | None, str]]:
    """一个分包的 `Requires-Dist` → [(归一化包名, 标记原文|None, 版本约束原文)]。"""
    rows: list[tuple[str, str | None, str]] = []
    for raw in dist.requires or []:
        head, _, marker_part = raw.partition(";")
        head = head.strip()
        name = re.split(r"[\[\s<>=!~;]", head, maxsplit=1)[0]
        if not name:
            continue
        spec = head[len(name) :]
        if spec.lstrip().startswith("["):  # 剥 extras 段：`name[a,b]>=1` → `>=1`
            _, _, spec = spec.partition("]")
        rows.append((norm(name), marker_part.strip() or None, spec.strip()))
    return rows

def installed_requires(name: str, extras: Iterable[str] = ()) -> list[tuple[str, str | None]]:
    """本环境里那个包声明了什么（`inert` 的边已筛掉）；没装返回 []（看不见 ⇒ 不判）。

    `extras` 是**锁里那一行**请求的 extras（`uvicorn[standard]`）：判据问的是"我们装的这一份
    需要什么"，不是"这个包总共能需要什么"。
    """
    wanted = {norm(e) for e in extras}
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return []
    out: list[tuple[str, str | None]] = []
    for dep, marker_text, _spec in _requires_of(dist):
        if marker_text is None:
            out.append((dep, None))
            continue
        if platform_of(marker_text, wanted) == SHAPE_INERT:
            continue
        out.append((dep, marker_text))
    return out

_PIN_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?==)(\S+)(.*)$")

class Pin:
    """锁里的一条 pin：行号、归一化基名、请求的 extras、版本、已有标记（None = 裸）。"""

    __slots__ = ("lineno", "name", "extras", "version", "marker")

    def __init__(
        self, lineno: int, name: str, extras: tuple[str, ...], version: str, marker: str | None
    ) -> None:
        self.lineno = lineno
        self.name = name
        self.extras = extras
        self.version = version
        self.marker = marker

    @property
    def label(self) -> str:
        return self.name if not self.extras else f"{self.name}[{','.join(self.extras)}]"

def parse_lock(text: str) -> list[Pin]:
    """顶格 pin 行（pip-tools 的规矩：pin 永不缩进；`#` 与 `--` 开头的行都不是 pin）。"""
    pins: list[Pin] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if not line or line[0] in " \t#-":
            continue
        m = _PIN_LINE.match(line)
        if not m:
            continue
        spec = m.group(1)
        extras: tuple[str, ...] = ()
        if "[" in spec:
            inner = spec[spec.index("[") + 1 : spec.index("]")]
            extras = tuple(norm(x) for x in inner.split(",") if x.strip())
        tail = m.group(3).strip()
        marker = (tail[1:].strip() or None) if tail.startswith(";") else None
        pins.append(Pin(i, norm(spec.split("[", 1)[0].rstrip("=")), extras, m.group(2), marker))
    return pins

def _same_semantics(got: str, want: str) -> bool:
    """两个标记在两平台下的真值是否一致（核语义而不是核字面写法）。"""
    try:
        g = Marker(got)
        w = Marker(want)
    except InvalidMarker:
        return False
    return _installs(g, ENV_WIN) == _installs(w, ENV_WIN) and _installs(
        g, ENV_LNX
    ) == _installs(w, ENV_LNX)

def lock_findings(
    text: str,
    edges: Mapping[str, list[Edge]],
    label: str,
    requires: Callable[[str, Iterable[str]], list[tuple[str, str | None]]] | None = None,
) -> tuple[list[str], list[str]]:
    """一把锁的 (判红的, 只出声不拦的)。

    判红三格：
      1. `CRASHES_ELSEWHERE` 里的包裸着或钉歪 —— 另一侧 `pip install` 在解析期就崩；
      2. 上游有一处**无条件**声明、锁里却带了标记 —— 被钉掉的那一侧静默不装；
      3. `CRASHES_ELSEWHERE` 的包按元数据判不出平台性（上游改了声明、或本地看不见它）——
         这条名单不靠元数据成立，冲突时**要点名**，不许悄悄放行。

    只出声不拦（改它 = 换另一侧平台实际装的东西，是没量过的环境改动，留给独立一刀）：
      4. 某个 pin 声明了"另一侧才装"的依赖，而锁里**整条没有**它 —— 那一侧只能拿到
         没锁版本的它（本轮实测到的 `uvicorn[standard]` → `uvloop` 就是这一形状；
         Windows 解析把它整条剥掉，补标记救不回来）。
    """
    problems: list[str] = []
    noise: list[str] = []
    pins = parse_lock(text)
    if not pins:
        return [f"{label} 里一个 pin 都读不到（空锁或形状变了）"], noise
    present = {p.name for p in pins}
    for pin in pins:
        cls = classify(pin.name, edges)
        if cls == "required" and pin.marker:
            problems.append(
                f"{label} L{pin.lineno} {pin.label}=={pin.version} 带标记 `{pin.marker}`，可上游有"
                "一处**无条件**声明 —— 被钉掉的那一侧会静默不装"
            )
            continue
        if pin.name in CRASHES_ELSEWHERE:
            want = expected_marker(pin.name, edges)
            if want is None:
                problems.append(
                    f"{label} L{pin.lineno} {pin.label}=={pin.version} 在 "
                    "`CRASHES_ELSEWHERE` 名单里，但按已装元数据判不出平台性 —— 查它现在的声明，"
                    "再决定这一行留不留（名单不会自己报警，这是它唯一的出声处）"
                )
            elif pin.marker is None:
                problems.append(
                    f"{label} L{pin.lineno} {pin.label}=={pin.version} 裸着 —— 另一侧平台的"
                    f" `pip install` 在解析期就崩（该钉 `; {want}`）"
                )
            elif not _same_semantics(pin.marker, want):
                problems.append(
                    f"{label} L{pin.lineno} {pin.label}=={pin.version} 钉了 `{pin.marker}`，"
                    f"两平台的真值不等于 `; {want}`"
                )
            continue
        if cls == SHAPE_NEVER:
            problems.append(
                f"{label} L{pin.lineno} {pin.label}=={pin.version}：上游声明在两平台都为假，"
                "锁里不该有它"
            )
    for pin in pins:
        for dep, marker_text in (requires or installed_requires)(pin.name, pin.extras):
            if marker_text is None or dep in present:
                continue
            shape = platform_of(marker_text, pin.extras)
            if shape in (SHAPE_WIN, SHAPE_UNIX):
                noise.append(
                    f"{label}：{pin.label}=={pin.version} 在"
                    f"{'仅 Windows' if shape == SHAPE_WIN else '仅非 Windows'}声明了 {dep}，"
                    "锁里没有它的 pin —— 那一侧平台拿到的是**没锁版本**的它"
                )
    return problems, noise

def repair(
    text: str,
    edges: Mapping[str, list[Edge]],
    names: Iterable[str] = CRASHES_ELSEWHERE,
) -> tuple[str, list[str]]:
    """把 `names` 里包名的裸/钉歪 pin 换成语义正确的标记。幂等：第二次跑 notes 为空。

    只动 `names`（默认=会崩那一族）：给"另一侧也装得上"的包顺手钉标记会删掉那一侧现在
    实际拿到的东西 —— 那不属于修"装不上"这一刀。
    """
    targets = {norm(n) for n in names}
    notes: list[str] = []
    out_lines: list[str] = []
    for line in text.splitlines():
        new_line = line
        if line and line[0] not in " \t#-" and "==" in line:
            m = _PIN_LINE.match(line)
            if m:
                name = norm(m.group(1).split("[", 1)[0].rstrip("="))
                if name in targets:
                    want = expected_marker(name, edges)
                    tail = m.group(3).strip()
                    have = tail[1:].strip() if tail.startswith(";") else tail
                    if want and not (have and _same_semantics(have, want)):
                        new_line = f"{line.split(';', 1)[0].rstrip()} ; {want}"
                        notes.append(f"{name} -> ; {want}")
        out_lines.append(new_line)
    return "\n".join(out_lines) + ("\n" if text.endswith("\n") else ""), notes
