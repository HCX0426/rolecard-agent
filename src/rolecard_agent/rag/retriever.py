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
import threading
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


class EmbedError(RuntimeError):
    """嵌入请求失败（分批重试之后仍然失败）。携带可读原因，由接入层翻译成 500。"""


# 单次嵌入请求的条数上限与重试次数（审查报告 M5）。
# 为什么必须分批：一篇长报告的 chunk 数可以上百，一次 POST 全部文本会顶到 httpx 超时，
# 结果是"整个上传以解析失败告终"，而失败原因与文档质量毫无关系。
_EMBED_BATCH = 64
_EMBED_RETRIES = 2


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
        """分批 + 有限重试。任一批失败即整体失败（宁可不入库，也不要半份索引）。"""
        out: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH):
            out.extend(self._embed_chunk(texts[start : start + _EMBED_BATCH]))
        return out

    def _embed_chunk(self, texts: list[str]) -> list[list[float]]:
        import time

        last: Exception | None = None
        for attempt in range(_EMBED_RETRIES + 1):
            try:
                res = self._client.post("/embeddings", json={"model": self._model, "input": texts})
                res.raise_for_status()
                payload = res.json()
                return [item["embedding"] for item in payload["data"]]
            except Exception as exc:  # noqa: BLE001 - 网络/HTTP/解析失败一视同仁地重试
                last = exc
                if attempt < _EMBED_RETRIES:
                    time.sleep(0.5 * (2**attempt))
        raise EmbedError(
            f"嵌入请求失败（{len(texts)} 条文本，重试 {_EMBED_RETRIES} 次后仍失败）：{last}"
        )


    def close(self) -> None:
        """M1：释放 httpx.Client 连接（rebuild 旧实例此前从不关闭，累积 fd/连接泄漏）。"""
        with contextlib.suppress(Exception):
            self._client.close()


def _find_embedding_key(settings: Settings) -> str | None:
    """嵌入 key 的**遗留 env-auto 路径**：只读环境变量。

    架构归一化后（服务引用化），应用的正式路径是「服务端点行 → 引用的 model_backend」，
    key 由行配置携带；本函数仅供 `order=None` 的 env-auto 老路径（及测试）使用。
    此前的 model_backend 兜底已删除 —— 一把 key 不该同时活在三处。
    """
    return os.environ.get("SILICONFLOW_API_KEY")


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


    def close(self) -> None:
        """M1：释放 httpx.Client 连接。"""
        with contextlib.suppress(Exception):
            self._client.close()


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


#: 相对尾部裁剪的比例：最终结果里只保留距离不超过「最优命中」这个倍数的候选。
#:
#: 为什么用**相对**而不是绝对阈值（审查报告 P1-6）：绝对阈值必须按嵌入器标定，
#: 而实测离线默认的 HashEmbedder 下「复查频率是多少」对正确文档只有 0.163、
#: 完全无关的「编程语言排行榜」却有 0.358 —— 固定阈值会砍掉正确结果、留下噪音。
#: 相对裁剪不需要任何标定：当 1 条明显最优、其余是陪跑时，把陪跑剪掉。
RAG_TAIL_RATIO = 3.0


def _collection_space(collection: object) -> str:
    """集合的向量度量。chroma 1.x 在 `configuration.hnsw.space`，旧版在 metadata。"""
    config = getattr(collection, "configuration", None) or {}
    hnsw = config.get("hnsw") if isinstance(config, dict) else None
    if isinstance(hnsw, dict) and hnsw.get("space"):
        return str(hnsw["space"])
    meta = getattr(collection, "metadata", None) or {}
    return str(meta.get("hnsw:space", "l2"))


def _similarity(space: str, distance: float) -> float | None:
    """把 chroma 的距离换算成**余弦相似度**（越大越相关）；换算不了返回 None。

    cosine / ip：单位向量下 d = 1 - cos → cos = 1 - d。
    l2：只有**单位向量**才成立 cos = 1 - d²/2 —— 而云端嵌入器（bge-m3）不保证归一化
    （实测 SiliconFlowEmbedder 直接返回原始向量），所以 l2 一律返回 None：
    宁可放弃绝对过滤，也不要基于错误前提去砍检索结果。
    """
    if space in ("cosine", "ip"):
        return 1.0 - distance
    return None


