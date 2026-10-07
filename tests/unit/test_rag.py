"""v2.1 RAG 检索内核测试：切块 / 索引 / 作用域隔离 / search_knowledge 工具。

Traceability: US-8（知识作用域 —— 角色只声明，内核掌库）。

全部离线：HashEmbedder 确定性向量（真实嵌入只影响质量，不影响链路正确性）。
作用域安全模型的三条不变量：
  1. search_knowledge 的签名里没有 scope —— 模型无法指定检索哪个集合；
  2. 可检索范围 = 当前角色 knowledge_scopes 的并集（execute_tools 注入）；
  3. 未授权角色得到明确拒绝，而不是空结果或报错。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.base.scopes import (
    role_knowledge_scopes_ctx,
)
from rolecard_agent.config import DEFAULT_SILICONFLOW_BASE_URL, Settings
from rolecard_agent.core.services import EndpointConfig
from rolecard_agent.rag.retriever import (
    _EMBED_BATCH,
    _EMBED_RETRIES,
    EmbedError,
    HashEmbedder,
    KnowledgeBase,
    SiliconFlowEmbedder,
    SiliconFlowReranker,
    chunk_text,
    make_embedder,
    make_reranker,
    make_search_tool,
)
from rolecard_agent.roles.service import RoleCards, RoleCardService


def cards(store: RoleCardService) -> RoleCards:
    """测试里"本机主人眼里的那些卡"的简写（M2a 之后每次读写都得说清为谁）。"""
    return store.scoped(DEFAULT_USER_ID)


class _FakeProjection:
    """来源投影表的内存替身（`R102-55`）：rag 单测只问"三个写入口有没有把话说给投影"；
    真正的 SQL 与一次性种子由 tests/unit/test_knowledge_sources.py 对真 sqlite 验。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, str]] = {}  # scope -> {source_key: name}

    def names(self, scope: str) -> list[str]:
        return sorted(set(self.rows.get(scope, {}).values()))

    def remember(self, scope: str, source_key: str, name: str) -> None:
        self.rows.setdefault(scope, {})[source_key] = name

    def forget(self, scope: str, source_key: str) -> None:
        self.rows.get(scope, {}).pop(source_key, None)

    def forget_scope(self, scope: str) -> None:
        self.rows.pop(scope, None)


@pytest.fixture
def kb(tmp_path: Path) -> KnowledgeBase:
    return KnowledgeBase(tmp_path / "chroma", HashEmbedder(), sources_store=_FakeProjection())


DOC_A = "胆囊结石随访须知：每 3 到 6 个月复查一次超声。若出现腹痛、发热或黄疸，请及时就医。"
DOC_B = "健康饮食建议：低脂饮食，规律作息，多饮水。"


# -- chunker ---------------------------------------------------------------------------


def test_chunk_merges_paragraphs_up_to_size() -> None:
    text = "第一段。\n\n第二段。\n\n第三段。"
    chunks = chunk_text(text, size=50, overlap=10)
    assert len(chunks) == 1  # 全部合并进一块
    assert "第一段" in chunks[0] and "第三段" in chunks[0]


def test_chunk_hard_splits_overlong_paragraph_with_overlap() -> None:
    text = "字" * 1200
    chunks = chunk_text(text, size=500, overlap=80)
    assert len(chunks) >= 3
    # 重叠：相邻块的尾部与头部有公共内容
    assert chunks[0][-80:] == chunks[1][:80]


def test_chunk_empty_text_returns_nothing() -> None:
    assert chunk_text("  \n\n  ") == []


# -- KnowledgeBase：索引 + 作用域隔离 ----------------------------------------------------


def test_index_and_search_round_trip(kb: KnowledgeBase) -> None:
    assert kb.index("health_reports", "随访须知.md", DOC_A) >= 1
    hits = kb.search(["health_reports"], "复查频率是多少", k=3)
    assert hits, "应检索到随访须知"
    assert hits[0].source == "随访须知.md"
    assert "复查" in hits[0].text or "超声" in hits[0].text


def test_scope_isolation(kb: KnowledgeBase) -> None:
    """US-8：检索范围严格限定在授权作用域内 —— 其他集合的内容不可见。"""
    kb.index("health_reports", "a.txt", DOC_A)
    kb.index("private_scope", "b.txt", DOC_A)  # 同样内容放在另一个作用域
    hits = kb.search(["health_reports"], "随访")
    assert all(h.scope == "health_reports" for h in hits)


