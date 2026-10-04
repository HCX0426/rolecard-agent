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
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from rolecard_agent.base.identity import DEFAULT_USER_ID, bound_user
from rolecard_agent.config import ModelBackend, Settings
from rolecard_agent.core.bootstrap import Assembly, Runtime, build_runtime
from rolecard_agent.core.nodes import KernelContext, _turn_backend, turn_settings
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
        assert tid == proactive_thread_id("wan", user_id=DEFAULT_USER_ID)

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


def test_deliver_proactive_does_not_interrupt_a_running_turn(tmp_path: Path) -> None:
    """用户那一轮还在图上跑时，主动投递**这次就不做**（审计 #12：他会丢一条消息）。

    成因不是调度策略：`update_state` 读的是"它此刻看到的最新检查点"，而那一轮的
    `graph.stream` 正在往同一个线程追加 —— 两边分叉同一个父节点，后写的盖掉先写的，
    用户刚发的那条就从界面上消失了（检查点里那条分支还在，所以翻不到也说不清）。
    这里用真图真检查点验两件事：拿不到锁 ⇒ 一句都不写；放了锁 ⇒ 正常落进去。
    """
    from rolecard_agent.core.graph import build_graph_config
    from rolecard_agent.core.reachout import proactive_thread_id
    from rolecard_agent.core.thread_locks import (
        release_thread,
        thread_is_busy,
        try_thread_write,
    )
    from rolecard_agent.roles.models import RoleCard

    runtime = _assemble(tmp_path)
    try:
        role = RoleCard(role_id="wan", role_name="苏晚晴", system_prompt="你是苏晚晴。")
        tid = proactive_thread_id("wan", user_id=DEFAULT_USER_ID)

        assert try_thread_write(tid, timeout=0.0)  # 模拟：用户那一轮正在飞
        try:
            assert thread_is_busy(tid)
            assert runtime.deliver_proactive(role, "这句现在不该冒出来") is None
        finally:
            release_thread(tid)

        assert not thread_is_busy(tid)
        assert runtime.deliver_proactive(role, "这轮说完了才说") == tid
        messages = runtime.state["graph"].get_state(
            build_graph_config(tid, runtime.effective)
        ).values["messages"]
        assert [str(m.content) for m in messages] == ["这轮说完了才说"]
    finally:
        runtime.conn.close()


