"""RAG 索引一趟的 Python 峰值内存与墙钟（快照 P3-3「一次性 embed 全量驻留」的判据留档）。

背景：审查快照写着"RAG 大文件一次性 embed 全量驻留内存、串行分批 → 按批流水线化"。
这条尺子的规矩（P3-5 同款）是**先测量再动手，测量可以否决修法**。本探针把现值钉死：

  * 同一份 `index()` 调用里，Python 侧峰值分配（tracemalloc）按文档规模（分块数）的曲线；
  * 墙钟；
  * 峰值里的两半各是什么：向量（分块数 × 维度 × 一个 float 对象）与文本（分块本体）。

嵌入器用**假的高维确定性向量**（bge-m3 的 1024 维，值从 md5 流里造）—— 量的就是
分配形状本身，不碰网络、不碰真模型；真云端嵌入的返回形状相同（list[list[float]]）。

跑法（离线）：

    .venv/Scripts/python.exe scripts/forensics/probe_rag_index_memory.py \
        --out build/scratch/rag_index_mem.json

结果只写文件，stdout 保持 ASCII（这台机器的控制台是 GBK，中文经它必乱）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from rolecard_agent.rag.retriever import Embedder, KnowledgeBase, chunk_text  # noqa: E402

#: bge-m3 的维度（SiliconFlow 云端行的真实形状；64 维的 hash 行会把峰值虚降一个量级）。
DIM = 1024


class FakeBgeEmbedder(Embedder):
    """确定性 1024 维假向量：形状与云端返回一致（list[list[float]]），不碰网络。"""

    name = "fake_bge1024"

    def _embed_batch(self, texts):  # noqa: D102
        import hashlib

        out = []
        for t in texts:
            seed = hashlib.md5(t.encode("utf-8")).digest()
            vec = []
            for i in range(DIM):
                b = seed[i % 16] + i
                vec.append(((b % 251) - 125) / 125.0)
            out.append(vec)
        return out


def make_doc(paragraphs: int) -> str:
    """`paragraphs` 个 ~160 字段落。`chunk_text` 按 500 字打包，实测约**三段一章**
    （150 段 → 50 章）—— 报实际分块数，不按段落数硬算（那会把读数说错三倍）。"""
    filler = "病历记录与随访说明的文字内容，包含指标、日期与医生签名等段落要素。"
    one = filler * 4 + "（编号占位）"
    return "\n\n".join(f"{one} 第 {i} 段 p{i:05d}" for i in range(paragraphs))


def measure_kb(paragraphs: int, chroma_dir: Path, label: str) -> dict:
    """一趟 `index()` 的峰值与墙钟。**不预设分块数**：chunk_text 会把短段落打包，
    段落数与分块数不是 1:1 —— 报实际分块数才是诚实读数。"""
    kb = KnowledgeBase(chroma_dir / f"c_{label}", FakeBgeEmbedder())
    try:
        text = make_doc(paragraphs)
        n_chunks = len(chunk_text(text))
        tracemalloc.start()
        t0 = time.perf_counter()
        n = kb.index(f"scope_{label}", f"src_{label}", text, source_name="probe.pdf")
        peak_cur, peak_traced = tracemalloc.get_traced_memory()
        wall = time.perf_counter() - t0
        tracemalloc.stop()
        assert n == n_chunks, (n, n_chunks)
        return {
            "paragraphs": paragraphs,
            "chunks": n,
            "peak_mb": round(peak_traced / 2**20, 2),
            "wall_s": round(wall, 3),
        }
    finally:
        kb.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="build/scratch/rag_index_mem.json")
    ap.add_argument("--paragraphs", default="150,600,1800,6000")
    args = ap.parse_args()

    sizes = [int(x) for x in args.paragraphs.split(",")]
    report: dict = {
        "subject": "KnowledgeBase.index() 的 Python 侧峰值分配（假 1024 维嵌入器，离线）",
        "dim": DIM,
        "float_object_bytes_note": "list[list[float]] 每 float 一个 PyFloat 对象(24B)+指针槽(8B)",
        "runs": [],
    }
    with tempfile.TemporaryDirectory(prefix="ragmem_") as td:
        for n in sizes:
            r = measure_kb(n, Path(td), f"p{n}")
            r["vector_mb_predicted"] = round(r["chunks"] * DIM * 32 / 2**20, 1)
            report["runs"].append(r)
            print(
                f"paras={n:<6} chunks={r['chunks']:<6} peak={r['peak_mb']:>8} MB  "
                f"wall={r['wall_s']:>6} s  pred_vectors={r['vector_mb_predicted']} MB"
            )

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"written -> {out_path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
