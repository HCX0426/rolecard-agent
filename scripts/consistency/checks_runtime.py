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
from .checks_config import config_env_contract
from .core import ROOT, fails, iter_files, out, simple_yaml, warns


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
        '"rolecard-backend.exe"': '用 f"{artifacts.BACKEND_NAME}.exe"',
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
                f"electron-builder 的 extraResources to={to_line.group(1)}"
                f" ≠ {artifacts.BACKEND_NAME}"
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
        problems.append(f"ps1 里「刚构建那份」= {got_backend} 与出处不同形（应为 {want_backend}）")
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
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "data_paths"),
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
    # P2 错误签名的落盘目录（base/error_signatures.py）：目录名同样以常量存在，
    # 尺子按同款形状数到它 —— 运行时往数据根写的每一个目录都必须被 gitignore 管住。
    es_py = ROOT / "src" / "rolecard_agent" / "base" / "error_signatures.py"
    for node in ast.walk(ast.parse(es_py.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "ERROR_DIRNAME" not in names:
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            dirs[value.value] = "base/error_signatures.py::ERROR_DIRNAME"
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
        e = data[6 + 16 * i : 22 + 16 * i]
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
            "app icon is not a clean multi-frame RGBA set: "
            + "；".join(problems)
            + " —— 重跑 .venv-ocr\\Scripts\\python.exe scripts/tools/make_app_icon.py"
        )


def check_safety_prompt() -> None:
    """The global safety rules must be *defined in code*, not just described in prose.

    They used to exist only in the archived design doc, while `需求与验收标准.md` US-4
    treated them as a shipped requirement. Safety-critical code is deliberately not
    delegated (CONTRIBUTING section 1).
    """
    path = ROOT / "src" / "rolecard_agent" / "core" / "agent" / "prompts.py"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    has_const = bool(re.search(r"^GLOBAL_SAFETY_PROMPT\s*=", text, flags=re.M))
    has_builder = "def build_system_prompt" in text
    has_rules = "禁止输出任何疾病诊断" in text
    ok = has_const and has_builder and has_rules
    out("safety prompt", ok, f"const={has_const} builder={has_builder} rules={has_rules}")
    if not ok:
        fails.append("GLOBAL_SAFETY_PROMPT is not concretely defined in core/agent/prompts.py")


def check_milestone_alignment() -> None:
    """里程碑号在 README 与 `架构总览` 两处必须一致。

    **为什么是对账两份活文档而不是"计划 + README"**（2026-11-01 改）：从前这把尺子读
    `docs/archive/审计台账汇编.md`（历史台账）当"计划"——于是**归档件成了尺子的数据源**，
    删不掉也搬不动。删掉归档的那一刻这把尺子就崩（实测：脚本当场 FileNotFoundError）。
    现在的事实源是两份**活文档**：README 的里程碑表（对外口径）与 `架构总览` §4
    （里程碑状态，内部口径）—— 两者都是"现状"，本就该互相印证；历史台账该只做历史。

    判据形状：README 的表格行 `| **M3** |` 与架构总览里的**基座号段声明** `M1–M5`。
    文档里另有两套同形编号（§4.1 那条 A–D 阶段的 M1–M8、§3 里那条 M1~M6），
    混进来会让这把尺子永远红 —— 所以只认"基座（M…–M…）"这一句。
    两边必须同形，且**不许为空**（空 = 两边都把里程碑表删了 = 这把尺子失效）。
    """
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    overview_text = (ROOT / "docs" / "架构总览.md").read_text(encoding="utf-8")
    readme_ids = set(re.findall(r"\|\s*\*\*M(\d)\*\*", readme_text))
    overview_ids: set[str] = set()
    for lo, hi in re.findall(r"基座（M(\d)\s*[–\-~]\s*M(\d)", overview_text):
        overview_ids.update(str(n) for n in range(int(lo), int(hi) + 1))
    ok = readme_ids == overview_ids and bool(readme_ids)
    detail = f"README={sorted(readme_ids)} 总览基座={sorted(overview_ids)}"
    out("milestone alignment", ok, detail)
    if not ok:
        fails.append(f"milestone ids differ between README and 架构总览: {detail}")


def check_v1_v2_boundary() -> None:
    """M4 delivers the HTTP API and the single-page UI *inside v1*; M5 delivers the
    engineering-grade frontend inside v1 too (pulled forward from v2.3).

    So no live document may still advertise them as a v2 roadmap item. This rule exists
    because pulling scope forward left exactly such a leftover behind twice.
    """
    scanned = ("README.md", "docs/需求与验收标准.md")
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
#: （`core/agent/nodes.py` 那段"同一台机 qwen3-vl:8b 关掉连接后 0.30s"记的是当时那台机器上那个
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
    # `-c core.quotepath=false` 是**这条尺子在 Linux 上的形状修正**（与 `gate.py` 的 `_git()`、
    # `scan_secrets.py` 的 `-z` 同一根因，2026-10-09 CI 现场量出）：CI 的 `core.quotepath`
    # 默认 **true**，git 会把非 ASCII 路径输出成带双引号的八进制转义 ⇒ 这一族文件"看着在
    # tracked 里、其实 `(ROOT / rel).exists()` 永远假"，被 `if ... .exists()` 静默丢掉。
    # 今天恰好为 0 命中（本仓非 ASCII 路径都是 `.md`，不在这些扩展名里）—— 那是巧合，
    # 不是安全：任何一份中文命名的 `.py`/`.toml` 一进来，这条判据就会静默看不见它。
    tracked = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "ls-files",
            "*.py",
            "*.yml",
            "*.yaml",
            "*.ts",
            "*.tsx",
            "*.js",
            "*.toml",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout.splitlines()
    scanned = [ROOT / rel for rel in tracked if rel and (ROOT / rel).exists()] or [
        p for root in ("src", "scripts") for p in (ROOT / root).rglob("*.py")
    ]
    self_rel = pathlib.Path(__file__).relative_to(ROOT).as_posix()
    holders: dict[str, set[str]] = {}
    for path in sorted(scanned):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        is_test = (
            rel == self_rel or "/tests/" in f"/{rel}" or rel.startswith("tests/") or ".test." in rel
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


def check_appearance_single_source() -> None:
    """外观默认值与「JS 往 <html> 上写哪几条内联」只许一处真值。

    为什么需要这把尺子（2026-11-03 归一化那轮的现形）：透明度在 JS 里算成**三档**
    （bg / glass / glassSoft），`applyAlpha` 把三条都写成 `<html>` 的内联样式；内联样式
    压过 `.dark` 选择器，于是 index.css 里给深色准备的那组档位（92% / 97%）**从写下来
    那一天起一次都没生效过**。同族第二处：CSS 写 `--bg-dim: 0.35`、JS 兜底写 0.25，而
    JS 总把兜底值写进内联 —— 用户历来看到的都是 0.25，那行 0.35 也是死码。两处都只有
    真浏览器实测才看得出，tsc / eslint / vitest 全都看不见（vitest 里 `.css` 导入被桩成
    空串，在那儿比字面值会写成一条**永远通过**的断言 —— 比没有检查更坏）。

    判据五条，任一取不到输入即判红（"扫不到"不等于"通过"）：

      1. JS 常量与 CSS 字面值逐一对齐：`BG_DIM_DEFAULT` ↔ `--bg-dim`、
         `BG_BLUR_DEFAULT` ↔ `--bg-blur`、`SOLID_DEFAULT` ↔ `--bg-solid`
         （后者由 appearance.ts 里 `ALPHA_MAX - ALPHA_DEFAULT` 现算，不抄字面量）；
      2. JS 与首帧脚本**都不许**再写 `--bg-alpha` / `--glass-alpha` / `--glass-alpha-soft`
         这三条内联 —— 层次是 CSS 的事，JS 一写就把主题档盖死（本条正是那次的回归锁）；
      3. `.dark` 块必须调 `--glass-offset`（否则"深色档位"这个说法又变成死码）；
      4. 首帧脚本与 appearance.ts 各自**只做一条** `--bg-solid` 赋值（多一条就回到三真值）；
      5. 首帧的 `--accent` 兜底色必须等于**默认角色**在 `accent.ts` 里的那档
         （2026-11-04 补：跨 CSS/TS 边界互 import 不了，只能钉；详见函数内第 5 段）。


    刻意不在范围：`frontend/dist`（构建产物，源头红它必红）；测试件（自足的假数据）。
    """
    ts_rel = "frontend/src/lib/appearance.ts"
    css_rel = "frontend/src/index.css"
    html_rel = "frontend/index.html"
    accent_rel = "frontend/src/lib/accent.ts"
    offenders: list[str] = []

    def _read(rel: str) -> str | None:
        p = ROOT / rel
        if not p.exists():
            offenders.append(f"{rel} 不存在（尺子的输入丢了）")
            return None
        return p.read_text(encoding="utf-8", errors="ignore")

    ts = _read(ts_rel)
    css = _read(css_rel)
    html = _read(html_rel)
    accent = _read(accent_rel)
    if ts is None or css is None or html is None or accent is None:
        out("appearance single source", False, "; ".join(offenders))
        fails.append(f"appearance single source inputs missing: {offenders}")
        return

    def _js_num(name: str) -> float | None:
        m = re.search(rf"export const {name} = ([\d.]+);", ts or "")
        if m:
            return float(m.group(1))
        # SOLID_DEFAULT 不写死：它是 `ALPHA_MAX - ALPHA_DEFAULT` 的表达式，现算
        if name == "SOLID_DEFAULT":
            hi = re.search(r"export const ALPHA_MAX = ([\d.]+);", ts or "")
            lo = re.search(r"export const ALPHA_DEFAULT = ([\d.]+);", ts or "")
            if hi and lo:
                return float(hi.group(1)) - float(lo.group(1))
        offenders.append(f"appearance.ts 里找不到 {name} 的数字字面值")
        return None

    def _css_num(var_name: str, block: str) -> str | None:
        """取某个块（`:root` / `.dark`）里 `--x: 值;` 的值。按括号配对切块，不靠行数。"""
        m = re.search(rf"{re.escape(block)}\s*\{{(?P<body>.*?)\n\}}", css or "", re.S)
        if not m:
            offenders.append(f"index.css 里找不到 {block} 块")
            return None
        one = re.search(rf"{re.escape(var_name)}:\s*([^;]+);", m.group("body"))
        if not one:
            offenders.append(f"index.css 的 {block} 里没有 {var_name}")
            return None
        return one.group(1).strip()

    # 1) JS 常量 ↔ CSS 字面值
    for js_name, css_var, unit in (
        ("BG_DIM_DEFAULT", "--bg-dim", ""),
        ("BG_BLUR_DEFAULT", "--bg-blur", "px"),
        ("SOLID_DEFAULT", "--bg-solid", ""),
    ):
        jv = _js_num(js_name)
        cv = _css_num(css_var, ":root")
        if jv is None or cv is None:
            continue
        cv_num = cv.rstrip(unit).strip() if unit else cv
        try:
            if float(cv_num) != jv:
                offenders.append(f"{js_name}={jv} 与 index.css {css_var}={cv} 不一致")
        except ValueError:
            offenders.append(f"index.css 的 {css_var}={cv} 不是个数（尺子读不动 = 判红，不算通过）")

    # 2) 不许再由 JS 写这三条内联（层次归 CSS）
    banned = ("--bg-alpha", "--glass-alpha", "--glass-alpha-soft")
    for rel, text in ((ts_rel, ts), (html_rel, html)):
        hit = [v for v in banned if f'setProperty("{v}"' in text or f"'{v}'" in text]
        if hit:
            offenders.append(f"{rel} 又把这些写成了内联样式（会盖死 .dark）：{hit}")

    # 3) 深色档位必须真有人读
    m_dark = re.search(r"\.dark\s*\{(?P<body>.*?)\n\}", css, re.S)
    dark_body = m_dark.group("body") if m_dark else ""
    if "--glass-offset" not in dark_body:
        offenders.append("index.css 的 .dark 里没有 --glass-offset（深色档位又会是死码）")

    # 4) --bg-solid 两侧各只赋值一次
    for rel, text, pat in (
        (ts_rel, ts, r'setProperty\("--bg-solid"'),
        (html_rel, html, r'setProperty\("--bg-solid"'),
    ):
        n = len(re.findall(pat, text))
        if n != 1:
            offenders.append(f"{rel} 里 --bg-solid 赋值出现 {n} 次（应为 1）")

    # 5) 首帧的 --accent 兜底色必须等于**默认角色**的 accent（2026-11-04 归一）
    #    `--accent` 有两处独立声明：`index.css :root`（首帧、JS 挂上之前的兜底）与
    #    `accent.ts` 的 `BUILTIN_ACCENTS`（React 起来后按当前角色注入）。首帧脚本**不写**
    #    --accent（实测 html 里零命中），所以首帧那一格完全靠 CSS 兜底 ——
    #    而它兜的该是**默认角色那一档**：无角色信息时用户第一眼看到的就是默认角色的温度。
    #    两边不等 ⇒ 首帧一个色、挂载翻成另一个色（闪变，像素可见，正是本轮在治的漂移）。
    #    跨 CSS/TS 边界互相 import 不了（Vite 不给 CSS 做 JS 插值），属"只能钉"档 ——
    #    与 `.sql` 里那个引用不了 Python 常量的 `DEFAULT 'local-user'` 同一族。
    #    要查的那个 role_id 不硬编码在尺子里：import 后端 `DEFAULT_ROLE_ID` 现取，
    #    "默认角色是谁"仍由 `roles/seed.py` 单点声明，这里只是消费。
    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.roles.seed import DEFAULT_ROLE_ID  # noqa: PLC0415

    css_accent = _css_num("--accent", ":root")
    m_default = re.search(rf'^\s*{re.escape(DEFAULT_ROLE_ID)}:\s*"([^"]+)"', accent, re.M)
    if css_accent is None:
        pass  # 输入读不出已由 _css_num 记进 offenders
    elif m_default is None:
        offenders.append(
            f"accent.ts 的 BUILTIN_ACCENTS 里找不到默认角色 {DEFAULT_ROLE_ID!r} 那条"
            "（首帧兜底无从比对 = 判红，不算通过）"
        )
    else:
        css_hex, ts_hex = css_accent.strip().lower(), m_default.group(1).strip().lower()

        def _norm_hex(h: str) -> str:
            # 只比 3/6 位 hex，且按 `expandHex` 的口径把 #abc 展成 #aabbcc 再比
            # （否则 `#b0a` 与 `#bb00aa` 这两个同一个色会被判成不一致 = 假红）。
            return "#" + "".join(c * 2 for c in h[1:]) if len(h) == 4 else h

        if _norm_hex(css_hex) != _norm_hex(ts_hex):
            offenders.append(
                f"首帧 --accent({css_hex}) 与 accent.ts 默认角色 "
                f"{DEFAULT_ROLE_ID}({ts_hex}) 不一致 ⇒ 挂载时角色色会闪变"
            )

    out(
        "appearance single source",
        not offenders,
        "; ".join(offenders[:5])
        if offenders
        else "JS 兜底值与 index.css 字面值对齐；透明度只写一条内联；.dark 的档位有人读；"
        "首帧 --accent = 默认角色色",
    )

    if offenders:
        fails.append(f"appearance drift: {offenders}")


def check_pet_geometry_single_source() -> None:
    """桌宠几何常数跨 shell/frontend 只许一份：前端 @shell 别名 import 壳的唯一出处。

    （A-9，2026-11-04 归一化）：`PetPage.tsx` 的 `HIT_SLACK_X` 从前**手抄** `132`，
    注释自认"数值抄自 shell/main/windows.ts，两处各有注释钉着对方" —— 壳改色片尺寸
    时前端照旧用旧数，正是"改了没生效"那族的病根（2026-10-29 栽过一次）。收成 import
    后壳推导前端自动跟，但**改回手抄不会有任何编译错误**，所以立这把尺子。
    判据四个接点，任一取不到输入即判红（"扫不到"不等于"通过"，与 appearance 同族）：

      1. 壳侧唯一出处还在：`petDockGeometry.ts` 导出 `LEGACY_SIDE_TUCK`；
      2. 前端经 `@shell/petDockGeometry` import 它，且 `HIT_SLACK_X` 的赋值行**逐字**
         等于 `LEGACY_SIDE_TUCK`（换成任何数字字面量 = 手抄，当场红）；
      3. vite 与 vitest 两份 config 都 import 共用的 `./shell-alias` —— 只挂一份 =
         构建与测试两套解析，别名接线本身也不许双份；
      4. tsconfig `paths` 有 `@shell/*`（JSON import 不进 JS 常量，只能钉）。
    """
    owner_rel = "shell/main/petDockGeometry.ts"
    pet_rel = "frontend/src/pages/PetPage.tsx"
    alias_rel = "frontend/shell-alias.ts"
    cfg_rels = ("frontend/vite.config.ts", "frontend/vitest.config.ts")
    tsconfig_rel = "frontend/tsconfig.json"

    texts: dict[str, str] = {}
    offenders: list[str] = []
    for rel in (owner_rel, pet_rel, alias_rel, *cfg_rels, tsconfig_rel):
        p = ROOT / rel
        if not p.exists():
            offenders.append(f"{rel} 不存在（尺子的输入丢了）")
            continue
        texts[rel] = p.read_text(encoding="utf-8", errors="ignore")

    # 每个子串都必须带边界（= / 空格 / 引号），否则 `LEGACY_SIDE_TUCK_MOVED` 这种
    # 保留前缀的改名会包含原串 —— 2026-11-04 M5 变异就是这么骗过第一版尺子的。
    if owner_rel in texts and "export const LEGACY_SIDE_TUCK =" not in texts[owner_rel]:
        offenders.append(f"{owner_rel} 不再导出 LEGACY_SIDE_TUCK（唯一出处搬家了，尺子要跟）")

    if pet_rel in texts:
        pet = texts[pet_rel]
        if "import { LEGACY_SIDE_TUCK } from '@shell/petDockGeometry';" not in pet:
            offenders.append(f"{pet_rel} 没有经 @shell/petDockGeometry import LEGACY_SIDE_TUCK")
        if "const HIT_SLACK_X = LEGACY_SIDE_TUCK;" not in pet:
            offenders.append(
                f"{pet_rel} 的 HIT_SLACK_X 赋值行不是逐字的 "
                "`const HIT_SLACK_X = LEGACY_SIDE_TUCK;`（改回字面量 = 手抄复发）"
            )

    if alias_rel in texts:
        a = texts[alias_rel]
        if "export const shellAlias =" not in a or "'@shell/'" not in a or "shell/main/" not in a:
            offenders.append(f"{alias_rel} 丢了 @shell → shell/main 的别名接线")

    for rel in cfg_rels:
        if rel not in texts:
            continue
        cfg = texts[rel]
        if 'from "./shell-alias"' not in cfg or "alias: shellAlias }" not in cfg:
            offenders.append(f"{rel} 没用共用别名（要直接 import ./shell-alias，不许自带一份）")

    if tsconfig_rel in texts and '"@shell/*"' not in texts[tsconfig_rel]:
        offenders.append(f"{tsconfig_rel} 没有 @shell/* paths（tsc 与 vite 的解析会分家）")

    out(
        "pet geometry single source",
        not offenders,
        "; ".join(offenders[:5])
        if offenders
        else "HIT_SLACK_X 直接引用壳的 LEGACY_SIDE_TUCK；vite/vitest/tsconfig 三处接同一别名",
    )
    if offenders:
        fails.append(f"pet geometry single source: {offenders}")


def check_clamp_single_source() -> None:
    """工作区夹取的数学只住在 `petDockGeometry.clampRectToWorkArea`（A-11 归一）。

    （A-11，2026-11-04）同一条式子 `x = min(max(x, work.x), work.x + max(0, work.w − w))`
    在审计时有**三份近亲写法**：`state.ts` 的 `clampToWorkArea`、`windows.ts`
    `petAnchorFor` 的行内八行、`flushRect` 只夹 y 的标量化半份 —— 改一处不跟另外两处，
    正是"多套逻辑"的形状。归一后三处都改调纯函数，任一份回来都判红：

      1. 纯层唯一定义还在（`export function clampRectToWorkArea(` 带括号收边界）；
      2. x 式数学本体在纯层里**恰好一次**（0 = 搬家了、2 = 纯层内也长双胞胎）；
      3. 旧写法的字面特征在两个调用文件里一个都不许在 —— 每个子串带边界
         （A-9 的 M5 教训：`clampToWorkAreaExtra` 这类保留前缀的改名会包含原串）；
      4. 两个调用点逐字改调新函数，且 dock 测试真的在跑它（判据绿但没人测 = 空尺子）。
    """
    pet_rel = "shell/main/petDockGeometry.ts"
    win_rel = "shell/main/windows.ts"
    st_rel = "shell/main/state.ts"
    test_rel = "shell/scripts/test-pet-dock-geometry.cjs"

    texts: dict[str, str] = {}
    offenders: list[str] = []
    for rel in (pet_rel, win_rel, st_rel, test_rel):
        p = ROOT / rel
        if not p.exists():
            offenders.append(f"{rel} 不存在（尺子的输入丢了）")
            continue
        texts[rel] = p.read_text(encoding="utf-8", errors="ignore")

    if pet_rel in texts:
        pet = texts[pet_rel]
        if "export function clampRectToWorkArea(" not in pet:
            offenders.append(f"{pet_rel} 缺 clampRectToWorkArea 唯一定义")
        x_math = "Math.min(Math.max(rect.x, work.x), maxX)"
        n_x = pet.count(x_math)
        if n_x != 1:
            offenders.append(f"{pet_rel} 工作区夹取 x 式出现 {n_x} 次（须恰好 1）")

    if win_rel in texts:
        win = texts[win_rel]
        for bad, why in (
            ("Math.min(Math.max(next.x", "petAnchorFor 的行内夹取回来了"),
            ("work.y + Math.max(0, work.height - size.height)", "flushRect 的 y 向旧式回来了"),
            ("function clampToWorkArea", "旧私有双胞胎的名字出现在 windows"),
        ):
            if bad in win:
                offenders.append(f"{win_rel}：{why}（{bad!r}）")
        if "clampRectToWorkArea(next, workArea)" not in win:
            offenders.append(f"{win_rel} 的 petAnchorFor 没改调 clampRectToWorkArea")
        if "{ ...size, x: rect.x, y: rect.y }, work).y" not in win:
            offenders.append(f"{win_rel} 的 flushRect 没改调 clampRectToWorkArea")

    if st_rel in texts:
        st = texts[st_rel]
        for bad, why in (
            ("function clampToWorkArea", "state 里的旧私有双胞胎回来了"),
            ("Math.min(Math.max(rect.x, workArea.x)", "state 的行内旧式回来了"),
        ):
            if bad in st:
                offenders.append(f"{st_rel}：{why}（{bad!r}）")
        if "clampRectToWorkArea(" not in st:
            offenders.append(f"{st_rel} 没调用 clampRectToWorkArea")
        if 'from "./petDockGeometry"' not in st:
            offenders.append(f"{st_rel} 没从 ./petDockGeometry import")

    if test_rel in texts and "clampRectToWorkArea" not in texts[test_rel]:
        offenders.append(f"{test_rel} 没覆盖 clampRectToWorkArea（判据绿但没人测）")

    out(
        "clamp single source",
        not offenders,
        "; ".join(offenders[:5])
        if offenders
        else "夹取唯一定义在纯层；windows/state 三份旧写法归零；dock 测试有读数",
    )
    if offenders:
        fails.append(f"clamp single source: {offenders}")


def check_pet_reload_state_replay() -> None:
    """重载后壳页两半回到同一起点：壳侧 did-finish-load 复位 + 页面挂载 pull 首值（A-7）。

    （A-7，2026-11-04）整页 reload（F5、`dataSource.ts` 切后端的 `location.reload`、
    错误页按钮）把渲染端连锅端掉：新页面挂载时**默认收起、面板零位**，而壳侧
    `petIsExpanded` / `lastPanelShift` 会活过这一代不复位 —— 后果各自可数：

      * 残留 `petIsExpanded=true` ⇒ 「摊着别自动动」那几道闸门（吸边恢复、自动收起、
        停靠恢复）一直按住，`placePet` 还把非零位移推给一张收起的页；
      * 残留旧 `lastPanelShift` ⇒ `sendPanelShift` 的同值去重把**下一次展开**的真发送
        吃掉，页面停在 0，面板贴屏边那一截被切（去重只该防同代内重发）。

    判据钉两半，缺任一半都算复发：

      1. 壳侧 `did-finish-load` 复位块在场，且两条复位**逐字连续**（只复位一条 = 只修半边）；
      2. 页面挂载仍 pull 三个首值 —— 这半边是既有实现，index.ts 注释许过「页面 pull 首值、
         push 收变更」的愿，从此由尺子盯，不靠记性。
    """
    win_rel = "shell/main/windows.ts"
    pet_rel = "frontend/src/pages/PetPage.tsx"

    texts: dict[str, str] = {}
    offenders: list[str] = []
    for rel in (win_rel, pet_rel):
        p = ROOT / rel
        if not p.exists():
            offenders.append(f"{rel} 不存在（尺子的输入丢了）")
            continue
        texts[rel] = p.read_text(encoding="utf-8", errors="ignore")

    if win_rel in texts:
        win = texts[win_rel]
        if 'win.webContents.on("did-finish-load"' not in win:
            offenders.append(
                f"{win_rel} 没有 did-finish-load 复位钩子（重载=换代，壳侧两条真相要归零）"
            )
        reset_block = "petIsExpanded = false;\n    lastPanelShift = 0;\n  });"
        if reset_block not in win:
            offenders.append(f"{win_rel} 的复位块不是「两条逐字连续」（少一条 = 只修半边）")

    if pet_rel in texts:
        page = texts[pet_rel]
        for call, why in (
            (".petContentVisible()", "内容旗子首值不再 pull"),
            (".petVoiceEnabled()", "语音旗子首值不再 pull"),
            (".petPanelAutoHideMs()", "自动收起秒数首值不再 pull"),
        ):
            if call not in page:
                offenders.append(f"{pet_rel}：{why}（{call}）")

    out(
        "pet reload state replay",
        not offenders,
        "; ".join(offenders[:5])
        if offenders
        else "did-finish-load 复位两条逐字在场；页面挂载 pull 三个首值",
    )
    if offenders:
        fails.append(f"pet reload state replay: {offenders}")


def check_window_open_wiring() -> None:
    """新开窗外送只走 `windowOpenVerdict` 这一个判据（A-10：http/https → 系统浏览器）。

    （A-10，2026-11-04）`setWindowOpenHandler` 从前**一律 deny**：注入被挡住，真人点的
    合法外链也一起死。修成"判据一种、外送一处、其余仍拦"，但**行为改对了 ≠ 接线不会
    再退**（改回全拦不报编译错、测试也只测纯函数），所以钉四点：

      1. 判据本体还在 `navigation.ts`（与 `navigationVerdict` 同家）；
      2. index.ts 逐字走判据 + 逐字 `shell.openExternal`（缺任一半 = 白名单没接上/裸外送）；
      3. index.ts **不许内联 scheme 判断**（`protocol === "http:"` 出现在接线层 = 第二套逻辑）；
      4. 导航护栏测试真跑 `windowOpenVerdict`（判据绿但没人测 = 空尺子）。
    """
    nav_rel = "shell/main/navigation.ts"
    idx_rel = "shell/main/index.ts"
    test_rel = "shell/scripts/test-navigation-guard.cjs"

    texts: dict[str, str] = {}
    offenders: list[str] = []
    for rel in (nav_rel, idx_rel, test_rel):
        p = ROOT / rel
        if not p.exists():
            offenders.append(f"{rel} 不存在（尺子的输入丢了）")
            continue
        texts[rel] = p.read_text(encoding="utf-8", errors="ignore")

    if nav_rel in texts and "export function windowOpenVerdict(" not in texts[nav_rel]:
        offenders.append(f"{nav_rel} 缺 windowOpenVerdict 判据本体")

    if idx_rel in texts:
        idx = texts[idx_rel]
        if 'if (windowOpenVerdict(url) === "open-external")' not in idx:
            offenders.append(f"{idx_rel} 没走 windowOpenVerdict（改回全拦=外链又全死）")
        if "shell.openExternal(url)" not in idx:
            offenders.append(f"{idx_rel} 没有 shell.openExternal（判了 open-external 却没外送）")
        if 'protocol === "http:"' in idx or 'protocol === "https:"' in idx:
            offenders.append(f"{idx_rel} 内联了 scheme 判断（判据该只在 navigation.ts 一处）")

    if test_rel in texts and "windowOpenVerdict(" not in texts[test_rel]:
        offenders.append(f"{test_rel} 没覆盖 windowOpenVerdict（判据绿但没人测）")

    out(
        "window open wiring",
        not offenders,
        "; ".join(offenders[:5])
        if offenders
        else "判据在 navigation.ts；index.ts 走判据+openExternal 不内联；测试有覆盖",
    )
    if offenders:
        fails.append(f"window open wiring: {offenders}")


def check_ipc_sender_guard() -> None:
    """壳侧每条 IPC 通道都有 sender 锁：19 条走 fromOwnWindow，renderer-ready 自带更严那道（A-3）。

    （A-3，2026-11-04）页面渲染的是第三方角色卡（AI 生成内容）：`sandbox: true` 挡的是
    node，挡不住"页面里的脚本把桥方法挨个调一遍"。参数不可信各 handler 本来就有纪律，
    而**谁在说话**这道从前只有 `shell:renderer-ready` 有。判据钉三件事：

      1. helper `fromOwnWindow` 在场，且**两扇窗都算**（只留一半 = 另一扇窗的 IPC 全死）；
      2. 逐通道过：注册处到下一个注册处之间的片段必须带锁行；`shell:renderer-ready`
         例外但不是免检 —— 它必须带自己那道更严的（只认控制台）；
      3. 全局账：锁行数 == 注册数 - 1（防"锁跑出通道外"凑数，也给新通道留对账基准）。

    判据按**注册本身**分段，将来加通道自动纳入，不维护通道清单。
    """
    idx_rel = "shell/main/index.ts"
    p = ROOT / idx_rel
    if not p.exists():
        out("ipc sender guard", False, f"{idx_rel} 不存在（尺子的输入丢了）")
        fails.append("ipc sender guard: [输入文件丢失]")
        return

    src = p.read_text(encoding="utf-8", errors="ignore")
    offenders: list[str] = []

    if "function fromOwnWindow(" not in src:
        offenders.append("缺 fromOwnWindow 定义")
    else:
        if "id === mainWin.webContents.id" not in src:
            offenders.append("fromOwnWindow 不认控制台窗")
        if "id === petWin.webContents.id" not in src:
            offenders.append("fromOwnWindow 不认桌宠窗（那扇的 IPC 会全死）")

    matches = list(re.finditer(r'ipcMain\.(?:handle|on)\("([^"]+)"', src))
    if not matches:
        offenders.append("index.ts 里一个 ipcMain 注册都找不到（输入变了？）")
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(src)
        seg = src[m.start() : end]
        channel = m.group(1)
        if channel == "shell:renderer-ready":
            if "event.sender.id !== mainWin.webContents.id" not in seg:
                offenders.append("shell:renderer-ready 没有自己的更严 sender 校验")
        elif "if (!fromOwnWindow(event)) return;" not in seg:
            offenders.append(f"{channel} 没上 fromOwnWindow 锁")

    guards = src.count("if (!fromOwnWindow(event)) return;")
    if guards != len(matches) - 1:
        offenders.append(f"锁行 {guards} ≠ 注册 {len(matches)} - 1（锁跑出通道外 / 有通道漏锁）")

    out(
        "ipc sender guard",
        not offenders,
        "; ".join(offenders[:6])
        if offenders
        else f"注册 {len(matches)} 条：19 条 fromOwnWindow + renderer-ready 自带严锁",
    )
    if offenders:
        fails.append(f"ipc sender guard: {offenders}")


def check_health_data_root() -> None:
    """/api/health 报 `data_root`，且只准问 `user_data_root()`（A-4）。

    （A-4，2026-11-04）09-24 之后世界上有两份数据根（仓库 `data/` 与安装态系统目录），
    外加 `DATA_ROOT` 整体搬家（M4）—— 并存的世界里"探活绿着但数据不对"要能从 health
    这一格直接归因到目录。判据钉四件事：

      1. import 带上 `user_data_root`（少了 = 运行时 NameError，health 500 = 容器永远
         unhealthy，比缺一格信息重得多）；
      2. health() 返回体里逐字 `"data_root": str(user_data_root()),`；
      3. API 层**不碰**第二套解析（`repo_root() / "data"`、`_platform_data_root` 都是
         另写一份规则 —— 忽略 `DATA_ROOT` 覆盖正是这条要消灭的病）；
      4. 行为半边有人测（`tests/unit/test_health_data_root.py` 钉逐字相等 + 覆盖跟走）。
    """
    rel = "src/rolecard_agent/api/main.py"
    p = ROOT / rel
    test_rel = "tests/unit/test_health_data_root.py"
    if not p.exists():
        out("health data root", False, f"{rel} 不存在（尺子的输入丢了）")
        fails.append("health data root: [输入文件丢失]")
        return

    src = p.read_text(encoding="utf-8", errors="ignore")
    offenders: list[str] = []

    if "from rolecard_agent.base.paths import console_dist_dir, user_data_root" not in src:
        offenders.append(f"{rel} import 没带上 user_data_root（health 会 500）")

    start = src.find('@app.get("/api/health")')
    end = src.find("dist_dir = console_dist_dir()")
    if start < 0 or end <= start:
        offenders.append(f"{rel} 找不到 health 段（输入变了？）")
    else:
        region = src[start:end]
        if '"data_root": str(user_data_root()),' not in region:
            offenders.append("health() 没报 data_root（归因不到目录）")
        if 'repo_root() / "data"' in region or "_platform_data_root" in region:
            offenders.append("health 内联了第二套数据根解析（会忽略 DATA_ROOT 覆盖）")

    if "_platform_data_root" in src:
        offenders.append(f"{rel} 碰了 paths 的内部推导（API 层只准走 user_data_root）")

    t = ROOT / test_rel
    if not t.exists():
        offenders.append(f"{test_rel} 缺失（行为半边没人测）")
    else:
        tsrc = t.read_text(encoding="utf-8", errors="ignore")
        if 'body["data_root"]' not in tsrc or "user_data_root()" not in tsrc:
            offenders.append(f"{test_rel} 没钉 data_root == user_data_root()")

    out(
        "health data root",
        not offenders,
        "; ".join(offenders)
        if offenders
        else "health 报 data_root 且只问 user_data_root；行为测试在场",
    )
    if offenders:
        fails.append(f"health data root: {offenders}")


#: raise/assert 里的路径引用：至少读出这么多条，否则判红（读法塌了不许静默绿）。
#: 现网实测 2026-11-03：src/ 388 条 raise/assert、其中 310 条带字符串，路径引用 4 条。
#: 下限取 2 是因为这个数天然小（只有"指挥读者去改哪个文件"的那几条才写路径）；
#: 判"读法坏了"靠的是下面 `_MIN_RAISE_NODES`，两条分工不同。
_MIN_PATH_REFS = 2
_MIN_RAISE_NODES = 120


#: 生产侧 `new ApiError(0, …)` 至少要有这么多处，读出来少于它就判红。
#: 现网实测 2026-11-03：api/index.ts 4 处 + lib/sync.ts 3 处 = 7。下限取 5 是给
#: 合理的合并留余地，同时挡住"解析器塌了 → 一个都没读到 → 全绿"（本仓 consistency
#: 家族的既有纪律：读不出输入不等于通过）。
_MIN_ZERO_STATUS_CALLS = 5


def _top_level_args(call: str) -> list[str]:
    r"""把 `a, b \`x\`, f(1,2)` 这样的实参表按**顶层逗号**切开（括号与引号内不算）。"""
    args: list[str] = []
    depth = 0
    quote = ""
    cur: list[str] = []
    for ch in call:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'`":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    args.append("".join(cur))
    return [a.strip() for a in args]


def check_zero_status_apierror_has_kind() -> None:
    """`new ApiError(0, …)` 必须带第三个实参（kind），生产代码里一处不许漏。

    为什么立这条（2026-11-03 审查 A1）：`status = 0` 这一个值同时装着**三件不同的事**
    —— 断网/后端没起、前端等到超时（后端很可能还在跑）、本机同步程序没在限时内回答。
    于是唯一出口 `describeError` 只能按 status 分档，那一档给的
    "网络错误：后端连不上，请确认服务已启动" 对**超时**是反话：上传/抽取到点时后端
    已经把文件落了盘、还在建索引（`api/index.ts:74-78` 的立论写得很清楚），这句话
    催用户去重启服务并照着"假失败"再点一次。措辞住在抛出方、语义却只有抛出方知道
    —— 所以语义必须由抛出方标出来，而不是让下游猜。

    判据（与 `single-source literals` 同一取向：读 AST 之外的**结构**，不数原文）：
      * 生产代码（`frontend/src/**` 的 .ts/.tsx，**排除测试件与 dist**）里每一处
        `new ApiError(` 的**第一个实参**若是字面 `0`，就必须有第三个实参且含
        `"timeout"` / `"network"` 之一；
      * 顶层逗号切分（模板串与嵌套括号不吃进去），不是正则一刀切；
      * 读出的调用数 < `_MIN_ZERO_STATUS_CALLS` ⇒ 判红（解析器塌了不许当通过）；
      * 测试件豁免：mock 载荷是自足的假数据，而 `errors.test.ts` 里**故意**留了一条
        不带 kind 的用例，那是在测"分不清就不赌"这一路。
    """
    root = ROOT / "frontend" / "src"
    offenders: list[str] = []
    calls = 0
    zero_calls = 0
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".ts", ".tsx") or not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if ".test." in rel or "dist" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        pos = 0
        while True:
            i = text.find("new ApiError(", pos)
            if i < 0:
                break
            pos = i + len("new ApiError(")
            # 括号配对取实参表（模板串里的括号不算，所以按引号状态跳过）
            depth = 1
            quote = ""
            j = pos
            while j < len(text) and depth > 0:
                ch = text[j]
                if quote:
                    if ch == quote:
                        quote = ""
                elif ch in "\"'`":
                    quote = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                j += 1
            if depth != 0:
                offenders.append(f"{rel} 有一处 new ApiError( 括号不配对，这条尺子读不动它")
                continue
            args = _top_level_args(text[pos : j - 1])
            calls += 1
            if args and args[0] == "0":
                zero_calls += 1
                kind = args[2] if len(args) >= 3 else ""
                if '"timeout"' not in kind and '"network"' not in kind:
                    line = text[:i].count("\n") + 1
                    offenders.append(f"{rel}:{line} status=0 却没标 kind（实参 {len(args)} 个）")
    if calls < _MIN_ZERO_STATUS_CALLS:
        offenders.insert(
            0, f"只读出 {calls} 处 new ApiError（下限 {_MIN_ZERO_STATUS_CALLS}）—— 读法坏了"
        )
    out(
        "status=0 ApiError has kind",
        not offenders,
        "; ".join(offenders[:5])
        if offenders
        # 两个数各说各的：`calls` 是扫到的全部抛点，其中 status=0 的才需要 kind。
        # 写成 "5/8 处都标了 kind" 会让人以为有 3 处漏标 —— 读数的措辞也算文案。
        else f"{calls} 处 new ApiError 里 {zero_calls} 处是 status=0，全部标了 kind",
    )
    if offenders:
        fails.append(f"ApiError(0) without kind: {offenders}")


def check_error_message_paths_resolve() -> None:
    """`raise` / `assert` 文本里指出的源码路径，必须在磁盘上存在。

    为什么只管 raise/assert、不管注释与 docstring（2026-11-03 审查 A3）：那一轮量出
    仓里 **21 处**写着 `core/migrations.py`，而那个文件从来不存在（步骤清单 P1-6 拆包后
    住在 `core/storage/migrations.py`）。但**不能**把尺子做成"全文扫路径"—— 实测那会抓到
    13 种"失踪"，其中一批是**合法的历史叙述**（`R102-59` 从 `core/reachout.py` 拆出…：
    拆分当时它确实在 core，后来才迁 features，把它"改正"成 features 反而把历史改错了，
    我这轮差点就这么干了）。散文里的旧名字是记录，报错文本里的旧名字是指令。

    判据只认**指令**：raise/assert 的字符串是运行时递给排障者的"你该去哪儿改"，指着
    一个不存在的文件，症状就是"照着报错文本找不到东西"——那两句正好是 P1-6 之后唯一
    会在此类事故里被人读到的文本（`db.py:552` 数据库打不开、`db.py:671` 补列器拦下
    NOT NULL 无默认列）。两条实测都指错，现已改正。

    读法三条，任一不满足判红（不用"扫到 0 条"当通过）：
      * raise/assert 节点数 < `_MIN_RAISE_NODES` ⇒ AST 读法塌了（现网 388）；
      * 路径引用数 < `_MIN_PATH_REFS` ⇒ 要么被人删了、要么正则不认新形状，两者都该问；
      * 路径的候选落点按三种形制试：`src/rolecard_agent/<tok>`、仓库根 `<tok>`、`src/<tok>`
        （与文档里两种写法同源）。f-string 里的字面片段也算（ast 的 Constant 位置天然覆盖）。
    """
    offenders: list[str] = []
    refs = 0
    nodes = 0
    path_pat = re.compile(
        r"(?<![\w/.-])((?:[a-z_][a-z0-9_]*/){1,6}[a-z_][a-z0-9_]*\.(?:py|sql))(?![\w-])"
    )
    for file in sorted((ROOT / "src").rglob("*.py")):
        rel = file.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(file.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError) as exc:
            offenders.append(f"{rel} 解析失败（{type(exc).__name__}），这条尺子看不见它")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Raise, ast.Assert)):
                continue
            nodes += 1
            for sub in ast.walk(node):
                if not (isinstance(sub, ast.Constant) and isinstance(sub.value, str)):
                    continue
                for m in path_pat.finditer(sub.value):
                    tok = m.group(1)
                    refs += 1
                    cands = (
                        ROOT / "src" / "rolecard_agent" / tok,
                        ROOT / tok,
                        ROOT / "src" / tok,
                    )
                    if not any(c.exists() for c in cands):
                        offenders.append(f"{rel}:{node.lineno} 指向不存在的 {tok}")
    if nodes < _MIN_RAISE_NODES:
        offenders.insert(
            0, f"只读出 {nodes} 条 raise/assert（下限 {_MIN_RAISE_NODES}）—— AST 读法坏了"
        )
    if refs < _MIN_PATH_REFS:
        offenders.insert(0, f"只读出 {refs} 条路径引用（下限 {_MIN_PATH_REFS}）—— 判据形状变了？")
    out(
        "error message paths resolve",
        not offenders,
        "; ".join(offenders[:4])
        if offenders
        else f"{refs} 条报错文本里的路径全部存在（扫 {nodes} 条 raise/assert）",
    )
    if offenders:
        fails.append(f"error messages point at missing files: {offenders}")


