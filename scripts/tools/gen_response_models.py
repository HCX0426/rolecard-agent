"""自动生成 API 响应模型（从运行时真实返回反推）—— P1 的"能自动就都自动"那一半。

为什么需要它：本仓 104 个端点此前**零处**声明 `response_model` ⇒ OpenAPI 里
193 个响应只有 82 个带 `$ref`，而那 82 个全是 `HTTPValidationError`（**错误**响应）
⇒ 成功响应没有任何契约可引用 ⇒ 前端几十个手写类型无法被机器校对。

为什么是"从运行时反推"而不是"人手写模型"：
  * 人手写 = 上百次人肉，且每个模型都要人工核对字段集（漏一个字段 = 响应被静默过滤掉，
    那是比"没有模型"更坏的一种错）；
  * 反推 = 机器打一遍端点、拿真实 JSON、推字段与类型 —— 字段集**就是**运行时事实。

**七道闸与已踩过的坑**（都写在代码注释里）：
  ① 只覆盖只读端点：写端点的返回随请求体变化，反推出来的形状只是"某一次调用的形状"；
  ② 嵌套对象各自成模型（按路径命名）—— 拍平成 `dict[str, Any]` 等于把契约丢回"任意 JSON"；
  ③ 端点必须带 `response_model_exclude_unset=True` —— 否则模型把端点没给的缺省键补成
     null（实测 pets 的 rows 多出 `'listening': None`）；
  ④ 生成期间用**占位模块**顶上（任意名字都给 `Any`）—— 真身被移走时 routers 还要
     import 它，不顶就自己锁死自己；占位必须是 `Any` 而不是宽松 BaseModel（后者只吃
     dict 型，顶层是数组的端点会 ResponseValidationError）；
  ⑤ 生成物过 `ruff format`（否则门禁的 ruff 步红），检查模式两边都格式化后再比对；
  ⑥ **形状随数据变化**的端点不建模（同一数组里元素类型不一致 ⇒ 一个模型表达不了）——
     实测 /api/knowledge 的 scopes：元素 A 的 sources 是字符串列表、B 的是对象列表；
     这类端点不生成模型、不接线，并记进"不可建模"清单让"没建模"看得见；
  ⑦ **响应必须与端点真返回逐字一致**：用 `_GIVEN_KEYS` 记下端点真给了哪些键，序列化时
     按它裁剪（exclude_unset 挡不住 before-validator 补的键 —— 实测补的键会进
     `__fields_set__`，于是 pets 的 rows 多出 `'listening': None`）。

**字段语义**：键**必填**（总在），值可空（可能为 null）。样本只能证明"这一次不是 null"，
证明不了"永远不是" —— 实测 /api/update-check 的 `error`：样本里是字符串（没出错），
出网失败时是 null。所以标量字段一律可空。

**数组元素形状取所有元素的并集**（字段并集）：列表第一条可能是空数组（实测
`/api/roles` 第一条是内置角色、exemplars 为空，而我们造的角色卡在后面），
只看第一条就把"空"当成了形状。样本里只要有一个非空元素，形状就定下来了。
"""

from __future__ import annotations

import os
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
    "/api/knowledge": "KnowledgeView",
    "/api/knowledge/scopes": "KnowledgeScopesView",
    "/api/local-service": "LocalServiceView",
    "/api/mcp/servers": "McpServersView",
    "/api/rag/metrics": "RagMetricsView",
    "/api/reachouts": "ReachoutsView",
    "/api/records": "RecordsView",
    "/api/sessions": "SessionsView",
    "/api/settings/memory": "SettingsMemoryView",
    "/api/settings/model-providers": "ModelProvidersView",
    "/api/settings/models": "ModelSettingsView",
    "/api/settings/runtime": "RuntimeSettingsView",
    "/api/shell-release": "ShellReleaseView",
    "/api/sync/inventory": "SyncInventoryView",
    "/api/update-check": "UpdateCheckView",
    "/api/uploads/orphans": "OrphanUploadsView",
    "/api/workspace/dir": "WorkspaceDirView",
    "/api/workspace/tree": "WorkspaceTreeView",
    "/api/health": "HealthView",
    "/api/health/deep": "HealthDeepView",
    "/api/approvals": "ApprovalsView",
    "/api/audit": "AuditView",
    "/api/domains": "DomainsView",
    "/api/roles/{role_id}/timeline": "RoleTimelineView",
    # —— 第二批（P1b ②，2026-10-26）：种子扩面后这些端点采得到**带字段的样本**。
    # /api/channels 的清单恒非空（宿主没注册也如实回一行形状）；那族 {thread_id} 端点
    # 吃的是种子里建好的**主动会话线程**（确定性 id，见 _resolve_path）。
    "/api/channels": "ChannelsView",
    "/api/session/{thread_id}": "SessionThreadView",
    "/api/session/{thread_id}/turn": "SessionTurnView",
    "/api/session/{thread_id}/messages": "SessionMessagesView",
    "/api/session/{thread_id}/context": "SessionContextView",
}

