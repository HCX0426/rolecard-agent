"""评测脚本的 API 路径尺子（`consistency.checks_meta.check_eval_harness_api_paths`）。

2026-10-27 实查出来的病根：`scripts/tools/run_eval.py` 一直打 `/api/plugins/{d}/toggle`，
那个端点在 2026-10-24 的"域常开"改造里**退役**了 —— 域一律常开，工具可见性改由角色的
`tool_whitelist` 决定。评测脚本没跟着改，于是每一趟评测的 11 条用例全部死在**对齐那一步**
（先是来源护栏 403，改成回环来源后是 405），通过率恒为 0%，而报告照常生成 ——
一份"0% 通过率"的 JSON 躺在 docs/eval/ 里，没人会当回事。

这类漂移必须由机器拦，理由是**它不会让任何测试红**：评测要真模型才跑得动，
而端点退役是有意的架构决定、**忘记改调用方**不是。调用方的错只体现为指标恒 0。

这把尺子的两个臂都必须能红：
  * 正向 —— 评测脚本里出现 api 层没有的路径 ⇒ 红（退役端点那一类）；
  * 反向 —— **f-string 动态路径必须被看见**（`f"/api/roles/{role_id}"`）。
    只认字面量的实现会漏掉它，于是"尺子报绿而脚本里明明留着退役端点"，
    恰好是这把尺子要防的事（2026-10-27 实测：改造后第一版尺子报绿）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from consistency import checks_meta  # noqa: E402


def _literal(node: ast.AST) -> str | None:
    return checks_meta._path_literal(node)


def test_字面量路径原样取出() -> None:
    assert _literal(ast.parse('"/api/session"').body[0].value) == "/api/session"


def test_fstring_路径还原成占位形状() -> None:
    """`f"/api/roles/{role_id}"` ⇒ `/api/roles/{}` —— 与路由表侧同一个抹平口径。"""
    node = ast.parse('f"/api/roles/{role_id}"').body[0].value
    assert _literal(node) == "/api/roles/{}"


def test_非路径表达式一律_none() -> None:
    assert _literal(ast.parse("some_var").body[0].value) is None
    assert _literal(ast.parse("123").body[0].value) is None


def test_路由表侧对_fstring_也认() -> None:
    """路由装饰器里的路径同样可能是 f-string —— 两侧口径必须一致。"""
    node = ast.parse('f"/api/session/{thread_id}"').body[0].value
    assert _literal(node) == "/api/session/{}"


def test_评测脚本当前打的路径都存在() -> None:
    """正向臂：仓里现状必须是绿的（否则这把尺子自己坏了）。"""
    api_dir = ROOT / "src" / "rolecard_agent" / "api"
    known: set[str] = set()
    for path in sorted(api_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in {"get", "post", "put", "patch", "delete"}:
                continue
            for arg in node.args:
                raw = checks_meta._path_literal(arg)
                if raw is not None and raw.startswith("/api/"):
                    known.add(raw)

    harness = ROOT / "scripts" / "tools" / "run_eval.py"
    tree = ast.parse(harness.read_text(encoding="utf-8", errors="ignore"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in {"get", "post", "put", "patch", "delete"}:
            continue
        for arg in node.args:
            raw = checks_meta._path_literal(arg)
            if raw is not None and raw.startswith("/api/"):
                called.add(raw)

    assert called, "评测脚本一条 API 路径都没打出来 —— 尺子的抽法坏了"
    import re

    norm = lambda p: re.sub(r"\{[^}]*\}", "{}", p)  # noqa: E731
    unknown = sorted(p for p in called if norm(p) not in {norm(k) for k in known})
    assert not unknown, f"评测脚本打了 api 层不存在的路径：{unknown}"


def test_退役的插件端点已经被评测脚本忘掉() -> None:
    """反向臂：`/api/plugins/.../toggle` 退役后，评测脚本不许再打它。

    钉这一条是因为它是**真实发生过**的那个漂移（域常开改造后忘了改调用方），
    而这类忘改不会被任何测试看见 —— 它只会让指标恒为 0。

    判据看的是**真的调了那个路径**（AST 里的调用），不是文件里出现过那串字：
    脚本里保留一段"那个端点为什么退役"的说明是有价值的，删掉它反而丢信息。
    """
    harness = ROOT / "scripts" / "tools" / "run_eval.py"
    tree = ast.parse(harness.read_text(encoding="utf-8", errors="ignore"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in {"get", "post", "put", "patch", "delete"}:
            continue
        for arg in node.args:
            raw = checks_meta._path_literal(arg)
            if raw is not None and raw.startswith("/api/"):
                called.add(raw)

    assert not [p for p in called if p.startswith("/api/plugins")], (
        "评测脚本又在打 /api/plugins/... —— 那个端点 2026-10-24 已退役"
        "（域常开，工具可见性由角色 tool_whitelist 决定）"
    )
