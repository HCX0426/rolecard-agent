"""Chunk -> embed -> Chroma -> similarity search (v2.1 RAG).

Deliberately one module, not an abstraction layer. Exposed to the agent as the
`search_knowledge` kernel tool; every domain shares it via the scope (collection) argument.

Design decisions worth stating:

  * **角色只声明作用域，库归内核（US-8）**：`search_knowledge(query)` 没有 scope 参数 ——
    模型永远不能指定去哪个集合检索；可检索范围 = 当前角色 `knowledge_scopes` 的并集，
    由内核在调用工具时注入。未授权角色的检索请求直接得到明确拒绝。
  * **Embedder 可插拔**（`make_embedder` + `Settings.embedding_backend`）：
    - `siliconflow`：BAAI/bge-m3（中文效果好，走已有 OpenAI 兼容端点，需 key）；
    - `chroma_default`：chromadb 自带 ONNX MiniLM（离线，首次使用需下载模型，中文偏弱）；
    - `hash`：确定性字符 n-gram 哈希向量（离线、零依赖、**仅供测试与链路跑通**，检索质量有限）；
    - `auto`（默认）：有 SILICONFLOW_API_KEY → siliconflow，否则 hash。
    注意：不同 embedder 维度不同，切换后需要重建集合（v2.1 用小语料，直接删除 chroma 目录重建）。
  * **embedding_backend 目前读 env（RAG_EMBEDDING）而非 Settings 契约** —— v2.1 第一个增量
    刻意保持 rag 自包含；若后续要进设置页，再正式升格为 Settings 字段（那时一起改 .env.example）。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rolecard_agent.config import Settings

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

_CHUNK_SIZE = 500
_CHUNK_OVERLAP = 80


def chunk_text(text: str, *, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """按段落优先、长度兜底切块。空段落丢弃；重叠保证跨块语义不断裂。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(para) > size:  # 超长段落硬切（带重叠）
            for i in range(0, len(para), size - overlap):
                chunks.append(para[i : i + size])
            buf = ""
            continue
        if buf and len(buf) + len(para) + 2 > size:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf.strip():
        chunks.append(buf)
    return chunks


# ---------------------------------------------------------------- embedders


class Embedder:
    """Common interface: batch text -> vectors. Never raises for empty input."""

    name: str = "base"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._embed_batch(texts)

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError


class HashEmbedder(Embedder):
    """确定性哈希向量：离线、零依赖、可复现 —— 质量有限，仅供测试与链路跑通。

    64 维字符 3-gram 词袋哈希。它的存在让"没配任何云 key 的干净环境"也能跑通
    RAG 全链路并测试之；真实检索质量请用 siliconflow（bge-m3）。
    """

    name = "hash"
    DIM = 64

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.DIM
            cleaned = re.sub(r"\s+", "", text)
            for i in range(max(0, len(cleaned) - 2)):
                gram = cleaned[i : i + 3]
                h = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:8], 16)
                vec[h % self.DIM] += 1.0
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out


class ChromaDefaultEmbedder(Embedder):
    """chromadb 自带 ONNX MiniLM：离线可用；首次使用会下载模型文件；中文效果一般。"""

    name = "chroma_default"

    def __init__(self) -> None:
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        self._fn = ONNXMiniLM_L6_V2()

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._fn(texts)]


class SiliconFlowEmbedder(Embedder):
    """BAAI/bge-m3 via SiliconFlow 的 OpenAI 兼容 /embeddings 端点（中文效果好）。"""

    name = "siliconflow"

    def __init__(self, *, api_key: str, base_url: str, model: str = "BAAI/bge-m3") -> None:
        import httpx

        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        self._model = model

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        res = self._client.post("/embeddings", json={"model": self._model, "input": texts})
        res.raise_for_status()
        payload = res.json()
        return [item["embedding"] for item in payload["data"]]


def make_embedder(settings: Settings) -> Embedder:
    """按 `Settings.embedding_backend` 构建嵌入器。

    `auto`：有 SILICONFLOW_API_KEY → siliconflow（bge-m3），否则 hash（离线兜底）。
    显式指定 siliconflow 但没有 key → 启动即报错（配置错误要大声，不要静默降级成
    质量很差的检索还让人以为一切正常）。
    """
    backend = settings.embedding_backend
    if backend == "auto":
        backend = "siliconflow" if os.environ.get("SILICONFLOW_API_KEY") else "hash"
    if backend == "hash":
        return HashEmbedder()
    if backend == "chroma_default":
        return ChromaDefaultEmbedder()
    if backend == "siliconflow":
        key = os.environ.get("SILICONFLOW_API_KEY")
        if not key:
            raise RuntimeError("embedding_backend=siliconflow 需要 SILICONFLOW_API_KEY 环境变量。")
        return SiliconFlowEmbedder(
            api_key=key,
            base_url=os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        )
    raise RuntimeError(f"未知 embedding backend: {backend!r}")