#: 路径参数 → 样本数据里的真实值。`{thread_id}` 用**主动会话的线程 id**
#: （s_proactive_<user>_<role>）：种子里 `ensure_proactive_thread` 建的那条，恒存在。
#: 这个串是 `core/proactive/proactive_thread.py::proactive_thread_id` 公式的展开值，
#: 漂了 ⇒ 采样 404 ⇒ 生成器当场红（有锚点用例钉着，见
#: tests/unit/test_response_models_seed_surface.py）。
_SAMPLE_PARAMS: dict[str, str] = {
    "role_id": "sample_role",
    "thread_id": "s_proactive_local-user_sample_role",
}

_MAX_DEPTH = 4

#: 形状随数据变化、无法用单一模型表达的端点（生成时判定，见 _shape_is_stable）。
UNMODELABLE: list[str] = []

#: 生成期间的占位模块：任意模型名都给 `Any`（FastAPI 拿到 `response_model=Any`
#: 就不做响应校验 —— 于是顶层是数组的端点也吃得下）。
_PLACEHOLDER_MODULE = '''"""生成期间的占位（真身由本脚本写回）—— 任意名字都给 Any。"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    return Any
'''


def _merge_samples(a: Any, b: Any) -> Any:
    """把两次采样的结果合并（结构相同 ⇒ 字段类型取并集）。

    只在**结构相同**（同 key 集、同数组长度级）时合并；结构不同就返回第一个 ——
    结构不同意味着端点返回的形状真的变了（那属于"形状不稳"，另一条路处理）。
    """
    if isinstance(a, dict) and isinstance(b, dict):
        out: dict[str, Any] = {}
        for key in set(a) | set(b):
            if key in a and key in b:
                out[key] = _merge_samples(a[key], b[key])
            else:
                out[key] = a.get(key, b.get(key))
        return out
    if isinstance(a, list) and isinstance(b, list):
        merged: list[Any] = []
        for x, y in zip(a, b, strict=False):
            merged.append(_merge_samples(x, y))
        return merged
    if a is None:
        return b
    if b is None:
        return a
    if type(a) is type(b):
        return a
    # 类型不同 ⇒ 形状不稳定 ⇒ Any（诚实）
    return None


def _shapes_compatible(a: Any, b: Any) -> bool:
    """两次采样的结构兼容吗（key 集一致）？"""
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b)
    return True


def _python_type(value: Any) -> str:
    """JSON 值 → Python 类型注解（null 不参与类型判断 —— 它只影响可空性）。"""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "Any"


def _merged_type(values: list[Any]) -> str:
    """一组样本值的**合并类型**（同字段在多次生成间不该抖动）。

    为什么需要：字段的值随状态变（实测 /api/update-check 的 latest：有网络是字符串、
    无网络是 null）。只看一个样本 ⇒ 两次生成分别给出 `str` 与 `Any` ⇒ 漂移判据永远红。
    合并规则：**null 不参与**（null 只说明可空），其余取类型并集；并集多于一种 ⇒ Any
    （形状真的不稳定，如实承认）。
    """
    kinds = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            kinds.add("bool")
        elif isinstance(value, int):
            kinds.add("int")
        elif isinstance(value, float):
            kinds.add("float")
        elif isinstance(value, str):
            kinds.add("str")
        else:
            kinds.add("object")
    if not kinds:
        return "Any"
    if len(kinds) == 1:
        return kinds.pop()
    return "Any"


def _class_name(base: str, suffix: str) -> str:
    """路径片段 → 类名片段。"""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in suffix)
    return f"{base}{cleaned[:1].upper()}{cleaned[1:]}"


