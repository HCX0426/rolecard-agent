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
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rolecard_agent.config import Settings
from rolecard_agent.core.observability import TraceEvent, timer

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

_CHUNK_SIZE = 500
_CHUNK_OVERLAP = 80
# 检索延迟滑动样本上限：只保留最近 N 次，进程内用于 P50/P95/P99 细分。
_LATENCY_CAP = 200
_LATENCY_STAGES = ("embed_ms", "vector_ms", "rerank_ms", "total_ms")


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """最近秩法（nearest-rank）分位数：对小样本稳定、无插值歧义。

    空样本返回 None —— "没有数据"与"0ms"是两回事，调用方（前端 / 运维）需要区分。
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return round(ordered[rank], 2)


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


def _find_embedding_key(settings: Settings) -> str | None:
    """找嵌入器 key：先查环境变量，再查 DB 合并后的后端配置（设置页存的 key）。

    用户在设置页填的 key 存 DB（effective_settings 已合并），但嵌入器此前只读
    环境变量 —— 两个 key 源不打通，导致上传 500（嵌入维度不匹配）。
    """
    key = os.environ.get("SILICONFLOW_API_KEY")
    if key:
        return key
    for b in settings.model_backends.values():
        if (b.provider or "").lower() == "openai" and b.api_key:
            return b.api_key
    return None


def make_embedder(
    settings: Settings,
    *,
    order: Sequence[str] | None = None,
    endpoints: Any = None,
) -> Embedder:
    """按 `Settings.embedding_backend` 构建嵌入器。

    `auto`：有 key → siliconflow（bge-m3），否则 hash（离线兜底）。
    key 来源：`SILICONFLOW_API_KEY` env → DB 后端配置（设置页存的）。
    显式指定 siliconflow 但没有任何 key → 启动即报错（配置错误要大声，不要静默降级成
    质量很差的检索还让人以为一切正常）。

    `order` + `endpoints`（「服务」页签的端点行，core/services.py）：给了就按序取第一个
    可用者 —— `hash` 恒可用；云端行按**行内** base_url/api_key/model 实例化（多云端实例
    各用各的 key）。`endpoints` 缺省时保留旧的字面 id 解析（env 取 key），兼容测试。
    """
    backend: str | None = settings.embedding_backend
    if order and endpoints:
        for cid in order:
            if cid == "hash":
                return HashEmbedder()
            cfg = endpoints.get(cid)
            if cfg is not None and cfg.api_key:
                return SiliconFlowEmbedder(
                    api_key=cfg.api_key,
                    base_url=cfg.base_url
                    or os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
                    model=cfg.model or "BAAI/bge-m3",
                )
        raise RuntimeError("服务策略里没有可用的嵌入端点（云端行需配置 API Key）。")
    if order:
        available = {"siliconflow": bool(_find_embedding_key(settings)), "hash": True}
        backend = next((c for c in order if available.get(c)), None)
        if backend is None:
            raise RuntimeError("服务策略把嵌入候选全部排除 —— 至少保留一个可用实现。")
    elif backend == "auto":
        backend = "siliconflow" if _find_embedding_key(settings) else "hash"
    if backend == "hash":
        return HashEmbedder()
    if backend == "chroma_default":
        return ChromaDefaultEmbedder()
    if backend == "siliconflow":
        key = _find_embedding_key(settings)
        if not key:
            raise RuntimeError("embedding_backend=siliconflow 需要 SILICONFLOW_API_KEY 环境变量。")
        return SiliconFlowEmbedder(
            api_key=key,
            base_url=os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        )
    raise RuntimeError(f"未知 embedding backend: {backend!r}")


class SiliconFlowReranker:
    """BAAI/bge-reranker-v2-m3 via SiliconFlow 的 /rerank 端点（非 OpenAI 兼容格式）。

    失败语义：调用失败**不抛出**——返回 None 让调用方回退到向量序，同时留痕。
    重排是质量增强，不能变成可用性故障。
    """

    name = "siliconflow"

    def __init__(
        self, *, api_key: str, base_url: str, model: str = "BAAI/bge-reranker-v2-m3"
    ) -> None:
        import httpx

        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        self._model = model

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]] | None:
        """返回 [(原索引, 相关性分数)] 按分数降序；失败返回 None（调用方回退向量序）。"""
        try:
            res = self._client.post(
                "/rerank",
                json={"model": self._model, "query": query, "documents": documents},
            )
            res.raise_for_status()
            results = res.json().get("results", [])
            return [(int(r["index"]), float(r["relevance_score"])) for r in results]
        except Exception:  # noqa: BLE001 - 重排失败 = 降级，不是故障
            return None


def make_reranker(
    settings: Settings,
    *,
    order: Sequence[str] | None = None,
    endpoints: Any = None,
) -> SiliconFlowReranker | None:
    """按 `Settings.rag_rerank` 构建重排器：off（默认，向量序足够）/ auto（有 key 即用）。

    `order` + `endpoints`（「服务」页签的端点行）：按序找第一个可产生重排器的条目 ——
    `off` 即关闭（返回 None）；云端行按**行内** key/base_url/model 实例化，没配 key 的行
    跳过（重排是质量增强，缺 key 不是故障）。`endpoints` 缺省时保留旧的字面 id 解析。
    """
    key = os.environ.get("SILICONFLOW_API_KEY")
    if order and endpoints:
        for cid in order:
            if cid == "off":
                return None
            cfg = endpoints.get(cid)
            if cfg is not None and cfg.api_key:
                return SiliconFlowReranker(
                    api_key=cfg.api_key,
                    base_url=cfg.base_url
                    or os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
                    model=cfg.model or "BAAI/bge-reranker-v2-m3",
                )
        return None
    if order:
        chosen = next((c for c in order if c in {"siliconflow", "off"}), None)
        if chosen == "siliconflow" and key:
            return SiliconFlowReranker(
                api_key=key,
                base_url=os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
            )
        return None
    backend = settings.rag_rerank
    if backend == "off":
        return None
    if backend == "auto":
        return (
            SiliconFlowReranker(
                api_key=key,
                base_url=os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
            )
            if key
            else None
        )
    if backend == "siliconflow":
        if not key:
            raise RuntimeError("rag_rerank=siliconflow 需要 SILICONFLOW_API_KEY 环境变量。")
        return SiliconFlowReranker(
            api_key=key,
            base_url=os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        )
    raise RuntimeError(f"未知 rag_rerank: {backend!r}")


# ---------------------------------------------------------------- 知识库


@dataclass(frozen=True, slots=True)
class Hit:
    scope: str
    source: str
    text: str
    distance: float


class KnowledgeDimensionMismatch(RuntimeError):
    """嵌入维度与已有集合不一致 —— 切换 RAG_EMBEDDING 后必须重建 chroma 目录。

    这类错误必须翻译成可操作的话：直接把 chroma 的原始异常抛给用户，他只会看到
    "Collection expecting embedding with dimension of 1024, got 64"，无从下手。
    """

    HINT = "嵌入后端与已有索引维度不一致。切换 RAG_EMBEDDING 后请删除 data/chroma 目录并重启重建。"


def _translate_dimension_error(exc: Exception) -> Exception:
    """把 chroma 的维度错误换成人类可读的 KnowledgeDimensionMismatch，其余原样抛。"""
    if "dimension" in str(exc).lower():
        return KnowledgeDimensionMismatch(KnowledgeDimensionMismatch.HINT)
    return exc


class KnowledgeBase:
    """Chroma 持久化知识库：一个作用域一个集合，库归内核、角色只声明作用域。

    `reranker` 可选（v2.1 后半）：提供时向量检索取 k×3 的池子再精排到 top-k；
    重排调用失败自动回退向量序（降级留痕由调用方的 tracer 记录）。
    """

    def __init__(
        self,
        chroma_path: Path,
        embedder: Embedder,
        reranker: SiliconFlowReranker | None = None,
    ) -> None:
        import chromadb

        self._client = chromadb.PersistentClient(path=str(chroma_path))
        self._embedder = embedder
        self._reranker = reranker
        # 检索延迟滑动样本（进程内）：每次 search 追加一条阶段耗时，供 P95 细分。
        self._samples: list[dict[str, float]] = []

    def index(self, scope: str, source: str, text: str) -> int:
        """切块 -> 嵌入 -> 入库（同 source 幂等重建）。返回入库的分块数。"""
        chunks = chunk_text(text)
        if not chunks:
            return 0
        collection = self._client.get_or_create_collection(name=scope)
        collection.delete(where={"source": source})  # 同源幂等重建
        vectors = self._embedder.embed(chunks)
        ids = [hashlib.md5(f"{source}:{i}".encode()).hexdigest()[:16] for i in range(len(chunks))]
        try:
            collection.add(
                ids=ids,
                embeddings=vectors,
                documents=chunks,
                metadatas=[
                    {"source": source, "scope": scope, "chunk": i} for i in range(len(chunks))
                ],
            )
        except Exception as exc:  # noqa: BLE001 - 维度错误要翻译成可操作提示
            raise _translate_dimension_error(exc) from exc
        return len(chunks)

    def search(
        self, scopes: Sequence[str], query: str, k: int = 4, *, tracer: object | None = None
    ) -> list[Hit]:
        """跨授权作用域检索：逐集合查询合并候选，有重排器则精排到 top-k。

        候选池取 max(k*3, 8) 条给重排足够空间。重排失败自动回退向量序（质量降级，
        留痕 `rerank_fallback`，不抛出 —— 重排是增强，不能变成可用性故障）。

        v2.2：对三个阶段计时（嵌入 / 向量检索 / 重排）+ 合计，写滑动样本供 P95 细分，
        并 emit `rag_search` 留痕（只含耗时与计数，不含文本，脱敏安全）。
        """
        if not scopes or not query.strip():
            return []
        with timer() as t_embed:
            vector = self._embedder.embed([query.strip()])[0]
        pool_size = max(k * 3, 8)
        with timer() as t_vec:
            hits: list[Hit] = []
            for scope in scopes:
                try:
                    collection = self._client.get_collection(name=scope)
                except Exception:  # noqa: BLE001 - 作用域尚无集合 = 没有知识，跳过而非报错
                    continue
                try:
                    found = collection.query(query_embeddings=[vector], n_results=pool_size)
                except Exception as exc:  # noqa: BLE001 - 维度错误翻译成可操作提示（搜索时抛出，由工具层兜住）
                    raise _translate_dimension_error(exc) from exc
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
        n_candidates = len(hits)

        reranked_flag = False
        with timer() as t_rerank:
            if self._reranker is not None and len(hits) > 1:
                reranked = self._reranker.rerank(query, [h.text for h in hits])
                if reranked is not None:
                    hits = [hits[i] for i, _ in reranked]
                    reranked_flag = True
                elif tracer is not None and hasattr(tracer, "emit"):
                    tracer.emit(
                        TraceEvent(event="rerank_fallback", detail={"reason": "rerank_failed"})
                    )
        result = hits[:k]

        total_ms = t_embed["ms"] + t_vec["ms"] + t_rerank["ms"]
        self._record_sample(
            embed_ms=t_embed["ms"],
            vector_ms=t_vec["ms"],
            rerank_ms=t_rerank["ms"],
            total_ms=total_ms,
        )
        if tracer is not None and hasattr(tracer, "emit"):
            tracer.emit(
                TraceEvent(
                    event="rag_search",
                    latency_ms=round(total_ms, 3),
                    detail={
                        "k": k,
                        "n_candidates": n_candidates,
                        "n_hits": len(result),
                        "reranked": reranked_flag,
                        "reranker": self._reranker.name if self._reranker else None,
                        "embed_ms": round(t_embed["ms"], 2),
                        "vector_ms": round(t_vec["ms"], 2),
                        "rerank_ms": round(t_rerank["ms"], 2),
                    },
                )
            )
        return result

    def _record_sample(
        self, *, embed_ms: float, vector_ms: float, rerank_ms: float, total_ms: float
    ) -> None:
        """追加一条延迟样本，并裁剪到 `_LATENCY_CAP`（滑动窗口）。"""
        self._samples.append(
            {
                "embed_ms": embed_ms,
                "vector_ms": vector_ms,
                "rerank_ms": rerank_ms,
                "total_ms": total_ms,
            }
        )
        if len(self._samples) > _LATENCY_CAP:
            del self._samples[: len(self._samples) - _LATENCY_CAP]

    def latency_p95(self) -> dict[str, object]:
        """检索延迟细分：P50/P95/P99，按阶段（嵌入 / 向量检索 / 重排 / 合计）。

        样本 = 最近 `_LATENCY_CAP` 次 search 的进程内滑动窗口；无样本时各分位为 None
        （"没有数据"不同于"0ms"）。`rerank_enabled` 反映当前是否挂了重排器——未挂时
        rerank_ms 恒为 0（阶段计时照常，便于对比开启前后的收益）。
        """
        out: dict[str, object] = {
            "samples": len(self._samples),
            "rerank_enabled": self._reranker is not None,
            "embedder": self._embedder.name,
        }
        for label, pct in (("p50", 50), ("p95", 95), ("p99", 99)):
            out[label] = {
                stage: _percentile([s[stage] for s in self._samples], pct)
                for stage in _LATENCY_STAGES
            }
        return out

    def scope_count(self, scope: str) -> int:
        """集合内分块数（集合不存在 = 0）。用于幂等判断，不抛错。"""
        try:
            return int(self._client.get_collection(name=scope).count())
        except Exception:  # noqa: BLE001 - 集合本就不存在
            return 0

    def describe(self) -> list[dict[str, object]]:
        """知识库概览：每个作用域的分块数与来源清单（设置页知识库管理视图）。"""
        out: list[dict[str, object]] = []
        for collection in self._client.list_collections():
            data = collection.get(include=["metadatas"])
            sources = sorted(
                {str((m or {}).get("source", "?")) for m in (data.get("metadatas") or [])}
            )
            out.append(
                {
                    "scope": collection.name,
                    "chunks": collection.count(),
                    "sources": sources,
                    "embedder": self._embedder.name,
                }
            )
        return out

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
        try:
            hits = kb.search(scopes, query, k=4)
        except KnowledgeDimensionMismatch as exc:
            # 维度不一致是管理员可修复的状态（重建索引），不该让整轮对话 500。
            return f"知识库暂不可用：{exc}"
        if not hits:
            return "知识库中没有找到与该问题相关的内容。"
        lines = [f"[{h.scope} · {h.source}] {h.text}" for h in hits]
        return "检索到的知识片段（按相关度排序）：\n" + "\n---\n".join(lines)

    return search_knowledge
