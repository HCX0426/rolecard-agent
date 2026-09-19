"""装配根（core/bootstrap.py）的单元测试 —— C/S 壳复用内核的**就绪判据**。

架构审计报告 §7 说 D 桌宠化的唯一实质障碍是"装配根只在 api/main.py"。那是一句会腐烂的
断言，除非有人把它钉住。这里钉两件事：

  1. `core.bootstrap` 引不到任何 HTTP 框架（fastapi / starlette / uvicorn）—— 否则
     "桌面壳零改动复用内核"就是空话；
  2. 不起 app 也能装配出一个可对话的内核（注册表 + 编译好的图 + 热重建会换装全部可变量）。

全部离线：临时 sqlite + 注入的假模型工厂（图只在 invoke 时用模型），绝不连真实后端。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from rolecard_agent.config import Settings
from rolecard_agent.core.bootstrap import Assembly, Runtime, build_runtime
from rolecard_agent.core.identity import DEFAULT_USER_ID
from rolecard_agent.domains.health.service import HealthQueryService
from rolecard_agent.domains.registry import DOMAINS, build_registry
from rolecard_agent.storage.db import bootstrap as apply_schema
from rolecard_agent.storage.db import connect

_HTTP_PACKAGES = ("fastapi", "starlette", "uvicorn")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        sqlite_path=tmp_path / "headless.db",
        chroma_path=tmp_path / "chroma",
        upload_dir=tmp_path / "uploads",
    )


def _wiring(
    assembly: Assembly,
    settings: Settings,
    knowledge: object,
    enabled_domains: object,
) -> object:
    """最小宿主接线：与 `api/main.py` 的 `_host_registry_factory` 同形，但不碰 HTTP。"""
    return build_registry(
        roles=assembly.roles,
        ingestion=assembly.ingestion,
        query=assembly.query,
        knowledge=knowledge,  # type: ignore[arg-type]
        enabled_domains=enabled_domains,  # type: ignore[arg-type]
        current_user=lambda: DEFAULT_USER_ID,
        upload_dir=settings.upload_dir,
        settings=settings,
        tracer=assembly.tracer,
        memory_conn=assembly.conn,
        fs_conn=assembly.conn,
    )


def _assemble(tmp_path: Path) -> Runtime:
    return build_runtime(
        domains=DOMAINS,
        query_factory=HealthQueryService,
        registry_factory=_wiring,  # type: ignore[arg-type]
        env_settings=_settings(tmp_path),
        model_factory=lambda *_a, **_k: None,
    )


def test_core_bootstrap_pulls_in_no_http_framework() -> None:
    """在**干净子进程**里导入装配根，看它把哪些顶层包拖进来。

    必须在子进程里测：同进程的测试套件早就 import 过 fastapi，父进程里查 sys.modules
    永远查不出"bootstrap 自己引了它"这件事。
    """
    probe = (
        "import sys; import rolecard_agent.core.bootstrap;"
        "print(','.join(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    root = Path(__file__).resolve().parents[2]

    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    top_level = set(proc.stdout.strip().split(","))
    leaked = sorted(top_level & set(_HTTP_PACKAGES))
    assert not leaked, f"core.bootstrap 引到了 HTTP 框架：{leaked}"


def test_kernel_assembles_without_an_app(tmp_path: Path) -> None:
    runtime = _assemble(tmp_path)
    try:
        assert runtime.state["graph"] is not None, "没起 app 就该没有图 —— 装配根漏了东西"
        # 内核工具 + 各域工具都在（启用域是闭包，插件启停实时生效）。
        assert runtime.registry.names(), "注册表是空的：宿主接线没生效"
        assert isinstance(runtime.query, HealthQueryService)
        # 模型页 DB 为空表 ⇒ 有效配置此刻与 env 一致；两者仍是**不同对象**，
        # 重建时换的是 effective，env 快照不受污染。
        assert runtime.effective.sqlite_path == runtime.env_settings.sqlite_path
        assert runtime.effective is not runtime.env_settings
    finally:
        runtime.conn.close()


def test_rebuild_swaps_every_mutable_slot(tmp_path: Path) -> None:
    """热重建之后可变引用必须**全部**换新 —— 少换任何一个就是"改了不生效"的接缝。"""
    runtime = _assemble(tmp_path)
    try:
        before_query = runtime.query
        before = (runtime.effective, runtime.knowledge, runtime.registry, runtime.state["graph"])
        runtime.rebuild()
        after = (runtime.effective, runtime.knowledge, runtime.registry, runtime.state["graph"])
        assert all(new is not old for new, old in zip(after, before, strict=True))
        # 域查询服务只持有连接，是稳定引用：不随重建换（换了反而会丢掉在途请求的引用）。
        assert runtime.query is before_query
    finally:
        runtime.conn.close()


def test_assembly_seeds_schema_and_demo_identity(tmp_path: Path) -> None:
    """建表与播种身份归装配根：宿主不该自己补一刀。重复装配同库也必须无害。"""
    db = tmp_path / "seed.db"
    apply_schema(connect(db), enabled_domains=DOMAINS)  # 宿主先建过一次也不冲突
    settings = Settings(sqlite_path=db, chroma_path=tmp_path / "c", upload_dir=tmp_path / "u")
    for _ in range(2):
        runtime = build_runtime(
            domains=DOMAINS,
            query_factory=HealthQueryService,
            registry_factory=_wiring,  # type: ignore[arg-type]
            env_settings=settings,
            model_factory=lambda *_a, **_k: None,
        )
        runtime.conn.close()

    conn = connect(db)
    try:
        tables = {
            str(r["name"])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"ingestion_task", "plugin", "role_card", "medical_report"} <= tables
        users = {str(r["user_id"]) for r in conn.execute("SELECT user_id FROM app_user")}
        assert DEFAULT_USER_ID in users
    finally:
        conn.close()