def _merge_objects(items: list[Any]) -> tuple[dict[str, Any], dict[str, int]] | None:
    """把列表里的所有对象元素**合并**成一个字典（字段取并集）。

    返回 (合并结果, 每个字段出现在多少个元素里)。第二个返回值决定字段是不是必填：
    服务视图那类端点返回**多个分类**（ocr / embedding / rerank / models…），每个分类
    的字段集不同（models 有 order_only、ocr 没有）。合并后若一律当必填，缺该字段的
    分类就会被 before-validator 补成 None ⇒ 响应校验失败（实测 'order_only: None'
    不是 bool）。所以：**出现在所有元素里的字段才必填**，其余可空。
    """
    merged: dict[str, Any] | None = None
    counts: dict[str, int] = {}
    total = 0
    kinds: set[str] = set()
    for item in items:
        if not isinstance(item, (dict, list)) or not item:
            continue
        kinds.add(type(item).__name__)
        if not isinstance(item, dict):
            continue
        total += 1
        if merged is None:
            merged = dict(item)
        else:
            for key, value in item.items():
                if key not in merged:
                    merged[key] = value
                elif merged[key] is not None and value is None:
                    # **任一元素为 null ⇒ 该字段可空**：合并时优先保留 null（服务视图里
                    # 某些分类的 effective 是 None、另一些是字符串；只取第一个会把可空
                    # 字段标成非空 ⇒ 响应校验失败 'Input should be a valid string'）。
                    merged[key] = None
                elif isinstance(merged[key], list) and isinstance(value, list):
                    # **嵌套数组也要合并**：不同元素的同一个数组字段，元素类型可能不同
                    # （实测 /api/knowledge 的 scopes：有的元素 sources 是字符串列表、
                    # 有的是对象列表）。只取第一个 ⇒ 另一类型的元素校验失败。
                    merged[key] = merged[key] + value
        for key in item:
            counts[key] = counts.get(key, 0) + 1
    if merged is None:
        return None
    if len(kinds) > 1:
        # 元素类型不统一（实测 /api/knowledge 的 sources）⇒ 一个模型概括不了。
        return None
    return merged, {**counts, "__total__": total}


def _build(
    value: Any,
    base_name: str,
    path: str,
    depth: int,
    out: dict[str, str],
    presence: dict[str, int] | None = None,
) -> str:
    """从 JSON 值构建类型注解，并把嵌套模型写进 `out`（类名 → 源码）。"""
    if depth >= _MAX_DEPTH:
        return "Any"
    if isinstance(value, dict):
        model_name = _class_name(base_name, path or "Root")
        if model_name in out:
            return model_name
        out[model_name] = ""
        lines = [
            f"class {model_name}(BaseModel):",
            f'    """自动生成：{model_name}（{path} 的对象形状）。"""',
            "",
            '    model_config = ConfigDict(extra="allow")',
            "",
            '    @model_validator(mode="before")',
            "    @classmethod",
            "    def _fill_missing(cls, data: Any) -> Any:",
            '        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。',
            "",
            "        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，",
            "        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。",
            "",
            "        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset",
            "        挡不住它（实测 pets 的 rows 多出 'listening': None）。",
            '        """',
            "        if isinstance(data, dict):",
            "            known = set(cls.model_fields)",
            "            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}",
            "            missing = {k: None for k in known if k not in given}",
            "            merged = {**given, **missing}",
            "            merged[_GIVEN_KEYS] = set(given)",
            "            return merged",
            "        return data",
            "",
            '    @model_serializer(mode="wrap")',
            # **返回注解必须留空**：标 `-> Any` 会让 Pydantic 把序列化 schema 整体
            # 算成 Any ⇒ openapi 里该模型变成 `{}`（实测全部生成模型在 app.openapi()
            # 中零字段，前端拿不到契约）。handler 的参数注解不受影响，保留。
            "    def _drop_filled(self, handler: Any):",
            '        """把"补出来的键"裁掉 —— 只留端点真给过的。"""',
            "        data = handler(self)",
            "        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)",
            "        if given is None:",
            "            return data",
            "        return {k: v for k, v in data.items() if k in given}",
        ]
        for key, sub in value.items():
            annotation = _build(
                sub, base_name, f"{path}_{key}" if path else key, depth + 1, out, presence
            )
            # **值一律可空**：样本只能证明"这一次不是 null"，证明不了"永远不是" ——
            # 实测 /api/update-check 的 error：样本里是字符串（没出错），出网失败时
            # 是 null。只按样本定 ⇒ 模型说它是非空字符串 ⇒ 真实响应被判非法
            # （'Input should be a valid string'）。键仍然**必填**（键总在，值可能为 null）。
            #
            # **类型随状态变的字段用 Any**：实测 /api/update-check 的 latest/url/error
            # —— 有网络时是字符串、无网络时是 None（⇒ Any）。两次生成的类型不同 ⇒
            # 漂移判据会永远红。这类字段一律 Any（诚实：形状不稳定就承认不稳定）。
            lines.append(f"    {key}: {annotation} | None")
            if annotation == "Any":
                lines[-1] = f"    {key}: Any"
        if not value:
            lines.append("    pass")
        out[model_name] = "\n".join(lines)
        return model_name
    if isinstance(value, list):
        # **全是标量的数组**（字符串/数字列表）⇒ 直接按标量类型建，不要当对象：
        # 实测 /api/sync/inventory 的 kinds 是 ["card","thread","memory","reachout"]
        # —— 按对象建模型 ⇒ 响应校验失败 'Input should be a valid dictionary'。
        scalars = [v for v in value if not isinstance(v, (dict, list))]
        if scalars and len(scalars) == len(value):
            return f"list[{_python_type(scalars[0])}]"
        merge_result = _merge_objects(value)
        if merge_result is None:
            # 元素形状推不出（空数组，或元素类型不统一）—— 写 `dict[str, Any]`：
            # 元素是对象这件事被保住，字段名未知。**不能写 Any**：Any 在前端变成
            # `unknown`，取数组元素再取它的字段整条链失去类型（实测 40+ 处 tsc 错误）。
            return "list[dict[str, Any]]"
        merged, presence = merge_result
        item_annotation = _build(merged, base_name, f"{path}Item", depth + 1, out, presence)
        return f"list[{item_annotation}]"
    if value is None:
        return "Any"
    return _python_type(value)


