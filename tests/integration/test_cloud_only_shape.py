"""运行形态 A 的另一半承诺：这台机器**根本没装 Ollama**，产品也必须照常可用。

用户 2026-09-20 明确问的就是这件事："别人直接接其他厂的 API key 用，现在支持吗？"
答案是要成立，就不能有任何模块悄悄假设"默认后端是 native"。

为什么单独一个文件、而且是 integration：这条承诺横跨装配根、启动预热、本地服务地址解析、
主动开口与会话投递四个模块。拆进各模块的单测里，就没有人为"这一形态整体成立"负责 ——
而回归恰恰会以"某个模块加了一行只对 Ollama 有意义的逻辑"的形式出现。

全程离线：云端后端只**构造**不发请求（真机验证过 ChatOpenAI 构造不联网），Ollama 原语打桩。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.config import Settings
from rolecard_agent.core import bootstrap
from rolecard_agent.core.agent.graph import build_graph_config
from rolecard_agent.core.bootstrap import Assembly, build_runtime
from rolecard_agent.core.model_settings import client_style
from rolecard_agent.core.telemetry.probes import local_inference_base_url
from rolecard_agent.domains.registry import (
    DOMAINS,
    build_query_services,
    build_registry,
    domain_seed_roles,
)
from rolecard_agent.features import reachout as svc
from rolecard_agent.features.proactive import build_gateway
from rolecard_agent.roles.models import RoleCard
from tests.conftest import ScriptedChat


class _Roles:
    """只喂一个开了主动资格的角色 —— 这条测试测的是"纯云形态"，不是角色服务。"""

    def __init__(self, roles: list[RoleCard]) -> None:
        self._roles = roles

    def list_roles(self) -> list[RoleCard]:
        return self._roles

    def scoped(self, user_id: str) -> _Roles:
        """调度器会问"替哪个主人挑人开口"（§4.1 的实例级身份）。这条桩不区分主人。"""
        return self


def _cloud_only_settings(tmp_path: Path) -> Settings:
    """一份"这台机器没有任何本地推理服务"的配置：只有一个 OpenAI 兼容云端后端。"""
    return Settings(
        sqlite_path=tmp_path / "cloud.db",
        chroma_path=tmp_path / "chroma",
        upload_dir=tmp_path / "uploads",
        model_backends={
            "deepseek": {
                "model": "deepseek-chat",
                "provider": "openai",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-not-a-real-key",
            }
        },
        model_default="deepseek",
    )


def _wiring(
    assembly: Assembly, settings: Settings, knowledge: object, enabled_domains: object
) -> object:
    return build_registry(
        roles=assembly.roles,
        ingestion=assembly.ingestion,
        query_services=assembly.queries,
        knowledge=knowledge,  # type: ignore[arg-type]
        enabled_domains=enabled_domains,  # type: ignore[arg-type]
        current_user=lambda: DEFAULT_USER_ID,
        upload_dir=settings.upload_dir,
        settings=settings,
        tracer=assembly.tracer,
        memory_conn=assembly.conn,
        fs_conn=assembly.conn,
    )


def _runtime(tmp_path: Path, **kw: object) -> bootstrap.Runtime:
    return build_runtime(  # type: ignore[return-value]
        domains=DOMAINS,
        query_services_factory=build_query_services,
        domain_seed_roles=domain_seed_roles(),
        proactive_factory=build_gateway,
        registry_factory=_wiring,  # type: ignore[arg-type]
        env_settings=_cloud_only_settings(tmp_path),
        model_factory=lambda *_a, **_k: None,
        **kw,  # type: ignore[arg-type]
    )


def test_cloud_only_config_assembles_a_working_kernel(tmp_path: Path) -> None:
    """纯云配置必须能装配出可用内核 —— 装配阶段不许探 Ollama。"""
    runtime = _runtime(tmp_path)
    try:
        assert runtime.state["graph"] is not None
        # 断言"不是 native"而不是断言 provider 的字面值：播种时按 base_url 认供应商
        # （deepseek 域名 → provider=deepseek），要钉的不变式是"这台机器没有本地推理"。
        backend = runtime.effective.backend(None)
        assert client_style(backend.provider) != "native"
    finally:
        runtime.assembly.conn.close()


def test_startup_warmup_does_not_touch_ollama_for_a_cloud_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """启动预热的正确行为是"云端默认 → 什么都不做"，而不是报一次错再跳过。"""
    calls: list[object] = []
    monkeypatch.setattr(bootstrap, "ollama_keep", lambda *a, **kw: calls.append(a))
    runtime = _runtime(tmp_path)
    try:
        runtime.pin_default_model()
        assert calls == []
    finally:
        runtime.assembly.conn.close()


def test_local_service_address_still_resolves_when_there_is_no_local_backend(
    tmp_path: Path,
) -> None:
    """没有本地后端时，"本地推理服务"这张卡要能老实回答"没在跑"，而不是无地址可问。

    出厂地址是兜底：卡片读的是服务本身，与"当前用哪个后端对话"是两码事。
    """
    settings = _cloud_only_settings(tmp_path)
    assert local_inference_base_url(settings) == "http://127.0.0.1:11434"


def test_proactive_reachouts_work_without_any_local_model(tmp_path: Path) -> None:
    """主动开口（桌宠的驱动源）在纯云形态下整条链路照常：生成 → 收件箱 → 投进主动会话。"""
    from datetime import UTC, datetime

    from rolecard_agent.features.reachout import ReachoutScheduler
    from rolecard_agent.roles.models import RoleCard

    reply = "外头降温了，穿上外套。"
    role = RoleCard(
        role_id="wan",
        role_name="苏晚晴",
        system_prompt="你是苏晚晴。",
        reachout_enabled=True,  # 内置角色出厂静默，这里要的是"能开口"这条链路
    )
    settings = _cloud_only_settings(tmp_path).model_copy(update={"reachout_enabled": True})
    runtime = _runtime(tmp_path)
    scheduler = ReachoutScheduler(
        inline_generation=True,
        settings_provider=lambda: settings,
        roles=_Roles([role]),  # type: ignore[arg-type]
        model_resolver=lambda _name: ScriptedChat([AIMessage(content=reply)]),
        conn=runtime.assembly.conn,
        tracer=runtime.assembly.tracer,
        deliver=runtime.deliver_proactive,
    )
    # 静默时段（本地 23:00–08:00）按真实时钟判：不给一个安全的本地时刻，半夜跑这条
    # 测试会因为"该睡觉了"而失败（tests/unit/test_reachout.py 的 _now() 同一课）。
    now_utc = datetime.now(UTC)
    now_local = datetime.now().astimezone().replace(hour=14, minute=0, second=0, microsecond=0)
    try:
        assert scheduler.tick_once(now_utc=now_utc, now_local=now_local) == 1
        rows = svc.list_reachouts(runtime.assembly.conn, user_id=DEFAULT_USER_ID)
        assert rows["unread"] == 1
        tid = rows["items"][0]["thread_id"]
        assert tid == svc.proactive_thread_id("wan", user_id=DEFAULT_USER_ID)
        # 会话真的建起来了、消息真的进了 checkpoint —— 桌宠点进去才有东西可看。
        state = runtime.state["graph"].get_state(build_graph_config(tid, runtime.effective))
        assert [str(m.content) for m in state.values["messages"]] == [reply]
    finally:
        runtime.assembly.conn.close()
