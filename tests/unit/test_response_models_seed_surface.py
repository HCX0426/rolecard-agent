"""P1b 第二批：生成器**样本面**的锚点用例（未完善清单 P1b ② 的那条路）。

为什么需要：`gen_response_models.py` 的松格（`list[dict[str, Any]]` / `Any`）几乎全部
来自"**采样时数组是空的**"——空数组推不出元素形状，生成器只能写最宽的 dict 兜底
（闸 ⑤ 的诚实取舍）。清掉它们的办法不是前端硬接 unknown，而是把 `_seed_minimal_data`
扩到"每个在册端点采到的样本与真实用户状态同形"：有会话、有 MCP server、有记忆行、
有报告、有待批、有收件箱条目。**这个文件钉的就是"扩了不许缩"**：

① 白名单与接线表两份清单必须同集合（一份漂了，生成物有模型而端点没契约，
   OpenAPI 里就是空响应 —— 正是 P1 第一刀修掉的那族缺陷）；
② 种子之后，六个"曾经空数组"的端点原始 JSON 必须非空（判据打在**原始 dict** 上，
   不打在生成模型上 —— 模型是产物，产物会随模板改，样本面才是事实）；
③ `{thread_id}` 类端点的样本线程必须存在（`_resolve_path` 的替换串与
   `proactive_thread_id` 的公式漂一格，采样就 404、生成器整步红）。

跑一次 create_app + 全套种子约十几秒 —— 这是这条锚点的全部代价，换的是
"前端手写 interface 的数量只会降不会弹"。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
_GEN = ROOT / "scripts" / "tools" / "gen_response_models.py"


def _load_gen():
    spec = importlib.util.spec_from_file_location("grm_under_test", str(_GEN))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["grm_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def seeded():
    """一次真种子 + 原始 JSON 读回（不经过生成模型 —— 样本面判据只看运行时）。"""
    grm = _load_gen()
    from fastapi.testclient import TestClient

    from rolecard_agent.api.main import create_app

    app = create_app(sqlite_path=pathlib.Path(tempfile.mkdtemp()) / "seed-anchor.db")
    grm._seed_minimal_data(app)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    yield grm, app, client
    runtime = getattr(app.state, "ctx", None)
    if runtime is not None:
        runtime.runtime.shutdown()


#: 采样但不接线的端点：`/api/health` 的端点函数住在 `api/main.py`（不在 routers/ 下），
#: 接线工具只改 `api/routers/*.py` ⇒ 它有生成模型、没有 response_model 接线。
#: 形状漂移仍由采样面看住（模型在，OpenAPI 里没有契约这件事是**已知且如实登记**的）。
_UNWIRED_OK = {"/api/health"}


def test_白名单与接线表同集合(seeded) -> None:
    """两份清单是**同一份清单的两半**：谁单独加端点，这里红。

    例外只有一格且写死在上头（`_UNWIRED_OK`）：采样面可以比接线表宽（为了盯形状），
    但**不许反过来** —— 接了线却没采样，OpenAPI 里就引了一个生成物里没有的模型名。
    """
    grm, _app, _client = seeded
    wiring = _wiring_keys()
    missing_wiring = set(grm.READONLY_ENDPOINTS) - wiring - _UNWIRED_OK
    assert not missing_wiring, f"采样了却没登记接线（生成/接线脱节）：{sorted(missing_wiring)}"
    extra_wiring = wiring - set(grm.READONLY_ENDPOINTS)
    assert not extra_wiring, f"接线表引了不在采样面的端点：{sorted(extra_wiring)}"


def _wiring_keys() -> set[str]:
    spec = importlib.util.spec_from_file_location(
        "wrm_under_test", str(ROOT / "scripts" / "tools" / "wire_response_models.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wrm_under_test"] = mod
    spec.loader.exec_module(mod)
    return set(mod.WIRING)


#: 第二批扩面的端点：从前没采样，现在必须进白名单。
#: （/api/session/proactive 不带：它的 role_id 是**查询参数**，白名单键要求与装饰器
#: 路径一字不差才能对账接线 —— 查询串形状另说。）
_NEW_WHITELIST = (
    "/api/channels",
    "/api/session/{thread_id}",
    "/api/session/{thread_id}/turn",
    "/api/session/{thread_id}/messages",
    "/api/session/{thread_id}/context",
)


def test_第二批端点已进白名单(seeded) -> None:
    grm, _app, _client = seeded
    missing = [p for p in _NEW_WHITELIST if p not in grm.READONLY_ENDPOINTS]
    assert not missing, f"未进采样白名单：{missing}"


@pytest.mark.parametrize(
    ("path", "key", "expect"),
    [
        ("/api/mcp/servers", "servers", "dict"),
        ("/api/settings/memory", "items", "dict"),
        ("/api/records", "items", "dict"),
        ("/api/approvals", "items", "dict"),
        ("/api/reachouts", "items", "dict"),
        ("/api/channels", "channels", "dict"),
    ],
)
def test_种子后这些数组非空(seeded, path: str, key: str, expect: str) -> None:
    """**判据本体**：空数组 ⇒ 生成物只能写 `list[dict[str, Any]]` ⇒ 前端迁不过去。
    种子扩了面，这里就必须能采到带字段的元素。"""
    _grm, _app, client = seeded
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} 采不到：{resp.status_code}"
    rows = resp.json()[key]
    assert rows, f"{path} 的 {key} 还是空的 —— _seed_minimal_data 没把它种出来"
    assert isinstance(rows[0], dict), f"{path} 的 {key}[0] 不是对象，元素形状推不出"


def test_收件箱的静默清单也非空(seeded) -> None:
    """quiet 是 ReachoutsView 里最后一个松格：样本角色必须"有资格主动"（reachable 过滤
    按 `reachout_enabled`），否则 quiet 恒空、前端 QuietStatus 永远迁不了。"""
    _grm, _app, client = seeded
    quiet = client.get("/api/reachouts").json()["quiet"]
    assert quiet, "quiet 为空：种子角色没有 reachout_enabled=True 的那一张"


def test_会话列表采到真实线程(seeded) -> None:
    """/api/sessions 的 items 形状 = ThreadRow（前端手写类型里最肥的一族）。"""
    _grm, _app, client = seeded
    rows = client.get("/api/sessions").json()
    assert rows, "会话列表为空：种子没建会话（proactive 那一步没进 _seed_minimal_data）"
    assert {"thread_id", "role_id", "is_proactive", "is_blank"} <= set(rows[0])


def test_凭据组样本里有一个带key的组(seeded) -> None:
    """`key_masked: Any` 的根因：两张样本组都 no-key ⇒ 合并类型只剩 null ⇒ Any。
    种子必须给出一组 api_key，让这一格采成字符串。"""
    _grm, _app, client = seeded
    groups = client.get("/api/settings/models").json()["providers"]
    assert any(g.get("key_masked") for g in groups), (
        "没有一个凭据组带 key_masked —— ProviderGroup 那族前端类型迁不动"
    )


def test_线程端点按样本线程可采(seeded) -> None:
    """`_resolve_path` 替换 {thread_id} 后必须 200：线程 id 的公式住在
    `core/proactive/proactive_thread.py`，白名单里写的是它的展开值 —— 漂了就 404。"""
    grm, _app, client = seeded
    for tail in ("", "/turn", "/messages", "/context"):
        path = grm._resolve_path(f"/api/session/{{thread_id}}{tail}")
        assert "{thread_id}" not in path, "占位没替换"
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} → {resp.status_code}：样本线程不存在或归属漂了"


# ——— 第三批（P1b ③，2026-10-28）：把"采样恒 null"的松格喂成非空的那批种子 ———
# 判据与第二批同构：种子扩了面就钉住"非空代表值必须采得到"，缩了这里红。


def test_审批行的三个恒null格有非空代表(seeded) -> None:
    """cwd / thread_id / result：产品里分别只有 run_command、会话链路、done 行才有
    非空 —— 种子必须各喂出一条（_seed_approval_shapes）。"""
    _grm, _app, client = seeded
    rows = client.get("/api/approvals").json()["items"]
    assert rows, "审批列表空了"
    assert any(r["cwd"] for r in rows), "cwd 全空：带 cwd 的 pending 行没种上"
    assert any(r["thread_id"] for r in rows), "thread_id 全空"
    assert any(r["result"] for r in rows), "result 全空：finish 的 done 行没种上"


def test_收件箱行的会话派生非空(seeded) -> None:
    """thread_id 是读侧派生（查到该角色的主动线程才非 null）：sample_reachable
    的主动线程没建，收件箱这一格就恒 null（ReachoutRow 迁不动的根）。"""
    _grm, _app, client = seeded
    rows = client.get("/api/reachouts").json()["items"]
    assert rows and any(r["thread_id"] for r in rows), "收件箱 thread_id 全空"


def test_运行时覆盖采得到可见值(seeded) -> None:
    """override_value 只在"有覆盖且非 secret"时非 null：种一条 web_allowed_domains。"""
    _grm, _app, client = seeded
    groups = client.get("/api/settings/runtime").json()["groups"]
    items = [i for g in groups for i in (g.get("items") or [])]
    assert any(i.get("override_value") for i in items), "没有一条可见的运行时覆盖"


def test_模型行有上下文窗口与采样惩罚(seeded) -> None:
    """num_ctx / repeat_penalty 只有显式写过才非 null（PUT 带 num_ctx + set_sampling）。"""
    _grm, _app, client = seeded
    groups = client.get("/api/settings/models").json()["providers"]
    models = [m for g in groups for m in (g.get("models") or [])]
    assert any(m.get("num_ctx") for m in models), "num_ctx 全空"
    assert any(m.get("repeat_penalty") for m in models), "repeat_penalty 全空"


def test_服务候选有解析到云后端的行(seeded) -> None:
    """候选行的 base_url/model/ref_backend 只有"引用行解析到已配置后端"才非 null。"""
    _grm, _app, client = seeded
    view = client.get("/api/services").json()
    cands = [c for s in view["services"] for c in (s.get("candidates") or [])]
    assert any(c.get("base_url") and c.get("ref_backend") for c in cands), (
        "没有一个候选解析到云后端 —— embedding 的 sample_cloud 引用没种上"
    )


def test_时间线有游标更正事件与会话锚点(seeded) -> None:
    """next_cursor（>limit 条记忆）、memory_correct（merge_items）、thread 锚点
    （倒填 created_at + touch 出的 recent 事件）—— TimelineEvent/Page 的三格。"""
    _grm, _app, client = seeded
    page = client.get("/api/roles/sample_role/timeline").json()
    assert page["next_cursor"], "next_cursor 为空：记忆条数没越过 limit=50"
    kinds = {e["kind"] for e in page["items"]}
    assert "memory_correct" in kinds, "没有更正事件：from_text 恒 null 的根"
    assert "thread" in kinds, "页内没有线程锚点：created_at 倒填 + touch 那一手没生效"
    assert any(e["from_text"] for e in page["items"]), "from_text 全空"


def test_报告行的备注与文本指标有非空代表(seeded) -> None:
    """note / value_text / ref_range / raw_text：create_report 本就收这些键，
    种子不喂就恒 null（IndexRow / ReportRecord 迁不动的根）。"""
    _grm, _app, client = seeded
    recs = client.get("/api/records").json()["items"]
    assert any(r.get("note") for r in recs), "报告 note 全空"
    idx = [i for r in recs for i in (r.get("indices") or [])]
    assert any(i.get("value_text") for i in idx), "value_text 全空"
    assert any(i.get("ref_range") for i in idx), "ref_range 全空"
    assert any(i.get("raw_text") for i in idx), "raw_text 全空"


def test_接线表里的路由文件路径真实存在() -> None:
    """/api/records 的前科：接线键集对账查不出"文件路径写错"——`../domains/…` 指向
    不存在的 api/domains/，records 从第一批起就**从来没接上线**（P1b 第三批抓到）。
    键集锚点之外，路径本体也必须存在。"""
    spec = importlib.util.spec_from_file_location(
        "wrm_paths", str(ROOT / "scripts" / "tools" / "wire_response_models.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wrm_paths"] = mod
    spec.loader.exec_module(mod)
    routers = ROOT / "src" / "rolecard_agent" / "api" / "routers"
    for api_path, (_model, filename) in mod.WIRING.items():
        assert (routers / filename).resolve().exists(), (
            f"{api_path} 的接线文件 {filename} 不存在 —— 接线从未生效"
        )