def _seed_want(client: Any, method: str, path: str, **kwargs: Any) -> None:
    """种子的**响响应答**：非 2xx 当场炸 —— 静默失败的种子 = 形状悄悄漂，
    `--check` 只会报"漂了"而不说为什么（2026-10-26 CI 实测那一课）。"""
    resp = getattr(client, method)(path, **kwargs)
    if resp.status_code >= 300:
        raise RuntimeError(
            f"样本数据创建失败 {method.upper()} {path} → {resp.status_code}：{resp.text[:200]}"
            "（采样面断了种子，形状必然漂 —— 先修种子再谈生成）"
        )


def _seed_minimal_data(app: Any) -> None:
    """给**空数组**塞最小数据，让生成器推得出元素类型。

    造的样本必须**满足后端自己的约束**（不然创建直接被拒）。数据全在临时库里。
    """
    from fastapi.testclient import TestClient

    from rolecard_agent.config import DEFAULT_SILICONFLOW_BASE_URL

    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    # 角色卡：exemplars / knowledge_scopes 这两个数组在样本里得非空
    _seed_want(
        client,
        "post",
        "/api/roles",
        json={
            "role_id": "sample_role",
            "role_name": "样本角色",
            "system_prompt": "样本",
            "temperature": 0.7,
            "model_name": None,
            "tool_whitelist": ["sample_tool"],
            "exemplars": [{"user": "样本问", "assistant": "样本答"}],
            "knowledge_scopes": ["sample_scope"],
            "description": "生成器样本",
        },
    )
    # 知识作用域：knowledge / scopes 端点的数组
    _seed_want(client, "post", "/api/knowledge/scopes", json={"name": "sample_scope"})
    # 模型后端（含 fallback + 一个带 key 的凭据组）：
    # - /api/settings/models 的 fallbacks 是**字符串数组**（后端名列表）。样本里它是空的 ⇒
    #   生成器推不出元素类型 ⇒ 建成 dict 型 ⇒ 真实响应（fallbacks 非空）校验失败
    #   'Input should be a valid dictionary'。
    # - `key_masked` 在样本组都 no-key 时采成 null ⇒ Any（ProviderGroup 那族迁不动的根因）。
    #   多给一个带 api_key 的供应商（硅基流动），这一格采成掩码字符串。
    #   ⚠️ 整表 PUT 是**替换语义**：想同时保 fallback 与非空 fallbacks，三个后端必须在
    #   同一次 PUT 里给全（分两次 PUT，后一次抹掉前一次的行）。
    _seed_want(
        client,
        "put",
        "/api/settings/models",
        json={
            "default": "sample_local",
            "backends": [
                {
                    "name": "sample_local",
                    "provider": "ollama",
                    "model": "sample-model",
                    "base_url": "http://127.0.0.1:1",
                    "usage": "chat",
                },
                {
                    "name": "sample_fallback",
                    "provider": "ollama",
                    "model": "sample-model-2",
                    "base_url": "http://127.0.0.1:1",
                    "usage": "chat",
                },
                {
                    "name": "sample_cloud",
                    "provider": "siliconflow",
                    "model": "sample-cloud-model",
                    # 抄 `config.DEFAULT_SILICONFLOW_BASE_URL`，不写字面量：
                    # "duplicated single-source literals" 尺子盯着呢（唯一归属在 config）。
                    "base_url": DEFAULT_SILICONFLOW_BASE_URL,
                    "api_key": "sk-sample-secret",
                    "usage": "chat",
                },
            ],
            "fallbacks": ["sample_fallback"],
        },
    )

    # ——— 第二批：把"从前是空数组"的那几族各喂一条最小真数据 ———
    # MCP server：/api/mcp/servers 的 servers 从前恒空 ⇒ list[dict[str, Any]]。
    # `enabled: False`：接线后的 rebuild 会真去连启用的 server —— 样本 URL 是死端口，
    # 别让它每次生成/校验都在日志里挣一条"加载失败"。行本身照进列表（读侧不过滤）。
    _seed_want(
        client,
        "post",
        "/api/mcp/servers",
        json={
            "id": "sample_mcp",
            "display_name": "样本 MCP",
            "url": "http://127.0.0.1:9/mcp",
            "headers": {"Authorization": "Bearer sample"},
            "enabled": False,
        },
    )
    # 记忆行：/api/settings/memory 的 items 从前恒空。
    _seed_want(client, "post", "/api/settings/memory/item", json={"text": "样本事实：生成器锚点"})
    # 一条会话（走主动会话那条确定性线程，_resolve_path 用它采 {thread_id} 那族端点）。
    _seed_want(client, "post", "/api/session/proactive", json={"role_id": "sample_role"})
    # 一条待批命令：/api/approvals 的 items 从前恒空。**直接走服务层** —— 路由的
    # submit 要从对话链路进来，这里只为让样本面非空。
    _seed_approval(app)
    # 一张"有资格主动"的角色卡：让 /api/reachouts 的 quiet 非空（ReachoutsView 最后一个松格）。
    _seed_reachable_role(app)
    # 一条主动开口（收件箱）：/api/reachouts 的 items 从前恒空。绑 sample_reachable
    # （见 _seed_reachout 的注释：不许搅浑 sample_role 的 timeline 形状）。
    _seed_reachout(app)
    # 一份体检报告：/api/records 的 items 从前恒空（health 域的读侧）。
    _seed_health_report(app)


