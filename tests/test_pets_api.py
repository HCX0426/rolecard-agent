"""`/api/pets` 两条只读端点（09-30，配合 `role_card.pet_pack`）。

只挂这一张 router，不跑整套装配：这两条不依赖 `AppContext`，把它们绑在全量 bootstrap 上
只会让用例变慢、变脆。分级那一半由 `tests/unit/test_route_access.py` 枚举真实路由去钉
（**没表态的端点默认算 operator**，`/api/pets` 已登记成 user 级只读）。

素材目录全部指到 `tmp_path`：这条链读的是文件系统，用例必须自己造那两处根。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rolecard_agent.api.routers import pets as pets_router
from rolecard_agent.core import pet_packs


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    user = tmp_path / "data-root" / "pets"
    bundled = tmp_path / "dist" / "pets"
    for pack, manifest in (
        (user / "elysia", {"label": "爱莉", "kind": "sheet", "rows": {"thinking": 7}}),
        (bundled / "default", {"label": "默认团子", "kind": "sheet"}),
        (bundled / "hollow", {"label": "只有声明没有图"}),
    ):
        pack.mkdir(parents=True)
        (pack / pet_packs.MANIFEST_FILE).write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
    (user / "elysia" / pet_packs.SHEET_FILE).write_bytes(b"\x89PNG\r\n\x1a\n fake-sheet")
    (bundled / "default" / pet_packs.SHEET_FILE).write_bytes(b"\x89PNG\r\n\x1a\n default-sheet")
    monkeypatch.setattr(pet_packs, "_roots", lambda: [("user", user), ("bundled", bundled)])
    monkeypatch.setattr(pet_packs, "user_data_root", lambda: tmp_path / "data-root")

    app = FastAPI()
    app.include_router(pets_router.router)
    return TestClient(app)


def test_the_listing_names_every_pack_and_why_one_was_dropped(client: TestClient) -> None:
    body = client.get("/api/pets").json()
    assert [(p["id"], p["source"]) for p in body["packs"]] == [
        ("elysia", "user"),
        ("default", "bundled"),
    ]
    assert body["packs"][0]["rows"] == {"thinking": 7}
    assert body["packs"][0]["sheet_url"] == "/api/pets/elysia/sprite.png"
    # 没被认的那些**必须带一句为什么**：放了目录却没出现在下拉里，最坏的处理是沉默。
    assert body["skipped"] == [{"id": "hollow", "reason": "缺 sprite.png"}]
    # 说的是"该往哪儿放"那一句，所以路径必须原样递到界面手上（分隔符按平台）。
    assert Path(body["user_dir"]).as_posix().endswith("data-root/pets")


def test_a_pack_serves_its_sheet(client: TestClient) -> None:
    res = client.get("/api/pets/elysia/sprite.png")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/png")
    assert b"fake-sheet" in res.content


def test_an_unknown_pack_is_a_404_not_the_default_picture(client: TestClient) -> None:
    """不能"找不到就发默认图"：那会让界面以为素材加载成功，把配错这件事彻底藏起来。"""
    assert client.get("/api/pets/nope/sprite.png").status_code == 404


def test_a_dropped_pack_is_also_a_404(client: TestClient) -> None:
    """`hollow` 有目录没图 —— 清单里没有它，取它也必须是 404 而不是 500。"""
    assert client.get("/api/pets/hollow/sprite.png").status_code == 404


def test_a_path_shaped_name_cannot_escape(client: TestClient) -> None:
    """`..%2Fsqlite` 这类名字在形状校验那一关就该被拒（404），不去 stat 任何东西。"""
    assert client.get("/api/pets/..%2Fdata-root%2Fsqlite/sprite.png").status_code in (404, 400)
