"""桌宠形象包：一个目录 = 一个包，扫**两处**，用户根赢过随包那份。

为什么要有"用户根"这一处（09-30 用户拍：素材外挂，模型他自己放）：安装包是整目录替换，
把用户放的素材写进 `frontend/dist/pets/` 等于"更新一次丢一次"；而且仓库**不打算**带来源
不明的角色素材（`scripts/make_pet_sheet.py` 头部那条自绘理由仍然成立）。所以：

    <数据根>/pets/<包名>/sprite.png          用户外挂（升级不动它，也不入库）
    <随包 dist>/pets/<包名>/sprite.png       仓库里自绘的那几套（许可干净，随包发）

一个包可选地带一份 `pack.json` 声明给人看的名字与行语义：

    {"label": "爱莉", "kind": "live2d", "motions": {"speaking": "TapBody"}}

`kind` 有两种：`sheet`（序列帧，入场券 `sprite.png`）与 `live2d`（入场券是目录里任意一个
`*.model3.json`）。**`live2d` 还要多一个前提：Cubism Core 运行时得在**
（`<数据根>/pets/_runtime/live2dcubismcore.min.js`）—— 渲染库自己要求宿主提供那份运行时，
而它是 Live2D 自家 SDK 条款下的产物，不由我们随包分发。缺它的时候 live2d 包**不进取选项，
但清单会带着该放哪儿那句话**：这才是"我放了模型怎么没动静"该有的回答。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from rolecard_agent.base.paths import console_dist_dir, user_data_root

#: 序列帧素材的文件名（协议：8 列 × 9 行、单格 192×208，见 `PetSprite.tsx`）。
SHEET_FILE = "sprite.png"
#: 可选的声明文件。缺了就按"kind=sheet、label=目录名、rows 空"处理 —— 放一张图就能用。
MANIFEST_FILE = "pack.json"
#: Live2D 模型的入口文件名后缀（真名字通常是 `<角色>.model3.json`，所以按后缀找而不是写死）。
MODEL_SUFFIX = ".model3.json"
#: Cubism Core 运行时的文件名与它该待的目录（见模块 docstring：不由我们随包发）。
CUBISM_CORE_FILE = "live2dcubismcore.min.js"

#: 今天有渲染器的 kind。`live2d` 还额外要求 core 在位，那条判断在 `_renderable()` 里。
RENDERABLE_KINDS: tuple[str, ...] = ("sheet", "live2d")

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
    #: 序列帧包自声明的状态→行（协议主表没确认的 5–8 行归包说）。
    rows: dict[str, int]
    #: Live2D 包自声明的 petStatus → motion 组名（`{"speaking": "TapBody"}` 之类）。
    motions: dict[str, str]
    #: "user" = 数据根下外挂的那份；"bundled" = 随包 dist 里那份。给人看清它从哪来。
    source: str
    #: 入口文件在包目录里的相对路径（`sprite.png` 或 `<角色>.model3.json`）。
    entry: str

    def url(self, relative: str = "") -> str:
        """包内某个文件的下载地址（`relative` 空 = 入口文件）。

        素材一律走后端这一条而不是静态托管：外挂那份长在数据根里，让静态 mount 看见数据根
        等于把 `sqlite/`、`uploads/` 一起挂出去。
        """
        base = f"/api/pets/{self.id}"
        return f"{base}/{relative or self.entry}"


@dataclass(frozen=True)
class PackScan:
    packs: list[PetPack]
    #: `(包名, 为什么没被列进来)`。放错东西的那只手需要一句话，不是沉默。
    skipped: list[tuple[str, str]]
    #: Cubism Core 在不在位（不在 ⇒ live2d 包全部进 `skipped` 并说明放哪儿）。
    cubism_core: bool


def user_pets_dir() -> Path:
    """用户外挂素材的根（升级不碰、不进 git）。"""
    return user_data_root() / "pets"


def cubism_core_path() -> Path:
    """Cubism Core 运行时的落点（由用户自己放，见模块 docstring 那句许可理由）。"""
    return user_pets_dir() / "_runtime" / CUBISM_CORE_FILE


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


def _string_map(manifest: dict[str, object], key: str) -> dict[str, str]:
    raw = manifest.get(key)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _row_map(manifest: dict[str, object]) -> dict[str, int]:
    raw = manifest.get("rows")
    rows: dict[str, int] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if str(value).isdigit():
                rows[str(key)] = int(value)
    return rows


def _model_entry(dirn: Path) -> str | None:
    """Live2D 包的入口：目录里任意一个 `*.model3.json`（名字通常是 `<角色>.model3.json`）。"""
    candidates = sorted(p.name for p in dirn.glob(f"*{MODEL_SUFFIX}") if p.is_file())
    return candidates[0] if candidates else None


def _entry_for(dirn: Path, kind: str) -> str | None:
    if kind == "live2d":
        return _model_entry(dirn)
    return SHEET_FILE if (dirn / SHEET_FILE).is_file() else None


def _renderable(kind: str) -> bool:
    if kind not in RENDERABLE_KINDS:
        return False
    if kind == "live2d":
        return cubism_core_path().is_file()
    return True


def _pack_from(dirn: Path, source: str) -> PetPack | None:
    """把一个目录变成一个包；不该进清单时返回 None（并由调用方说明为什么）。"""
    name = dirn.name
    if not _SLUG.match(name):
        return None
    manifest = _manifest(dirn)
    kind = str(manifest.get("kind") or "sheet")
    entry = _entry_for(dirn, kind)
    if entry is None or not _renderable(kind):
        return None
    return PetPack(
        id=name,
        label=str(manifest.get("label") or name),
        kind=kind,
        rows=_row_map(manifest),
        motions=_string_map(manifest, "motions"),
        source=source,
        entry=entry,
    )


def _why_missing(dirn: Path) -> str:
    """给一句"为什么这一格没进选项" —— 顺序是：形状 → kind → 运行时 → 入口文件。"""
    name = dirn.name
    if not _SLUG.match(name):
        return "目录名不像包名（要小写字母开头，只含 a-z0-9_-）"
    kind = str(_manifest(dirn).get("kind") or "sheet")
    if kind not in RENDERABLE_KINDS:
        return f"今天没有这种渲染器（kind={kind}）"
    if kind == "live2d" and not cubism_core_path().is_file():
        return f"缺 Cubism Core 运行时：把 {CUBISM_CORE_FILE} 放进 {cubism_core_path().parent}"
    if _entry_for(dirn, kind) is None:
        wanted = f"*{MODEL_SUFFIX}" if kind == "live2d" else SHEET_FILE
        return f"缺入口文件 {wanted}"
    return "没被认（未知原因）"


def scan() -> PackScan:
    """扫两处根。**同名时用户那份赢** —— 他要覆盖默认包是正当需求，而升级不该冲掉。"""
    packs: list[PetPack] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, root in _roots():
        if not root.is_dir():
            continue
        for dirn in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "_runtime"):
            pack = _pack_from(dirn, source)
            if pack is None:
                skipped.append((dirn.name, _why_missing(dirn)))
                continue
            if pack.id in seen:
                continue  # 用户根先扫 ⇒ 这里被跳过的一定是被覆盖的那份随包包
            seen.add(pack.id)
            packs.append(pack)
    return PackScan(packs=packs, skipped=skipped, cubism_core=cubism_core_path().is_file())


def pack_dir(pack_id: str) -> Path | None:
    """这个包的目录（用户根优先）。形状不对直接 None，不去碰文件系统。"""
    if not _SLUG.match(pack_id):
        return None
    for _source, root in _roots():
        candidate = root / pack_id
        if candidate.is_dir():
            return candidate
    return None


def pack_file(pack_id: str, relative: str) -> Path | None:
    """包目录**内**的一个文件（Live2D 模型要引用 moc3 / 贴图 / motion / physics 好几个）。

    这里是唯一的路径守卫：先确认解析结果还在包目录里面，再谈读不读。
    `..`、绝对路径、指到别的包 —— 一律 None。
    """
    base = pack_dir(pack_id)
    if base is None or not relative or Path(relative).is_absolute():
        return None
    try:
        target = (base / relative).resolve()
        return target if target.is_relative_to(base.resolve()) and target.is_file() else None
    except OSError:
        return None


def sheet_path(pack_id: str) -> Path | None:
    """这个包的序列帧文件（用户根优先）。留着是给只关心旧形状的用例与调用方用的。"""
    base = pack_dir(pack_id)
    if base is None:
        return None
    sprite = base / SHEET_FILE
    return sprite if sprite.is_file() else None
