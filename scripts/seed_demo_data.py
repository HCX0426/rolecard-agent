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

from rolecard_agent.api.main import DEFAULT_TENANT_ID, DEFAULT_USER_ID  # noqa: E402
from rolecard_agent.config import Settings  # noqa: E402
from rolecard_agent.domains.health.service import HealthQueryService  # noqa: E402
from rolecard_agent.storage.db import bootstrap, connect  # noqa: E402


def main() -> None:
    settings = Settings.from_env()
    conn = connect(settings.sqlite_path)
    bootstrap(conn, enabled_domains=("health",))
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
    existing = query.list_reports(DEFAULT_USER_ID)
    if existing:
        print(f"演示用户已有 {len(existing)} 份报告，跳过（幂等）。")
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
