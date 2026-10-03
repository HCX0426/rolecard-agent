"""chroma 偶发（`R102-41`）现场取证层的用例。

这一层的存在理由：那发偶发**不可请求**（约 1.3-2%/轮），而每次红完只剩同一句
`Nothing found on disk`，`tmp_path` 随后就被回收 ⇒ 十五批取证批批从头。
所以它必须在红的那一刻把"**元数据说这个段在、盘上那个目录没了**"钉下来。

关键那臂是真把段目录抽走（用真 chromadb 造形状，不是我自己拼一个看着像的库）——
本仓口径：判据读的是被测系统**当下真实的 schema**，chroma 1.5.9 的 `segments` 表没有 `path` 列、
段目录名就是 `segment_id`，这两条都是我 10-03 现读来的，写死进用例才不会被下一次"按旧版猜"带走。
"""

from __future__ import annotations

import pathlib
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import chroma_flake_evidence as cfe  # noqa: E402
import pytest_with_evidence as pwe  # noqa: E402

FLAKE_TEXT = "Error creating hnsw segment reader: Nothing found on disk"


@pytest.fixture
def ascii_root() -> Iterator[pathlib.Path]:
    """一个**纯 ASCII** 的临时根。

    为什么不用 pytest 的 `tmp_path`（10-03 现学的，代价是一次假诊断）：`tmp_path` 按测试函数名
    生成，而本仓用例名是中文 ⇒ 路径里带非 ASCII。A/B 实测（同机同版本 chromadb）：
    ASCII 根下 vector 段目录里是 4 个 hnsw 文件，**非 ASCII 根下目录存在但一个文件都没有**，
    而 `segments` 表照旧登记那一段。也就是说拿 `tmp_path` 造 chroma 形状，造出来的是
    "元数据有段、盘上没文件"这一族，用它去测一个专门抓这族的取证层，判据当场失真。
    这条库行为已单独记进记忆，它是 `R102-41` 的一个**相邻缺陷**，不是那一发本身
    （在册那发发生在 ASCII 路径与 Linux runner 上）。
    """
    d = pathlib.Path(tempfile.mkdtemp(prefix="rc_cfe_"))
    assert d.is_relative_to(pathlib.Path(tempfile.gettempdir()).resolve())
    assert all(ord(ch) < 128 for ch in str(d)), f"夹具根路径不是 ASCII：{d}"
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _chroma_with_two_collections(base: pathlib.Path):
    """真起一个持久化目录、写两个集合，返回 (client, persist 目录, 两个 vector 段 id)。"""
    chromadb = pytest.importorskip("chromadb")
    persist = base / "chroma"
    client = chromadb.PersistentClient(path=str(persist))
    for name in ("alpha", "beta"):
        col = client.get_or_create_collection(name)
        col.add(ids=["1"], documents=["hello"])
    con = sqlite3.connect(f"file:{(persist / 'chroma.sqlite3').as_posix()}?mode=ro", uri=True)
    try:
        vector_ids = [
            str(row[0])
            for row in con.execute("SELECT id, type FROM segments")
            if "vector" in str(row[1])
        ]
    finally:
        con.close()
    return client, persist, vector_ids


def test_签名判据两臂() -> None:
    assert cfe.hits_signature(FLAKE_TEXT) is True
    assert cfe.hits_signature("chromadb.errors.InternalError: x") is True
    assert cfe.hits_signature("assert 1 == 2") is False, "别的失败不该被当成那一发偶发"


def test_取证层与门禁共用同一份签名清单() -> None:
    """两处各抄一份 ⇒ 改了判据漏了另一处。这条问的就是"是不是同一个对象"。"""
    assert pwe.CHROMA_FLAKE_SIGNATURES is cfe.CHROMA_FLAKE_SIGNATURES


def test_落盘文件带nodeid与异常原文(tmp_path: pathlib.Path) -> None:
    out = cfe.dump_evidence(tmp_path / "ev", "tests/unit/test_a.py::test_b", FLAKE_TEXT)
    text = out.read_text(encoding="utf-8")
    assert out.name.startswith("r102-41-evidence-")
    assert "test_b" in text and FLAKE_TEXT in text


def test_两次现场不互相覆盖(tmp_path: pathlib.Path) -> None:
    cfe.dump_evidence(tmp_path, "test::a", FLAKE_TEXT)
    cfe.dump_evidence(tmp_path, "test::b", FLAKE_TEXT)
    assert len(cfe.existing_evidence(tmp_path)) == 2, "覆盖掉上一次的现场就等于没取证"


def test_段目录在盘上时逐段列尺寸(ascii_root: pathlib.Path) -> None:
    client, persist, vector_ids = _chroma_with_two_collections(ascii_root)
    try:
        lines = cfe._segment_state(persist)
    finally:
        client.close()
    body = "\n".join(lines)
    assert "目录在盘" in body, body
    assert "data_level0.bin" in body, "hnsw 那件文件是该层的判据本体，得真读出来"
    assert "0 个 vector 段的目录不在盘上" in body, body


def test_把段目录抽走时它必须点名(ascii_root: pathlib.Path) -> None:
    """**这一条是这层存在的全部理由**：元数据说有、盘上没有 ⇒ 现场里必须有那个段与它的集合。"""
    client, persist, vector_ids = _chroma_with_two_collections(ascii_root)
    assert vector_ids, "造不出 vector 段，这条用例就没在量任何东西"
    victim = vector_ids[0]
    client.close()
    shutil.rmtree(persist / victim)
    lines = cfe._segment_state(persist)
    body = "\n".join(lines)
    assert victim in body, body
    assert "目录不在盘上" in body, body
    assert "1 个 vector 段的目录不在盘上" in body, body


def test_注册表为空时第二腿仍留得下盘上形状(ascii_root: pathlib.Path) -> None:
    """第一版只问注册表 —— 而 `Client.close()` 之后它是**空的**（`R102-74` 那条库行为）。

    症状会是：现场里一句"注册表为空"然后什么盘上形状都没有，看着像"没发生那发偶发"。
    所以调用方可以传扫描根（conftest 传用例的 tmp_path），从 `chroma.sqlite3` 现找。
    """
    client, persist, vector_ids = _chroma_with_two_collections(ascii_root)
    victim = vector_ids[0]
    client.close()
    shutil.rmtree(persist / victim)
    out = cfe.dump_evidence(
        ascii_root / "ev", "test::closed_registry", FLAKE_TEXT, extra_roots=[ascii_root]
    )
    body = out.read_text(encoding="utf-8")
    assert "注册表为空" in body, body
    assert victim in body and "目录不在盘上" in body, "第二腿没把形状留住，这层就白加"


def test_库读不出时如实写一行而不是留空(tmp_path: pathlib.Path) -> None:
    """空与"干净"长得一模一样（`R102` 轮那条分母教训），所以每条问不出都要有字。"""
    persist = tmp_path / "chroma"
    persist.mkdir()
    rows = cfe._segment_state(persist)
    assert any("没有 chroma.sqlite3" in row for row in rows), rows
    (persist / "chroma.sqlite3").write_bytes(b"not an sqlite file at all")
    body = "\n".join(cfe._segment_state(persist))
    assert "读不出" in body or "打不开" in body, body
