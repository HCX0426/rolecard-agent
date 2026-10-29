"""README 六页签截图的驱动：自起隔离后端 + 全新演示库 + headless Chrome。

用法（仓库根）：

    .venv\\Scripts\\python.exe scripts\\tools\\render_readme_shots.py

四条红线按 `probe_chat_ui.py` 的规矩守着：不打 :8000、端口现取、**库是全新临时库**
（README 是给外人看的门面，绝不能带真库数据 —— 与取证探针"复制真库"刻意相反）、
只 terminate 自己 spawn 的那个 pid。

演示数据复用 `seed_demo_data.py` 的那条线（虚构报告 + 随访须知文档 + 演示身份），
但 chroma 落临时目录（那份脚本写死 `data/chroma`，这里不能跟着写仓库数据根）。
跑完图在 build/readme-shots/，人工过目后自行替换 docs/assets/console-*.png。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from rolecard_agent.base.identity import DEFAULT_TENANT_ID, DEFAULT_USER_ID  # noqa: E402
from rolecard_agent.config import Settings  # noqa: E402
from rolecard_agent.core.assembler import candidate_ids  # noqa: E402
from rolecard_agent.core.models.services import ServiceEndpointService  # noqa: E402
from rolecard_agent.domains.health.service import HealthQueryService  # noqa: E402
from rolecard_agent.rag.retriever import KnowledgeBase, make_embedder  # noqa: E402
from rolecard_agent.storage.db import bootstrap, connect  # noqa: E402


def _seed_demo(sqlite_path: Path, chroma_path: Path) -> None:
    """演示数据 = `seed_demo_data.py` 的同一份口径，只是 chroma 指进临时目录。"""
    os.environ["SQLITE_PATH"] = str(sqlite_path)
    settings = Settings.from_env()
    conn = connect(sqlite_path)
    bootstrap(conn, enabled_domains=("health",))
    ServiceEndpointService(conn, owner=DEFAULT_USER_ID).seed_once()
    conn.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES (?, '本地演示')",
        (DEFAULT_TENANT_ID,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name)"
        " VALUES (?, ?, '本地用户')",
        (DEFAULT_USER_ID, DEFAULT_TENANT_ID),
    )
    conn.commit()

    services = ServiceEndpointService(conn, owner=DEFAULT_USER_ID)
    kb = KnowledgeBase(
        chroma_path,
        make_embedder(
            settings,
            order=candidate_ids(services, "embedding"),
            endpoints=services.endpoint_map("embedding"),
        ),
    )
    doc = (
        "胆囊结石随访须知（虚构演示文档）：\n\n"
        "胆囊结石直径小于 10 mm 且无症状者，建议每 6 到 12 个月复查一次腹部超声。"
        "复查应固定同一家医疗机构，便于前后对比。\n\n"
        "出现持续性腹痛、发热或皮肤巩膜黄染时，提示可能出现并发症，应及时就医。\n\n"
        "本须知为演示数据，不构成任何医疗建议。"
    )
    kb.index("health_reports", "随访须知（演示）.md", doc)

    query = HealthQueryService(conn)
    if not query.list_reports(DEFAULT_USER_ID):
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
                }
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
                }
            ],
        )
    conn.close()


def main() -> int:
    # 与 run_api.force_utf8_stdio 同一条纪律：GBK 控制台打不下 JS 驱动的 ✅（U+2705），
    # 不修会在 print 那行 UnicodeEncodeError —— 图其实已经拍完了。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    workdir = Path(tempfile.mkdtemp(prefix="readme_shots_"))
    sqlite_path = workdir / "app.db"
    _seed_demo(sqlite_path, workdir / "chroma")

    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "PYTHONIOENCODING": "utf-8",
        "SQLITE_PATH": str(sqlite_path),
        "CHROMA_PATH": str(workdir / "chroma"),
        "UPLOAD_DIR": str(workdir / "uploads"),
        "WORKSPACE_DIR": str(workdir / "workspace"),
        "RUN_API_PORT": str(port),
        "MEMORY_EXTRACT_AUTO": "0",
        "MODEL_PIN_ON_STARTUP": "0",
        "SILICONFLOW_API_KEY": "",  # 演示库不配 key：页面照常渲染，不外呼
    }
    log = workdir / "backend.log"
    with log.open("wb") as fh:
        server = subprocess.Popen(  # noqa: S603
            [sys.executable, str(ROOT / "scripts" / "run_api.py")],
            cwd=str(ROOT),
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}"
    rc = 1
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            if server.poll() is not None:
                tail = log.read_text("utf-8", "replace")[-2000:]
                print(f"后端起不来（退出码 {server.returncode}），日志尾部：\n{tail}")
                return 1
            with contextlib.suppress(Exception):
                if urllib.request.urlopen(f"{base}/api/health", timeout=3).status == 200:  # noqa: S310
                    break
            time.sleep(1.0)
        else:
            print("后端 90s 内没就绪")
            return 1

        # 侧栏不能是空的：建两条演示会话（不发消息 —— 没配模型，空态引导卡也是真实界面）。
        for _ in range(2):
            req = urllib.request.Request(  # noqa: S310
                f"{base}/api/session",
                data=json.dumps({}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with contextlib.suppress(Exception):
                urllib.request.urlopen(req, timeout=10)  # noqa: S310

        proc = subprocess.run(  # noqa: S603
            ["node", str(ROOT / "scripts" / "js" / "render_readme_shots.js"), base],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(ROOT),
            encoding="utf-8",
            errors="replace",
        )
        print((proc.stdout or "") + (proc.stderr or ""))
        rc = proc.returncode
    finally:
        server.terminate()
        with contextlib.suppress(Exception):
            server.wait(timeout=20)
        print(f"\n（读数出处：临时库 {sqlite_path} / 端口 {port}）")
        # 演示库跑完即删（与 smoke/探针同一课：%TEMP% 不留任何一份库拷贝）。
        shutil.rmtree(workdir, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
