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

    怎么"读 config"（P3-9 第五格，2026-10-08）：从前是一张 20 个前缀 + 3 个例外名的白名单
    在**原文**上正则扫，两类洞都是拿现网文件量出来的：
      **漏盖** —— RUN_/MAX_/RATE_/SYNC_/API_/IDENTITY_/CONSENSUS_/LOCAL_/OBS_ 这些没有
        前缀的键，config 读了也没人问（AST 化后多出 12 个被盖住的键，夹具钉着）；
      **虚盖** —— SILICONFLOW_API_KEY 被前缀正则"覆盖"，靠的是 config.py 里那句**删除
        说明**（`曾经并存的 … 随 P1-5 一并去掉`）：注释在替契约背书。AST 不看注释，
        它归 `check_entrypoint_env_documented` 管 —— 那把钥匙是 run_api.py 的。
    现在按 AST 读 `from_env` 的映射表与分支 `src.get`，键名从结构里长出来，加键不再需要
    来这里补白名单（旧 docstring 的"正则覆盖的全部前缀都要在这里列出来"那条纪律随白名单
    一起退休 —— 不用记，就不会漏）。
    暴露口径 = 活键**或**注释行（`.env.example` 自己在文件头宣布过："活键或注释都算说出
    来过"）：MAX_UPLOAD_BYTES 这族出厂默认与代码一致、以注释行给抄写形状。
    值漂移那一问仍然只看活键 —— 注释行没有"出厂默认"可比，两问口径不同是故意的。
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
    table, cfg_keys = config_env_contract(cfg_text)
    # 读法金丝雀：现网表 51 对、下限 40。config.py 一旦换形状（把表挪出 `for … in (…)` 的
    # 元组字面量、改成别的构造），这里会塌 —— 而"键变少"的契约**只会变绿**（要问的键变
    # 少了），塌了却全绿正是最坏的那种坏。所以给读法本身上一条下限：读不出结构 = 红，
    # 而不是安静地少盖一半。
    if len(table) < _MIN_MAPPING_PAIRS:
        out(
            "config contract",
            False,
            f"映射表只读出 {len(table)} 对（下限 {_MIN_MAPPING_PAIRS}）—— "
            "config.py 结构变了？读法没跟上",
        )
        fails.append(f"config mapping table unreadable: {len(table)} pairs")
        return
    # 活键 ∪ 注释键（口径见 docstring；两个 pattern 在现网文件上实测等价，54=54 对称差空）
    env_keys_doc = env_keys | set(re.findall(r"#\s*([A-Z][A-Z0-9_]{2,})[=\s]", env_text))
    missing = sorted(k for k in cfg_keys if k not in env_keys_doc)
    detail = f"missing: {missing}" if missing else f"{len(cfg_keys)} keys covered"
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

    # 映射表对（AST，与键名同一份读法 —— 旧版这里另有一条自己的正则，正是"同一结构两条
    # 读法、改一边漏一边"的形状）+ 走 `from_env` 独立分支的那几条（09-28 轮 `R28-14b`）。
    pairs = list(table) + list(_ENV_BRANCH_PAIRS)
    # 分支键的覆盖是**断言**，不是清单：`src.get("K")` 独立分支的键必须要么在 pairs
    # （有默认值可比）、要么在 JSON 豁免表（另一把尺子管），否则红。旧版靠手写清单兜着，
    # 清单漏一个洞就是静默的 —— 实测就漏了 OBS_EMIT_RAW_TEXT（R28-14b 那族的第五个），
    # 加键时不再靠人记得来补，漏了当场红。
    branch_keys = cfg_keys - {k for k, _ in table}
    uncovered = sorted(branch_keys - {k for k, _ in pairs} - _JSON_BLOB_EXEMPT)
    out(
        "config branch coverage",
        not uncovered,
        f"分支键 {len(branch_keys)} 个各有归属"
        if not uncovered
        else f"没人管的分支键：{uncovered}",
    )
    if uncovered:
        fails.append(f"config branch keys with no value pair or exemption: {uncovered}")
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


