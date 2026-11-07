"""知识来源投影表的 SQL 半边 + 一次性种子（`R102-55` 根治）。

rag 半边（三个写入口的合同）由 test_rag.py 的内存替身钉；这里对**真 sqlite** 验
`KnowledgeSourceStore` 的语义，以及 `heal_knowledge_sources` 对**存量分块**的回填 ——
真实形状来自两个真根的直接核对：安装根的 `elysia_lore` 16 条没有 ingestion 台账、
dev 根那批旧上传的元数据连 `source_name` 键都没有（展示名要退回 `source`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rolecard_agent.core.bootstrap import heal_knowledge_sources
from rolecard_agent.core.ingest.knowledge_sources import KnowledgeSourceStore
from rolecard_agent.rag.retriever import HashEmbedder, KnowledgeBase
from rolecard_agent.storage.db import bootstrap, connect

DOC = "胆囊结石随访须知：每 3 到 6 个月复查一次超声。"


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "app.db")
    bootstrap(c, enabled_domains=())
    return c


def test_remember_updates_in_place_and_names_dedupes(conn) -> None:
    """同身份重传 = 改名（就地更新，不留孤儿行）；两个身份同名 = 展示面只出现一次。"""
    store = KnowledgeSourceStore(conn)
    store.remember("s", "k1", "报告A.pdf")
    store.remember("s", "k1", "报告A改.pdf")
    assert store.names("s") == ["报告A改.pdf"]
    store.remember("s", "k2", "报告A改.pdf")
    assert store.names("s") == ["报告A改.pdf"]
    assert store.names("另一个作用域") == []


def test_forget_and_forget_scope(conn) -> None:
    store = KnowledgeSourceStore(conn)
    store.remember("s", "k1", "a.pdf")
    store.remember("s", "k2", "b.pdf")
    store.remember("t", "k3", "c.pdf")
    store.forget("s", "k1")
    assert store.names("s") == ["b.pdf"]
    store.forget("s", "不存在的身份")  # 0 行也照常提交（R102-42 的纪律）
    store.forget_scope("s")
    assert store.names("s") == []
    assert store.names("t") == ["c.pdf"]  # 只清指名的那个作用域


def test_seed_backfills_existing_chunks_once(tmp_path: Path, conn) -> None:
    """种子把**存量**分块补进投影：新世界（有 source_name）与旧世界（连键都没有）同吃。"""
    kb = KnowledgeBase(tmp_path / "chroma", HashEmbedder())  # 裸建法 = 新表之前的世界
    kb.index("notes", "报告.txt", DOC)  # 旧世界形状：身份 = 文件名
    kb.index("elysia_lore", "01.md", DOC, source_name="01-基本设定与人设.md")
    # 更老的块：元数据里**没有** source_name 键（真机上就有一批）
    collection = kb._client.get_or_create_collection(name="notes")
    collection.add(
        ids=["legacy-1"],
        embeddings=[[0.0] * HashEmbedder.DIM],
        documents=[DOC],
        metadatas=[{"source": "旧文件.txt", "scope": "notes", "chunk": 0}],
    )

    assert heal_knowledge_sources(kb, conn) == 3
    store = KnowledgeSourceStore(conn)
    assert store.names("notes") == ["报告.txt", "旧文件.txt"]
    assert store.names("elysia_lore") == ["01-基本设定与人设.md"]

    # 幂等：投影已有名字就不再扫（第二趟 0 行）
    assert heal_knowledge_sources(kb, conn) == 0


def test_seed_clears_projection_for_empty_scopes(tmp_path: Path, conn) -> None:
    """空集合反过来清投影残留（自愈）：分块全没了，名单不该还挂着一个名字。"""
    store = KnowledgeSourceStore(conn)
    store.remember("notes", "k", "ghost.pdf")
    kb = KnowledgeBase(tmp_path / "chroma", HashEmbedder())
    kb._client.get_or_create_collection(name="notes")  # 集合在、分块为 0

    assert heal_knowledge_sources(kb, conn) == 0
    assert store.names("notes") == []


def test_seed_is_a_noop_on_fully_projected_state(tmp_path: Path, conn) -> None:
    """产品路径已经写过的投影：种子一行不动（不会把旧读法重新带回来）。"""
    store = KnowledgeSourceStore(conn)
    kb = KnowledgeBase(tmp_path / "chroma", HashEmbedder(), sources_store=store)
    kb.index("notes", "task-1", DOC, source_name="报告.pdf")

    assert heal_knowledge_sources(kb, conn) == 0
    assert store.names("notes") == ["报告.pdf"]


def test_keys_by_name_resolves_identity_for_delete(conn) -> None:
    """按展示名删文档的下刀口：名字 → 全部索引身份。重名两份都列出来，删才不漏。

    `delete_document` 靠它把概览给的展示名对上分块身份（身份才是删除的靶子）。
    """
    store = KnowledgeSourceStore(conn)
    store.remember("s", "k1", "报告.pdf")
    store.remember("s", "k2", "报告.pdf")  # 同名不同身份（两份"报告.pdf"）
    store.remember("s", "k3", "别的.pdf")
    store.remember("t", "k9", "报告.pdf")  # 别的作用域同名，不串味

    assert store.keys_by_name("s", "报告.pdf") == ["k1", "k2"]
    assert store.keys_by_name("s", "别的.pdf") == ["k3"]
    assert store.keys_by_name("s", "没有的名字") == []
    assert store.keys_by_name("空作用域", "报告.pdf") == []

    # 删掉身份后，名字解析跟着收（投影与身份同批行，不另开事实源）。
    store.forget("s", "k1")
    assert store.keys_by_name("s", "报告.pdf") == ["k2"]
