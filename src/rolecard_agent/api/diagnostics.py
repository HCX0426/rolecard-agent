"""P2 观测最小版的 api 面：未捕获异常出口记账 + 一键诊断包。

两格同住一个模块的理由：它们回答的是同一个问题（"这台机器刚才出了什么事"），
拆两处住，就会有一处忘改 —— 与本仓"一个事实一个住址"的纪律同族。

**未捕获异常出口**（`install_unhandled_recorder`）：
FastAPI/Starlette 对没注册 handler 的异常回**纯文本** "Internal Server Error" —— 那是
`errors.py` 收口前"错误响应裂成两半"的第三个半（中间件、注册表之外，泛型 500 还裸着）。
这一格既把它收进 `error_response` 的唯一正文工厂（前端 `readableDetail` 能读出中文），
也顺手把异常登记进 `base/error_signatures`（签名 + error.jsonl）。
刻意**不**在响应里带签名与栈帧：堆栈只进日志不进返回值（铁律），返回值只说"已记录"。

**诊断包**（`GET /api/diagnostics/export`）：
zip 收四样 —— manifest.json（版本指纹 + 非敏感环境摘要）、error.jsonl（签名历史）、
recent.json（窗口内 {签名: 最近时间}）、双日志副本（backend.log / shell.log，找得到的）。
三条闸：
* 门禁 = require_operator（与 `/api/health/deep` 同一条依赖，不留两处真相）；
* 隐私 = **名单制**：manifest 只写白名单字段，**从不**整包 dump 环境变量 ——
  凭据 / API key 这类秘密住的环境变量不该被"导出运行环境"顺手带走；
  密钥形状的名字只报"设没设"，不报值；
* 审计 = 导出动作记 audit_log（谁在什么时候导出现场，与其余操作员动作同族）。

日志位置：打包态壳把双日志落在 `%APPDATA%\\rolecard-agent`（Electron userData），
后端**读得到**（同一台机器同一个用户），按默认位置找即可，不用壳配合；
`ROLECARD_LOG_DIR` 给开发态/测试一个显式指路的机会。找不到就在 inventory 里如实记缺 ——
"包里没有日志"与"假装收齐了"是两件事。
"""

from __future__ import annotations

import io
import json
import os
import platform
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from rolecard_agent.api.auth import Actor
from rolecard_agent.api.deps import AppContext, get_context
from rolecard_agent.api.errors import error_response
from rolecard_agent.api.routers.health import require_operator
from rolecard_agent.base import error_signatures
from rolecard_agent.base.app_identity import APP_NAME
from rolecard_agent.base.paths import user_data_root
from rolecard_agent.core.build_info import read_build_info


def install_unhandled_recorder(app: FastAPI) -> None:
    """把"没注册的异常"接进签名记账 + 统一错误正文（create_app 调一次）。"""

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        error_signatures.record(exc)
        return error_response(
            500, "服务端出了没预料的错，现场已记录 —— 排障可导诊断包（设置页「导出诊断包」）。"
        )


router = APIRouter()


def _shell_log_dir() -> Path | None:
    """壳的双日志目录：`ROLECARD_LOG_DIR` 覆盖，否则 Windows 的 Roaming 默认位。"""
    override = (os.environ.get("ROLECARD_LOG_DIR") or "").strip()
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
    return None


#: 环境摘要的名单制白名单（值可进包的那几样：不含任何秘密）。
#: 名单制而不是"滤掉已知敏感键"：新加的秘密键默认落在名单外（fail-closed）。
_SAFE_ENV_KEYS = (
    "AUTH_MODE",
    "DATA_ROOT",
    "RUN_API_PORT",
    "OLLAMA_HOST",
    "ROLECARD_PET",
)


def _env_summary() -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            summary[key] = value
    # 云端观测开关：名字本身无害，值可能有 key —— 只报"设没设"。
    for key in ("LANGSMITH_API_KEY", "LANGFUSE_SECRET_KEY"):
        if os.environ.get(key):
            summary[key] = "set"
    return summary


#: 单文件进包上限：诊断包不该把磁盘塞满；超限截尾保留"最近"那一半。
_MAX_FILE_BYTES = 2 * 1024 * 1024


def build_diagnostics_zip(request: Request | None = None) -> bytes:
    """组诊断包（不落盘，内存里 zip）。测试与端点共用这一个组装点。"""
    buf = io.BytesIO()
    logs_dir = _shell_log_dir()
    present: dict[str, bool] = {}
    manifest: dict[str, Any] = {
        "app": APP_NAME,
        # 版本从装配根现取（`update_check` 同一条口径：不抄第二个版本来源）。
        "version": request.app.version if request is not None else "unknown",
        "build": read_build_info().as_dict(),
        "python": sys.version,
        "platform": platform.platform(),
        "data_root": str(user_data_root()),
        "logs_dir": str(logs_dir) if logs_dir else None,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "env": _env_summary(),
    }
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for label, path in (
            ("errors/error.jsonl", error_signatures.log_path()),
            ("logs/backend.log", (logs_dir / "backend.log") if logs_dir else None),
            ("logs/shell.log", (logs_dir / "shell.log") if logs_dir else None),
        ):
            if path is None or not path.exists():
                present[label] = False
                continue
            present[label] = True
            data = path.read_bytes()
            if len(data) > _MAX_FILE_BYTES:
                data = "...(截去头部, 保留最近 2MiB)\n".encode() + data[-_MAX_FILE_BYTES:]
            zf.writestr(label, data)
        zf.writestr(
            "errors/recent.json",
            json.dumps(error_signatures.recent_signatures(), ensure_ascii=False, indent=2),
        )
        zf.writestr(
            "inventory.json",
            json.dumps(
                {
                    "present": present,
                    "note": "缺失 = 本机没有该现场（开发态没有壳日志是常态），不是导出失败",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return buf.getvalue()


@router.get("/api/diagnostics/export", dependencies=[Depends(require_operator)])
def export_diagnostics(
    request: Request,
    ctx: AppContext = Depends(get_context),
    actor: Actor = Depends(require_operator),
) -> Response:
    """一键诊断包（zip）—— 用户报 bug 时附带现场（P2 验收：能一键产出）。"""
    blob = build_diagnostics_zip(request)
    ctx.audit.log(
        actor=actor.id,
        action="export_diagnostics",
        target="diagnostics.zip",
        detail={"bytes": len(blob), "recent_errors": error_signatures.recent_count()},
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"{APP_NAME}-diagnostics-{stamp}.zip"
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
