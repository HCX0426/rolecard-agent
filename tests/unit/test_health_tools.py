"""Unit tests for the health domain tools.  Traceability: US-3.

`upload_medical_report` - the WRITER for the kernel `ingestion_task` ledger - is covered here
end-to-end so the ingestion table is exercised rather than left as dead schema
(技术评审与决策.md §9 B1). The three READ tools (query_health_record / compare_health_index /
list_reports) have their own file: tests/unit/test_health_query.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.domains.health.service import HealthQueryService
from rolecard_agent.domains.health.tools import make_domain_tools
from rolecard_agent.storage.db import bootstrap, connect


@pytest.fixture
def env(tmp_path: Path) -> tuple[IngestionService, list, Path]:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=["health"])
    # `ingestion_task.user_id` references app_user - seed the acting user.
    c.executescript(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');"
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "  VALUES ('u1', 't1', 'u1');"
    )
    c.commit()
    ing = IngestionService(c)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    tools = make_domain_tools(
        ing, HealthQueryService(c), current_user=lambda: "u1", upload_dir=uploads
    )
    f = uploads / "report.pdf"
    f.write_bytes(b"%PDF-1.4 not a real scan")
    return ing, tools, f


def _upload(tools: list) -> object:
    return next(t for t in tools if t.name == "upload_medical_report")


def test_upload_creates_one_ingestion_task(env: tuple[IngestionService, list, Path]) -> None:
    ing, tools, f = env
    out = _upload(tools).invoke({"file_path": str(f)})
    assert "intake" in out
    tasks = ing.list_for_user("u1")
    assert len(tasks) == 1
    assert tasks[0]["status"] == "pending"


def test_reupload_same_bytes_is_idempotent(env: tuple[IngestionService, list, Path]) -> None:
    ing, tools, f = env
    _upload(tools).invoke({"file_path": str(f)})
    first = ing.list_for_user("u1")
    _upload(tools).invoke({"file_path": str(f)})  # identical bytes
    assert ing.list_for_user("u1") == first  # still exactly one task


def test_upload_missing_file_is_reported_not_raised(
    env: tuple[IngestionService, list, Path],
) -> None:
    ing, tools, f = env
    out = _upload(tools).invoke({"file_path": str(f) + ".nope"})
    assert "不存在" in out
    assert ing.list_for_user("u1") == []  # no half-written ledger row


# -- 路径边界（审查报告 H1） -----------------------------------------------------------


@pytest.mark.parametrize(
    "outside",
    [
        "C:/Windows/win.ini",
        "C:/Users/Public/Documents/其他用户.txt",
    ],
)
def test_upload_refuses_paths_outside_the_upload_dir(
    env: tuple[IngestionService, list, Path], outside: str
) -> None:
    """模型可自由填 `file_path`，因此越界路径必须被拒绝而不是登记。

    回归护栏：修复前 `p.is_file()` 是唯一前提，`C:/Windows/win.ini` 能建成 intake 任务，
    再经 `POST /api/records/extract` 的 `source_file.exists()` 分支被解析后送进模型 ——
    一条"读任意主机文件"的完整链路。
    """
    ing, tools, _ = env
    out = _upload(tools).invoke({"file_path": outside})
    assert "上传目录" in out
    assert ing.list_for_user("u1") == []  # 没有登记、没有落库


def test_upload_refuses_parent_traversal(env: tuple[IngestionService, list, Path]) -> None:
    """相对路径里的 `..` 由 resolve() 归一，不能借它跑出上传目录。"""
    ing, tools, f = env
    escaping = str(f.parent / ".." / "escaped.txt")
    (f.parent.parent / "escaped.txt").write_text("x", encoding="utf-8")
    out = _upload(tools).invoke({"file_path": escaping})
    assert "上传目录" in out
    assert ing.list_for_user("u1") == []