def test_proactive_lines_only_offer_what_has_not_been_settled(tmp_path: Path) -> None:
    """主动开口的上下文只带"还没了结的那一截"—— 一刀两向，断的是两件相反的毛病。

    向他的那一刀（真库实测 2026-09-22，`s_proactive_elysia`）：用户 19:13:29 说「想你了」，
    她 19:13:36 正常答了；调度器随后在 19:31 与 20:35 又各"主动"冒了一句，而两次的素材都是
    那条会话的尾部 6 条 —— 于是两句都还在回 19:13 那句话。用户读到的是"我发一条，它回我两条
    重复的"，而对话永远不往前走。分界线就是她自己最后说过话的位置。

    向她的那一刀（09-26 同一条会话上用户报的"也不管之前的内容"）：只有前一刀时，"她上一条
    还悬着、他一个字没回"在她眼里是**空白**，于是她只会另找一个由头 —— 读起来就是"我还没回话
    呢，她转头说起唱歌"。所以线空着的那一侧必须换成"你说过而他没回"那一段，而不是空串。
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

        # 她刚说完、用户还没吭声 ⇒ 悬着的是**她那句**，素材得让她知道（不是空串）
        hanging = runtime.proactive_recent_lines("wan")
        assert "外头降温了" in hanging and "还没有回" in hanging, hanging
        assert "还没有接过话" not in hanging, "这一段说的是她悬着，不是他悬着"

        push(HumanMessage(content="想你了"))
        lines = runtime.proactive_recent_lines("wan")
        assert "想你了" in lines and "还没有接过话" in lines
        assert "外头降温了" not in lines, "他回过话之后，她那句就不再悬着了"

        push(AIMessage(content="我也想"))  # 答过了 → 他那句立刻从素材里退出去
        assert "想你了" not in runtime.proactive_recent_lines("wan")

        # 工具结果不是"她出口说的话"：既不能当她答过话（会把待接的那句抹掉），
        # 也不能当成她说过的话喂回去（那等于让她以为自己对一段 JSON 说出口过）。
        push(HumanMessage(content="你在哪呢"))
        push(ToolMessage(content='{"ok": true}', tool_call_id="t1"))
        lines = runtime.proactive_recent_lines("wan")
        assert "你在哪呢" in lines, lines
        assert "ok" not in lines, lines
    finally:
        runtime.conn.close()


def test_proactive_window_still_has_material_when_she_has_the_last_word(tmp_path: Path) -> None:
    """「未收尾话题」的扫描读**最近一窗**，不读"没接住那截"（09-26 轮 R26-03 的修法）。

    两者差别不是宽度而是**方向**：这一源要找的是"说到一半没了下文"的事，而那件事往往
    正是她接住过、只是没落地的那件。`proactive_recent_lines` 只给"还没了结"的那一截
    （他那句她没接、或她那句他没回），接过的话一律切掉 —— 拿它当输入，判据与素材是反的，
    实测下来扫描一次也没发生过（生产读数见 `scripts/probe_open_threads_reach.py`）。
    同一份检查点上两个读法必须一个切掉、一个留着，这条钉的就是"分开"这件事本身。
    """
    from langchain_core.messages import AIMessage, HumanMessage

    from rolecard_agent.core.graph import build_graph_config
    from rolecard_agent.roles.models import RoleCard

    runtime = _assemble(tmp_path)
    try:
        role = RoleCard(role_id="wan", role_name="苏晚晴", system_prompt="你是苏晚晴。")
        tid = runtime.deliver_proactive(role, "外头降温了，穿上外套。")
        graph = runtime.state["graph"]
        cfg = build_graph_config(tid, runtime.effective)

        graph.update_state(
            cfg, {"messages": [HumanMessage(content="我下周要体检，结果出来跟你说")]}
        )
        graph.update_state(cfg, {"messages": [AIMessage(content="好，我等你说")]})

        assert "还没有接过话" not in runtime.proactive_recent_lines("wan"), (
            "她已经接过了，那一刀必须切掉他那句 —— 否则同一件事被回两遍"
        )
        window = runtime.proactive_recent_window("wan")
        assert "我下周要体检" in window, window
        assert "好，我等你说" in window, window
        assert "还没有接过话" not in window, "扫描那段不该带'没接住'的措辞，它拿的是整窗"
    finally:
        runtime.conn.close()


# -- 凭据按这一轮的主人（M2d 尾巴的收口）----------------------------------------------

A_KEY = "sk-aaaaaaaaaaaaaaaa"
B_KEY = "sk-bbbbbbbbbbbbbbbbbb"
B_USER = "u2"


def _settings_with_a_local_key(tmp_path: Path) -> Settings:
    """实例主人（`local-user`）那一份走 env 播种，第二个身份走设置页写入。"""
    return Settings(
        sqlite_path=tmp_path / "cred.db",
        chroma_path=tmp_path / "chroma",
        upload_dir=tmp_path / "uploads",
        model_backends={
            "chat": ModelBackend(
                model="Qwen3-8B",
                provider="siliconflow",
                base_url="https://api.siliconflow.cn/v1",
                api_key=A_KEY,
            )
        },
        model_default="chat",
    )


def _record_models(seen: list[Settings]) -> Callable[..., object]:
    """记录型假工厂：把"构建这个模型时收到的是哪份配置"留下来，返回一个可认身份的替身。

    先 `settings.backend(...)` 一次再记录：真工厂（`core.graph.build_model`）就是在这里
    对未知后端名抛 `KeyError`，`resolve_role_model` 的降级分支认的正是那条失败。假工厂
    若不照做，"未知后端降级"那条路径在测试里根本走不到（会静默变成"用假模型跑过去了"）。
    """

    def factory(
        settings: Settings, backend_name: str | None = None, temperature: float | None = None
    ) -> object:
        settings.backend(backend_name)
        seen.append(settings)
        return object()

    return factory


def _save_second_identity(runtime: Runtime) -> None:
    """第二个身份配**自己的一台**后端（`model_backend.name` 是全局唯一主键，所以名字带后缀）。

    这不是测试的凑合，是真实形状：后端名全局唯一 ⇒ B 的组必然与 A 不同名。于是"照旧按
    实例主人那份快照解析"的症状不是张冠李戴，而是 `KeyError` → **降级到 A 的默认模型** ——
    同一件事的另一种表现，同样是他花了别人的 key。
    """
    runtime.model_settings.save(
        user_id=B_USER,
        default="chat-b",
        backends=[
            {
                "name": "chat-b",
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "Qwen3-32B",
                "api_key": B_KEY,
                "usage": "chat",
            }
        ],
    )


def test_a_turn_carries_the_key_of_the_person_who_is_talking(tmp_path: Path) -> None:
    """这一轮花谁的 key 按**本轮主人**取，不按实例主人（M2d 尾巴的收口）。

    收口前：`effective_settings` 已经按人存了配置，但运行期那份快照是**实例级**的 ——
    第二个身份让角色卡指向自己的后端，模型构建时却在第一份快照里找不到它，于是**降级到
    实例主人那台默认模型**：他拿到的是别人的答复，且花的是别人的 key（"每个人花自己的 key"
    只兑现了一半）。`bound_user` 正是图节点入口绑的那一层（既有用例钉着"工具节点读到的是
    这条线程的主人"），所以这里用同一个机制验。
    """
    seen: list[Settings] = []
    runtime = build_runtime(
        domains=DOMAINS,
        query_factory=HealthQueryService,
        registry_factory=_wiring,  # type: ignore[arg-type]
        env_settings=_settings_with_a_local_key(tmp_path),
        model_factory=_record_models(seen),
    )
    try:
        _save_second_identity(runtime)
        mine = runtime.resolve_role_model("chat")  # 不在任何一轮里 ⇒ 实例主人那份
        with bound_user(B_USER):
            theirs = runtime.resolve_role_model("chat-b")
        assert seen[-2].backend("chat").api_key == A_KEY
        last = seen[-1]
        assert B_KEY in repr(last.model_backends), (
            f"B 那一轮没用上自己的 key，最后一次构建拿的是 {sorted(last.model_backends)}"
        )
        assert mine is not theirs
    finally:
        runtime.conn.close()


def test_a_default_backend_belongs_to_the_person_who_is_talking(tmp_path: Path) -> None:
    """没指定后端（`backend_name=None`）那一条短路，从前直接回**实例主人**的默认模型。

    这是最容易漏的一条：它跑在 `resolve_role_model` 的最前面、连工厂都不碰，所以
    "角色卡留空 = 跟着全局走"的人恰恰是**每一轮都在花第一把 key** 的那一群。同一条
    用例顺带钉住缓存仍然在人际复用（同一主人同一后端不重建）—— 缺了主人那一维，
    B 会命中 A 那条 `(None, None)`。
    """
    seen: list[Settings] = []
    runtime = build_runtime(
        domains=DOMAINS,
        query_factory=HealthQueryService,
        registry_factory=_wiring,  # type: ignore[arg-type]
        env_settings=_settings_with_a_local_key(tmp_path),
        model_factory=_record_models(seen),
    )
    try:
        _save_second_identity(runtime)
        first_a = runtime.resolve_role_model("chat")
        assert runtime.resolve_role_model("chat") is first_a, "同一主人同一后端被反复重建"
        with bound_user(B_USER):
            theirs = runtime.resolve_role_model(None)
            assert runtime.resolve_role_model(None) is theirs
        assert theirs is not runtime.state["default_model"], (
            "B 什么都没选，却拿到了实例主人那台默认模型 —— 这条短路没按本轮主人分流"
        )
        assert B_KEY in repr(seen[-1].model_backends)
    finally:
        runtime.conn.close()


def test_a_background_caller_names_the_owner_it_works_for(tmp_path: Path) -> None:
    """跨线程的调用方要**显式**交出主人 —— `bound_user` 是 `ContextVar`，不跨线程传播。

    自动提取跑在"响应流完之后"的后台线程里（`_distill_after_turn` → `_thread_model`）：
    那边 `active_user_id` 一定是空的，所以它必须把这条线程的主人传进来。不传的症状不是报错，
    而是**替别人抽记忆** —— 拿实例主人的凭据跑第二个人的提取，静悄悄，且花的是别人的钱。
    这里不绑 `bound_user`，直接模拟那个后台线程。
    """
    seen: list[Settings] = []
    runtime = build_runtime(
        domains=DOMAINS,
        query_factory=HealthQueryService,
        registry_factory=_wiring,  # type: ignore[arg-type]
        env_settings=_settings_with_a_local_key(tmp_path),
        model_factory=_record_models(seen),
    )
    try:
        _save_second_identity(runtime)
        built = runtime.resolve_role_model("chat-b", user_id=B_USER)
        assert built is not runtime.state["default_model"], "没按传进来的主人解析，降级了"
        assert B_KEY in repr(seen[-1].model_backends)
    finally:
        runtime.conn.close()


def test_an_unknown_backend_degrades_instead_of_raising(tmp_path: Path) -> None:
    """拼错/被删掉的后端名照旧降级（可用性 fail-soft），不因收口而变成 500。

    降级落在那台编译期默认模型上（它是"这台机器上一定跑得起来"的那一份）；留痕里带上
    `user_id`，因为两个身份各配自己的组时，光看后端名分不出是谁那一轮降的级。
    """
    seen: list[Settings] = []
    runtime = build_runtime(
        domains=DOMAINS,
        query_factory=HealthQueryService,
        registry_factory=_wiring,  # type: ignore[arg-type]
        env_settings=_settings_with_a_local_key(tmp_path),
        model_factory=_record_models(seen),
    )
    try:
        _save_second_identity(runtime)
        with bound_user(B_USER):
            degraded = runtime.resolve_role_model("no-such-backend")
        assert degraded is runtime.state["default_model"]
    finally:
        runtime.conn.close()


def test_turn_settings_reads_the_resolver_not_the_build_time_snapshot() -> None:
    """节点侧（能力位那一处）读的是 resolver 那份，不是 `ctx.settings`。

    模型凭据与"这一轮用哪个后端的能力位（工具/视觉）"必须同源：差一个人就会出现
    "拿 B 的 key，按 A 声明的能力判要不要绑工具"。`ctx.settings` 始终是实例主人那份
    （知识库、工具闭包那些设备级读法还在用它），所以这条钉的是**两者没有混用**。
    """
    build_time = Settings(model_backends={"chat": ModelBackend(model="m", api_key="sk-build")})
    per_turn = Settings(model_backends={"chat": ModelBackend(model="m", api_key="sk-turn")})
    ctx = KernelContext(
        model=cast("Any", None),
        registry=cast("Any", None),
        roles=cast("Any", None),
        tracer=cast("Any", None),
        settings=build_time,
    )
    assert turn_settings(ctx) is build_time, "没接 resolver 就该回落构建期那份"
    ctx.settings_resolver = lambda: per_turn
    assert turn_settings(ctx) is per_turn
    assert _turn_backend({"model_name": "chat"}, None, ctx).api_key == "sk-turn"


def test_an_inflight_build_cannot_republish_a_stale_model_after_rebuild(tmp_path: Path) -> None:
    """换装之后，**在飞的**那次模型构造不能把旧配置的产物回填进缓存（`R28-05`）。

    `rebuild` 里那两句 `clear()` 只挡得住"已经进缓存的旧值"，挡不住"清完之后才落笔的旧值"：
    并发那一轮在锁外拿着旧配置快照构造模型，构造要花几百毫秒到几秒，等它写回时 clear 早过去了。
    症状不是报错，是**改了设置不生效** —— 换装完成之后的轮次继续花旧 key、旧 num_ctx、旧温度。
    """
    built: list[object] = []
    stale: list[object] = []
    entered = threading.Event()
    release = threading.Event()

    def factory(_settings: object, name: object = None, *_a: object, **_k: object) -> object:
        model = object()  # 每次都造一个新对象，好认"谁进了缓存"
        built.append(model)
        if name == "whatever":
            # 只有并发那一轮那一发停在构造中间，好把 rebuild 插进去
            # （装配期自己也会构造默认模型，别把门挂在那一发上 —— 第一版就挂错了地方）。
            stale.append(model)
            entered.set()
            assert release.wait(5), "放行没来 —— 用例本身卡死了"
        return model

    runtime = build_runtime(
        domains=DOMAINS,
        query_factory=HealthQueryService,
        registry_factory=_wiring,  # type: ignore[arg-type]
        env_settings=_settings(tmp_path),
        model_factory=factory,
    )
    try:
        thread = threading.Thread(
            target=lambda: runtime.resolve_role_model(
                "whatever", 0.5, user_id=DEFAULT_USER_ID
            )
        )
        thread.start()
        assert entered.wait(5), "并发那次构造没开始"

        runtime.rebuild()          # 用户在这一刻改了设置
        release.set()
        thread.join(5)
        assert not thread.is_alive(), "在飞的那次构造没跑完"
        assert len(built) >= 2, "rebuild 自己没构造过默认模型？"

        assert stale, "并发那一轮根本没走到构造"
        assert not any(m is stale[0] for m in runtime.role_models.values()), (
            "旧配置产物活过了这次换装 —— 缓存回填没被代数挡住，改了设置就是不生效"
        )
    finally:
        runtime.conn.close()
