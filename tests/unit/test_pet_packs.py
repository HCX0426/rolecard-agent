"""`core/pet_packs.py`：桌宠形象包的扫描与解析（全程对着临时目录，不碰任何真素材）。

钉的是 09-30 那套"两处素材 + 一包一目录"的规矩（`role_card.pet_pack` 那一条的读侧）：

  1. **一包 = 一个目录**，`sprite.png` 是入场券，`pack.json` 只补充"给人看的名字"和
     那几行动画语义 —— 缺声明也能用（放一张图就能换装是刻意的低门槛）。
  2. **数据根那份赢过随包那份**：升级是整目录替换，随包目录迟早被冲；而"我要用这张图盖掉
     出厂形象"是正当需求，赢的方向必须是用户那一侧。
  3. **没渲染器的包不进取选项，但要说为什么**（`kind: live2d` 现在画不了）—— 只给一句
     "没生效"和给一个只能选默认的死下拉，是两种不同的坏。
  4. **slug 校验在碰文件系统之前**：`sheet_path("../sqlite")` 必须是 None，不是"去 stat 一下"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rolecard_agent.core import pet_packs


def _pack(dirn: Path, *, sprite: bool = True, manifest: object | None = None) -> Path:
    dirn.mkdir(parents=True, exist_ok=True)
    if sprite:
        (dirn / pet_packs.SHEET_FILE).write_bytes(b"\x89PNG fake")
    if manifest is not None:
        raw = dirn / pet_packs.MANIFEST_FILE
        raw.write_text(
            manifest if isinstance(manifest, str) else json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
    return dirn


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """把两处根指到临时目录：`(用户根, 随包根)`。"""
    user = tmp_path / "data-root" / "pets"
    bundled = tmp_path / "dist" / "pets"
    user.mkdir(parents=True)
    bundled.mkdir(parents=True)
    monkeypatch.setattr(pet_packs, "_roots", lambda: [("user", user), ("bundled", bundled)])
    return user, bundled


def test_a_directory_with_a_sprite_is_a_pack(roots: tuple[Path, Path]) -> None:
    _pack(
        roots[1] / "default",
        manifest={"label": "默认团子", "kind": "sheet", "rows": {"thinking": 7}},
    )
    found = pet_packs.scan()
    assert [(p.id, p.label, p.source, p.rows) for p in found.packs] == [
        ("default", "默认团子", "bundled", {"thinking": 7})
    ]
    assert found.packs[0].sheet_url == "/api/pets/default/sprite.png"
    assert found.skipped == []


def test_no_manifest_still_ships_a_pack(roots: tuple[Path, Path]) -> None:
    """只丢一张图进来就该能用 —— 门槛低到不需要读文档。"""
    _pack(roots[0] / "mycat")
    found = pet_packs.scan()
    assert [(p.id, p.label, p.kind) for p in found.packs] == [("mycat", "mycat", "sheet")]
    assert found.packs[0].rows == {}


def test_a_broken_manifest_does_not_hide_the_picture(roots: tuple[Path, Path]) -> None:
    """`pack.json` 坏了按"没写"处理：一张能用的图不该被一个逗号挡在门外。"""
    _pack(roots[0] / "rose", manifest="{ this is not json ")
    found = pet_packs.scan()
    assert [p.id for p in found.packs] == ["rose"]
    assert found.packs[0].label == "rose"


def test_the_user_root_wins_over_the_bundled_one(roots: tuple[Path, Path]) -> None:
    _pack(roots[0] / "default", manifest={"label": "我盖的那份"})
    _pack(roots[1] / "default", manifest={"label": "随包那份"})
    found = pet_packs.scan()
    assert [(p.id, p.source, p.label) for p in found.packs] == [("default", "user", "我盖的那份")]


def test_a_missing_sprite_is_skipped_with_a_reason(roots: tuple[Path, Path]) -> None:
    _pack(roots[0] / "empty", sprite=False, manifest={"label": "空的"})
    found = pet_packs.scan()
    assert found.packs == []
    assert found.skipped == [("empty", "缺 sprite.png")]


def test_an_unrenderable_kind_is_skipped_saying_so(roots: tuple[Path, Path]) -> None:
    """今天只有序列帧渲染器：`kind: live2d` 不进取选项，但要说清是没渲染器而不是没图。"""
    _pack(roots[0] / "elysia", manifest={"label": "爱莉", "kind": "live2d"})
    found = pet_packs.scan()
    assert found.packs == []
    assert found.skipped == [("elysia", "今天没有这种渲染器（kind=live2d）")]


def test_a_directory_name_that_is_not_a_slug_is_skipped(roots: tuple[Path, Path]) -> None:
    _pack(roots[0] / "My Cat")
    found = pet_packs.scan()
    assert found.packs == []
    assert found.skipped and found.skipped[0][0] == "My Cat"
    assert "不像包名" in found.skipped[0][1]


def test_sheet_path_prefers_the_user_root(roots: tuple[Path, Path]) -> None:
    mine = _pack(roots[0] / "default")
    _pack(roots[1] / "default")
    assert pet_packs.sheet_path("default") == mine / pet_packs.SHEET_FILE


@pytest.mark.parametrize("bad", ["../sqlite", "..", "a/b", "", "Default", "x" * 70])
def test_sheet_path_refuses_anything_that_is_not_a_slug(bad: str) -> None:
    """形状不对就**不去碰文件系统**：这一条是防"包名变成一条路径"。"""
    assert pet_packs.sheet_path(bad) is None


def test_missing_roots_are_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """两处根都不存在（全新安装、还没建过目录）⇒ 空清单，不抛。"""
    monkeypatch.setattr(
        pet_packs,
        "_roots",
        lambda: [("user", tmp_path / "nope"), ("bundled", tmp_path / "also-nope")],
    )
    found = pet_packs.scan()
    assert (found.packs, found.skipped) == ([], [])


def test_user_pets_dir_hangs_off_the_data_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """外挂目录必须在**数据根**下面，而不是 `frontend/dist` 下面 —— 后者升级会整目录替换。"""
    monkeypatch.setattr(pet_packs, "user_data_root", lambda: tmp_path)
    assert pet_packs.user_pets_dir() == tmp_path / "pets"