def _apply_relevance_floor(result: list[Hit], min_similarity: float) -> list[Hit]:
    """对最终 top-k 做相关性收敛，返回过滤后的列表。

    两步，顺序固定：
      1. **绝对下限**（只在算得出余弦相似度时生效）：低于 floor 的直接丢；
      2. **相对尾部裁剪**：距离超过「最优命中」× `RAG_TAIL_RATIO` 的丢掉 ——
         度量无关，所以对 l2 老集合也安全。最优距离为 0（完全命中）时不做裁剪：
         那种情况下"谁更近"已经没有分辨力，硬按 0 的倍数裁会只剩完全相同的那条。
    """
    kept = result
    if min_similarity > 0:
        kept = [h for h in kept if h.similarity is None or h.similarity >= min_similarity]
    if len(kept) > 1:
        best = min(h.distance for h in kept)
        if best > 0:
            cutoff = best * RAG_TAIL_RATIO
            kept = [h for h in kept if h.distance <= cutoff]
    return kept


@dataclass(frozen=True, slots=True)
class Hit:
    scope: str
    #: 给人看的来源名（用户上传的原始文件名）。工具层只展示它。
    source: str
    text: str
    distance: float
    #: 索引身份（同 key 幂等重建）。展示层不需要，但排障时要能区分同名文件。
    source_key: str = ""
    #: 余弦相似度（越大越相关）；度量换算不出来时为 None（见 `_similarity`）。
    similarity: float | None = None


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


