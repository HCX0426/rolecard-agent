"""Repo consistency check. Run in CI and before every commit.

Why this exists: as the project went through several rounds of revision, documents and
code drifted apart (a renamed config key still referenced in a docstring, stale project
names, requirements files pulling in scope the version does not need). Eyeballing does
not catch these - the checker caught one on its very first run.

Usage:
    python scripts/check_consistency.py        # exits 1 on failure, 0 on pass
"""

from __future__ import annotations

import ast
import datetime
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]

fails: list[str] = []
warns: list[str] = []
passed = 0

#: **任何一条能跑起完整产品的路径都得装的那五族** —— 两处尺子共用一张表（10-01）：
#: `installer scope parity` 问"每条安装路径装没装齐这五份"，
#: `spec runtime deps declared` 问"随包后端要收的每一族模块，在这五份里有没有声明出处"。
#: 分开写两份的话，漏的那一格恰好是 MCP 这次走的那道缝：五份对四条路径是齐的，
#: 而 spec 要的 `mcp` 这一族只在传递依赖里活着，没有任何一处写着"这条路径装过它"。
RUNTIME_REQ_FILES = (
    "requirements.txt",
    "requirements-api.txt",
    "requirements-rag.txt",
    "requirements-cloud.txt",
    "requirements-mcp.txt",
)


def _package_names(lines: list[str]) -> set[str]:
    """requirements 风格的那些行 → 发行包名集合（注释与 extras/版本比较符都剥掉）。"""
    names: set[str] = set()
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(re.split(r"[<>=!\[:]", line, maxsplit=1)[0].strip().lower())
    return names


def _runtime_declared_names() -> set[str]:
    """那五份运行时 requirements 里点名装过的发行包。"""
    declared: set[str] = set()
    for name in RUNTIME_REQ_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        declared |= _package_names(path.read_text(encoding="utf-8", errors="ignore").splitlines())
    return declared

# Directories that must never be walked. `ROOT.rglob("*.py")` happily descends into a
# virtualenv, which made the line-budget metric report 300k lines of site-packages instead
# of the project (C24).
IGNORED_DIRS = {
    ".git",
    ".venv",
    ".venv-dev",
    ".venv-ocr",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
    "data",
    "node_modules",
    # `build/` 是本地脚手架（sidecar 解包、安装包、日志、临时克隆），`.gitignore` 整目录挡着。
    # 09-26 干净克隆彩排时我在 build/ci-clone 里放了一份仓库副本，两条检查立刻把**副本**里
    # 的脚本当成待检文件判红 —— 与 C24 那次"把 site-packages 数成项目代码"同一族：
    # 尺子必须只看这一个世界。
    "build",
    # `out`/`release`（`R102-35`）：shell 的构建产物目录，各自 .gitignore 挡着 —— 从前
    # 判据读进 gitignore 的构建树，同一份码在干净 clone 打 427、在本机打 446。
    "out",
    "release",
}


_file_walk_cache: dict[tuple[str, ...], list[pathlib.Path]] = {}


def iter_files(*suffixes: str) -> list[pathlib.Path]:
    """Repo files, skipping environments, caches and generated data.

    结果按 suffix 集合缓存，且遍历时**原地剪枝** IGNORED_DIRS 子树：8 个检查各调一次、
    每次全量 rglob（frontend/node_modules 几万文件照走，只是最后被过滤）曾把整份脚本
    拖到 17s（门禁耗时盘点）。目录在单次运行内不会变，缓存 + 剪枝都是纯收益。
    """
    key = tuple(sorted(suffixes))
    cached = _file_walk_cache.get(key)
    if cached is not None:
        return cached
    found: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 原地剪枝：巨树（node_modules / .venv / data …）整个不进入，而不是进入后再过滤。
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            if suffixes and pathlib.Path(name).suffix not in suffixes:
                continue
            found.append(pathlib.Path(dirpath) / name)
    _file_walk_cache[key] = found
    return found


def strip_comments(text: str) -> str:
    """Remove SQL (`--`) and Python (`#`) comment lines and trailing comments.

    Needed by the domain-isolation check: prose *about* a concept must not be mistaken for
    a definition *of* it. Writing "there is no user table here" kept tripping it.
    """
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(("--", "#")):
            continue
        lines.append(line.split("--", 1)[0].split("#", 1)[0])
    return "\n".join(lines)


