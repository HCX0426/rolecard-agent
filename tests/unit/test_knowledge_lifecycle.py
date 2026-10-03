"""`R102-74`：`KnowledgeBase.close()` 必须把 chroma 那一格 system 一起放掉。

为什么这条值得一个用例（而不是一句注释）：chroma 的本地客户端把 system 存在**进程级注册表**
`SharedSystemClient._identifier_to_system` 里（键是 `persist_directory`），实测

  - `del 客户端 + gc.collect()` → 注册表**仍是 1**（对象被表钉住，永远不会被回收）；
  - `Client.close()` → 归零。

而装配根每次热重建（保存模型设置、换嵌入器、改「运行环境」）都会 `old_knowledge.close()`
换新实例 —— 从前那一句只关了 httpx，于是桌面版开着用几个小时，进程里就堆了几十格 system，
每格带着自己的 sqlite 句柄与后台建索引线程池。更要紧的是：`R102-41` 那记
`Nothing found on disk` 的复现形状，探针实测就是"**同一进程里并存多格 system**"。

两条判据各挡一种坏法：正向（close 之后注册表回到基线）、反向（不 close 就会一格一格涨 ——
证明这条判据不是恒真）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rolecard_agent.rag.retriever import HashEmbedder, KnowledgeBase

pytest.importorskip("chromadb")

from chromadb.api.client import SharedSystemClient  # noqa: E402

REGISTRY = SharedSystemClient._identifier_to_system


def _make(tmp_path: Path, tag: str) -> KnowledgeBase:
    return KnowledgeBase(tmp_path / tag, HashEmbedder())


def test_close_releases_the_chroma_system(tmp_path: Path) -> None:
    """正向：建 4 个、各 close() → 注册表回到基线，不留常驻格。"""
    base = len(REGISTRY)
    for i in range(4):
        kb = _make(tmp_path, f"closed-{i}")
        kb.index("health_reports", f"{i}.txt", "随访须知：第 7 天复查血常规。")
        kb.close()
    assert len(REGISTRY) == base, (
        f"close() 没摘掉 chroma 的 system：基线 {base}，现在 {len(REGISTRY)} —— "
        "每次热重建都会往进程里留一格（sqlite 句柄 + 建索引线程池）"
    )


def test_without_close_the_systems_pile_up(tmp_path: Path) -> None:
    """反向臂：不 close（连 del + gc 都不够）→ 注册表按数量涨。

    这一条不是冗余：没有它，上一条可能因为"本来就没人注册"而空转。
    收尾交给 conftest 那个 autouse 摘除（本文件不自己清，清了就等于把判据踩在自己脚下）。
    """
    import gc

    base = len(REGISTRY)
    for i in range(3):
        kb = _make(tmp_path, f"leak-{i}")
        kb.index("health_reports", f"n{i}.txt", "化疗后注意体温。")
        del kb
    gc.collect()
    assert len(REGISTRY) == base + 3, (
        f"预期「不 close 就一格一格堆着」（基线 {base} → {base + 3}），实到 {len(REGISTRY)} —— "
        "chroma 的注册表行为变了，本文件的正向判据也要跟着重读"
    )


def test_rebuild_path_has_only_one_live_system(tmp_path: Path) -> None:
    """装配根那一侧的形状：重建 = 新的活着、旧的摘掉，注册表不随重建次数涨。"""
    base = len(REGISTRY)
    first = _make(tmp_path, "kb-a")
    first.index("health_reports", "a.txt", "随访须知")
    second = _make(tmp_path, "kb-b")  # 新实例（重建时先建新、再关旧的）
    second.index("health_reports", "b.txt", "复查频率")
    first.close()
    assert len(REGISTRY) == base + 1, (
        f"重建之后应当只剩新那一格：基线 {base}，现在 {len(REGISTRY)}"
    )
    second.close()
    assert len(REGISTRY) == base
