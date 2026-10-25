"""自动生成 API 响应模型（从运行时真实返回反推）—— P1 的"能自动就都自动"那一半。

为什么需要它：本仓 104 个端点此前**零处**声明 `response_model` ⇒ OpenAPI 里
193 个响应只有 82 个带 `$ref`，而那 82 个全是 `HTTPValidationError`（**错误**响应）
⇒ 成功响应没有任何契约可引用 ⇒ 前端 62 个手写类型无法被机器校对。

为什么是"从运行时反推"而不是"人手写模型"：
  * 人手写 = 104 次人肉，且每个模型都要人工核对字段集（漏一个字段 = 响应被静默过滤掉，
    那是比"没有模型"更坏的一种错）；
  * 反推 = 机器打一遍端点、拿真实 JSON、推字段与类型 —— 字段集**就是**运行时事实。

**三道安全闸**（自动生成最怕的就是"自动地生成一个错的"）：
  ① **只覆盖只读端点**（白名单）：写端点的返回随请求体变化，反推出来的形状只是
     "某一次调用的形状"，把它固化成模型就是在骗后来人；
  ② **嵌套对象各自成模型**（按路径命名，如 `ServicesViewServicesItem`）—— 拍平成
     `dict[str, Any]` 等于把契约丢回"任意 JSON"，前端照样校不了；
  ③ **生成物必须过门禁**：本脚本产出 `src/rolecard_agent/api/response_models.py`，
     随仓库入库；`--check` 每次 CI 比对"现运行时的形状 == 入库模型"，端点返回变了
     而模型没跟上 → 红（与 dist 同步 / openapi 契约同步同族判据）。

**字段可空性**：所有字段可选（`total=False` 语义）—— 响应形状会随状态变（空列表、
某次没带某字段），模型宁可宽松也不许把真实响应判成非法。`extra="allow"`：
多出来的字段照收（后端加字段时旧模型不会把响应判非法）。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
from typing import Any

# 中文与「❌」在管道输出上会撞 GBK（`console encoding` 尺子盯这个）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

OUT_PATH = ROOT / "src" / "rolecard_agent" / "api" / "response_models.py"

#: 只读端点白名单（路径 → 模型名）。写端点不自动建模（见模块 docstring 闸 ①）。
READONLY_ENDPOINTS: dict[str, str] = {
    "/api/roles": "RoleList",
    "/api/services": "ServicesView",
    "/api/pets": "PetsView",
    "/api/tools/catalog": "ToolsCatalog",
}

_MAX_DEPTH = 4


#: 生成期间的占位模块：任意模型名都给一个宽松模型（`extra="allow"` 不丢字段）。
_PLACEHOLDER_MODULE = '''"""生成期间的占位（真身由本脚本写回）—— 任意名字都拿得到宽松模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _Placeholder(BaseModel):
    model_config = ConfigDict(extra="allow")


def __getattr__(name: str) -> Any:
    return _Placeholder
