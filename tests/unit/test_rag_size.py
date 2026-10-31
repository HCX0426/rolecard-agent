"""R1031-33 体积治理：字节读数、差集回收（尽力而为）、开机清扫（兜底）——三本账各钉一头。

为什么单给这一族写用例（而不是并进 test_rag.py）：它防的不是检索对错，是**删错的代价**
与**静默吃盘**。两头都是形状题：

  * `collection_bytes` 是"读数"：量不到必须回空 dict / 落 0，不许报错、更不许拿段数
    冒充字节 —— "少一个键"在本仓历来比"多一个键"危险（R28-55 一族）。
  * 回收是"判据"：R102-41 那一族偶发（`Nothing found on disk`）里 catalog 答不出账，
    **读不出的空账当"没引用"用 = 一次偶发删光知识库**。所以全部 fail-closed：
    账读不出 ⇒ 一个不碰；差集只认"这次删除亲手摘掉的 id"；全扫带 uuid 形状闸 +
    十分钟宽限期（新目录可能是别的进程正在建的集合）。

实测前提（chromadb 1.5.9，2026-10-31 现场解剖，两条都进用例）：
  1. `delete_collection` 掉 catalog 行、**不掉盘**（`<uuid>/data_level0.bin` 留在原地）；
  2. 目录名的 uuid 是 **segment id** 而不是 collection id（映射在 segments 表）；
  3. **Windows 上同进程删不动自己刚删的索引**（chroma 攥着句柄，rmtree 1s 后仍
     WinError 32，gc 救不了；Linux 的 POSIX 语义能删）——所以回收是**两层**：
     删除当场差集回收（Linux 绿路径）+ 下次开机全扫兜底（新进程句柄干净，实测可行）。
平台差异由测试分支吸收：钉的是**不变量**（账先清、盘最迟下次开机清、中间孤儿必须
可见可量），不是钉平台行为。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from rolecard_agent.rag.retriever import HashEmbedder, KnowledgeBase

DOC = "胆囊结石随访须知：每 3 到 6 个月复查一次超声。"


def _kb(tmp_path: Path) -> KnowledgeBase:
    return KnowledgeBase(tmp_path / "chroma", HashEmbedder())


def _backdate(paths: list[Path]) -> None:
    """把目录与其中文件的 mtime 调到 1 小时前 —— 模拟"上一世泄漏的旧孤儿"。"""
    old = (os.path.getmtime(paths[0]) if paths else 0) - 3600.0
    for p in paths:
        for f in list(p.rglob("*")) + [p]:
            if f.is_file():
                os.utime(f, (old, old))
        os.utime(p, (old, old))


def test_describe_reports_bytes_per_scope(tmp_path: Path) -> None:
    """概览页有"这个知识库吃了多少盘"这一格：索引过的集合字节必须非零、认得作用域。"""
    kb = _kb(tmp_path)
    kb.index("health_reports", "t1", DOC)
    row = next(r for r in kb.describe() if r["scope"] == "health_reports")
    assert isinstance(row["bytes"], int) and row["bytes"] > 0, row
    # 字节与分块数是**两本账**：count 数段，bytes 量目录（HNSW 落盘远大于文本本身）。
    assert row["bytes"] > len(DOC), "字节若只等于文本大小，说明量错了对象"


def test_empty_scope_is_zero_not_missing(tmp_path: Path) -> None:
    """空集合（create_scope 刚建、没索引）：bytes 落 0，键必须在。"""
    kb = _kb(tmp_path)
    kb.create_scope("quietly")
    row = next(r for r in kb.describe() if r["scope"] == "quietly")
    assert row["bytes"] == 0


def test_reset_scope_reclaims_or_defers_to_boot_sweep(tmp_path: Path) -> None:
    """**跨平台不变量**：重建之后，最迟经过一次开机清扫，盘上不许留着上一代的尸体。

    两平台的实测差异（Linux CI 与本机 Windows 各跑过，2026-10-31）：
      * Linux：`delete_collection` 当场 unlink（POSIX 允许删打开的文件）——没有泄漏，
        差集回收找不到目录、孤儿全扫也是空。
      * Windows：unlink 被自己进程的文件句柄押着（close 也不放，实测 1s 后仍在）——
        第一层如实报 (0,0)，泄漏变成**可见的孤儿**，开机清扫（close+重开=下世进程）
        实测收得走。
    所以这里不许钉"哪一层删的"，钉的是**结果**：账清、describe 清、清扫后盘上无残留。
    读数诚实性只在泄漏真实存在时判（第一层没删成就不许虚报 bytes_freed）。
    """
    kb = _kb(tmp_path)
    kb.index("health_reports", "t1", DOC)
    assert kb.collection_bytes().get("health_reports", 0) > 0, "前提：索引过就该有落盘"

    segs_removed, bytes_freed = kb.reset_scope("health_reports")
    assert "health_reports" not in kb.collection_bytes()
    assert all(r["scope"] != "health_reports" for r in kb.describe())

    orphans, ok = kb.orphan_segment_dirs()
    assert ok is True, "集合删完后账必须读得出"
    if orphans:
        # Windows 形状：第一层当场删不动 ⇒ 读数必须如实是 0（只算真从盘上消失的）。
        assert segs_removed == 0 and bytes_freed == 0, (segs_removed, bytes_freed)
    # 模拟下次开机（两平台都走一遍 —— Linux 上无事可扫，同样不许报错）：
    _backdate(orphans)
    kb.close()
    kb2 = _kb(tmp_path)
    removed, freed = kb2.sweep_orphan_segments()
    assert kb2.orphan_segment_dirs()[0] == [], "开机清扫之后，盘上必须再无泄漏"
    if orphans:
        assert removed == len(orphans) and freed > 0, (removed, freed)
    else:
        assert (removed, freed) == (0, 0), "Linux 无泄漏 ⇒ 清扫无事可做，读数如实"
    kb2.close()


def test_reset_twice_is_still_idempotent_with_new_return(tmp_path: Path) -> None:
    """第二次重建（集合已不在）：不炸、回收读数 (0,0) —— 幂等半边（引擎审计 #8）
    不许被新返回值弄坏。"""
    kb = _kb(tmp_path)
    kb.index("health_reports", "t1", DOC)
    kb.reset_scope("health_reports")
    assert kb.reset_scope("health_reports") == (0, 0)


def test_sweep_reclaims_aged_orphans_only(tmp_path: Path) -> None:
    """全扫的三条边界：旧孤儿收走、新孤儿（可能是别人正在建的集合）不碰、
    非 uuid 杂项永远不在射程。"""
    kb = _kb(tmp_path)
    kb.index("health_reports", "t1", DOC)
    base = tmp_path / "chroma"
    aged = base / str(uuid.uuid4())
    fresh = base / str(uuid.uuid4())
    junk = base / "not-a-uuid"
    for d in (aged, fresh, junk):
        d.mkdir()
        (d / "data_level0.bin").write_bytes(b"x" * 64)
    _backdate([aged])

    removed, freed = kb.sweep_orphan_segments()
    assert removed == 1 and freed >= 64, (removed, freed)
    assert not aged.exists(), "旧孤儿必须被收走"
    assert fresh.is_dir(), "宽限期内的新目录不许碰（可能是别的进程正在建集合）"
    assert junk.is_dir(), "非 uuid 形状永远不在射程"
    # sweep 是只删孤儿，不伤在账的目录：health_reports 的落盘还在。
    assert kb.collection_bytes().get("health_reports", 0) > 0


def test_unreadable_ledger_means_no_reclaim(tmp_path: Path) -> None:
    """账读炸（R102-41 的形状）：orphan 扫描回 `([], False)`、清扫一个不碰、
    差集回收两本账任一读不出也一个不碰 —— 危险形状被结构挡住。

    造形用**坏文件**（`not a database`）而不是删/改名活库：chroma 在 Windows 攥着
    sqlite 句柄，连改名都是 sharing violation（本模块承认的物理事实，上面的用例
    各用各的合法形状绕开）。坏文件让 `SELECT id FROM segments` 当场炸 —— 正是
    "表在但读不出"的那一本账。账层三函数只碰 `_chroma_path`，不起 client。
    """
    corrupt = tmp_path / "chroma"
    corrupt.mkdir()
    (corrupt / "chroma.sqlite3").write_bytes(b"this is not a database, at all")
    stray = corrupt / str(uuid.uuid4())
    stray.mkdir()
    (stray / "data_level0.bin").write_bytes(b"x" * 32)
    _backdate([stray])

    kb = KnowledgeBase.__new__(KnowledgeBase)  # 纯账层对象：不起 client，不碰句柄
    kb._chroma_path = corrupt  # noqa: SLF001
    ids, ok = kb._segment_ledger()  # noqa: SLF001
    assert (ids, ok) == (set(), False), "坏账必须报 False（问不出），不是'空账'"
    orphans, ok2 = kb.orphan_segment_dirs()
    assert orphans == [] and ok2 is False
    assert kb.sweep_orphan_segments() == (0, 0), "账读不出时清扫必须原地不动"
    assert kb._reclaim_segment_dirs(set(), True, set()) == (0, 0)  # noqa: SLF001
    assert kb._reclaim_segment_dirs({"x"}, False, set()) == (0, 0)  # noqa: SLF001
    assert stray.is_dir(), "以上全是不删 —— 问不出就不动盘"
