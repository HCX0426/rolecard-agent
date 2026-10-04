"""Domain tools: query_health_record / compare_health_index / list_reports /
upload_medical_report.

Unverified values must carry the marker inside the TOOL RETURN TEXT
("【未经人工校验】") - never rely on the model to add it.

NOTE: there is deliberately NO retrieval tool here. Document search is a KERNEL capability
(rag/retriever.py exposes `search_knowledge`) because it must work for every domain, not
just this one. A domain that needs a narrower scope passes it as an argument.

The three READ tools are thin formatters over `HealthQueryService`: they never touch SQL and
never widen a query beyond `current_user()` - user isolation is enforced in the service's
WHERE clause, and the acting user is resolved at invocation time from a zero-arg callable
(the model cannot name who it is acting as). `upload_medical_report` is the WRITER for the
kernel `ingestion_task` ledger.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from rolecard_agent.base.markers import UNVERIFIED_MARKER
from rolecard_agent.core.ingestion import IngestionService
from rolecard_agent.domains.health.service import HealthQueryService

# The names this domain contributes. The single source of truth for the built-in role's
# whitelist (`roles/seed.py`) and for `tests/unit/test_builtin_tools.py`'s resolution check.
DOMAIN_TOOL_NAMES: tuple[str, ...] = (
    "query_health_record",
    "compare_health_index",
    "list_reports",
    "upload_medical_report",
)

# 有副作用的工具：**不允许执行器重试**。`upload_medical_report` 会写 ingestion 台账，
# 重试一次就多一条记录。装配点（domains/registry.build_registry）据此分流注册。
# 声明在域自身而不是装配点：谁能安全重试是工具的性质，不是宿主的知识。
WRITE_TOOL_NAMES: frozenset[str] = frozenset({"upload_medical_report"})


def resolve_upload_target(file_path: str, upload_dir: str | Path) -> Path | None:
    """把模型的入参解析为**上传目录内**的真实路径；越界或非法一律返回 None。

    ## 为什么必须有这道边界（审查报告 H1）

    `file_path` 是模型可自由填写的参数，而模型又受**上传文档内容**的影响（提示注入）。
    在加入本函数之前，工具唯一的前提是 `p.is_file()`，因此：

      * `upload_medical_report("C:/Windows/win.ini")` 能成功登记；
      * 随后 `POST /api/records/extract` 的兜底分支（`source_file.exists()`）会把该文件
        解析成文本并送进抽取模型 —— 一条完整的"读任意主机文件 → 送出本机"链路；
      * `parsed_text_path()` 还会在受害者文件**同目录**写一份 `.parsed.txt`。

    所以这里不做"净化文件名"，而是直接做**归属校验**：解析后的绝对路径必须落在
    `upload_dir` 之内（含 `..`、符号链接、大小写等全部由 `resolve()` 收敛）。
    允许的形态只有上传端点自己写出来的 `<uploads>/<uuid8>_<name>`。
    """
    try:
        candidate = Path(file_path).expanduser().resolve()
        root = Path(upload_dir).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate == root or not candidate.is_relative_to(root):
        return None
    return candidate


def _sha256_of(path: str) -> str:
    """Hash file bytes - the idempotency key for re-uploads.

    Reading in chunks keeps a multi-hundred-MB scan from being held in memory; the hash is what
    lets the ingestion ledger collapse "same file twice" into one task.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fmt_num(value: float) -> str:
    """6.0 -> "6", 5.5 -> "5.5", 6.15 -> "6.15" - readable, no float dust.

    非有限值（`inf` / `nan`）必须先挡住：`int(inf)` 抛 OverflowError、`int(nan)` 抛
    ValueError，而这个函数在工具返回路径上 —— 一条脏数据会把"查询成功"变成
    "工具执行失败"（审查报告 L3）。脏值原样显示，让用户看见问题本身。
    """
    if not math.isfinite(value):
        return str(value)
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.4f}".rstrip("0")


