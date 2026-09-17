"""Health 查询工具（M3）与 `HealthQueryService` 的单元测试。  Traceability: US-1, US-2.

四个决定性约束，每个都对应一条测试：

  1. **用户隔离在服务层 WHERE 子句里强制**（不是在工具层）——工具层出 bug 也不可能放大
     一个用户能看到的数据（test_user_isolation）。
  2. **未核验标记必须在工具返回文本里**，不指望模型自觉补（test_*marker）。
  3. **诚实缺失**：查不到时返回"没有记录 + 现有指标清单"，让模型自我纠正而不是编造
     （test_query_unknown_*）。这正是内置角色范例里"档案没有就直接说没有"的工具侧支点。
  4. **对比是算术不是诊断**：只报"最早→最近 + 变化量"，单次记录明确拒绝对比。

全部离线：内存库 + `current_user` 注入常量。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rolecard_agent.domains.health.service import (
    HealthInvalidReport,
    HealthNotFound,
    HealthQueryService,
)
from rolecard_agent.domains.health.tools import make_domain_tools
from rolecard_agent.storage.db import bootstrap, connect

U1 = "u1"
U2 = "u2"


def _seed_identity(c: sqlite3.Connection) -> None:
    c.executescript(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('t1', 'demo');"
        f"INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        f"VALUES ('{U1}', 't1', 'u1');"
        f"INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        f"VALUES ('{U2}', 't1', 'u2');"
    )


@pytest.fixture
def service() -> HealthQueryService:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=["health"])
    _seed_identity(c)
    return HealthQueryService(c)


def _seed_two_years(service: HealthQueryService, *, user: str = U1) -> None:
    """内置角色范例里的那条数据线：2025 年 5.0 mm → 2026 年 6.0 mm。"""
    service.create_report(
        user_id=user,
        report_type="超声",
        check_time="2025-05-01",
        institution="市第一医院",
        indices=[
            {"index_name": "结石直径", "index_value": 5.0, "unit": "mm", "ref_range": "0-5"},
        ],
    )
    service.create_report(
        user_id=user,
        report_type="超声",
        check_time="2026-03-12",
        institution="市第一医院",
        note="复查",
        indices=[
            {"index_name": "结石直径", "index_value": 6.0, "unit": "mm", "ref_range": "0-5"},
            {
                "index_name": "总胆红素",
                "index_value": 18.5,
                "unit": "µmol/L",
                "ref_range": "3.4-20.5",
                "is_verified": True,
            },
        ],
    )


# -- service: create + read round trip -----------------------------------------------


def test_create_report_then_search_round_trips(service: HealthQueryService) -> None:
    _seed_two_years(service)
    rows = service.search_indices(U1, "结石直径")
    assert len(rows) == 2
    assert rows[0]["check_time"].startswith("2025-05-01")  # 按时间升序
    assert float(rows[-1]["index_value"]) == 6.0
    assert rows[-1]["is_verified"] == 0


def test_create_rejects_row_without_any_value(service: HealthQueryService) -> None:
    """没有数值也没有文本的指标行是无法回答的——比空行更糟的是'看似有数据'。"""
    with pytest.raises(HealthInvalidReport):
        service.create_report(
            user_id=U1,
            report_type="超声",
            check_time="2026-03-12",
            indices=[{"index_name": "空洞指标"}],
        )


def test_create_rejects_non_numeric_value(service: HealthQueryService) -> None:
    with pytest.raises(HealthInvalidReport):
        service.create_report(
            user_id=U1,
            report_type="超声",
            check_time="2026-03-12",
            indices=[{"index_name": "结石直径", "index_value": "六毫米"}],
        )


# -- user isolation（安全边界） --------------------------------------------------------


def test_user_isolation_is_enforced_in_the_service(service: HealthQueryService) -> None:
    _seed_two_years(service, user=U1)
    assert service.search_indices(U2, "结石直径") == []  # u2 看不见 u1 的任何数据
    assert service.list_reports(U2) == []
    assert service.index_names(U2) == []


# -- tools: query_health_record --------------------------------------------------------


@pytest.fixture
def tools(service: HealthQueryService, tmp_path: Path) -> list:
    # upload_dir 是宿主的路径边界（审查报告 H1）；本文件只测读工具，取一个空目录即可。
    return make_domain_tools(  # type: ignore[arg-type]
        None, service, current_user=lambda: U1, upload_dir=tmp_path
    )


def _tool(tools: list, name: str) -> object:
    return next(t for t in tools if t.name == name)


def test_query_formats_history_with_units_and_marker(
    service: HealthQueryService, tools: list
) -> None:
    _seed_two_years(service)
    out = _tool(tools, "query_health_record").invoke({"index_name": "结石直径"})
    assert "2025-05-01" in out and "2026-03-12" in out
    assert "5 mm" in out and "6 mm" in out
    assert "（参考 0-5）" in out
    assert out.count("【未经人工校验】") == 2  # 标记在工具文本里，不依赖模型自觉


def test_query_verified_row_has_no_marker(service: HealthQueryService, tools: list) -> None:
    _seed_two_years(service)
    out = _tool(tools, "query_health_record").invoke({"index_name": "总胆红素"})
    assert "18.5 µmol/L" in out
    assert "【未经人工校验】" not in out  # is_verified=1 的行不打标记


def test_query_partial_match_finds_stored_name(service: HealthQueryService, tools: list) -> None:
    _seed_two_years(service)
    out = _tool(tools, "query_health_record").invoke({"index_name": "结石"})
    assert "结石直径" in out and "共 2 条记录" in out


def test_query_unknown_name_lists_existing_indices(
    service: HealthQueryService, tools: list
) -> None:
    _seed_two_years(service)
    out = _tool(tools, "query_health_record").invoke({"index_name": "血糖"})
    assert "没有" in out and "血糖" in out
    assert "结石直径" in out and "总胆红素" in out  # 自我纠正提示


def test_query_date_filter_excludes_out_of_range(service: HealthQueryService, tools: list) -> None:
    _seed_two_years(service)
    out = _tool(tools, "query_health_record").invoke(
        {"index_name": "结石直径", "start_date": "2026-01-01"}
    )
    assert "2026-03-12" in out
    assert "2025-05-01" not in out


def test_query_accepts_partial_dates(service: HealthQueryService) -> None:
    """P1-8 回归：`2026-03` / `2026` 这种部分日期必须能取到数据。

    以前 SQL 写的是 `date(check_time) >= date(?)`，而 SQLite 里 `date('2026-03')` 求值是
    **NULL** → 条件恒为 NULL → **静默返回空**：模型传「2026年3月」时档案看着像空的，
    与 `_DATE_RE` 注释承诺的"应该拿到数据"完全相反。
    """
    _seed_two_years(service)

    by_month = service.search_indices(U1, "结石直径", start_date="2026-03", end_date="2026-03")
    assert [r["check_time"] for r in by_month] == ["2026-03-12"]

    by_year = service.search_indices(U1, "结石直径", start_date="2026")
    assert [r["check_time"] for r in by_year] == ["2026-03-12"]

    through_2025 = service.search_indices(U1, "结石直径", end_date="2025")
    assert [r["check_time"] for r in through_2025] == ["2025-05-01"]

    # 单日边界：结束日当天必须含在内（上界是"次日零点"的开区间）
    on_day = service.search_indices(
        U1, "结石直径", start_date="2026-03-12", end_date="2026-03-12"
    )
    assert [r["check_time"] for r in on_day] == ["2026-03-12"]

    # 跨年边界：12 月的下一个月是次年 1 月
    from_december = service.search_indices(U1, "结石直径", start_date="2025-12")
    assert [r["check_time"] for r in from_december] == ["2026-03-12"]


def test_query_ignores_unparseable_dates_instead_of_returning_nothing(
    service: HealthQueryService,
) -> None:
    """认不出的写法按"没有过滤"处理，而不是静默查空（`_DATE_RE` 的既定语义）。"""
    _seed_two_years(service)

    rows = service.search_indices(U1, "结石直径", start_date="2026年3月")

    assert len(rows) == 2  # 两条都在：当作没有过滤


# -- tools: compare_health_index -------------------------------------------------------


def test_compare_two_numeric_records_shows_delta(service: HealthQueryService, tools: list) -> None:
    _seed_two_years(service)
    out = _tool(tools, "compare_health_index").invoke({"index_name": "结石直径"})
    assert "5 mm" in out and "6 mm" in out
    assert "上升 1 mm" in out
    assert out.endswith("【未经人工校验】")


def test_compare_single_record_refuses_to_invent_trend(
    service: HealthQueryService, tools: list
) -> None:
    service.create_report(
        user_id=U1,
        report_type="超声",
        check_time="2026-03-12",
        indices=[{"index_name": "结石直径", "index_value": 6.0, "unit": "mm"}],
    )
    out = _tool(tools, "compare_health_index").invoke({"index_name": "结石直径"})
    assert "只有 1 次记录" in out and "无法" in out


def test_compare_non_numeric_pairs_skip_delta(service: HealthQueryService, tools: list) -> None:
    service.create_report(
        user_id=U1,
        report_type="胃镜",
        check_time="2025-01-01",
        indices=[{"index_name": "幽门螺杆菌", "value_text": "阳性"}],
    )
    service.create_report(
        user_id=U1,
        report_type="胃镜",
        check_time="2026-01-01",
        indices=[{"index_name": "幽门螺杆菌", "value_text": "阴性"}],
    )
    out = _tool(tools, "compare_health_index").invoke({"index_name": "幽门螺杆菌"})
    assert "阳性" in out and "阴性" in out
    assert "上升" not in out and "下降" not in out  # 文本值不做伪算术


# -- F2：数据管理（修正 / 删除） -------------------------------------------------------


def _first_index_id(service: HealthQueryService, user: str) -> str:
    report = service.list_records(user)[0]
    return str(report["indices"][0]["index_id"])


def test_update_index_applies_corrections(service: HealthQueryService) -> None:
    _seed_two_years(service)
    index_id = _first_index_id(service, U1)
    row = service.update_index(
        user_id=U1,
        index_id=index_id,
        changes={"index_value": 4.8, "is_verified": True, "raw_text": "人工复核 4.8mm"},
    )
    assert float(row["index_value"]) == 4.8 and row["is_verified"] == 1


def test_update_index_switching_to_text_value(service: HealthQueryService) -> None:
    """显式把数值清空、换成文本 —— 终态校验应放行这种合法切换。"""
    _seed_two_years(service)
    index_id = _first_index_id(service, U1)
    row = service.update_index(
        user_id=U1,
        index_id=index_id,
        changes={"index_value": None, "value_text": "约 6 mm，伴声影"},
    )
    assert row["index_value"] is None and "6 mm" in str(row["value_text"])


def test_update_index_rejects_both_empty(service: HealthQueryService) -> None:
    _seed_two_years(service)
    index_id = _first_index_id(service, U1)
    with pytest.raises(HealthInvalidReport):
        service.update_index(
            user_id=U1,
            index_id=index_id,
            changes={"index_value": None, "value_text": None},
        )


def test_update_index_rejects_unknown_field(service: HealthQueryService) -> None:
    _seed_two_years(service)
    index_id = _first_index_id(service, U1)
    with pytest.raises(HealthInvalidReport):
        service.update_index(user_id=U1, index_id=index_id, changes={"report_id": "x"})


def test_update_wrong_user_is_not_found(service: HealthQueryService) -> None:
    """归属校验：错误用户得到 NotFound（不泄露'行存在但属于别人'）。"""
    _seed_two_years(service, user=U1)
    index_id = _first_index_id(service, U1)
    with pytest.raises(HealthNotFound):
        service.update_index(user_id=U2, index_id=index_id, changes={"index_value": 1.0})


def test_delete_report_cascades_indices(service: HealthQueryService) -> None:
    _seed_two_years(service)
    report_id = service.list_records(U1)[0]["report_id"]
    service.delete_report(user_id=U1, report_id=report_id)
    remaining = service.list_records(U1)
    assert len(remaining) == 1  # 另一份报告还在
    assert service.search_indices(U1, "结石直径")  # 2026 那份的指标仍可查


def test_delete_index_keeps_report(service: HealthQueryService) -> None:
    _seed_two_years(service)
    index_id = _first_index_id(service, U1)
    service.delete_index(user_id=U1, index_id=index_id)
    assert all(i["index_id"] != index_id for r in service.list_records(U1) for i in r["indices"])


def test_delete_wrong_user_not_found(service: HealthQueryService) -> None:
    _seed_two_years(service, user=U1)
    report_id = service.list_records(U1)[0]["report_id"]
    with pytest.raises(HealthNotFound):
        service.delete_report(user_id=U2, report_id=report_id)


# -- tools: list_reports ---------------------------------------------------------------


def test_list_reports_empty_archive(service: HealthQueryService, tools: list) -> None:
    out = _tool(tools, "list_reports").invoke({})
    assert "还没有任何已入库的报告" in out


def test_list_reports_shows_counts_and_note(service: HealthQueryService, tools: list) -> None:
    _seed_two_years(service)
    out = _tool(tools, "list_reports").invoke({})
    assert "共有 2 份报告" in out
    assert "2026-03-12 · 超声 · 市第一医院 · 2 项指标 · 备注：复查" in out
    assert "2025-05-01" in out
