"""`/api/pets` —— 桌宠形象包的清单与素材读取（09-30，配合 `role_card.pet_pack`）。

只有两条**只读**端点：列清单、发一张图。为什么发图要后端做而不是靠静态托管：
用户外挂那份长在 `<数据根>/pets/`，而控制台是 `frontend/dist` 的静态 mount —— 让它看见
数据根等于把整个数据目录挂到公网上（`sqlite/`、`uploads/` 都在旁边）。所以两处素材
统一走这一条按 slug 白名单解析的路径：`pack_id` 过不了形状校验就 404，一次文件系统都不会碰。

清单里为什么带 `skipped`：他哪天放了一个 `kind: "live2d"` 的包进来而下拉里什么都没有，
"为什么没生效"必须有一句人话回答，而不是让他去猜是哪一层吞了它。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from rolecard_agent.core import pet_packs

router = APIRouter(tags=["pets"])


@router.get("/api/pets")
def list_pets() -> dict[str, object]:
    """可用的形象包（用户根覆盖随包那份）+ 每个没进清单的目录为什么没进。"""
    found = pet_packs.scan()
    return {
        "packs": [
            {
                "id": p.id,
                "label": p.label,
                "kind": p.kind,
                "rows": p.rows,
                "source": p.source,
                "sheet_url": p.sheet_url,
            }
            for p in found.packs
        ],
        "skipped": [{"id": name, "reason": why} for name, why in found.skipped],
        # 外挂目录说给人听：角色页那一格旁边直接印它，省得有人去 dist 里放素材（升级会没）。
        "user_dir": str(pet_packs.user_pets_dir()),
    }


@router.get("/api/pets/{pack_id}/sprite.png")
def pet_sprite(pack_id: str) -> FileResponse:
    """发这个包的序列帧。找不到 = 404（不猜别的包，也不返回默认图 —— 那会掩盖配错）。"""
    path = pet_packs.sheet_path(pack_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"没有这个桌宠形象包：{pack_id}")
    return FileResponse(path, media_type="image/png")
