"""Repo consistency check. Run in CI and before every commit.

Why this exists: as the project went through several rounds of revision, documents and
code drifted apart (a renamed config key still referenced in a docstring, stale project
names, requirements files pulling in scope the version does not need). Eyeballing does
not catch these - the checker caught one on its very first run.

Usage:
    python scripts/check_consistency.py        # exits 1 on failure, 0 on pass
"""

from __future__ import annotations

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
    env_keys = set(
        re.findall(
            r"^([A-Z][A-Z0-9_]+)=",
            (ROOT / ".env.example").read_text(encoding="utf-8"),
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
    )
    pattern = r"\b(" + "|".join(prefixes) + r"|SQLITE_PATH|CHROMA_PATH|UPLOAD_DIR)\b"
    cfg_keys = set(re.findall(pattern, cfg_text))
    missing = sorted(k for k in cfg_keys if k not in env_keys and k != "LANGSMITH_PROJECT")
    detail = f"missing: {missing}" if missing else f"{len(env_keys)} keys aligned"
    out("config contract", not missing, detail)
    if missing:
        fails.append(f".env.example missing keys documented in config.py: {missing}")


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
    """各域**自己建**的表名 = 内核源码里不该出现的专有名词（架构审计报告 §5.1 / P1-1）。

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


def check_line_endings() -> None:
    """Everything under src/ tests/ scripts/ must be LF.

    33 files were CRLF on the first real lint run, because PowerShell's Set-Content and
    several Windows editors default to CRLF. Mixed line endings become whole-file diffs on
    CI and make `ruff format --check` fail for reasons unrelated to the change
    (C22). .gitattributes prevents it happening again.
    """
    offenders: list[str] = []
    for base in ("src", "tests", "scripts"):
        for path in (ROOT / base).rglob("*"):
            if "__pycache__" in path.parts:
                continue  # 字节码是二进制：其中偶然出现 \r\n 字节序列会造成误报
            if path.is_file() and b"\r\n" in path.read_bytes():
                offenders.append(str(path.relative_to(ROOT)))
    detail = f"CRLF in: {offenders}" if offenders else "all LF"
    out("line endings", not offenders, detail)
    if offenders:
        fails.append(f"CRLF line endings found: {offenders}")


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
    prefixes = ("docs/", "src/", "scripts/", "tests/", "data/")
    pattern = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./\-]*\.(?:md|py|toml|txt|sql|json|cfg|ini))`")

    broken: list[str] = []
    for path in iter_files(".md"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            for ref in pattern.findall(line):
                if "*" in ref or ref in not_yet:
                    continue
                if not (ref.startswith(prefixes) or ref in bare):
                    continue
                if not (ROOT / ref).exists():
                    broken.append(f"{path.relative_to(ROOT)}:{lineno} -> {ref}")
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
    check_readme_quickstart()
    check_milestone_alignment()
    check_v1_v2_boundary()
    check_doc_references()
    check_doc_links()
    check_dead_config()
    check_role_whitelists_resolve()
    check_us_traceability()
    check_exemplar_leaks_eval_answers()
    report_line_budget()

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
