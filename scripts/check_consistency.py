"""Repo consistency check. Run in CI and before every commit.

Why this exists: as the project went through several rounds of revision, documents and
code drifted apart (a renamed config key still referenced in a docstring, stale project
names, requirements files pulling in scope the version does not need). Eyeballing does
not catch these - the checker caught one on its very first run.

Usage:
    python scripts/check_consistency.py        # exits 1 on failure, 0 on pass

**这一文件是薄包装器**（快照批次 5「拆包」那一格的第一刀，2026-10-07）：
判据本体住 `scripts/consistency/`
包（core = 共享状态与遍历；checks = 67 条判据正文；registry = CHECKS 执行顺序）。
行为契约不变：同样的断言、同样的输出、退出码 1 = 有红。

兼容面（按 `import check_consistency` 拿属性的历史消费者，读与写都转发到 checks）：

* ``tests/unit/test_app_icon_corners.py`` → ``_png_corner_alphas``
* ``tests/unit/test_ci_host_python_imports.py`` → ``check_ci_host_python_stdlib_only``
  与 ``_imported_modules``
* ``tests/unit/test_artifacts.py`` → ``check_artifact_single_source`` 与
  ``_div_chain_parts``
* ``scripts/tools/build_audit_index.py`` → ``_TARGET_HEAD_RE`` / ``_TARGET_ROW_RE``
"""

from __future__ import annotations

import sys
import types

from consistency import core

# 唯一的静态 from-import 消费者（build_audit_index.py）要的两个正则 —— 显式再导出，
# mypy 才看得见；其余历史消费者走下面的转发。
from consistency.checks import _TARGET_HEAD_RE, _TARGET_ROW_RE  # noqa: F401
from consistency.registry import CHECKS


class _ForwardingModule(types.ModuleType):
    """读与写都转发到 checks：既有测试不止**读**属性（``cc._png_corner_alphas``），还
    **写**（``monkeypatch.setattr(cc, "ROOT", repo)``、``cc.out = …``）—— 只转发读，
    替身就打不到判据真正住的那一侧，测试会假绿。按"checks 里已有这个名字就写过去"
    分流，其余照常落在本模块（main/CHECKS 等自己的名字）。
    """

    def __getattr__(self, name: str):  # PEP 562
        import consistency.checks as _checks

        return getattr(_checks, name)

    def __setattr__(self, name: str, value: object) -> None:
        import consistency.checks as _checks

        if hasattr(_checks, name):
            setattr(_checks, name, value)
        else:
            super().__setattr__(name, value)


# 换类前先确认本模块真的注册在 sys.modules 里：build_audit_index.py 用合成名
# （"cc_for_index"）按路径加载本文件，那种实例不在 sys.modules、也不需要转发。
_self = sys.modules.get(__name__)
if _self is not None:
    _self.__class__ = _ForwardingModule


def main() -> int:
    for check in CHECKS:
        check()

    print("\n--- WARNS ---（不进红，但也不假装没看见）")
    for item in core.warns or ["none"]:
        print("  " + item)

    print("\n--- FAILS ---")
    for item in core.fails or ["none"]:
        print("  " + item)

    # The count is reported here rather than quoted in the docs: a hard-coded number in
    # prose goes stale the moment a check is added, which is the exact failure mode this
    # script exists to prevent.
    print(f"\nassertions: {core.passed} passed, {len(core.fails)} failed")
    print("RESULT:", "PASS" if not core.fails else f"{len(core.fails)} FAILING CHECK(S)")
    return 0 if not core.fails else 1


if __name__ == "__main__":
    sys.exit(main())
