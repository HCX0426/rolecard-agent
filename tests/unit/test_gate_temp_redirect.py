"""门禁把子进程临时目录搬进仓内每趟命名空间 —— 这条判据钉两件事，各对应一发实测假红。

背景（2026-11-04）：受限启动上下文里跑门禁时，系统 `%TEMP%` 间歇性不可写 ——
`shell updater 清理判据` 7 臂全栽 `EPERM: mkdtemp`，下一趟 `前端 vitest` 收集前
死在 `EPERM: open vitest-coverage-*.lock`。判据依赖"系统 TEMP 此刻可不可写"
与代码无关，是环境掷骰子。修复 = `_redirect_temp_for_children()`：TMPDIR/TEMP/TMP
指向 `build/tmp/gate-<pid>`（atexit 清扫），并在 `main()` 第一个 spawn 之前调用。

两条断言对应两个可能被后人拆掉的点：
1. **环境真的被改了**（改函数体成 no-op → 红）；
2. **main() 真的调了它**（注释掉调用 → 红）—— 函数本身写得再对，没人调也是零。
第 2 条最初用源码子串匹配写，变异测试当场打脸：把调用**注释掉**后字面量还在
注释里、照样命中，绿的 —— 换成 AST（注释不进语法树）才算数。
"""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_gate():
    spec = importlib.util.spec_from_file_location("gate_under_test", ROOT / "scripts" / "gate.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_redirect_points_all_three_keys_inside_the_per_run_namespace():
    """TEMP/TMP/TMPDIR 三键都落进 `build/tmp/gate-<本进程pid>`，目录真实存在。"""
    gate = _load_gate()
    keys = ("TMPDIR", "TEMP", "TMP")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        gate._redirect_temp_for_children()
        expected = ROOT / "build" / "tmp" / f"gate-{os.getpid()}"
        for key in keys:
            value = os.environ.get(key, "")
            # 三键全设：python 认 TMPDIR>TEMP>TMP、node 在 Windows 认 TEMP>TMP，
            # 漏任何一键，另一端仍落到系统 TEMP（= 那两发假红的原地）。
            assert value == str(expected), f"{key} 未指向本趟命名空间：{value!r}"
        assert expected.is_dir(), f"本趟临时目录没建成：{expected}"
    finally:
        # 回滚：pytest 进程自己的环境不能被这条测试污染到后面的用例。
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def test_main_invokes_the_redirect_before_the_first_possible_spawn():
    """`main()` 必须**以真实调用**（非注释）在第一个 spawn 之前执行该重定向。

    用 AST 而非源码子串：子串匹配会把注释掉的调用也当命中（变异测试实测过这一枪：
    仅注释调用仍全绿）。`_changed_paths` 是 main 里第一处触发 spawn 的调用
    （pytest_plan 支与正常路径各一处，取最早行号）；晚于它 = 首个子进程仍继承系统 TEMP。
    """
    tree = ast.parse((ROOT / "scripts" / "gate.py").read_text(encoding="utf-8"))
    main_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n for n in ast.walk(main_fn) if isinstance(n, ast.Call)]

    def callee(call: ast.Call) -> str:
        fn = call.func
        if isinstance(fn, ast.Name):
            return fn.id
        return getattr(fn, "attr", "")

    redirect_lines = [c.lineno for c in calls if callee(c) == "_redirect_temp_for_children"]
    assert redirect_lines, "main() 里没有 _redirect_temp_for_children() 的真实调用"
    spawn_lines = [c.lineno for c in calls if callee(c) == "_changed_paths"]
    assert spawn_lines, "main() 里找不到 _changed_paths 调用，判据基线变了，先重审"
    assert min(redirect_lines) < min(spawn_lines), (
        "重定向必须排在第一个 spawn 之前："
        f"redirect@{min(redirect_lines)} 不早于 _changed_paths@{min(spawn_lines)}"
    )
