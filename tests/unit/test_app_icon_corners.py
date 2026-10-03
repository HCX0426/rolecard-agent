"""图标四角那一格的用例（用户报的"桌面图标有白底"，尺子从前只判到"有没有 alpha 通道"）。

有通道 ≠ 透明：通道在、四角全不透明，桌面上照样是一块白底。所以这条判据读的是**角像素**。

两条臂都必须能红：
  · 造一张四角不透明的 RGBA PNG 塞进 ICO ⇒ `check_app_icon_frames` 红；
  · 同一张改成四角透明 ⇒ 绿；
  · 读不出形状（非 RGBA / 隔行）⇒ 红，而且红在"读不出"这一句上，不许当成干净。

PNG 是这里手写的（filter 0 + zlib + CRC），因为主 `.venv` 没有 PIL，而这条断言跑在每一次
门禁与 CI 上 —— 为一格判据把图像库拖进运行树是本仓反对的换法。手写也正好逼判据只依赖
标准库这件事被验证一次。
"""

from __future__ import annotations

import pathlib
import struct
import sys
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import check_consistency as cc  # noqa: E402


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(width: int, height: int, corners_alpha: int, color_type: int = 6) -> bytes:
    """一张四角为 `corners_alpha`、中心不透明的 PNG（filter 全 0，非隔行，8bit）。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            corner = x in (0, width - 1) and y in (0, height - 1)
            if color_type == 6:
                a = corners_alpha if corner else 255
                raw += bytes((255, 0, 0, a))
            else:
                raw += bytes((255, 0, 0))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def _ico(frames: list[tuple[int, bytes]]) -> bytes:
    """按 ICONDIRENTRY 的真实布局拼一个 ICO。

    目录项 16 字节 = 宽(1) 高(1) 色数(1) 保留(1) planes(2) bitCount(2) **bytesInRes(4)**
    (偏移 8) **imageOffset(4)**(偏移 12)。第一版我把 size/offset 从偏移 6 就开始写，
    整体前移两字节 ⇒ 尺子按 e[8:12] 切出来的不是 PNG 而是中间某处，报"帧不是 PNG"——
    看着像判据坏了，其实是夹具的字节布局错。**这份夹具必须按规范拼，否则它验不到判据。**
    """
    entries = bytearray()
    blobs = bytearray()
    offset = 6 + 16 * len(frames)
    for side, f in frames:
        w = 0 if side >= 256 else side
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 0, len(f), offset)
        blobs += f
        offset += len(f)
    return struct.pack("<HHH", 0, 1, len(frames)) + bytes(entries) + bytes(blobs)


def test_四角不透明的帧必须被读成不透明() -> None:
    blob = _png(32, 32, 255)
    got = cc._png_corner_alphas(blob)
    assert got == [255, 255, 255, 255], got


def test_四角透明的帧读成零() -> None:
    blob = _png(32, 32, 0)
    assert cc._png_corner_alphas(blob) == [0, 0, 0, 0]


def test_非RGBA的形状读出None而不是猜() -> None:
    assert cc._png_corner_alphas(_png(16, 16, 0, color_type=2)) is None
    assert cc._png_corner_alphas(b"not a png at all") is None


def test_白底那张判红而透明那张判绿(tmp_path: pathlib.Path, monkeypatch) -> None:
    """尺子本身的两臂。"""
    repo = tmp_path
    (repo / "shell" / "build").mkdir(parents=True)
    (repo / "shell").joinpath("app-icon-master.png").write_bytes(_png(256, 256, 0))
    opaque_ico = repo / "shell" / "build" / "icon.ico"
    # 六档以上、含 16/32/48/256，但四角全不透明 —— 这正是"通道在而白底也在"那一版
    SIZES = (256, 128, 64, 48, 40, 32, 24, 20, 16)
    opaque_ico.write_bytes(_ico([(s, _png(s, s, 255)) for s in SIZES]))
    monkeypatch.setattr(cc, "ROOT", repo)
    cc.fails.clear()
    cc.out = lambda *a, **k: None  # 只看结论，不污染门禁输出
    cc.check_app_icon_frames()
    assert cc.fails, "白底那一版没被判红 ⇒ 这条判据是摆设"
    assert any("白底回来了" in f for f in cc.fails), cc.fails

    cc.fails.clear()
    opaque_ico.write_bytes(_ico([(s, _png(s, s, 0)) for s in SIZES]))
    cc.check_app_icon_frames()
    assert not cc.fails, cc.fails


def test_仓库里那份ico此刻四角真的透明() -> None:
    """正向那一半：真文件必须过（只测夹具的断言等于没测）。"""
    data = (ROOT / "shell" / "build" / "icon.ico").read_bytes()
    count = int.from_bytes(data[4:6], "little")
    assert count >= 6, count
    corners: list[list[int]] = []
    for i in range(count):
        e = data[6 + 16 * i : 22 + 16 * i]
        ln = int.from_bytes(e[8:12], "little")
        off = int.from_bytes(e[12:16], "little")
        got = cc._png_corner_alphas(data[off : off + ln])
        assert got is not None, f"第 {i} 帧读不出"
        corners.append(got)
    assert all(max(c) < 16 for c in corners), corners


def test_图标层在非windows时记跳过而不是冒充过(monkeypatch) -> None:
    """CI 的 Linux 档跑不到 PE 资源 —— 那一层必须**被念出来**，不能安静返回 True。

    这条用例本身就是 10-03 那发 CI 红的另一臂：`ctypes.windll` 在 Linux 上没有，
    我用平台合取守卫让两档 mypy 都过，但"守卫走对了分支"这件事得有人量。
    """
    import probe_package_artifact as probe

    probe.SKIPPED.clear()
    monkeypatch.setattr(probe, "_IS_WINDOWS", False, raising=True)
    assert probe.check_icon_frames() is True, "跳过这一臂不该判红"
    assert len(probe.SKIPPED) == 1 and "图标层" in probe.SKIPPED[0], probe.SKIPPED
    probe.SKIPPED.clear()


def test_图标层在windows上产物缺失时判红不判跳过(monkeypatch, tmp_path: pathlib.Path) -> None:
    """另一臂：Windows 上"产物不在"是**没跑**，不是跳过，更不是过（②那层 10-01 定的形状）。"""
    import probe_package_artifact as probe

    monkeypatch.setattr(probe, "_IS_WINDOWS", True, raising=True)
    monkeypatch.setattr(probe, "UNPACKED", tmp_path / "没有这一层", raising=True)
    probe.SKIPPED.clear()
    assert probe.check_icon_frames() is False
    assert not probe.SKIPPED, "产物缺失被记成跳过 ⇒ 汇总里就成了'跳过一层'而不是'这层没过'"