#: updater.ts 里"一次失败只有一个读数主人"的落点（2026-11-03 审查 A-8）。
#: 每项 = 上游调用名；它的 `.catch` 体内不许再出现下面这两个动作。
_UPDATER_CATCH_SITES = ("checkForUpdates", "downloadUpdate")
_UPDATER_FORBIDDEN_IN_CATCH = ("options.notify(", "setState(")


def _js_block_body(text: str, from_index: int) -> str:
    """取 `from_index` 之后**第一个** `{…}` 的内容（嵌套算进去，字符串/模板串里的不算）。

    为什么不用正则：要判的是"这个 catch 的函数体**里**有没有 notify"。正则一刀切会把
    注释也算进去，而模板串里的 `${...}` 会骗过括号配对 —— 两种错的方向都不可控。
    """
    i = text.find("{", from_index)
    if i < 0:
        return ""
    depth = 0
    quote = ""
    j = i
    while j < len(text):
        ch = text[j]
        if quote:
            if ch == quote and text[j - 1] != "\\":
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j]
        j += 1
    return ""


def check_updater_failure_readout_has_one_owner() -> None:
    """失败读数与状态读数只有 `error` 监听器一个主人（盯**接线**，跨 TS）。

    为什么判据搬进 `updateFeedback.ts`、能被 node 直测之后，还要这条（Python 读 TS 原文）：
    判据测试只能证明"判据本身算得对"，**证明不了 `updater.ts` 真在用它**。那两条
    `.catch` 里有没有人把 `notify`/`setState` 再加回来，判据测试一个字都看不见 ——
    而"先 emit 再 throw"这件事住在 electron-updater 的编译产物里，升级那个包、或有人
    "顺手把失败也写进状态行"，双弹就复活。这正是 A-8 的原发形状（同一次失败两条同名
    toast）。尺子因此同时盯两侧：判据对不对（shell 那边有测试）、接线有没有绕过它（这条）。

    判据三条，任一不满足判红（"看不见"不等于"通过"）：
      1. `checkForUpdates()` / `downloadUpdate()` 的 `.catch` **函数体内**不许出现
         `options.notify(` 或 `setState(`；
      2. `updateFeedback.ts` 导出的判据函数在 `updater.ts` 里**各有真实调用点**
         （导出了没人调 = 判据空转，线上走的还是旧逻辑而测试全绿）；
      3. 两个输入文件读不到、两处上游调用一个都找不到、或某个 catch 的函数体抠不出
         内容 ⇒ 判红（读法塌了不许静默绿）。

    注释过滤是**行首启发式**（`//`、`/*`、续行 `*`）：误判方向是假红（块注释中间那行
    没对齐也会被当代码），不是假绿 —— 宁可多报，因为漏报就是这条尺子存在的理由。

    为什么 Python 再读一遍 TS、而不是让壳那边的测试顺手管：两边问的是**不同的问题**。
    `test-update-feedback.cjs` 证"判据算得对"（给定上下文，该不该弹、弹哪一阶段）；
    这条证"接线没绕过判据"（那两个 catch 里没有第二个主人、判据函数真被调用）。
    判据测试对这两件事**一个字都看不见** —— 它 import 的是 `updateFeedback.js`，
    不看 `updater.ts`。两条都必要，缺一不可。
    （gate 的 full 档确实跑 `npm run test:cleanup` / `test:dock`，接线也在那里被
    `check_consistency` 覆盖到同一把门禁 —— 这条不是为了"多一处要人记得跑"。）
    """
    updater = ROOT / "shell" / "main" / "updater.ts"
    feedback = ROOT / "shell" / "main" / "updateFeedback.ts"
    offenders: list[str] = []
    for p in (updater, feedback):
        if not p.exists():
            offenders.append(f"{p.relative_to(ROOT).as_posix()} 不存在（这条尺子的输入丢了）")
    if offenders:
        out("updater failure readout has one owner", False, "; ".join(offenders))
        fails.append(f"updater readout inputs missing: {offenders}")
        return

    text = updater.read_text(encoding="utf-8", errors="ignore")
    fb_text = feedback.read_text(encoding="utf-8", errors="ignore")

    checked = 0
    for call in _UPDATER_CATCH_SITES:
        pos = 0
        while True:
            i = text.find(f"{call}()", pos)
            if i < 0:
                break
            pos = i + len(call) + 2
            catch_at = text.find(".catch(", i)
            if catch_at < 0 or catch_at - i > 200:
                offenders.append(f"{call}() 之后找不到紧邻的 .catch —— 吞 rejection 那一条不成立")
                continue
            checked += 1
            body = _js_block_body(text, catch_at)
            if not body.strip():
                offenders.append(f"{call}().catch 的函数体抠不出内容（读法塌了，不判通过）")
                continue
            code = "\n".join(
                line for line in body.splitlines() if not line.strip().startswith(("//", "*", "/*"))
            )
            for bad in _UPDATER_FORBIDDEN_IN_CATCH:
                if bad in code:
                    line = text[:catch_at].count("\n") + 1
                    offenders.append(
                        f"shell/main/updater.ts:{line} {call}().catch 里有 {bad} —— "
                        "读数出现第二个主人"
                    )
    if checked == 0:
        offenders.append("两处上游调用一个都没找到 —— 接线形状变了，这条尺子已看不见它要防的东西")

    wanted = {
        "failureOf",
        "failureTitle",
        "advanceStageOnDownload",
        "isCheckTerminal",
        "shouldHandoffQuitToInstall",
    }
    exported = set(re.findall(r"^export (?:function|const) (\w+)", fb_text, re.M))
    missing_export = sorted(wanted - exported)
    if missing_export:
        offenders.append(f"updateFeedback.ts 少了这些导出：{missing_export}")
    for name in sorted(wanted & exported):
        if len(re.findall(rf"\b{name}\(", text)) == 0:
            offenders.append(f"{name} 被导出但 updater.ts 一次都没调（判据空转）")

    out(
        "updater failure readout has one owner",
        not offenders,
        "; ".join(offenders[:5])
        if offenders
        else f"{checked} 处 .catch 只记日志；{len(wanted)} 条判据各有调用点",
    )
    if offenders:
        fails.append(f"updater readout owners: {offenders}")