def test_search_unknown_scope_returns_empty(kb: KnowledgeBase) -> None:
    kb.index("health_reports", "a.txt", DOC_A)
    assert kb.search(["不存在的集合"], "随访") == []


def test_same_source_reindex_is_idempotent(kb: KnowledgeBase) -> None:
    kb.index("health_reports", "a.txt", DOC_A)
    assert kb.index("health_reports", "a.txt", DOC_A) >= 1
    hits = kb.search(["health_reports"], "随访")
    assert len([h for h in hits if h.source == "a.txt"]) <= 4  # 不因重复入库而爆炸
    ids = kb._client.get_collection("health_reports").get()["ids"]
    assert len(ids) == len(set(ids))  # id 唯一：同源重建覆盖而非追加


def test_two_files_with_the_same_name_do_not_eat_each_other(kb: KnowledgeBase) -> None:
    """P0 回归（审查报告 2026-09-17）：索引身份是 task id，**不是文件名**。

    改前上传路径传的是原始文件名，而分块 id 由 `md5(f"{source}:{i}")` 推导 ——
    两个「报告.pdf」的 id 完全重合，后一份 upsert 直接覆盖前一份：旧文档的索引
    永久消失，检索还会拿错内容，全程没有任何提示。
    """
    kb.index("health_reports", "task-1", DOC_A, source_name="报告.pdf")
    kb.index("health_reports", "task-2", DOC_B, source_name="报告.pdf")

    ids = kb._client.get_collection("health_reports").get()["ids"]
    assert len(ids) == len(set(ids))

    hits = kb.search(["health_reports"], "胆囊结石随访", k=8)
    assert any("胆囊" in h.text for h in hits), "先上传的那份被后者覆盖了"
    assert {h.source for h in hits} == {"报告.pdf"}  # 展示的仍是文件名
    assert {h.source_key for h in hits} == {"task-1", "task-2"}  # 身份彼此独立