def _seed_approval(app: Any) -> None:
    """种一条 pending 审批（不执行、不出网；只为让采样本时数组非空）。"""
    ctx = app.state.ctx
    ctx.approvals.submit("echo sample", cwd=None, role_id="sample_role", role_name="样本角色")


def _seed_reachout(app: Any) -> None:
    """种一条主动开口（收件箱）。用 features.reachout.inbox.record_reachout。

    绑在 **sample_reachable** 而不是 sample_role：`/api/roles/{role_id}/timeline` 采的是
    sample_role，往它的线上再掺一种事件（reachout 的 verb 是 null、thread 的是 "start"），
    那个端点就"一个模型表达不了"了 —— 种子之间不许互相搅浑对方的形状。
    """
    from rolecard_agent.features.reachout import inbox

    ctx = app.state.ctx
    role = ctx.role_cards.get("sample_reachable")
    inbox.record_reachout(
        ctx.conn, role, "样本：生成器锚点", user_id=ctx.current_user(), fired_by="tick"
    )


def _seed_health_report(app: Any) -> None:
    """种一份最小体检报告（health 域服务）。"""
    ctx = app.state.ctx
    ctx.health.create_report(
        user_id=ctx.current_user(),
        report_type="样本报告",
        check_time="2026-01-01",
        institution="生成器",
        note=None,
        indices=[{"index_name": "白细胞", "index_value": 6.5, "unit": "10^9/L"}],
    )