def out(label: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
    print(f"{'OK  ' if ok else 'FAIL'} {label}{(' :: ' + detail) if detail else ''}")


def check_pyproject() -> None:
    try:
        pp = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        out("pyproject.toml", False, str(exc))
        fails.append(f"pyproject.toml unreadable: {exc}")
        return
    proj = pp["project"]
    ok = proj["name"] == "rolecard-agent" and proj["requires-python"] == ">=3.13"
    detail = f"name={proj['name']} py={proj['requires-python']} deps={len(proj['dependencies'])}"
    out("pyproject.toml", ok, detail)
    if not ok:
        fails.append("pyproject.toml name / requires-python drifted")

    tool = pp.get("tool", {})
    ok = "ruff" in tool and "pytest" in tool
    out("pyproject tooling", ok, f"ruff={'ruff' in tool} pytest={'pytest' in tool}")
    if not ok:
        fails.append("pyproject.toml is missing ruff / pytest config")


def check_requirements_scope() -> None:
    """requirements.txt is the v1 kernel set. v2 deps must stay out of it."""
    lines = [
        line.split("#", 1)[0]
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    ]
    body = "\n".join(lines)
    leaked = [d for d in ("rapidocr", "opencv", "chromadb", "fastapi", "uvicorn") if d in body]
    out("requirements.txt scope", not leaked, f"leaked: {leaked}" if leaked else "v1 core only")
    if leaked:
        fails.append(f"requirements.txt pulls v2-only deps: {leaked}")


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
        ROOT / "scripts" / "check_consistency.py",
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


def check_config_contract() -> None:
    """.env.example must expose every key config.py advertises.

    正则覆盖的**全部**前缀都要在这里列出来。此前只覆盖 MODEL_ / OBS_ / 路径三项，
    于是 `CONTEXT_MAX_CHARS` / `TOOL_TIMEOUT_SECONDS` / `AUTH_TRUSTED_PROXIES` 这类新键
    即使漏进 .env.example 也不会被发现 —— 一个只检查部分键的契约检查比没有更容易骗人
    （代码审查报告（第二轮）L4）。
    """
    env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    env_keys = set(
        re.findall(
            r"^([A-Z][A-Z0-9_]+)=",
            env_text,
            flags=re.M,
        )
    )
    cfg_text = (ROOT / "src" / "rolecard_agent" / "config.py").read_text(encoding="utf-8")
    prefixes = (
        "MODEL_[A-Z_]+",
        "OBS_[A-Z_]+",
        "AUTH_[A-Z_]+",
        "CONTEXT_[A-Z_]+",
        "TOOL_[A-Z_]+",
        "WEB_[A-Z_]+",
        "WORKSPACE_[A-Z_]+",
        "TAVILY_[A-Z_]+",
        "SAUCENAO_[A-Z_]+",
        "OCR_[A-Z_]+",
        "RAG_[A-Z_]+",
        # 这两个前缀此前漏在表外：SILICONFLOW_API_KEY / MCP_SERVERS 明明在 config.py 里解析，
        # 却从不被契约检查覆盖 —— 漏一个前缀就是"这一族键可以随便漂"（架构审计报告 §3）。
        "SILICONFLOW_[A-Z_]+",
        "MCP_[A-Z_]+",
        "EXTRACT_[A-Z_]+",
        "LANGSMITH_[A-Z_]+",
        "MEMORY_[A-Z_]+",
        "AGENT_[A-Z_]+",
        "REACHOUT_[A-Z_]+",
        "FILE_WATCH_[A-Z_]+",
        # D②-4 起新增的一族：桌面壳安装包的托管目录（SHELL_RELEASE_DIR）。
        "SHELL_[A-Z_]+",
    )
    pattern = r"\b(" + "|".join(prefixes) + r"|SQLITE_PATH|CHROMA_PATH|UPLOAD_DIR)\b"
    cfg_keys = set(re.findall(pattern, cfg_text))
    missing = sorted(k for k in cfg_keys if k not in env_keys and k != "LANGSMITH_PROJECT")
    detail = f"missing: {missing}" if missing else f"{len(env_keys)} keys aligned"
    out("config contract", not missing, detail)
    if missing:
        fails.append(f".env.example missing keys documented in config.py: {missing}")

    # 键名对齐只保证"这一族没漏"，**保证不了值没漂**。09-26 轮 R26-10 实测：
    # `MEMORY_EXTRACT_ROUNDS` 代码默认 5，`.env.example` 长期写着 12 —— 新克隆照抄就等于
    # 把自动提取调回"几乎不触发"，而键名检查一路绿。所以这里把 example 的值和 `Settings`
    # 的真实默认对一遍。真要故意给一个非默认值，就写进下面的豁免表并说清理由 ——
    # "逼出一个书面理由"正是这条检查的全部价值。
    _ENV_DEFAULT_EXEMPT = {
        # 空 = 走 config 里的出厂端点（example 那三行注释就是这么解释的），不是漂移。
        "SILICONFLOW_BASE_URL": "留空 = 用出厂默认端点（见 .env.example 该键上方注释）",
    }
    import sys  # noqa: PLC0415 - 与 main() 里同样的延迟导入姿势

    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.config import Settings  # noqa: PLC0415

    def _norm(value: object) -> str:
        text = str(value).strip().strip("\"'").replace("\\", "/")
        return text[2:] if text.startswith("./") else text

    pairs = re.findall(r'\("([A-Z][A-Z0-9_]+)",\s*"([a-z_0-9]+)"\)', cfg_text)
    # 走 `from_env` **独立分支**的那几条（09-28 轮 `R28-14b`）：它们不在上面那张
    # ("KEY","field") 表里，于是键名检查绿、默认值检查绿，而这一族的值漂移零兜底 ——
    # 正是发现 9 留给门禁的那块盲区。补成表，是为了让下面那段比较**只有一份实现**，
    # 而不是再写一套"看起来一样"的逻辑。
    pairs += [
        ("MODEL_THINKING", "model_thinking"),
        ("MODEL_FALLBACKS", "model_fallbacks"),
        ("MODEL_THINKING_MODELS", "model_thinking_models"),
        ("MCP_SERVERS", "mcp_servers"),
    ]
    env_values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", env_text, flags=re.M))
    settings = Settings()
    drifted: list[str] = []
    for key, field in pairs:
        if key not in env_keys or key in _ENV_DEFAULT_EXEMPT or not hasattr(settings, field):
            continue
        default = getattr(settings, field)
        example = env_values.get(key, "")
        if isinstance(default, bool):
            same = example.strip().lower() in ({"true", "1"} if default else {"false", "0"})
        elif default is None:
            same = example.strip() == ""
        elif isinstance(default, (list, tuple)):
            # 逗号分隔的列表：`MODEL_FALLBACKS=` 那个空串就是"没有"，与 `[]` 同一个意思。
            want = [str(x).strip() for x in default]
            got = [x.strip() for x in example.split(",") if x.strip()]
            same = want == got
        else:
            same = _norm(default).lower() == _norm(example).lower()
            if not same:
                try:  # 120 与 120.0 是同一个数，不是漂移
                    same = float(default) == float(example)
                except (TypeError, ValueError):
                    same = False
        if not same:
            drifted.append(f"{key}: example={example!r} 默认={default!r}")
    out("env default values", not drifted, "; ".join(drifted[:4]) if drifted
        else f"{len(pairs) - len(_ENV_DEFAULT_EXEMPT)} 个默认值与 example 一致")
    if drifted:
        fails.append(f".env.example values drifted from Settings defaults: {drifted}")

    # `MODEL_BACKENDS` / `MCP_SERVERS` 是 JSON blob，没有"默认值"可对 —— 它们能漂的是另一件事：
    # **照着抄的那一段解析不了，或者过了 json 却过不了运行时校验**。
    # 注释行也算（那是给人复制的那一行，不是散文）。
    from pydantic import ValidationError  # noqa: PLC0415

    from rolecard_agent.config import McpServerConfig, ModelBackend  # noqa: PLC0415

    bad_json: list[str] = []
    for line in env_text.splitlines():
        stripped = line.lstrip("# ").strip()
        for key, model in (("MODEL_BACKENDS", ModelBackend), ("MCP_SERVERS", McpServerConfig)):
            if not stripped.startswith(f"{key}="):
                continue
            raw = stripped.split("=", 1)[1].strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                where = "注释行" if line.lstrip().startswith("#") else "生效行"
                bad_json.append(f"{key}（{where}）不是合法 JSON：{exc}")
                continue
            try:
                if isinstance(parsed, dict):
                    {k: model(**v) for k, v in parsed.items()}
                else:
                    [model(**item) for item in parsed]
            except (ValidationError, TypeError, ValueError) as exc:
                bad_json.append(f"{key} 能解析但过不了运行时校验：{exc}")
    ok_json = not bad_json
    out(
        "env example json valid",
        ok_json,
        "; ".join(bad_json[:2]) if bad_json else "两段 JSON 示例可解析可校验",
    )
    if bad_json:
        fails.append(f".env.example JSON examples are unusable: {bad_json}")


def check_installer_scope() -> None:
    """四个安装入口必须装**同一组运行时依赖**（09-28 轮 `R28-11`/`R28-12` 那一族的闸）。

    同一个坑这轮踩了三次，每次都是"某一条安装路径少装一族，而本机 .venv 恰好装过"：
      * 镜像少 `requirements-cloud.txt` —— CI 第一发真跑就红在"配任何 OpenAI 兼容端点保存即 500"，
        因为容器里没有 Ollama，云端 key 本来就是主用例；
      * `install.bat` 只装 base+api+rag，cloud 那一族是结尾一句 `echo` 提示；
      * README 快速开始第 2 步同病（决策 5 那轮补的）。
    少装一族的症状永远不在装的人自己身上（他的机器早就装过了），所以这条只能机器查。

    判据：`RUNTIME_REQ_FILES` 是"任何一条能跑起完整产品的路径都得有"的那五份，
    逐个入口现读它引用了哪些 `requirements*.txt`。dev / ocr / package **不在表内**：
    那几份按形态有意分开装（dev 不进生产运行树、OCR 栈必须独立 venv、package 只有打包机要）。

    **`requirements-mcp.txt` 于 2026-10-01 从"按形态分开装"挪进这张表**，理由不是口味而是两个
    出货形态都已经装了它：随包后端 09-29 起带它（`R28-34`：spec 缺一条就拒绝出产物），镜像
    10-01 起带它（`R28-53`：这一族病第三次红，前两次都是"本机绿、产物缺"）。既然发出去的两份
    都装着，"源装形态可以不装"就只是同一条产品能力的第三种拼法 —— 让它继续留在表外的代价，
    正是这条检查存在要防的那件事。

    另一半（同一轮 `R28-32` 加的）：**磁盘上每一份 requirements*.txt 都要在这两张表里之一**。
    新增一族却没人分类，症状与"少装一族"完全一样而方向相反 —— 它被某条路径默默需要着，
    却没有任何一处写着"这条路径装过它"。空理由不算理由，照样红。
    """
    # 判据读的是**每一条真跑 pip 的命令**，不是"文件里提过这个名字"。
    # 为什么不是全文压扁比一次（第一版就是这么写的，被一次变异当场否证）：Dockerfile 的
    # `COPY requirements.txt ... requirements-mcp.txt ./` 那一行**六个文件名都在**，而下一行
    # `RUN pip install` 只装四份 —— 全文比的话这一路永远绿，而那正是 `R28-53` 的缺陷本尊
    # （镜像里 MCP 永远 fail-open）。所以：从 `pip install` 那一行起，把行尾 `\` 的续行接上，
    # 只对**这一条命令**问它装齐了没有；一条文件里有几条就挨个问几条（ci.yml 有三条）。
    surfaces = {
        "install.bat": "install.bat",
        "Dockerfile": "Dockerfile",
        "ci.yml": ".github/workflows/ci.yml",
        "README": "README.md",
    }

    def _pip_commands(text: str) -> list[str]:
        """每条 `pip install` 命令，续行已接上（续行符可能是 \\ 或 Windows 的 ^）。

        **注释行一律跳过**：Dockerfile 里"为什么装这一族"那段散文就写着 pip install 这几个字，
        把它当命令读，这条尺子会把自己的解释文字报成缺陷（第一趟就是这么红的）。
        """
        lines = text.splitlines()
        cmds: list[str] = []
        for i, line in enumerate(lines):
            if "pip install" not in line:
                continue
            probe = line.strip()
            if probe.startswith("#") or probe.startswith("//") or probe.lower().startswith(
                ("rem ", "::")
            ):
                continue
            buf = line
            j = i
            while buf.rstrip().endswith(("\\", "^")) and j + 1 < len(lines):
                j += 1
                buf += " " + lines[j]
            cmds.append(" ".join(buf.split()))
        # 只问"装一套依赖"的那些命令：`python -m pip install --upgrade pip` 提升级 pip 自己，
        # 它本来就不该带 -r，把它算进来等于给每条 CI job 都白造一条红。
        return [c for c in cmds if "-r requirements" in c]

    missing: list[str] = []
    for label, rel in surfaces.items():
        path = ROOT / rel
        if not path.exists():
            missing.append(f"{label} 这个入口文件不见了（{rel}）")
            continue
        cmds = _pip_commands(path.read_text(encoding="utf-8", errors="ignore"))
        if not cmds:
            missing.append(f"{label} 里找不到任何 pip install 命令")
            continue
        for cmd in cmds:
            missing += [
                f"{label} 有一条 pip install 没装 {req}（{cmd[:70]}…）"
                for req in RUNTIME_REQ_FILES
                if req not in cmd
            ]
    out(
        "installer scope parity",
        not missing,
        "; ".join(missing[:4])
        if missing
        else f"{len(surfaces)} 个安装入口都覆盖 {len(RUNTIME_REQ_FILES)} 份运行时依赖",
    )
    if missing:
        fails.append(f"installer surfaces miss runtime deps: {missing}")

    # 另一半：磁盘上每一份 requirements*.txt 都要在两张表里之一（空理由不算理由）。
    SEPARATE_BY_SHAPE = {
        "requirements-dev.txt": "开发/CI 依赖，不进生产运行树",
        "requirements-ocr.txt": "OCR 栈不进运行树，必须独立 venv（该文件开头有现行理由）",
        "requirements-package.txt": "只有打包机要（PyInstaller，见 ci.yml 的 windows-release）",
        "requirements-package-ocr.txt": "只有打随包 OCR worker 时要（PyInstaller 装进 .venv-ocr，"
        "见 scripts/build_ocr_worker.py；10-03 起装机版靠那份产物才有本地 OCR）",
    }
    unclassified = [
        p.name
        for p in sorted(ROOT.glob("requirements*.txt"))
        if p.name not in RUNTIME_REQ_FILES and p.name not in SEPARATE_BY_SHAPE
    ]
    blank = [name for name, why in SEPARATE_BY_SHAPE.items() if not why.strip()]
    ok_class = not unclassified and not blank
    out(
        "requirements 分类",
        ok_class,
        "; ".join([f"没分类：{unclassified}", f"空理由：{blank}"][:2])
        if not ok_class
        else f"{len(RUNTIME_REQ_FILES)} 份运行时 + {len(SEPARATE_BY_SHAPE)} 份按形态分开，都有理由",
    )
    if not ok_class:
        fails.append(f"requirements files missing a classification: {unclassified or blank}")


def _imported_modules(code: str) -> list[str]:
    """从一段 `python -c` 的串里只取**被 import 的模块名**。

    为什么不是 `re.findall(r"(?:import|from)\\s+(\\w+)")` 一把梭：那样
    `from rolecard_agent.config import DEFAULT_SILICONFLOW_BASE_URL` 会报出**两个**名字，
    而后面那个是被导入的**符号**、不是模块 —— 第一版就是这么把一条正确诊断说成两条的，
    读数里混进不属于模块的东西，下次真要查"哪个依赖漏了"时就得先分辨哪些是噪声。
    """
    mods: list[str] = []
    from_pat = re.compile(r"\bfrom\s+([A-Za-z_][\w.]*)\s+import\b")
    for m in from_pat.finditer(code):
        mods.append(m.group(1))
    # 把 `from X import a, b` 那一段（含 `import` 这个词本身）整段摘掉，剩下的才交给下面的
    # 裸 `import` 匹配 —— 否则 a/b 会被当成模块再抓一遍（第一版正是这样把一条正确诊断
    # 说成两条的）。
    rest = from_pat.sub(" ", code)
    for m in re.finditer(r"\bimport\s+([A-Za-z_][\w.,\s]*?)(?=[;)]|$|\bprint\b|\bimport\b)", rest):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if name:
                mods.append(name)
    # 顺序不承载意义（`from X import …` 要先整段摘掉才能不抓到符号名，摘的动作天然打乱原序），
    # 所以归一化成去重排序 —— 免得调用方把"两个名字换了个位"读成一次行为变化。
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


#: 操作系统给的那些变量：它们不是本应用的契约，写进 .env.example 反而误导人以为可以设。
_PLATFORM_ENV = {"LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "HOME", "PATH", "USERPROFILE"}


def _env_names_read_in_src(src: pathlib.Path) -> dict[str, str]:
    """`src/**` 里通过 `os.environ` 读到的变量名 → 第一处 `文件:行`。

    三种写法都算：`os.environ.get("X", …)`、`os.environ["X"]`、`X in os.environ`。
    只数**字符串常量**那一种（变量名是拼出来的读不出来 —— 本仓没有那种写法，
    真要有人写，这条尺子会漏，漏在明处）。
    """
    found: dict[str, str] = {}
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("environ.get"):
                if node.args and isinstance(node.args[0], ast.Constant):
                    name = node.args[0].value
            elif isinstance(node, ast.Subscript) and ast.unparse(node.value).endswith("environ"):
                if isinstance(node.slice, ast.Constant):
                    name = node.slice.value
            elif isinstance(node, ast.Compare) and ast.unparse(node.left).endswith("environ"):
                for comp in [node.left, *node.comparators]:
                    if isinstance(comp, ast.Constant):
                        name = comp.value
            if (
                isinstance(name, str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", name)
                and name not in found
            ):
                found[name] = f"{rel}:{getattr(node, 'lineno', 0)}"
    return found


def check_startup_env_documented() -> None:
    """`src` 里真读的每个环境变量，都必须对谁**说出来过**。

    为什么单独立一条（10-01，部署面盘点，台账 `R28-60`）：`.env.example` 头部自称
    "契约归属：config.py"，而 `RUN_API_HOST` / `RUN_API_PORT` / `RUN_API_RELOAD` /
    `FRONTEND_DIST` / `ROLECARD_PARENT_PID` 这一族**不经 config.py 的映射表**
    （启动器与包内布局直接读 `os.environ`），于是它们在契约文件里一格都没有 ——
    只在代码注释里活着。
    已有的 `config contract` 那条查不到它们，因为它比的是 config 表 ↔ example。
    这一条补的是"代码认但没人知道"那一半。

    范围只到 `src/**`：`scripts/` 里那些 `STOP_PROBE_BASE` / `LIVE_DB_PATH` 是取证脚本的私有
    开关，不是部署契约，写进 example 只会让人以为设了它就能改变应用行为。
    """
    sys.path.insert(0, str(ROOT / "src"))
    env_text = (ROOT / ".env.example").read_text(encoding="utf-8", errors="ignore")
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]{2,})=", env_text, flags=re.M))
    documented |= set(re.findall(r"#\s*([A-Z][A-Z0-9_]{2,})[=\s]", env_text))
    cfg_path = ROOT / "src" / "rolecard_agent" / "config.py"
    cfg_text = cfg_path.read_text(encoding="utf-8", errors="ignore")
    documented |= set(re.findall(r'"([A-Z][A-Z0-9_]{2,})"', cfg_text))

    read = _env_names_read_in_src(ROOT / "src" / "rolecard_agent")
    missing = sorted(
        f"{name}（{read[name]}）"
        for name in read
        if name not in documented and name not in _PLATFORM_ENV
    )
    detail = (
        f"src 读的 {len(read)} 个 env 名全部在 example 或 config 映射表里说过"
        if not missing
        else f"代码在读、契约文件里却一格没有：{missing[:6]}"
    )
    out("startup env documented", not missing, detail)
    if missing:
        fails.append(f"undocumented env read by src: {missing}")


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


def _readings_head_is_current(current_head: str, readings_head: str) -> bool | None:
    """读数的 head 与当前 HEAD 的合法关系（见 check_readme_headline_numbers 里的两条形状）。

    读数里的 head 可能是短 sha（gate 写入时截过）：按前缀比，长度以读数那格为准。

    **三态**（10-03 加，因为 CI 上真退化成问不出过）：
      * `True` —— 形状①或形状②成立；
      * `False` —— 父提交读得到，而 HEAD 与父之间**动了代码** ⇒ 读数确实不属于现在这份代码；
      * `None` —— 需要父提交却读不到（浅克隆 / 没有 git / 根提交）⇒ **问不出**。
    把"问不出"报成红，就是让 CI 用它自己的环境差异指控代码陈旧（那次红的是归属，
    而 README 与读数两边都是 1469）；按本仓口径，只有**确认负**才拦得住人。
    """
    if current_head.startswith(readings_head):
        return True
    try:
        parent = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - 读不到父提交 ⇒ 形状②无法判定，这是"问不出"不是"不符"
        return None
    if not parent:
        return None
    if not parent.startswith(readings_head):
        return False
    try:
        changed = subprocess.run(
            ["git", "diff", "--name-only", parent, current_head],
            cwd=ROOT,
            capture_output=True,
            text=True,
            # utf-8 + splitlines：本仓有**带空格的中文文件名**，默认编码/`split()` 会把
            # `架构审计（…轮）.md` 拆成两截（`gate.py` 的 `_git` 是同一个教训，那里写着
            # "encoding 不是可选的"）。路径一律 posix，与 `docs_only` 直接对得上。
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout.splitlines()
    except Exception:  # noqa: BLE001 - 同上：这条 diff 问不出，也不能判红
        return None
    docs_only = {"docs/gate-readings.json", "README.md", "docs/架构审计索引.md"}
    return all(path in docs_only for path in changed)


def check_readme_headline_numbers() -> None:
    """README 首屏那组数必须等于**上一趟门禁量到的**那份读数。

    为什么立它（10-01，台账 `R28-55` 的第二次收口）：那条缺陷记的是"457 + 63 + 24 条断言"
    漂成了 1301 / 336 / 40 —— 我当时只把数字重测了一遍。可同一天下午我就又把它漂了一次
    （加完用例，1301 变 1320），说明**手抄的数字不管测得多准都会再漂**。真正的修法是把
    "当前真值"这件事交给量它的那个人：`gate.py` 每跑一趟就写 `docs/gate-readings.json`
    （pytest / vitest / consistency 三步的输出里现读，**按键合并**，每个键自带测量时刻），
    这条断言拿 README 首屏去比它。

    那份读数**入库**而不是放 `build/`：09-30 那条规矩说尺子的判据里不许有 gitignore 的东西 ——
    判据一读暂存区，问的就不是"这格数是不是真的"，而是"这台机器上有没有这个文件"（CI 上它若
    不存在，这条永远只会打印"跳过"，等于没有）。它比的是"文档 vs 最近一次实测记录"，所以
    它拦得住"把 README 改成一个没量过的数"，拦不住"加了用例却两样都不更新" —— 后者由跑门禁
    的那个人看见（他那一趟会把新数写进读数，于是 README 当场对不上）。

    少一个键比多一个键更响：某一步**跑了却没量到**时 `gate.py` 落 `<key>_unreadable` 记号，
    这一格判红。第一版就是少了 `backend_tests` 这个键而全绿 —— 那两个"读不到"的根（pytest 的
    `-q` 叠成 verbosity −2、vitest 带着 ANSI 色）见 `gate.py` 的 `_ANSI` 与 STEPS 注释。

    文件整个不存在才跳过并说明（与"未知不拦"同一条纪律）：没跑过门禁不是缺陷，把它判红只会让
    人先关掉这条。跳过的条数会上屏，不会被读成"通过"。
    """
    readme = ROOT / "README.md"
    readings_path = ROOT / "docs" / "gate-readings.json"
    if not readings_path.exists():
        out(
            "README headline numbers",
            True,
            "跳过：还没有 docs/gate-readings.json —— 跑一次 scripts/gate.py 就有读数了",
        )
        warns.append("README headline numbers: 本趟无门禁读数可比（跳过，不代表通过）")
        return
    try:
        readings = json.loads(readings_path.read_text(encoding="utf-8"))
    except ValueError:
        out("README headline numbers", False, "gate-readings.json 读不出 JSON（产物坏了，另说）")
        fails.append("gate-readings.json unreadable")
        return
    text = readme.read_text(encoding="utf-8", errors="ignore")
    want = {
        "backend_tests": (r"\*\*(\d+) 个后端测试", "后端测试数"),
        "frontend_tests": (r"(\d+) 个前端测试", "前端测试数"),
        "coverage_percent": (r"覆盖率 ([\d.]+)%", "覆盖率"),
        # 「一致性有几条断言」**不在这里比**：那把尺子的条数里含"比对 README"这一条自己，
        # 于是 README 漂 ⇒ 这一条红 ⇒ 读到的条数少 1 ⇒ 那句数变成两处错。自指的东西不能当读数，
        # 退回门禁输出里那一行 `assertions: N passed, M failed` —— 散文不抄它（`R28-69`），
        # 而"有没有红"本来就由 `gate` 的退出码负责，写在文档里那句只是复述。
    }
    drift: list[str] = []
    checked = 0
    for key, (pattern, label) in want.items():
        if f"{key}_unreadable" in readings:
            # 「那一步跑了却没量到」是**确认的负面**，不是未知：与"这档没跑那一步"（键压根不在
            # 文件里，跳过）分得很清。漏掉这一格，README 那个数就会在被废掉的尺子下面永远绿。
            drift.append(
                f"{label}：{readings[f'{key}_unreadable']} 那一步跑过却没读到数"
                "（步骤的输出格式或参数变了 —— 先修读数，不要改 README）"
            )
            continue
        red_at = readings.get(f"{key}_red_at")
        if red_at:
            # `R102-36` 半条（10-03 收）：红跑不写值、但留痕。这里只上屏提醒、不进红 ——
            # 值本身仍是可信的（上一次**绿跑**量到的那个），病是"旧值被一次红跑钉在原地
            # 而没有任何一格说明最近一趟是红的"；修法就是把那格说明补上（warn 即它的位置）。
            warns.append(
                f"{label}：最近一趟（{red_at}）这一步红过 —— 本格仍是"
                f"{'上一次绿跑' if key in readings else '此前从未'}量到的值"
                "（先修那一步让它重量，别改 README）"
            )
        if key not in readings:
            continue
        checked += 1
        hit = re.search(pattern, text)
        if not hit:
            drift.append(f"README 里找不到「{label}」那一格（读数说 {readings[key]}）")
            continue
        if hit.group(1) != str(readings[key]):
            drift.append(f"{label}：README 写 {hit.group(1)}，上一趟门禁量到 {readings[key]}")
    if not checked:
        out("README headline numbers", True, "读数文件里一个可比项都没有（跳过，不代表通过）")
        return
    # 读数属于哪个 HEAD（`R102-36` 的主体半边）：从前只比"README ↔ 旧读数"，而那份读数可能
    # 是几个提交之前量的 —— 文档比代码旧 N 条照样打绿（实测 1343 vs 1345 就这么绿过）。
    # 合法的两种形状（再旧就红，先重跑一趟门禁刷新读数，而不是改 README 去凑旧世界）：
    #   ① readings.head == HEAD（读数就是在当前提交量的）；
    #   ② readings.head == HEAD^ 且 HEAD 与父之间只动了读数/README/审计索引 ——
    #      那是"跑完门禁、把读数与 README 对齐"的跟进提交本身。没有这半条，任何提交
    #      都会让读数变旧一格，判据就永远差一个提交（自指死锁）。
    readings_head = str(readings.get("head") or "")
    if readings_head:
        try:
            current_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except Exception:  # noqa: BLE001 - 没有 git 的环境（源码包）没法比，按跳过处理
            current_head = ""
        if current_head:
            verdict = _readings_head_is_current(current_head, readings_head)
            if verdict is False:
                drift.append(
                    f"读数的 HEAD 是 {readings_head[:12]}，当前 HEAD 是 {current_head[:12]}"
                    " —— 这份读数不属于现在的代码：先重跑一趟门禁刷新读数，再对 README"
                )
            elif verdict is None:
                # 问不出（浅克隆读不到 HEAD~1，或没有 git）。按本仓口径这不拦，但**必须出声** ——
                # 一声不吭地放行，下一次真漂移就混在"它本来也这样"里过去了。
                warns.append(
                    f"README headline numbers: 读数记在 {readings_head[:12]} 而 HEAD 是 "
                    f"{current_head[:12]}，这一趟**读不到父提交**（浅克隆？fetch-depth<2？）"
                    " ⇒ 归属那一问按 unknown 放行，数本身仍比过"
                )
    stamps = "，".join(
        f"{label} {readings[key]}@{readings.get(f'{key}_at', '?')}"
        for key, (_, label) in want.items()
        if key in readings
    )
    out(
        "README headline numbers",
        not drift,
        # 每个键**自带**测量时刻：覆盖率只有 ci/full 那趟量得到，快门禁只更新用例数 ——
        # 共用一个时间戳会把"上周的覆盖率"洗成"刚才量的"（`gate.py` 的 `_write_readings` 同理）。
        f"{checked} 项与 gate-readings.json 一致（读数 HEAD {readings.get('head', '?')}）：{stamps}"
        if not drift
        else "; ".join(drift[:4]),
    )
    if drift:
        fails.append(f"README headline numbers drift: {drift}")


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


#: api 层允许 import 具体域的**登记接缝**（`R102-10`：集单调最严——除这两处外即红）。
#: 每条的"为什么"就写在这里；登记过时（那个文件不再 import 具体域了）也红 ——
#: 与 `route access` 的"清单里没有死条目"同一条纪律。
API_DOMAIN_SEAMS = {
    "src/rolecard_agent/api/main.py": "宿主侧域接线：具体域类只在这里交给装配根（唯一一处）",
    "src/rolecard_agent/api/routers/records.py": "域通用路由：按 kind 分派各域的抽取器与异常名",
}


def check_api_domain_seams() -> None:
    """api 层 import 具体域只许发生在登记接缝上（`R102-10` 那把迟到的尺子）。

    `deps.py` 从前自述"api 层不 import 具体域"，而 `main.py` 与 `records.py` 就在
    import —— 分叉处正好在尺子的覆盖面外（`core no domain token` 只管 core）。修法不是
    把那两句改没，而是承认**两个接缝是刻意的**（装配一处、分派一处），再立这把尺子把
    "第三处"挡在门外：新增一个 import 具体域的 api 文件 = 红，必须来这里登记并写理由。
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
            ):
                hits.setdefault(rel, set()).add(node.module)
    unregistered = sorted(rel for rel in hits if rel not in API_DOMAIN_SEAMS)
    stale = sorted(rel for rel in API_DOMAIN_SEAMS if rel not in hits)
    ok = not unregistered and not stale
    detail = (
        f"{len(hits)} 处接缝全在登记内（{', '.join(sorted(hits))}）"
        if ok
        else f"未登记：{unregistered}；登记过时（已不再 import 具体域）：{stale}"
    )
    out("api domain seams", ok, detail)
    if unregistered:
        fails.append(
            "api imports concrete domains outside registered seams: "
            f"{unregistered}（登记处见 check_consistency.API_DOMAIN_SEAMS）"
        )
    if stale:
        fails.append(f"api domain seams registry is stale: {stale}")


def check_audit_action_vocabulary() -> None:
    """审计的两侧都要有尺子（2026-10-02 轮 `R102-07` + `R102-14`）。

    **第一侧：一条 INSERT 只许有一份。** 审计从前有五个写入点 —— `roles/service.py`
    （api 侧 55 处全借它）、`core/plugins.py`、`core/tools/{files,mcp,run}.py` —— 五份
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
        fails.append(
            f"AUDIT_ACTIONS entries with no call site (rename forks history): {stale}"
        )
    if bad_prefixes:
        fails.append(
            f"dynamic audit prefixes not declared in DYNAMIC_ACTION_PREFIXES: {bad_prefixes}"
        )


def check_session_thread_write_seam() -> None:
    """`session_thread` 的写 SQL 只许住在 storage 层（`R102-05` 第二步的尺子）。

    这条表从前有七个写入者：`api/routers/sessions.py`（5 处）、`core/sync.py`（2）、
    `core/reachout/inbox.py`、`core/memory_distill.py`、`roles/service.py`、
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


#: 写语句所在、但**本函数自己不结束事务**的那九个位置（`R102-03` 沿袭项的盘点结果）。
#:
#: 逐处读过才登记：每一条都是"辅助函数由调用方收口"的形状 —— 写在这里的意义不是
#: "这些地方可以悬挂"，而是**新增一处这样的写法必须先过一道判断**（要么自己 commit，
#: 要么进这份名单并说清调用方在哪一步收口）。`R102-42` 那族（0 行的写也开了事务、
#: 抛之前不结束）的教训就是"同一句理由散在几处、注释传到第二处就停"；这份名单把
#: "谁收口"变成判据而不是记忆。
#:
#: 判据只数**代码里的字符串常量**（注释/docstring 里提到表名不算），并跳过协议声明
#: （函数体只有 `...` 或 `pass` —— 那是契约，不是写点：`DomainQueryService.create_report`
#: 就是被这样误收过一次）。
WRITE_TXN_HELPERS = frozenset(
    {
        "src/rolecard_agent/core/checkpointer.py::_set_flag",
        "src/rolecard_agent/core/checkpointer.py::_drop_orphan_writes",
        "src/rolecard_agent/core/memory_distill.py::extract",
        "src/rolecard_agent/core/model_settings.py::_write_chat_refs",
        "src/rolecard_agent/core/plugins.py::_bump_tool_epoch",
        "src/rolecard_agent/core/sync.py::_write_memory",
        "src/rolecard_agent/core/sync.py::_write_reachout",
        # 2026-10-04 审查快照：replace 档的行类清空**刻意不收口** —— 与随后的导入共用
        # 一个事务，成败一体；调用方 api/routers/sync.py::post_import 收口（导入有失败
        # 即 rollback + 400，成功则统一 commit）。
        "src/rolecard_agent/api/routers/sync.py::_clear_rows_for_replace",
        "src/rolecard_agent/storage/threads.py::delete_threads_for_user",
        "src/rolecard_agent/storage/threads.py::set_current_role",
    }
)


def check_write_txn_ownership_inventory() -> None:
    """"哪个产品写点会留未提交事务"从此有一份被看着的名单（`R102-03` 沿袭的那一角）。

    第 47 条断言 `dangling write txn` 管的是"判了 rowcount 那一支有没有先结束事务"；
    这一条管的是**另一支**：函数体里有写语句、而本函数既不 commit 也不 rollback ——
    这类写法本身不坏（helper 交给调用方收口是常见形状），坏的是**没人知道有几处**。
    10-03 的盘点给出 10 个候选，逐个读上下文后 9 处登记、1 处是协议声明（不是写点）。

    两个方向都要能红：新增了没登记的红，登记着却已经不在了也红（`R102-37` 的空转臂教训）。
    """
    src = ROOT / "src" / "rolecard_agent"
    found: set[str] = set()
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            writes = [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and re.search(r"\b(INSERT INTO|UPDATE\s+\w+|DELETE FROM)\b", n.value)
            ]
            if not writes:
                continue
            ends = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"commit", "rollback"}
                for n in ast.walk(fn)
            )
            if ends:
                continue
            real_body = [
                n
                for n in fn.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            ]
            if not real_body or (len(real_body) == 1 and isinstance(real_body[0], ast.Pass)):
                continue  # 协议/抽象声明：契约不是写点
            found.add(f"{rel}::{fn.name}")

    unregistered = sorted(found - WRITE_TXN_HELPERS)
    stale = sorted(WRITE_TXN_HELPERS - found)
    ok = not unregistered and not stale
    detail = (
        f"{len(found)} 个「写而不收口」的位置全在名单内"
        if ok
        else f"未登记：{unregistered}；名单里已经不在了：{stale}"
    )
    out("write txn ownership inventory", ok, detail)
    if unregistered:
        fails.append(
            "new function writes without ending the transaction and is not registered: "
            f"{unregistered}（要么自己 commit/rollback，要么进 "
            "check_consistency.WRITE_TXN_HELPERS 并说清调用方在哪一步收口）"
        )
    if stale:
        fails.append(
            f"WRITE_TXN_HELPERS has entries that no longer match a write site: {stale}"
        )


def _audit_ledger_path() -> pathlib.Path:
    """**最新一轮**审计台账住在哪儿：先扫 `docs/`（在写的），再扫 `docs/archive/`（已封存的）。

    为什么不是写死一个文件名：归档不该让尺子瞎掉 —— 本仓第九条踩过的那一发就是
    "台账全搬进 `docs/archive/` 之后，那把管台账形状的尺子再也没看见它"。
    按文件名里那个日期取最新的一轮，两个目录一起比；找不到就返回一个不存在的路径，
    让调用方按"台账不在"出声，而不是悄悄一步不进。
    """
    cands = [
        *sorted((ROOT / "docs").glob("架构审计（*轮）.md")),
        *sorted((ROOT / "docs" / "archive").glob("架构审计（*轮）.md")),
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
    """台账 H2 标题里那个"（N 条）"必须由脚本现数，不许手写（10-03 批 22 复核当场照出的）。

    这一轮加进 `R102-73` 之后，标题还写着"72 条" —— 与本仓反复在治的那一件是同一件事：
    **写死的数就是会漂的真相**（README 那组数、`R102-36` 那一族、批 12 那句"全部有结论"）。
    所以这里现数表格行数，再读标题里那个数字，两者必须相等。

    判据只看 H2 那张表（`| R102-NN | 级别 | … |` 形状的行），不看散文里的引用 —— 后者
    由 `audit citations` 与 `audit index in sync` 管着，三把尺子各管一件事。
    """
    ledger = _audit_ledger_path()
    if not ledger.exists():
        out("audit ledger row count", False, f"台账不在：{ledger}")
        fails.append(f"audit ledger missing: {ledger}")
        return
    text = ledger.read_text(encoding="utf-8")
    span = _ledger_h2_span(text, "audit ledger row count")
    if span is None:
        return
    start, end = span
    rows = len(re.findall(r"^\|\s*(R102-\d+)\s*\|", text[start:end], re.M))
    stated = re.search(r"## H2 ·[^(（]*（(\d+) 条）", text[start:end])
    if stated is None:
        out("audit ledger row count", False, f"H2 标题里没有「（N 条）」这一格（现数 {rows} 行）")
        fails.append("audit ledger H2 heading has no '（N 条）' cell to verify")
        return
    n = int(stated.group(1))
    ok = n == rows
    out("audit ledger row count", ok, f"表格现数 {rows} 行，标题写 {n} 条")
    if not ok:
        fails.append(
            f"audit ledger H2 heading says {n} rows while the table holds {rows} —— "
            "那个数由脚本现数，别手写"
        )


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


#: H2「状态」格里算"表过态"的字样（`R102-77` 的尺子在用）。
_LEDGER_STATUS_MARKERS = (
    "已收口", "未收口", "不修", "恒", "按设计", "取舍", "无需", "不动", "不改", "已处置", "已拍",
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
    """台账 H2 每行的「状态」格必须表态；说"已收口"的行必须在 H12 里找得出处（`R102-77`）。

    10-03 那次对账照出来的不是代码没做，而是**一份文档在两处说两种话**：44 行的状态格从没随
    批 1–5 回写，于是读汇总表的人以为还欠 44 件，读 H12 的人以为只剩 1 件。写死的数与不回的
    状态格是同一族（`R102-36`），所以这里两臂都判：
      · 状态格里一个表态字样都没有 ⇒ 红（沉默不许当成"没做"也不当成"做了"）；
      · 状态格写着"已收口"、而该编号在 H12 的执行记录里查无出处 ⇒ 也红（收口要有出处）。
    """
    ledger = _audit_ledger_path()
    if not ledger.exists():
        out("ledger status states a verdict", False, f"台账不在：{ledger}")
        fails.append(f"audit ledger missing: {ledger}")
        return
    text = ledger.read_text(encoding="utf-8")
    span = _ledger_h2_span(text, "ledger status states a verdict")
    if span is None:
        return
    start, end = span
    if "## H12 " not in text:  # 同一条规矩：出处无处可查要出声，不是抛 traceback
        out(
            "ledger status states a verdict",
            False,
            "最新台账里没有 H12 执行记录 —— 「已收口」的出处无处可查",
        )
        fails.append("newest audit ledger has no H12 execution record")
        return
    h12 = text[text.index("## H12 "):]
    detail_rows = text[end:]  # H2 之后就是逐条详表（H7/H8）与执行记录（H12）
    silent: list[str] = []
    orphan: list[str] = []
    total = 0
    for line in text[start:end].splitlines():
        m = re.match(r"^\|\s*(R102-\d+)\s*\|", line)
        if not m:
            continue
        total += 1
        rid = m.group(1)
        status = line.split("|")[-2]
        if not any(k in status for k in _LEDGER_STATUS_MARKERS):
            silent.append(rid)
        elif "已收口" in status and not _ledger_closure_traced(rid, h12, detail_rows):
            orphan.append(rid)
    ok = not silent and not orphan
    out(
        "ledger status states a verdict",
        ok,
        f"{total} 行全部表态；「已收口」都能在 H12 找到出处"
        if ok
        else f"没表态：{silent}；收口查无出处：{orphan}",
    )
    if silent:
        fails.append(
            f"audit ledger status cells carry no verdict: {silent} —— "
            "状态格沉默，读表的人就分不清「没做」与「做了没回写」"
        )
    if orphan:
        fails.append(
            f"audit ledger claims 已收口 with no trace in H12: {orphan} —— 收口要写在哪一批"
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
    产物由 `scripts/make_app_icon.py` 生成，这条只验结果，三格都判：
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
            + " —— 重跑 .venv-ocr\\Scripts\\python.exe scripts/make_app_icon.py"
        )


def check_single_text_extractor() -> None:
    """消息取文本只允许一处实现：`base/text.py::text_of`（架构审计报告 台账 `R28-59`）。

    这条断言防的是两类复发：
      1. **就地复刻** —— `domains/health/extract.py` 曾抄了第二份，且用空串拼接而不是空格，
         于是同一条消息在"流式输出"与"抽取解析"两条路上渲染成不同文本；
      2. **裸取消息体** —— 把多模态/流式形态下的分块列表直接字符串化，得到的是 Python
         repr（方括号花括号那一串）。那份 repr 曾进过 guard、用户收件箱，以及"增强提示词"
         贴回输入框的文本。
    所以除了"不许有第二份定义"，两种裸取写法也一起挡掉：先 getattr 取 content 再整体
    字符串化、以及拿 content 属性兜一个空串当文本用。它们正是上面那个 bug 的形状。
    """
    offenders: list[str] = []
    for path in iter_files(".py"):
        if "tests" in path.parts:
            continue  # 测试里为验证降级行为而手工构造怪形状，是刻意的
        rel = path.relative_to(ROOT).as_posix()
        if rel == "src/rolecard_agent/base/text.py":
            continue  # 唯一实现本尊
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        ):
            redefined = re.search(r"^\s*def _(?:text_of|serialize_text|content_text)\b", line)
            stringified = re.search(r"\bstr\(\s*getattr\([^()]*,\s*[\"']content[\"']", line)
            empty_defaulted = re.search(r"\.content\s+or\s+[\"'][\"']", line)
            if redefined or stringified or empty_defaulted:
                offenders.append(f"{rel}:{lineno}")
    detail = "single implementation" if not offenders else str(offenders[:6])
    out("single text extractor", not offenders, detail)
    if offenders:
        fails.append(f"message text must be read via base/text.py::text_of only: {offenders}")


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


def _package_constraints(lines: list[str]) -> dict[str, str]:
    """requirements 风格的行 → {发行包名: 版本约束原文}（"" = 无约束）。

    2026-10-04 审查快照的升级：此前 parity 只比**包名集合**，版本约束被
    `re.split` 剥掉 —— 同一包两处钉版不一致（langgraph-checkpoint-sqlite 一处
    `==3.1.1`、一处裸奔）就这样绿着进了仓库。约束逐字比对才是"镜像"的完整语义。
    """
    pins: dict[str, str] = {}
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"([^<>=!\[:]+)\s*(.*)", line)
        base = m.group(1).strip().lower() if m else line.lower()
        rest = m.group(2).strip() if m else ""
        pins[base] = rest
    return pins


def check_dependency_parity() -> None:
    """pyproject.toml is the single source of truth; requirements*.txt mirror it.

    Drift between the two is silent: it only shows up for whoever installs the *other*
    way. That is precisely the class of mistake a weaker model introduces, so it gets an
    assertion rather than a convention.

    2026-10-04（审查快照）：比对从"包名集合"升级为"包名 → 约束"全字典 ——
    名字集合相等而约束漂移（`==3.1.1` vs 裸名）从此必红。
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def _diff(label: str, py_lines: list[str], req_file: str) -> None:
        py_pins = _package_constraints(py_lines)
        req_pins = _package_constraints(
            (ROOT / req_file).read_text(encoding="utf-8").splitlines()
        )
        drift = {
            name: {"pyproject": py_pins.get(name), "requirements": req_pins.get(name)}
            for name in sorted(set(py_pins) | set(req_pins))
            if py_pins.get(name) != req_pins.get(name)
        }
        ok = not drift
        detail = "in sync" if ok else "; ".join(
            f"{n}: {d['pyproject']!r} vs {d['requirements']!r}" for n, d in drift.items()
        )
        out(label, ok, detail)
        if not ok:
            fails.append(f"pyproject.toml / {req_file} drift: {detail}")

    _diff("dependency parity", list(pyproject["project"]["dependencies"]), "requirements.txt")

    # The extras map to their own requirement files. Without this the api / rag / dev
    # mirrors can drift unnoticed - the earlier version of this check covered only the base
    # set, which is precisely how a mirror silently becomes wrong. Since 2026-10-04 the
    # comparison is per-constraint, not name-sets (2026-10-04 审查快照).
    extras = pyproject["project"].get("optional-dependencies", {})
    for extra, filename in (
        ("api", "requirements-api.txt"),
        ("rag", "requirements-rag.txt"),
        ("cloud", "requirements-cloud.txt"),
        ("dev", "requirements-dev.txt"),
    ):
        if extra not in extras:
            continue
        _diff(f"extra parity: {extra}", list(extras[extra]), filename)


#: spec 里的模块名（下划线）与发行包名（连字符）之间那点形状差。PEP 503 的归一只到
#: "下划线/连字符等价"，这里就照那一条来，别写第二套映射表。
def _module_to_distribution(module: str) -> str:
    return module.lower().replace("_", "-")


def _spec_runtime_packages() -> list[str]:
    """spec 的 `RUNTIME_PACKAGES` —— **读法只有一个出处**：`scripts/check_bundle_parity.py`。

    这条尺子与那条尺子问的是同一份清单（一条对 requirements，一条对 src 的懒加载），
    AST 解析各写一遍就会有一边先漂。本脚本与它同在 `scripts/` 下，`python scripts/x.py`
    时该目录已在 `sys.path[0]`，所以直接 import。
    """
    from check_bundle_parity import spec_runtime_packages  # noqa: PLC0415 - 同目录的尺子

    return spec_runtime_packages()


def check_spec_runtime_vs_requirements() -> None:
    """随包后端要收的每一族**模块**，都必须在那五份运行时 requirements 里有**声明出处**。

    为什么单独立一条（10-01，打包链台账 R28-57）：这条链上已经有两张表 —— spec 的
    `RUNTIME_PACKAGES`（模块名，决定"打进包的是哪些族"）与 `RUNTIME_REQ_FILES`（发行名，
    决定"装的时候装什么"），而**没有任何一处对读它们**。MCP 那半年的形状就是这么来的：
    spec 从 09-29 起要求 `mcp` 这个模块，`pip` 那侧却只写了 `langchain-mcp-adapters`，
    `mcp` 全靠传递依赖带进来 —— 于是 Dockerfile 少一份 requirements 时，装完照样"成功"，
    而包里的模块数是零（`R28-53`）。这与 `R28-11`（httpx 靠传递依赖兜住）是同一条病，
    只是这次躲在打包侧。

    判据方向只查一边：spec 要的每族模块必须在运行时那几份里被点名。反方向不查 ——
    requirements 里多一条不代表 spec 就该收它（`pyinstaller`、`pytest` 那些本来就不进包）。
    """
    packages = _spec_runtime_packages()
    if not packages:
        out(
            "spec runtime deps declared",
            False,
            "没从 packaging/rolecard-backend.spec 里读出 RUNTIME_PACKAGES —— 这条尺子自己看不见了",
        )
        fails.append("spec RUNTIME_PACKAGES unreadable")
        return
    declared = _runtime_declared_names()
    missing = [pkg for pkg in packages if _module_to_distribution(pkg) not in declared]
    declared_pairs = [_module_to_distribution(m) for m in missing]
    detail = (
        f"{len(packages)} 族模块全部在 {len(RUNTIME_REQ_FILES)} 份运行时 requirements 里有声明"
        if not missing
        else f"spec 要收却没人声明的模块：{missing}（发行名 {declared_pairs}）"
    )
    out("spec runtime deps declared", not missing, detail)
    if missing:
        fails.append(f"spec RUNTIME_PACKAGES not declared in requirements: {missing}")


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
    scripts = sorted((ROOT / "scripts").glob("*.py"))
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
        # contains an example of it.
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
            ["git", "check-ignore", "--stdin"],
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


# NOTE: check_python_pin was removed (2026-09-17) together with .python-version / uv.lock.
# The repo now manages dependencies with .venv + pip only; the Python floor lives solely in
# pyproject.toml's requires-python, and check_pyproject() already verifies it parses.


# Settings fields that are parsed on purpose but not read yet. Declaring them here is the
# point: a field that is merely forgotten and a field that is deliberately forward-looking
# look identical in the source, so the difference has to be written down somewhere.
RESERVED_SETTINGS = {
    "langsmith_api_key",  # v2.4 cloud observability
    "langsmith_project",  # v2.4 cloud observability
    # chroma_path left the reserved set in v2.1: the knowledge base reads it for real.
    # upload_dir left the reserved set in M5: the chat upload entry reads it for real.
}

# User stories that are explicitly NOT covered by automated tests yet. They are deferred to a
# later milestone, not forgotten - the traceability check still requires them to be *named* here
# so the gap stays visible (技术评审与决策.md §9 D3).
# US-9 (console frontend) was covered once M5 landed -> removed from this set (2026-09-15).
# US-6 (quick reproduce: 3 commands / lock file / demo video) still has no *test*; the non-video
# parts are guarded by readme_quickstart / dependency_parity / python_pin, and the 60s demo
# video was dropped by decision (2026-09-15).
DEFERRED_US = {"US-6"}


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

def find_dangling_write_txns(root: pathlib.Path | None = None) -> list[str]:
    """找出"判了 `rowcount` 却在那条路上不结束事务"的位置（`R102-42`）。

    为什么这条需要尺子而不是注释：SQLite 在写语句前隐式 BEGIN，**改到 0 行的 `UPDATE`
    同样开了一个写事务**；而 `if cur.rowcount == 0: raise ...` 这一路走不到 `commit()`。
    于是那把 RESERVED 锁留在调用它的那条线程上 —— 线程池里的线程不死，锁就没有来路可解，
    表现是同一台机器上后续所有写请求等 5 秒一起 `database is locked`。
    本仓早就知道这件事（`domains/health/service.py:365,412` 的注释原话是"未命中也要结束事务，
    否则悬挂的写事务会堵住别的线程"），**却只在两处做了**：同族另外五处漏着 ——
    这正是"一个形状靠注释传下去"的下场，所以它归尺子管。

    判据（只看 `if` 的测试里比较了 `X.rowcount` 的那种）：`body` 与 `orelse` 两个分支里，
    凡在第一个 `commit()` / `rollback()` **之前**就出现 `raise` 或 `return` 的，算一条。
    """
    base = root if root is not None else ROOT / "src" / "rolecard_agent"
    offenders: list[str] = []
    for path in sorted(base.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            tested = {n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)}
            if "rowcount" not in tested:
                continue
            for branch in (node.body, node.orelse):
                ended = False
                for stmt in branch:
                    names = {
                        n.attr
                        for n in ast.walk(stmt)
                        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
                    }
                    if names & {"commit", "rollback"}:
                        ended = True
                    if any(isinstance(s, (ast.Raise, ast.Return)) for s in _statements_of(stmt)):
                        if not ended:
                            offenders.append(f"{rel}:{stmt.lineno}")
                        break
    return offenders


def _statements_of(node: ast.stmt) -> list[ast.stmt]:
    """这一个语句"自己或最前面的一层"里包含的语句 —— 用来判 `if ...: raise` 这种一行体。"""
    if isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
        inner: list[ast.stmt] = [node]
        inner.extend(getattr(node, "body", []))
        inner.extend(getattr(node, "orelse", []))
        for handler in getattr(node, "handlers", []):
            inner.extend(handler.body)
        return inner
    return [node]


def check_dangling_write_txns() -> None:
    """门禁那一格：把 `find_dangling_write_txns` 的结果报出来。"""
    offenders = find_dangling_write_txns()
    out(
        "dangling write txn",
        not offenders,
        "；".join(offenders[:6])
        + (f"（共 {len(offenders)} 处）" if len(offenders) > 6 else "")
        if offenders
        else "所有判 rowcount 的分支都在抛/回之前结束了事务（写语句即便改到 0 行也开了事务）",
    )
    if offenders:
        fails.append(f"rowcount branches that leave a write transaction open: {offenders}")


def check_shape_migration_ddl() -> None:
    """搬层用的暂存表 DDL 必须与声明面**逐列同形**（架构审计 2026-10-02 轮 `R102-11`）。

    `core/model_settings.py` 的 `CREATE TABLE model_backend__layers` 把 `core/schema.sql`
    里 `model_backend` 的十列又抄了一遍，而 `_SHAPE_MIGRATED_TABLES` 让补列器对这张表
    **不动手** —— 两份 DDL 一旦分叉，"旧库那份抄的"就赢：新库有列、搬完层的旧库没列，
    读侧 `no such column`（`repeat_penalty` 那一发的教训，文件注释自己记着）。
    这把尺子就是那句"逐列相等"的兑现：分叉当场红，不再等老库升上来才炸。
    """
    schema_text = (ROOT / "src" / "rolecard_agent" / "core" / "schema.sql").read_text(
        encoding="utf-8"
    )
    ms_text = (ROOT / "src" / "rolecard_agent" / "core" / "model_settings.py").read_text(
        encoding="utf-8"
    )

    def declared_columns(create_block: str) -> set[str]:
        """从 CREATE TABLE 的列定义区抓列名（跳过约束行、SQL `--` 注释、python 串的引号）。"""
        names: set[str] = set()
        for raw in create_block.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue  # model_settings.py 的抄写现场里，python 注释行夹在串与串之间
            line = re.sub(r"--.*$", "", line).strip().strip('"').rstrip(",").strip()
            if not line or re.match(
                r"^(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)\b", line, flags=re.I
            ):
                continue
            names.add(line.split()[0])
        return names

    schema_m = re.search(r"CREATE TABLE IF NOT EXISTS model_backend \((.*?)\);", schema_text, re.S)
    layer_m = re.search(r'CREATE TABLE model_backend__layers \((.*?)\)"', ms_text, re.S)
    if not schema_m or not layer_m:
        fails.append("shape migration DDL: 判据的两个锚点（schema.sql / model_settings.py）没抓到")
        out("shape migration DDL", False, "anchor missing")
        return
    declared = declared_columns(schema_m.group(1))
    copied = declared_columns(layer_m.group(1))
    drift = sorted(declared ^ copied)
    out(
        "shape migration DDL",
        not drift,
        "；".join(drift[:8])
        if drift
        else f"model_backend__layers 与 model_backend 逐列相等（{len(declared)} 列）",
    )
    if drift:
        fails.append(
            "model_backend__layers DDL drifted from schema.sql: "
            f"{drift} —— 搬完层的旧库会 no such column；改列要两边一起改"
        )


def check_dead_config() -> None:
    """Every Settings field must be read somewhere outside config.py.

    A parsed-but-unread setting is worse than a missing one: `.env.example` advertises it, so
    someone configures it and believes it took effect. That is how `langsmith_api_key` and
    `model_fallbacks` sat unused (技术评审与决策.md §9 A2 / A4).
    """
    cfg_path = ROOT / "src" / "rolecard_agent" / "config.py"
    fields = re.findall(
        r"^\s{4}([a-z][a-z0-9_]*)\s*:", cfg_path.read_text(encoding="utf-8"), flags=re.M
    )
    others = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in iter_files(".py")
        if "tests" not in p.parts
    )
    # Count across production code INCLUDING config.py, and require more than the declaration
    # line itself. Excluding config.py looked right but produced a false positive:
    # `model_backends` is read by `backend()` and `resolve_fallbacks()` in that same file.
    unread = sorted(
        f for f in fields if f not in RESERVED_SETTINGS and len(re.findall(rf"\b{f}\b", others)) < 2
    )
    detail = (
        f"unread: {unread}"
        if unread
        else f"{len(fields)} fields, {len(RESERVED_SETTINGS)} reserved"
    )
    out("dead config", not unread, detail)
    if unread:
        fails.append(f"Settings fields never read outside their declaration: {unread}")


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
    **运行层**的 `requirements*.txt` 里声明 —— dev 层与打包机层（`-package`，只有
    PyInstaller）不进随包运行树，生产 import 靠它们兜等于没兜（httpx 当时正是"只有 dev
    声明 + langchain-core 传递"的双侥幸）。
    声明侧不读 pyproject：依赖 parity 那条已保证 pyproject 与 requirements 一致，这里
    只对一份事实面。import 名 ≠ 发行版名的（如 `import tavily` ← `tavily-python`）走
    显式别名表 —— 新映射缺了就红，把表补上即可，别名表本身就是"模块↔发行版"的登记处。
    """
    # import 名 → 发行版名 的已知差异。命中别名后仍按发行版名去声明集里找。
    import_dist_aliases = {"tavily": "tavily-python"}

    declared: set[str] = set()
    # 只有**运行层**能给 src 的 import 背书：dev 与打包机（-package）两层都不在随包运行树里。
    non_runtime = {"requirements-dev.txt", "requirements-package.txt"}
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


def check_env_example_models() -> None:
    """.env.example 里出现的本地模型名，必须是现役的那一个 —— 退役的不许再提。

    为什么单独立一条（2026-09-28 轮 `R28-09`）：这份文件是新用户唯一会照着敲的东西，
    而它当时写的示例是 `qwen2.5vl:7b`、正文还写着"对话模型必须是**文本版 qwen2.5:7b**"，
    对照的却是 `config.py` 里"qwen2.5 系均退役、默认 qwen3-vl:8b"——照它配出来的第一步
    就是已知会 400 的 `bind_tools`（vl 版官方模板不支持工具调用）。
    这不是"文档写旧了"，是**文档把人推向一个我们已经在代码里确认坏的配置**。

    判据两侧都取自代码，不手抄：现役名 = `DEFAULT_LOCAL_BACKEND["model"]`，
    退役名单 = `config.RETIRED_LOCAL_MODELS`（写在那里的理由就是这一条要能查）。
    只看 `MODEL_BACKENDS=` 那两行与"对话模型必须是"那句 —— 注释里作为**历史沿革**提到
    退役名的地方（比如解释为什么退役）不算，那是该留下的知识。
    """
    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.config import DEFAULT_LOCAL_BACKEND, RETIRED_LOCAL_MODELS  # noqa: PLC0415

    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    offenders: list[str] = []

    # 1) 生效的 MODEL_BACKENDS 行（注释掉的示例行不算：那是"按需添加"的写法示范）
    for line in text.splitlines():
        if line.lstrip().startswith("#") or not line.startswith("MODEL_BACKENDS"):
            continue
        for retired in RETIRED_LOCAL_MODELS:
            if retired in line:
                offenders.append(f"{line.split('=', 1)[0]} 引用了退役模型 {retired}")

    # 2) "对话模型必须是 X"那种带命令性的句子
    for retired in RETIRED_LOCAL_MODELS:
        if re.search(rf"(必须|应当|要用)[^。\n]*{re.escape(retired)}", text):
            offenders.append(f"正文把退役模型 {retired} 当要求写")

    # 3) start.bat 的 `set MODEL_THINKING_MODELS=`（`R102-23`）：它设置的是**真 env**、
    #    优先于 .env —— 写死旧名单的话，换默认模型时走 start.bat 的人仍被钉在旧名单上，
    #    症状是"思考过程忽然不显示"而没人改过开关。判据：必须与 config 从
    #    `DEFAULT_LOCAL_BACKEND` 派生的名单逐字一致（R28-13 的"不写第二遍"在 cmd 这一侧的兑现）。
    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.config import Settings as _CfgSettings  # noqa: PLC0415

    bat = ROOT / "start.bat"
    if bat.exists():
        expected_think = ",".join(_CfgSettings().model_thinking_models)
        for line in bat.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"\s*set\s+MODEL_THINKING_MODELS=(.*)", line, flags=re.I)
            if not m:
                continue
            value = m.group(1).strip()
            if value != expected_think:
                offenders.append(
                    f"start.bat 的 MODEL_THINKING_MODELS（{value or '空'}）与派生名单"
                    f"（{expected_think}）不一致 —— 它是真 env，会盖过 .env"
                )

    if DEFAULT_LOCAL_BACKEND.get("model") in ("", None):
        offenders.append("config.py 的默认本地后端没有 model —— 这条检查失去现役名基准")

    out("env example models", not offenders, "配置示例里的模型名与代码一致")
    if offenders:
        fails.append("env example advertises retired models: " + "; ".join(offenders))


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


#: 用户看得见的字面里不许再出现的词（`R28-45` 的事后闸）。判据分两半，各用**各自的精确信号**：
#:  * 前端扫 `frontend/dist` 的产物 —— 压缩后的 JS 里没有注释，出现在那里的字必然是用户看得见的
#:    （与 `bundled copy` 同一招，不需要一台 JSX 解析器去猜"这行是不是注释"）；
#:  * 后端扫 `src/**/*.py` 的**字符串常量**，docstring 与注释不算（那些是写给开发者看的，
#:    「后端」在那儿仍然精确指 `ModelBackend` 那一行）。
_BANNED_USER_VISIBLE = (
    "模型后端",      # L2 那一行在界面上叫「模型」
    "默认后端",      # 「默认」的家是「服务」页那条优先级，不是模型页
    "后端名",        # 字段名叫「模型名」
    "后端序列",      # 那条叫「对话优先级」
    "后端列表",
    "服务地址",      # L4 那个进程叫「本机程序」，它的地址是「程序地址」
    "本机后端",
    "本地推理服务",
    "Ollama 服务",
    "本地服务",      # 「本机程序」的落选备选（`R102-20`：同一旗标一张屏两个名字，
                     # 术语词表提案 §6.2 定名「本机程序」—— 从前它不在词表里，尺子照绿）
)


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
    from rolecard_agent.roles.seed import BUILTIN_ROLES, DOMAIN_SEED_ROLES  # noqa: PLC0415

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

    # 域种子角色（DOMAIN_SEED_ROLES）与内置角色同样随代码出厂：白名单写错名字要在这里
    # 大声失败，反向覆盖（声明了却没人引用）也要把它们的引用算进去。
    wanted: set[str] = set()
    for role in (*BUILTIN_ROLES, *DOMAIN_SEED_ROLES):
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
    from rolecard_agent.roles.seed import BUILTIN_ROLES  # noqa: PLC0415

    case_path = ROOT / "tests" / "eval" / "cases" / "health.json"
    if not case_path.exists():
        out("exemplar leaks eval answers", True, "no eval cases yet")
        return
    cases = json.loads(case_path.read_text(encoding="utf-8"))
    eval_inputs = {_normalise_question(str(c.get("input") or "")) for c in cases if c.get("input")}

    leaked: list[str] = []
    for role in BUILTIN_ROLES:
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
    """Every shipped user story must be traceable to at least one test (技术评审与决策.md §9 D3).

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
_CITATION_DOC_KEYS: tuple[tuple[str, frozenset[str]], ...] = (
    ("架构计划", frozenset({"架构计划.md"})),
    ("设计稿", frozenset({"主动消息与记忆设计稿.md"})),
    ("总览", frozenset({"架构总览.md"})),
    ("需求", frozenset({"需求与验收标准.md"})),
    (
        "审计",
        frozenset(
            {
                "架构审计.md",
                "架构审计（2026-09-26 轮）.md",
                "架构审计（2026-09-28 轮）.md",
                "架构审计（2026-10-02 轮）.md",
            }
        ),
    ),
)
_SECTION_RE = re.compile(r"§\s*(\d+(?:\.\d+)*)")
# `[RP]\d+-\d+`（`R102-33`）：R 号命名空间（R26/R28/R102…）从来都在被引用，而旧正则只认
# P 号 —— 95 个文件 290 处引用处于盲区，"有这条检查看着"是假的。宽判据实验（内存里做）：
# R 号进来后被引用编号 92 个、悬空 2 个（见 _CITATION_PROSE_ONLY），洪峰可控。
_PID_RE = re.compile(r"\b([RP]\d+-\d+)\b")
#: 文档里可以当被引用目标的两种形状：标题编号（`### 12.19 …`）与台账行号（`| 12.4 |`）。
_TARGET_HEAD_RE = re.compile(r"^#{2,5}\s+(\d+(?:\.\d+)*)\b")
_TARGET_ROW_RE = re.compile(r"^\|\s*([RP]\d+-\d+|\d+\.\d+)\s*\|")
#: 只活在**散文**里、从来没有台账行锚点的历史编号（同一次宽判据实验量得的全部悬空）。
#: R28-44/45 描述的事件已并入 §10 的教训正文 —— 它们不是断链，是"正文吸收了条目"。
#: 这份名单是**豁免登记处**：新的悬空出现时先查是不是同类，是就登记理由，不是就修引用。
_CITATION_PROSE_ONLY = frozenset({"R28-44", "R28-45"})


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


def _audit_index():
    """索引生成器（同一份扫描口径的唯一出处）。"""
    script = ROOT / "scripts" / "build_audit_index.py"
    spec = importlib.util.spec_from_file_location("audit_index", str(script))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_citation_reachability() -> None:
    """代码/测试/壳里的每一处 `§x.y` 与 `P{n}-{m}` 引用都必须真的指得到东西。

    为什么单独立这条（2026-09-25，审计 §12.19）：`check_doc_links` 只查 markdown 反引号里的
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
                    dangling.append(f"{rel_file}:{lineno} §{num}")
                    continue
                if any("/archive/" in f"/{item}" for item in found):
                    in_archive += 1
                named = next(
                    (words for word, words in _CITATION_DOC_KEYS if word in line),
                    None,
                )
                if named and not any(
                    item.rsplit("/", 1)[-1] in named for item in found
                ):
                    doubtful.append(
                        f"{rel_file}:{lineno} §{num} 点了那份文档，号却不在它里面"
                    )
            for num in _PID_RE.findall(line):
                total += 1
                if num in _CITATION_PROSE_ONLY:
                    continue  # 豁免登记处：只活在散文里的历史编号（见该常量的理由）
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


#: 「最后更新」那一行的日期（头部声明），与正文里出现过的日期。
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
            "docs/架构审计索引.md 不见了（跑 scripts/build_audit_index.py）",
        )
        fails.append("audit index missing")
        return
    expected = index.render()
    current = target.read_text(encoding="utf-8")
    if current == expected:
        rows = sum(
            1
            for line in expected.splitlines()
            if line.startswith("| ") and " | " in line[2:]
        )
        out("audit index in sync", True, f"{rows} 行与重算结果逐字一致")
        return
    delta = len(expected.splitlines()) - len(current.splitlines())
    out(
        "audit index in sync",
        False,
        f"索引与重算不一致（行数差 {delta:+}）—— 重跑 scripts/build_audit_index.py",
    )
    fails.append("audit index stale")


def main() -> int:
    check_pyproject()
    check_requirements_scope()
    check_stale_identifiers()
    check_config_contract()
    check_startup_env_documented()
    check_dependency_parity()
    check_spec_runtime_vs_requirements()
    check_promised_artifacts()
    check_core_no_domain_token()
    check_api_domain_seams()
    check_audit_action_vocabulary()
    check_session_thread_write_seam()
    check_write_txn_ownership_inventory()
    check_audit_ledger_row_count()
    check_data_root_dirs_gitignored()
    check_ledger_status_states_verdict()
    check_app_icon_frames()
    check_single_text_extractor()
    check_domain_isolation()
    check_safety_prompt()
    check_line_endings()
    check_ps1_encoding()
    check_console_encoding()
    check_readme_quickstart()
    check_readme_headline_numbers()
    check_milestone_alignment()
    check_v1_v2_boundary()
    check_doc_references()
    check_doc_links()
    check_markdown_table_shape()
    check_citation_reachability()
    check_doc_freshness()
    check_audit_index_in_sync()
    check_version_parity()
    check_dead_config()
    check_dependency_layering()
    check_env_example_models()
    check_installer_scope()
    check_ci_host_python_stdlib_only()
    check_artifact_single_source()
    check_single_source_literals()
    check_dangling_write_txns()
    check_shape_migration_ddl()
    check_bundled_copy()
    check_vocabulary()
    check_deploy_env_parity()
    check_role_whitelists_resolve()
    check_us_traceability()
    check_exemplar_leaks_eval_answers()
    report_line_budget()

    print("\n--- WARNS ---（不进红，但也不假装没看见）")
    for item in warns or ["none"]:
        print("  " + item)

    print("\n--- FAILS ---")
    for item in fails or ["none"]:
        print("  " + item)

    # The count is reported here rather than quoted in the docs: a hard-coded number in
    # prose goes stale the moment a check is added, which is the exact failure mode this
    # script exists to prevent.
    print(f"\nassertions: {passed} passed, {len(fails)} failed")
    print("RESULT:", "PASS" if not fails else f"{len(fails)} FAILING CHECK(S)")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