def test_scope_count_only_treats_missing_collection_as_zero(
    kb: KnowledgeBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`R102-04`：问不到 ≠ 没有 —— 只有"集合不存在"给 0，其余异常照抛。

    从前 `except Exception: return 0` 会把"客户端这次问不到"（忙、盘、连接坏了）也报成
    空库；0 是**正常结果**，于是界面显示空库、幂等判断以为没有 —— 静默把未知报成正常。
    """
    assert kb.scope_count("never-created") == 0  # 真不存在仍是 0（原语义保留）

    def broken(*, name: str):  # noqa: ANN202
        raise RuntimeError("这次问不到（盘/锁/连接）")

    monkeypatch.setattr(kb._client, "get_collection", broken)
    with pytest.raises(RuntimeError, match="问不到"):
        kb.scope_count("health_reports")


def test_delete_source_propagates_read_failures(
    kb: KnowledgeBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`R102-04` 同族：删除时"取分块"失败不许报成"删干净了"（契约就是失败即失败）。"""
    kb.index("health_reports", "task-1", DOC_A)

    class _Collection:
        @staticmethod
        def get(*, where: dict[str, str]) -> dict[str, object]:
            raise RuntimeError("取分块失败")

    monkeypatch.setattr(kb._client, "get_collection", lambda *, name: _Collection())
    with pytest.raises(RuntimeError, match="取分块失败"):
        kb.delete_source("health_reports", "task-1")


def test_source_name_is_only_a_label(kb: KnowledgeBase) -> None:
    """展示名不参与身份：同一个文档改个名重建，仍是同一份（不产生第二份）。"""
    kb.index("health_reports", "task-1", DOC_A, source_name="报告.pdf")
    before = kb.scope_count("health_reports")

    kb.index("health_reports", "task-1", DOC_A, source_name="报告（改名）.pdf")

    assert kb.scope_count("health_reports") == before
    hits = kb.search(["health_reports"], "随访", k=4)
    assert {h.source for h in hits} == {"报告（改名）.pdf"}


def test_legacy_chunks_without_source_name_still_show_their_name(kb: KnowledgeBase) -> None:
    """本次改动前入库的分块没有 `source_name`：**检索结果**的展示必须退回 `source`。

    真机上就有一批这样的分块（旧索引不会自动重建），所以这条回落是必需的。
    概览页（describe）那半边自 `R102-55` 起由投影表负责，存量分块的入库见
    tests/unit/test_knowledge_sources.py 的一次性种子用例。
    """
    collection = kb._client.get_or_create_collection(name="health_reports")
    collection.add(  # 故意绕过 index()，模拟旧版元数据
        ids=["legacy-1"],
        embeddings=[[0.0] * HashEmbedder.DIM],
        documents=[DOC_A],
        metadatas=[{"source": "旧文件.txt", "scope": "health_reports", "chunk": 0}],
    )

    hits = kb.search(["health_reports"], "随访", k=4)
    assert [h.source for h in hits] == ["旧文件.txt"]
    assert hits[0].source_key == "旧文件.txt"


def test_projection_tracks_index_delete_and_reset(tmp_path: Path) -> None:
    """投影表（`R102-55`）：index 记住 / delete_source 忘掉 / reset_scope 清空。

    概览页的来源清单从此出投影，不再全量倒灌分块元数据 —— 三个写入口就是全部合同。
    """
    store = _FakeProjection()
    kb = KnowledgeBase(tmp_path / "chroma", HashEmbedder(), sources_store=store)
    kb.index("health_reports", "task-1", DOC_A, source_name="报告A.pdf")
    kb.index("health_reports", "task-2", DOC_B, source_name="报告B.pdf")
    assert store.rows["health_reports"] == {"task-1": "报告A.pdf", "task-2": "报告B.pdf"}
    assert kb.describe()[0]["sources"] == ["报告A.pdf", "报告B.pdf"]

    kb.delete_source("health_reports", "task-1")
    assert store.rows["health_reports"] == {"task-2": "报告B.pdf"}

    kb.reset_scope("health_reports")
    assert "health_reports" not in store.rows


def test_reindex_after_delete_restores_the_projection(tmp_path: Path) -> None:
    """删除后再传同一份文件（状态机不推进的重用路）：分块重建，投影也必须回来。

    写入口挂在 `index()` 本体上、不挂状态推进 —— 正是为了"重传同一份字节"这条真实路径：
    那时 `advance` 一步不走（任务已是 indexed），只有 index() 会再次被调用。
    """
    store = _FakeProjection()
    kb = KnowledgeBase(tmp_path / "chroma", HashEmbedder(), sources_store=store)
    kb.index("health_reports", "task-1", DOC_A, source_name="报告A.pdf")
    kb.delete_source("health_reports", "task-1")
    assert store.rows.get("health_reports") == {}
    kb.index("health_reports", "task-1", DOC_A, source_name="报告A.pdf")
    assert store.rows["health_reports"] == {"task-1": "报告A.pdf"}


def test_delete_source_removes_only_that_document(kb: KnowledgeBase) -> None:
    """P1-1 的底座：报告行删了，它的检索分块也必须跟着走，且不能误伤同作用域别的文档。"""
    kb.index("health_reports", "task-1", DOC_A, source_name="报告A.pdf")
    kb.index("health_reports", "task-2", DOC_B, source_name="报告B.pdf")
    before = kb.scope_count("health_reports")

    removed = kb.delete_source("health_reports", "task-1")

    assert removed >= 1
    assert kb.scope_count("health_reports") == before - removed
    hits = kb.search(["health_reports"], "胆囊结石随访", k=8)
    assert all(h.source_key != "task-1" for h in hits), "删掉的那份仍能被检索到"
    assert any(h.source_key == "task-2" for h in hits), "不该连带删掉别的文档"


def test_delete_source_is_idempotent_on_missing_targets(kb: KnowledgeBase) -> None:
    """不存在的作用域/来源都返回 0 且不抛错：删除路径要能被重复调用。"""
    assert kb.delete_source("没这个作用域", "task-1") == 0
    assert kb.index("health_reports", "task-1", DOC_A) >= 1

    assert kb.delete_source("health_reports", "task-1") >= 1
    assert kb.delete_source("health_reports", "task-1") == 0


def test_reindexing_with_empty_text_clears_the_old_chunks(kb: KnowledgeBase) -> None:
    """P1-7 回归：同一份文件重传成空，不能留着旧分块继续被检索。

    以前 `index()` 遇到空文本在 **stale 清理之前**就 `return 0`，于是上一次入的
    分块原样留着 —— 内容已经没有了，模型却还能检索到并引用它，docstring 承诺的
    "同 source 幂等重建"当场被打破。
    """
    assert kb.index("health_reports", "task-1", DOC_A) >= 1
    assert kb.search(["health_reports"], "胆囊结石随访", k=4)

    assert kb.index("health_reports", "task-1", "   ") == 0

    assert kb.scope_count("health_reports") == 0
    assert kb.search(["health_reports"], "胆囊结石随访", k=4) == []


# -- search_knowledge 工具：作用域安全模型 ----------------------------------------------


@pytest.fixture
def search_tool(kb: KnowledgeBase):
    return make_search_tool(kb)


def test_tool_denies_when_role_has_no_scopes(search_tool) -> None:
    """未声明 knowledge_scopes 的角色 → 明确拒绝（不是空结果，更不是越权检索）。"""
    role_knowledge_scopes_ctx.set(())
    out = search_tool.invoke({"query": "复查频率"})
    assert "未授权" in out and "knowledge_scopes" in out


def test_tool_returns_hits_for_authorised_role(kb: KnowledgeBase, search_tool) -> None:
    kb.index("health_reports", "随访须知.md", DOC_A)
    role_knowledge_scopes_ctx.set(("health_reports",))
    out = search_tool.invoke({"query": "复查频率"})
    assert "检索到的知识片段" in out
    assert "随访须知.md" in out


def test_scopes_never_in_tool_signature(search_tool) -> None:
    """安全模型的关键断言：工具参数表里没有 scope —— 模型无从指定检索集合。"""
    schema = search_tool.args_schema.model_json_schema()
    assert "query" in schema.get("properties", {})
    assert "scope" not in schema.get("properties", {})


# -- 嵌入/重排选型：「服务」页的端点序是唯一事实面（P1-5 收口） --------------------------------


def _row(
    eid: str,
    *,
    kind: str = "cloud",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> EndpointConfig:
    """一条服务端点行的快照（工厂消费的形态）。内置行（hash/off/rapidocr）kind=local。"""
    return EndpointConfig(
        id=eid,
        label=eid,
        kind=kind,
        base_url=base_url,
        api_key=api_key,
        model=model,
        enabled=True,
        builtin=kind == "local",
    )


def _strategy(*rows: EndpointConfig) -> tuple[list[str], dict[str, EndpointConfig]]:
    """按启用序给出 (order, endpoints) —— 与 AppContext 交给工厂的形状一致。"""
    return [r.id for r in rows], {r.id: r for r in rows}


def test_embedder_takes_the_first_usable_row_in_order() -> None:
    order, endpoints = _strategy(_row("sf", api_key="sk-row"), _row("hash", kind="local"))
    assert make_embedder(Settings(), order=order, endpoints=endpoints).name == "siliconflow"

    # 排在后面的行轮不到：把 hash 提前就用 hash（内置行恒可用 = 出厂默认）。
    order, endpoints = _strategy(_row("hash", kind="local"), _row("sf", api_key="sk-row"))
    assert make_embedder(Settings(), order=order, endpoints=endpoints).name == "hash"


def test_embedder_uses_the_row_own_credentials_not_any_env_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-4 + P1-5 的合流：凭据只有一个家 = 端点行所引用的模型页后端。

    以前这里既可能取行内 key、也可能取 `SILICONFLOW_API_KEY` env（两条路径、两个事实面）。
    现在 env 里塞什么都不影响工厂，而行的 base_url/model 必须被逐行消费（多云端实例各用各的）。
    """
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-from-env")
    order, endpoints = _strategy(
        _row("sf", api_key="sk-from-row", base_url="https://row.example/v1", model="m-row")
    )
    embedder = make_embedder(Settings(), order=order, endpoints=endpoints)
    auth = embedder._client.headers.get("Authorization") or ""  # noqa: SLF001
    assert "sk-from-row" in auth and "sk-from-env" not in auth
    assert str(embedder._client.base_url).rstrip("/") == "https://row.example/v1"  # noqa: SLF001


def test_embedder_falls_back_to_settings_base_url_and_capability_default_model() -> None:
    """行内未填 base_url → 用 `Settings.siliconflow_base_url`；未填 model → 用 bge-m3。"""
    order, endpoints = _strategy(_row("sf", api_key="sk-row"))
    embedder = make_embedder(
        Settings(siliconflow_base_url="https://mirror.example/v1"),
        order=order,
        endpoints=endpoints,
    )
    assert str(embedder._client.base_url).rstrip("/") == "https://mirror.example/v1"  # noqa: SLF001
    assert embedder._model == "BAAI/bge-m3"  # noqa: SLF001
    # 出厂默认端点只有一处常量（字段默认即它），改它只需要改 config.py 一行。
    assert Settings().siliconflow_base_url == DEFAULT_SILICONFLOW_BASE_URL


def test_embedder_fails_loudly_when_no_row_is_usable() -> None:
    """云端行没配 key 又没有 hash 内置行 → 启动即报错。

    静默降级成质量很差的检索会让人以为一切正常；而"服务策略里一个可用嵌入端点都没有"
    是配置错误，必须大声（服务页本身不允许停掉最后一条启用行，所以这只能来自手改库）。
    """
    order, endpoints = _strategy(_row("sf", api_key=None))
    with pytest.raises(RuntimeError, match="没有可用的嵌入端点"):
        make_embedder(Settings(), order=order, endpoints=endpoints)


def test_reranker_off_row_means_vector_order_and_missing_key_is_not_a_failure() -> None:
    """重排是质量增强：`off` 行 = 关闭；云端行缺 key 顺延而不是故障。"""
    order, endpoints = _strategy(_row("off", kind="local"))
    assert make_reranker(Settings(), order=order, endpoints=endpoints) is None

    order, endpoints = _strategy(_row("sf", api_key=None), _row("off", kind="local"))
    assert make_reranker(Settings(), order=order, endpoints=endpoints) is None

    order, endpoints = _strategy(_row("sf", api_key="sk-row", model="rerank-m"))
    reranker = make_reranker(Settings(), order=order, endpoints=endpoints)
    assert reranker is not None and reranker._model == "rerank-m"  # noqa: SLF001


# -- 集成：search_knowledge 在内核执行器里也拿到作用域 ---------------------------------


def test_execute_tools_injects_role_scopes_to_search_tool(
    kb: KnowledgeBase,
    tmp_path: Path,
    roles,  # noqa: ANN001 - conftest fixture
) -> None:
    """最小闭环：index → execute_tools 调 search_knowledge → 命中授权作用域内容。"""
    from langchain_core.messages import ToolMessage

    from rolecard_agent.base.observability import NullTracer
    from rolecard_agent.core.nodes import KernelContext, execute_tools
    from rolecard_agent.core.tools.registry import ToolRegistry
    from rolecard_agent.roles.models import RoleCardCreate

    kb.index("health_reports", "随访须知.md", DOC_A)
    reg = ToolRegistry()
    reg.register(make_search_tool(kb))
    cards(roles).create(
        RoleCardCreate(
            role_id="scholar",
            role_name="学者",
            system_prompt="x",
            knowledge_scopes=["health_reports"],
        )
    )
    ctx = KernelContext(
        model=None,  # execute_tools 不触模型
        registry=reg,
        roles=roles,
        tracer=NullTracer(),
        settings=Settings(),
        enabled_domains=lambda: (),
        tool_epoch=lambda: 1,
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "search_knowledge", "args": {"query": "复查频率"}, "id": "c1"}
                ],
            )
        ],
        "current_role_id": "scholar",
        "enabled_domains": [],
        "thread_id": "t",
    }
    out = execute_tools(state, ctx)
    message = out["messages"][0]
    assert isinstance(message, ToolMessage)
    assert "随访须知.md" in message.content  # 作用域经内核注入，命中授权集合


# -- v2.2 检索延迟细分（P50/P95/P99，按阶段） -------------------------------------------


class _Recorder:
    """最小 tracer 桩：只收集 emit 的事件，供断言（不依赖 LocalTracer 的 I/O）。"""

    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)


class _FailingReranker:
    """鸭子类型的重排器桩：rerank 恒返回 None（模拟调用失败 → 调用方回退向量序）。"""

    name = "fake"

    def rerank(self, _query: str, _documents: list[str]) -> None:
        return None


def test_latency_p95_empty_before_any_search(kb: KnowledgeBase) -> None:
    """没有检索发生时分位为 None —— "没有数据"与"0ms"必须可区分。"""
    metrics = kb.latency_p95()
    assert metrics["samples"] == 0
    assert metrics["p95"]["total_ms"] is None


def test_search_records_latency_samples(kb: KnowledgeBase) -> None:
    kb.index("health_reports", "随访须知.md", DOC_A)
    for _ in range(5):
        kb.search(["health_reports"], "复查频率", k=3)
    metrics = kb.latency_p95()
    assert metrics["samples"] == 5
    assert metrics["rerank_enabled"] is False
    assert metrics["embedder"] == "hash"
    assert metrics["p95"]["embed_ms"] is not None
    assert metrics["p95"]["rerank_ms"] < 5  # 未挂重排器：该阶段开销可忽略
    assert metrics["p95"]["total_ms"] >= metrics["p95"]["vector_ms"]


def test_search_emits_rag_search_trace(kb: KnowledgeBase) -> None:
    kb.index("health_reports", "随访须知.md", DOC_A)
    rec = _Recorder()
    kb.search(["health_reports"], "复查频率", k=3, tracer=rec)
    rag_events = [e for e in rec.events if getattr(e, "event", None) == "rag_search"]
    assert rag_events, "应 emit rag_search 留痕"
    detail = rag_events[0].detail  # type: ignore[attr-defined]
    assert {"embed_ms", "vector_ms", "rerank_ms", "reranked"} <= set(detail)


def test_rerank_failure_falls_back_and_is_timed(tmp_path: Path) -> None:
    """挂了重排器但调用失败 → 回退向量序仍返回结果，并留痕 rerank_fallback。

    需要 ≥2 个候选，否则 search 会跳过重排（len(hits) > 1 守卫）——故索引两份文档。
    """
    kb = KnowledgeBase(tmp_path / "chroma", HashEmbedder(), _FailingReranker())  # type: ignore[arg-type]
    kb.index("health_reports", "随访须知.md", DOC_A)
    kb.index("health_reports", "饮食建议.md", DOC_B)
    rec = _Recorder()
    hits = kb.search(["health_reports"], "复查频率", k=3, tracer=rec)
    assert hits, "重排失败必须回退向量序，仍返回结果"
    assert kb.latency_p95()["rerank_enabled"] is True
    assert any(getattr(e, "event", None) == "rerank_fallback" for e in rec.events)

# -- 索引的原子性与嵌入分片（代码审查报告（第二轮）M4 / M5） -----------------------


class _FailingEmbedder(HashEmbedder):
    """前 n 次正常，之后开始失败 —— 用来模拟"嵌入这一步炸了"。"""

    name = "failing"

    def __init__(self, fail_after: int = 0) -> None:
        self._left = fail_after

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self._left <= 0:
            raise RuntimeError("embedding backend exploded")
        self._left -= 1
        return super()._embed_batch(texts)


def test_index_failure_does_not_destroy_the_existing_chunks(tmp_path: Path) -> None:
    """**幂等重建必须不丢数据**：嵌入失败时旧分块要原样还在。

    修复前的顺序是 `delete(source)` → `embed()` → `add()`：嵌入一失败，该来源的既有分块
    已经被删掉 —— 检索里凭空少一份文档，而 ingestion 台账那边还写着 `indexed`。
    这种"状态与事实背离"比直接报错难查得多。
    """
    embedder = _FailingEmbedder(fail_after=99)
    kb = KnowledgeBase(tmp_path / "chroma", embedder)
    kb.index("health_reports", "a.txt", DOC_A)
    assert kb.search(["health_reports"], "随访"), "前置条件：第一次索引应当成功"

    embedder._left = 0  # 之后每次嵌入都失败
    with pytest.raises(RuntimeError):
        kb.index("health_reports", "a.txt", DOC_A)

    embedder._left = 99  # 修好它，才能验证"库里的旧分块还在"
    assert kb.search(["health_reports"], "随访"), (
        "嵌入失败后旧的索引被清空了 —— 这正是修复前的问题"
    )


class _CountingEmbedder(SiliconFlowEmbedder):
    """把真正发请求的那一层换成计数器，用来验证**分片循环**（不打网络）。

    刻意不调父类 `__init__`（那会建 httpx 客户端）：这里要测的是"分几批"，不是传输。
    """

    def __init__(self) -> None:
        self.batches: list[int] = []
        self._model = "stub"

    def _embed_chunk(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        return [[0.0] * 4 for _ in texts]


# -- 嵌入重试与重排降级的**真实**失败路径（覆盖率基线最后一格：retriever 82%） ----------
#
# 上面的 `_CountingEmbedder` / `_FailingReranker` 都把"发请求那一层"整个换掉了，
# 于是重试循环与降级判定这两个函数体**从没跑过**。这一族恰好全是"只有真失败才暴露"的形状：
#
#   * 重试次数有上限（上游挂了不能把一次上传吊死），而耗尽后必须报 `EmbedError` 并带上
#     **最后一次的真因**；
#   * "宁可不入库，也不要半份索引"：第一批成功、第二批失败 ⇒ 整次失败，一条向量都不返回
#     （半份索引会让检索静默只查到文件前半，而没有任何地方说"这份文档少了后半"）；
#   * 重排失败必须是 None（降级）而**不能是空列表** —— 调用方把空列表读成"精排后一条不剩"，
#     一次响应格式漂移就能把检索打成静默无结果（探针实测：同库同查询 2 条 → 0 条）；
#   * 成功路径要按**后端给的顺序**返回，不许在客户端悄悄重排。


class _RetryClient:
    """假 httpx 客户端：前 fail_times 次抛错，之后成功。用来数真实重试次数的边界。"""

    def __init__(self, fail_times: int, *, payload: dict | None = None) -> None:
        self._fail = fail_times
        self.calls = 0
        self._payload = payload or {
            "data": [{"embedding": [0.5]}, {"embedding": [0.25]}]
        }

    def post(self, *_a: object, **_k: object) -> object:
        self.calls += 1
        if self.calls <= self._fail:
            raise ConnectionError(f"上游挂了第 {self.calls} 次")
        return _RagResp(json_body=self._payload)


def _embedder_with(client: object) -> SiliconFlowEmbedder:
    emb = SiliconFlowEmbedder.__new__(SiliconFlowEmbedder)  # 不建真 httpx 客户端
    emb._client = client  # type: ignore[assignment]
    emb._model = "m"
    return emb


class _RagResp:
    """假 httpx 响应：够 `raise_for_status()` 与 `json()` 两件事就够测试用。"""

    def __init__(self, json_body: dict, status: int = 200) -> None:
        self._json, self.status_code = json_body, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise ConnectionError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._json


def test_embeddings_are_sent_in_batches() -> None:
    """长文档必须分批嵌入（修复前一次 POST 全部 chunk，顶到超时就是整篇上传失败）。"""
    embedder = _CountingEmbedder()
    total = _EMBED_BATCH * 2 + 5
    vectors = embedder.embed(["文本" * 20 for _ in range(total)])

    assert len(vectors) == total
    assert embedder.batches == [_EMBED_BATCH, _EMBED_BATCH, 5], embedder.batches


def test_嵌入重试成功时不该把失败报出去(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一次失败、第二次成功 = 一次普通的瞬时故障，结果必须正常返回。

    `sleep` 打桩掉：这里判的是"重试了几次、最后成没成"，不是退避时长；不桩掉的话
    每次跑这套用例都要真等 0.5s + 1s（退避本身另有一条用例单独判）。
    """
    slept: list[float] = []
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: slept.append(s), raising=True)
    client = _RetryClient(fail_times=1)
    emb = _embedder_with(client)
    assert emb._embed_chunk(["a", "b"]) == [[0.5], [0.25]]  # noqa: SLF001
    assert client.calls == 2, "失败一次就该再试一次"
    assert slept == [0.5], f"退避应当从 0.5s 起、且真的睡了：{slept}"


def test_嵌入重试耗尽报的是带次数的可读错误(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试上限是有的（否则一次上传会吊死到 job 超时），耗尽后要说清几次、为什么。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda _s: None, raising=True)
    client = _RetryClient(fail_times=99)
    emb = _embedder_with(client)
    with pytest.raises(EmbedError) as got:
        emb._embed_chunk(["a", "b"])  # noqa: SLF001
    assert f"重试 {_EMBED_RETRIES} 次后仍失败" in str(got.value), str(got.value)
    assert "上游挂了第 3 次" in str(got.value), "最后一次的真因必须带出来"
    # 1 次首发 + _EMBED_RETRIES 次重试，不多不少
    assert client.calls == _EMBED_RETRIES + 1, client.calls


def test_退避是指数而不是固定值(monkeypatch: pytest.MonkeyPatch) -> None:
    """`0.5 * 2**attempt`：固定值会在上游过载时越重试越糟（同一时刻挤更多请求）。"""
    import time as _time

    slept: list[float] = []
    monkeypatch.setattr(_time, "sleep", lambda s: slept.append(s), raising=True)
    emb = _embedder_with(_RetryClient(fail_times=99))
    with pytest.raises(EmbedError):
        emb._embed_chunk(["a"])  # noqa: SLF001
    assert slept == [0.5, 1.0], slept


def test_一批失败不许返回半份向量(monkeypatch: pytest.MonkeyPatch) -> None:
    """**"宁可不入库，也不要半份索引"**：第二批失败 ⇒ 整次失败，一条都不返回。

    若这里返回第一批的结果，调用方会把"只索引了前半"的文档当成索引成功 ——
    检索永远查不到后半部分内容，而没有任何一处会报错或留痕。
    """
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda _s: None, raising=True)

    class _SecondBatchFails:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *_a: object, **_k: object) -> object:
            self.calls += 1
            if self.calls > 1:  # 第一批成功、第二批怎么试都失败
                raise ConnectionError("第二批炸了")
            return _RagResp(
                json_body={"data": [{"embedding": [0.5]}] * _EMBED_BATCH}
            )

    emb = _embedder_with(_SecondBatchFails())
    total = _EMBED_BATCH * 2
    with pytest.raises(EmbedError):
        emb.embed(["文" * 30] * total)


