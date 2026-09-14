"""Domain tools: query_health_record / compare_health_index / list_reports /
upload_medical_report.

Unverified values must carry the marker inside the TOOL RETURN TEXT
("[AI extracted, not human verified]") - never rely on the model to add it.

NOTE: there is deliberately NO retrieval tool here. Document search is a KERNEL capability
(rag/retriever.py exposes `search_knowledge`) because it must work for every domain, not
just this one. A domain that needs a narrower scope passes it as an argument.

`upload_medical_report` is the one tool implemented here today: it is the WRITER for the
kernel `ingestion_task` ledger, and is what makes that table real rather than a dead schema
(技术评审与决策.md §9 B1). The other three names are declared up front so the built-in role's
whitelist resolves against known names; their bodies land in M3.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from rolecard_agent.core.ingestion import IngestionService

# The names this domain contributes, declared before the implementations land in M3.
#
# Declared rather than inferred because two other places already depend on them being known:
# `roles/seed.py` builds the built-in role's whitelist from them, and
# `tests/unit/test_builtin_tools.py` asserts every whitelisted name resolves to something.
# Before this existed, the whitelist pointed at names that appeared nowhere in the code.
DOMAIN_TOOL_NAMES: tuple[str, ...] = (
    "query_health_record",
    "compare_health_index",
    "list_reports",
    "upload_medical_report",
)


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


def make_domain_tools(
    ingestion: IngestionService, *, current_user: Callable[[], str]
) -> list[BaseTool]:
    """Build the health domain's tools.

    `current_user` is a zero-arg callable resolved at invocation time, NOT a model argument:
    the acting user is a security context, the model must not be able to name who it is acting
    as. The app wires it from the session (M2 API); tests inject a constant.
    """

    @tool("upload_medical_report")
    def upload_medical_report(file_path: str) -> str:
        """登记一份待解析的体检报告文件，返回 intake 任务 id。

        实际 OCR / 结构化解析在 v2.2 接入；登记后任务处于 pending，重复上传相同文件会复用同一任务。
        """
        p = Path(file_path)
        if not p.is_file():
            return f"文件不存在：{file_path}"
        file_hash = _sha256_of(file_path)
        task_id = ingestion.create(
            user_id=current_user(), source_file=file_path, file_hash=file_hash
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

    # Only upload_medical_report is implemented in v1; the rest join in M3. Returning a single
    # tool keeps the registry honest: nothing is bound that cannot run.
    return [upload_medical_report]
