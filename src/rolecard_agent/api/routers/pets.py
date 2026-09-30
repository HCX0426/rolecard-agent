"""`/api/pets` —— 桌宠形象包的清单与素材读取（09-30，配合 `role_card.pet_pack`）。

只有两类**只读**端点：列清单、发包里的文件。为什么发文件要后端做而不是靠静态托管：
用户外挂那份长在 `<数据根>/pets/`，而控制台是 `frontend/dist` 的静态 mount —— 让它看见
数据根等于把整个数据目录挂到公网上（`sqlite/`、`uploads/` 就在旁边）。所以两处素材统一走
这一条按 slug + "解析结果必须在包目录内"双重校验的路径。

清单里为什么带 `skipped` 与 `cubism_core`：他哪天放了一个 `kind: "live2d"` 的模型进来而
下拉里什么都没有，"为什么没生效"必须有一句人话回答（多半是缺 Cubism Core 运行时），
而不是让他去猜是哪一层吞了它。
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
                "motions": p.motions,
                "source": p.source,
                "entry": p.entry,
                "sheet_url": p.url(),
            }
            for p in found.packs
        ],
        "skipped": [{"id": name, "reason": why} for name, why in found.skipped],
        # 外挂目录说给人听：角色页那一格旁边直接印它，省得有人去 dist 里放素材（升级会没）。
        "user_dir": str(pet_packs.user_pets_dir()),
        # Live2D 的运行时在不在位 —— 不在 ⇒ 所有 live2d 包都会被跳过，这句话得能被界面看见。
        "cubism_core": found.cubism_core,
        "cubism_core_path": str(pet_packs.cubism_core_path()),
    }


@router.get("/api/pets/_runtime/live2dcubismcore.min.js")
def cubism_core() -> FileResponse:
    """Cubism Core 运行时（Live2D 的渲染库要求宿主先把它挂到 window 上）。

    为什么这条单独存在而不是走包内文件那条路：`_runtime` 过不了包名的形状校验（那是故意的，
    包名不许变成路径），而这份东西**不由我们随包分发** —— 它是 Live2D SDK 条款下的产物，
    要由使用者在自己这边接受条款后放进来。缺它时 live2d 包全部不进清单，清单里带着该放哪儿
    那句话，所以这里 404 也不会让人摸黑。
    """
    path = pet_packs.cubism_core_path()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="没有 Cubism Core 运行时")
    return FileResponse(path, media_type="application/javascript")


@router.get("/api/pets/{pack_id}/{relative:path}")
def pet_asset(pack_id: str, relative: str) -> FileResponse:
    """发这个包里的一个文件（入口图 / model3.json / moc3 / 贴图 / motion / physics）。

    找不到 = 404，**不回退到默认包**：那会让界面以为素材加载成功，把配错这件事彻底藏起来。
    真正的回退决定由前端 `pets/registry.ts` 在"清单里没有这个包"时做，那里看得见原因。
    """
    path = pet_packs.pack_file(pack_id, relative)
    if path is None:
        raise HTTPException(status_code=404, detail=f"没有这个桌宠素材：{pack_id}/{relative}")
    return FileResponse(path)
