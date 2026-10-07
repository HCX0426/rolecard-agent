"""启动各步耗时分布：真库副本上给 `build_runtime` 的每一段打点（审查快照 PERF-8 的"先测量"）。

要回答的问题（动手之前必须有数，不能拍脑袋改）：

* 每次启动全量重建的"声明形状内存探针"（`_declared_columns`：整套 DDL 在 :memory: 里
  跑一遍）到底花多少毫秒；
* `reconcile_columns` 两遍（DDL 前/后）各花多少 —— 审查快照提议的修法是
  "user_version + schema hash 命中时跳过第二遍"，跳过值不值、跳过后还剩什么大头，
  由这份读数说了算；
* 收口链（`make_checkpointer`：补钟 → 一次性收口 → 常态修剪 → 空页回收 → WAL 截断）
  串行各段的占比；
* 其余装配段（插件播种 / retention / 知识库 / 注册表）的占比。

打法：**monkeypatch 计时器包住真实函数**再调 `create_app` —— 不复制一份 build_runtime
的步骤序列（那会随实现漂移，量到的是"探针自己写的那份"）。注意几个接线事实：被计时的
函数经**调用方模块的绑定**被引用（`core.bootstrap.apply_schema`、`api.main.build_runtime`
都是 `from … import` 进来的独立名字），补丁必须打在**那一份绑定**上；而同模块内部互调
（`bootstrap()` 里调 `_declared_columns`）走模块全局表，打在定义处即生效。

真库只读，一切写在副本上。

跑法：

    .venv/Scripts/python.exe scripts/forensics/probe_startup_timing.py [次数]

默认 5 次（同一份副本上反复装配 —— bootstrap 幂等，且正是生产每次开机走的那条路）。
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

os.environ.setdefault("NO_PROXY", "*")
# 副本上不需要把 8B 钉进显存（那会真打一次 Ollama）。
os.environ["MODEL_PIN_ON_STARTUP"] = "false"

# Windows 控制台默认 GBK：本探针打中文表头，不重配编码会抛 UnicodeEncodeError
# （`console encoding` 那把尺子盯的就是这一族）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "forensics"))

import scratch_db  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

from rolecard_agent.api import main as api_main  # noqa: E402
from rolecard_agent.base.observability import TraceEvent  # noqa: E402
from rolecard_agent.core import bootstrap as core_bootstrap  # noqa: E402
from rolecard_agent.core.storage import checkpointer as core_checkpointer  # noqa: E402
from rolecard_agent.storage import db as storage_db  # noqa: E402

COPY = ROOT / "build" / "scratch-startup.db"


class StubModel:
    """记录型替身：不联网、不花钱，只让 `create_app` 的模型装配段走到真形状。"""

    def invoke(self, input: Any, **kwargs: Any) -> AIMessage:  # noqa: A002 - 对齐 ChatLike 签名
        return AIMessage(content="")

    def stream(self, input: Any, **kwargs: Any) -> Any:  # noqa: A002 - 对齐 ChatLike 签名
        yield AIMessage(content="")

    def bind(self, *_a: Any, **_k: Any) -> StubModel:
        return self

    def bind_tools(self, *_a: Any, **_k: Any) -> StubModel:
        return self

    def with_structured_output(self, *_a: Any, **_k: Any) -> StubModel:
        return self


class Recorder:
    def emit(self, event: TraceEvent) -> None:  # noqa: ARG002 - 只求形状齐
        return None


#: 计时点：`(打补丁的模块, 函数名) -> 报表里的名字`。理由见模块 docstring 的接线段。
TARGETS: list[tuple[Any, str, str]] = [
    # storage 内部互调：打在定义处（bootstrap() 经模块全局表调它们）
    (storage_db, "_check_schema_generation", "① 代际检查"),
    (storage_db, "_declared_columns", "② 声明形状内存探针（DDL 全套跑 :memory:）"),
    # core.bootstrap 侧的独立绑定：apply_schema 是 from-import 的别名
    (core_bootstrap, "apply_schema", "④ apply_schema（含 ①②③ 与 DDL 脚本执行）"),
    (core_bootstrap, "seed_plugin_rows", "⑤ 插件播种"),
    (core_bootstrap, "prune_retention_tables", "⑥ retention 清理"),
    (core_bootstrap, "build_knowledge", "⑦ 知识库装配"),
    (core_bootstrap, "heal_knowledge_sources", "⑧ 知识来源回填"),
    (core_bootstrap, "assemble_registry", "⑨ 注册表装配"),
    (core_bootstrap, "make_checkpointer", "⑮ make_checkpointer（⑩-⑭ 的总和容器）"),
    # checkpointer 内部互调：打在定义处
    (core_checkpointer, "ensure_checkpoint_clock", "⑩ 检查点补钟"),
    (core_checkpointer, "compact_backlog_once", "⑪ 祖先快照一次性收口"),
    (core_checkpointer, "prune_checkpoints", "⑫ 检查点常态修剪"),
    (core_checkpointer, "reclaim_if_fragmented", "⑬ 空页回收"),
    (core_checkpointer, "truncate_wal_at_boot", "⑭ WAL 开机截断"),
    # api.main 侧的独立绑定：create_app 调的是它自己那份
    (api_main, "build_runtime", "⓪ build_runtime 全程"),
]


def _one_round() -> dict[str, list[float]]:
    """跑一轮 `create_app`（= 一次完整装配），返回各计时点的耗时（毫秒）。"""
    calls: dict[str, list[float]] = defaultdict(list)
    originals: list[tuple[Any, str, Any]] = []

    def wrap(label: str, orig: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                return orig(*args, **kwargs)
            finally:
                calls[label].append((time.perf_counter() - t0) * 1000)

        return wrapper

    # reconcile_columns 按调用次序分两遍贴标签（bootstrap 固定调两遍：DDL 前/后）。
    reconcile_n = 0

    def reconcile_wrap(*args: Any, **kwargs: Any) -> Any:
        nonlocal reconcile_n
        reconcile_n += 1
        tag = "③a reconcile（DDL 前）" if reconcile_n % 2 == 1 else "③b reconcile（DDL 后）"
        t0 = time.perf_counter()
        try:
            return orig_reconcile(*args, **kwargs)
        finally:
            calls[tag].append((time.perf_counter() - t0) * 1000)

    orig_reconcile = storage_db.reconcile_columns
    originals.append((storage_db, "reconcile_columns", orig_reconcile))
    storage_db.reconcile_columns = reconcile_wrap

    for mod, name, label in TARGETS:
        orig = getattr(mod, name)
        originals.append((mod, name, orig))
        setattr(mod, name, wrap(label, orig))

    try:
        app = api_main.create_app(  # noqa: PLC0415 - 就是要它走补丁过的绑定
            sqlite_path=COPY, model=StubModel(), tracer=Recorder()
        )
        # 装配在 create_app 内部就完成（lifespan 只管预热与关机）；读穿视图上取 Runtime 收口。
        app.state.ctx.runtime.shutdown()
    finally:
        for mod, name, orig in originals:
            setattr(mod, name, orig)
    return calls


def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    src = scratch_db.resolve_live_db()
    scratch_db.copy_of_live_db(COPY, src)
    size_mb = COPY.stat().st_size / 1024 / 1024
    print(f"源库 = {src}\n副本 = {COPY}（{size_mb:.1f} MB）× {rounds} 轮装配")

    all_rounds: list[dict[str, list[float]]] = []
    for i in range(rounds):
        all_rounds.append(_one_round())
        print(f"  第 {i + 1}/{rounds} 轮完成")

    labels: list[str] = []
    for rd in all_rounds:
        for label in rd:
            if label not in labels:
                labels.append(label)

    print(f"\n{'段':34}{'中位数':>10}{'最小':>10}{'最大':>10}")
    for label in labels:
        samples = [v for rd in all_rounds for v in rd.get(label, [])]
        if not samples:
            continue
        print(
            f"{label:34}{statistics.median(samples):10.1f}"
            f"{min(samples):10.1f}{max(samples):10.1f}"
        )

    totals = [sum(rd.get("⓪ build_runtime 全程", [0])) for rd in all_rounds]
    print(
        f"\nbuild_runtime 全程中位数 = {statistics.median(totals):.1f} ms（验收线 2000 ms）"
    )
    recon_a = [v for rd in all_rounds for v in rd.get("③a reconcile（DDL 前）", [])]
    recon_b = [v for rd in all_rounds for v in rd.get("③b reconcile（DDL 后）", [])]
    if recon_a and recon_b:
        print(
            f"reconcile 段：DDL 前 {statistics.median(recon_a):.1f} ms + "
            f"DDL 后 {statistics.median(recon_b):.1f} ms = "
            f"{statistics.median(recon_a) + statistics.median(recon_b):.1f} ms"
            "（验收线合计 <50 ms）"
        )


if __name__ == "__main__":
    main()
