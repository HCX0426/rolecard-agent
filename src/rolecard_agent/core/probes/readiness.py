"""就绪深探：真去问一遍几个真依赖，而不是回一句 `status=ok`。

2026-10-04 审查快照「探活端点不探任何真实依赖」那一格的前半。装配根里那条探活回的是
"进程活着"，它必须**便宜**且**免鉴权**（容器与反代 15s 问一次），所以它不该去开向量库、
扫数据库 —— 代价是"活着"与"能用"之间没有判据：chroma 目录被改坏之后那条探活照样 200，
而第一次上传才炸。

分工因此是两句话：

  * 免鉴权那条（秒级）：进程活着 + 版本 + 上限常量 —— 调用方是编排器；
  * 深探那条（操作员）：**真依赖**开不开 —— sqlite `quick_check`、向量库心跳、模型配置
    存在性。慢，但只在排障时被调用 —— 调用方是人。

**本模块里不出现任何域名**（`core/ no domain token` 那条尺子盯着）：它讲的是"依赖就绪"，
不是某个业务域；深探端点的路径与门禁归 `api/routers/` 那半边写。

纪律与 `base/probes.py` 同一条：**任何异常都折成一条 detail，绝不往上抛**。深探的调用方正在
排障，它要的是一张表，不是一个 500。
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any

#: 每条探针 detail 的上限：异常文本可能很长（向量库会把路径与栈一起塞进来），截断是为了
#: 让这张表还能被人读。截断**不隐藏失败** —— `ok=False` 就写在它旁边。
_DETAIL_MAX = 300


def _detail(text: object) -> str:
    return str(text)[:_DETAIL_MAX]


def _sqlite_check(path: Path) -> dict[str, Any]:
    """只读开一次库跑 `PRAGMA quick_check` —— 不碰应用那条连接（它在别的线程上）。

    `mode=ro` 是刻意的：深探**绝不**写库，也不该因为"顺手建了个空库"而报绿。
    """
    if not path.exists():
        return {"ok": False, "detail": f"库文件不存在：{path}"}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 探针的返回值就是结论
        return {"ok": False, "detail": _detail(f"{type(exc).__name__}: {exc}")}
    verdict = str(row[0]) if row else "?"
    return {"ok": verdict.strip().lower() == "ok", "detail": f"quick_check={verdict}"}


def _chroma_check(path: Path) -> dict[str, Any]:
    """打开向量库并问一次心跳。

    懒 import：chromadb 在 `requirements-rag.txt`（可选那一族），没装时这一条要明说
    "知识库不可用"，而不是让一个 ImportError 把整张表带走。
    """
    try:
        import chromadb
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": _detail(f"chromadb 没装（知识库不可用）：{exc}")}
    try:
        client = chromadb.PersistentClient(path=str(path))
        try:
            beat = client.heartbeat()
        finally:
            # 关掉（本地客户端会在**进程级注册表**里留一条 system 记录，见 retriever 的
            # `R102-74` 注释）。深探会被反复调用，不收句柄就会攒出一串 ResourceWarning ——
            # 而"零 warnings 摘要"是本仓门禁的口径，探针不该是它的例外。
            close = getattr(client, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": _detail(f"{type(exc).__name__}: {exc}")}
    return {"ok": True, "detail": f"heartbeat={beat}"}


def _models_check(backends: list[dict[str, object]] | None) -> dict[str, Any]:
    """模型配置存在性：至少有一行配置。

    **只问配置在不在，不发网络请求** —— "这个模型现在答不答得上话"是服务页探活的事
    （`core/services.check_availability` + `base/probes`），深探不该在这里再实现一份。
    `None` = 读配置本身失败了，与"一条都没配"是两件事，分开说。
    """
    if backends is None:
        return {"ok": False, "detail": "模型配置读不出来（异常已落日志）"}
    if not backends:
        return {"ok": False, "detail": "一条模型配置都没有"}
    return {"ok": True, "detail": f"{len(backends)} 条模型配置"}


def readiness_report(
    *,
    sqlite_path: str | Path,
    chroma_path: str | Path,
    model_backends: list[dict[str, object]] | None,
) -> dict[str, Any]:
    """三项全过才算 `ok`；否则 `degraded`（并逐项说明谁不行）。"""
    checks = {
        "sqlite": _sqlite_check(Path(sqlite_path)),
        "chroma": _chroma_check(Path(chroma_path)),
        "models": _models_check(model_backends),
    }
    ok = all(bool(item["ok"]) for item in checks.values())
    return {"status": "ok" if ok else "degraded", "checks": checks}
