"""`core/pet_packs.py`：桌宠形象包的扫描与解析（全程对着临时目录，不碰任何真素材）。

钉的是 09-30 那套"两处素材 + 一包一目录"的规矩（`role_card.pet_pack` 那一条的读侧）：

  1. **一包 = 一个目录**，入口文件是入场券（`sheet` 看 `sprite.png`，`live2d` 看
     `*.model3.json`），`pack.json` 只补充"给人看的名字"和动画语义 —— 缺声明也能用。
  2. **数据根那份赢过随包那份**：升级是整目录替换，随包目录迟早被冲；而"我要用这张图盖掉
     出厂形象"是正当需求，赢的方向必须是用户那一侧。
  3. **没渲染器的包不进取选项，但要说为什么**（`kind: dance` 这种，或者 live2d 而 Cubism
     Core 还没放）—— 只给一句"没生效"和给一个只能选默认的死下拉，是两种不同的坏。
  4. **形状校验在碰文件系统之前**，而包内相对路径解析之后必须还在包目录里面。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rolecard_agent.core import pet_packs


def _pack(
    dirn: Path,
    *,
    entry: str | None = pet_packs.SHEET_FILE,
    manifest: object | None = None,
) -> Path:
    dirn.mkdir(parents=True, exist_ok=True)
    if entry:
        (dirn / entry).write_bytes(b"\x89PNG fake")
    if manifest is not None:
        (dirn / pet_packs.MANIFEST_FILE).write_text(
            manifest
            if isinstance(manifest, str)
            else json.dumps(manifest, ensure_ascii=False),
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
    monkeypatch.setattr(pet_packs, "user_data_root", lambda: tmp_path / "data-root")
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
    assert found.packs[0].entry == pet_packs.SHEET_FILE
    assert found.packs[0].url() == "/api/pets/default/sprite.png"
    assert found.skipped == []


def test_no_manifest_still_ships_a_pack(roots: tuple[Path, Path]) -> None:
    """只丢一张图进来就该能用 —— 门槛低到不需要读文档。"""
    _pack(roots[0] / "mycat")
    found = pet_packs.scan()
    assert [(p.id, p.label, p.kind) for p in found.packs] == [("mycat", "mycat", "sheet")]
    assert found.packs[0].rows == {}
    assert found.packs[0].motions == {}


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
    assert [(p.id, p.source, p.label) for p in found.packs] == [
        ("default", "user", "我盖的那份")
    ]


def test_a_missing_sprite_is_skipped_with_a_reason(roots: tuple[Path, Path]) -> None:
    _pack(roots[0] / "empty", entry=None, manifest={"label": "空的"})
    found = pet_packs.scan()
    assert found.packs == []
    assert found.skipped == [("empty", "缺入口文件 sprite.png")]


def test_an_unknown_kind_is_skipped_saying_so(roots: tuple[Path, Path]) -> None:
    _pack(roots[0] / "weird", manifest={"label": "?", "kind": "dance"})
    found = pet_packs.scan()
    assert found.packs == []
    assert found.skipped == [("weird", "今天没有这种渲染器（kind=dance）")]


def test_live2d_needs_the_cubism_core_and_says_where_it_goes(roots: tuple[Path, Path]) -> None:
    """缺运行时的 live2d 包**不进取选项，但话要说全**：该放哪个文件名、放哪个目录。"""
    _pack(
        roots[0] / "elysia",
        entry="elysia.model3.json",
        manifest={"label": "爱莉", "kind": "live2d"},
    )
    found = pet_packs.scan()
    assert found.packs == []
    assert found.cubism_core is False
    reason = found.skipped[0][1]
    assert reason.startswith(f"缺 Cubism Core 运行时：把 {pet_packs.CUBISM_CORE_FILE} 放进 ")
    assert str(roots[0] / "_runtime") in reason


def test_live2d_pack_is_listed_once_the_core_is_there(roots: tuple[Path, Path]) -> None:
    core = pet_packs.cubism_core_path()
    core.parent.mkdir(parents=True)
    core.write_text("// cubism core", encoding="utf-8")
    _pack(
        roots[0] / "elysia",
        entry="elysia.model3.json",
        manifest={"label": "爱莉", "kind": "live2d", "motions": {"speaking": "TapBody"}},
    )
    found = pet_packs.scan()
    assert found.cubism_core is True
    pack = found.packs[0]
    assert (pack.kind, pack.entry, pack.motions) == (
        "live2d",
        "elysia.model3.json",
        {"speaking": "TapBody"},
    )
    assert pack.url() == "/api/pets/elysia/elysia.model3.json"
    assert pack.url("textures/face.png") == "/api/pets/elysia/textures/face.png"


def test_a_directory_name_that_is_not_a_slug_is_skipped(roots: tuple[Path, Path]) -> None:
    _pack(roots[0] / "My Cat")
    found = pet_packs.scan()
    assert found.packs == []
    assert found.skipped and found.skipped[0][0] == "My Cat"
    assert "不像包名" in found.skipped[0][1]


def test_the_runtime_directory_is_not_reported_as_a_broken_pack(
    roots: tuple[Path, Path],
) -> None:
    """`_runtime/` 是放运行时的，不该被当成一个"缺图的包"报出来吓人。"""
    (roots[0] / "_runtime").mkdir()
    (roots[0] / "_runtime" / pet_packs.CUBISM_CORE_FILE).write_text("// core", encoding="utf-8")
    found = pet_packs.scan()
    assert found.skipped == []
    assert found.cubism_core is True


def test_sheet_path_prefers_the_user_root(roots: tuple[Path, Path]) -> None:
    mine = _pack(roots[0] / "default")
    _pack(roots[1] / "default")
    assert pet_packs.sheet_path("default") == mine / pet_packs.SHEET_FILE


@pytest.mark.parametrize("bad", ["../sqlite", "..", "a/b", "", "Default", "x" * 70])
def test_pack_lookup_refuses_anything_that_is_not_a_slug(bad: str) -> None:
    """形状不对就**不去碰文件系统**：这一条是防"包名变成一条路径"。"""
    assert pet_packs.pack_dir(bad) is None
    assert pet_packs.pack_file(bad, "sprite.png") is None


@pytest.mark.parametrize(
    "relative",
    ["../secret.txt", "../../etc/passwd", "/absolute/path", "textures/../../sqlite/app.db", "."],
)
def test_pack_file_cannot_escape_its_own_directory(
    roots: tuple[Path, Path], relative: str
) -> None:
    """第二个守卫：包内相对路径**解析之后还得在包目录里**（Live2D 模型会引用子目录文件，
    所以这条路径必须开，但开了就不能让人借它往外面走）。"""
    pack = _pack(roots[0] / "default")
    (pack.parent / "secret.txt").write_text("nope", encoding="utf-8")
    assert pet_packs.pack_file("default", relative) is None


def test_pack_file_reads_a_nested_model_asset(roots: tuple[Path, Path]) -> None:
    """正例：真的在包里的子文件必须读得到 —— 否则上面那条守卫就退化成"一律拒绝"。"""
    pack = _pack(roots[0] / "elysia", entry="elysia.model3.json")
    (pack / "motions").mkdir()
    nested = pack / "motions" / "tap.motion3.json"
    nested.write_text("{}", encoding="utf-8")
    assert pet_packs.pack_file("elysia", "motions/tap.motion3.json") == nested


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
    assert pet_packs.cubism_core_path() == (
        tmp_path / "pets" / "_runtime" / pet_packs.CUBISM_CORE_FILE
    )
