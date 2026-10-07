"""`/api/pets` 三条只读端点（09-30 起）：清单 / Cubism 运行时 / 包内素材。

只挂这一张 router，不跑整套装配：这三条都不依赖 `AppContext`，把它们绑在全量 bootstrap 上
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
from rolecard_agent.features import pet_packs


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
    assert body["packs"][0]["entry"] == "sprite.png"
    # 没被认的那些**必须带一句为什么**：放了目录却没出现在下拉里，最坏的处理是沉默。
    assert body["skipped"] == [{"id": "hollow", "reason": "缺入口文件 sprite.png"}]
    # 说的是"该往哪儿放"那一句，所以路径必须原样递到界面手上（分隔符按平台）。
    assert Path(body["user_dir"]).as_posix().endswith("data-root/pets")
    # 这台机器上没放 Cubism Core ⇒ 界面据此说明"为什么 live2d 的包一个都没有"。
    assert body["cubism_core"] is False
    core_posix = Path(body["cubism_core_path"]).as_posix()
    assert core_posix.endswith("pets/_runtime/live2dcubismcore.min.js")


def test_a_live2d_pack_is_listed_once_the_core_lands(tmp_path: Path, client: TestClient) -> None:
    """模型已经在了，缺的只是运行时 —— 放上去之后它就该出现（同一次扫描，不重启）。"""
    core = pet_packs.cubism_core_path()
    core.parent.mkdir(parents=True, exist_ok=True)
    core.write_text("// core", encoding="utf-8")
    model = tmp_path / "data-root" / "pets" / "hiyori"
    model.mkdir(parents=True)
    (model / "hiyori.model3.json").write_text("{}", encoding="utf-8")
    (model / pet_packs.MANIFEST_FILE).write_text(
        '{"label": "日鞠", "kind": "live2d", "motions": {"speaking": "TapBody"}}', encoding="utf-8"
    )
    body = client.get("/api/pets").json()
    assert body["cubism_core"] is True
    entry = next(p for p in body["packs"] if p["id"] == "hiyori")
    assert entry["kind"] == "live2d" and entry["entry"] == "hiyori.model3.json"
    assert entry["motions"] == {"speaking": "TapBody"}
    assert entry["sheet_url"] == "/api/pets/hiyori/hiyori.model3.json"


def test_a_pack_serves_its_sheet(client: TestClient) -> None:
    res = client.get("/api/pets/elysia/sprite.png")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/png")
    assert b"fake-sheet" in res.content


def test_a_nested_model_asset_is_served(tmp_path: Path, client: TestClient) -> None:
    """Live2D 的 model3.json 会引用子目录里的贴图/motion —— 那条相对路径必须能取到。"""
    motions = tmp_path / "data-root" / "pets" / "elysia" / "motions"
    motions.mkdir(parents=True)
    (motions / "tap.motion3.json").write_text('{"Curves": []}', encoding="utf-8")
    res = client.get("/api/pets/elysia/motions/tap.motion3.json")
    assert res.status_code == 200
    assert b"Curves" in res.content


def test_an_unknown_pack_is_a_404_not_the_default_picture(client: TestClient) -> None:
    """不能"找不到就发默认图"：那会让界面以为素材加载成功，把配错这件事彻底藏起来。"""
    assert client.get("/api/pets/nope/sprite.png").status_code == 404


def test_a_dropped_pack_is_also_a_404(client: TestClient) -> None:
    """`hollow` 有目录没图 —— 清单里没有它，取它也必须是 404 而不是 500。"""
    assert client.get("/api/pets/hollow/sprite.png").status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "/api/pets/elysia/../secret.txt",
        "/api/pets/elysia/textures/../../secret.txt",
        "/api/pets/..%2Fdata-root%2Fsqlite/sprite.png",
        "/api/pets/elysia/%2E%2E%2Fsecret.txt",
    ],
)
def test_a_path_shaped_name_cannot_escape_the_pack(client: TestClient, url: str) -> None:
    """包目录是唯一边界：`..` 无论以什么形式出现都拿不到东西（404 / 400，绝不 200）。"""
    assert client.get(url).status_code in (400, 404)


def test_missing_cubism_runtime_is_a_404_not_an_empty_script(client: TestClient) -> None:
    """没放运行时 ⇒ 404 且**不返回空 JS**。

    空 body 会让浏览器那侧 `<script>` 加载"成功"，Live2D 于是报一个跟真原因（缺运行时）
    毫无关系的错，而清单里那句"cubism_core: false"就白写了 —— 这条路径的沉默比 404 贵。
    它是用户自己接受条款后才放进来的东西，我们**不随包分发**，所以缺是常态、不是错误。
    """
    res = client.get("/api/pets/_runtime/live2dcubismcore.min.js")
    assert res.status_code == 404, res.text
    assert "Cubism Core" in res.json()["detail"], res.text


def test_the_cubism_runtime_is_served_as_javascript(tmp_path: Path, client: TestClient) -> None:
    """放上去之后就该以 JS 发出去 —— 界面那句"为什么 live2d 一个都没有"随之翻成有。"""
    core = pet_packs.cubism_core_path()
    core.parent.mkdir(parents=True, exist_ok=True)
    core.write_text("window.CubismFramework = {};", encoding="utf-8")
    res = client.get("/api/pets/_runtime/live2dcubismcore.min.js")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/javascript"), res.headers
    assert b"CubismFramework" in res.content
    assert client.get("/api/pets").json()["cubism_core"] is True
