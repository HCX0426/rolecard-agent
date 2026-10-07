"""config 主题这一族判据（P3-9 按主题细分，从 checks.py 平移，正文一字未改）。

按主题拆出；执行顺序由 registry.CHECKS 唯一决定，本模块只回答"这一族住哪"。
行为等价由门禁实跑全部 CHECKS 证明。
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import sys
from collections import Counter

from .core import ROOT, fails, iter_files, out


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

# Settings fields that are parsed on purpose but not read yet. Declaring them here is the
# point: a field that is merely forgotten and a field that is deliberately forward-looking
# look identical in the source, so the difference has to be written down somewhere.
RESERVED_SETTINGS = {
    "langsmith_api_key",  # v2.4 cloud observability
    "langsmith_project",  # v2.4 cloud observability
    # chroma_path left the reserved set in v2.1: the knowledge base reads it for real.
    # upload_dir left the reserved set in M5: the chat upload entry reads it for real.
}

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
    #
    # **一次扫完再查表**（2026-10-07 实测改的）：从前是每个字段跑一遍
    # `re.findall(rf"\b{f}\b", others)` —— 54 个字段 × 整份 corpus，cProfile 里
    # `re.findall` 那 33 万次调用大半来自这一格，它单独占掉整个一致性检查的 2.1s（总计 12.8s）。
    # 词元计数与 `\b` 计数在这里**等价**：字段名是 `[a-z][a-z0-9_]*`，而标识符边界恰好就是
    # `\b` 的边界（`my_api_key` 里数不出 `api_key`，两种写法一致）。
    seen = Counter(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", others))
    unread = sorted(f for f in fields if f not in RESERVED_SETTINGS and seen[f] < 2)
    detail = (
        f"unread: {unread}"
        if unread
        else f"{len(fields)} fields, {len(RESERVED_SETTINGS)} reserved"
    )
    out("dead config", not unread, detail)
    if unread:
        fails.append(f"Settings fields never read outside their declaration: {unread}")

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
