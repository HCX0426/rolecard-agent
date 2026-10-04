"""为演示库（data/sqlite/app.db）注入虚构演示数据 —— 全部为编造数值，不涉及任何真人。

用法（PowerShell）：

    .venv\\Scripts\\python.exe scripts\\seed_demo_data.py

幂等：演示用户名下已有报告时直接跳过。数据线刻意对齐内置角色的范例对白
（结石直径 2025 年 5 mm → 2026 年 3 月 6 mm），这样录演示视频时角色行为与
档案数据互相印证。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rolecard_agent.base.identity import DEFAULT_TENANT_ID, DEFAULT_USER_ID  # noqa: E402
from rolecard_agent.config import Settings  # noqa: E402
from rolecard_agent.core.services import ServiceEndpointService  # noqa: E402
from rolecard_agent.domains.health.service import HealthQueryService  # noqa: E402
from rolecard_agent.storage.db import bootstrap, connect  # noqa: E402


def main() -> None:
    settings = Settings.from_env()
    conn = connect(settings.sqlite_path)
    bootstrap(conn, enabled_domains=("health",))
    # 与 `run_eval.py` 同一处修补：`seed_once()` 平时在装配根里调，`storage.db.bootstrap`
    # 不播「服务」页那些行，所以绕过装配根的脚本会拿到一张空的端点表，
    # 后面 `make_embedder` 直接大声失败（嵌入器与 app 必须同源，维度不一致检索就废）。
    # 这支脚本建的库里只播一个演示身份（下面那两条 INSERT 就是它），所以主人是同一个。
    ServiceEndpointService(conn, owner=DEFAULT_USER_ID).seed_once()
    conn.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES (?, '本地演示')",
        (DEFAULT_TENANT_ID,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "VALUES (?, ?, '本地用户')",
        (DEFAULT_USER_ID, DEFAULT_TENANT_ID),
    )
    conn.commit()

    query = HealthQueryService(conn)

    # v2.1：注入知识文档（作用域 health_reports）。放在报告注入之前、独立幂等 ——
    # 报告已存在时不能连带跳过知识文档。嵌入器与主服务同源（同一环境 → 同一 backend
    # → 向量维度一致）；切换嵌入后端后需删除 data/chroma 重建。
    # 选型必须走「服务」页那份事实面（`candidate_ids`/`endpoint_map`）：这里曾经直接
    # `make_embedder(settings)`，而那个签名早已改成两个必填关键字参数 —— 于是这个脚本
    # 一跑就 TypeError，却因为"scripts 不归 mypy 管"（09-26 轮 S-6）静默坏了很久。
    from rolecard_agent.core.bootstrap import candidate_ids
    from rolecard_agent.rag.retriever import KnowledgeBase, make_embedder

    services = ServiceEndpointService(conn, owner=DEFAULT_USER_ID)
    kb = KnowledgeBase(
        Path("data/chroma").resolve(),
        make_embedder(
            settings,
            order=candidate_ids(services, "embedding"),
            endpoints=services.endpoint_map("embedding"),
        ),
    )
    if kb.scope_count("health_reports") == 0:
        doc = (
            "胆囊结石随访须知（虚构演示文档）：\n\n"
            "胆囊结石直径小于 10 mm 且无症状者，建议每 6 到 12 个月复查一次腹部超声。"
            "复查应固定同一家医疗机构，便于前后对比。\n\n"
            "出现持续性腹痛、发热或皮肤巩膜黄染时，提示可能出现并发症，应及时就医，"
            "而不是等待下次随访。\n\n"
            "饮食方面：减少油腻食物，规律进餐。本须知为演示数据，不构成任何医疗建议。"
        )
        chunks = kb.index("health_reports", "随访须知（演示）.md", doc)
        print(f"已注入知识文档（{chunks} 段，作用域 health_reports）。")
    else:
        print("知识作用域已有内容，跳过（幂等）。")

    existing = query.list_reports(DEFAULT_USER_ID)
    if existing:
        print(f"演示用户已有 {len(existing)} 份报告，跳过报告注入（幂等）。")
        return

    query.create_report(
        user_id=DEFAULT_USER_ID,
        report_type="超声",
        check_time="2025-05-01",
        institution="市第一医院",
        note="年度体检",
        indices=[
            {
                "index_name": "结石直径",
                "index_value": 5.0,
                "unit": "mm",
                "ref_range": "0-5",
                "is_verified": True,
                "raw_text": "胆囊结石一枚，直径约5mm",
            },
            {
                "index_name": "总胆红素",
                "index_value": 16.2,
                "unit": "µmol/L",
                "ref_range": "3.4-20.5",
                "is_verified": True,
            },
        ],
    )
    query.create_report(
        user_id=DEFAULT_USER_ID,
        report_type="超声",
        check_time="2026-03-12",
        institution="市第一医院",
        note="复查",
        indices=[
            {
                "index_name": "结石直径",
                "index_value": 6.0,
                "unit": "mm",
                "ref_range": "0-5",
                "is_verified": False,
                "raw_text": "结石较前增大，约6mm",
            },
            {
                "index_name": "总胆红素",
                "index_value": 18.5,
                "unit": "µmol/L",
                "ref_range": "3.4-20.5",
                "is_verified": True,
            },
            {
                "index_name": "尿酸",
                "index_value": 488.0,
                "unit": "µmol/L",
                "ref_range": "208-428",
                "is_verified": False,
            },
        ],
    )
    print(
        "已注入 2 份虚构演示报告（2025-05-01 / 2026-03-12，共 5 项指标，"
        f"归属演示用户 {DEFAULT_USER_ID}）。"
    )


if __name__ == "__main__":
    main()