'''


def _python_type(value: Any) -> str:
    """JSON 值 → Python 类型注解（宽一点：字符串可能是数字，数字可能是字符串）。"""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "Any"


def _class_name(base: str, suffix: str) -> str:
    """路径片段 → 类名片段（首字母大写，非字母数字变下划线）。"""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in suffix)
    return f"{base}{cleaned[:1].upper()}{cleaned[1:]}"


def _build(value: Any, base_name: str, path: str, depth: int, out: dict[str, str]) -> str:
    """从 JSON 值构建类型注解，并把嵌套模型写进 `out`（类名 → 源码）。

    返回值是**当前值**的类型注解文本；嵌套对象会递归生成各自的模型。
    """
    if depth >= _MAX_DEPTH:
        return "Any"
    if isinstance(value, dict):
        model_name = _class_name(base_name, path or "Root")
        # 先占位，避免自引用时无限递归（递归数据结构在响应里少见，但别崩）
        if model_name in out:
            return model_name
        out[model_name] = ""
        lines = [
            f"class {model_name}(BaseModel):",
            f'    """自动生成：{model_name}（{path} 的对象形状）。"""',
            "",
            '    model_config = ConfigDict(extra="allow")',
        ]
        for key, sub in value.items():
            annotation = _build(sub, base_name, f"{path}_{key}" if path else key, depth + 1, out)
            lines.append(f"    {key}: {annotation} | None = None")
        if not value:
            lines.append("    pass")
        out[model_name] = "\n".join(lines)
        return model_name
    if isinstance(value, list):
        if not value:
            # 空列表推不出元素类型 —— 用 Any，不猜。
            return "list[Any]"
        item_annotation = _build(value[0], base_name, f"{path}Item", depth + 1, out)
        return f"list[{item_annotation}]"
    if value is None:
        return "Any"
    return _python_type(value)


def _sample(path: str) -> Any:
    """打一次端点，拿真实 JSON。失败就抛（不静默跳过 —— 静默跳过 = 生成物悄悄少一类）。

    **为什么先移走旧生成物**：`_sample` 要 import `rolecard_agent.api.main` → 它 import
    routers → routers import 本模块的生成物（旧的）。旧生成物若因为上一次生成器版本
    不同而**语法/配置不合法**（比如 RootModel 带 extra），import 直接炸 —— 生成器于是
    永远写不出新版本（自己把自己锁死）。移走 = 生成器不依赖旧产物的合法性。
    """
    from fastapi.testclient import TestClient

    from rolecard_agent.api.main import create_app

    app = create_app(sqlite_path=pathlib.Path(tempfile.mkdtemp()) / "gen-models.db")
    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.get(path)
    if resp.status_code != 200:
        raise RuntimeError(f"{path} 返回 {resp.status_code}：{resp.text[:200]}")
    return resp.json()


def generate() -> tuple[str, str | None]:
    """跑一遍白名单端点，渲染出整个生成文件的内容。

    **第一步把旧的生成物移走**：`_sample` 要 import 应用 → import routers → import 旧生成物。
    旧产物若不合法（上一版生成器的产物带着 RootModel+extra 这类已废配置），import 会炸，
    生成器就永远写不出新版本 —— 自己把自己锁死。移走之后 import 链上没有它。
    """
    backup: str | None = None
    if OUT_PATH.exists():
        backup = OUT_PATH.read_text(encoding="utf-8")
        OUT_PATH.unlink()

    # **占位模块**：生成期间 routers 还要 import 本模块（`from ... import ServicesView`），
    # 而真身此刻不在盘上 ⇒ import 直接炸 ⇒ 生成器永远写不出新版本（自己锁死自己）。
    # 解法：写入一个"任意名字都拿得到宽松模型"的替身（`extra="allow"` 的 BaseModel：
    # 不丢任何字段 —— 实测响应体原样通过），生成结束后再被真身替换。
    OUT_PATH.write_text(_PLACEHOLDER_MODULE, encoding="utf-8", newline=chr(10))

    header = [
        '"""API 响应模型 —— **自动生成，勿手改**。',
        "",
        "生成器：`scripts/tools/gen_response_models.py`（从运行时真实返回反推字段集）。",
        "漂移判据：同一个脚本的 `--check`（CI 每次 push 比对现形状 vs 入库模型）。",
        "手改这份文件 = 下一次生成被覆盖；要改字段集，改端点再重跑生成器。",
        "",
        "配套：引用这些模型的端点必须带 `response_model_exclude_unset=True` ——",
        "否则模型会把端点**没给**的缺省键补成 null（实测 pets 的 rows 多出 'listening': None，",
        "响应与端点真返回不再逐字一致）。",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        "from pydantic import BaseModel, ConfigDict, RootModel",
        "",
    ]
    try:
        return _generate_inner(header), backup
    except BaseException:
        # 失败也要把旧生成物放回去 —— "生成失败"不该顺手把仓库里的文件删了
        if backup is not None:
            OUT_PATH.write_text(backup, encoding="utf-8", newline=chr(10))
        raise


def _generate_inner(header: list[str]) -> str:
    models: dict[str, str] = {}
    roots: list[tuple[str, str, str]] = []  # (模型名, 端点路径, 顶层注解)
    for path, model_name in READONLY_ENDPOINTS.items():
        sample = _sample(path)
        if isinstance(sample, list):
            # 顶层是数组：用 RootModel 包（前端拿到的仍是数组，OpenAPI 里有个名字）
            item_annotation = _build(sample[0] if sample else None, model_name, "Item", 1, models)
            annotation = f"list[{item_annotation}]"
            roots.append((model_name, path, annotation))
        else:
            annotation = _build(sample, model_name, "", 0, models)
            roots.append((model_name, path, annotation))

    blocks: list[str] = list(header)
    for source in models.values():
        if source:
            blocks.extend(["", "", source])
    for model_name, path, annotation in roots:
        blocks.extend(
            [
                "",
                "",
                f"class {model_name}(RootModel[{annotation}]):",
                f'    """{path} 的顶层形状（数组包一层只为给它一个名字）。"""',
                "",
            ]
        )
    return "\n".join(blocks) + "\n"


def _format_text(text: str) -> str:
    """把生成文本过一遍 ruff format（生成物必须符合 ruff，否则门禁的 ruff 步会红）。"""
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "-"],
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        # ruff 不可用/失败：原样返回（不静默改内容）—— 但要出声，否则"生成物不合规"
        # 只会在门禁的 ruff 步才暴露，而那时没人知道是这个生成器的责任。
        print(f"⚠️ ruff format 失败（exit={proc.returncode}）：{proc.stderr[:200]}")
        return text
    return proc.stdout


def _format_generated() -> None:
    """把刚落盘的生成物格式化（幂等：已经是合规格式则不变）。"""
    OUT_PATH.write_text(
        _format_text(OUT_PATH.read_text(encoding="utf-8")),
        encoding="utf-8",
        newline=chr(10),
    )


def main() -> int:
    # generate() 会把旧生成物移走（生成期间用占位模块顶上），并把旧内容交回来 ——
    # 检查模式要比对的正是**移走前**那份，不是盘上此刻那份（此刻是占位）。
    content, previous = generate()
    content = _format_text(content)
    if "--check" in sys.argv:
        if previous is None:
            print(f"❌ {OUT_PATH} 不存在 —— 跑一次生成器并提交")
            return 1
        # 检查模式也必须把旧内容放回去 —— 生成期间盘上是**占位模块**，
        # 检查完不还原 = 仓库里留下一个占位文件（实测踩过：下一次生成读到的是占位）。
        if previous is not None:
            OUT_PATH.write_text(previous, encoding="utf-8", newline=chr(10))
            previous = _format_text(previous)
        if previous != content:
            print("❌ 响应模型与运行时形状漂移。")
            print("   修法：python scripts/tools/gen_response_models.py 然后提交")
            return 1
        print("OK 响应模型与运行时形状一致")
        return 0
    OUT_PATH.write_text(content, encoding="utf-8", newline=chr(10))
    _format_generated()
    print(f"written {OUT_PATH}（{len(READONLY_ENDPOINTS)} 个端点）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
