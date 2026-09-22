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
import re
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


def test_deliver_proactive_lands_in_the_roles_thread(tmp_path: Path) -> None:
    """主动开口必须真的落进"该角色的主动会话"—— 这是"能回复 / 能翻历史"的地基。

    用装配出来的**真图与真检查点**验（不起 app）：只验 SQL 行的话，checkpoint 那条腿
    断了也照样绿，而用户碰到的正是"点进去是空的、回不了"。
    """
    from rolecard_agent.core.graph import build_graph_config
    from rolecard_agent.core.reachout import proactive_thread_id
    from rolecard_agent.roles.models import RoleCard

    runtime = _assemble(tmp_path)
    try:
        role = RoleCard(role_id="wan", role_name="苏晚晴", system_prompt="你是苏晚晴。")
        tid = runtime.deliver_proactive(role, "今天腰还酸吗？")
        assert tid == proactive_thread_id("wan")

        row = runtime.conn.execute(
            "SELECT current_role_id, user_id, title FROM session_thread WHERE thread_id = ?",
            (tid,),
        ).fetchone()
        # 绑角色（回复时 persona 才对）+ 绑演示用户（会话列表才看得见）
        assert row["current_role_id"] == "wan" and row["user_id"] == DEFAULT_USER_ID
        assert "主动找你" in str(row["title"])

        graph = runtime.state["graph"]
        config = build_graph_config(tid, runtime.effective)
        messages = graph.get_state(config).values["messages"]
        assert [str(m.content) for m in messages] == ["今天腰还酸吗？"]
        # 主动投递也要带时间：没带的话，回放里她"什么时候说的"就查不出来，
        # 而"她是不是一句话说了好几遍"只能靠时间分辨（2026-09-22 取证时只能靠 id 前缀猜）。
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
            str(messages[0].additional_kwargs.get("created_at") or ""),
        ), "主动投递的消息没带 created_at"

        # 第二条：建行幂等（不冲突），历史按顺序累积 —— 角色下一次看得见自己说过什么。
        runtime.deliver_proactive(role, "记得喝水")
        assert len(graph.get_state(config).values["messages"]) == 2
    finally:
        runtime.conn.close()


def test_proactive_lines_only_offer_what_she_has_not_picked_up(tmp_path: Path) -> None:
    """主动开口的上下文只带"她还没接住的那几句"—— 这条断的是"一句话回三遍"。

    真库实测（2026-09-22，`s_proactive_elysia`）：用户 19:13:29 说「想你了」，她 19:13:36
    正常答了；调度器随后在 19:31 与 20:35 又各"主动"冒了一句，而两次的素材都是那条会话的
    尾部 6 条 —— 于是两句都还在回 19:13 那句话。用户读到的是"我发一条，它回我两条重复的"，
    而对话永远不往前走。分界线就是她自己最后说过话的位置，所以这里连**真图真检查点**一起验。
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from rolecard_agent.core.graph import build_graph_config
    from rolecard_agent.roles.models import RoleCard

    runtime = _assemble(tmp_path)
    try:
        role = RoleCard(role_id="wan", role_name="苏晚晴", system_prompt="你是苏晚晴。")
        tid = runtime.deliver_proactive(role, "外头降温了，穿上外套。")
        graph = runtime.state["graph"]
        config = build_graph_config(tid, runtime.effective)

        def push(*items: object) -> None:
            graph.update_state(config, {"messages": list(items)})

        # 她刚说完、用户还没吭声 ⇒ 没有待接的话，这次开口只能另找由头（记忆 / 时间 / 文件事件）
        assert runtime.proactive_recent_lines("wan") == ""

        push(HumanMessage(content="想你了"))
        lines = runtime.proactive_recent_lines("wan")
        assert "想你了" in lines and "还没有接过话" in lines

        push(AIMessage(content="我也想"))  # 答过了 → 那句立刻从素材里退出去
        assert runtime.proactive_recent_lines("wan") == ""

        # 工具结果不是"她出口说的话"：既不能当她答过话（会把待接的那句抹掉），
        # 也不能当成她说过的话喂回去（那等于让她以为自己对一段 JSON 说出口过）。
        push(HumanMessage(content="你在哪呢"))
        push(ToolMessage(content='{"ok": true}', tool_call_id="t1"))
        lines = runtime.proactive_recent_lines("wan")
        assert "你在哪呢" in lines, lines
        assert "ok" not in lines, lines
    finally:
        runtime.conn.close()