# -- rerank 真实函数体：降级与成功两路 ------------------------------------------------


def _reranker_body(client: object) -> SiliconFlowReranker:
    rr = SiliconFlowReranker.__new__(SiliconFlowReranker)  # 不建真 httpx 客户端
    rr._client = client  # type: ignore[assignment]
    rr._model = "m"
    return rr


class _OneShotClient:
    """一个只回固定响应体的假 httpx 客户端（`rerank` 要的是客户端，不是响应）。"""

    def __init__(self, body: dict, status: int = 200) -> None:
        self._resp = _RagResp(body, status)

    def post(self, *_a: object, **_k: object) -> _RagResp:
        return self._resp


def test_重排成功按后端给的顺序返回不自己排序() -> None:
    """后端已经按相关性排好了；客户端再排一次会打乱它的序（分数与索引都可能不同源）。"""
    r = _reranker_body(
        _OneShotClient(
            {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ]
            }
        )
    )
    assert r.rerank("q", ["A", "B"]) == [(1, 0.9), (0, 0.1)]


def test_重排响应缺结果按降级处理而不是零条命中() -> None:
    """200 但没有 results ⇒ **None**（调用方回退向量序），不能是 `[]`。

    探针实测的落差：同一个库、同一个查询，不挂重排器命中 2 条，挂上"返回 [] 的重排器"
    命中 0 条 —— 一次响应格式漂移就足以让知识库**静默查无结果**，而界面上看起来一切正常。
    """
    for body in ({}, {"results": []}, {"results": None}):
        r = _reranker_body(_OneShotClient(body))
        assert r.rerank("q", ["A", "B"]) is None, f"{body} 应降级为 None"


