"""一致性判据的**兼容聚合面**（P3-9「按主题细分」收尾后的形状）。

判据正文已按主题搬进八个模块（`checks_install` / `checks_config` / `checks_audit` /
`checks_docs` / `checks_domain` / `checks_storage` / `checks_runtime` / `checks_meta`）；
**执行顺序的唯一出处是 `registry.CHECKS`**，本模块不再持有任何判据正文。

它只做一件事：**把分散在各族的名字重新聚合回 `checks` 这一个属性面**。原因是若干历史
消费者是「按属性取」而不是按模块取 —— `scripts/check_consistency.py` 的 `_ForwardingModule`
（测试既**读** `cc._png_corner_alphas`，又**写** `monkeypatch.setattr(cc, "ROOT", …)`）、
`build_audit_index.py` 的 `ruler._citation_targets` / `ruler._SECTION_RE`、以及
`from consistency.checks import …` 的显式导入。有这一层，改"判据住哪"就不用动每一个
消费者；没有它，搬家会牵出一串调用点改动。

**只转发、不复制定义** —— 抄一份就是第二事实面，正是本仓反复治的那一族病。
"""
from __future__ import annotations

from .checks_audit import (  # noqa: F401
    _BANNED_USER_VISIBLE,
    _CITATION_DOC_KEYS,
    _CITATION_PROSE_ONLY,
    _LEDGER_STATUS_MARKERS,
    _PID_RE,
    _SECTION_RE,
    _TARGET_HEAD_RE,
    _TARGET_ROW_RE,
    DEFERRED_US,
    _audit_index,
    _audit_ledger_path,
    _covered_user_stories,
    _git_ignored,
    _ledger_closure_traced,
    _ledger_h2_span,
    _required_user_stories,
    check_audit_action_vocabulary,
    check_audit_index_in_sync,
    check_audit_ledger_row_count,
    check_citation_reachability,
    check_ledger_status_states_verdict,
    check_us_traceability,
    report_line_budget,
)
from .checks_config import (  # noqa: F401
    _PLATFORM_ENV,
    RESERVED_SETTINGS,
    _env_names_read_in_src,
    check_config_contract,
    check_dead_config,
    check_env_example_models,
    check_startup_env_documented,
)
from .checks_docs import (  # noqa: F401
    _BARE_DATE,
    _FULL_DATE,
    _HEADER_DATE,
    _HEADLINE_ZONE_LINES,
    _READINGS_LINK,
    _citation_targets,
    _dates_in,
    _is_table_delim,
    _unescaped_pipes,
    check_doc_freshness,
    check_doc_links,
    check_doc_references,
    check_markdown_table_shape,
    check_promised_artifacts,
    check_readme_headline_numbers,
    check_readme_quickstart,
)
from .checks_domain import (  # noqa: F401
    API_DOMAIN_SEAMS,
    GENERIC_DOMAIN_MODULES,
    _domain_private_tokens,
    _sql_table_names,
    check_api_domain_seams,
    check_core_no_domain_token,
    check_domain_isolation,
)
from .checks_install import (  # noqa: F401
    LOCK_SURFACES,
    RUNTIME_REQ_FILES,
    _cmd_covers,
    _family_of,
    _installed_families,
    _job_block,
    _lock_pins,
    _module_to_distribution,
    _package_constraints,
    _package_names,
    _runtime_declared_names,
    _runtime_form_marker,
    _spec_runtime_packages,
    check_capability_matrix,
    check_dependency_parity,
    check_installer_scope,
    check_lockfile_parity,
    check_pyproject,
    check_requirements_scope,
    check_spec_runtime_vs_requirements,
)
from .checks_meta import (  # noqa: F401
    IDENTITY_IMPLICIT_READS,
    _enclosing_func,
    _imported_modules,
    check_changelog,
    check_ci_host_python_stdlib_only,
    check_coverage_threshold,
    check_identity_implicit_reads_are_registered,
    check_session_thread_write_seam,
    check_stale_identifiers,
    check_version_parity,
)
from .checks_runtime import (  # noqa: F401
    _BUNDLED_COPY_ALLOWED,
    _BUNDLED_GENDERED,
    _DATA_ROOT_EXEMPT,
    _DEPLOY_REQUIRED_ENV,
    SINGLE_SOURCE_LITERALS,
    _compose_app_env,
    _crlf_exempt_globs,
    _data_root_write_dirs,
    _div_chain_parts,
    _normalise_question,
    _path_chain_parts,
    _png_corner_alphas,
    check_app_icon_frames,
    check_artifact_single_source,
    check_bundled_copy,
    check_console_encoding,
    check_data_root_dirs_gitignored,
    check_dependency_layering,
    check_deploy_env_parity,
    check_exemplar_leaks_eval_answers,
    check_line_endings,
    check_local_service_port_single_source,
    check_milestone_alignment,
    check_ps1_encoding,
    check_role_whitelists_resolve,
    check_safety_prompt,
    check_single_source_literals,
    check_v1_v2_boundary,
    check_vocabulary,
)
from .checks_storage import (  # noqa: F401
    _SQL_SHAPE,
    LOGLINE_MIN_CALLS,
    RAW_PRINT_ALLOWLIST,
    WRITE_TXN_HELPERS,
    _code_strings,
    _declared_table_names,
    _statements_of,
    check_api_holds_no_sql,
    check_dangling_write_txns,
    check_log_channels_unified,
    check_shape_migration_ddl,
    check_single_text_extractor,
    check_storage_db_has_no_business_tables,
    check_sync_write_ownership,
    check_write_txn_ownership_inventory,
    find_dangling_write_txns,
)
from .core import ROOT, fails, iter_files, out, warns  # noqa: F401