def config_env_contract(cfg_text: str) -> tuple[list[tuple[str, str]], set[str]]:
    """config.py 的 env 契约按 AST 读：（映射表 `(KEY, field)` 对，config 认的全部键）。

    两处结构都收：`for env_key, field in (("KEY", "field"), …)` 那张表，和走
    `src.get("KEY")` / `src["KEY"]` 的**独立分支**（MODEL_BACKENDS 那族）。旧版的两条
    正则一条只认表、一条靠前缀白名单扫原文 —— 同一结构两条读法，改一边漏一边。

    只数字符串常量且键名要长成 `KEY` 样子：变量拼出来的键读不出来，本仓没有那种写法，
    真要有人写，这条尺子会**漏在明处**（下一次契约红会把人引到这里）。
    表的匹配**限定在 For 节点的元组里**，不全文件乱认 —— 文件里任何别的
    ("ALLCAPS", "snake") 二元组不是契约（现网 51 对与表行数正好对上，量过）。
    """
    tree = ast.parse(cfg_text)
    table: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.iter, ast.Tuple):
            continue
        for elt in node.iter.elts:
            if not (isinstance(elt, ast.Tuple) and len(elt.elts) == 2):
                continue
            a, b = elt.elts
            if (
                isinstance(a, ast.Constant)
                and isinstance(b, ast.Constant)
                and isinstance(a.value, str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]+", a.value)
                and isinstance(b.value, str)
                and re.fullmatch(r"[a-z_][a-z0-9_]*", b.value)
            ):
                table.append((a.value, b.value))
    branch: set[str] = set()
    for node in ast.walk(tree):
        name = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            name = node.args[0].value
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            name = node.slice.value
        if isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]+", name):
            branch.add(name)
    keys = {k for k, _ in table} | branch
    return table, keys


#: 映射表可读性的下限（现网 51 对）。写成常量而不是散在断言里，是因为夹具要复现
#: 同一个判据 —— 两处各写一个数字就是给"改了判据漏了另一处"留门。
_MIN_MAPPING_PAIRS = 40


def _env_example_documented(env_text: str, cfg_text: str) -> set[str]:
    """"说出来过"的三处来源：活键、注释键、config 文本里带引号的键。

    口径与 `check_startup_env_documented` 同源（.env.example 文件头宣布的"活键或注释都
    算"），从前两处各写一遍三条正则 —— 改口径只改这里。**契约检查不用它**：那条的方向是
    config → example，把 config 自己的引号算进"已暴露"等于自己给自己背书（循环）。
    """
    return (
        set(re.findall(r"^([A-Z][A-Z0-9_]{2,})=", env_text, flags=re.M))
        | set(re.findall(r"#\s*([A-Z][A-Z0-9_]{2,})[=\s]", env_text))
        | set(re.findall(r'"([A-Z][A-Z0-9_]{2,})"', cfg_text))
    )


# 走 `from_env` 独立分支的键 →（字段, 默认值可比）。表里没有它们，但它们是 env 契约的
# 一部分 —— 谁不在这里也不在 JSON 豁免表，`config branch coverage` 当场红（清单是断言
# 不是备忘：R28-14b 那族从前手写漏了 OBS_EMIT_RAW_TEXT，洞静默了一个月）。
_ENV_BRANCH_PAIRS: list[tuple[str, str]] = [
    ("MODEL_THINKING", "model_thinking"),
    ("MODEL_FALLBACKS", "model_fallbacks"),
    ("MODEL_THINKING_MODELS", "model_thinking_models"),
    ("MCP_SERVERS", "mcp_servers"),
    # 实测加入后两臂都绿：example `false` vs Settings 默认 False（bool 分支认 false/0）。
    ("OBS_EMIT_RAW_TEXT", "obs_emit_raw_text"),
]

#: JSON blob 那些键没有"一个出厂默认值"可比；能漂的是"抄下来解析不了"，
#: 那半边由 `check_config_contract` 里的 `env example json valid` 那条管（原话在它上面）。
_JSON_BLOB_EXEMPT = {"MODEL_BACKENDS"}

def _env_names_read_in_src(src: pathlib.Path) -> dict[str, str]:
    """通过 `os.environ` 读到的变量名 → 第一处 `文件:行`。

    三种写法都算：`os.environ.get("X", …)`、`os.environ["X"]`、`X in os.environ`。
    只数**字符串常量**那一种（变量名是拼出来的读不出来 —— 本仓没有那种写法，
    真要有人写，这条尺子会漏，漏在明处）。

    入参可以是目录（`src/**`，尺子的常规用法）或**单个文件**（入口那条尺子只问
    `scripts/run_api.py` 一个）。只给目录时 `path.rglob` 对文件返回空 —— 实测过：
    把文件当目录传，得到的是"没读到"而不是报错，而"没读到"在断言里就是恒绿。
    """
    paths = [src] if src.is_file() else sorted(src.rglob("*.py"))
    found: dict[str, str] = {}
    for path in paths:
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
    cfg_path = ROOT / "src" / "rolecard_agent" / "config.py"
    cfg_text = cfg_path.read_text(encoding="utf-8", errors="ignore")
    documented = _env_example_documented(env_text, cfg_text)

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


#: run_api.py 的 `--smoke` 校对参数：只在跑冒烟时读，不是部署契约 —— 与
#: `check_startup_env_documented` 豁免取证私有开关同一口径（写进 example 反而让人以为
#: 设了它就能改变应用行为；用法在 run_api.py 自己的模块 docstring 里）。
_SMOKE_PROBE_ENV = {"SMOKE_BASE_URL", "SMOKE_MODEL"}