def check_gate_step_lists_aligned() -> None:
    """`gate.py` 的三份步骤名单必须互相对齐（新增步骤忘了登记 = 红）。

    为什么立这条（2026-11-03 自查）：门禁的步骤清单 `STEPS`、每步上限表
    `STEP_TIMEOUTS`、CI 跳过集 `CI_SKIP` 是**同一批步骤名的三处声明**。`_step_timeout`
    用 `dict.get(name, DEFAULT)` 查表 —— 查不到不报错，只是那一步的每步上限**静默失效**
    （掉进 900s）。表里「openapi 契约同步」那条注释正是作者自己撞过这一族后补的。
    而本轮往 full 档加 `shell 更新反馈判据` 时我**又漏登记了 STEP_TIMEOUTS** —— 普查
    三份名单才发现；顺手量出既有的一处同类缺口：`npm 依赖审计` 从来没进过那张表
    （它内部硬超时 600s，却一路拿着 DEFAULT 900 跑）。既然连"刚被这条纪律提醒过的人"
    都会漏，它就不该靠记得。

    判据全走 AST，两条读法教训都写在这里：
      * 上一轮我用**正则**普查，把注释与 CLI flag 都算成步骤名（读数脏）；
      * 更险的是 `STEPS`/`STEP_TIMEOUTS` 写成**带注解的赋值**
        （`STEPS: list[...] = [...]` 是 `ast.AnnAssign` 不是 `ast.Assign`）——
        只认 `Assign` 的读法会把它读成**空集**，于是三份差集全报"无"。
        空集算出来的"没问题"与真没问题长得一模一样，所以**先断言三份都非空**再算差集。

    三条判据：
      1. 三份名单各自非空，否则判红（读法塌了不许静默绿）；
      2. 每个步骤名都要在 `STEP_TIMEOUTS` 里有格子（缺 = 每步上限静默掉进 DEFAULT）；
      3. 每个 `CI_SKIP` 成员都要真对应一个步骤（拼错 = 那一步在 CI 里其实没跳）。

    方向差集 `STEP_TIMEOUTS - STEPS`（孤儿格子）**刻意不判红**：删步骤时留下的旧格子
    只是噪声，不会让人少跑任何检查；判它只会逼人把表清空一半。数量写进读数，看得见不拦。
    """
    gate = ROOT / "scripts" / "gate.py"
    offenders: list[str] = []
    if not gate.exists():
        out("gate step lists aligned", False, "scripts/gate.py 不存在（这条尺子的输入丢了）")
        fails.append("gate.py missing for step-list check")
        return
    tree = ast.parse(gate.read_text(encoding="utf-8", errors="ignore"))

    def _targets(node: ast.AST) -> list[str]:
        # Assign 与 AnnAssign 两种写法都要认 —— 漏后者就是上面那段假绿。
        if isinstance(node, ast.Assign):
            return [t.id for t in node.targets if isinstance(t, ast.Name)]
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return [node.target.id]
        return []

    steps: list[str] = []
    timeouts: list[str] = []
    ci_skip: list[str] = []
    for node in ast.walk(tree):
        for name in _targets(node):
            value = getattr(node, "value", None)
            if value is None:
                continue
            if name == "STEPS" and isinstance(value, (ast.List, ast.Tuple)):
                for el in value.elts:
                    # 每项形如 ("名字", [cmd...], "档位")；展开调用（如 _smoke_steps()）
                    # 不是元组字面量，拿不到就跑不出非空断言 —— 那里能兜住。
                    if (
                        isinstance(el, ast.Tuple)
                        and el.elts
                        and isinstance(el.elts[0], ast.Constant)
                    ):
                        steps.append(str(el.elts[0].value))
            elif name == "STEP_TIMEOUTS" and isinstance(value, ast.Dict):
                timeouts = [str(k.value) for k in value.keys if isinstance(k, ast.Constant)]
            elif name == "CI_SKIP":
                for sub in ast.walk(value):
                    if isinstance(sub, (ast.Set, ast.List, ast.Tuple)):
                        ci_skip += [str(e.value) for e in sub.elts if isinstance(e, ast.Constant)]

    for label, lst in (("STEPS", steps), ("STEP_TIMEOUTS", timeouts), ("CI_SKIP", ci_skip)):
        if not lst:
            offenders.append(f"{label} 读出 0 条 —— 读法坏了，空集会把下面算成全绿")
    if not offenders:
        S, T, C = set(steps), set(timeouts), set(ci_skip)
        for name in sorted(S - T):
            offenders.append(f"步骤「{name}」没有 STEP_TIMEOUTS 格子（每步上限静默掉进 DEFAULT）")
        for name in sorted(C - S):
            offenders.append(f"CI_SKIP 里的「{name}」不对应任何步骤（拼错 = CI 里其实没跳）")
    out(
        "gate step lists aligned",
        not offenders,
        "; ".join(offenders[:5])
        if offenders
        else f"{len(steps)} 步骤名 × {len(timeouts)} 上限格子 × {len(ci_skip)} 个 CI 跳过项两两对齐"
        f"（孤儿上限格子 {len(set(timeouts) - set(steps))} 个，只算噪声不拦）",
    )
    if offenders:
        fails.append(f"gate step lists drift: {offenders}")


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
    `requirements/requirements-cloud.txt`，症状是配任何 OpenAI 兼容端点保存即 500，
    而本机 .venv 恰好装过所以门禁看不见。这条检查防的是同族的另一半：**import 了但哪层都没声明** ——
    `httpx`（4 处顶层 import）与 `typing_extensions`（core/agent/state.py）当时全靠
    langchain-core / pydantic 的传递依赖兜住；传递兜住时不报错，某天上游收窄约束
    就静默断（agent 取证时实测过 langchain-core 1.6.3 的 Requires-Dist 确实带着 httpx）。

    判据：AST 扫 `src/**/*.py` 的全部 import（含函数内的 lazy import —— 那条路径被触发
    同样 500），顶层模块名去 stdlib、去第一方后，归一化（下划线→连字符）后必须在
    **运行层**的 `requirements/requirements*.txt` 里声明 —— dev 层（含 PyInstaller；它从前独占
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
    non_runtime = {"requirements/requirements-dev.txt"}
    for req in sorted(ROOT.glob("requirements/requirements*.txt")):
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
            "src imports not declared in any requirements/requirements*.txt: "
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


