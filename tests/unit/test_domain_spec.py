"""P1-5（域机制 DomainSpec 化）的回归用例：**新增一个域 = 新增一个目录，中心代码零改动**。

2026-10-04 审查快照给旧现实记的账是：新增一域要改 ≥6 处中心代码（registry 的 DOMAINS
元组与 factories 字典、api/main 的 isinstance 接线、records 的分派、roles/seed 的工具名与
知识作用域），于是 finance 域只能做空壳。收口之后判据只剩一条：**域自己带一份 `SPEC`**，
registry / 装配根 / 角色播种三处都从它读，谁也不点名谁。

为什么不在测试里真的往 `src/rolecard_agent/domains/` 写一个临时域目录：xdist 四个 worker
同时跑，别的 worker 正在 `build_registry` / `create_app` 的窗口里撞见一个半写或半删的目录
会当场红 —— 偶发失败比覆盖不足更贵。所以这里从**同一枚接缝**验：`discover_specs` 是唯一的
枚举入口，把替身塞给它就等于"多了一个域"，三处下游（工具注册 / 种子角色 / 查询服务）跟着
走而一行中心代码都不用改。目录枚举本身的规则（跳过私有目录、必须是包、必须有 SPEC）用临时
根目录直接验，不碰 src。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import cast

import pytest
from langchain_core.tools import tool

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.core.domain_service import DomainQueryService
from rolecard_agent.core.ingest.ingestion import IngestionService
from rolecard_agent.domains import registry as registry_mod
from rolecard_agent.domains.registry import (
    DOMAINS,
    build_query_services,
    build_registry,
    discover_specs,
    domain_seed_roles,
)
from rolecard_agent.domains.spec import (
    ActorLike,
    DomainSpec,
    DomainToolContext,
    RouterDeps,
    RouterHost,
)
from rolecard_agent.rag.retriever import KnowledgeBase
from rolecard_agent.roles.models import RoleCardCreate
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import SqlConnection


@tool
def demo_ping() -> str:
    """演示域的只读工具（金标准用：新域的工具必须不改中心代码就注册得上）。"""
    return "pong"


@tool
def demo_write(note: str) -> str:
    """演示域的写工具（声明进 write_tool_names ⇒ 执行器不许重试）。"""
    return note


def _demo_seed_role() -> RoleCardCreate:
    return RoleCardCreate(
        role_id="demo_curator",
        role_name="演示域管理员",
        system_prompt="你是演示域的管理员。",
        temperature=0.3,
        model_name=None,
        tool_whitelist=["demo_ping", "demo_write"],
        exemplars=[],
        knowledge_scopes=[],
        description="P1-5 金标准：随域 SPEC 出厂的种子角色。",
    )


class _FakeQueryService:
    """只桩"按域取服务"这件事：本用例不碰它的读写方法。"""

    def __init__(self, conn: object) -> None:
        self.conn = conn


def _fake_query_service(conn: SqlConnection) -> DomainQueryService:
    return cast("DomainQueryService", _FakeQueryService(conn))


def _demo_spec() -> DomainSpec:
    """一个「假想的新域」：只带自己的四件声明，与 registry / main / roles 无任何往来。"""
    return DomainSpec(
        id="demo",
        tool_factory=lambda _ctx: [demo_ping, demo_write],
        query_service_factory=_fake_query_service,
        seed_roles=(_demo_seed_role(),),
        write_tool_names=frozenset({"demo_write"}),
    )


def _independent_dirs(root: Path) -> list[str]:
    """独立复刻目录判据（**不调用被测实现**）：非下划线目录且带 __init__.py 才算域目录。"""
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith((".", "_")) and (p / "__init__.py").is_file()
    )


def test_registered_domains_are_their_directories() -> None:
    """`DOMAINS` 不是手写清单：它逐项等于 `domains/` 下的目录，且每个域都有 schema。

    这条防的是"两份清单漂移"（DOMAINS 里有、目录没有 ⇒ 启动 FileNotFoundError；目录里有、
    DOMAINS 没有 ⇒ 那个域永远注册不上，而且没人会红）。
    """
    package_dir = Path(registry_mod.__file__).resolve().parent
    specs = discover_specs()

    assert tuple(spec.id for spec in specs) == DOMAINS
    assert tuple(_independent_dirs(package_dir)) == DOMAINS, "域名单与目录不一致（有第二份清单？）"
    for spec in specs:
        schema = package_dir / spec.id / "schema.sql"
        assert schema.is_file(), f"域 {spec.id} 缺 schema.sql（storage 启动会 FileNotFoundError）"


def test_a_new_domain_needs_no_central_change(
    monkeypatch: pytest.MonkeyPatch, conn: object, tmp_path: Path
) -> None:
    """金标准：塞一个新域的 `SPEC` 进枚举入口，三处下游全部跟着走，中心代码一行不改。

    三处下游 = ① 工具注册（含写/读的重试分流）② 出厂种子角色聚合 ③ 按域建查询服务。
    旧实现里这三处分别要动 `factories` 字典、`roles/seed.py` 与 `main.py` 的 isinstance 接线。
    """
    monkeypatch.setattr(registry_mod, "discover_specs", lambda root=None: (_demo_spec(),))
    connection = cast("SqlConnection", conn)

    # ① 工具注册：不传 specs（默认走枚举）⇒ 新域的工具照常注册，域归属与重试开关都对。
    registry = build_registry(
        roles=RoleCardService(connection),
        ingestion=IngestionService(connection),
        query_services=build_query_services(connection),
        knowledge=cast("KnowledgeBase", None),  # search_knowledge 的 KB 本用例不碰
        enabled_domains=lambda: ["demo"],
        current_user=lambda: DEFAULT_USER_ID,
        upload_dir=tmp_path / "uploads",
    )
    by_name = {s.name: s for s in registry.specs()}
    assert by_name["demo_ping"].domain == "demo"
    assert by_name["demo_write"].domain == "demo"
    assert registry.is_idempotent("demo_ping"), "读工具该允许执行器重试"
    assert not registry.is_idempotent("demo_write"), "写工具绝不能重试（审查报告 M10）"

    # ② 种子角色：聚合自 SPEC —— `roles/seed.py` 一个字都没改。
    assert [r.role_id for r in domain_seed_roles()] == ["demo_curator"]

    # ③ 查询服务：按域 id 各建各的，喂错域没有中间态。
    services = build_query_services(connection)
    assert list(services) == ["demo"]
    assert isinstance(services["demo"], _FakeQueryService)


def test_the_real_domains_still_declare_themselves() -> None:
    """替身只活在它自己的用例里：真清单仍由目录枚举给出，health 的工厂自带头等检查。"""
    assert "health" in DOMAINS and "finance" in DOMAINS
    assert [r.role_id for r in domain_seed_roles()] == ["medical_archivist"]

    # 原 `api/main.py` 那句"喂错服务就拒绝启动"的 isinstance 接线搬进了域自己的工具工厂
    # —— 装配点不再认识任何服务类，所以这道检查只能长在域身上。
    health = next(spec for spec in discover_specs() if spec.id == "health")
    ctx = DomainToolContext(
        ingestion=cast("IngestionService", None),  # 喂错服务时走不到用它那一步
        query=cast("DomainQueryService", _FakeQueryService(object())),
        current_user=lambda: DEFAULT_USER_ID,
        upload_dir=Path("."),
    )
    with pytest.raises(RuntimeError, match="HealthQueryService"):
        health.tool_factory(ctx)


def test_domain_routes_are_mounted_from_their_own_spec(conn: object) -> None:
    """`router_contrib` 是那族端点的**住址**：宿主只靠这份声明挂路由，api 层零 import 具体域。

    为什么值得单独立一条：搬迁之后再没有任何 import 关系能证明 `/api/records` 还在 ——
    少交一份路由**不会报错**，只会静默缺一族端点（前端数据页整页 404）。这里钉住"真的交回来
    了、路径对得上"，顺带钉住数据型域不交路由（None 是合法值，不是漏写）。
    """
    from rolecard_agent.domains.health import SPEC
    from rolecard_agent.domains.health.service import HealthQueryService

    deps = RouterDeps(
        get_context=lambda: cast("RouterHost", None),
        get_actor=lambda: cast("ActorLike", None),
        query_services={"health": HealthQueryService(cast("SqlConnection", conn))},
    )
    routers = SPEC.router_contrib(deps)  # 联合类型里 None 那一半由下一条 finance 用例钉住
    paths = {route.path for router in routers for route in router.routes}
    assert paths == {
        "/api/records",
        "/api/records/report",
        "/api/records/extract",
        "/api/records/index/{index_id}",
        "/api/records/report/{report_id}",
    }, paths

    finance = next(spec for spec in discover_specs() if spec.id == "finance")
    assert finance.router_contrib is None, "数据型域没有专属路由（数据走通用 domain_data CRUD）"


def test_a_domain_package_without_a_spec_fails_loudly() -> None:
    """缺 `SPEC` = 启动即红。域的工具永远绑不上是本项目最不接受的那种静默失败。"""
    module_name = "rolecard_agent.domains._tmp_missing_spec"
    sys.modules[module_name] = types.ModuleType(module_name)
    try:
        # `_load_spec` 拼的是 `<本包>.<短名>`，所以这里注册全名、传短名。
        with pytest.raises(ValueError, match="SPEC"):
            registry_mod._load_spec("_tmp_missing_spec")
    finally:
        del sys.modules[module_name]


def test_a_spec_id_that_disagrees_with_its_directory_fails_loudly() -> None:
    """`SPEC.id` 必须等于目录名：对不上就是"注册了 id、装的是另一套表"的错位。"""
    module_name = "rolecard_agent.domains._tmp_mismatched_spec"
    module = types.ModuleType(module_name)
    module.SPEC = DomainSpec(id="not_the_directory_name", tool_factory=lambda _ctx: ())
    sys.modules[module_name] = module
    try:
        with pytest.raises(ValueError, match="不一致"):
            registry_mod._load_spec("_tmp_mismatched_spec")
    finally:
        del sys.modules[module_name]


def test_domain_dir_rules_skip_private_and_require_a_package(tmp_path: Path) -> None:
    """目录判据：私有目录（`__pycache__`、`_x`）跳过；候选目录必须是包。"""
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "_scratch").mkdir()
    (tmp_path / "bare").mkdir()  # 没有 __init__.py
    with pytest.raises(ValueError, match="不是包"):
        registry_mod._domain_dirs(tmp_path)

    # 有 __init__.py 的目录（哪怕声明是坏的）算候选 —— SPEC 的死活由 `_load_spec` 判。
    (tmp_path / "bare" / "__init__.py").write_text("SPEC = None\n", encoding="utf-8")
    (tmp_path / "bare" / "schema.sql").write_text("", encoding="utf-8")
    names = [d.name for d in registry_mod._domain_dirs(tmp_path)]
    assert names == ["bare"], "私有目录该被跳过，包目录该被留下"
