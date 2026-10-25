"""界面背景图（2026-10-25，第五轴）：本地图片的上传与静态托管。

**为什么要有它**：材质轴（玻璃↔实板）与透明度轴（整个界面透多少）要看得见效果，
就得有东西在背后 —— 一张自己的图。没有它，"透明"只是把底色调淡，看不出层次。

三件事：

  * `POST /api/appearance/background` —— 收一张图（png/jpg/webp/gif），落到**数据根**
    下的 `appearance/`，返回它的 URL（`/api/appearance/background/<name>`）；
  * `GET /api/appearance/background/{name}` —— 静态托管那一目录（带缓存头）；
  * `DELETE /api/appearance/background/{name}` —— 删掉（换图时清旧的，免得目录越积越多）。

**为什么图片不进 `data/uploads`**：那是"文档上传"的世界（每份都有台账、有来源、有抽取），
背景图不是文档 —— 它只是界面装饰。混进去会让 ingestion 台账多出无意义的行。

**安全边界**：文件名由服务端生成（不采信客户端名字），只允许图片后缀，体积上限
复用 `max_upload_bytes`；目录在数据根内，不接受路径分隔符。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_actor, get_context
from rolecard_agent.base.paths import user_data_root

router = APIRouter()

#: 允许的后缀（图片；svg 不收 —— 它能带脚本，是个 XSS 面）。
_ALLOWED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

#: 目录名（数据根下）。一个就够 —— 界面只有一张背景图。
_DIR_NAME = "appearance"


def background_dir() -> Path:
    """背景图目录（数据根下；不建目录 until 真的上传）。"""
    return user_data_root() / _DIR_NAME


@router.post("/api/appearance/background", status_code=201)
def upload_background(
    file: UploadFile,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> dict[str, object]:
    """上传一张界面背景图，返回它的 URL。"""
    name = (file.filename or "").strip()
    suffix = Path(name).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        allowed = "、".join(sorted(_ALLOWED_SUFFIXES))
        raise HTTPException(status_code=400, detail=f"只接受图片（{allowed}）。")
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件是空的。")
    if len(raw) > ctx.settings.max_upload_bytes:
        raise HTTPException(
            status_code=400, detail=f"文件超过上限（{ctx.settings.max_upload_bytes} 字节）。"
        )
    # **文件名由服务端生成**（不采信客户端名字）：客户端的名字可能带路径分隔符，
    # 也可能撞掉已有的图。内容 hash 做身份的一部分：同一张图再传不重复占空间。
    import hashlib

    digest = hashlib.sha256(raw).hexdigest()[:12]
    target = background_dir() / f"{digest}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    ctx.audit.log(
        actor=actor.id,
        action="set_appearance_background",
        target=target.name,
        detail={"bytes": len(raw)},
    )
    return {"url": f"/api/appearance/background/{target.name}", "name": target.name}


@router.get("/api/appearance/background/{name}")
def get_background(name: str) -> FileResponse:
    """取一张背景图（缓存头：文件名带内容 hash，变了就是新名，可以长缓存）。"""
    # **路径穿越防线**：只接受纯文件名（不含分隔符、不含 ..）。
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=404, detail="没有这张图。")
    target = background_dir() / name
    if not target.is_file():
        raise HTTPException(status_code=404, detail="没有这张图。")
    return FileResponse(
        target,
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.delete("/api/appearance/background/{name}")
def delete_background(
    name: str,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(get_actor),
) -> dict[str, object]:
    """删掉一张背景图（换图时清旧的）。"""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=404, detail="没有这张图。")
    target = background_dir() / name
    if not target.is_file():
        raise HTTPException(status_code=404, detail="没有这张图。")
    target.unlink()
    ctx.audit.log(
        actor=actor.id, action="clear_appearance_background", target=name, detail={}
    )
    return {"deleted": name}