def _compose_service_env(compose: str, service: str) -> dict[str, object] | None:
    """`services.<service>.environment` 这一格（按结构取），没有这一段返回 None。

    `None` 与 `{}` 是两件事：前者是"这一段不存在"（调用方该出声），后者是"存在但为空"。
    旧版正则分不出这两个，取不到就统一返回 []，于是"缺护栏"那一问静默变绿。
    """
    doc = simple_yaml(compose)
    svc = (doc.get("services") or {}).get(service) or {}
    env = svc.get("environment")
    return env if isinstance(env, dict) else None


def _deploy_port_findings(compose: str, caddy_text: str) -> list[str]:
    """端口这一问的两条规矩，按结构问（P3-9 解析器化的第四格）。

    为什么换掉两条正则（**先量过的缺陷**，不是猜的：把现网 compose 临时改成三发变异跑
    整份一致性 —— `ports: - "8080:8000"`、`ports: - "127.0.0.1:8000:8000"`、以及整段删掉
    `expose:` —— 旧尺子**三发全绿**；那三发现在钉在 `tests/unit/test_deploy_port_guard.py`
    里，改名 M1/M2/M3，与本文件同一条 commit 落盘）：

      旧 A `re.search(r'^\\s*-\\s*"?8000:\\d+', compose)` —— 锚在"冒号左边字面是 8000"，
         于是 `- "8080:8000"`（把应用容器口挂到宿主 8080）与 `- "127.0.0.1:8000:8000"`
         都从它底下滑过去，而这条断言的**结论文本当时就在说"应用端口不 publish"** ——
         判据是摆设，报告还照样绿。
      旧 B `re.findall(r'^\\s*-\\s*"?(\\d{2,5})"?\\s*$', compose)` 扫全文取 expose，
         整段 `expose:` 删掉时取到空集，而下面写的是 `if exposed and upstream:` ——
         **取不到 = 不问**，于是"删掉 expose 而 Caddyfile 还打着 app:8000"也静默绿。

    现在的问法贴着 compose 文件头那句真正的规矩（"ports 只出现在 caddy 那一段"）：

      ① 除 caddy 之外的任何 service **不许有 `ports:` 这一格**（宿主侧暴露与端口号无关，
         绑回环也是暴露）；
      ② Caddyfile 的 `reverse_proxy app:<port>` 必须出现在 `services.app.expose` 里，
         而**那段读不出序列不再等于"没东西可查"** —— 缺就是红，并把当时的形状写出来。

    序列里的 `- "8000"` 被 `simple_yaml` 读成字符串 `"8000"`（不是 `{"8000": None}`：
    端口映射冒号后没空格，按 YAML 的规矩是一个标量），所以下面统一按字符串比。
    """
    problems: list[str] = []
    services = (simple_yaml(compose).get("services") or {}) if compose else {}
    if not isinstance(services, dict):
        return [
            f"compose 的 services 读不出映射（形状是 {type(services).__name__}）—— "
            "这一问不能静默跳过"
        ]

    published = sorted(
        name
        for name, node in services.items()
        if name != "caddy" and isinstance(node, dict) and node.get("ports")
    )
    if published:
        problems.append(
            f"这些 service 把端口 publish 到了宿主：{published}（这一档只许 caddy 出公网；"
            "应用一旦被 publish，「忘了配鉴权」就只是裸奔而不是起不来）"
        )

    upstream = {f"app:{m}" for m in re.findall(r"reverse_proxy\s+app:(\d+)", caddy_text)}
    # 先绑一个名字再 isinstance：写成 `X if isinstance(X, dict) else {}` 里那两次 X 是
    # 两个独立的调用，mypy 不会把前一次的判据递到后一次（`union-attr` 就是这么来的）。
    app_raw = services.get("app")
    app_node = app_raw if isinstance(app_raw, dict) else {}
    raw_expose = app_node.get("expose")
    if not isinstance(raw_expose, list):
        # 读不出序列的三种形状都到这里：没这一段（None）、写了 `expose:` 而下面没条目
        # （也是 None）、以及形状根本是标量。**没有一种等于"没东西可查"**：旧写法取到
        # 空集就 `if exposed and upstream:` 跳过，于是删掉 expose 那一格是绿的。
        # 刻意不写 `raw_expose or []`：`simple_yaml` 交不出空列表（子节点为空就是 None，
        # `[]` 属于流式语法、直接抛），那条分支**守不住任何东西** —— 写它就是把防线
        # 当装饰，而本函数要治的正是装饰。（这条是把自己的变异测试装回旧写法时量出来的：
        # 旧写法能骗过当时全部用例，于是补了 `test_deploy_port_guard.py` 的形状那几发。）
        if upstream:
            shape = "这一段不存在或下面是空的" if raw_expose is None else repr(raw_expose)[:40]
            problems.append(
                f"services.app 读不出 expose 序列（实际形状：{shape}），而 Caddyfile 打着 "
                f"{'、'.join(sorted(upstream))} —— 容器间端口没暴露，反代打不通；"
                "这一问不许因为读不出就跳过"
            )
        return problems
    exposed = {f"app:{item}" for item in raw_expose}
    if upstream and not upstream <= exposed:
        problems.append(
            f"Caddyfile 打到 {'、'.join(sorted(upstream))}，compose expose 的是 "
            f"{'、'.join(sorted(exposed))}"
        )
    return problems


