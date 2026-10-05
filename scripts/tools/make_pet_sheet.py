"""生成**随包的**桌宠形象素材：`frontend/public/pets/<包>/sprite.png` + 同目录的 `pack.json`。

跑法：`--pack mint` 出单个包，`--all` 把 `VARIANTS` 里每套都重生成一遍（默认 `default`）。
用户自己放的素材**不落在这里** —— 那一份在 `<数据根>/pets/<包>/`（安装目录是整目录替换，
放这儿等于"更新一次丢一次"）。两处由 `features/pet_packs.py` 一起扫，同名时数据根那份赢。

`pack.json` 里写 `label` / `kind` / `rows`：行语义（协议未确认的 5–8 行在这套包里各是什么
动画）只有生成它的脚本知道，所以它随素材走，不在 `frontend/src` 里存第二份。

为什么是**自绘**而不是下载现成素材包（2026-09-28 拍）：图面协议是开源的（waifu-sprites,
MIT-0），但集市上的宠物包多为同人/游戏角色，授权状况不明 —— 往仓库里塞一张来源不清的
图，是给"能公开分发的自包含安装包"埋一颗雷。自绘 = 许可干净、可再生成、参数即风格。

图面协议（与 `frontend/src/components/pet/PetSprite.tsx` 同一份）：
    1536×1872（= 192×208 的 8 列 × 9 行），**行 = 动画，列 = 帧**。
本脚本生成的这份包，行语义（默认包自定，见 `DEFAULT_PACK_ROWS`）：
    0 idle 呼吸 + 眨眼    1 跑向右    2 跑向左    3 挥手/说话
    4 跳跃                5 晕（×眼）  6 等待（头歪）7 目光扫（琢磨）  8 睡觉（Z）
前 5 行与协议主表对齐；后 4 行是这份包自己声明的（协议未确认那几行）。

跑法（需要一个带 Pillow 的解释器；主 .venv 刻意不装它 —— 这只是素材工具，不是运行依赖）：

    .venv-ocr\\Scripts\\python.exe scripts\\make_pet_sheet.py

产物随源码一起入库（dist 是入库的，所以素材也必须入库）；改参数重跑即可换风格。
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

# 不进主依赖：这是"生成素材的那台机器"才需要的能力，缺了它运行期一切照常。
# （mypy 的 ignore_missing_imports 已开，所以这里不需要 type: ignore。）
from PIL import Image, ImageDraw

# 控制台在 GBK 下打不出某些字符就会整段失败 —— 这个脚本只打中文与 ASCII，
# 但按仓库纪律（`R26-24`）仍显式重配一次，免得以后有人加了一句就炸。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SCALE = 2  # 超采样：先按 2 倍画，再降采样到协议格尺寸（这就是全套抗锯齿）
CELL_W, CELL_H = 192, 208
COLS, ROWS = 8, 9

PETS_DIR = Path(__file__).resolve().parents[2] / "frontend" / "public" / "pets"


@dataclass(frozen=True)
class Variant:
    """一套包 = 一个 id、给人看的名字、调色板覆盖、有没有呆毛、它自己声明的行语义。"""

    id: str
    label: str
    palette: dict[str, tuple[int, ...]]
    tuft: bool
    rows: dict[str, int]


#: 每套包 = 一份调色板覆盖 + 一点形状差别 + 行语义。**参数即风格**（改风格只动这里）。
VARIANTS: dict[str, Variant] = {
    v.id: v
    for v in (
        Variant(id="default", label="默认团子", palette={}, tuft=False,
                rows={"listening": 6, "thinking": 7}),
        Variant(
            id="mint",
            label="薄荷",
            palette={
                "BODY": (206, 236, 216, 255),
                "BODY_SHADE": (176, 216, 190, 255),
                "OUTLINE": (96, 128, 110, 255),
                "EAR_IN": (236, 176, 168, 255),
            },
            tuft=True,  # 头顶一撮呆毛：远看也能一眼分人
            rows={"listening": 6, "thinking": 7},
        ),
        Variant(
            id="rose",
            label="霜玫",
            palette={
                "BODY": (244, 214, 224, 255),
                "BODY_SHADE": (228, 186, 202, 255),
                "OUTLINE": (132, 96, 108, 255),
                "BLUSH": (236, 132, 132, 120),
            },
            tuft=False,
            rows={"listening": 6, "thinking": 7},
        ),
    )
}

# 调色板：奶油系团子。**一处定义**，改风格只动这里。
OUTLINE = (138, 114, 104, 255)
BODY = (246, 224, 200, 255)
BODY_SHADE = (235, 205, 175, 255)
EAR_IN = (242, 168, 138, 255)
EYE = (74, 64, 58, 255)
MOUTH = (181, 101, 94, 255)
BLUSH = (242, 164, 137, 96)
SHADOW = (60, 50, 45, 48)
WHITE = (255, 255, 255, 150)
#: 这一包有没有头顶那撮呆毛。素材工具一次只画一套，所以用模块级切换而不是把 9 支
#: 颜色穿进每一个 draw_* 的签名 —— 那是为了三套配色改坏全部画函数。
TUFT = False


def s(v: float) -> float:
    """逻辑坐标（按 192×208 设计）→ 超采样画布坐标。"""
    return v * SCALE


def ell(d: ImageDraw.ImageDraw, cx: float, cy: float, rx: float, ry: float,
        fill: tuple[int, ...] | None, outline: tuple[int, ...] | None = None,
        width: float = 3.0) -> None:
    d.ellipse(
        [s(cx - rx), s(cy - ry), s(cx + rx), s(cy + ry)],
        fill=fill, outline=outline, width=int(s(width)),
    )


def line(d: ImageDraw.ImageDraw, pts: list[tuple[float, float]],
         fill: tuple[int, ...], width: float = 3.0) -> None:
    d.line([(s(x), s(y)) for x, y in pts], fill=fill, width=int(s(width)), joint="curve")


def frame_params(row: int, i: int) -> dict[str, float | str | bool]:
    """第 row 行动画、第 i 帧的形态参数。**所有动画都从这里出来** —— 画的那半段不认识
    行号，加一行动画只在这里补一段。

    两种节拍：`w` 是循环波（首尾相接，用于 idle/跑/挥手这类往复），`a` 是一次性弧线
    （0→1→0，用于跳跃这种"起跳-落地"的整段动作，收尾那一帧要正好回到地面）。
    """
    t = i / COLS
    w = math.sin(2 * math.pi * t)
    a = math.sin(math.pi * i / (COLS - 1))
    p: dict[str, float | str | bool] = {
        "dx": 0.0, "dy": 0.0, "sx": 1.0, "sy": 1.0, "tilt": 0.0,
        "eye": "open", "pupil_dx": 0.0, "mouth": "smile",
        "arm_l": 0.0, "arm_r": 0.0, "foot_l": 0.0, "foot_r": 0.0, "lift": 0.0,
        "zzz": 0.0, "dizzy": False,
    }
    if row == 0:      # idle：呼吸 + 第 8 帧眨一下眼
        p["sy"] = 1 + 0.024 * w
        p["sx"] = 1 - 0.018 * w
        p["eye"] = "blink" if i == 7 else "open"
    elif row == 1:    # 跑向右：整体斜 + 双脚交替
        p["dx"] = 6 * w
        p["tilt"] = 4 * w
        p["mouth"] = "o"
        p["foot_l"] = -7 * max(0.0, w)
        p["foot_r"] = -7 * max(0.0, -w)
        p["dy"] = -2 * abs(w)
    elif row == 2:    # 跑向左：镜像
        p["dx"] = -6 * w
        p["tilt"] = -4 * w
        p["mouth"] = "o"
        p["foot_l"] = -7 * max(0.0, -w)
        p["foot_r"] = -7 * max(0.0, w)
        p["dy"] = -2 * abs(w)
    elif row == 3:    # 挥手（说话）：右臂抬起、来回摆；嘴张开
        p["arm_r"] = -52 + 22 * w
        p["mouth"] = "open"
        p["dy"] = -3 * abs(w)
        p["eye"] = "happy"
    elif row == 4:    # 跳：a 弧线离地、落地那两帧压扁
        p["dy"] = -46 * a
        p["lift"] = 46 * a
        p["sy"] = 1 - 0.06 * (1 - a)
        p["sx"] = 1 + 0.05 * (1 - a)
        p["mouth"] = "open"
    elif row == 5:    # 晕：× 眼 + 歪 + 头顶转圈
        p["eye"] = "x"
        p["mouth"] = "wave"
        p["tilt"] = 6 * w
        p["dx"] = 3 * w
        p["dizzy"] = True
    elif row == 6:    # 等待：头歪、目光左右找
        p["tilt"] = 7 * w
        p["pupil_dx"] = 5 * w
        p["mouth"] = "line"
    elif row == 7:    # 琢磨：目光扫读 + 微微前倾
        p["pupil_dx"] = 9 * w
        p["dx"] = 3 + 4 * w
        p["mouth"] = "line"
        p["eye"] = "squint"
    else:             # 睡：闭眼、慢呼吸、Z 往上飘
        p["eye"] = "closed"
        p["sy"] = 1 + 0.035 * w
        p["sx"] = 1 - 0.026 * w
        p["mouth"] = "o"
        p["zzz"] = (i + 1) / COLS
    return p


def draw_face(d: ImageDraw.ImageDraw, p: dict[str, float | str | bool]) -> None:
    eye = str(p["eye"])
    pdx = float(p["pupil_dx"])
    for base_x in (78.0, 114.0):
        cx = base_x + pdx
        if eye == "blink":
            line(d, [(cx - 9, 116), (cx + 9, 116)], EYE, 3.4)
        elif eye == "closed":
            # 闭眼是一条向下的小弧（睡）
            d.arc([s(cx - 9), s(106), s(cx + 9), s(122)], 200, 340, fill=EYE, width=int(s(3.2)))
        elif eye == "x":
            line(d, [(cx - 8, 108), (cx + 8, 124)], EYE, 3.4)
            line(d, [(cx - 8, 124), (cx + 8, 108)], EYE, 3.4)
        elif eye == "happy":
            d.arc([s(cx - 9), s(108), s(cx + 9), s(126)], 180, 360, fill=EYE, width=int(s(3.4)))
        elif eye == "squint":
            ell(d, cx, 118, 8.5, 6.0, EYE)
            ell(d, cx + 2.5, 115, 2.6, 2.6, WHITE)
        else:
            ell(d, cx, 116, 8.5, 11.0, EYE)
            ell(d, cx + 2.5, 111.5, 2.8, 2.8, WHITE)
            ell(d, cx - 2.5, 121.5, 1.6, 1.6, (230, 220, 210, 190))
    # 腮红
    ell(d, 62, 132, 7.0, 4.5, BLUSH)
    ell(d, 130, 132, 7.0, 4.5, BLUSH)
    mouth = str(p["mouth"])
    if mouth == "open":
        ell(d, 96, 140, 7.0, 6.0, MOUTH)
    elif mouth == "o":
        ell(d, 96, 140, 4.0, 3.4, MOUTH)
    elif mouth == "line":
        line(d, [(89, 140), (103, 140)], MOUTH, 2.6)
    elif mouth == "wave":
        line(d, [(88, 138), (92, 143), (96, 138), (100, 143), (104, 138)], MOUTH, 2.4)
    else:  # smile
        d.arc([s(88), s(132), s(104), s(146)], 20, 160, fill=MOUTH, width=int(s(2.6)))


def draw_arm(d: ImageDraw.ImageDraw, x: float, y: float, angle_deg: float,
             inward: int, *, cx: float, cy: float) -> None:
    """一只前爪。`angle_deg` 是相对"垂下"的抬臂角度（正 = 抬起）；抬起时爪子挪到
    身体侧上方、**压着身体轮廓边缘** —— 不能挪成一颗悬空的小球（第一次生成就是这个
    毛病，单独一个球漂在旁边像零件掉了）。"""
    if abs(angle_deg) < 0.5:
        ell(d, x, y, 11.5, 15.0, BODY, OUTLINE, 3.0)
        return
    sway = 6.0 * math.sin(math.radians(angle_deg) * 2.0)  # 来回摆那一下
    px = cx + inward * (60 + sway * 0.5)
    py = cy - 26 + 4 * math.sin(math.radians(angle_deg))
    ell(d, px, py, 11.0, 13.0, BODY, OUTLINE, 3.0)


def draw_character(p: dict[str, float | str | bool]) -> Image.Image:
    """把整只角色画在一张透明层上（旋转换轴用），阴影不在这层里。"""
    img = Image.new("RGBA", (CELL_W * SCALE, CELL_H * SCALE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    dx, dy = float(p["dx"]), float(p["dy"])
    sx, sy = float(p["sx"]), float(p["sy"])
    cx, cy = 96 + dx, 124 + dy

    # 耳朵（先画，压在身体下面）——带一点倾斜，用临时图旋转
    for ex, ang in ((66.0, -16.0), (126.0, 16.0)):
        ear = Image.new("RGBA", (int(s(44)), int(s(64))), (0, 0, 0, 0))
        ed = ImageDraw.Draw(ear)
        ed.ellipse([0, 0, s(30), s(56)], fill=BODY, outline=OUTLINE, width=int(s(3)))
        ed.ellipse([s(7), s(10), s(23), s(46)], fill=EAR_IN)
        ear = ear.rotate(ang, resample=Image.BICUBIC, expand=True)
        img.alpha_composite(ear, (int(s(ex) - ear.width / 2), int(s(60) - ear.height / 2)))
    d = ImageDraw.Draw(img)

    # 身体（头身一体）＋ 高光
    ell(d, cx, cy, 58 * sx, 55 * sy, BODY, OUTLINE, 3.2)
    ell(d, cx - 20, cy - 18, 20 * sx, 15 * sy, WHITE)
    # 脚（在身体下缘露出来）
    ell(d, cx - 20, cy + 50 + float(p["foot_l"]), 15, 9, BODY, OUTLINE, 2.8)
    ell(d, cx + 20, cy + 50 + float(p["foot_r"]), 15, 9, BODY, OUTLINE, 2.8)
    # 前爪
    draw_arm(d, cx - 52, cy + 18, float(p["arm_l"]), -1, cx=cx, cy=cy)
    draw_arm(d, cx + 52, cy + 18, float(p["arm_r"]), +1, cx=cx, cy=cy)
    # 呆毛（只有带这一标志的包画）：两根短线，压在头顶轮廓上
    if TUFT:
        line(d, [(cx - 2, cy - 54), (cx + 4, cy - 68)], OUTLINE, 2.6)
        line(d, [(cx + 2, cy - 54), (cx + 12, cy - 63)], OUTLINE, 2.6)
    # 脸（眼睛/腮红/嘴）
    draw_face(d, p)
    return img


def draw_cell(row: int, i: int) -> Image.Image:
    p = frame_params(row, i)
    cell = Image.new("RGBA", (CELL_W * SCALE, CELL_H * SCALE), (0, 0, 0, 0))
    d = ImageDraw.Draw(cell)
    # 地面影子：跳起来（lift>0）时变小变淡 —— 少了它"跳"看不出高度
    lift = float(p["lift"])
    shrink = max(0.45, 1 - lift / 60.0)
    ell(d, 96, 182, 46 * shrink, 10 * shrink, (60, 50, 45, int(48 * shrink)))
    char = draw_character(p)
    # 歪头/前倾：绕脚底中心转一下，比"整体平移"像活物
    tilt = float(p["tilt"])
    if abs(tilt) > 0.3:
        char = char.rotate(tilt, resample=Image.BICUBIC, center=(s(96), s(180)))
    cell.alpha_composite(char)
    if p["dizzy"]:
        d = ImageDraw.Draw(cell)
        for k in range(3):
            ang = (i / COLS) * 2 * math.pi + k * 2.1
            ell(d, 96 + 34 * math.cos(ang), 52 + 9 * math.sin(ang), 3.4, 3.4, MOUTH)
    if float(p["zzz"]) > 0:
        # Z 往上飘：越晚的帧位置越高（三轮循环，收尾接回起点）
        d = ImageDraw.Draw(cell)
        for k in range(3):
            at = (float(p["zzz"]) + k * 0.33) % 1.0
            zx, zy = 132 + 10 * at, 52 - 26 * at
            line(d, [(zx, zy), (zx + 11, zy), (zx, zy + 13), (zx + 11, zy + 13)], EYE, 2.6)
    return cell.resize((CELL_W, CELL_H), Image.LANCZOS)


#: 出厂调色板的快照。每套包都从这里出发，只覆盖它声明过的那几支。
BASE: dict[str, tuple[int, ...]] = {
    "OUTLINE": OUTLINE, "BODY": BODY, "BODY_SHADE": BODY_SHADE, "EAR_IN": EAR_IN,
    "EYE": EYE, "MOUTH": MOUTH, "BLUSH": BLUSH, "SHADOW": SHADOW, "WHITE": WHITE,
}


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build(variant: Variant) -> None:
    """按一份 `Variant` 出一套素材：sprite.png + pack.json。"""
    global TUFT
    # 不回到出厂色出发就会串色：第一次生成 mint 时忘了这条，rose 出来还是绿的 ——
    # 这种错没人看得出来，除非真的看图，所以它写在注释里而不是交给某个检查去抓。
    merged = dict(BASE)
    merged.update(variant.palette)
    for key, value in merged.items():
        globals()[key] = value
    TUFT = variant.tuft

    sheet = Image.new("RGBA", (CELL_W * COLS, CELL_H * ROWS), (0, 0, 0, 0))
    for row in range(ROWS):
        for i in range(COLS):
            sheet.paste(draw_cell(row, i), (i * CELL_W, row * CELL_H))
    out_dir = PETS_DIR / variant.id
    out_dir.mkdir(parents=True, exist_ok=True)
    sprite = out_dir / "sprite.png"
    sprite.write_bytes(_png(sheet))
    manifest = {"label": variant.label, "kind": "sheet", "rows": variant.rows}
    # 写 bytes 而不是 str：Windows 上 `write_text` 会把 "\n" 翻成 "\r\n"，而仓库的铁律是全部 LF
    # （`.gitattributes` + `line endings` 那条断言）—— 素材工具也是脚本，不因"它只产图"而豁免。
    (out_dir / "pack.json").write_bytes(
        (json.dumps(manifest, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    )
    print(f"wrote {sprite} + pack.json  {sheet.width}x{sheet.height}  "
          f"cell={CELL_W}x{CELL_H}  cols={COLS} rows={ROWS}  bytes={sprite.stat().st_size}")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成随包桌宠素材（sprite.png + pack.json）")
    parser.add_argument("--pack", default=None, choices=sorted(VARIANTS))
    parser.add_argument("--all", action="store_true", help="把 VARIANTS 里每套都重生成一遍")
    args = parser.parse_args()
    for name in (sorted(VARIANTS) if args.all else [args.pack or "default"]):
        build(VARIANTS[name])


if __name__ == "__main__":
    main()