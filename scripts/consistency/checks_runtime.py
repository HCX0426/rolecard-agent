"""runtime 主题这一族判据（P3-9 按主题细分，从 checks.py 平移，正文一字未改）。

按主题拆出；执行顺序由 registry.CHECKS 唯一决定，本模块只回答"这一族住哪"。
行为等价由门禁实跑全部 CHECKS 证明。
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import subprocess
import sys

# 跨族共享助手：唯一定义在别的族模块，按「谁在用谁 import」接线（不复制定义）。
from .checks_audit import _BANNED_USER_VISIBLE  # noqa: F401
from .core import ROOT, fails, iter_files, out, warns


def _div_chain_parts(node: ast.AST) -> list[str]:
    """一条 `X / "a" / "b"` 链上的字符串片段，按原序带回引号。

    pathlib 把每一段拆成**各自独立的常量**，所以只比单个常量永远看不见 `"build" / "sidecar"`
    —— M5 那发变异第一版就是这么蒙混过关的（它正是这条尺子要防的那一类"两处各拼一遍"）。
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _div_chain_parts(node.left) + _div_chain_parts(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [f'"{node.value}"']
    return []


def check_artifact_single_source() -> None:
    """产物路径与安装包名的拼法只许有一处（`core/artifacts.py`），配置侧必须与它一致。

    为什么立这条（10-01，打包链台账 `R28-59`）：`build/sidecar/rolecard-backend` 从前由六个
    文件各拼一遍，装后那条 `_internal\\frontend\\dist` 由三个脚本各拼一遍，安装包名模式由四个
    地方各写一遍。漂移不会喊：`artifactName` 改了只有装机脚本那条会红，而下载卡按 glob 找 ——
    它会安静地判断"没有可下载的包"，界面于是**照设计**不渲染入口，缺陷长得像正常行为
    （"没有产物就不画死按钮"是刻意的，所以这个假象没有人会怀疑）。

    两半：**(a)** 谁都不许在自己的码里重新拼那几条路径（按字面形状找，不看文件名）；
    **(b)** 不能 import Python 的那两侧（`install_package.ps1` / `electron-builder.yml` /
    `ci.yml`）写的字面量必须与这里的常量**同形** —— 它们天生只能抄，那就每次对一遍。
    """
    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.core import artifacts  # noqa: PLC0415

    owner = "src/rolecard_agent/core/artifacts.py"
    banned = {
        '"build" / "sidecar': "用 artifacts.sidecar_bundle(root)",
        '"resources" / "rolecard-backend"': "用 artifacts.installed_backend_bundle",
        '"_internal" / "frontend"': "用 artifacts.installed_dist(root)",
        '"Programs" / "rolecard-agent"': "用 artifacts.installed_dir(local_appdata)",
        '"rolecard-agent-*.exe"': "用 artifacts.ARTIFACT_GLOB",
        '"rolecard-backend.exe"': "用 f\"{artifacts.BACKEND_NAME}.exe\"",
        '"win-unpacked"': "用 artifacts.unpacked_backend(release_dir)",
    }
    # 读的是 **AST 里的字符串常量**，不是原文（与 `single-source literals` 同一个取向）：
    # 注释里提一嘴"落在 build/sidecar 里"是这件东西的存在理由，不是第二处拼法。按原文比，
    # 这把尺子第一趟就红在自己的注释与自己的禁令表上 —— "新写的尺子把它防的毛病带进实现"
    # 这一形状本轮第三次（`R28-27` / `R28-31` / `R28-34`）。
    # 只豁免两格：出处文件本体（它当然要有这些字面量）与本文件（一把尺子必须能说出它禁什么）。
    exempt = {owner, pathlib.Path(__file__).relative_to(ROOT).as_posix()}
    offenders: list[str] = []
    for base in (ROOT / "src", ROOT / "scripts"):
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in exempt:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                haystack: list[str] = []
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    haystack.append(node.value)
                elif isinstance(node, ast.BinOp):
                    joined = _div_chain_parts(node)
                    if len(joined) > 1:
                        haystack.append(" / ".join(joined))
                for text in haystack:
                    for literal, remedy in banned.items():
                        if literal in text:
                            line = getattr(node, "lineno", 0)
                            offenders.append(f"{rel}:{line} 重拼了 {literal} —— {remedy}")
    if offenders:
        out("artifact single source", False, "; ".join(offenders[:4]))
        fails.append(f"artifact paths re-spelled: {offenders[:6]}")
        return

    # (b) 配置侧同形检查
    problems: list[str] = []
    builder = ROOT / "shell" / "electron-builder.yml"
    if builder.exists():
        text = builder.read_text(encoding="utf-8", errors="ignore")
        name_line = re.search(r"^\s*artifactName:\s*(\S+)\s*$", text, flags=re.M)
        if not name_line or not name_line.group(1).startswith(f"{artifacts.APP_NAME}-"):
            problems.append(
                f"electron-builder.yml 的 artifactName 不以 {artifacts.APP_NAME}- 开头"
                f"（读到 {name_line.group(1) if name_line else '没有这一行'}）"
            )
        to_line = re.search(r"^\s*to:\s*(\S+)\s*$", text, flags=re.M)
        if to_line and to_line.group(1) != artifacts.BACKEND_NAME:
            problems.append(
            f"electron-builder 的 extraResources to={to_line.group(1)} ≠ {artifacts.BACKEND_NAME}"
        )
    ps1_path = ROOT / "scripts" / "install_package.ps1"
    ps1_raw = ps1_path.read_text(encoding="utf-8", errors="ignore")

    def ps1_shape(variable: str) -> str | None:
        """`$X = Join-Path … "一段路径"` 里引号内那段的**归一形状**（反斜杠折叠、小写）。"""
        match = re.search(rf"^\${variable}\s*=.*?\"([^\"]+)\"", ps1_raw, flags=re.M | re.S)
        if not match:
            return None
        return re.sub(r"\\+", "/", match.group(1)).lower()

    def expected(*parts: str) -> str:
        return "/".join(part.lower() for part in parts)

    got_exe = ps1_shape("installedExe")
    want_exe = expected(*artifacts.INSTALL_SUBDIR, f"{artifacts.APP_NAME}.exe")
    if got_exe is None:
        problems.append("读不到 install_package.ps1 里的 $installedExe 那条 Join-Path")
    elif got_exe != want_exe:
        problems.append(f"装后 exe={got_exe} 与出处不同形（应为 {want_exe}）")
    got_backend = ps1_shape("bUILT")
    want_backend = expected(
        "build",
        artifacts.SIDECAR_DIR.split("/")[1],
        artifacts.BACKEND_NAME,
        f"{artifacts.BACKEND_NAME}.exe",
    )
    if got_backend is not None and got_backend != want_backend:
        problems.append(
            f"ps1 里「刚构建那份」= {got_backend} 与出处不同形（应为 {want_backend}）"
        )
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8", errors="ignore")
    globs = set(re.findall(rf"{artifacts.APP_NAME}-[^\s'\"]*\.exe", ci))
    if globs and not all(re.match(rf"^{artifacts.APP_NAME}-[\w.*-]+\.exe$", g) for g in globs):
        problems.append(f"ci.yml 里的安装包模式与出处不同形：{sorted(globs)}")
    out(
        "artifact single source",
        not problems,
        f"路径与名字只有一处拼法；配置侧 {artifacts.APP_NAME} / {artifacts.BACKEND_NAME} 同形"
        if not problems
        else "; ".join(problems[:4]),
    )
    if problems:
        fails.append(f"artifact literals drift: {problems}")


#: 数据根下"会被应用写字"的目录必须整目录进 `.gitignore`（`R102-76`）。
#:
#: 开发态的数据根就是仓库的 `data/`（`base/paths.py::_platform_data_root` 故意如此），
#: 所以任何新落进那一格的目录，只要没被忽略，就会被下一次 `git add` 当成源码带进库 ——
#: 而 `data/workspace` 是 `fs_write` 工具的默认根、`retention-backups` 是被删审计行与
#: 命令原文的 JSONL，两格内容都是真实用户数据。
#: 例外必须写理由，且例外本身也被数：登记了却不再对应代码里的目录 ⇒ 红（防豁免名单变垃圾桶）。
_DATA_ROOT_EXEMPT: dict[str, str] = {
    "sqlite": "按后缀逐类忽略（*.db / -wal / -shm / -journal / *.bak / *.trace.jsonl），"
              "`.gitkeep` 与 `_stale-dev-snapshot-*/` 各有专门条目 —— 这一格是刻意分开的",
}


def _path_chain_parts(node: ast.expr) -> list[str]:
    """拆 `root / "sqlite" / "app.db"` 这种链，返回**从里到外**的字符串段。

    不能靠 `ast.walk` 取"第一个常量"：那是 BFS，`app.db` 会排在 `sqlite` 前面。
    """
    parts: list[str] = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            parts.append(node.right.value)
        node = node.left
    return parts[::-1]


def _data_root_write_dirs() -> dict[str, str]:
    """从**代码**里数出数据根下会写字的目录名（不看文件系统：文件系统里躺着的全是被忽略的运行时件）。"""
    dirs: dict[str, str] = {}
    paths_py = ROOT / "src" / "rolecard_agent" / "base" / "paths.py"
    tree = ast.parse(paths_py.read_text(encoding="utf-8"))
    fn = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "data_paths"
        ),
        None,
    )
    if fn is None:  # pragma: no cover - 推导处改名时先当红
        return dirs
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            parts = _path_chain_parts(value)
            if parts:
                dirs[parts[0]] = f"base/paths.py::data_paths[{key.value}]"
    db_py = ROOT / "src" / "rolecard_agent" / "storage" / "db.py"
    for node in ast.walk(ast.parse(db_py.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "RETENTION_BACKUP_DIRNAME" not in names:
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            dirs[value.value] = "storage/db.py::RETENTION_BACKUP_DIRNAME"
    return dirs


def check_data_root_dirs_gitignored() -> None:
    """`data/<运行时目录>/*` 必须在 `.gitignore` 里，例外要带理由（`R102-76`，批 24 的尺子）。

    两臂都判：新目录没被忽略 ⇒ 红；豁免名单里留着一格代码里已经不写的目录 ⇒ 也红。
    """
    dirs = _data_root_write_dirs()
    if not dirs:
        out(
            "data root dirs are gitignored",
            False,
            "从代码里一个目录都没数出来（data_paths 改名了？）",
        )
        fails.append("data-root ruler is hollow: no directory was enumerated from base/paths.py")
        return
    lines = [
        ln.strip()
        for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    ungoverned = [
        name
        for name in dirs
        if name not in _DATA_ROOT_EXEMPT
        and not any(line in {f"data/{name}", f"data/{name}/*"} for line in lines)
    ]
    stale = [name for name in _DATA_ROOT_EXEMPT if name not in dirs]
    ok = not ungoverned and not stale
    detail = (
        f"现数 {len(dirs)} 格（豁免 {len(_DATA_ROOT_EXEMPT)}）：" + ", ".join(sorted(dirs))
        if ok
        else f"没被忽略：{ungoverned}；豁免已失效：{stale}"
    )
    out("data root dirs are gitignored", ok, detail)
    if ungoverned:
        fails.append(
            "data-root runtime dirs are not gitignored (dev root IS the repo's data/): "
            f"{ungoverned} —— 用户数据会被下一次 git add 带进库"
        )
    if stale:
        fails.append(
            f"exemption list entries no longer match a code-declared dir: {stale}"
            "（豁免要跟着实况走）"
        )


def _png_corner_alphas(blob: bytes, limit: int = 512) -> list[int] | None:
    """读一张 PNG 的**四个角**的 alpha（只用标准库：zlib 解压 + 逐行反过滤）。

    为什么自己解而不是引 PIL：主 `.venv` 里没有 PIL（那是 `.venv-ocr` 才有的重依赖），
    而这条断言跑在每一次门禁与 CI 上 —— 为一格判据把图像库拖进运行树，正是本仓反对的那种换法。

    只读非隔行、8bit、color type 6 的图（图标就是这么生成的），别的形状一律返回 `None`
    让调用方**出声**而不是猜。`limit` 是给单边像素数的兜底，防着有人把一个巨型图塞进 ICO。
    """
    import zlib

    if blob[:8] != b"\x89PNG\r\n\x1a\n" or len(blob) < 33:
        return None
    width = int.from_bytes(blob[16:20], "big")
    height = int.from_bytes(blob[20:24], "big")
    depth, ctype, _compress, _filter, interlace = blob[24], blob[25], blob[26], blob[27], blob[28]
    if depth != 8 or ctype != 6 or interlace != 0:
        return None
    if not (0 < width <= limit and 0 < height <= limit):
        return None
    idat = bytearray()
    pos = 33
    while pos + 8 <= len(blob):
        ln = int.from_bytes(blob[pos : pos + 4], "big")
        kind = blob[pos + 4 : pos + 8]
        if kind == b"IEND":
            break
        if kind == b"IDAT":
            idat += blob[pos + 8 : pos + 8 + ln]
        pos += 12 + ln
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    bpp, stride = 4, width * 4
    need = (stride + 1) * height
    if len(raw) < need:
        return None

    def paeth(a: int, b: int, c: int) -> int:
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)

    out = bytearray()
    prev = bytearray(stride)
    for y in range(height):
        base = y * (stride + 1)
        ft = raw[base]
        line = bytearray(raw[base + 1 : base + 1 + stride])
        if ft == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 0xFF
        elif ft == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ft == 3:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 0xFF
        elif ft == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                c = prev[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + paeth(a, prev[x], c)) & 0xFF
        elif ft != 0:
            return None
        out += line
        prev = line
    idx = [0, (width - 1) * 4, (height - 1) * stride, height * stride - 4]
    return [out[i + 3] for i in idx]


def check_app_icon_frames() -> None:
    """应用图标必须是**多帧 + 带 alpha**（`R102` 归档后由用户报的"桌面图标有白底"换来）。

    装机版快捷方式的图标取自 exe 内嵌的那份 `shell/build/icon.ico`，而它从壳选型那次起
    就没人重生成过：**只有一帧 256、四角全不透明**（源图是"圆角方块摆在白画布上"的展示图，
    外圈留白被原样烙进来，右下角还带着生成器水印）。16/32/48 全靠硬缩，托盘与任务栏因此发糊。
    产物由 `scripts/tools/make_app_icon.py` 生成，这条只验结果，三格都判：
      · 帧数与档位：至少 6 帧，且 16/32/48/256 都在；
      · 每帧必须是 PNG 编码且 **color type = 6（RGBA）** —— 没有 alpha 通道就不可能透明；
      · 母图 `shell/app-icon-master.png` 必须在（图标要能重生成，不是手画的孤品）。
    """
    ico = ROOT / "shell" / "build" / "icon.ico"
    master = ROOT / "shell" / "app-icon-master.png"
    if not ico.exists():
        out("app icon is multi-frame RGBA", False, f"图标不在：{ico}")
        fails.append(f"app icon missing: {ico}")
        return
    data = ico.read_bytes()
    problems: list[str] = []
    if len(data) < 6 or int.from_bytes(data[2:4], "little") != 1:
        out("app icon is multi-frame RGBA", False, "不是 ICO（类型字段不是 1）")
        fails.append("shell/build/icon.ico is not an ICO")
        return
    count = int.from_bytes(data[4:6], "little")
    sizes: list[int] = []
    for i in range(count):
        e = data[6 + 16 * i: 22 + 16 * i]
        if len(e) < 16:
            break
        w = 256 if e[0] == 0 else e[0]
        h = 256 if e[1] == 0 else e[1]
        off = int.from_bytes(e[12:16], "little")
        ln = int.from_bytes(e[8:12], "little")
        sizes.append(min(w, h))
        blob = data[off : off + ln]
        if blob[:8] != b"\x89PNG\r\n\x1a\n":
            problems.append(f"{w}px 帧不是 PNG（读不到 alpha 通道）")
        elif len(blob) < 26 or blob[25] != 6:
            problems.append(f"{w}px 帧的 PNG color type 不是 6（RGBA），没有 alpha 通道")
        else:
            # 第四格：光"有 alpha 通道"挡不住白底 —— 通道在、四角全不透明照样是白底。
            # 阈值 16 是给圆角那圈抗锯齿留的余地（实测装机那份 32px 帧四角 alpha=1，
            # 而带白底那版是 255）；判据读的是**角**，不是"平均透明度"那种会被整图摊平的数。
            corners = _png_corner_alphas(blob)
            if corners is None:
                problems.append(f"{w}px 帧的角像素读不出（非 8bit/RGBA/无隔行？不能当成干净）")
            elif max(corners) >= 16:
                problems.append(f"{w}px 帧四角 alpha={corners} —— 白底回来了")
    if len(set(sizes)) < 6:
        problems.append(f"只有 {len(set(sizes))} 档帧，至少要 6 档")
    missing = sorted({16, 32, 48, 256} - set(sizes))
    if missing:
        problems.append(f"缺档位 {missing}")
    if not master.exists():
        problems.append(f"母图不在：{master.relative_to(ROOT)}")
    ok = not problems
    out(
        "app icon is multi-frame RGBA",
        ok,
        f"{len(set(sizes))} 档帧（{sorted(set(sizes))}），全部 PNG-RGBA"
        if ok
        else "；".join(problems),
    )
    if problems:
        fails.append(
            "app icon is not a clean multi-frame RGBA set: " + "；".join(problems)
            + " —— 重跑 .venv-ocr\\Scripts\\python.exe scripts/tools/make_app_icon.py"
        )


def check_safety_prompt() -> None:
    """The global safety rules must be *defined in code*, not just described in prose.

    They used to exist only in the archived design doc, while `需求与验收标准.md` US-4
    treated them as a shipped requirement. Safety-critical code is deliberately not
    delegated (CONTRIBUTING section 1).
    """
    path = ROOT / "src" / "rolecard_agent" / "core" / "prompts.py"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    has_const = bool(re.search(r"^GLOBAL_SAFETY_PROMPT\s*=", text, flags=re.M))
    has_builder = "def build_system_prompt" in text
    has_rules = "禁止输出任何疾病诊断" in text
    ok = has_const and has_builder and has_rules
    out("safety prompt", ok, f"const={has_const} builder={has_builder} rules={has_rules}")
    if not ok:
        fails.append("GLOBAL_SAFETY_PROMPT is not concretely defined in core/prompts.py")


def check_milestone_alignment() -> None:
    """Milestone ids in the README must match the plan (now docs/archive/实施计划.md)."""
    readme_ids = set(re.findall(r"\*\*M(\d)", (ROOT / "README.md").read_text(encoding="utf-8")))
    plan_path = ROOT / "docs" / "archive" / "实施计划.md"
    plan_ids = set(re.findall(r"\*\*M(\d)", plan_path.read_text(encoding="utf-8")))
    ok = readme_ids == plan_ids and bool(plan_ids)
    detail = f"README={sorted(readme_ids)} plan={sorted(plan_ids)}"
    out("milestone alignment", ok, detail)
    if not ok:
        fails.append(f"milestone ids differ between README and plan: {detail}")


def check_v1_v2_boundary() -> None:
    """M4 delivers the HTTP API and the single-page UI *inside v1*; M5 delivers the
    engineering-grade frontend inside v1 too (pulled forward from v2.3).

    So no live document may still advertise them as a v2 roadmap item. This rule exists
    because pulling scope forward left exactly such a leftover behind twice.
    """
    scanned = ("README.md", "docs/archive/实施计划.md", "docs/需求与验收标准.md")
    offenders: list[str] = []
    for name in scanned:
        path = ROOT / name
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "v2." not in line:
                continue
            if "接入层" in line or "单页" in line or "工程化前端" in line:
                offenders.append(f"{name}:{lineno}")
    out(
        "v1/v2 boundary",
        not offenders,
        str(offenders) if offenders else "API + single-page UI stay in v1",
    )
    if offenders:
        fails.append(f"API / single-page UI still advertised as v2: {offenders}")


def _crlf_exempt_globs() -> set[str]:
    """`.gitattributes` 里明写了 `eol=crlf` 的那些 glob。

    09-26 干净克隆实测抓出来的自相矛盾：`.gitattributes` 末尾写着
    "Kept as-is / *.bat text eol=crlf / *.ps1 text eol=crlf"，而这条检查只看字节 ——
    于是在**克隆**里 `scripts/*.ps1` 判红，在作者工作树里判绿（他的文件是手写的 LF，
    从没被 checkout 覆写过）。规矩只能有一个来源：这里跟着 `.gitattributes` 走，
    不再抄一份扩展名清单（只认按文件名匹配的 glob，如 `*.ps1`）。
    """
    attr = ROOT / ".gitattributes"
    if not attr.exists():
        return set()
    globs: set[str] = set()
    for line in attr.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "eol=crlf" not in text:
            continue
        globs.add(text.split()[0])
    return globs


def check_line_endings() -> None:
    """Everything under src/ tests/ scripts/ must be LF, **except** what `.gitattributes`
    deliberately keeps as CRLF.

    33 files were CRLF on the first real lint run, because PowerShell's Set-Content and
    several Windows editors default to CRLF. Mixed line endings become whole-file diffs on
    CI and make `ruff format --check` fail for reasons unrelated to the change
    (C22). .gitattributes prevents it happening again.
    """
    import fnmatch  # noqa: PLC0415

    exempt = _crlf_exempt_globs()
    offenders: list[str] = []
    skipped = 0
    for base in ("src", "tests", "scripts"):
        for path in (ROOT / base).rglob("*"):
            if "__pycache__" in path.parts:
                continue  # 字节码是二进制：其中偶然出现 \r\n 字节序列会造成误报
            if not path.is_file():
                continue
            if any(fnmatch.fnmatch(path.name, glob) for glob in exempt):
                skipped += 1
                continue  # .gitattributes 说了"这类就按 CRLF 检出"，那它不是缺陷
            if b"\r\n" in path.read_bytes():
                offenders.append(str(path.relative_to(ROOT)))
    note = f"（按 .gitattributes 豁免 {skipped} 个：{'/'.join(sorted(exempt)) or '无'}）"
    detail = f"CRLF in: {offenders}" if offenders else f"all LF {note}"
    out("line endings", not offenders, detail)
    if offenders:
        fails.append(f"CRLF line endings found: {offenders}")


def check_ps1_encoding() -> None:
    """带非 ASCII 的 `.ps1` 必须是 **UTF-8 with BOM**。

    为什么：`开发流程.md` 教的是 `powershell -File scripts\\install_package.ps1`，而 Windows
    PowerShell 5.1 对**没有 BOM** 的文件按 ANSI 代码页解码（这台机器是 GBK）。中文注释被读成
    半个字符时，尾字节会把紧随其后的 ASCII 一起吞掉，于是报一句跟真因毫无关系的
    `MissingEndCurlyBrace`。实测 2026-09-30：同一份装机脚本在 5.1 下解析失败、在 pwsh 7 下正常
    （7 默认按 UTF-8 读）—— 也就是"能不能装上"取决于用哪个 shell，这种依赖只能由尺子挡掉。
    纯 ASCII 的文件豁免：哪种代码页读出来都一样，不必强加 BOM。
    """
    offenders: list[str] = []
    for path in iter_files(".ps1"):
        raw = path.read_bytes()
        if all(byte < 128 for byte in raw):
            continue
        if raw[:3] != b"\xef\xbb\xbf":
            offenders.append(str(path.relative_to(ROOT)))
    detail = (
        f"non-ASCII without BOM: {offenders}"
        if offenders
        else "带非 ASCII 的 .ps1 全部带 BOM（5.1 与 pwsh 读出同一份）"
    )
    out("ps1 encoding", not offenders, detail)
    if offenders:
        fails.append(f".ps1 lacks a UTF-8 BOM, PowerShell 5.1 will misread it: {offenders}")


def check_console_encoding() -> None:
    """每个 `scripts/*.py` 入口：会打出 GBK 装不下的字符，就必须自己重配 stdout 编码。

    这条是 `R26-24` 的**收口**而不是重复发现它：那次只修了 `gate.py`，而同一族的
    `build_sidecar.py` 一路漏着 —— 症状最坏的那种：PyInstaller 已经全部成功、225 MB 产物
    都落盘了，收尾那句带对勾 emoji 的 print 在 GBK 控制台上抛 `UnicodeEncodeError` ⇒ 退出码 1，
    看起来像"打包失败"。（09-25 深夜实测撞上。）

    口径用"能不能被 gbk 编码"判，而不是抄一份 emoji 清单：中文本身 GBK 装得下，
    炸的从来是对勾、播放三角、秒表这类 emoji —— 只盯 emoji 清单会漏，只盯"有没有中文"会全是误报。
    而且只看**真被打出来的字符串**（AST 里 `print(...)` 的实参）：注释与文档串里的 emoji
    永远不会进控制台，按全文算会误伤三个本来就安全的脚本。
    """
    import ast  # noqa: PLC0415

    def _printed_literals(tree: ast.AST) -> list[str]:
        out_: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "print":
                continue
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                for part in ast.walk(arg):
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        out_.append(part.value)
                    elif isinstance(part, ast.FormattedValue) and isinstance(
                        part.format_spec, ast.Constant
                    ):
                        out_.append(str(part.format_spec.value))
        return out_

    offenders: list[str] = []
    scripts = sorted((ROOT / "scripts").rglob("*.py"))
    for path in scripts:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literals = _printed_literals(tree)
        if not literals:
            continue
        try:
            "".join(literals).encode("gbk")
            continue  # 打出来的东西全在 GBK 里：这个脚本在任何 codepage 下都不会因 print 而崩
        except UnicodeEncodeError:
            pass
        if "reconfigure" not in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    detail = (
        f"会 print GBK 装不下的字符而没重配编码：{offenders}"
        if offenders
        else f"{len(scripts)} 个脚本入口对齐"
    )
    out("console encoding", not offenders, detail)
    if offenders:
        fails.append(f"scripts print non-GBK characters without reconfiguring stdout: {offenders}")


#: 只许在**一处**出现的那些字面量：`值 → 唯一归属文件`（`R28-13`/`R28-14` 的结构性收口）。
#: 读的是 AST 里的字符串常量，**docstring 不算**：说明性文字里写死模型名是刻意的
#: （`core/nodes.py` 那段"同一台机 qwen3-vl:8b 关掉连接后 0.30s"记的是当时那台机器上那个
#: 模型的实测），而**默认值/字典里再抄一份就是第二个事实面** —— 换默认值时它静静留在原地，
#: 症状是"改了没生效"，正是本仓这一轮抓了三次的同一族。
SINGLE_SOURCE_LITERALS = {
    "https://api.siliconflow.cn/v1": "src/rolecard_agent/config.py",
    "qwen3-vl:8b": "src/rolecard_agent/config.py",
    # 毫秒 touch 的唯一出处（`R102-62`）：从前抄在 7 处，精度依据只活在注释里 ——
    # 谁把它"顺手改简单"成 CURRENT_TIMESTAMP（秒级），侧栏同秒去歧就静默失效。
    "strftime('%Y-%m-%d %H:%M:%f', 'now')": "src/rolecard_agent/storage/threads.py",
}


def check_single_source_literals() -> None:
    """每个登记的字面量，代码里只许出现在它归属的那一个文件里。"""
    # 按文件收集"非 docstring 的字符串常量"命中的字面量。docstring 的识别方式：它是
    # `ast.Expr` 语句的值 —— 与"写在字典里的值"在 AST 上是两种位置，不是靠肉眼判的。
    #
    # **扫 `src/` 也扫 `scripts/`**（10-01，`R28-14` 的①）：从前只扫 src，于是那把管"唯一出处"
    # 的尺子正好看不见三份取证脚本各抄一遍同一个端点 —— 尺子的范围就是它的盲区。
    # 例外只有一个：**本文件自己**（它的表里必然写着那些字面量，把扫描者算进去等于永远红，
    # 与 `artifact single source` 豁免归属者与自身同一处理）。
    # **扫描根 = git 跟踪的全部文本件**（`R102-39`）：从前按目录名（src/scripts）+ `.py` 划界，
    # shell/、tests/、packaging/、.github/ 全在界外 —— 四臂变异当场证明"抄进 tests/ 三条全绿"，
    # 而真实照不见的那一份就住在 .github/workflows/ci.yml（已在它的使用现场改为 config 现读）。
    # 两类豁免，各写明理由：
    #   * 本文件自己（它的登记表里必然写着那些字面量，自指）；
    #   * **测试件**（`*.test.*` / tests 目录）：mock 载荷里的字面量是刻意的自足，
    #     让测试 import 生产常量等于让被测物替测试背书。
    tracked = subprocess.run(
        ["git", "ls-files", "*.py", "*.yml", "*.yaml", "*.ts", "*.tsx", "*.js", "*.toml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    scanned = [
        ROOT / rel
        for rel in tracked
        if rel and (ROOT / rel).exists()
    ] or [p for root in ("src", "scripts") for p in (ROOT / root).rglob("*.py")]
    self_rel = pathlib.Path(__file__).relative_to(ROOT).as_posix()
    holders: dict[str, set[str]] = {}
    for path in sorted(scanned):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        is_test = (
            rel == self_rel
            or "/tests/" in f"/{rel}"
            or rel.startswith("tests/")
            or ".test." in rel
        )
        if is_test:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits: set[str] = set()
        if path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            docstrings = {
                id(node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                ):
                    hits.update(lit for lit in SINGLE_SOURCE_LITERALS if lit in node.value)
        else:
            # 非 Python 的文本件（yml/ts/js/toml…）按原文找：它们没有 docstring 的概念，
            # 而且**注释里的出现同样算数** —— 注释也是一处"可读到的事实"。
            hits.update(lit for lit in SINGLE_SOURCE_LITERALS if lit in text)
        if hits:
            holders[rel] = hits

    offenders: list[str] = []
    for lit, owner in SINGLE_SOURCE_LITERALS.items():
        where = sorted(name for name, hits in holders.items() if lit in hits)
        extra = [name for name in where if name != owner]
        if owner not in where:
            offenders.append(f"{lit} 在归属文件 {owner} 里反而没有了（现出现在 {where}）")
        elif extra:
            offenders.append(f"{lit} 被抄进 {extra}（唯一归属应是 {owner}）")
    out(
        "single-source literals",
        not offenders,
        "; ".join(offenders[:3])
        if offenders
        else f"{len(SINGLE_SOURCE_LITERALS)} 个字面量各自只在一处（docstring 里的实测出处不计）",
    )
    if offenders:
        fails.append(f"duplicated single-source literals: {offenders}")


def check_local_service_port_single_source() -> None:
    """本机推理服务端口（11434）在 src/ 与 scripts/ 里只许出现在 config.py。

    两个主机形制（`localhost` 与 `127.0.0.1`）并存的那段时间，"默认地址"在仓库里有五个
    各写各的主人：种子、模型页提示、探针兜底、模型探测兜底、OCR 兜底 —— 归一的难点从来
    不是替换那几行，而是**下次有人抄第二遍时没人拦**。判据因此长这样：

      * 扫 `src/` 与 `scripts/`：两处都能 import 包、都有条件用常量（取证探针本来就现读
        config，它的注释自己写着端点那一族同病）；
      * 归属文件缺失同样判红：单源被删 = 机制空转，与"配置缺失"那一族同款；
      * **Electron 壳与 .bat 刻意不在范围**：跨语言拿不到 Python 常量，那是启动侧自己的
        默认（独立事实面）；散文与测试同理 —— 测试里的载荷是自足的假数据。
      * 主机形制（为什么是 127.0.0.1 不是 localhost）的理由只写在常量旁边，这里不重复第二份。
    """
    owner = "src/rolecard_agent/config.py"
    self_rel = pathlib.Path(__file__).relative_to(ROOT).as_posix().replace("\\", "/")
    offenders: list[str] = []
    owner_has = False
    for pkg in ("src", "scripts"):
        for path in sorted((ROOT / pkg).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix().replace("\\", "/")
            if rel == self_rel:
                continue  # 本文件的表里必然写着这个端口（自指，同 single-source 的豁免）
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "11434" not in text:
                continue
            if rel == owner:
                owner_has = True
            else:
                offenders.append(rel)
    if not owner_has:
        offenders.insert(0, f"{owner} 里反而没有了 11434（单源被删 = 机制空转）")
    out(
        "local service port single source",
        not offenders,
        "; ".join(offenders[:4])
        if offenders
        else f"11434 只在 {owner}（口径与理由都在常量注释里）",
    )
    if offenders:
        fails.append(f"local service port duplicated: {offenders}")


def check_dependency_layering() -> None:
    """src/ 里 import 的每一个第三方发行版，必须在六份 requirements 之一直接声明。

    为什么单独立这条（2026-09-28 轮 R28-11）：那天实锤的是"层漏了"——Dockerfile/CI 没装
    `requirements-cloud.txt`，症状是配任何 OpenAI 兼容端点保存即 500，而本机 .venv 恰好装过
    所以门禁看不见。这条检查防的是同族的另一半：**import 了但哪层都没声明** ——
    `httpx`（4 处顶层 import）与 `typing_extensions`（core/state.py）当时全靠
    langchain-core / pydantic 的传递依赖兜住；传递兜住时不报错，某天上游收窄约束
    就静默断（agent 取证时实测过 langchain-core 1.6.3 的 Requires-Dist 确实带着 httpx）。

    判据：AST 扫 `src/**/*.py` 的全部 import（含函数内的 lazy import —— 那条路径被触发
    同样 500），顶层模块名去 stdlib、去第一方后，归一化（下划线→连字符）后必须在
    **运行层**的 `requirements*.txt` 里声明 —— dev 层（含 PyInstaller；它从前独占
    `requirements-package.txt`，2026-10-07 锁文件落地时并进 dev）不进随包运行树，
    生产 import 靠它们兜等于没兜（httpx 当时正是"只有 dev 声明 + langchain-core 传递"
    的双侥幸）。
    声明侧不读 pyproject：依赖 parity 那条已保证 pyproject 与 requirements 一致，这里
    只对一份事实面。import 名 ≠ 发行版名的（如 `import tavily` ← `tavily-python`）走
    显式别名表 —— 新映射缺了就红，把表补上即可，别名表本身就是"模块↔发行版"的登记处。
    """
    # import 名 → 发行版名 的已知差异。命中别名后仍按发行版名去声明集里找。
    import_dist_aliases = {"tavily": "tavily-python"}

    declared: set[str] = set()
    # 只有**运行层**能给 src 的 import 背书：dev 层（连带 PyInstaller）不在随包运行树里。
    non_runtime = {"requirements-dev.txt"}
    for req in sorted(ROOT.glob("requirements*.txt")):
        if req.name in non_runtime:
            continue
        for line in req.read_text(encoding="utf-8").splitlines():
            name = line.split("#", 1)[0].strip()
            if not name:
                continue
            # 去掉 extras / 版本约束 / 环境标记 / 续行残片，只留发行版名
            name = re.split(r"[<>=!;\[\s]", name, maxsplit=1)[0].strip().lower().replace("_", "-")
            if name:
                declared.add(name)

    first_seen: dict[str, str] = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    first_seen.setdefault(alias.name.split(".")[0], rel)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                first_seen.setdefault(node.module.split(".")[0], rel)

    stdlib: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())
    undeclared = sorted(
        mod
        for mod in first_seen
        if mod not in stdlib
        and mod != "rolecard_agent"
        and import_dist_aliases.get(mod, mod).lower().replace("_", "-") not in declared
    )
    out(
        "dependency layering",
        not undeclared,
        f"{len(first_seen)} 个第三方/第一方顶层模块，{len(undeclared)} 个未声明",
    )
    if undeclared:
        fails.append(
            "src imports not declared in any requirements*.txt: "
            + ", ".join(f"{m} ({first_seen[m]})" for m in undeclared[:8])
        )


def check_bundled_copy() -> None:
    """已构建的 `frontend/dist` 里不许出现写死的女性称谓（09-28 轮 `R28-44` 的事后闸）。

    为什么这条查产物而不是查源码：**压缩后的 JS 里没有注释** —— 源码里那些「她在打字」
    多半是给我们自己看的说明，只有产物里出现的字才是用户真看得见的。所以这个信号是精确的，
    不需要一台 JSX AST 解析器去猜"这行是不是注释"。
    它今天就有价值：B 类改完之后我自己在 `ChatToolbar` 漏了一句「会打开她自己的那条对话」，
    是**浏览器里看一眼**才发现的（grep 的样式没覆盖"她自己"）。这条把"看一眼"变成尺子。
    新的合法用法（比如某个角色的名字里带"她"）出现时，把它加进 `_BUNDLED_COPY_ALLOWED` 并写理由。
    """
    assets = sorted((ROOT / "frontend" / "dist" / "assets").glob("*.js"))
    if not assets:
        out("bundled copy", True, "还没有构建产物，跳过（不是负面）")
        return
    hits: list[str] = []
    for path in assets:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in _BUNDLED_GENDERED:
            if token in _BUNDLED_COPY_ALLOWED:
                continue
            at = text.find(token)
            if at >= 0:
                snippet = text[max(0, at - 24) : at + 24].replace("\n", " ")
                hits.append(f"{path.name}: …{snippet}…")
    out(
        "bundled copy",
        not hits,
        "; ".join(hits[:3]) if hits else f"{len(assets)} 个产物 chunk 里没有写死的女性称谓",
    )
    if hits:
        fails.append(f"gendered copy shipped in frontend/dist: {hits[:4]}")


_BUNDLED_GENDERED = ("她", "她们")
#: 产物里允许出现的女性称谓（键 = 那串字，值 = 为什么允许）。目前是空的。
_BUNDLED_COPY_ALLOWED: frozenset[str] = frozenset()


def check_vocabulary() -> None:
    """界面上的"后端/服务"不再一词三层（红：确证的混指；warn：裸「后端」的残余计数）。"""
    hits: list[str] = []
    soft = 0

    assets = sorted((ROOT / "frontend" / "dist" / "assets").glob("*.js"))
    for path in assets:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in _BANNED_USER_VISIBLE:
            at = text.find(token)
            if at >= 0:
                snippet = text[max(0, at - 26) : at + 26].replace("\n", " ")
                hits.append(f"{path.name}: …{snippet}…")
            soft += text.count("后端")

    for path in sorted((ROOT / "src").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        docs = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docs:
                continue  # docstring 是给开发者看的，那里「后端」仍然精确
            for token in _BANNED_USER_VISIBLE:
                if token in node.value:
                    hits.append(f"{rel}:{node.lineno} …{node.value[:34]}…")
            soft += node.value.count("后端")

    out(
        "ui vocabulary",
        not hits,
        "; ".join(hits[:3])
        if hits
        else (
            f"产物 {len(assets)} 个 chunk + src 的字符串常量里都没有"
            f" {len(_BANNED_USER_VISIBLE)} 个混指词（裸「后端」还剩 {soft} 处，只数不拦）"
        ),
    )
    if hits:
        fails.append(f"mixed-layer wording shipped to users: {hits[:4]}")


#: 公网部署面上"缺了就起不来"或"错了会静默降级"的那几条护栏：compose 里一条都不能少。
_DEPLOY_REQUIRED_ENV = ("AUTH_MODE", "AUTH_CREDENTIALS", "AUTH_TRUSTED_PROXIES")


def _compose_app_env(compose: str) -> list[str]:
    """取 `services.app.environment` 那一段里的键名（纯文本解析，不引 yaml 依赖）。

    为什么不扫全文：compose 的 `environment:` 每个 service 都有一段，而"这个旋钮应用读不读"
    只对 `app` 那一段成立 —— 扫全文会把 Caddy 的 `ROLECARD_DOMAIN` 当成空转旋钮。
    """
    block = re.search(r"^  app:\n(.*?)(?=^  [a-z_]+:\n|^[a-z])", compose, flags=re.M | re.S)
    if not block:
        return []
    env = re.search(r"^    environment:\n((?:      .+\n?|\s*\n)*)", block.group(1), flags=re.M)
    if not env:
        return []
    return re.findall(r"^      ([A-Z][A-Z0-9_]+):", env.group(1), flags=re.M)


def check_deploy_env_parity() -> None:
    """compose / Caddyfile 上的旋钮，必须都是应用真读的那些（v2.4 收官那档的部署面）。

    为什么立这条：compose 是最容易写着**没人读的环境变量**的地方，而写错的后果不是红，是
    "配了没用" —— `AUTH_TRUSTED_PROXY` 少一个 S 就是限流整个塌成 Caddy 那一个桶，服务照样
    200。第一趟就照出一个真的：compose 里原本写着 `DEEPSEEK_API_KEY`，而全仓只有
    `SILICONFLOW_API_KEY` 有"启动时自动注册"那半条路（`run_api.py`），另一条是空转的旋钮。
    """
    compose_path = ROOT / "docker-compose.yml"
    if not compose_path.exists():
        out("deploy env parity", False, "docker-compose.yml 不见了（公网那一档的部署面就是它）")
        fails.append("docker-compose.yml missing")
        return
    compose = compose_path.read_text(encoding="utf-8", errors="ignore")
    cfg_text = (ROOT / "src" / "rolecard_agent" / "config.py").read_text(
        encoding="utf-8", errors="ignore"
    )
    env_text = (ROOT / ".env.example").read_text(encoding="utf-8", errors="ignore")
    # 已知旋钮 = config 的 env 映射表 ∪ `.env.example` 里已经解释过的那族（键名对齐由
    # `config contract` 那条管，这里不另起一套口径）。
    known = set(re.findall(r'\("([A-Z][A-Z0-9_]+)",\s*"[a-z_0-9]+"\)', cfg_text))
    known |= set(re.findall(r"^([A-Z][A-Z0-9_]+)=.*$", env_text, flags=re.M))

    problems: list[str] = []
    # **只看 app 那一段的 environment**。第一版扫全文，于是把 caddy 的 `ROLECARD_DOMAIN`
    # 也算成"应用不读的旋钮"报了红 —— 那不是它该管的：别的 service 的环境变量是给
    # Caddy 读的，本来就不在这张映射表里。
    keys = _compose_app_env(compose)
    unknown = sorted(k for k in keys if k not in known)
    if unknown:
        problems.append(f"compose 写了应用不读的旋钮 {unknown}")
    missing = [k for k in _DEPLOY_REQUIRED_ENV if f"{k}:" not in compose]
    if missing:
        problems.append(f"护栏缺条 {missing}")
    mode = re.search(r'^      AUTH_MODE:\s*"?([A-Za-z]+)"?', compose, flags=re.M)
    if mode and mode.group(1) != "on":
        problems.append(f"AUTH_MODE={mode.group(1)}：反代之后 auto 把所有人都当回环，等于没鉴权")
    if re.search(r'^\s*-\s*"?8000:\d+', compose, flags=re.M):
        problems.append("应用端口被 publish 到宿主（这一档只许 443 出公网）")

    caddy = ROOT / "deploy" / "Caddyfile"
    exposed = set(re.findall(r'^\s*-\s*"?(\d{2,5})"?\s*$', compose, flags=re.M))
    if caddy.exists() and exposed:
        upstream = set(
            re.findall(r"reverse_proxy\s+app:(\d+)", caddy.read_text(encoding="utf-8"))
        )
        if upstream and not upstream <= exposed:
            problems.append(
                f"Caddyfile 打到 app:{sorted(upstream)}，compose expose 的是 {sorted(exposed)}"
            )

    readme = (ROOT / "README.md").read_text(encoding="utf-8", errors="ignore")
    required_vars = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+):\?", compose))
    must_teach = sorted(v for v in required_vars if v not in readme)
    if must_teach:
        problems.append(f"这几个必填变量 README 没教 {must_teach}")

    # —— 10-01 加的四问：这条断言从前**只问键名存不存在**，而"配了没用"这一族缺陷全都住在值里。
    # (a) 口令不许写死在 compose 里。旧写法只查 `AUTH_CREDENTIALS:` 这个子串在不在，
    #     于是把明文口令直接写进这份要进 git 的文件照样绿 —— 那是凭据入库，不是配置。
    cred_line = re.search(r"^      AUTH_CREDENTIALS:\s*(.+?)\s*$", compose, flags=re.M)
    if cred_line and not cred_line.group(1).startswith("${"):
        problems.append(
            f"AUTH_CREDENTIALS 写的是字面量（{cred_line.group(1)[:16]}…）—— "
            "compose 进 git，口令不许住在里面，必须是 ${ROLECARD_CREDENTIALS:?…} 这种形状"
        )
    # (b) 豁免路径与镜像的存活探针必须指同一条：改了 compose 这一格而 Dockerfile 的
    #     HEALTHCHECK 还打在 /api/health，症状是容器**永远 unhealthy**，
    #     而应用本身好着 —— 编排器看到的是"活着但没就绪"，比崩更难查。
    health_target = ""
    dockerfile = ROOT / "Dockerfile"
    if dockerfile.exists():
        # 那条探针是 `u.urlopen('http://127.0.0.1:'+os.environ.get(...)+'/api/health',timeout=4)`，
        # 里面**有括号**，所以不能按"从一个引号跨到另一个引号"的正则去抓 —— 第一版那么写，
        # 读不出来就静默跳过，于是 D-b 那发变异（把豁免路径改掉）当场绿：尺子瞎了却报告正常。
        # 改成把 HEALTHCHECK 那条 CMD 里**所有**引号串挑出来，取以 `/` 开头的那一个。
        probe_line = ""
        for line in dockerfile.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "urlopen" in line:
                probe_line = line
                break
        # 单引号里那条**以 / 开头**的串就是探针路径（外层双引号整段是 python -c 的码，
        # 用它只会读到"import os,urllib…"这一大坨 —— 第一版就栽在这里，读不出来于是静默跳过）。
        paths = re.findall(r"'(/[A-Za-z0-9_/.\-]+)'", probe_line)
        health_target = paths[-1] if paths else ""
    exempt = re.search(r'^      AUTH_EXEMPT_PATHS:\s*"?([^"\n]+)"?', compose, flags=re.M)
    if not health_target:
        problems.append("Dockerfile 里读不出 HEALTHCHECK 打的是哪条路径 —— 这一问不能静默跳过")
    elif exempt and health_target not in exempt.group(1):
        problems.append(
            f"Dockerfile 的 HEALTHCHECK 打 {health_target}，而 compose 只豁免 {exempt.group(1)}"
            " —— 那一格一改，容器就永远报 unhealthy 而应用其实好着"
        )
    # (c) Caddyfile 的 `{$NAME}` 占位符必须由 compose 喂给 caddy 那个服务：
    #     键名检查从前**只扫 app 段**，caddy 段整段在范围外，改名漂移没人问。
    caddy_file = ROOT / "deploy" / "Caddyfile"
    caddy_block = re.search(r"^  caddy:.*?(?=^  \w|\Z)", compose, flags=re.M | re.S)
    if caddy_file.exists() and caddy_block:
        caddy_lines = [
            line
            for line in caddy_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            if not line.lstrip().startswith("#")  # 注释里的 `{$VAR}` 是解释，不是真的插值
        ]
        wanted = set(re.findall(r"\{\$([A-Z][A-Z0-9_]+)", "\n".join(caddy_lines)))
        given = set(re.findall(r"^      ([A-Z][A-Z0-9_]+):", caddy_block.group(0), flags=re.M))
        unbound = sorted(wanted - given)
        if wanted and unbound:
            problems.append(f"Caddyfile 用了 ${{{unbound}}}，compose 的 caddy 段没喂这些值")
    # (d) 同一个旋钮在两处有默认值时必须**说出为什么不同**（限流：镜像 30 / 自用 0 是两档形态
    #     的真实差别，不是漂移 —— 但"不是漂移"这件事得写在行上，否则下一次没人分得清）。
    #     example 里空值 = "默认不设"，那不是"另一个默认值"，不参与比对。
    example_text = (ROOT / ".env.example").read_text(encoding="utf-8", errors="ignore")
    example_defaults = {
        key: value
        for key, value in re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", example_text, flags=re.M)
        if value.strip()
    }
    compose_lines = compose.splitlines()
    for position, line in enumerate(compose_lines):
        match = re.search(r"([A-Z][A-Z0-9_]+):\s*\$\{[A-Z0-9_]+:-([^}]*)\}", line)
        if not match:
            continue
        key, compose_default = match.group(1), match.group(2)
        if key not in example_defaults or example_defaults[key] == compose_default:
            continue
        context = "\n".join(compose_lines[max(0, position - 2) : position + 1])
        # 要求的是"这行注释**指向另一处**"，不是"出现某个词" —— 钉一个中文词等于把判据写在措辞上，
        # 换一种说法就假红。指向 `.env.example` 才是"说出它与谁不同"这件事的最小充分形式。
        explained = "#" in context and ".env.example" in context
        if not explained:
            problems.append(
                f"{key} 在 compose 默认 {compose_default}、example 默认 {example_defaults[key]}，"
                "而那一行没写「这两档为什么不同」—— 默认值不一致要么是设计要么是漂移，让它自己说"
            )

    out(
        "deploy env parity",
        not problems,
        "; ".join(problems)
        if problems
        else (
            f"compose 的 {len(keys)} 个旋钮都在 config/example 里"
            f"；护栏 {len(_DEPLOY_REQUIRED_ENV)} 条在场"
            "；AUTH_MODE=on、应用端口不 publish、README 教齐了必填变量"
            "；口令不是字面量、豁免路径与 HEALTHCHECK 同条、"
            "Caddyfile 的占位符有人喂、两处默认值不同那行指向了对方"
        ),
    )
    if problems:
        fails.append(f"deploy surface drift: {problems}")


def check_role_whitelists_resolve() -> None:
    """Every tool name in a built-in role's whitelist must resolve to a declared tool.

    The built-in role's whitelist listed `list_domains` / `list_roles` while
    `core/tools/builtin.py` was still an empty docstring - a permission list pointing at
    nothing, which reads as working code (技术评审与决策.md §9 B1).

    ## 声明面只认 `@tool("...")`

    此前这里还加了两条"宽松兜底"：`def (\\w+)\\(` 和 `"([a-z][a-z0-9_]{2,})"`。后者的意思是
    "文件里出现过的任何小写字符串字面量都算已声明工具" —— 于是这个断言几乎恒真，
    白名单写错名字也照样绿。**一个永远不会失败的检查最危险的地方在于它给出的是假信心**
    （代码审查报告（第二轮）L4）。工具名在代码里只有一个权威声明处：`@tool("name")`。
    """
    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.domains.registry import domain_seed_roles  # noqa: PLC0415
    from rolecard_agent.roles.seed import BUILTIN_ROLES  # noqa: PLC0415

    self_rel = pathlib.Path(__file__).relative_to(ROOT).as_posix()
    declared: set[str] = set()
    for path in iter_files(".py"):
        if "tests" in path.parts:
            continue
        if path.relative_to(ROOT).as_posix() == self_rel:
            continue  # 本文件 docstring 里的 `@tool("name")` 是讲解，不是声明（`R102-37` 幻影）
        declared |= set(
            re.findall(r'@tool\("(\w+)"\)', path.read_text(encoding="utf-8", errors="ignore"))
        )

    # 域种子角色（各域 `DomainSpec.seed_roles` 聚合，2026-10-04 起住在域自己包里）与
    # 内置角色同样随代码出厂：白名单写错名字要在这里大声失败，反向覆盖（声明了却没人
    # 引用）也要把它们的引用算进去。
    wanted: set[str] = set()
    for role in (*BUILTIN_ROLES, *domain_seed_roles()):
        wanted |= set(role.tool_whitelist or [])

    missing = sorted(wanted - declared)
    detail = (
        f"unresolved: {missing} (declared: {sorted(declared)})"
        if missing
        else f"{len(wanted)} names resolve against {len(declared)} declared tools"
    )
    out("role whitelists", not missing, detail)
    if missing:
        fails.append(f"built-in role whitelists name undeclared tools: {missing}")
    # 反向覆盖（`R102-37` 订正：原来注释许诺"死工具也大声失败"，代码只在 wanted 整体为空
    # 时才红 —— 那是一支**从不输出的空转臂**。真反向判（declared - wanted 非空即红）会先
    # 照到 `run_command`：它是审批门的工具面，出厂白名单**刻意**不含它（批准语义不属于
    # 开箱即用的对话轮）。在那次拍板之前，反向差集落 **warn**：屏幕上看得见，不假装通过。
    unused = sorted(declared - wanted)
    if declared and not wanted:
        fails.append("tools are declared but no built-in role references any of them")
        out("role whitelist coverage", False, "no whitelist references any declared tool")
    elif unused:
        warns.append(
            f"role whitelist coverage: 出厂白名单没人引用的工具 {unused}"
            " —— 是死工具还是刻意只走审批门，需要一次拍板（R102-37）"
        )


def _normalise_question(text: str) -> str:
    """问题归一：去掉标点与空白 —— 「…是多少？」与「…是多少」是同一道题。"""
    return re.sub(r"[\s，。！？?!,.:：\"'（）()【】\[\]]", "", text)


def check_exemplar_leaks_eval_answers() -> None:
    """内置角色的**范例不能是评测题的答案**。

    ## 为什么需要一条机器校验

    角色范例（few-shot）会原样进入 system prompt。`medical_archivist` 的第一条范例曾写成
    「上次检查的结石直径是多少？→ …6.0 mm…【未经人工校验】」，与评测用例 health-001 几乎
    逐字相同（连问号都只差一个）。后果在评测记录里看得清清楚楚：模型的回答与范例**逐字一致**，
    一次工具都没调 —— 数值对、标记对、**过程不达标**。而且这种失败极难排查：断言的三项里
    两项都"通过"了，只有"必须调工具"这一项失败，看起来像模型抽风。

    规则：范例的提问与任何评测用例的提问**归一化后不得相同**。只查提问而不查回答，
    是因为回答重叠无法静态判定（同一段医疗话术出现在两边是正常的）；
    提问重叠才是"把答案递给模型"的可判定信号。
    """
    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.domains.registry import domain_seed_roles  # noqa: PLC0415
    from rolecard_agent.roles.seed import BUILTIN_ROLES  # noqa: PLC0415

    case_path = ROOT / "tests" / "eval" / "cases" / "health.json"
    if not case_path.exists():
        out("exemplar leaks eval answers", True, "no eval cases yet")
        return
    cases = json.loads(case_path.read_text(encoding="utf-8"))
    eval_inputs = {_normalise_question(str(c.get("input") or "")) for c in cases if c.get("input")}

    # 域种子角色一并查：出过事故的那张卡（`medical_archivist`）恰恰是域角色 —— 从前这里
    # 只遍历 BUILTIN_ROLES，于是"范例不能是评测答案"这条规则对**真正高危的那张卡**是空转
    # （2026-10-04 域机制收口时补上：`DomainSpec.seed_roles` 聚合后与内置角色同一视界）。
    leaked: list[str] = []
    for role in (*BUILTIN_ROLES, *domain_seed_roles()):
        for ex in role.exemplars or []:
            q = _normalise_question(ex.user)
            if q and q in eval_inputs:
                leaked.append(f"{role.role_id}:{ex.user}")

    ok = not leaked
    out("exemplar leaks eval answers", ok, str(leaked) if leaked else "clean")
    if not ok:
        fails.append(
            "built-in exemplar duplicates an eval question - the model can pass by "
            f"parroting the prompt instead of calling tools: {leaked}"
        )
