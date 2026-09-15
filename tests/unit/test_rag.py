"""v2.1 RAG 检索内核测试：切块 / 索引 / 作用域隔离 / search_knowledge 工具。

Traceability: US-8（知识作用域 —— 角色只声明，内核掌库）。

全部离线：HashEmbedder 确定性向量（真实嵌入只影响质量，不影响链路正确性）。
作用域安全模型的三条不变量：
  1. search_knowledge 的签名里没有 scope —— 模型无法指定检索哪个集合；
  2. 可检索范围 = 当前角色 knowledge_scopes 的并集（execute_tools 注入）；
  3. 未授权角色得到明确拒绝，而不是空结果或报错。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from rolecard_agent.config import Settings
from rolecard_agent.core.nodes import (
    role_knowledge_scopes_ctx,
)
from rolecard_agent.rag.retriever import (
    HashEmbedder,
    KnowledgeBase,
    chunk_text,
    make_embedder,
    make_search_tool,
)


@pytest.fixture
def kb(tmp_path: Path) -> KnowledgeBase:
    return KnowledgeBase(tmp_path / "chroma", HashEmbedder())


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


# -- embedder 选型 ----------------------------------------------------------------------


def test_make_embedder_auto_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-x")

    assert make_embedder(Settings()).name == "siliconflow"


def test_make_embedder_auto_without_key_is_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    assert make_embedder(Settings()).name == "hash"


def test_make_embedder_siliconflow_without_key_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置错误要大声失败：静默降级成质量很差的检索会让人误以为一切正常。"""
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SILICONFLOW_API_KEY"):
        make_embedder(Settings(embedding_backend="siliconflow"))


def test_make_embedder_unknown_backend_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="未知 embedding backend"):
        make_embedder(Settings(embedding_backend="warp-drive"))


# -- 集成：search_knowledge 在内核执行器里也拿到作用域 ---------------------------------


def test_execute_tools_injects_role_scopes_to_search_tool(
    kb: KnowledgeBase,
    tmp_path: Path,
    roles,  # noqa: ANN001 - conftest fixture
) -> None:
    """最小闭环：index → execute_tools 调 search_knowledge → 命中授权作用域内容。"""
    from langchain_core.messages import ToolMessage

    from rolecard_agent.core.nodes import KernelContext, execute_tools
    from rolecard_agent.core.observability import NullTracer
    from rolecard_agent.core.tools.registry import ToolRegistry
    from rolecard_agent.roles.models import RoleCardCreate

    kb.index("health_reports", "随访须知.md", DOC_A)
    reg = ToolRegistry()
    reg.register(make_search_tool(kb))
    roles.create(
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