def _seed_reachable_role(app: Any) -> None:
    """建一张 reachout_enabled 的角色卡（quiet_status 只列"有资格主动"的角色）。

    为什么单独一张、不给 sample_role 直接开：`ensure_proactive_thread` 与采样线程
    都绑在 sample_role 上，让**它**再带 reachout_enabled 会让 quiet 那格与收件箱耦合
    —— 样本角色保持默认关，另建 `sample_reachable` 专供 quiet。
    """
    from rolecard_agent.roles.models import RoleCardCreate

    ctx = app.state.ctx
    ctx.role_cards.create(
        RoleCardCreate(
            role_id="sample_reachable",
            role_name="样本（可主动）",
            system_prompt="样本",
            reachout_enabled=True,
            description="生成器 quiet 样本",
        )
    )


def _type_tag(value: Any) -> str:
    """值的类型标签（用于跨元素比对）。"""
    if isinstance(value, list):
        inner = {_type_tag(v) for v in value}
        return "list[" + ",".join(sorted(inner)) + "]"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _stable_across(items: list[Any]) -> bool:
    """数组元素**之间**的形状一致性：同名字段在所有元素里类型必须一致。

    字段集可以不同（services 的五个分类字段集本就不同 ⇒ 用"必填但可空"处理）；
    但**同名同位置类型不同**就不稳（实测 /api/knowledge 的 scopes：元素 A 的 sources
    是字符串列表、元素 B 的是对象列表 —— 一个模型表达不了）。
    """
    objs = [item for item in items if isinstance(item, dict)]
    if len(objs) < 2:
        return True
    all_keys: set[str] = set()
    for obj in objs:
        all_keys |= set(obj.keys())
    for key in all_keys:
        kinds = set()
        for obj in objs:
            if key in obj:
                kinds.add(_type_tag(obj[key]))
        if len(kinds) > 1:
            return False
    return True


def _stable_at(value: Any, seen: set[int]) -> bool:
    """这个值的形状稳定吗（递归）。"""
    if isinstance(value, dict):
        if id(value) in seen:
            return True
        seen = seen | {id(value)}
        return all(_stable_at(v, seen) for v in value.values())
    if isinstance(value, list):
        if not value:
            return True
        kinds = {type(item).__name__ for item in value}
        if len(kinds) > 1:
            return False
        if not _stable_across(value):
            return False
        return all(_stable_at(item, seen) for item in value)
    return True


def _shape_is_stable(sample: Any) -> bool:
    """这个响应的形状**稳定**吗？"""
    return _stable_at(sample, set())


def _resolve_path(path: str) -> str:
    """把路径里的 `{param}` 换成样本数据里的真实值。

    带路径参数的端点（`/api/roles/{role_id}/timeline`）不替换就 404 —— 采样拿不到
    形状，模型也就推不出来。用的值来自**我们刚造的样本数据**（角色卡、主动会话线程），
    名单在 `_SAMPLE_PARAMS`。
    """
    for name, value in _SAMPLE_PARAMS.items():
        path = path.replace("{" + name + "}", value)
    return path


def _sample(path: str) -> Any:
    """打一次端点，拿真实 JSON。失败就抛（不静默跳过 —— 静默跳过 = 生成物悄悄少一类）。"""
    from fastapi.testclient import TestClient

    from rolecard_agent.api.main import create_app

    # SQLITE_PATH 必须与环境对齐（2026-10-26 CI 实测）：deep 探针（/api/health/deep）
    # 读的是 **settings** 的 sqlite_path，不是 create_app 的参数 —— 只传参数不设 env，
    # 干净的 CI checkout 上它看默认相对路径 `data/sqlite/app.db`，503 直接炸掉采样
    # （与 test_api_surface 夹具同一课：参数与 settings 必须同时给）。
    db_path = pathlib.Path(tempfile.mkdtemp()) / "gen-models.db"
    os.environ["SQLITE_PATH"] = str(db_path)
    app = create_app(sqlite_path=db_path)
    _seed_minimal_data(app)
    # `client=("127.0.0.1", 50000)`：**TCP 对端**设成回环 —— 本机来源护栏只认对端 IP。
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    resp = client.get(path)
    if resp.status_code != 200:
        raise RuntimeError(f"{path} 返回 {resp.status_code}：{resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"{path} 返回的不是 JSON（content-type={resp.headers.get('content-type')!r}）"
        ) from exc


