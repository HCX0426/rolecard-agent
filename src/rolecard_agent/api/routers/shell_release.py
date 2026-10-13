"""桌面壳安装包：可下载状态与下载（里程碑 D②-4）。

**为什么用 FileResponse 而不是再 mount 一个 StaticFiles**：控制台 SPA 挂在 `/`，多挂一个
`/release` 就要和它抢匹配顺序 —— 顺序错了要么 404 要么把页面吞掉，这种坑不值得留。

**文件名只由服务端列目录得到**：客户端能说"给我最新那个产物"，不能说"给我这个文件"，
所以这里没有路径拼接，也就没有目录穿越。

`available` 的判据是**目录里到底有没有 exe**，而不是配置项开没开：部署方配了目录却忘了拷
产物时，界面不该出现一个点了没反应的按钮。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from rolecard_agent.api.deps import AppContext, get_context
from rolecard_agent.core.artifacts import ARTIFACT_GLOB
from rolecard_agent.core.update_check import check_update

router = APIRouter()

# 安装包文件名的形状只有一个出处（`core/artifacts.py`）：从前这里、装机脚本、
# electron-builder 的 artifactName、CI 的上传 glob 各写一遍"rolecard-agent-*.exe"，
# 改名时只有装机脚本那条会红 —— 而这一格的症状是下载入口安静消失，长得像设计意图。
_ARTIFACT_GLOB = ARTIFACT_GLOB


def _latest_artifact(directory: Path | None) -> Path | None:
    if directory is None or not directory.is_dir():
        return None
    files = [p for p in directory.glob(_ARTIFACT_GLOB) if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


@router.get("/api/shell-release")
def shell_release(ctx: AppContext = Depends(get_context)) -> dict[str, object]:
    """有没有壳安装包可下。没有 ⇒ `available: false`，界面连入口都不渲染。"""
    configured = ctx.settings.shell_release_dir is not None
    artifact = _latest_artifact(ctx.settings.shell_release_dir)
    if artifact is None:
        return {"available": False, "configured": configured}
    stat = artifact.stat()
    return {
        "available": True,
        "configured": True,
        "file_name": artifact.name,
        "size_bytes": stat.st_size,
        "built_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds"),
    }


@router.get("/api/shell-release/download")
def download_shell_release(ctx: AppContext = Depends(get_context)) -> FileResponse:
    """下载当前那个产物（没有就 404，不猜路径）。"""
    artifact = _latest_artifact(ctx.settings.shell_release_dir)
    if artifact is None:
        raise HTTPException(status_code=404, detail="没有可下载的桌面壳安装包")
    return FileResponse(artifact, filename=artifact.name, media_type="application/octet-stream")


@router.get("/api/update-check")
def update_check(request: Request, ctx: AppContext = Depends(get_context)) -> dict[str, object]:
    """「检查更新」：当前版本 vs 最新版本，只读、不下载、不装（设计稿 §3.1）。

    判据全在 `core/update_check.py`（api 薄：这里只接线，且版本从 app.version 现取 ——
    它由 `api/main.py` 用 API_VERSION 装配，不再抄第二个版本来源）。出网失败收敛成
    `error` 读数，端点永远 200 —— "检查更新失败"不该长得像"应用坏了"。
    """
    settings = ctx.settings
    return check_update(
        current=request.app.version,
        enabled=settings.update_check_enabled,
        repo=settings.update_repo,
        release_dir=settings.shell_release_dir,
    ).to_api()
