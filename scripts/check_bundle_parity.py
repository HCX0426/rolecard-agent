"""随包后端的 **import↔bundle parity**：`src/` 真 import 的第三方顶层模块，必须真在打出来的包里。

为什么单独立这一条（2026-09-28 轮 `R28-34`）：`packaging/rolecard-backend.spec` 的
`hiddenimports` 是一份**手抄清单**，而它抄的是"构建机这台 .venv 恰好装过什么"。装了就进包、
没装就**安静地什么都不进**（`collect_submodules()` 对不存在的包返回空列表，不报错 —— spec 里
`sys.path` 那一行记的就是这个坑的另一半）。于是"随包后端里有没有某个模块"这件事，从此没有任何
尺子量过。实锤的那一条：`langchain_mcp_adapters` 在 .venv 里根本没装 ⇒ 包里 0 个模块 ⇒
**打包态的 MCP 永远 fail-open**：设置→扩展那面板照常能增删 server、交通灯照常画，而工具永远
加载不出来，日志里只有一句 warning。B/S 形态还能靠
`pip install -r requirements/requirements-mcp.txt` 自救，
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
sys.path.insert(0, str(ROOT / "src"))
from rolecard_agent.core.artifacts import sidecar_bundle  # noqa: E402

#: sidecar 的位置只有一个拼法（`core/artifacts.py`，10-01 台账 R28-59）。
DEFAULT_BUNDLE = sidecar_bundle(ROOT)

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


#: 随包后端的 spec —— `RUNTIME_PACKAGES` 只有一个读法，两边（这条尺子与门禁里那条
#: "模块↔依赖族"断言）都从这里要，别各写一遍 AST。
SPEC_PATH = ROOT / "packaging" / "rolecard-backend.spec"


def spec_runtime_packages() -> list[str]:
    """spec 里 `RUNTIME_PACKAGES` 那串模块名。**AST 读，不执行 spec** ——
    执行它要 PyInstaller 在场（`Analysis`/`PYZ` 是它注入的全名），而 CI 的 gate job 没装它。
    """
    if not SPEC_PATH.exists():
        return []
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8", errors="ignore"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", "") == "RUNTIME_PACKAGES" for target in node.targets
        ):
            return [str(item) for item in ast.literal_eval(node.value)]
    return []


def lazy_third_party_imports(src: Path) -> dict[str, str]:
    """**函数体/类体内**的第三方 import → 第一条 `文件:行`。

    为什么单独立一个扫描器（10-01，M2 那发变异照出来的洞）：`RUNTIME_PACKAGES` 的作用是把
    一个包**整族**收进来（`collect_submodules` + `collect_data_files`），而"整族"这件事对
    懒加载那几族尤其要紧 —— 它们在运行到那一行之前根本不出现在调用图里，子模块与数据文件
    全靠这份清单。于是"有人把某族从清单里摘掉"这件事，只要 src 还在懒加载它，就是
    **构建成功、症状等用户点到那一格才出现** —— 与 `R28-34` 那个"MCP 永远 fail-open"一字不差，
    只是方向反过来：那次是清单少写了，这次是清单被删。
    顶层 import 不在本条范围内（PyInstaller 的 import 图自己跟得到，不需要这份清单）。
    """
    found: dict[str, str] = {}
    stdlib = cast("frozenset[str]", getattr(sys, "stdlib_module_names", frozenset()))
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src.parent).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for holder in ast.walk(tree):
            if not isinstance(
                holder, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ):
                continue
            for node in ast.walk(holder):
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


#: **靠 import 图自己就能收到、不需要整族清单**的那些族 → 理由。
#: 与 `NOT_BUNDLED_BY_DESIGN` 是两本账，不能并成一本书：后者说"这族刻意不进包"，
#: 这一本说"这族进包，但不用 `collect_submodules` 整族收"。并成一本书就会有一本签错字。
#: 10-01 那两条形如实测：对刚打出的那份 bundle 读顶层名（284 个），两条都在。
COLLECTED_BY_IMPORT_GRAPH: dict[str, str] = {
    "httpx": "纯 Python、无数据文件、无动态子模块加载；`rag/retriever.py:154` 那句函数体内 "
    "import 已被 import 图跟到 —— 10-01 对刚打的 bundle 实测顶层名里有它",
    "pypdf": "同上：`rag/parser.py:97` 懒加载，10-01 实测已在 bundle 顶层名里；"
    "它不像 chromadb 那样按 entry point 找子模块，所以不需要整族收集",
}


def missing_from_spec(
    lazy: dict[str, str],
    listed: list[str],
    allow: dict[str, str] | None = None,
) -> dict[str, str]:
    """懒加载得到、却没在 `RUNTIME_PACKAGES` 里的第三方族。

    例外记在 `COLLECTED_BY_IMPORT_GRAPH`（**空理由不算理由**，与 `NOT_BUNDLED_BY_DESIGN`
    同一条纪律）：那本账上每一条都要能说出"为什么整族收集对它不适用"。
    """
    allowed = allow if allow is not None else COLLECTED_BY_IMPORT_GRAPH
    listed_lower = {name.lower() for name in listed}
    return {
        module: where
        for module, where in lazy.items()
        if module.lower() not in listed_lower and not allowed.get(module, "").strip()
    }


def bundle_top_names(bundle: Path) -> set[str]:
    """已打好的 sidecar 里**可用**的顶层模块名：PYZ 的 TOC ∪ `_internal/` 下的目录与扩展模块。

    两条都要：纯 Python 包进 PYZ，而 `.pyd`（如 `_cffi_backend`）与带原生库的包是落在
    `_internal/` 目录里的，只看 PYZ 会把它们读成缺席 —— 那是假阳性，会把人推去改根本没错的 spec。
    """
    exe = bundle / "rolecard-backend.exe"
    if not exe.exists():
        # 存在性判断**在 import PyInstaller 之前**：CI 的 venv 里没有打包工具（它不打 Windows 包），
        # 先 import 会让"产物不存在"这一分支读成 ModuleNotFoundError
        # —— 09-29 那次 CI 红就红在这个假设上，与这条尺子防的是同一件事。
        msg = f"bundle 里没有可执行文件：{exe}"
        raise FileNotFoundError(msg)
    from PyInstaller.archive.readers import CArchiveReader  # noqa: PLC0415 - 只有打包检查才需要它

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


def declared_schema_files(src: Path) -> list[str]:
    """源码声明族里"运行时会在包里找"的建表脚本 → 它们在包内的相对路径。

    形状照抄 spec 与 `storage/db.py:domain_schema_path()`：内核两份 + 每个域目录一份。
    这一条防的是 `R28-33`：spec 当年手抄四份，新增一个带 schema 的域插件之后，
    源码态建表正常、**打包态建表直接失败**，构建期一句报警都没有。
    """
    # 数法只有一个出处（台账 `R28-59` 的一半）：`core/artifacts.py::schema_package_paths` ——
    # 从前这里与 spec 各写一遍同样的 glob，两条规则一分叉，症状就是
    # 「源码态建表正常、打包态建表直接失败」而构建期零报警（`R28-33`）。
    from rolecard_agent.core.artifacts import schema_package_paths  # noqa: PLC0415

    pkg = src / "rolecard_agent"
    return [f"{dest}/schema.sql" for _, dest in schema_package_paths(pkg)]


def bundled_schema_files(bundle: Path) -> set[str]:
    """已打好的包里实际带着的建表脚本（相对 `_internal` 的路径，统一正斜杠）。"""
    internal = bundle / "_internal"
    if not internal.is_dir():
        return set()
    return {
        p.relative_to(internal).as_posix()
        for p in internal.rglob("schema.sql")
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

    # —— 这一段**不需要产物**（10-01）：spec 那份清单与 src 的懒加载对读，本机与 CI 都能天天查。
    # 放在"没有 bundle 就跳过"那句**前面**，否则这条尺子只有打完工才醒一次，而它防的正是
    # "打完才发现少收一族"。
    lazy = lazy_third_party_imports(ROOT / "src")
    listed = spec_runtime_packages()
    if not listed:
        print(
            f"❌ 读不到 {SPEC_PATH.relative_to(ROOT)} 里的 RUNTIME_PACKAGES —— 这条尺子自己瞎了",
            file=sys.stderr,
        )
        return 2
    gaps = missing_from_spec(lazy, listed)
    if gaps:
        print(
            f"❌ src 里**懒加载**的 {len(lazy)} 族第三方模块里，{len(gaps)} 族不在 spec 的 "
            f"RUNTIME_PACKAGES（{len(listed)} 条）里：",
            file=sys.stderr,
        )
        for module, where in sorted(gaps.items()):
            print(f"   - {module}  ← 那一句在 {where}", file=sys.stderr)
        print(
            "   懒加载那一行在跑到之前不出现在调用图里，子模块与数据文件全靠这份清单：\n"
            "     ① 该整族收 ⇒ 补进 spec 的 RUNTIME_PACKAGES（补之前先确认构建机装过它，\n"
            "        `collect_submodules()` 对没安装的包返回空列表且不报错）；\n"
            "     ② 刻意不整族收 ⇒ 加进 NOT_BUNDLED_BY_DESIGN 并写理由（空理由不算理由）。\n",
            file=sys.stderr,
        )
        return 1
    print(f"✓ spec 收包清单覆盖 src 的 {len(lazy)} 族懒加载（清单 {len(listed)} 条）")

    if not (bundle / "rolecard-backend.exe").exists():
        print(
            f"⏭️  没有随包后端（{bundle} 不存在或还没打出 exe）—— 没打过包不是负面，\n"
            "    bundle 那一半跳过；上面那半（spec 清单 ↔ src 懒加载）是不需要产物就能查的。\n"
            "    要量 bundle 那一半先跑：.venv\\Scripts\\python.exe scripts\\build_sidecar.py"
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

    schemas = declared_schema_files(ROOT / "src")
    have = bundled_schema_files(bundle)
    missing_sql = [rel for rel in schemas if rel not in have]
    if missing_sql:
        print(
            f"❌ 源码声明的 {len(schemas)} 份建表脚本里，{len(missing_sql)} 份不在包里：",
            file=sys.stderr,
        )
        for rel in missing_sql:
            print(f"   - {rel}", file=sys.stderr)
        print(
            "   症状是**打包态建表直接失败而源码态一切正常**（R28-33）：运行时按\n"
            "   `domains/<id>/schema.sql` 现数，spec 得跟着数同一件事 —— 现在它是 glob，\n"
            "   所以这条红通常意味着**包是旧的**：重跑 `scripts/build_sidecar.py` 即可。\n"
            "   真的想让某个域不进包，那是另一件产品事，得写在这里而不是留给红字。",
            file=sys.stderr,
        )
        return 1

    print(
        f"✅ 随包后端 import parity：src 的 {len(imports)} 个第三方顶层模块全在包里"
        f"（bundle 顶层名 {len(bundled)} 个）"
    )
    print(f"   建表脚本 parity：{len(schemas)} 份声明全在包里（core / roles / 各域）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