def test_重排器字段残缺或状态码异常都走降级() -> None:
    """后端给了 results 但字段不对、或 HTTP 4xx/5xx：都不能让一次"质量增强"打挂检索。"""
    missing = _reranker_body(_OneShotClient({"results": [{"nope": 1}]}))
    assert missing.rerank("q", ["A", "B"]) is None
    err = _reranker_body(_OneShotClient({"results": []}, status=503))
    assert err.rerank("q", ["A", "B"]) is None


def test_重排器失败时整条检索仍有结果() -> None:
    """端到端一臂：**真**重排器（连不上 ⇒ 走真实 except 分支返回 None）下检索照常。

    现有的那条回退用例把重排器整个换成了返回 None 的假对象，所以真实的异常分支从没跑过。
    这里用真的 `SiliconFlowReranker` 指到一个必然失败的地址，让异常→None→回退向量序
    这条链完整地走一遍。
    """
    tmp = Path(tempfile.mkdtemp())
    rr = SiliconFlowReranker(api_key="k", base_url="http://127.0.0.1:9")  # 端口 9 必拒
    kb = KnowledgeBase(tmp / "chroma", HashEmbedder(), rr)
    kb.index("health_reports", "a.md", DOC_A)
    kb.index("health_reports", "b.md", DOC_B)
    hits = kb.search(["health_reports"], "复查频率", k=3)
    assert len(hits) == 2, f"重排器连不上时检索必须照旧出结果：{hits}"
    rr.close()