def generate() -> tuple[str, str | None]:
    """跑一遍白名单端点，渲染出整个生成文件的内容。返回 (新内容, 旧内容)。

    **第一步把旧的生成物移走**并写占位模块：`_sample` 要 import 应用 → import routers →
    import 本模块的生成物。旧产物若不合法，import 会炸，生成器就永远写不出新版本 ——
    自己把自己锁死。

    **采样期钉环境**（闸 ⑧，2026-10-26）：两个端点的形状随本机环境变 ——
    `/api/update-check` 的值在有网/没网下类型不同（实测 latest/url：release 目录有产物
    时是字符串、没有时是 null），`/api/shell-release` 的字段集随本机有没有构建产物变。
    不钉 ⇒ 有网的机器与没网的机器各生成一份 ⇒ `--check` 在另一台机器上随机红
    （本机实测：CI 趟绿、本机趟红，同一提交）。钉法：
    `UPDATE_CHECK_ENABLED=0` 让 update-check 走 disabled 分支（8 键全在、不出网）；
    采样期摘掉 `SHELL_RELEASE_DIR`（shell-release 恒回 available/configured 两键）。
    代价如实记：这两个视图的**值类型**比真实响应窄（latest 在成功检查时确实是字符串），
    前端仍按 `| null` 用 —— 换到的是"判据跨机器恒绿"，这条交易值得。
    """
    import os

    os.environ["UPDATE_CHECK_ENABLED"] = "0"
    os.environ.pop("SHELL_RELEASE_DIR", None)
    # **采样期把用户数据根整体搬进临时目录**（闸 ⑧ 扩面，2026-10-26 CI 实测那一课）：
    # user_data_root 喂着 user_pets_dir / 孤儿上传扫描 / 知识演示种子这一整族 —— 本机
    # 真数据根里有什么（Cubism Core 在不在、data-root 攒了什么），采样就采到什么形状
    # （实测现场：随包 live2d 包 hiyori 依赖未入库的 Core 运行时，本机有 ⇒ skipped=[]，
    # CI 没有 ⇒ skipped=[{id, reason}]，PetsView 两机各一份）。钉到干净临时根后，这族
    # 形状由**入库的随包资产**唯一决定：live2d 包恒 skipped、孤儿恒空、演示种子恒同。
    os.environ["DATA_ROOT"] = tempfile.mkdtemp(prefix="gen-models-data-root-")
    backup: str | None = None
    if OUT_PATH.exists():
        backup = OUT_PATH.read_text(encoding="utf-8")
        OUT_PATH.unlink()
    OUT_PATH.write_text(_PLACEHOLDER_MODULE, encoding="utf-8", newline="\n")
    # 字节码缓存 + sys.modules 缓存都要清：前者按 mtime 判（同秒改写会命中旧 .pyc），
    # 后者让 import 拿到**上一次**的旧模块（占位机制对已缓存无效）。
    for pyc in OUT_PATH.parent.glob("__pycache__/response_models.*.pyc"):
        pyc.unlink()
    sys.modules.pop("rolecard_agent.api.response_models", None)

    models: dict[str, str] = {}
    roots: list[tuple[str, str, str]] = []

    try:
        for path, model_name in READONLY_ENDPOINTS.items():
            # **采样两次**：同端点两次的返回可能不同（实测 /api/update-check 的 latest：
            # 有网络是字符串、无网络是 null）。只看一次 ⇒ 两次生成的类型不同 ⇒
            # 漂移判据永远红。两次都采，把差异合并掉（null 只影响可空性，类型取并集）。
            sample = _sample(_resolve_path(path))
            sample2 = _sample(_resolve_path(path))
            if not _shapes_compatible(sample, sample2):
                sample = _merge_samples(sample, sample2)
            if not _shape_is_stable(sample):
                # **形状随数据变化的端点不该有单一模型**：/api/knowledge 的 scopes 里，
                # 有的作用域 sources 是字符串列表、有的是对象列表 —— 一个模型表达不了
                # （硬套 ⇒ 另一类元素校验失败）。这类端点**不生成模型、不接线**，
                # 并记进清单让"没建模"这件事看得见（不是静默漏掉）。
                UNMODELABLE.append(path)
                continue
            if isinstance(sample, list):
                merge_result = _merge_objects(sample)
                if merge_result is None:
                    item_annotation = "dict[str, Any]"
                else:
                    merged, presence = merge_result
                    item_annotation = _build(merged, model_name, "Item", 1, models, presence)
                roots.append((model_name, path, f"list[{item_annotation}]"))
            else:
                annotation = _build(sample, model_name, "", 0, models)
                roots.append((model_name, path, annotation))
    except BaseException:
        # 失败也要把旧生成物放回去 —— "生成失败"不该顺手把仓库里的文件删了
        if backup is not None:
            OUT_PATH.write_text(backup, encoding="utf-8", newline="\n")
        raise

    # header 里的「不可建模」清单必须在**采样循环之后**才算：UNMODELABLE 是循环里填的
    # —— 从前这段在循环前求值，那一行恒为「（无）」（清账时发现的静默撒谎：真被跳过的
    # 端点在生成物头注里从不出现）。被点名的端点**逐行**列（一行塞五个路径会撞 E501，
    # 而 ruff format 不折 docstring —— 生成物必须自带过 ruff check 的形状）。
    unmodelable_note = (
        ["不可建模（形状随数据变化）的端点：", *(f"  - {p}" for p in UNMODELABLE)]
        if UNMODELABLE
        else ["不可建模（形状随数据变化）的端点：（无）"]
    )
    header = [
        '"""API 响应模型 —— **自动生成，勿手改**。',
        "",
        "生成器：`scripts/tools/gen_response_models.py`（从运行时真实返回反推字段集）。",
        "漂移判据：同一个脚本的 `--check`（CI 每次 push 比对现形状 vs 入库模型）。",
        "手改这份文件 = 下一次生成被覆盖；要改字段集，改端点再重跑生成器。",
        "",
        *unmodelable_note,
        "",
        "配套：引用这些模型的端点必须带 `response_model_exclude_unset=True` ——",
        "否则模型会把端点**没给**的缺省键补成 null（实测 pets 的 rows 多出",
        "'listening': None，响应与端点真返回不再逐字一致）。",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        "from pydantic import BaseModel, ConfigDict, RootModel, model_serializer, model_validator",
        "",
        "#: 记录「端点真给了哪些键」的内部键名（序列化时按它裁剪）。",
        '_GIVEN_KEYS = "__given_keys__"',
        "",
    ]

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
    return "\n".join(blocks) + "\n", backup


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
        # ruff 不可用/失败：原样返回（不静默改内容）—— 但要出声。
        print(f"⚠️ ruff format 失败（exit={proc.returncode}）：{proc.stderr[:200]}")
        return text
    return proc.stdout


