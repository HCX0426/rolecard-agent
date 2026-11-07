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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rolecard_agent.base.identity import DEFAULT_TENANT_ID, DEFAULT_USER_ID  # noqa: E402
from rolecard_agent.config import Settings  # noqa: E402
from rolecard_agent.core.models.services import ServiceEndpointService  # noqa: E402
from rolecard_agent.storage.db import bootstrap, connect  # noqa: E402


def main() -> None:
    """播一支演示库：本机身份 + 「服务」页内置端点。**不注入任何域数据**。

    2026-11-07 起出厂不带代码侧的域（两个域包被物理删除），此前这里注入的两份体检报告与
    随访须知因此失去归宿 —— 域内容归用户：想要哪个域就在资料页自己建、自己补录。
    """
    settings = Settings.from_env()
    conn = connect(settings.sqlite_path)
    bootstrap(conn, enabled_domains=())
    # `seed_once()` 平时在装配根里调，`storage.db.bootstrap` 不播「服务」页那些行，
    # 所以绕过装配根的脚本会拿到一张空的端点表，后面 `make_embedder` 直接大声失败
    # （嵌入器与 app 必须同源，维度不一致检索就废）。
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
    conn.close()
    print(f"演示库已就绪（身份 {DEFAULT_USER_ID}；未注入任何域数据）。")


if __name__ == "__main__":
    main()
