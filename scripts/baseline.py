"""一次性产出 `build/baseline.json` —— 全项目"当前有多少"的唯一口径。

为什么要有这个脚本（2026-09-25，审计 §12.19 的 direct 产物）：架构审计里"约 15 处
`DEFAULT_USER_ID`"与实测 22 处差了 7 处，差的不是口径而是**谁在什么时候手数的**。多路取证
（六个并行 agent）一旦各自 grep，同一件事会拿到六个数，然后整份报告都在讨论数字不一致。

所以规矩是：**agent 只许引用这张表，不许自己数**。脚本本身只读真库（`mode=ro`），
任何写操作都不发生；建 app 时显式给一个临时 `sqlite_path`，免得碰任何一份真实数据。

用法：`python scripts/baseline.py`（写 build/baseline.json 并打一份摘要）
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "baseline.json"
SRC_DIRS = ("src", "scripts", "tests", "frontend/src", "shell")


def _scratch(name: str) -> Path:
    """暂存库的路径。**不用 TemporaryDirectory**：`create_app()` 走的是线程局部连接，
    它不会在函数返回时关掉，于是 Windows 上"退出时删临时目录"必然 PermissionError ——
    这份脚本自己就撞过。落进 `build/_baseline/`（gitignore 内），带 pid 避免同一进程内撞名。

    带 pid 的代价是**每跑一次留一对**：旧写法只删"自己这一对"（同名才撞得上），于是 10-01
    清点时这里堆了 100 个文件 / 15 MB。所以每次建路顺手扫一次龄。
    """
    directory = ROOT / "build" / "_baseline"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{Path(name).stem}-{os.getpid()}.db"
    for suffix in ("", "-wal", "-shm"):
        stale = path.with_name(path.name + suffix)
        if stale.exists():
            stale.unlink(missing_ok=True)
    _sweep_stale_scratch(directory)
    return path


# 两份 baseline 可以并发（本机在跑、CI 同时在跑），所以不能按 pid 删别人的，只能按**龄期**。
# 一次跑完是几十秒量级，留 10 分钟已经宽到不可能误杀还在用的那份。
_STALE_SCRATCH_SECONDS = 600.0


def _sweep_stale_scratch(directory: Path) -> None:
    """删掉超过龄期的暂存件（含 -wal / -shm）；删不动就跳过 —— 一件卫生活不该让基线跑挂。"""
    cutoff = time.time() - _STALE_SCRATCH_SECONDS
    for stale in directory.glob("*.db*"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            continue


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False
    ).stdout.strip()


def section_git() -> dict[str, Any]:
    dirty = [ln for ln in _git("status", "--short").splitlines() if ln.strip()]
    return {
        "head": _git("rev-parse", "--short", "HEAD"),
        "branch": _git("branch", "--show-current"),
        "dirty_lines": len(dirty),
        "dirty": dirty[:40],
    }


def _ro_conn(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def section_routes() -> dict[str, Any]:
    """真实路由表 —— 从活的 app 上取，不读文档也不猜。用暂存库，不碰任何数据根。

    **为什么走 `app.openapi()` 而不是遍历 `app.routes`**：这个 FastAPI 版本把
    `include_router` 做成惰性的 `_IncludedRouter`，`app.routes` 只有 19 项（13 个是未展开的
    router 对象），照着它数会得到"全项目 5 条路由"这种离谱数 —— 第一次跑就撞上了。
    `openapi()` 会强制把嵌套 router 展开成调用方真正能打的 (path, method) 集合。
    """
    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.api.main import create_app  # noqa: PLC0415

    app = create_app(sqlite_path=_scratch("routes.db"))
    spec = app.openapi()
    table = sorted(
        f"{m.upper()} {path}"
        for path, ops in spec["paths"].items()
        for m in ops
        if m not in {"head", "options"}
    )
    return {
        "count": len(table),
        "source": "app.openapi()（含惰性 include 的展开）",
        "paths": table,
        "operator_by_default": "未表态即 operator（见 api/access.py），此处只给全量",
    }


def _columns(path: Path) -> dict[str, set[str]]:
    conn = _ro_conn(path)
    try:
        out: dict[str, set[str]] = {}
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            out[t] = {r[1] for r in conn.execute(f"PRAGMA table_info('{t}')")}
        return out
    finally:
        conn.close()


def declared_table_names() -> set[str]:
    """schema 文件原文里"声明了哪些表" —— 用来盯 declared 形状有没有被建空。

    读不到就抛：这个集合是 `column_drift_vs_declared` 那格的**分母**，静默回空集合会让
    一根恒绿的尺子看起来在管事（2026-10-02 轮实测：旧写法建出的暂存库 0 张表）。
    """
    files = [
        ROOT / "src" / "rolecard_agent" / "core" / "schema.sql",
        ROOT / "src" / "rolecard_agent" / "roles" / "schema.sql",
        *sorted((ROOT / "src" / "rolecard_agent" / "domains").glob("*/schema.sql")),
    ]
    names: set[str] = set()
    for path in files:
        if not path.exists():
            raise FileNotFoundError(f"schema 文件不见了：{path}")
        # 注释行里也写着 `CREATE TABLE IF NOT EXISTS`（core/schema.sql:295 就在讲这件事），
        # 不先剔掉就会把 "IF" 当成表名数进来 —— 那不是幻影，那是把尺子的分母数脏。
        sql = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("--")
        )
        names.update(
            re.findall(
                r"CREATE TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*?)\s*\(",
                sql,
            )
        )
    if not names:
        raise RuntimeError(f"这些 schema 里一条 CREATE TABLE 都没读到：{files}")
    return names


def declared_shape() -> dict[str, set[str]]:
    """**真跑过 `bootstrap()`** 的声明形状（表 → 列名集合）—— 列漂移那格比的就是它。

    这里曾经只 `connect()` 一个空库再数 `sqlite_master`：`connect()` 只设 PRAGMA、不建表
    （`storage/db.py:50`），于是 declared 恒为空、`section_schema` 那个 for 一次都不进，
    两根的 `column_drift_vs_declared` 恒写"无"，而门禁第 12 步 `baseline --check`
    （R26-21 专门为"算了没人看"接进来的那半步）也从此**不可能红**。缺表就抛，不许回空。
    """
    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.domains.registry import DOMAINS  # noqa: PLC0415
    from rolecard_agent.storage.db import bootstrap, connect  # noqa: PLC0415

    conn = connect(_scratch("declared.db"))
    try:
        applied = bootstrap(conn, enabled_domains=DOMAINS)
        shape = {
            t: {r[1] for r in conn.execute(f"PRAGMA table_info('{t}')")}
            for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    missing = sorted(declared_table_names() - set(shape))
    if missing:
        raise RuntimeError(
            f"声明形状建出来缺表 {missing}（跑过的 schema 文件：{applied}）—— "
            "这一格宁可乐，也不能回一个空字典 —— 漂移那格恒等于「无」就是没量。"
        )
    return shape


def data_roots() -> dict[str, Path | None]:
    """两个数据根 + 安装目录里那份随包 dist。开发态根在 2026-09-25 起是**空的**（陈旧快照
    已隔离到 `data/sqlite/_stale-dev-snapshot-20260924/`），这里仍把它列出来，是为了让"两根"
    这件事本身可见，而不是等下一个人再撞一次。"""
    # 两个候选**取自 `scratch_db.CANDIDATE_SOURCES`**（09-26 轮 R26-17）：从前这里按
    # `$LOCALAPPDATA` 自己拼一遍，那边按 `Path.home()/AppData` 硬编码一遍，
    # `persona_meter` 再按 `Settings.sqlite_path` 走第三套 —— 三套规则并存时，
    # "改前/改后"的尺子与被量的 A/B 看的可以不是同一个世界。规则只留一处。
    sys.path.insert(0, str(ROOT / "scripts"))
    import scratch_db  # noqa: PLC0415

    labels = ("dev(repo/data)", "installed(%LOCALAPPDATA%)")
    return dict(zip(labels, scratch_db.CANDIDATE_SOURCES, strict=True))


def section_schema(declared: dict[str, set[str]]) -> dict[str, Any]:
    """声明态（临时新库 = schema.sql 全量）逐根比对实库的列差异 = 迁移漂移的直读证据。"""
    per_root: dict[str, Any] = {}
    for tag, path in data_roots().items():
        if path is None or not path.exists():
            per_root[tag] = {"exists": False}
            continue
        actual = _columns(path)
        drift: dict[str, Any] = {}
        for table, want in declared.items():
            have = actual.get(table)
            if have is None:
                drift[table] = {"missing_table": True}
            elif have != want:
                drift[table] = {"extra": sorted(have - want), "absent": sorted(want - have)}
        per_root[tag] = {
            "exists": True,
            "size_bytes": path.stat().st_size,
            "tables": len(actual),
            "column_drift_vs_declared": drift or "无",
        }
    return per_root


def section_identity() -> dict[str, Any]:
    """一份库里"到底是谁的数据"—— 全部按**集合**记，不按计数（§12.18 那条教训）。"""
    out: dict[str, Any] = {}
    for tag, path in data_roots().items():
        if path is None or not path.exists():
            out[tag] = {"exists": False}
            continue
        conn = _ro_conn(path)
        try:

            def _set(sql: str, *, con: sqlite3.Connection = conn) -> list[str]:
                """查询坏了就炸。**绝不返回空表** —— 第一版在这里吞了 `sqlite3.Error`，
                于是 `SELECT id FROM role_card`（真列名是 `role_id`）报成"这台机器 0 张角色卡"，
                而 0 和"我列名猜错了"在 JSON 里长得一模一样。这份文件是给六个 agent 当真值的。
                """
                return sorted({str(r[0]) for r in con.execute(sql)})

            out[tag] = {
                "exists": True,
                "sessions": _set("SELECT thread_id FROM session_thread"),
                "reachouts": _set("SELECT id FROM agent_reachout"),
                "roles": _set("SELECT role_id FROM role_card"),
                "memory_items": _set("SELECT id FROM role_memory_item"),
                "last_update": (
                    conn.execute("SELECT MAX(updated_at) FROM session_thread").fetchone()[0]
                ),
            }
        finally:
            conn.close()
    return out


def section_artifacts() -> dict[str, Any]:
    """dist 与安装包内那份是否同一版本。判据是**文件名里的 hash**，不是"我记得打过"。"""
    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.core.artifacts import installed_dist  # noqa: PLC0415

    def bundles(directory: Path) -> list[str]:
        if not directory.exists():
            return []
        return sorted(p.name for p in directory.glob("index-*.js"))

    repo_dist = bundles(ROOT / "frontend" / "dist" / "assets")
    # 装后那条 `_internal\frontend\dist` 从前在三个脚本里各拼一遍（台账 R28-59）；现在问一处。
    local_appdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    install_dir = installed_dist(Path(local_appdata)) / "assets"
    in_package = bundles(install_dir)
    return {
        "repo_dist": repo_dist,
        "installed_package": in_package,
        "match": bool(repo_dist) and repo_dist == in_package,
    }


def section_settings() -> dict[str, Any]:
    """配置面有多大。**两种口径都记**，并标出它们吻不吻：

    `check_consistency.py` 的"dead config"是按**源码正则**数字段（`^    name :`），
    而 pydantic 自己知道的是 `model_fields`。两个数一旦不等，就说明有一个口径在说谎 ——
    那本身就是审计输入，不该由我这边再悄悄统一成其中一个。
    """
    import re  # noqa: PLC0415

    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.config import Settings  # noqa: PLC0415
    from rolecard_agent.core.runtime_settings import RUNTIME_FIELDS  # noqa: PLC0415

    cfg = ROOT / "src" / "rolecard_agent" / "config.py"
    text = cfg.read_text(encoding="utf-8")
    start = text.index("class Settings(")
    tail = text[start + 1:]
    end = len(tail)
    for marker in ("\nclass ", "\ndef ", "\n@app", "\nif __"):
        found = tail.find(marker, 1)  # 从 1 起：marker 允许顶格，但跳过 class Settings 自己
        if found != -1:
            end = min(end, found)
    body = tail[:end]
    # 与 check_consistency 的"dead config"同一个形状（四个空格 + 名字 + 冒号），但**只截
    # class Settings 那一段**：第一版直接对整份 config.py 跑正则，量到的是"文件里所有类的
    # 字段之和"（66），和 pydantic 报的 48 差 18 —— 那 18 个是别的类的，不是谁在说谎，
    # 是两个口径量的不是同一件事。口径不同就要写清，别让人以为在争论事实。
    regex_count = len(re.findall(r"^\s{4}([a-z][a-z0-9_]*)\s*:", body, flags=re.M))
    model_count = len(Settings.model_fields)
    return {
        "pydantic_model_fields": model_count,
        "regex_counted_in_settings_body": regex_count,
        "the_two_agree": model_count == regex_count,
        "note": "一致性脚本量的仍是整份 config.py（口径更宽），此处的差值不是缺陷",
        "runtime_editable": len(RUNTIME_FIELDS),
    }


def _py_files(base: Path):
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def section_hardcoded_identity() -> dict[str, Any]:
    """`DEFAULT_USER_ID` 被写死在多少地方 —— 审计里被手数过四次、每次都得一个新数的那个格。

    为什么这里一次给**四把尺子**而不是"一个正确的数"：同一个事实先后被记成"约 15 处"（09-19
    手估）、"22 处"（09-25，只数 `api/` 下的**行**）、"按 AST 复算 21 处"（09-26，整个 `src/`
    的语法节点，但**把常量定义那一行也算成了一次用法** —— 这把我自己的代码第一版犯了同一个错，
    见 `name_loads` 的注释）、以及"26"（整个 `src/` 按行，含定义与五处 import 与装配根的播种）。
    **它们从来不是四个事实，是四把尺子** —— 分歧没发生在数据上，只发生在有没有说清怎么数。
    于是这节把四把都算出来写进 JSON：文档引用时要么点这个键，要么就得写明用的是哪一把。
    真开工做多租户时，"还剩多少处要改"是 `src_ast.uses`（**20**：只数真的用了它的地方）。
    """
    import ast  # noqa: PLC0415

    api = ROOT / "src" / "rolecard_agent" / "api"
    whole = ROOT / "src"

    def grep_lines(base: Path) -> int:
        return sum(
            1
            for path in _py_files(base)
            for ln in path.read_text(encoding="utf-8").splitlines()
            if "DEFAULT_USER_ID" in ln
        )

    def name_loads(base: Path) -> dict[str, int]:
        """只数**真正用了这个常量**的地方：`ast.Name` 的 **Load** 上下文。

        这个 `ctx` 过滤不是洁癖，是本节存在的理由：第一版没写它，于是 `base/identity.py:17`
        那行 `DEFAULT_USER_ID = "local-user"`（定义）被算成一次用法，整个 `src/` 读出 21 ——
        与 09-26 那句"按 AST 复算是 21 处"一字不差。**原来那个数不是数错了别的东西，
        是把定义当成了使用**，而它当时大概也是这么产生的。
        """
        out = {"uses": 0, "in_call_args": 0}
        for path in _py_files(base):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Name)
                    and node.id == "DEFAULT_USER_ID"
                    and isinstance(node.ctx, ast.Load)
                ):
                    out["uses"] += 1
                if isinstance(node, (ast.Call, ast.keyword)):
                    args = list(node.args) if isinstance(node, ast.Call) else [node.value]
                    out["in_call_args"] += sum(
                        1
                        for a in args
                        if isinstance(a, ast.Name) and a.id == "DEFAULT_USER_ID"
                    )
        return out

    return {
        "api_layer_grep_lines": grep_lines(api),
        "src_grep_lines": grep_lines(whole),
        "api_layer_ast": name_loads(api),
        "src_ast": name_loads(whole),
        "note": "四个数量问的是同一件事的四种口径：行 vs 语法节点、api/ vs 整个 src/。"
        "引用时点名口径，别再当场 grep 出一个第五数。",
    }


def section_citations() -> dict[str, Any]:
    """代码里对架构审计的引用规模 —— 决定"这份文档能不能搬"的唯一依据。

    `check_consistency.py` 的 doc-link 检查只查 markdown 链接，**查不到 .py 里的散文引用**，
    所以搬走文件的后果是静默断链。这一节就是把这个静默变成可数的。
    """
    hits: list[str] = []
    for base in SRC_DIRS:
        directory = ROOT / base
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".js"}:
                continue
            if "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "架构审计" in text:
                hits.append(str(path.relative_to(ROOT)))
    return {"files": len(hits), "list": sorted(hits)}


def section_growth(installed: Path | None) -> dict[str, Any]:
    """几张**只增不减**的表现在多大。

    单独立一节是因为它们的"增长"不是 bug 而是设计（审计留痕、检查点历史），但**没人记着
    它们会有多大** —— 一次全项目审计里"检查点会不会无限长"这种问题必须能用一条数回答。
    """
    if installed is None or not installed.exists():
        return {"exists": False}
    conn = _ro_conn(installed)
    try:
        tables = (
            "checkpoints",
            "writes",
            "audit_log",
            "token_usage_day",
            "role_memory_item",
            "agent_reachout",
            "session_thread",
            "role_card",
        )
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()


def build() -> dict[str, Any]:
    declared = declared_shape()
    installed = next(
        p for tag, p in data_roots().items() if tag.startswith("installed")
    )
    return {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "git": section_git(),
        "settings": section_settings(),
        "routes": section_routes(),
        "schema_per_root": section_schema(declared),
        # 漂移那格的分母要一起上屏：`column_drift_vs_declared="无"` 在分母为 0 的时候与
        # "真的没有漂移"长得一模一样（`R102-31`），所以这里明写" declared 有几张表几列"。
        "schema_declared": {
            "tables": len(declared),
            "columns": sum(len(cols) for cols in declared.values()),
            "built_by": "storage.db.bootstrap(enabled_domains=DOMAINS)",
        },
        "data_identity": section_identity(),
        "table_growth_installed_root": section_growth(installed),
        "artifacts": section_artifacts(),
        "audit_citations": section_citations(),
        "hardcoded_identity": section_hardcoded_identity(),
    }


def _drift_lines(data: dict[str, Any]) -> list[str]:
    """各根上"实库列集合 ≠ schema 声明"的那些条。空表 = 没有列漂移。"""
    out: list[str] = []
    for root, entry in (data.get("schema_per_root") or {}).items():
        if not isinstance(entry, dict):
            continue
        drift = entry.get("column_drift_vs_declared")
        if drift and drift not in ("无", "none", "None", []):
            out.append(f"{root}: {drift}")
    return out


def main() -> None:
    # `--check`：只问一句话"这份库与声明有没有列漂移"，漂移就非零退出。
    # 从前 baseline.py 是唯一会算列漂移的脚本，却既不在门禁也不在 CI（09-26 轮 R26-21）——
    # 算了没人看，等于没有。`--check` 就是把它接进门禁的那半个接口。
    if "--check" in sys.argv:
        drift = _drift_lines(build())
        if drift:
            print("列漂移（实库 ≠ 声明）：\n  " + "\n  ".join(drift))
            raise SystemExit(1)
        print("无列漂移")
        return
    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    routes = data["routes"]["count"]
    ident = data["data_identity"]
    st = data["settings"]
    print(f"HEAD {data['git']['head']} 脏 {data['git']['dirty_lines']} 行｜路由 {routes}"
          f"｜Settings {st['pydantic_model_fields']} 字段"
          f"{'（与一致性脚本的数法不一致！）' if not st['the_two_agree'] else ''}")
    for tag, info in ident.items():
        if not info.get("exists"):
            print(f"  {tag}: 不存在")
            continue
        print(f"  {tag}: 会话 {len(info['sessions'])} 主动 {len(info['reachouts'])}"
              f" 角色 {len(info['roles'])} 记忆条 {len(info['memory_items'])}"
              f" 最后更新 {info['last_update']}")
    art = data["artifacts"]
    print(f"  dist 与包内一致: {art['match']}  {art['repo_dist']} vs {art['installed_package']}")
    print(f"  审计引用文件数: {data['audit_citations']['files']}")
    hid = data["hardcoded_identity"]
    print(f"  DEFAULT_USER_ID：api 层按行 {hid['api_layer_grep_lines']}｜整个 src 按行 "
          f"{hid['src_grep_lines']}｜api 层用法 {hid['api_layer_ast']['uses']}｜整个 src 用法 "
          f"{hid['src_ast']['uses']}（多租户要改的就是这一条）")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