def check_entrypoint_env_documented() -> None:
    """生产入口 `scripts/run_api.py` 读的每个 env，都必须说过（example 或 config 文本）。

    为什么不并进 `startup env documented`：那条**刻意只管 `src/**`**（见其 docstring：
    scripts 里其余是取证私有开关）。但 run_api.py 不是探针 —— README 与冒烟起的就是它，
    它读 12 个 env，其中 `SILICONFLOW_API_KEY` 是"启动时注册云后端"那半条路的钥匙。

    为什么现在必须立（不是顺手加的）：`check_config_contract` 的 AST 化让
    `SILICONFLOW_API_KEY ∈ .env.example` 这条断言**失去了它意外的宿主** —— 从前靠前缀
    正则撞上 config.py 里那句删除说明才顺带管着它（注释替契约背书，不是判据）。断言不许
    随宿主消失：搬到真正的主人名下，这一条就是那个宿主。
    """
    entrypoint = ROOT / "scripts" / "run_api.py"
    env_text = (ROOT / ".env.example").read_text(encoding="utf-8", errors="ignore")
    cfg_text = (ROOT / "src" / "rolecard_agent" / "config.py").read_text(
        encoding="utf-8", errors="ignore"
    )
    documented = _env_example_documented(env_text, cfg_text)
    read = _env_names_read_in_src(entrypoint) if entrypoint.exists() else {}
    # 读不到 ≠ 没人读：文件在却一个 env 名都解析不出来（路径错/语法错被跳过）时，
    # 空集合会把下面算成全绿 —— 那是恒绿尺子，这里先掐掉。
    if not read:
        out("entrypoint env documented", False, f"{entrypoint.name} 读不到 env 名 —— 不许静默绿")
        fails.append(f"entrypoint unreadable: {entrypoint.name}")
        return
    missing = sorted(k for k in read if k not in documented and k not in _SMOKE_PROBE_ENV)
    out(
        "entrypoint env documented",
        not missing,
        f"入口读的 {len(read)} 个 env 全说过（豁免冒烟参数 {sorted(_SMOKE_PROBE_ENV)}）"
        if not missing
        else f"入口在读、没人说过：{missing}",
    )
    if missing:
        fails.append(f"undocumented env read by run_api.py: {missing}")

# Settings fields that are parsed on purpose but not read yet. Declaring them here is the
# point: a field that is merely forgotten and a field that is deliberately forward-looking
# look identical in the source, so the difference has to be written down somewhere.
RESERVED_SETTINGS = {
    "langsmith_api_key",  # v2.4 cloud observability
    "langsmith_project",  # v2.4 cloud observability
    # chroma_path left the reserved set in v2.1: the knowledge base reads it for real.
    # upload_dir left the reserved set in M5: the chat upload entry reads it for real.
}

def _settings_field_names(cfg_text: str) -> list[str]:
    """类体里 `name: type` 注解字段（AnnAssign）的名字，按声明顺序。

    为什么不用旧的 `^\\s{4}name:` 正则：认死四空格 —— 缩进一变整段取不到，而"字段变少"
    对 `dead config` 这条断言**只会更绿**（没人读的字段跟一起消失，红臂再也红不了）。
    实测两种读法在现网文件上取到的 78 个字段**集合完全相等**（Settings 60 +
    ModelBackend 12 + McpServerConfig 7），所以这一刀是语义不变的换底；AST 还多给一个
    诚实性：无注解的类级赋值（pydantic 的 `model_config` 机器配置）本来就不该算字段，
    AnnAssign-only 正好把它挡在外面，而旧正则靠"那行没有冒号"的巧合才没数到它。
    """
    tree = ast.parse(cfg_text)
    names: list[str] = []
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                names.append(stmt.target.id)
    return names


def check_dead_config() -> None:
    """Every parsed field must be read somewhere outside config.py.

    A parsed-but-unread setting is worse than a missing one: `.env.example` advertises it, so
    someone configures it and believes it took effect. That is how `langsmith_api_key` and
    `model_fallbacks` sat unused (技术评审与决策.md §9 A2 / A4).

    字段范围比这句 docstring 说的宽：Settings、ModelBackend、McpServerConfig 三类的注解
    字段都算（旧正则的 4 空格扫到的就是这三类，实测 78=78）—— 模型页配置里的字段照样
    是"配了没用"，少盖一类没有道理。
    """
    cfg_path = ROOT / "src" / "rolecard_agent" / "config.py"
    fields = _settings_field_names(cfg_path.read_text(encoding="utf-8"))
    # 一个字段都没读出来 = 解析没跟上（文件没了/结构换了），而空字段集对这条断言恒绿 ——
    # 与 entrypoint 那条"读不到就红"同一条纪律：先掐掉恒绿的形状。
    if not fields:
        out("dead config", False, "config.py 一个注解字段都没读出来 —— 结构变了？不许恒绿")
        fails.append(f"settings fields unreadable: {cfg_path.name}")
        return
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