def test_响应缺results时端到端仍能查到东西(tmp_path: Path) -> None:
    """**这一格的回归钉子**：200 但响应没有 `results` ⇒ 检索照常命中，不是查无结果。

    修之前这一条会拿到 0 命中（探针实测：同一个库同一个查询，不挂重排器 2 条、挂上这个
    0 条）—— 界面上看起来一切正常，用户只看到"没搜到相关内容"。所以这条必须端到端测：
    单独测 `rerank()` 返回 None 只能证明函数自己变了，证明不了调用方真的回退了。
    """
    builder = KnowledgeBase(tmp_path / "chroma", HashEmbedder())
    builder.index("health_reports", "a.md", DOC_A)
    builder.index("health_reports", "b.md", DOC_B)
    plain = KnowledgeBase(tmp_path / "chroma", HashEmbedder())
    assert len(plain.search(["health_reports"], "复查频率", k=3)) == 2, "基线自己就不成立"

    drift = _reranker_body(_OneShotClient({}))  # 状态 200，body 里没有 results
    kb = KnowledgeBase(tmp_path / "chroma", HashEmbedder(), drift)
    hits = kb.search(["health_reports"], "复查频率", k=3)
    assert len(hits) == 2, f"一次响应形状漂移不该把检索打成空：{hits}"
