"""界面背景图端点（`api/routers/appearance.py`）的端到端用例。

钉住四件事：
* 上传 png/jpg/webp/gif → 拿到 URL；**非图片后缀与空文件被拒**（400）；
* 取图端点回图片本体（200 + 正确的 content-type 家族）+ 路径穿越被拒（404）；
* 删图端点删掉文件、再取 404；
* 审计留痕（`set_appearance_background` / `clear_appearance_background`）。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.base.paths import user_data_root


def _png_bytes(w: int = 4, h: int = 4, rgb: tuple[int, int, int] = (200, 140, 90)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(sqlite_path=tmp_path / "app.db")
    with TestClient(app) as c:
        yield c


def test_upload_get_delete_roundtrip(client: TestClient) -> None:
    res = client.post(
        "/api/appearance/background",
        files={"file": ("wall.png", _png_bytes(), "image/png")},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["url"].startswith("/api/appearance/background/")
    name = body["name"]
    assert name.endswith(".png")

    got = client.get(body["url"])
    assert got.status_code == 200
    assert got.content[:8] == b"\x89PNG\r\n\x1a\n"

    # 文件真的落到了数据根下
    assert (user_data_root() / "appearance" / name).is_file()

    deleted = client.delete(f"/api/appearance/background/{name}")
    assert deleted.status_code == 200
    assert client.get(body["url"]).status_code == 404


def test_rejects_non_image_and_empty(client: TestClient) -> None:
    bad = client.post(
        "/api/appearance/background",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert bad.status_code == 400
    assert "图片" in bad.json()["detail"]

    empty = client.post(
        "/api/appearance/background",
        files={"file": ("x.png", b"", "image/png")},
    )
    assert empty.status_code == 400


def test_rejects_path_traversal(client: TestClient) -> None:
    """文件名只收纯文件名：带分隔符或 .. 的一律**拿不到文件**。

    判据用"不是 200"而不是"必须是 404"：带 %2F 的请求在路由层就被解码成多级路径
    （到不了 handler，于是是 405 而不是 404）；而 handler 自己那道防线（拒绝含分隔符
    的名字）覆盖的是"名字被解码后仍带分隔符"的情形。两种都必须是**拿不到文件**。
    """
    for bad in ("..%2F..%2Fapp.db", "sub%2Fimg.png", ".hidden"):
        assert client.get(f"/api/appearance/background/{bad}").status_code != 200
        assert client.delete(f"/api/appearance/background/{bad}").status_code != 200


def test_rejects_drive_relative_name(client: TestClient) -> None:
    """Windows 盘符相对名 `C:foo` 既不含斜杠也不以点开头 —— 旧的分隔符黑名单挡不住它
    （2026-10-30 安全审计 #6）：`background_dir() / "C:foo"` 得到 `C:foo`（换盘到进程
    CWD 解析），读/删打到数据根之外。改成生成名形状白名单后，它连形状都不合 → 404。
    """
    for bad in ("C:evil", "C:evil.png", "D:app.db"):
        assert client.get(f"/api/appearance/background/{bad}").status_code != 200
        assert client.delete(f"/api/appearance/background/{bad}").status_code != 200


def test_missing_background_is_404(client: TestClient) -> None:
    assert client.get("/api/appearance/background/nope.png").status_code == 404
    assert client.delete("/api/appearance/background/nope.png").status_code == 404


def test_upload_writes_audit(client: TestClient) -> None:
    client.post(
        "/api/appearance/background",
        files={"file": ("wall.png", _png_bytes(), "image/png")},
    )
    rows = client.get("/api/audit?limit=50").json()
    actions = {r["action"] for r in rows}
    assert "set_appearance_background" in actions
