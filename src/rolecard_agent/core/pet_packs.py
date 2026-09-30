"""桌宠形象包：一个目录 = 一个包，扫**两处**，用户根赢过随包那份。

为什么要有"用户根"这一处（09-30 用户拍：素材外挂，模型他自己放）：安装包是整目录替换，
把用户放的素材写进 `frontend/dist/pets/` 等于"更新一次丢一次"；而且我们**不打算**把来路
不明的角色素材带进仓库（`scripts/make_pet_sheet.py` 头部那条自绘理由仍然成立）。所以：

    <数据根>/pets/<包名>/sprite.png      用户外挂（升级不动它，也不入库）
    <随包 dist>/pets/<包名>/sprite.png   仓库里自绘的那几套（许可干净，随包发）

一个包可选地带一份 `pack.json` 声明给人看的名字与行语义：

    {"label": "爱莉", "kind": "sheet", "rows": {"thinking": 7, "listening": 6}}

为什么要 `kind`：今天只有序列帧渲染器，而这张表是"能不能选"的唯一判据 —— 没有渲染器的包
**不进取选项**（宁可不出现，也不给一个"选了没反应"的死控件），但它会出现在 `skipped` 里带着
一句为什么，这样"我明明放了个目录怎么没生效"这件事不会变成要靠读代码才能猜的哑谜。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from rolecard_agent.core.paths import console_dist_dir, user_data_root

#: 序列帧素材的文件名（协议：8 列 × 9 行、单格 192×208，见 `PetSprite.tsx`）。
SHEET_FILE = "sprite.png"
#: 可选的声明文件。缺了就按"kind=sheet、label=目录名、rows 空"处理 —— 放一张图就能用。
MANIFEST_FILE = "pack.json"
#: 今天真有渲染器的 kind。将来上 Live2D 就是往这个元组里加一条 `live2d`，
#: `role_card.pet_pack` 那一列与角色页的下拉都不用改。
RENDERABLE_KINDS: tuple[str, ...] = ("sheet",)

#: 目录名必须像个包名（与 `roles/models.PET_PACK_PATTERN` 同一条形状）。
#: 为什么在两处都写：这一条守的是"目录名不许变成路径"，那条守的是"请求体不许写别的" ——
#: 少任何一边，另一半就只是装饰。
_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class PetPack:
    """一个可用的形象包。"""

    id: str
    label: str
    kind: str
    rows: dict[str, int]
    #: "user" = 数据根下外挂的那份；"bundled" = 随包 dist 里那份。给人看清它从哪来。
    source: str

    @property
    def sheet_url(self) -> str:
        """素材一律走后端那一条 URL。

        为什么不"随包的用静态路径、外挂的用 API"：那会让前端要为每个包判断走哪条门，
        而两处的读语义本来一模一样（只读一张图）。一个答案，两边都不会漂。
        """
        return f"/api/pets/{self.id}/sprite.png"


@dataclass(frozen=True)
class PackScan:
    packs: list[PetPack]
    #: `(包名, 为什么没被列进来)`。放错东西的那只手需要一句话，不是沉默。
    skipped: list[tuple[str, str]]


def user_pets_dir() -> Path:
    """用户外挂素材的根（升级不碰、不进 git）。"""
    return user_data_root() / "pets"


def bundled_pets_dir() -> Path:
    """随包那份 dist 里的 `pets/`（仓库自绘的包长在这儿）。"""
    return console_dist_dir() / "pets"


def _roots() -> list[tuple[str, Path]]:
    return [("user", user_pets_dir()), ("bundled", bundled_pets_dir())]


def _manifest(dirn: Path) -> dict[str, object]:
    """读 `pack.json`。坏了就当没写 —— 一张能用的图不该被一个逗号挡在门外。"""
    raw = dirn / MANIFEST_FILE
    if not raw.is_file():
        return {}
    try:
        loaded = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _pack_from(dirn: Path, source: str) -> PetPack | None:
    """把一个目录变成一个包；不该进清单时返回 None（并由调用方说明为什么）。"""
    name = dirn.name
    if not _SLUG.match(name):
        return None
    manifest = _manifest(dirn)
    kind = str(manifest.get("kind") or "sheet")
    if kind not in RENDERABLE_KINDS:
        return None
    if not (dirn / SHEET_FILE).is_file():
        return None
    rows_raw = manifest.get("rows")
    rows: dict[str, int] = {}
    if isinstance(rows_raw, dict):
        for key, value in rows_raw.items():
            if str(value).isdigit():
                rows[str(key)] = int(value)
    return PetPack(
        id=name,
        label=str(manifest.get("label") or name),
        kind=kind,
        rows=rows,
        source=source,
    )


def _why_missing(dirn: Path) -> str:
    """给一句"为什么这一格没进选项" —— 先说 kind，再说图，因为 kind 不对时放图也没用。"""
    kind = str(_manifest(dirn).get("kind") or "sheet")
    if kind not in RENDERABLE_KINDS:
        return f"今天没有这种渲染器（kind={kind}）"
    if not (dirn / SHEET_FILE).is_file():
        return f"缺 {SHEET_FILE}"
    return "目录名不像包名（要小写字母开头，只含 a-z0-9_-）"


def scan() -> PackScan:
    """扫两处根。**同名时用户那份赢** —— 他要覆盖默认包是正当需求，而升级不该冲掉。"""
    packs: list[PetPack] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, root in _roots():
        if not root.is_dir():
            continue
        for dirn in sorted(p for p in root.iterdir() if p.is_dir()):
            pack = _pack_from(dirn, source)
            if pack is None:
                skipped.append((dirn.name, _why_missing(dirn)))
                continue
            if pack.id in seen:
                continue  # 用户根先扫 ⇒ 这里被跳过的一定是被覆盖的那份随包包
            seen.add(pack.id)
            packs.append(pack)
    return PackScan(packs=packs, skipped=skipped)


def sheet_path(pack_id: str) -> Path | None:
    """这个包的序列帧文件（用户根优先）。形状不对直接 None，不去碰文件系统。"""
    if not _SLUG.match(pack_id):
        return None
    for _source, root in _roots():
        candidate = root / pack_id / SHEET_FILE
        if candidate.is_file():
            return candidate
    return None
