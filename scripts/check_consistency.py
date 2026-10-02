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
        "src/rolecard_agent/core/text.py",
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


def check_single_text_extractor() -> None:
    """消息取文本只允许一处实现：`core/text.py::text_of`（架构审计报告 台账 `R28-59`）。

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
        if rel == "src/rolecard_agent/core/text.py":
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
        fails.append(f"message text must be read via core/text.py::text_of only: {offenders}")


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


def check_dependency_parity() -> None:
    """pyproject.toml is the single source of truth; requirements*.txt mirror it.

    Drift between the two is silent: it only shows up for whoever installs the *other*
    way. That is precisely the class of mistake a weaker model introduces, so it gets an
    assertion rather than a convention.
    """

    def package_names(lines: list[str]) -> set[str]:
        return _package_names(lines)

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    py_deps = package_names(pyproject["project"]["dependencies"])
    req_deps = package_names((ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines())

    only_py = sorted(py_deps - req_deps)
    only_req = sorted(req_deps - py_deps)
    ok = not only_py and not only_req
    detail = (
        "in sync" if ok else f"only in pyproject: {only_py} | only in requirements.txt: {only_req}"
    )
    out("dependency parity", ok, detail)
    if not ok:
        fails.append(f"pyproject.toml / requirements.txt drift: {detail}")

    # The extras map to their own requirement files. Without this the api / rag / dev
    # mirrors can drift unnoticed - the earlier version of this check covered only the base
    # set, which is precisely how a mirror silently becomes wrong.
    extras = pyproject["project"].get("optional-dependencies", {})
    for extra, filename in (
        ("api", "requirements-api.txt"),
        ("rag", "requirements-rag.txt"),
        ("cloud", "requirements-cloud.txt"),
        ("dev", "requirements-dev.txt"),
    ):
        if extra not in extras:
            continue
        extra_set = package_names(list(extras[extra]))
        mirror_set = package_names((ROOT / filename).read_text(encoding="utf-8").splitlines())
        diff = sorted(extra_set ^ mirror_set)
        extra_ok = not diff
        out(f"extra parity: {extra}", extra_ok, "in sync" if extra_ok else f"diff: {diff}")
        if not extra_ok:
            fails.append(f"extras[{extra}] vs {filename} drift: {diff}")


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
    name_cls = f"{cjk}A-Za-z0-9_"
    pattern = re.compile(
        rf"`([{name_cls}][{name_cls}.\-/]*\.(?:md|py|toml|txt|sql|json|cfg|ini|zip))`"
    )
    link_pattern = re.compile(r"\]\(([^)\s#]+?\.(?:md|png|jpg|json))\)")

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
    scanned = [p for root in ("src", "scripts") for p in (ROOT / root).rglob("*.py")]
    self_rel = pathlib.Path(__file__).relative_to(ROOT).as_posix()
    holders: dict[str, set[str]] = {}
    for path in sorted(scanned):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel == self_rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        docstrings = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        hits: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                hits.update(lit for lit in SINGLE_SOURCE_LITERALS if lit in node.value)
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

    declared: set[str] = set()
    for path in iter_files(".py"):
        if "tests" in path.parts:
            continue
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
    # 反向也要成立：声明了工具却没人用得上 = 死工具（要么忘了写进白名单，要么忘了注册）。
    if declared and not wanted:
        fails.append("tools are declared but no built-in role references any of them")
        out("role whitelist coverage", False, "no whitelist references any declared tool")


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
            }
        ),
    ),
)
_SECTION_RE = re.compile(r"§\s*(\d+(?:\.\d+)*)")
_PID_RE = re.compile(r"\b(P\d-\d+)\b")
#: 文档里可以当被引用目标的两种形状：标题编号（`### 12.19 …`）与台账行号（`| 12.4 |`）。
_TARGET_HEAD_RE = re.compile(r"^#{2,5}\s+(\d+(?:\.\d+)*)\b")
_TARGET_ROW_RE = re.compile(r"^\|\s*(P\d-\d+|\d+\.\d+)\s*\|")


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
    check_artifact_single_source()
    check_single_source_literals()
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
