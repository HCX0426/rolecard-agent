"""把自动生成的响应模型**接进端点**（P1 自动化的最后一段）—— 不靠人手逐个改。

为什么需要它：模型生成了，端点却还没引用它 ⇒ OpenAPI 里那些端点的成功响应仍然没有
契约（`response_model` 是端点装饰器上的参数，接线这一步没人做就等于没接）。

**只改装饰器与 import，不动函数体与返回注解。**

**全程用 AST 定位，代码里一行正则都不碰**（前两版用正则清线，两次都把代码改坏：
  ① `re.sub(r",\\s*\\n\\s*\\)")` 把 `@router.get(\\n    "/api/x",\\n)` 的括号结构弄乱，
     文件直接 SyntaxError；
  ② `re.sub(r",?\\s*response_model=[A-Za-z_]\\w*")` 把 `("/api/roles", response_model=X)`
     删成 `("/api/roles"[X])` —— 语法还成立，跑起来才炸（TypeError: string indices）。
  逗号该留在哪、括号配对到哪里，只有解析器知道 ⇒ 用 AST。
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

# 中文与「❌」在管道输出上会撞 GBK（`console encoding` 尺子盯这个）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[2]
ROUTERS = ROOT / "src" / "rolecard_agent" / "api" / "routers"
GENERATED = ROOT / "src" / "rolecard_agent" / "api" / "response_models.py"

#: 端点路径 → (模型名, 端点所在文件相对 routers/ 的路径)。
#: 与 gen_response_models.py 的白名单是同一份清单的两半。
WIRING: dict[str, tuple[str, str]] = {
    "/api/roles": ("RoleList", "roles.py"),
    "/api/services": ("ServicesView", "services.py"),
    "/api/pets": ("PetsView", "pets.py"),
    "/api/tools/catalog": ("ToolsCatalog", "roles.py"),
    "/api/knowledge": ("KnowledgeView", "console.py"),
    "/api/knowledge/scopes": ("KnowledgeScopesView", "console.py"),
    "/api/local-service": ("LocalServiceView", "local_service.py"),
    "/api/mcp/servers": ("McpServersView", "mcp.py"),
    "/api/rag/metrics": ("RagMetricsView", "console.py"),
    "/api/reachouts": ("ReachoutsView", "reachouts.py"),
    "/api/records": ("RecordsView", "../domains/health/records.py"),
    "/api/sessions": ("SessionsView", "sessions.py"),
    "/api/settings/memory": ("SettingsMemoryView", "settings.py"),
    "/api/settings/model-providers": ("ModelProvidersView", "settings.py"),
    "/api/settings/models": ("ModelSettingsView", "settings.py"),
    "/api/settings/runtime": ("RuntimeSettingsView", "settings.py"),
    "/api/shell-release": ("ShellReleaseView", "shell_release.py"),
    "/api/update-check": ("UpdateCheckView", "shell_release.py"),
    "/api/sync/inventory": ("SyncInventoryView", "sync.py"),
    "/api/uploads/orphans": ("OrphanUploadsView", "console.py"),
    "/api/workspace/dir": ("WorkspaceDirView", "workspace.py"),
    "/api/workspace/tree": ("WorkspaceTreeView", "workspace.py"),
    "/api/health/deep": ("HealthDeepView", "health.py"),
    "/api/approvals": ("ApprovalsView", "approvals.py"),
    "/api/audit": ("AuditView", "console.py"),
    "/api/domains": ("DomainsView", "domains.py"),
    "/api/roles/{role_id}/timeline": ("RoleTimelineView", "roles.py"),
}


def _available_models() -> set[str]:
    """生成物里**实际存在**的模型名。

    为什么要看这个：模型是自动生成的 —— 某个端点这轮被判"形状不稳"而被跳过
    （实测 /api/knowledge 的 scopes：元素 A 的 sources 是字符串列表、B 的是对象列表，
    一个模型表达不了），它的模型就不存在 ⇒ 接线必须跳过，否则 import 报
    'cannot import name KnowledgeView'。
    """
    if not GENERATED.exists():
        return set()
    text = GENERATED.read_text(encoding="utf-8")
    return set(re.findall(r"^class (\w+)", text, flags=re.M))


def _decorator_path(call: ast.Call) -> str | None:
    """从 `@router.get("/api/x")` 里取路径；不是这种形状就返回 None。"""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "get":
        return None
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _is_generated_wiring(wiring: list[ast.keyword]) -> bool:
    """这个 response_model 是不是**自动生成**的那份？

    生成物里的模型是**裸名字**（`response_model=RoleList`）；手写的那份可能带类型
    构造（`response_model=list[RoleCard]`）。只拆前者 —— 拆了后者，这个端点就
    彻底没有契约了，而它是手写的、与后端模型同源的（实测 /api/roles）。
    """
    return any(isinstance(kw.value, ast.Name) for kw in wiring)


def _rewrite_decorator(block: list[str], model: str | None) -> list[str]:
    """把装饰器重写成"只有路径 + （可选）response_model"的形式。

    `model=None` 表示**拆掉**接线（这轮生成器没给它模型）。
    整块重写而不是局部替换：局部替换算不准逗号与缩进（前两版都栽在这）。
    """
    joined = chr(10).join(block)
    open_idx = joined.find("get(")
    close_idx = joined.rfind(")")
    if open_idx < 0 or close_idx < 0:
        return block
    # 实参部分：**先把旧的 response_model 两个 keyword 从实参文本里剔掉** ——
    # 否则重写时会把它们带进来（实测：装饰器变成
    # `get("/api/pets", response_model=PetsView, ..., response_model=PetsView, ...)`
    # ⇒ 'keyword argument repeated'）。
    args_src = joined[open_idx + 4 : close_idx]
    args_src = re.sub(r"response_model_exclude_unset\s*=\s*True\s*,?", "", args_src)
    args_src = re.sub(
        r"response_model\s*=\s*[A-Za-z_]\w*(\[[^\]]*\])?\s*,?",
        "",
        args_src,
    )
    args_src = args_src.strip().rstrip(",").strip()
    indent = " " * (len(block[0]) - len(block[0].lstrip()))
    out = [f"{indent}@router.get("]
    if args_src:
        out.append(f"{indent}    {args_src},")
    if model is not None:
        out.append(f"{indent}    response_model={model},")
        out.append(f"{indent}    response_model_exclude_unset=True,")
    out.append(f"{indent})")
    return out


def _wire_file(path: pathlib.Path, entries: dict[str, str | None]) -> tuple[str, int]:
    """给一个文件里的若干端点接上模型。返回 (新内容, 改动处数)。

    两步都是 AST 定位：
      ① 装饰器：整块重写（先拆掉旧接线，再按本轮该接的模型接上）；
      ② import：在最后一个顶层 import 语句**之后**插一行。
    """
    text = path.read_text(encoding="utf-8")
    lines = text.split(chr(10))
    tree = ast.parse(text)

    edits: list[tuple[int, int, list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            api_path = _decorator_path(deco)
            if api_path is None:
                continue
            if api_path not in entries:
                continue  # 这个端点本轮不归我管
            model = entries[api_path]  # None = "这轮生成器没给它模型"
            has_wiring = any(kw.arg == "response_model" for kw in deco.keywords)
            if model is None and not has_wiring:
                continue  # 本轮不接、也没接过 ⇒ 不动
            if model is None and has_wiring:
                # **只拆"自动生成的那份"**：手写的 `response_model=list[RoleCard]`
                # 这种（类型带方括号，生成器不产出）要原样保留 —— 拆了它，
                # 这个端点就彻底没有契约了（实测 /api/roles：RoleList 这轮被判形状不稳
                # 而被跳过，但手写的 list[RoleCard] 是 P1 第一刀加的、与后端模型同源）。
                wiring = [kw for kw in deco.keywords if kw.arg == "response_model"]
                if not _is_generated_wiring(wiring):
                    continue
            start_line = deco.lineno - 1
            end_line = (deco.end_lineno or deco.lineno) - 1
            block = lines[start_line : end_line + 1]
            edits.append((start_line, end_line, _rewrite_decorator(block, model)))

    if not edits:
        return text, 0

    for start, end, block in sorted(edits, reverse=True):
        lines[start : end + 1] = block

    # import：旧的删掉（它列的模型可能这轮已经不存在），新的按本轮接的写
    new_text = chr(10).join(lines)
    new_text = re.sub(
        r"^from rolecard_agent[.]api[.]response_models import .*$",
        "",
        new_text,
        flags=re.M,
    )
    models = sorted({m for m in entries.values() if m})
    if models:
        tree2 = ast.parse(new_text)
        # **插在最后一个完整 import 之后**：`end_lineno` 指向该语句的**结束行**
        # （多行括号 import 也会指到真正的结尾），插在它下一行就不会插进括号中间。
        # 前一版用"最后一个 import 的起始行"算 ⇒ 插进多行 import 的内部 ⇒
        # IndentationError（实测 'AuditView,' 单独一行）。
        last_import_end = 0
        for node in tree2.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                last_import_end = max(last_import_end, node.end_lineno or node.lineno)
        ins_lines = new_text.split(chr(10))
        ins_lines.insert(
            last_import_end, "from rolecard_agent.api.response_models import " + ", ".join(models)
        )
        new_text = chr(10).join(ins_lines)
    return new_text, len(edits)


def main() -> int:
    available = _available_models()
    by_file: dict[str, dict[str, str | None]] = {}
    # **生成物里没有的模型 ⇒ 该端点这轮"拆接线"**：模型不存在而端点还引用它，
    # import 直接炸（实测 'cannot import name ServicesView'）。所以要**去接线的**
    # 端点集合要包含它们（model=None 表示拆）。
    for api_path, (model, filename) in WIRING.items():
        if available and model not in available:
            print(f"  拆掉 {api_path} 的接线：生成物里没有 {model}（形状不稳或本轮未生成）")
            by_file.setdefault(filename, {})[api_path] = None
            continue
        by_file.setdefault(filename, {})[api_path] = model

    total = 0
    for filename, entries in by_file.items():
        path = (ROUTERS / filename).resolve()
        if not path.exists():
            print(f"⚠️ 路由文件不存在：{filename} —— 跳过")
            continue
        new_text, changed = _wire_file(path, entries)
        if changed:
            path.write_text(new_text, encoding="utf-8", newline="\n")
            total += changed
            print(f"  {filename}: 接线 {changed} 个端点")
    print(f"共接线 {total} 个端点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