def _chunk_ids(source: str, n: int) -> list[str]:
    """分块 id：由 (source, 序号) 确定性推导 —— 同 source 重索引得到同一批 id，因此 upsert
    天然幂等。故意不含内容 hash：内容变了也应该**替换**同一位置的分块，而不是留下两份。
    """
    return [hashlib.md5(f"{source}:{i}".encode()).hexdigest()[:16] for i in range(n)]


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
        min_similarity: float = 0.0,
    ) -> None:
        import chromadb

        self._client = chromadb.PersistentClient(path=str(chroma_path))
        self._embedder = embedder
        self._reranker = reranker
        # 绝对相似度下限（0 = 不过滤）。只在能算出余弦相似度的集合上生效 ——
        # 默认 0 是因为阈值要按嵌入器标定，见 Settings.rag_min_similarity 的实测说明。
        self._min_similarity = min_similarity
        # 检索延迟滑动样本（进程内）：每次 search 追加一条阶段耗时，供 P95 细分。
        # 加锁：KnowledgeBase 是**跨线程共享**的（FastAPI 线程池 + 图执行），旧实现的
        # append + 切片裁剪在并发下会与 latency_p95() 的读取互相踩（审查报告 L2）。
        self._samples: list[dict[str, float]] = []
        self._samples_lock = threading.Lock()

    def close(self) -> None:
        """M1：释放嵌入/重排器持有的 httpx 连接（rebuild_runtime 换装旧实例时调用）。"""
        for obj in (self._embedder, self._reranker):
            if obj is not None and hasattr(obj, "close"):
                with contextlib.suppress(Exception):
                    obj.close()

    def _collection_for_write(self, scope: str) -> Any:
        """写索引用的集合：不存在时**按 cosine 度量新建**。

        chroma 的默认度量是 l2，而 l2 距离只有在「向量已归一化」时才等价于余弦距离
        （见 `_similarity`）—— 于是绝对阈值对 l2 集合只能关闭。新建集合用 cosine，
        `RAG_MIN_SIMILARITY` 才有意义。已存在的集合**不动**：改度量等于要重建索引，
        想升级就删掉 data/chroma 或走「重建作用域」再重传（README 有说明）。
        """
        try:
            return self._client.get_collection(name=scope)
        except Exception:  # noqa: BLE001 - 不存在 → 下面新建
            pass
        for kwargs in (
            {"configuration": {"hnsw": {"space": "cosine"}}},  # chroma 1.x
            {"metadata": {"hnsw:space": "cosine"}},  # 旧版写法
        ):
            try:
                return self._client.create_collection(name=scope, **kwargs)
            except Exception:  # noqa: BLE001 - 换一种写法再试；都失败就退回 get（会抛）
                continue
        return self._client.get_collection(name=scope)

    def index(self, scope: str, source: str, text: str, *, source_name: str | None = None) -> int:
        """切块 -> 嵌入 -> 入库（同 source 幂等重建）。返回入库的分块数。

        **`source` 是索引身份，`source_name` 只是展示名** —— 这个区分是审查报告 P0 的核心：
        分块 id 由 `source` 确定性推导、旧分块也按 `source` 清理，所以身份一旦与别的文档
        重合，后写入的会静默覆盖先前的。上传路径**曾用原始文件名当身份**：两个「报告.pdf」
        互相吃掉，旧文档的索引永久消失且没有任何提示。
        身份请传唯一且稳定的东西（上传路径传 ingestion task id，它按内容 hash 去重，
        恰好满足"同一份字节重建、不同内容彼此独立"）。展示名进元数据，不参与身份。

        ## 步骤顺序是刻意的（审查报告 M4）

        旧实现是 `delete(source)` → `embed()` → `add()`：`embed()` 失败时该来源的既有分块
        已经被删掉 —— 检索里凭空少一份文档，而 ingestion 台账那边还写着 `indexed`。
        这是"状态与事实背离"，比单纯报错难查得多。

        现在的顺序：**先嵌入**（最可能失败的一步，失败则库完全没动）→ `upsert` 新分块
        （同 id 覆盖）→ 删掉本次不再出现的旧 id（文档变短时清理残留）。
        任何一步失败都不会让既有索引消失。
        """
        chunks = chunk_text(text)
        if not chunks:
            # 空文本 = 这份文档现在没有内容了，必须把它的旧分块**清掉**：直接 return 0
            # 会让上一次的索引继续被检索到（"同一份文件重传成空"是真实场景：扫描件、
            # 内容被清空的文件、解析退化的 PDF），幂等契约当场被打破（审查报告 P1-7）。
            self.delete_source(scope, source)
            return 0
        collection = self._collection_for_write(scope)
        vectors = self._embedder.embed(chunks)  # 先做最容易失败的一步
        ids = _chunk_ids(source, len(chunks))
        try:
            existing = collection.get(where={"source": source})
            stale = [str(i) for i in (existing.get("ids") or [])]
        except Exception:  # noqa: BLE001 - 取不到旧 id 只是少一次清理，不该让入库失败
            stale = []
        try:
            # 注：集合对象在类型上收敛成 Any（见 _collection_for_write）——
            # chroma 的存根要求 numpy ndarray 的具体 dtype，而"嵌套 float 列表"在运行期
            # 完全被接受，也是 chroma 自己的文档示例写法。存根比实现更严，不改数据形状。
            collection.upsert(
                ids=ids,
                embeddings=vectors,
                documents=chunks,
                metadatas=[
                    {
                        "source": source,
                        "source_name": source_name or source,
                        "scope": scope,
                        "chunk": i,
                    }
                    for i in range(len(chunks))
                ],
            )
        except Exception as exc:  # noqa: BLE001 - 维度错误要翻译成可操作提示
            raise _translate_dimension_error(exc) from exc
        outdated = sorted(set(stale) - set(ids))
        if outdated:
            with contextlib.suppress(Exception):
                collection.delete(ids=outdated)
        return len(chunks)

    def delete_source(self, scope: str, source: str) -> int:
        """按**索引身份**删除某来源的全部分块，返回删除条数（集合/来源不存在 = 0）。

        为什么需要它：报告行在 SQLite、检索分块在 chroma —— 删除业务数据时必须两边
        一起清，否则"已经删掉"的病历原文仍会被模型检索到并引用（审查报告 P1-1）。
        `reset_scope` 太狠（会连带清掉同作用域里别的文档），所以要有按来源的粒度。

        与 `index()` 里的清理不同，这里的删除**不吞异常**：调用方（删除报告）已经先把
        SQLite 行留着，删索引失败就该整个失败、让用户重试，而不是留下"以为删了"的状态。
        """
        try:
            collection = self._client.get_collection(name=scope)
        except Exception:  # noqa: BLE001 - 集合不存在 = 没有东西可删
            return 0
        try:
            existing = collection.get(where={"source": source})
        except Exception:  # noqa: BLE001 - 取不到就当没有（与 index() 的 stale 探测同策略）
            return 0
        ids = [str(i) for i in (existing.get("ids") or [])]
        if not ids:
            return 0
        collection.delete(ids=ids)
        return len(ids)

    def search(
        self, scopes: Sequence[str], query: str, k: int = 4, *, tracer: object | None = None
    ) -> list[Hit]:
        """跨授权作用域检索：逐集合查询合并候选，有重排器则精排到 top-k。

        候选池取 max(k*3, 8) 条给重排足够空间。重排失败自动回退向量序（质量降级，
        留痕 `rerank_fallback`，不抛出 —— 重排是增强，不能变成可用性故障）。

        v2.2：对三个阶段计时（嵌入 / 向量检索 / 重排）+ 合计，写滑动样本供 P95 细分，
        并 emit `rag_search` 留痕（只含耗时与计数，不含文本，脱敏安全）。

        v2.5 相关性收敛（审查报告 P1-6）：最终结果先过**绝对下限**（仅 cosine/ip 度量，
        见 `_similarity`），再做**相对尾部裁剪**（`RAG_TAIL_RATIO`）。两者都不会让
        "没有相关内容"变成"返回几条凑数片段" —— 空结果由工具层如实回答。
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
                    found = collection.query(
                        # 同上：chroma 存根要求 numpy dtype，运行期接受嵌套 float 列表。
                        query_embeddings=[vector],  # type: ignore[arg-type]
                        n_results=pool_size,
                    )
                except Exception as exc:  # noqa: BLE001 - 维度错误翻译成可操作提示（搜索时抛出，由工具层兜住）
                    raise _translate_dimension_error(exc) from exc
                docs = (found.get("documents") or [[]])[0]
                metas = (found.get("metadatas") or [[]])[0]
                dists = (found.get("distances") or [[]])[0]
                space = _collection_space(collection)
                for doc, meta, dist in zip(docs, metas, dists, strict=True):
                    key = str((meta or {}).get("source", "?"))
                    hits.append(
                        Hit(
                            scope=scope,
                            # 展示名：新写入的带 source_name；本次改动前入库的分块没有这个
                            # 字段，退回 source 本身 —— 显示效果与改动前完全一致。
                            source=str((meta or {}).get("source_name") or key),
                            text=doc,
                            distance=float(dist),
                            source_key=key,
                            similarity=_similarity(space, float(dist)),
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
        result = _apply_relevance_floor(hits[:k], self._min_similarity)

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
        """追加一条延迟样本，并裁剪到 `_LATENCY_CAP`（滑动窗口）。加锁见 __init__ 的说明。"""
        with self._samples_lock:
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
        with self._samples_lock:
            snapshot = list(self._samples)
        out: dict[str, object] = {
            "samples": len(snapshot),
            "rerank_enabled": self._reranker is not None,
            "embedder": self._embedder.name,
        }
        for label, pct in (("p50", 50), ("p95", 95), ("p99", 99)):
            out[label] = {
                stage: _percentile([s[stage] for s in snapshot], pct)
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
                {
                    str((m or {}).get("source_name") or (m or {}).get("source", "?"))
                    for m in (data.get("metadatas") or [])
                }
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
        """删除整个作用域集合（embedder 切换后维度不兼容时的重建入口）。

        删除失败**必须抛出**：吞掉异常会让调用方以为清空了（审计都写了），而集合还在
        —— "以为删了"的状态比失败难查得多（审查报告 P2）。
        """
        self._client.delete_collection(name=scope)


# ---------------------------------------------------------------- 内核工具


def make_search_tool(kb: KnowledgeBase, *, tracer: object | None = None) -> BaseTool:
    """构建 search_knowledge —— 检索是**内核能力**（v2.1），所有领域共享。

    作用域安全模型（US-8）：工具签名里没有 scope —— 可检索范围由内核在调用瞬间
    注入（execute_tools 从当前角色读取 knowledge_scopes）。未授权角色得到明确拒绝，
    模型无法通过构造参数越权检索任何集合。

    `tracer` 必须由宿主传进来（审查报告 M3）：`search()` 里的 `rag_search` 与
    `rerank_fallback` 两个事件都要求 `tracer is not None`，而工具是**唯一**的生产调用点。
    旧实现没传，于是"重排失败已降级回向量序"这件事在日志里完全不存在 —— 检索质量变差
    时无法归因。
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
            hits = kb.search(scopes, query, k=4, tracer=tracer)
        except KnowledgeDimensionMismatch as exc:
            # 维度不一致是管理员可修复的状态（重建索引），不该让整轮对话 500。
            return f"知识库暂不可用：{exc}"
        if not hits:
            return "知识库中没有找到与该问题相关的内容。"
        lines = [f"[{h.scope} · {h.source}] {h.text}" for h in hits]
        return "检索到的知识片段（按相关度排序）：\n" + "\n---\n".join(lines)

    return search_knowledge
