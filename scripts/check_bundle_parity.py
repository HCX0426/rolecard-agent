"""随包后端的 **import↔bundle parity**：`src/` 真 import 的第三方顶层模块，必须真在打出来的包里。

为什么单独立这一条（2026-09-28 轮 `R28-34`）：`packaging/rolecard-backend.spec` 的
`hiddenimports` 是一份**手抄清单**，而它抄的是"构建机这台 .venv 恰好装过什么"。装了就进包、
没装就**安静地什么都不进**（`collect_submodules()` 对不存在的包返回空列表，不报错 —— spec 里
`sys.path` 那一行记的就是这个坑的另一半）。于是"随包后端里有没有某个模块"这件事，从此没有任何
尺子量过。实锤的那一条：`langchain_mcp_adapters` 在 .venv 里根本没装 ⇒ 包里 0 个模块 ⇒
**打包态的 MCP 永远 fail-open**：设置→扩展那面板照常能增删 server、交通灯照常画，而工具永远
加载不出来，日志里只有一句 warning。B/S 形态还能靠 `pip install -r requirements-mcp.txt` 自救，
桌面包里的人没有 pip —— 那句提示对他是一句不可执行的建议。

判据：AST 扫 `src/**/*.py` 的全部 import（**含函数体内的 lazy import** —— MCP 那一条正是
`from langchain_mcp_adapters.client import MultiServerMCPClient` 写在函数里，静态"看起来没在用"），
顶层名去 stdlib、去第一方，逐个去问已打好的 bundle：PYZ 的 TOC 里有没有，或 `_internal/` 下
有没有同名的目录 / `.pyd`。缺了就红，红清单上带**第一条触发它的文件:行**，直接可定位。

`--allow` 那一族是"刻意不进包"的登记处（与 `check_consistency.py` 的 `import_dist_aliases` 同一个
思路：允许例外，但例外必须署名并写理由，新增一项就得在这里长一行）。本轮**一条都没用** ——
除了 MCP 该进包，实测 src 没有第二个模块缺席。

用法（解释器用仓库那一份，别用裸 python）：

    .venv\\Scripts\\python.exe scripts\\check_bundle_parity.py
    .venv\\Scripts\\python.exe scripts\\check_bundle_parity.py
    # 换一份产物来量：--bundle build/sidecar/rolecard-backend

退出码：0 = parity 成立（或**根本没打过包** —— 没产物不是负面，按本仓"未知不拦"的同一判据跳过，
并把这个判断打在输出里）；1 = 有模块缺席；2 = bundle 存在但读不出 PYZ（产物坏了，另说）。

放在哪跑：`scripts/gate.py` 的 **full 档**（快档没有 bundle，跑了也只是跳过）。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_BUNDLE = ROOT / "build" / "sidecar" / "rolecard-backend"

# 刻意不进包的第三方顶层名 → 理由。每一条都得是"想过并写下了"的理由，不是"忘了"。
# 目前为空：唯一一条缺席（MCP）在 09-29 被判定为**该进包**（只增 1.87 MB：
# langchain-mcp-adapters 0.16 + mcp 1.71，安装包 191 MB 的 1%，且 pip dry-run 显示
# 除这两个包外没有任何其他版本被推动 —— websockets 保持 16.1.1，不动 langgraph-sdk 的 `<17`）。
NOT_BUNDLED_BY_DESIGN: dict[str, str] = {}


def scan_third_party_imports(src: Path) -> dict[str, str]:
    """`src/` 里 import 到的第三方顶层模块名 → 第一条它的 `文件:行`。

    行号是为了红的时候不用再 grep 一遍。stdlib 用 `sys.stdlib_module_names` 判，不手抄清单；
    第一方 `rolecard_agent` 直接排掉（它由 spec 的 `collect_submodules` 整包收）。
    """
    found: dict[str, str] = {}
    stdlib = cast("frozenset[str]", getattr(sys, "stdlib_module_names", frozenset()))
    for path in sorted(src.rglob("*.py")):
        # 相对**被扫的那棵树**算，不相对仓库根：单测要能用 fixture 里的假 `src/`，
        # 而 `relative_to(ROOT)` 对仓库外的路径直接抛 ValueError（第一版就这么挂的）。
        rel = path.relative_to(src.parent).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:  # 不是我们的码，交给 ruff/mypy 去红
            continue
        for node in ast.walk(tree):
            # 行号取在两个 isinstance 分支**里面**：`ast.AST` 这个基类型上没有 lineno，
            # 把它提到外面去读，mypy 直接拦下（而这条检查的价值恰恰在行号可定位）。
            if isinstance(node, ast.Import):
                seen = [(alias.name, node.lineno) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                seen = [(node.module, node.lineno)]
            else:
                continue
            for name, lineno in seen:
                top = name.split(".")[0]
                if top in stdlib or top == "rolecard_agent" or top in found:
                    continue
                found[top] = f"{rel}:{lineno}"
    return found


def bundle_top_names(bundle: Path) -> set[str]:
    """已打好的 sidecar 里**可用**的顶层模块名：PYZ 的 TOC ∪ `_internal/` 下的目录与扩展模块。

    两条都要：纯 Python 包进 PYZ，而 `.pyd`（如 `_cffi_backend`）与带原生库的包是落在
    `_internal/` 目录里的，只看 PYZ 会把它们读成缺席 —— 那是假阳性，会把人推去改根本没错的 spec。
    """
    from PyInstaller.archive.readers import CArchiveReader  # noqa: PLC0415 - 只有打包检查才需要它

    exe = bundle / "rolecard-backend.exe"
    if not exe.exists():
        msg = f"bundle 里没有可执行文件：{exe}"
        raise FileNotFoundError(msg)
    archive = CArchiveReader(str(exe))
    pyz = [name for name, entry in archive.toc.items() if entry[-1] == "z"]
    if not pyz:
        kinds = sorted({entry[-1] for entry in archive.toc.values()})
        msg = f"{exe} 里找不到 PYZ 条目（CArchive toc 类型分布：{kinds}）"
        raise ValueError(msg)
    toc = archive.open_embedded_archive(pyz[0]).toc
    names = {str(key).split(".")[0] for key in toc}

    internal = bundle / "_internal"
    if internal.is_dir():
        for child in internal.iterdir():
            if child.is_dir():
                names.add(child.name.lower())
            elif child.suffix in (".pyd", ".so"):
                names.add(child.stem.split(".")[0].lower())
    return names


def missing_from_bundle(
    imports: dict[str, str],
    bundled: set[str],
    allow: dict[str, str] | None = None,
) -> dict[str, str]:
    """缺席的模块 → 触发它的 `文件:行`。

    名字比较按 bundle 侧归一小写：`_internal/` 下的目录名各家风格不一（`PIL`、`PyPDF2`…），
    大小写敏感只会造出一批"看起来缺了"的假阳性。

    「刻意不进包」只在**写了非空理由**时生效：一条空理由的登记与"忘了"在效果上一模一样，
    却会给出绿灯 —— 那比红更贵。
    """
    allowed = allow if allow is not None else NOT_BUNDLED_BY_DESIGN
    return {
        module: where
        for module, where in imports.items()
        if module.lower() not in bundled and not allowed.get(module, "").strip()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="随包后端的 import↔bundle parity")
    parser.add_argument(
        "--bundle",
        default=str(DEFAULT_BUNDLE),
        help="sidecar 目录（默认 build/sidecar/rolecard-backend）",
    )
    args = parser.parse_args()

    # Windows 控制台默认 GBK，中文步骤名与「✅」会在这里抛 UnicodeEncodeError —— 与 gate.py 同一族。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    bundle = Path(args.bundle)
    if not (bundle / "rolecard-backend.exe").exists():
        print(
            f"⏭️  没有随包后端（{bundle} 不存在或还没打出 exe）—— 没打过包不是负面，跳过。\n"
            "    要量这件事先跑：.venv\\Scripts\\python.exe scripts\\build_sidecar.py"
        )
        return 0

    imports = scan_third_party_imports(ROOT / "src")
    try:
        bundled = bundle_top_names(bundle)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ bundle 读不出 PYZ：{exc}", file=sys.stderr)
        return 2

    missing = missing_from_bundle(imports, bundled)
    if missing:
        print(
            f"❌ src import 的 {len(imports)} 个第三方顶层模块里，{len(missing)} 个不在随包后端里"
            f"（bundle 顶层名 {len(bundled)} 个）：",
            file=sys.stderr,
        )
        for module, where in sorted(missing.items()):
            print(f"   - {module}  ← 第一次 import 在 {where}", file=sys.stderr)
        print(
            "   两种收法二选一：\n"
            "     ① 该进包 ⇒ 在 spec 的 RUNTIME_PACKAGES 里补上它。注意 `collect_submodules()`\n"
            "        对没安装的包**返回空列表且不报错**，所以补名字之前先确认构建机装过\n"
            "        （`importlib.util.find_spec(\"…\") is not None`）。\n"
            "     ② 刻意不进 ⇒ 加进 scripts/check_bundle_parity.py 的 NOT_BUNDLED_BY_DESIGN\n"
            "        并写理由（空理由不算理由，照样红）。\n"
            "   放着手不管是最坏的那种：界面照常摆着那格，而它永远点不出东西。",
            file=sys.stderr,
        )
        return 1

    print(
        f"✅ 随包后端 import parity：src 的 {len(imports)} 个第三方顶层模块全在包里"
        f"（bundle 顶层名 {len(bundled)} 个）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
