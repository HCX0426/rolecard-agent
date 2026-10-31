"""P6：httpx.AsyncClient 直打 app 的接入层集成面（不起真进程）。

为什么单独立这一支（未完善清单 P6）：单测管函数、镜像臂只走一条真链路，**中间接缝**
（认证中间件 → 错误出口 → 路由挂载 → 分级清单）此前靠覆盖率平均数兜。这一支把三样
东西钉成 HTTP 事实：

1. **鉴权豁免矩阵**：每个 GET 端点按 `api/access.py::classify` 的档位核对运行时状态码
   （public 免鉴权可达；user 匿名 401、带凭据放行；operator 使用者 403、操作员放行）。
   判据驱动自 classify 本身 —— 清单改了矩阵自动跟着改，测试不抄第二份名单
   （清单里写死一条就红的那条纪律，搬到运行时这一侧）。
2. **统一错误结构过 ASGI 接缝**：401/403 的正文是 `{"detail": 中文}` JSON ——
   `test_error_contract` 用 TestClient 钉过同样的形状，但 TestClient 走的是它的同步
   封装；这一支用 httpx.ASGITransport（生产 ASGI 路径的形状），两把钥匙开同一把锁。
3. **每个 router ≥1 条写侧接缝用例**：各 router 的第一个非 GET 端点，匿名必须 401
   （认证闸在业务之前 —— 未鉴权的 POST 不该触到任何 handler）。

路径清单读**入库契约副本** `frontend/openapi.json`（它与代码同步由门禁的
「openapi 契约同步」步看着）—— 契约先行这件事在测试面同样成立：矩阵的覆盖面
就是契约声明的端点面。
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from rolecard_agent.api.access import PUBLIC, USER, classify

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "frontend" / "openapi.json").read_text(encoding="utf-8"))

#: 模板路径的样本参数（与生成器 _resolve_path 同一形状：只为把路径打到运行时）。
SAMPLES = {
    "{role_id}": "sample",
    "{thread_id}": "t-none",
    "{domain}": "health",
    "{name}": "x.png",
    "{pack_id}": "p",
    "{relative}": "sprite.png",
    "{task_id}": "u-none",
    "{server_id}": "s-none",
    "{index_id}": "i-none",
    "{scope}": "sc-none",
    "{record_id}": "r-none",
    "{report_id}": "rp-none",
    "{reachout_id}": "msg-none",
    "{approval_id}": "1",
    "{item_id}": "1",
    "{key}": "chat",
    "{eid}": "e-none",
    "{keep_id}": "1",
    "{drop_id}": "2",
}

#: 操作员档之外还叠了回环护栏的端点（`require_loopback`，权限审查 R102-45 那族）：
#: 矩阵从非回环对端打，operator 也会 403 —— 这是**叠加闸**在工作，不是分级错了。
#: 豁免只免 operator 侧的期望状态码，user 侧 403 与匿名 401 照常钉。
LOOPBACK_OVERLAY = {"/api/local-service"}

#: "放行"的判定：过了认证/分级闸之后的业务结果都算放行（404=样本 id 不存在、
#: 422=缺查询参数、405=该路径没声明 GET、409=这台机器的形状不对 —— 都不是鉴权层的
#: 决定：认证层只产 401/403，409 只可能出自 handler —— 操作员既然拿到了 409，闸门就是过了；
#: 匿名 401 与使用者 403 的期望照常钉死，放宽拦不住任何鉴权 bug）。
#: 409 是 2026-10-31 CI 红出来的（run 41200198131，门禁臂+Windows 臂同一族）：
#: `/api/data/export` 的"主库不在数据根 ⇒ 拒绝备份空壳"形状闸在本模块装配下**必然**触发
#: （app.db 住本模块 tmp，DATA_ROOT 是会话共享根）——矩阵该问的是"闸门放行了没有"，
#: 不是"业务成不成"；200 那条真路径由 tests/unit/test_data_export_api.py 钉。
PASSED_THROUGH = {200, 204, 404, 405, 409, 422}

USER_CRED = ("alice", "pw")
OPERATOR_CRED = ("bob", "pw")  # AUTH_CREDENTIALS 里带 operator: 前缀


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _fill(template: str) -> str | None:
    path = template
    for token, sample in SAMPLES.items():
        path = path.replace(token, sample)
    return None if "{" in path else path


def _get_paths() -> list[str]:
    return sorted(p for p, ops in SPEC["paths"].items() if p.startswith("/api") and "get" in ops)


@pytest.fixture(scope="module")
def auth_on_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[httpx.AsyncClient]:
    """鉴权开启的 app + 非回环对端的 AsyncClient（整个模块共用一次装配）。

    teardown 显式走 `runtime.shutdown()` —— ASGITransport 不跑 lifespan，不调它的话
    模块级 app 的 chroma 客户端会漏进进程级注册表活过整个模块：那正是 R102-41 那发
    偶发（`Nothing found on disk`）的复现条件"同进程并存多格 system"（全量趟实测
    2026-10-26：本模块污染的注册表把下一支用例的取证判据打红了）。
    conftest 的逐用例清理罩不住 module 级 fixture 开的这一格 —— 谁开谁收。
    """
    keys = (
        "AUTH_MODE",
        "AUTH_CREDENTIALS",
        "UPDATE_CHECK_ENABLED",
        "ROLECARD_ERROR_LOG",
        "CHROMA_PATH",
        "SQLITE_PATH",
    )
    old = {k: os.environ.get(k) for k in keys}
    tmp = tmp_path_factory.mktemp("api-surface")
    os.environ["AUTH_MODE"] = "on"
    os.environ["AUTH_CREDENTIALS"] = "alice:pw,operator:bob:pw"
    os.environ["UPDATE_CHECK_ENABLED"] = "0"
    os.environ["ROLECARD_ERROR_LOG"] = str(tmp / "error.jsonl")
    os.environ["CHROMA_PATH"] = str(tmp / "chroma")  # 别污染演示库（test_sessions_api 同例）
    # SQLITE_PATH 也必须进环境：deep 探针（/api/health/deep）读的是 **settings** 的
    # sqlite_path，不是 create_app 的参数 —— 只传参数不设 env，探针就去看默认相对路径
    # `data/sqlite/app.db`：开发机上那文件恰好存在 ⇒ 本机绿；CI 的干净 checkout 没有
    # ⇒ 红（2026-10-26 实测，判红带响应体的那一趟说的真相）。
    os.environ["SQLITE_PATH"] = str(tmp / "app.db")
    app = None
    try:
        from rolecard_agent.api.main import create_app

        app = create_app(sqlite_path=tmp / "app.db")
        # client=(203.0.113.9, …)：TEST-NET 对端 = 非回环 —— 本地护栏与 auto 档
        # 的特殊路径都不掺进来，矩阵量的是纯分级行为。
        transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 50000))
        # 同步 fixture 不能 await：把 transport 挂在壳上，每个 async 测试用它开
        # 自己的 AsyncClient（transport 复用 = app 装配一次，整个模块共用）。
        yield _ClientCtx(transport)
    finally:
        if app is not None:
            app.state.ctx.runtime.shutdown()
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _ClientCtx:
    """跨"同步 fixture → 异步测试"的桥：每个 async 测试用同一个 transport 开 AsyncClient。"""

    def __init__(self, transport: httpx.ASGITransport) -> None:
        self.transport = transport

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, base_url="http://127.0.0.1", timeout=15)


def _headers(cred: tuple[str, str] | None) -> dict[str, str]:
    if cred is None:
        return {}
    return {"Authorization": _basic(*cred)}


@pytest.mark.parametrize("template", _get_paths())
async def test_authz_matrix(auth_on_env: _ClientCtx, template: str) -> None:
    """每个 GET 端点：档位来自 classify，运行时状态码必须与档位一致。"""
    path = _fill(template)
    if path is None:
        pytest.skip(f"样本表没配全：{template}")
    tier = classify(path, "GET")
    async with auth_on_env.client() as c:
        anon = await c.get(path)
        if tier == PUBLIC:
            assert anon.status_code == 200, f"{path}: public 档匿名必须 200（healthcheck 的命根子）"
            return
        assert anon.status_code == 401, f"{path}: 非 public 档匿名必须 401"
        user = await c.get(path, headers=_headers(USER_CRED))
        if tier == USER:
            assert user.status_code in PASSED_THROUGH, (
                f"{path}: user 档带使用者凭据必须过闸（实测 {user.status_code}）"
            )
        else:
            assert user.status_code == 403, f"{path}: operator 档使用者必须 403"
        op = await c.get(path, headers=_headers(OPERATOR_CRED))
        expected = PASSED_THROUGH | ({403} if path in LOOPBACK_OVERLAY else set())
        assert op.status_code in expected, (
            f"{path}: operator 档操作员必须过分级闸（403 仅当叠了回环护栏）"
            f" —— 实测 {op.status_code}，body={op.text[:300]}（红要带现场：503/5xx 是"
            "端点自报的退化原因，直接看 body 里的 checks，别让下一趟再猜）"
        )


async def test_denials_carry_json_detail_over_asgi(auth_on_env: _ClientCtx) -> None:
    """统一错误结构走真 ASGI 接缝：401 与 403 都是 `{"detail": 中文/字符串}` JSON。"""
    async with auth_on_env.client() as c:
        denied = await c.get("/api/roles")  # user 档：匿名必 401
        assert denied.headers["content-type"].startswith("application/json")
        assert isinstance(denied.json()["detail"], str)
        forbidden = await c.get("/api/settings/runtime", headers=_headers(USER_CRED))
        assert forbidden.status_code == 403
        body = forbidden.json()
        assert isinstance(body["detail"], str) and body["detail"], "403 必须带一句可行动的中文"


def _router_write_endpoints() -> dict[str, tuple[str, str]]:
    """每个 router 文件 → (方法, 第一个非 GET 端点)。读源码 AST 级正则即可（测试侧，
    与生成器"不碰正则"的纪律无关 —— 那里是改写代码，这里只是读数）。"""
    out: dict[str, tuple[str, str]] = {}
    routers_dir = ROOT / "src" / "rolecard_agent" / "api" / "routers"
    files = [p for p in routers_dir.glob("*.py") if p.name != "__init__.py"]
    files.append(ROOT / "src" / "rolecard_agent" / "api" / "diagnostics.py")
    for f in sorted(files):
        text = f.read_text(encoding="utf-8")
        for method, path in re.findall(r'@router\.(post|put|patch|delete)\(\s*"([^"]+)"', text):
            out[f.name] = (method.upper(), path)
            break
    return out


@pytest.mark.parametrize(("router_file", "endpoint"), sorted(_router_write_endpoints().items()))
async def test_every_router_blocks_unauthenticated_writes(
    auth_on_env: _ClientCtx, router_file: str, endpoint: tuple[str, str]
) -> None:
    """P6 验收的另一半：每个 router 至少一条 httpx 接缝用例 —— 钉"未鉴权的写碰不到 handler"。"""
    method, template = endpoint
    path = _fill(template)
    if path is None:
        pytest.skip(f"样本表没配全：{template}")
    async with auth_on_env.client() as c:
        res = await c.request(method, path)
    assert res.status_code == 401, (
        f"{router_file} 的 {method} {path}: 未鉴权的写必须在认证闸就停住（实测 {res.status_code}）"
    )
    assert res.headers["content-type"].startswith("application/json")
