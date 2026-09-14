"""Domain tools: query_health_record / compare_health_index / list_reports /
upload_medical_report.

Unverified values must carry the marker inside the TOOL RETURN TEXT
("[AI extracted, not human verified]") - never rely on the model to add it.

NOTE: there is deliberately NO retrieval tool here. Document search is a KERNEL capability
(rag/retriever.py exposes `search_knowledge`) because it must work for every domain, not
just this one. A domain that needs a narrower scope passes it as an argument.
"""

from __future__ import annotations

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
