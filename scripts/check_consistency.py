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
import json
import os
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]

fails: list[str] = []
warns: list[str] = []
passed = 0

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
    leaked = [d for d in ("paddle", "chromadb", "fastapi", "uvicorn") if d in body]
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

    判据：`RUNTIME_REQ_FILES` 是"任何一条能跑起完整产品的路径都得有"的那四份，
    逐个入口现读它引用了哪些 `requirements*.txt`。dev / ocr / mcp / package **不在表内**：
    那几份按形态有意分开装（dev 不进生产运行树、paddle 必须独立 venv、mcp 由随包后端自己带、
    package 只有打包机要），把它们一起比会天天误报。

    另一半（同一轮 `R28-32` 加的）：**磁盘上每一份 requirements*.txt 都要在这两张表里之一**。
    新增一族却没人分类，症状与"少装一族"完全一样而方向相反 —— 它被某条路径默默需要着，
    却没有任何一处写着"这条路径装过它"。空理由不算理由，照样红。
    """
    # 生效行与注释行都算数：`install.bat` 的提示句里出现文件名不算"装过"，所以只取
    # 真正执行 pip 的那一行；镜像与 CI 的写法各异，统一用"这一行引用了这个文件"来判。
    RUNTIME_REQ_FILES = (
        "requirements.txt",
        "requirements-api.txt",
        "requirements-rag.txt",
        "requirements-cloud.txt",
    )
    # 入口 → (文件, 认"装过了"的行特征)。刻意写死特征而不是通用正则：每条路径的形状本来就不一样。
    surfaces = {
        "install.bat": ("install.bat", "pip install"),
        "Dockerfile": ("Dockerfile", "-r requirements"),
        "ci.yml": (".github/workflows/ci.yml", "-r requirements"),
        "README": ("README.md", "-r requirements"),
    }
    missing: list[str] = []
    for label, (rel, marker) in surfaces.items():
        path = ROOT / rel
        if not path.exists():
            missing.append(f"{label} 这个入口文件不见了（{rel}）")
            continue
        # README 的"快速开始"是带 \ 续行的代码块，按物理行找会漏后面几份 —— 压成一行再比。
        # marker 单独查一次：整条 pip 行被删掉时也要红，而不是"少一份依赖"这种半句话。
        flat = " ".join(path.read_text(encoding="utf-8", errors="ignore").split())
        if marker not in flat:
            missing.append(f"{label} 里找不到装依赖的那一行（没有 {marker!r}）")
            continue
        missing += [f"{label} 没引用 {req}" for req in RUNTIME_REQ_FILES if req not in flat]
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
        "requirements-ocr.txt": "paddle 与主环境冲突，必须独立 venv（见该文件开头）",
        "requirements-mcp.txt": "只有接入外部 MCP server 的部署要装",
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
    """版本号各有它的"必须一致"对象，从前一处都没人比（09-26 轮 R26-21）。

    三条规则来自各处注释里自己写下的承诺，不是新发明：
      * `pyproject.toml` 的 version 承诺"与 api/main.py 的 FastAPI version 保持一致"；
      * 壳与其托管的前端是**同一个产品版本**（安装包文件名与下载卡都按它显示），
        所以 `shell/package.json` 与 `frontend/package.json` 必须相等。
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
        # 读的是 `API_VERSION` 那个常量（`R28-26`）：从前这里是 `version="x.y.z"`，
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
    if py != api:
        bad.append(f"pyproject {py} ≠ api/main.py 的 API_VERSION {api}（注释承诺两者一致）")
    if shell != front:
        bad.append(f"shell/package.json {shell} ≠ frontend/package.json {front}")

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

    detail = f"py={py} api={api} shell={shell} frontend={front}"
    out("version parity", not bad, "; ".join(bad) if bad else detail)
    if bad:
        fails.append(f"version claims out of sync: {bad}")


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
    """消息取文本只允许一处实现：`core/text.py::text_of`（架构审计报告 P1-8）。

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
        names: set[str] = set()
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if line:
                names.add(re.split(r"[<>=!\[;]", line, maxsplit=1)[0].strip().lower())
        return names

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
    # 两条刻意不参与：
    #  * `docs/archive/` 是**封存件** —— 里面的路径是"写它的那天"的事实，按 R26-19 的同一个
    #    决定（引用可达性进门禁，但归档档里的编号与路径原地不动）不去追修它们。
    #  * `.workbuddy/` 是另一个 IDE 的会话日志（见记忆「Parallel IDE workflow」）——
    #    那是历史陈述句不是文档，且由另一个工具在写。
    def out_of_scope(rel: pathlib.Path) -> bool:
        parts = set(rel.parts)
        return "archive" in parts or ".workbuddy" in parts

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
                    broken.append(f"{rel}:{lineno} -> {ref}")
    detail = "; ".join(broken[:4]) if broken else "all resolve"
    out("doc links", not broken, detail)
    if broken:
        fails.append(f"broken doc references: {broken}")


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
    台账恰恰全靠列位对齐。`docs/archive/` 按封存件排除（与 `check_doc_links` 同一口径）。
    """
    offenders: list[str] = []
    for path in iter_files(".md"):
        rel = path.relative_to(ROOT)
        if "archive" in rel.parts:
            continue
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
    holders: dict[str, set[str]] = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
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
_CITATION_DOC_KEYS: tuple[tuple[str, str], ...] = (
    ("架构计划", "docs/archive/架构计划.md"),
    ("设计稿", "docs/主动消息与记忆设计稿.md"),
    ("总览", "docs/架构总览.md"),
    ("需求", "docs/需求与验收标准.md"),
    ("审计", "docs/架构审计.md"),
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


def check_citation_reachability() -> None:
    """代码/测试/壳里的每一处 `§x.y` 与 `P{n}-{m}` 引用都必须真的指得到东西。

    为什么单独立这条（2026-09-25，审计 §12.19）：`check_doc_links` 只查 markdown 反引号里的
    **路径**，查不到 .py docstring 里"（架构审计报告 P1-5）"这种**散文引用** —— 而这类引用
    实测 29 个文件、400 处。后果不是难看，是**文档一动就静默断链**，而断掉的正好是"这个决策
    为什么长这样"的唯一线索（今天的误判就被一句过期的"P0-3 尚未闭环"带偏过一次）。

    判据按"只在确证的负面上进红"分三级（与 `fail open on uncertainty` 同一条纪律）：
      * **红**：这个编号在**任何**一份文档里都不存在 —— 要么写错，要么那一节被删了。
      * **黄**：只存在于 `docs/archive/` 下 —— 引用没有断，但读者拿到的已是作废的规划。
        首跑实测 27 处指向《架构计划》的 5.2 / 5.3 两节与那份 UI 整改稿的 2.2.2 —— 那些设计
        早已搬进架构总览。这句话刻意**不写 §**：它是在**叙述**那些编号，不是在**指向**它们，
        而这条检查分不出这两种（这一行自己就被自己黄到过，2026-09-28 实测在列）。
        （这里曾经写的是"94 处"——一个没复算的数。本检查存在的理由就是不许这种事发生，
        结果它自己第一条就犯了，见架构审计（2026-09-26 轮）的 R26-06。）
      * **黄**：行内点了某份文档、但那个编号在它里面没有而在别处有 —— 归因可疑，不武断。
    """
    live = {p.relative_to(ROOT).as_posix() for p in (ROOT / "docs").glob("*.md")}
    archived = {
        p.relative_to(ROOT).as_posix() for p in (ROOT / "docs" / "archive").glob("*.md")
    }
    # 一律用**仓库相对 posix 路径**当 key。第一版拿 `pathlib.Path` 绝对路径去
    # `str(h).startswith("docs/archive")`，Windows 上永远是 False —— 于是"把审计档搬进
    # archive/"这个本该被看见的动作，只报了 4 条可疑。是搬档模拟实验把它照出来的。
    targets = {rel: _citation_targets(ROOT / rel) for rel in (*live, *archived)}

    def _home(num: str) -> list[str]:
        return [rel for rel, pool in targets.items() if num in pool]

    dangling: list[str] = []
    archived_refs: list[str] = []
    doubtful: list[str] = []
    total = 0
    for path in iter_files(".py", ".ts", ".tsx", ".js"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel_file = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in _SECTION_RE.finditer(line):
                total += 1
                num = match.group(1)
                homes = _home(num)
                named = next((rel for word, rel in _CITATION_DOC_KEYS if word in line), None)
                if not homes:
                    dangling.append(f"{rel_file}:{lineno} §{num}")
                elif all(h in archived for h in homes):
                    archived_refs.append(f"{rel_file}:{lineno} §{num}→{homes[0]}")
                elif named and named not in homes:
                    doubtful.append(f"{rel_file}:{lineno} §{num} 点了「{named}」却在别处")
            for num in _PID_RE.findall(line):
                total += 1
                if not _home(num):
                    dangling.append(f"{rel_file}:{lineno} {num}")

    detail = (
        f"{total} citations; {len(dangling)} dangling, "
        f"{len(archived_refs)} 指向归档档, {len(doubtful)} 归因可疑"
    )
    out("audit citations", not dangling, detail)
    for label, items in (
        ("Dangling §/P citations", dangling),
        ("Citations into archived (superseded) docs", archived_refs),
        ("Citations whose named doc lacks the number", doubtful),
    ):
        if items:
            line = f"{label} ({len(items)}): " + " | ".join(items[:8])
            (fails if label.startswith("Dangling") else warns).append(line)


def main() -> int:
    check_pyproject()
    check_requirements_scope()
    check_stale_identifiers()
    check_config_contract()
    check_dependency_parity()
    check_promised_artifacts()
    check_core_no_domain_token()
    check_single_text_extractor()
    check_domain_isolation()
    check_safety_prompt()
    check_line_endings()
    check_console_encoding()
    check_readme_quickstart()
    check_milestone_alignment()
    check_v1_v2_boundary()
    check_doc_references()
    check_doc_links()
    check_markdown_table_shape()
    check_citation_reachability()
    check_version_parity()
    check_dead_config()
    check_dependency_layering()
    check_env_example_models()
    check_installer_scope()
    check_single_source_literals()
    check_bundled_copy()
    check_vocabulary()
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
