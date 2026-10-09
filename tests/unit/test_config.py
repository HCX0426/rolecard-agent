"""Config parsing: env in, Settings out.  Traceability: US-5, US-8.

The interesting cases are the failure ones. A malformed MODEL_BACKENDS must raise rather than
quietly fall back to the local default, because a silent fallback turns a misconfiguration
into "the model is answering oddly" three hours later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rolecard_agent.config import (
    DEFAULT_SILICONFLOW_BASE_URL,
    ModelBackend,
    Settings,
)


def test_defaults_are_usable_without_any_env() -> None:
    settings = Settings()
    backend = settings.backend()
    # 2026-09-17 终版：默认本地模型是 qwen3-vl:8b（对话 + tools + 识图 一体，一行多用）；
    # qwen2.5:7b / qwen2.5vl:7b / local_vl 均已退役，配置里不应再出现它们。
    assert backend.model == "qwen3-vl:8b"
    assert backend.provider == "ollama"
    assert settings.obs_backend == "local"
    assert settings.obs_emit_raw_text is False  # redacted by default
    assert settings.memory_enabled is True
    assert settings.agent_default_mode == "agent"  # 出厂默认 = 智能体（2026-10-10 统一走智能体）


def test_agent_default_mode_from_env() -> None:
    assert Settings.from_env({"AGENT_DEFAULT_MODE": "agent"}).agent_default_mode == "agent"
    # 空串 = 未设置（回落默认），不能被当成非法值炸启动
    assert Settings.from_env({"AGENT_DEFAULT_MODE": ""}).agent_default_mode == "agent"
    # 想回到一问一答：显式给 chat（出厂默认虽已是 agent，本项仍可覆盖）
    assert Settings.from_env({"AGENT_DEFAULT_MODE": "chat"}).agent_default_mode == "chat"


def test_parses_backends_from_json() -> None:
    settings = Settings.from_env(
        {
            "MODEL_BACKENDS": (
                '{"local": {"model": "qwen2.5vl:7b"},'
                ' "cloud": {"model": "deepseek-chat", "provider": "openai",'
                ' "base_url": "https://api.example.com/v1", "api_key": "sk-x"}}'
            ),
            "MODEL_DEFAULT": "cloud",
        }
    )
    assert settings.backend().model == "deepseek-chat"
    # 默认地址现读 config 的单源常量（127.0.0.1 形制，口径注释在常量旁边）
    assert settings.backend("local").base_url == "http://127.0.0.1:11434"
    assert settings.backend("cloud").api_key == "sk-x"


def test_malformed_backends_raises() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        Settings.from_env({"MODEL_BACKENDS": "{not json"})


def test_backends_must_be_an_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        Settings.from_env({"MODEL_BACKENDS": '["local"]'})


def test_a_misspelled_backend_field_is_rejected_at_startup() -> None:
    """拼错一个字段名要**当场**报错，不能静悄悄用默认值跑（S-1 加 `extra="forbid"`）。

    pydantic 默认忽略未知字段，所以从前写错 `num_ct` 等于没写：那一项照常起来，只是
    "我明明设了 num_ctx"从来没生效 —— 而写配置的人不会去查一个不存在的报错。
    """
    with pytest.raises(ValueError, match="invalid MODEL_BACKENDS config"):
        Settings.from_env({"MODEL_BACKENDS": '{"cloud": {"model": "m", "num_ct": 8192}}'})


def test_unknown_backend_raises_with_the_known_names() -> None:
    settings = Settings()
    with pytest.raises(KeyError, match="configured: local"):
        settings.backend("nope")


def test_fallbacks_are_split_and_trimmed() -> None:
    settings = Settings.from_env({"MODEL_FALLBACKS": "cloud, backup ,"})
    assert settings.model_fallbacks == ["cloud", "backup"]


def test_redaction_flag_is_parsed_in_both_directions() -> None:
    assert Settings.from_env({"OBS_EMIT_RAW_TEXT": "true"}).obs_emit_raw_text is True
    assert Settings.from_env({"OBS_EMIT_RAW_TEXT": "0"}).obs_emit_raw_text is False


def test_paths_are_parsed() -> None:
    settings = Settings.from_env({"SQLITE_PATH": "/tmp/x.db", "OBS_LOG_PATH": "/tmp/trace.jsonl"})
    assert settings.sqlite_path == Path("/tmp/x.db")
    assert settings.obs_log_path == Path("/tmp/trace.jsonl")


def test_unknown_backend_reference_does_not_break_construction() -> None:
    """A role may name a backend that the deployment has not configured; that must surface
    when the role is used, not while parsing."""
    settings = Settings.from_env({"MODEL_DEFAULT": "local"})
    assert settings.backend("local").provider == "ollama"


# ------------------------------------------------------------------- fallback chain
#
# The chain used to be parsed and then ignored (技术评审与决策.md §9 A4). The resolution rules
# live here, in a pure function, because `build_model` itself needs real provider packages to
# exercise - the part worth testing is which names survive and in what order.


def test_no_fallbacks_by_default() -> None:
    assert Settings().resolve_fallbacks() == []


def test_primary_is_never_its_own_fallback() -> None:
    """Falling back to the backend that just failed is not a fallback.

    Note the primary when no backend is named is `model_default`, not "the first one listed".
    """
    settings = Settings.from_env(
        {
            "MODEL_BACKENDS": '{"cloud": {"model": "a", "provider": "openai"}}',
            "MODEL_DEFAULT": "cloud",
            "MODEL_FALLBACKS": "cloud,local",
        }
    )
    assert settings.resolve_fallbacks() == ["local"]


def test_fallback_chain_preserves_configured_order() -> None:
    settings = Settings.from_env(
        {
            "MODEL_BACKENDS": (
                '{"a": {"model": "1", "provider": "openai"},'
                ' "b": {"model": "2", "provider": "openai"}}'
            ),
            "MODEL_DEFAULT": "a",
            "MODEL_FALLBACKS": "b,local,a",
        }
    )
    assert settings.resolve_fallbacks() == ["b", "local"]


def test_unknown_names_are_dropped_rather_than_crashing_mid_conversation() -> None:
    settings = Settings.from_env({"MODEL_FALLBACKS": "typo,local"})
    assert settings.resolve_fallbacks("cloud") == ["local"]


def test_duplicates_are_collapsed() -> None:
    settings = Settings.from_env({"MODEL_FALLBACKS": "local,local"})
    assert settings.resolve_fallbacks("cloud") == ["local"]


def test_chain_is_capped() -> None:
    """Longer chains make a failure harder to localise and hide degraded answers."""
    settings = Settings.from_env(
        {
            "MODEL_BACKENDS": (
                '{"a": {"model": "1", "provider": "openai"},'
                ' "b": {"model": "2", "provider": "openai"},'
                ' "c": {"model": "3", "provider": "openai"}}'
            ),
            "MODEL_FALLBACKS": "a,b,c",
        }
    )
    assert settings.resolve_fallbacks("local") == ["a", "b"]


def test_a_role_picking_a_lower_priority_backend_keeps_the_global_default() -> None:
    """角色级高于全局优先级 = **换 primary**，不是把操作员排在第一那台从降级路径上删掉。

    修前候选池只有 `model_fallbacks` 那一段，于是角色挑了靠后那台之后，全局默认（最被信任
    的那台）反而不再给它兜底 —— 用户 2026-09-25 要这条时指的是"角色级 > 优先级"，而当时
    的实现连"默认那台还在链上"一起改掉了，是顺手丢的。
    """
    backends = {
        n: ModelBackend(model=f"m-{n}", provider="openai") for n in ("a", "b", "c")
    }
    settings = Settings(model_backends=backends, model_default="a", model_fallbacks=["b", "c"])
    # 默认自己当 primary：与修前逐字相同（"不回退到自己"那条规则把它丢掉）
    assert settings.resolve_fallbacks() == ["b", "c"]
    # 角色挑了优先级里靠后那台：它当 primary，而 a 回到链首
    assert settings.resolve_fallbacks("c") == ["a", "b"]
    assert settings.resolve_fallbacks("b") == ["a", "c"]


def test_backend_name_names_the_one_that_will_actually_serve() -> None:
    """用量账本要的是"**实际接话那台**"，不是"角色卡上写的那台"，更不是 NULL。

    角色级后端选择上线后，"没设置"是常态：账上若记成 NULL，按后端分组的那页就会凭空
    少掉一批调用，看起来像 bug（而它只是没人把降级后的名字带回来）。
    """
    backends = {
        "a": ModelBackend(model="m-a", provider="openai"),
        "b": ModelBackend(model="m-b", provider="openai"),
    }
    settings = Settings(model_backends=backends, model_default="a")
    assert settings.backend_name(None) == "a"  # 没声明 → 全局默认
    assert settings.backend_name("b") == "b"  # 声明了就按声明（角色级 > 全局优先级）
    assert settings.backend_name("gone") == "a"  # 声明的那台被删了 → 与降级同源

# -- 数值型环境变量：`0` 必须被保留（代码审查报告（第二轮）L1） --------------------


def test_zero_is_a_real_value_not_a_missing_one() -> None:
    """`MODEL_TIMEOUT_SECONDS=0` 必须真的关掉超时。

    修复前解析用的是真值判断（`if value := src.get(k)`），`"0"` 是**真值字符串**但
    旧代码走的是 `src.get(k)` 的布尔语义 —— 空串被跳过是对的，而 `"0"` 一并被跳过就成了
    静默失效：`config.py` 的字段注释明确写着"设成 0 或负数 = 不设超时（仅调试用）"，
    但这个开关在 env 路径上根本不可用。
    """
    settings = Settings.from_env({"MODEL_TIMEOUT_SECONDS": "0"})
    assert settings.model_timeout_seconds == 0

    settings = Settings.from_env({"CONTEXT_MAX_CHARS": "0", "TOOL_TIMEOUT_SECONDS": "0"})
    assert settings.context_max_chars == 0
    assert settings.tool_timeout_seconds == 0


def test_empty_string_still_means_unset() -> None:
    """空串 = 未设置（否则 `AUTH_CREDENTIALS=` 会把默认值覆盖成空 —— 那是对的一半，
    但 `MODEL_TIMEOUT_SECONDS=` 这种写法不该变成 0）。"""
    settings = Settings.from_env({"MODEL_DEFAULT": "", "AUTH_CREDENTIALS": ""})
    assert settings.model_default == "local"
    assert settings.model_timeout_seconds == 120.0


def test_context_and_tool_budgets_have_sane_defaults() -> None:
    """两个新预算都得有非零默认值 —— 默认值就是"忘了配也不会坏"。"""
    settings = Settings()
    assert settings.context_max_chars > 0
    assert settings.tool_timeout_seconds > 0
    assert settings.auth_trusted_proxies == ""  # 默认不信任任何代理


def test_web_master_switch_and_thinking_parse_from_env() -> None:
    """功能①②的 env 解析：总闸 bool、白名单串、思考总开关三态只认 auto/off 之外原样透传。"""
    settings = Settings.from_env(
        {
            "WEB_SEARCH_ENABLED": "0",
            "WEB_ALLOWED_DOMAINS": "wikipedia.org, arxiv.org",
            "MODEL_THINKING": "off",
        }
    )
    assert settings.web_search_enabled is False
    assert settings.web_allowed_domains == "wikipedia.org, arxiv.org"
    assert settings.model_thinking == "off"

    # 缺省 = 开 + 不限 + auto（空串视为未设）
    defaults = Settings.from_env({"WEB_SEARCH_ENABLED": "", "MODEL_THINKING": ""})
    assert defaults.web_search_enabled is True
    assert defaults.web_allowed_domains == ""
    assert defaults.model_thinking == "auto"


def test_shipped_defaults_ask_for_reasoning_from_the_bundled_local_model() -> None:
    """随包那个本地模型必须**正好**落在思考名单里。

    名单是按 `model` 名精确匹配的（`core/agent/graph.py`），所以"把默认后端换个模型名"会**静默**
    把它从名单里摘出去：思考照想、那十几~几十秒照花、屏幕上一个字都不显示 —— 那正是
    09-26 轮 `R26-29` 量出来的形状（不列名时一次回话生成 366~3809 token 而可见正文几十字），
    也是用户提"要让她看起来在打字"的直接原因。这条断言钉的是**配对**，不是名单非空：
    两边各自改都对，改歪一边就回到空泡。
    """
    shipped = Settings()
    assert shipped.model_default
    assert shipped.backend(shipped.model_default).model in shipped.model_thinking_models


def test_siliconflow_endpoint_and_ocr_python_parse_from_env() -> None:
    """P1-4 + P1-5：仍归 env 的只剩两项 —— 云端兜底端点、本地 RapidOCR 解释器。

    `SILICONFLOW_API_KEY` 不再是 Settings 字段：凭据的家是模型页（DB 是事实面），env 那把
    key 只被 scripts/run_api.py 用作"首启注册一个硅基流动后端行"的引导输入。
    RAG_EMBEDDING / RAG_RERANK / OCR_BACKEND / OCR_API_KEY 那一族开关则整个删掉了 ——
    它们在 .env.example 上宣传"可改"，而工厂里对应的分支生产上从不执行（服务页恒有启用的
    内置行）。一个改了不生效的开关比没有开关更糟（架构审计报告 P1-5）。
    """
    parsed = Settings.from_env(
        {
            "SILICONFLOW_BASE_URL": "https://mirror.example/v1",
            "OCR_PYTHON": ".venv-ocr/Scripts/python.exe",
        }
    )
    assert parsed.siliconflow_base_url == "https://mirror.example/v1"
    assert parsed.ocr_python == ".venv-ocr/Scripts/python.exe"

    # 未设置 = 出厂兜底端点（只有一处常量）+ 自动发现 .venv-ocr。
    defaults = Settings.from_env({})
    assert defaults.siliconflow_base_url == DEFAULT_SILICONFLOW_BASE_URL
    assert defaults.ocr_python is None

    names = set(Settings.model_fields)
    assert not {
        "siliconflow_api_key",
        "embedding_backend",
        "rag_rerank",
        "ocr_backend",
        "ocr_api_key",
    } & names