def _as_float(value: object) -> float | None:
    """把来自 SQLite 行的任意取值收敛成 float；转不了返回 None。

    为什么需要：`row.get(...)` 的静态类型是 `object`，而这个值直接进 `float()` / `_fmt_num()`
    —— 不收敛的话既过不了类型检查，也让"脏数据会让工具崩掉"这件事只体现在运行期。
    """
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _value_str(row: dict[str, object]) -> str:
    """Numeric value + unit, or the verbatim non-numeric text."""
    numeric = _as_float(row.get("index_value"))
    if numeric is not None:
        unit = row.get("unit")
        unit_part = f" {unit}" if unit else ""
        return f"{_fmt_num(numeric)}{unit_part}"
    return str(row.get("value_text") or "(无数值)")


def _source_str(row: dict[str, object]) -> str:
    parts = [str(row.get("report_type") or "")]
    if row.get("institution"):
        parts.append(str(row["institution"]))
    return " · ".join(p for p in parts if p)


def _row_line(row: dict[str, object]) -> str:
    """One archive row as the model should relay it: date (type · place): value (ref) marker."""
    ref = row.get("ref_range")
    ref_part = f"（参考 {ref}）" if ref else ""
    marker = "" if row.get("is_verified") else UNVERIFIED_MARKER
    when = str(row["check_time"])[:10]
    where = _source_str(row)
    return f"{when}（{where}）：{_value_str(row)}{ref_part}{marker}"


def _not_found(user_id: str, index_name: str, query: HealthQueryService, *, scoped: str) -> str:
    """The honest miss: say what was searched for, then offer what actually exists."""
    names = query.index_names(user_id)
    if names:
        return f"档案里没有「{index_name}」的记录{scoped}。现有的指标有：{'、'.join(names)}。"
    return f"档案里没有「{index_name}」的记录{scoped}（档案目前还没有任何指标）。"