def _format_generated() -> None:
    """把刚落盘的生成物格式化（幂等）。"""
    OUT_PATH.write_text(
        _format_text(OUT_PATH.read_text(encoding="utf-8")),
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    content, previous = generate()
    content = _format_text(content)
    if "--check" in sys.argv:
        # 检查模式也必须把旧内容放回去 —— 生成期间盘上是**占位模块**。
        if previous is not None:
            OUT_PATH.write_text(previous, encoding="utf-8", newline="\n")
            previous = _format_text(previous)
        if previous is None:
            print(f"❌ {OUT_PATH} 不存在 —— 跑一次生成器并提交")
            return 1
        if previous != content:
            import difflib

            diff = [
                ln
                for ln in difflib.unified_diff(
                    previous.splitlines(),
                    content.splitlines(),
                    fromfile="committed",
                    tofile="fresh-sample",
                    lineterm="",
                )
            ]
            print("❌ 响应模型与运行时形状漂移。差异摘要（前 60 行）：")
            print("\n".join(diff[:60]) or "（无可打印 diff —— 内容级差异，跑一次生成器看全量）")
            print("   修法：python scripts/tools/gen_response_models.py 然后提交")
            return 1
        print("OK 响应模型与运行时形状一致")
        return 0
    OUT_PATH.write_text(content, encoding="utf-8", newline="\n")
    print(f"written {OUT_PATH}（{len(READONLY_ENDPOINTS)} 个端点）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