def _compose_app_env(compose: str) -> list[str]:
    """取 `services.app.environment` 那一段里的键名（走 `simple_yaml`，不引 PyYAML）。

    为什么不扫全文：compose 的 `environment:` 每个 service 都有一段，而"这个旋钮应用读不读"
    只对 `app` 那一段成立 —— 扫全文会把 Caddy 的 `ROLECARD_DOMAIN` 当成空转旋钮。

    为什么从正则换成按结构取（P3-9「解析器化」的第三格）：旧版是三条正连环环相扣，
    `app:` 认死两空格、`environment:` 认死四空格、键认死六空格 —— **缩进一变就整段取不到
    而静默返回 []**，于是"compose 里不许有没人读的旋钮"这条就恒绿了（判据变摆设，与
    `_imported_modules` / `check_changelog` 那两次同一个坑）。按 `services.app.environment`
    取是在问"这一格的值"，不是在文本里找一个长得像的行。

    解析不了（用了子集之外的 YAML）时**不静默判绿**：`simple_yaml` 直接抛，比假装没这段更诚实。
    """
    return list(_compose_service_env(compose, "app") or {})


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
    # 已知旋钮 = config 认的**全部**键（映射表 + `src.get` 独立分支，AST 一份读法，
    # P3-9 第五格：从前这里自己再抄一条 `\("KEY", "field"\)` 正则 —— 同一结构两条读法、
    # 改一边漏一边，且它只认表、认不出 MODEL_BACKENDS 那族分支键）∪ `.env.example`
    # 里已经解释过的那族（键名对齐由 `config contract` 那条管，这里不另起一套口径）。
    known = set(config_env_contract(cfg_text)[1])
    known |= set(re.findall(r"^([A-Z][A-Z0-9_]+)=.*$", env_text, flags=re.M))

    problems: list[str] = []
    # **只看 app 那一段的 environment**。第一版扫全文，于是把 caddy 的 `ROLECARD_DOMAIN`
    # 也算成"应用不读的旋钮"报了红 —— 那不是它该管的：别的 service 的环境变量是给
    # Caddy 读的，本来就不在这张映射表里。
    keys = _compose_app_env(compose)
    unknown = sorted(k for k in keys if k not in known)
    if unknown:
        problems.append(f"compose 写了应用不读的旋钮 {unknown}")
    # 值也是从 `app.environment` 这一格读（不再 `^      AUTH_MODE:` 满文件找）：
    # 认死六空格的正则既会**匹配到别的服务**同缩进的键，缩进一变又整条取不到而静默判绿。
    app_env = _compose_service_env(compose, "app") or {}
    missing = [k for k in _DEPLOY_REQUIRED_ENV if k not in app_env]
    if missing:
        problems.append(f"护栏缺条 {missing}")
    mode = app_env.get("AUTH_MODE")
    if mode is not None and mode != "on":
        problems.append(f"AUTH_MODE={mode}：反代之后 auto 把所有人都当回环，等于没鉴权")

    caddy = ROOT / "deploy" / "Caddyfile"
    caddy_text = caddy.read_text(encoding="utf-8", errors="ignore") if caddy.exists() else ""
    # 端口这两问按结构问（旧两条正则有**实测的三发漏检**，见 `_deploy_port_findings`）。
    problems.extend(_deploy_port_findings(compose, caddy_text))

    readme = (ROOT / "README.md").read_text(encoding="utf-8", errors="ignore")
    required_vars = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+):\?", compose))
    must_teach = sorted(v for v in required_vars if v not in readme)
    if must_teach:
        problems.append(f"这几个必填变量 README 没教 {must_teach}")

    # —— 10-01 加的四问：这条断言从前**只问键名存不存在**，而"配了没用"这一族缺陷全都住在值里。
    # (a) 口令不许写死在 compose 里。旧写法只查 `AUTH_CREDENTIALS:` 这个子串在不在，
    #     于是把明文口令直接写进这份要进 git 的文件照样绿 —— 那是凭据入库，不是配置。
    cred = app_env.get("AUTH_CREDENTIALS")
    if cred is not None and not str(cred).startswith("${"):
        problems.append(
            f"AUTH_CREDENTIALS 写的是字面量（{str(cred)[:16]}…）—— "
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
    exempt_value = app_env.get("AUTH_EXEMPT_PATHS")
    if not health_target:
        problems.append("Dockerfile 里读不出 HEALTHCHECK 打的是哪条路径 —— 这一问不能静默跳过")
    elif exempt_value is not None and health_target not in str(exempt_value):
        problems.append(
            f"Dockerfile 的 HEALTHCHECK 打 {health_target}，而 compose 只豁免 {exempt_value}"
            " —— 那一格一改，容器就永远报 unhealthy 而应用其实好着"
        )
    # (c) Caddyfile 的 `{$NAME}` 占位符必须由 compose 喂给 caddy 那个服务：
    #     键名检查从前**只扫 app 段**，caddy 段整段在范围外，改名漂移没人问。
    caddy_file = ROOT / "deploy" / "Caddyfile"
    # 同样按结构取 `services.caddy.environment`，不再 `^  caddy:` 认死两空格地切块
    # （缩进一变就切不到而**静默不问**这一格 —— 与上面几处同一个坑）。
    caddy_env = _compose_service_env(compose, "caddy")
    if caddy_file.exists() and caddy_env is not None:
        caddy_lines = [
            line
            for line in caddy_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            if not line.lstrip().startswith("#")  # 注释里的 `{$VAR}` 是解释，不是真的插值
        ]
        wanted = set(re.findall(r"\{\$([A-Z][A-Z0-9_]+)", "\n".join(caddy_lines)))
        given = set(caddy_env)
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


#: 刻意**只走审批门、不进出厂白名单**的工具（`R102-37` 的拍板，2026-10-12）。
#:
#: `run_command` 的"批准语义"不属于开箱即用的对话轮：出厂角色白名单一律不含它，
#: 命令要跑必须过「设置——命令执行」的审批档（`core/common/approvals.py`）。
#: 反向差集（declared - wanted）因此对它是**预期内命中**，不是死工具的证据。
#: 但豁免必须自带判据 —— 它在 `check_role_whitelists_resolve` 里被三只钉子看着：
#: 声明在场、审计词表在场、出厂白名单不碰它。任一失守即红。
APPROVAL_GATED_TOOLS: frozenset[str] = frozenset({"run_command"})


def check_role_whitelists_resolve() -> None:
    """Every tool name in a built-in role's whitelist must resolve to a declared tool.

    The built-in role's whitelist listed `list_domains` / `list_roles` while
    `core/tools/builtin.py` was still an empty docstring - a permission list pointing at
    nothing, which reads as working code (技术评审与决策.md 「实现对齐核查」B1).

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
    # 开箱即用的对话轮）。**拍板（2026-10-12）：维持"只走审批门"，收成显式豁免** ——
    # 豁免不是死缓，三只钉子看着它（任一失守即红）：
    #   ① 豁免的工具必须真实声明（`@tool("...")` 在场）—— 删了工具忘了删豁免，当天红；
    #   ② 豁免的工具必须在审计动作词表里 —— 审批门的工具面必须留痕，词表删词即红；
    #   ③ 豁免的工具不得出现在任何出厂白名单里 —— 哪天决定开箱给它，豁免就过期了。
    from rolecard_agent.base.audit import AUDIT_ACTIONS  # noqa: PLC0415

    gated = APPROVAL_GATED_TOOLS
    undeclared_gated = sorted(gated - declared)
    unlogged_gated = sorted(gated - AUDIT_ACTIONS)
    leaked_gated = sorted(gated & wanted)
    gated_problems = (
        [f"豁免工具未声明: {undeclared_gated}" if undeclared_gated else ""]
        + [f"豁免工具不在审计词表: {unlogged_gated}" if unlogged_gated else ""]
        + [f"豁免工具已进出厂白名单（豁免过期）: {leaked_gated}" if leaked_gated else ""]
    )
    gated_problems = [p for p in gated_problems if p]
    if gated_problems:
        fails.extend(f"approval-gated tool exemption invalid: {p}" for p in gated_problems)
        out("role whitelist coverage", False, "; ".join(gated_problems))
    else:
        unused = sorted(declared - wanted - gated)
        if declared and not wanted:
            fails.append("tools are declared but no built-in role references any of them")
            out("role whitelist coverage", False, "no whitelist references any declared tool")
        elif unused:
            warns.append(
                f"role whitelist coverage: 出厂白名单没人引用的工具 {unused}"
                " —— 是死工具还是刻意只走审批门，需要一次拍板（R102-37）"
            )
        else:
            out(
                "role whitelist coverage",
                True,
                f"反向差集只余刻意豁免的审批门工具 {sorted(gated)}（三只钉子在场）",
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
