"""重生成应用图标：从 `shell/app-icon.png` 裁出圆角方块、套透明蒙版，产出多帧 `icon.ico`。

**为什么要有这个脚本而不是手改图**：装机版桌面快捷方式的图标取自 exe 内嵌资源
（`rolecard-agent.exe,0`），而那份 `shell/build/icon.ico` 从 `b798f80`（壳选型改 Electron 那次）
起就没人重生成过 —— 实测它**只有一帧 256×256，四角全不透明**：

  · 白底：源图是一张"圆角方块摆在近纯白画布上、还带一层投影"的展示图，外圈那 8% 留白
    被原样烙进了图标 ⇒ 桌面上看就是一个白方块（用户 10-03 报的）。
  · 水印：右下角「Qoder AI 生成」在圆角方块**之外**，按方块裁掉就顺带没了。
  · 单帧：Windows 的 16/32/48 那一档全靠硬缩，托盘和任务栏因此发糊。

源图**保留不动**（它是美术件的出处），产物两份：`shell/app-icon-master.png`（RGBA 母图）
与 `shell/build/icon.ico`（多帧）。跑完自己验：逐帧量四角 alpha 必须是 0、中心必须是 255，
不对就非零退出 —— 不靠人肉看图。

用法（主环境没有 PIL：图像解码本来就不在主进程里，见 `requirements-ocr.txt`）：

    .venv-ocr\\Scripts\\python.exe scripts\\make_app_icon.py
"""

from __future__ import annotations

import io
import pathlib
import sys

from PIL import Image, ImageDraw

# Windows 控制台默认 GBK，而这份输出里有 ✅/❌ 与中文 —— 不重配编码，脚本会**打印时**崩
# （实测：图标全对，最后那句"自检通过"把进程炸了）。与门禁 `console encoding` 同一条规矩。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "shell" / "app-icon.png"
MASTER = ROOT / "shell" / "app-icon-master.png"
ICO = ROOT / "shell" / "build" / "icon.ico"

#: 图标要带的帧。Windows 外壳取 16/32/48，256 给高分屏与大图标视图，中间几档给任务栏与 alt-tab。
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
#: 圆角半径占边长的比例。实测源图：半径 ≈ 160px / 边长 840px ⇒ 0.19，取 0.20。
CORNER_RATIO = 0.20
#: 判"这是图、不是白底/投影"的门槛：饱和度（max-min 通道）。蓝绿渐变 ≥ 28，
#: 而白底与灰投影的饱和度接近 0 —— 用亮度判不行，猫头本身就是白的。
SATURATION_MIN = 28


def _tile_bbox(im: Image.Image) -> tuple[int, int, int, int]:
    """按饱和度找出那块圆角方块的包围盒（水印和投影都是灰的，进不来）。"""
    rgb = im.convert("RGB")
    w, h = rgb.size
    px = rgb.load()
    minx, miny, maxx, maxy = w, h, 0, 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            r, g, b = px[x, y]
            if max(r, g, b) - min(r, g, b) >= SATURATION_MIN:
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
    if maxx <= minx or maxy <= miny:
        raise SystemExit("一个饱和像素都没找到 —— 源图不是预期的那张？")
    return minx, miny, maxx, maxy


def _square_crop(im: Image.Image) -> Image.Image:
    """把包围盒补成正方形（居中），避免非等比缩放把猫头压扁。"""
    x0, y0, x1, y1 = _tile_bbox(im)
    side = max(x1 - x0, y1 - y0) + 1
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    half = side // 2
    return im.convert("RGB").crop((cx - half, cy - half, cx - half + side, cy - half + side))


def _rounded_alpha(side: int) -> Image.Image:
    """圆角矩形的 alpha 蒙版。4× 超采样再缩回来 —— 直接画 16px 的圆角会成锯齿块。"""
    scale = 4
    big = Image.new("L", (side * scale, side * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, side * scale - 1, side * scale - 1],
        radius=int(round(side * scale * CORNER_RATIO)),
        fill=255,
    )
    return big.resize((side, side), Image.LANCZOS)


