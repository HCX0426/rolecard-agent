"""未捕获异常的签名计数落盘（P2 观测最小版的第一格）。

为什么需要（开发流程.md P2）：本仓的观测面在"运行时出了什么事"这一环最薄 ——
backend.log 每次启动覆盖、深探只答"依赖开不开"，一次未捕获异常炸过就没了痕迹，
用户报 bug 时双方都看不见现场。这一格把三件事接起来：**每次未捕获异常落一行可聚合的
签名 + 进程保留 24h 窗口计数 + 一键诊断包收走全部现场**（诊断包在 `api/diagnostics.py`）。

三条硬规矩（形状都来自本仓既有纪律）：

* **只记签名与栈帧坐标，不记消息**：异常消息可能带着病历正文、文件路径、用户输入 ——
  与"审计只记路径不记内容"（架构总览 §5）同族。签名 = sha1(类型 + 最近一帧 file:line)
  前 12 位：同一处炸第二次签名必相同（可聚合），消息里的 id 变化不影响签名。
* **观测失败不许带走被观测的**：落盘炸（目录不可写/磁盘满）时 record 照常返回签名、
  计数照常 —— 错误处理函数再炸一次是"故障放大"的教科书形状，本仓不写。
* **进程内窗口，重启归零**：health 里的 `error_count_recent` 答"现在要不要看一眼"，
  历史去 jsonl 里查（追加式，不在这里做截断 —— 它的生命周期由诊断包与保留策略说话）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

#: 滑窗长度（秒）：health 的 error_count_recent 数的是这窗口内的记录。
RECENT_WINDOW_SECONDS = 24 * 3600

#: 签名长度：12 位十六进制对"异常点位"这种小空间足够唯一，又不至于把栈帧原样摊出来。
SIG_LENGTH = 12

_lock = threading.Lock()
#: 签名 → 最近一次记录时间（epoch 秒）。只留窗口内的，过期即弃。
_recent: dict[str, float] = {}

#: 落盘位置（进程内唯一真相，测试用 reset_for_tests 挪进 tmp_path）。
_log_path: Path = Path("data") / "errors" / "error.jsonl"
_clock: Callable[[], float] = time.time


#: 数据根下这一族的目录名（`data/errors/`）。提成模块级常量是给一致性尺子
#: `data root dirs are gitignored` 数的 —— 与 `storage/db.py::RETENTION_BACKUP_DIRNAME`
#: 同款形状：运行时往数据根写目录，目录名必须以**可被静态数到的常量**存在，
#: 尺子才能问"它被 .gitignore 管住了没有"（开发态数据根就是仓库 data/，
#: 错误签名带栈帧文件名行号，同样是绝不入库的运行时件）。
ERROR_DIRNAME = "errors"


def default_log_path() -> Path:
    """默认落盘位置：`ROLECARD_ERROR_LOG` 覆盖，否则数据根下 `errors/error.jsonl`。

    数据根问 `base.paths.user_data_root()`（与 sqlite/chroma 同根：开发态仓库 `data/`、
    冻结态 `%LOCALAPPDATA%`）。刻意不把路径挂进 Settings —— 它不是用户配置项，
    是运维现场的位置；配置面每多一格，一致性尺子与 example 就多背一格债。
    """
    override = (os.environ.get("ROLECARD_ERROR_LOG") or "").strip()
    if override:
        return Path(override)
    from rolecard_agent.base.paths import user_data_root

    return user_data_root() / ERROR_DIRNAME / "error.jsonl"


def _signature(exc: BaseException) -> tuple[str, str]:
    """(签名, 栈帧坐标)。坐标取**最近一帧**（炸点本身），格式 `file.py:line`。

    没有 traceback（从没抛过的异常实例）时退化成只按类型 —— 那已经是"形状相同"的
    另一说，聚合到一类是诚实而不是精度损失。
    """
    frame = ""
    tb = exc.__traceback__
    while tb is not None:
        frame = f"{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}"
        tb = tb.tb_next
    raw = f"{type(exc).__name__}|{frame}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:SIG_LENGTH], frame


def record(exc: BaseException) -> str:
    """登记一次未捕获异常：返回签名，追加一行 JSONL，更新滑窗。永不抛。"""
    sig, frame = _signature(exc)
    now = _clock()
    with _lock:
        _recent[sig] = now
        _prune(now)
        try:
            _log_path.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
                "sig": sig,
                "type": type(exc).__name__,
                "at": frame,
            }
            with _log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            # 规矩②：观测失败不许带走被观测的。计数与签名仍然有效（进程内的真相
            # 比磁盘的真相新，这可以接受；反过来不行）。
            pass
    return sig


def recent_count() -> int:
    """24h 窗口内的记录总数（health 的 `error_count_recent` 读数）。"""
    now = _clock()
    with _lock:
        _prune(now)
        return len(_recent)


def log_path() -> Path:
    """当前落盘位置（诊断包收它；生产值是导入时算的 default_log_path()）。"""
    return _log_path


def recent_signatures() -> dict[str, float]:
    """窗口内 {签名: 最近时间} —— 诊断包点名"哪几处最近炸过"用。"""
    now = _clock()
    with _lock:
        _prune(now)
        return dict(_recent)


def _prune(now: float) -> None:
    """丢窗口外的键。调用方须持锁。"""
    cutoff = now - RECENT_WINDOW_SECONDS
    for sig in [s for s, ts in _recent.items() if ts < cutoff]:
        _recent.pop(sig, None)


def reset_for_tests(path: Path | None = None, clock: Callable[[], float] | None = None) -> None:
    """把落盘位置与时钟挪进测试世界（**只在测试里调** —— 生产路径由导入时初始化）。"""
    global _log_path, _clock
    with _lock:
        _recent.clear()
        _log_path = path if path is not None else default_log_path()
        if clock is not None:
            _clock = clock


reset_for_tests()