# ---------------------------------------------------------------- 知识库


@dataclass(frozen=True, slots=True)
class Hit:
    scope: str
    source: str
    text: str
    distance: float


class KnowledgeBase:
    """Chroma 持久化知识库：一个作用域一个集合，库归内核、角色只声明作用域。"""

    def __init__(self, chroma_path: Path, embedder: Embedder) -> None:
        import chromadb

        self._client = chromadb.PersistentClient(path=str(chroma_path))
        self._embedder = embedder

    def index(self, scope: str, source: str, text: str) -> int:
        """切块 -> 嵌入 -> 入库（同 source 幂等重建）。返回入库的分块数。"""
        chunks = chunk_text(text)
        if not chunks:
            return 0
        collection = self._client.get_or_create_collection(name=scope)
        collection.delete(where={"source": source})  # 同源幂等重建
        vectors = self._embedder.embed(chunks)
        ids = [hashlib.md5(f"{source}:{i}".encode()).hexdigest()[:16] for i in range(len(chunks))]
        collection.add(
            ids=ids,
            embeddings=vectors,
            documents=chunks,
            metadatas=[{"source": source, "scope": scope, "chunk": i} for i in range(len(chunks))],
        )
        return len(chunks)

    def search(self, scopes: Sequence[str], query: str, k: int = 4) -> list[Hit]:
        """跨授权作用域检索：逐集合查询后按距离合并取 top-k。集合不存在 = 该作用域还没有知识。"""
        if not scopes or not query.strip():
            return []
        vector = self._embedder.embed([query.strip()])[0]
        hits: list[Hit] = []
        for scope in scopes:
            try:
                collection = self._client.get_collection(name=scope)
            except Exception:  # noqa: BLE001 - 作用域尚无集合 = 没有知识，跳过而非报错
                continue
            found = collection.query(query_embeddings=[vector], n_results=k)
            docs = (found.get("documents") or [[]])[0]
            metas = (found.get("metadatas") or [[]])[0]
            dists = (found.get("distances") or [[]])[0]
            for doc, meta, dist in zip(docs, metas, dists, strict=True):
                hits.append(
                    Hit(
                        scope=scope,
                        source=str((meta or {}).get("source", "?")),
                        text=doc,
                        distance=float(dist),
                    )
                )
        hits.sort(key=lambda h: h.distance)
        return hits[:k]

    def scope_count(self, scope: str) -> int:
        """集合内分块数（集合不存在 = 0）。用于幂等判断，不抛错。"""
        try:
            return int(self._client.get_collection(name=scope).count())
        except Exception:  # noqa: BLE001 - 集合本就不存在
            return 0

    def reset_scope(self, scope: str) -> None:
        """删除整个作用域集合（embedder 切换后维度不兼容时的重建入口）。"""
        with contextlib.suppress(Exception):
            self._client.delete_collection(name=scope)


# ---------------------------------------------------------------- 内核工具


def make_search_tool(kb: KnowledgeBase) -> BaseTool:
    """构建 search_knowledge —— 检索是**内核能力**（v2.1），所有领域共享。

    作用域安全模型（US-8）：工具签名里没有 scope —— 可检索范围由内核在调用瞬间
    注入（execute_tools 从当前角色读取 knowledge_scopes）。未授权角色得到明确拒绝，
    模型无法通过构造参数越权检索任何集合。
    """
    from langchain_core.tools import tool

    from rolecard_agent.core.nodes import current_knowledge_scopes

    @tool("search_knowledge")
    def search_knowledge(query: str) -> str:
        """在当前角色已授权的知识作用域内检索知识库，返回最相关的文档片段（含来源）。

        适用于用户询问知识库中可能记录过的知识、说明、背景信息时。作用域由管理员在
        角色卡中配置；未授权时本工具会明确说明，不要重试或编造。
        """
        scopes = tuple(current_knowledge_scopes())
        if not scopes:
            return (
                "当前角色未授权任何知识作用域，无法检索。"
                "请联系管理员在角色卡中声明 knowledge_scopes。"
            )
        hits = kb.search(scopes, query, k=4)
        if not hits:
            return "知识库中没有找到与该问题相关的内容。"
        lines = [f"[{h.scope} · {h.source}] {h.text}" for h in hits]
        return "检索到的知识片段（按相关度排序）：\n" + "\n---\n".join(lines)

    return search_knowledge