def build() -> int:
    if not SOURCE.exists():
        raise SystemExit(f"源图不在：{SOURCE}")
    tile = _square_crop(Image.open(SOURCE))
    side = tile.size[0]
    out = tile.convert("RGBA")
    out.putalpha(_rounded_alpha(side))
    MASTER.write_bytes(b"")  # 覆盖前不留旧内容（写失败也不会留一份"看着像新的"母图）
    out.save(MASTER, format="PNG")
    ICO.parent.mkdir(parents=True, exist_ok=True)
    ICO.write_bytes(b"")
    out.save(ICO, format="ICO", sizes=[(s, s) for s in ICON_SIZES])
    print(f"母图 {MASTER.name} {side}×{side} RGBA；图标 {ICO.name} 帧={ICON_SIZES}")
    return verify()


def _ico_frames(path: pathlib.Path) -> list[tuple[int, bytes]]:
    """按 ICO 目录项把每一帧的原始字节切出来（不碰 PIL 那套版本不稳的 seek 语义）。

    返回 `[(边长, 帧数据)]`，边长取自目录项（0 表示 256）。
    """
    data = path.read_bytes()
    if len(data) < 6 or int.from_bytes(data[2:4], "little") != 1:
        raise SystemExit(f"{path.name} 不是 ICO（类型字段不是 1）")
    count = int.from_bytes(data[4:6], "little")
    out: list[tuple[int, bytes]] = []
    for i in range(count):
        e = data[6 + 16 * i: 22 + 16 * i]
        if len(e) < 16:
            break
        w = 256 if e[0] == 0 else e[0]
        h = 256 if e[1] == 0 else e[1]
        # 目录项布局：宽高各 1 字节、颜色/reserved 各 1、planes/bitcount 各 2，
        # 然后 **8..12 是这帧的字节数、12..16 是它在文件里的偏移**（读反过一次：
        # 于是每一帧都被切成一堆垃圾，还被报成"不是 PNG"）。
        off = int.from_bytes(e[12:16], "little")
        ln = int.from_bytes(e[8:12], "little")
        out.append((min(w, h), data[off:off + ln]))
    return out


def verify() -> int:
    """逐帧量四角与中心：四角必须全透明、中心必须不透明。"""
    bad: list[str] = []
    frames = _ico_frames(ICO)
    sizes = sorted({s for s, _b in frames})
    for s, blob in sorted(frames):
        if blob[:4] != b"\x89PNG":
            # 我们自己生成时是 PNG（PIL 默认）；哪天变成 BMP，这条判据就得改成读 DIB 位深，
            # 别让它悄悄不检查 —— 所以这里直接算失败。
            bad.append(f"{s}px 帧不是 PNG 编码，没法按像素验透明度（生成方式变了？）")
            continue
        g = Image.open(io.BytesIO(blob)).convert("RGBA")
        sw, sh = g.size
        if (sw, sh) != (s, s):
            bad.append(f"目录项说 {s}px，帧本身是 {sw}×{sh}")
        corners = [g.getpixel(p)[3] for p in [(0, 0), (sw - 1, 0), (0, sh - 1), (sw - 1, sh - 1)]]
        center = g.getpixel((sw // 2, sh // 2))[3]
        # 小帧的圆角只有一两个像素宽，抗锯齿会把最外圈那几格抬起来一点：16px 档放宽到 40，
        # 其余仍是 8。判据不容真实的抗锯齿，就等于留一条永远会红的假判据。
        ceiling = 40 if s <= 16 else 8
        if any(a > ceiling for a in corners):
            bad.append(f"{s}px 帧四角不够透 alpha={corners}（上限 {ceiling}）")
        if center < 250:
            bad.append(f"{s}px 帧中心是透的 alpha={center}")
        print(f"  {s:>3}px 帧 四角 alpha={corners} 中心={center}")
    need = {16, 32, 48, 256}
    if not need.issubset(set(sizes)):
        bad.append(f"缺帧：要 {sorted(need)}，实有 {sizes}")
    if bad:
        print("❌ 图标自检没过：" + "；".join(bad), file=sys.stderr)
        return 1
    print("✅ 图标自检通过：四角全透明、中心不透明、16/32/48/256 都在")
    return 0


if __name__ == "__main__":
    raise SystemExit(verify() if "--check" in sys.argv else build())
