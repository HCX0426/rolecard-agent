"""上传目录孤儿文件的盘点与回收（`core/uploads.py` + 对应端点）。

Traceability: US-7。

为什么这组测试值得单独写：回收是**唯一会永久删除用户数据**的动作，所以每一条安全边界
都必须有机器守住：

  1. 被台账引用的文件**绝不能**被删（否则正在用的报告被清掉）；
  2. `.parsed.txt` 副本跟随主文件（单独留一份孤立副本没有意义）；
  3. 越界的符号链接不碰（上传目录是服务创建的，但内容可能被人工干预）；
  4. 盘点是**只读**的 —— 先给人看清单，再执行；
  5. 执行写审计（条数 + 释放字节数）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.core.uploads import referenced_paths, remove_orphans, scan_orphans


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    return TestClient(create_app(sqlite_path=tmp_path / "uploads.db"))


def _upload(client: TestClient, name: str, body: bytes) -> dict[str, object]:
    tid = str(client.post("/api/session", json={}).json()["thread_id"])
    res = client.post(
        f"/api/session/{tid}/upload", files={"file": (name, body, "text/plain")}
    )
    assert res.status_code == 201, res.text
    return res.json()


def test_referenced_uploads_are_never_listed_as_orphans(client: TestClient) -> None:
    """**最重要的那条**：正在用的文件不能被判成垃圾。"""
    _upload(client, "在用的报告.txt", "随访须知：每半年复查。".encode())
    report = client.get("/api/uploads/orphans").json()
    assert report["orphans"] == []
    assert report["referenced"] >= 1
    assert report["scanned"] >= 1


def test_scan_lists_a_historical_leftover(client: TestClient, tmp_path: Path) -> None:
    """历史遗留副本（M4 修复前"重复上传各写一份"产生的那种）应被盘点出来。"""
    uploads = tmp_path / "uploads"
    _upload(client, "报告.txt", "内容".encode())
    leftover = uploads / "deadbeef_报告.txt"  # 模拟旧版本留下的孤儿副本
    leftover.write_text("历史垃圾", encoding="utf-8")
    (uploads / "deadbeef_报告.txt.parsed.txt").write_text("副本", encoding="utf-8")

    report = client.get("/api/uploads/orphans").json()
    names = {o["name"] for o in report["orphans"]}
    assert names == {"deadbeef_报告.txt", "deadbeef_报告.txt.parsed.txt"}
    assert report["total_bytes"] == len("历史垃圾".encode()) + len("副本".encode())


def test_scan_is_read_only(client: TestClient, tmp_path: Path) -> None:
    """盘点**绝不**动文件 —— 先给操作员看清单，确认之后才执行。"""
    uploads = tmp_path / "uploads"
    stray = uploads / "leftover.txt"
    uploads.mkdir(parents=True, exist_ok=True)
    stray.write_text("x", encoding="utf-8")

    client.get("/api/uploads/orphans")
    client.get("/api/uploads/orphans")
    assert stray.exists(), "盘点端点在只读语义下删了文件"


def test_cleanup_removes_only_orphans_and_reports_why(
    client: TestClient, tmp_path: Path
) -> None:
    uploads = tmp_path / "uploads"
    kept = _upload(client, "留着.txt", "保留内容".encode())
    stray_main = uploads / "aaaa1111_旧.txt"
    stray_parsed = uploads / "aaaa1111_旧.txt.parsed.txt"
    stray_main.write_text("垃圾", encoding="utf-8")
    stray_parsed.write_text("垃圾副本", encoding="utf-8")

    before = client.get("/api/uploads/orphans").json()
    assert len(before["orphans"]) == 2

    res = client.post("/api/uploads/cleanup").json()
    assert res["deleted"] == 2
    assert res["freed_bytes"] > 0
    assert res["referenced"] >= 1  # 保留的那份被台账引用
    assert not stray_main.exists() and not stray_parsed.exists()

    # 在用的那份必须毫发无损，而且它的任务仍在台账里
    still = list((tmp_path / "uploads").glob("*"))
    assert any(f.name.endswith(str(kept["file"])) for f in still)
    assert client.get("/api/uploads/orphans").json()["orphans"] == []


def test_cleanup_is_audited(client: TestClient, tmp_path: Path) -> None:
    """破坏性动作必须留痕：删了几个、释放多少字节。"""
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "x.txt").write_text("x", encoding="utf-8")

    client.post("/api/uploads/cleanup")
    audit = client.get("/api/audit?limit=50").json()
    row = next(a for a in audit if a["action"] == "cleanup_orphan_uploads")
    assert '"deleted": 1' in str(row["detail_json"])


def test_parsed_companion_survives_while_its_primary_is_referenced(
    client: TestClient, tmp_path: Path
) -> None:
    """主文件在用 → `.parsed.txt` 副本保留（结构化抽取要复用它，删了会白跑一次 OCR）。

    注意副本的**真实落盘名**是 `<uuid8>_<原名>.parsed.txt`（见 `core/uploads.parsed_text_path`），
    响应里的 `file` 字段只有原名 —— 测试必须按真实名字构造，否则测的不是那条规则。
    """
    uploads = tmp_path / "uploads"
    body = _upload(client, "有副本.txt", "正文".encode())
    primary = next(p for p in uploads.iterdir() if p.name.endswith(f"_{body['file']}"))
    companion = primary.with_name(primary.name + ".parsed.txt")
    companion.write_text("解析文本", encoding="utf-8")

    report = client.get("/api/uploads/orphans").json()
    assert companion.name not in {o["name"] for o in report["orphans"]}
    assert companion.exists()
    assert primary.exists()


def test_missing_upload_dir_is_not_an_error(client: TestClient) -> None:
    """目录还不存在（全新部署、一次都没上传过）→ 空报告，而不是 500。"""
    assert client.get("/api/uploads/orphans").json()["orphans"] == []


# -- 纯函数层（不经 HTTP） ---------------------------------------------------------


def test_symlink_pointing_outside_is_not_followed(tmp_path: Path) -> None:
    """越界的符号链接不进入候选集。

    Windows 上创建符号链接需要特权，拿不到就跳过 —— 跳过比"假装测过"诚实。
    """
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("敏感", encoding="utf-8")
    link = uploads / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    report = scan_orphans(uploads, referenced=set())
    assert "link.txt" not in {f.name for f in report.files}
    assert outside.exists()


def test_normalize_collapses_different_spellings_of_one_path(tmp_path: Path) -> None:
    """同一个文件的两种写法必须归一到同一个键。

    这是"误判孤儿"的唯一防线：上传端点存库的是它写文件时的路径，扫描时拿到的是目录项，
    两者写法（相对段、分隔符、大小写）不完全一致。归一不一致 → 在用的文件被判为垃圾 →
    数据丢失。所以这里断言的是**集合只有一个元素**，而不是某个具体字符串。
    """
    spelled_two_ways = [
        str(tmp_path / "uploads" / "sub" / ".." / "r.txt"),
        str(tmp_path / "uploads" / "r.txt"),
    ]
    assert len(referenced_paths(spelled_two_ways)) == 1


def test_remove_orphans_tolerates_a_vanishing_file(tmp_path: Path) -> None:
    """盘点到执行之间文件消失（人工删了/别的进程清了）→ 跳过它，不中断整批。"""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "a.txt").write_text("a", encoding="utf-8")
    (uploads / "b.txt").write_text("b", encoding="utf-8")
    report = scan_orphans(uploads, referenced=set())
    (uploads / "a.txt").unlink()  # 执行前先消失一个

    deleted, _ = remove_orphans(uploads, report)
    assert deleted == 1  # 只有 b 被删，且不抛异常

def test_a_ledger_row_from_another_data_root_still_counts_as_referenced(tmp_path: Path) -> None:
    """跨数据根的台账行不再制造沉默（`R28-19` 的一半）。

    真机形状：她的库里有三行 `source_file` 指着**仓库那个旧根**的绝对路径，而文件在安装根的
    uploads 目录里。以前按整条路径比 ⇒ 认不出，那些文件被列成"可回收"（差点被删）；
    反过来目录空着时又一句不说（没人知道台账指向别处）。现在按文件名认，两边都对。
    """
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "47ba2914_report.txt").write_text("在用的报告", encoding="utf-8")
    other_root = tmp_path / "old-root" / "uploads"
    referenced = referenced_paths([str(other_root / "47ba2914_report.txt")])

    report = scan_orphans(root, referenced)

    assert report.files == (), f"在用文件被判成可回收（跨根的台账行没认出来）：{report.files}"
    assert report.referenced == 1
    assert report.dangling == (), "原件就在这台机器上，不该报悬挂"


def test_dangling_ledger_rows_are_said_out_loud(tmp_path: Path) -> None:
    """反方向那一半：台账说有、这台机器上没有 ⇒ 说出来，而不是"一切正常 0 个孤儿"。"""
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "kept.txt").write_text("别的文件", encoding="utf-8")
    referenced = referenced_paths(
        [str(tmp_path / "elsewhere" / "gone-a.txt"), str(tmp_path / "elsewhere" / "gone-b.txt")]
    )

    report = scan_orphans(root, referenced)

    assert report.dangling == ("gone-a.txt", "gone-b.txt"), report.dangling
    assert [f.name for f in report.files] == ["kept.txt"], "该报的孤儿也不能被 dangling 挤掉"


def test_a_missing_upload_dir_reports_every_ledger_row_as_dangling(tmp_path: Path) -> None:
    """目录压根没建：不是"扫描 0 个，全部被引用"，而是把台账那些行点名出来。"""
    referenced = referenced_paths([str(tmp_path / "nope" / "uploads" / "x.txt")])

    report = scan_orphans(tmp_path / "no-such-dir", referenced)

    assert report.files == () and report.scanned == 0
    assert report.dangling == ("x.txt",), "目录不在时沉默 = 这个缺陷的本尊"