def make_domain_tools(
    ingestion: IngestionService,
    query: HealthQueryService,
    *,
    current_user: Callable[[], str],
    upload_dir: str | Path,
) -> list[BaseTool]:
    """Build the health domain's tools.

    `current_user` is a zero-arg callable resolved at invocation time, NOT a model argument:
    the acting user is a security context, the model must not be able to name who it is acting
    as. The app wires it from the session (M2 API); tests inject a constant.

    `upload_dir` 同理是**宿主提供的安全上下文**而不是模型参数：域写工具只允许在这个目录
    内取文件（见 `resolve_upload_target` 的 docstring）。
    """

    @tool("query_health_record")
    def query_health_record(index_name: str, start_date: str = "", end_date: str = "") -> str:
        """按指标名称查询用户健康档案里的历史记录，按时间先后返回每一次的数值、单位与参考区间。

        index_name 支持部分匹配（如 "结石" 能查到 "结石直径"）；start_date / end_date
        可选，格式 YYYY-MM-DD，用于限定报告日期区间。数值未经人工核验时必须原样转述
        【未经人工校验】标记。
        适用边界：用户想看某指标的**原始记录条目**时使用。若用户想知道的是变化、趋势、
        前后对比，请改用 compare_health_index，不要用本工具逐条罗列再自行总结。
        寒暄或询问自身能力时不要调用任何查询工具。
        """
        user = current_user()
        start = start_date.strip() or None
        end = end_date.strip() or None
        scoped = f"（报告日期 {start} ~ {end} 之间）" if start or end else ""
        rows = query.search_indices(user, index_name, start_date=start, end_date=end)
        if not rows:
            return _not_found(user, index_name, query, scoped=scoped)
        body = "\n".join(_row_line(r) for r in rows)
        return f"「{rows[0]['index_name']}」共 {len(rows)} 条记录：\n{body}"

    @tool("compare_health_index")
    def compare_health_index(index_name: str) -> str:
        """对比某项指标在档案里最早一次与最近一次记录的数值变化（含变化量与单位）。

        **当用户想知道某指标的变化、趋势、前后对比、和上次比怎么样时，优先使用本工具**
        （而不是 query_health_record 逐条查询后自行总结）。只有 1 次记录时如实说明无法
        对比，不要编造趋势。数值未经人工核验时结论必须带【未经人工校验】标记。
        """
        user = current_user()
        rows = query.search_indices(user, index_name)
        if not rows:
            return _not_found(user, index_name, query, scoped="")
        if len(rows) == 1:
            return (
                f"「{rows[0]['index_name']}」在档案里只有 1 次记录"
                f"（{str(rows[0]['check_time'])[:10]}：{_value_str(rows[0])}），无法做跨次对比。"
            )
        first, last = rows[0], rows[-1]
        text = (
            f"「{last['index_name']}」共 {len(rows)} 次记录："
            f"{str(first['check_time'])[:10]} 为 {_value_str(first)} → "
            f"{str(last['check_time'])[:10]} 为 {_value_str(last)}"
        )
        v1, v2 = first.get("index_value"), last.get("index_value")
        if v1 is not None and v2 is not None:
            delta = float(v2) - float(v1)  # type: ignore[arg-type]
            if delta == 0:
                text += "（持平）"
            else:
                unit = last.get("unit")
                unit_part = f" {unit}" if unit else ""
                word = "上升" if delta > 0 else "下降"
                text += f"（{word} {_fmt_num(abs(delta))}{unit_part}）"
        if not (first.get("is_verified") and last.get("is_verified")):
            text += UNVERIFIED_MARKER
        return text

    @tool("list_reports")
    def list_reports() -> str:
        """列出用户档案里所有已入库的报告：报告日期、类型、机构与每份报告的指标数量。"""
        user = current_user()
        reports = query.list_reports(user)
        if not reports:
            return (
                "档案里还没有任何已入库的报告。可以用 upload_medical_report 登记报告文件，"
                "或由用户手工录入指标。"
            )
        lines = []
        for r in reports:
            institution = f" · {r['institution']}" if r.get("institution") else ""
            line = (
                f"{str(r['check_time'])[:10]} · {r['report_type']}{institution}"
                f" · {r['n_indices']} 项指标"
            )
            if r.get("note"):
                line += f" · 备注：{r['note']}"
            lines.append(line)
        return f"档案里共有 {len(reports)} 份报告：\n" + "\n".join(lines)

    @tool("upload_medical_report")
    def upload_medical_report(file_path: str) -> str:
        """登记一份**已在系统里**的待解析报告文件（只接受上传目录内的文件），返回 intake 任务 id。

        实际 OCR / 结构化解析在 v2.2 接入；登记后任务处于 pending，重复上传相同文件会复用同一任务。
        出于安全边界，本工具不能读取上传目录之外的任何路径 —— 越界时如实说明，不要改为猜测路径。
        """
        target = resolve_upload_target(file_path, upload_dir)
        if target is None:
            return (
                f"只能登记上传目录内的文件（{Path(upload_dir)}）。"
                "请让用户通过控制台的「上传」入口提交文件，不要传其它路径。"
            )
        if not target.is_file():
            return f"文件不存在：{file_path}"
        file_hash = _sha256_of(str(target))
        task_id = ingestion.create(
            user_id=current_user(), source_file=str(target), file_hash=file_hash
        )
        existing = ingestion.get(task_id)
        if existing["attempts"]:  # a prior failed/pending task was reused
            return (
                f"已复用已有 intake 任务 {task_id}（status={existing['status']}，"
                f"历史尝试 {existing['attempts']} 次）。相同文件无需重复登记。"
            )
        return (
            f"已登记报告文件，intake 任务 {task_id}（status=pending）。"
            f"相同文件再次上传将复用同一任务。"
        )

    return [query_health_record, compare_health_index, list_reports, upload_medical_report]
