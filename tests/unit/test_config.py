"""Config parsing: env in, Settings out.  Traceability: US-5, US-8.

The interesting cases are the failure ones. A malformed MODEL_BACKENDS must raise rather than
quietly fall back to the local default, because a silent fallback turns a misconfiguration
into "the model is answering oddly" three hours later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rolecard_agent.config import DEFAULT_SILICONFLOW_BASE_URL, Settings


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
    assert settings.agent_default_mode == "chat"  # 出厂默认 = 普通对话


def test_agent_default_mode_from_env() -> None:
    assert Settings.from_env({"AGENT_DEFAULT_MODE": "agent"}).agent_default_mode == "agent"
    # 空串 = 未设置（回落默认），不能被当成非法值炸启动
    assert Settings.from_env({"AGENT_DEFAULT_MODE": ""}).agent_default_mode == "chat"


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
    assert settings.backend("local").base_url == "http://localhost:11434"
    assert settings.backend("cloud").api_key == "sk-x"


def test_malformed_backends_raises() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        Settings.from_env({"MODEL_BACKENDS": "{not json"})


def test_backends_must_be_an_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        Settings.from_env({"MODEL_BACKENDS": '["local"]'})


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


def test_siliconflow_credentials_and_ocr_python_parse_from_env() -> None:
    """P1-4：`SILICONFLOW_*` / `OCR_PYTHON` 必须经 Settings 落地，而不是在 rag 里直读 env。

    这三把值以前有一半是在 `rag/retriever.py`、`rag/ocr.py` 里 `os.environ.get` 取的 ——
    配置契约（.env.example 对齐、掩码、dead-config 检查、运行环境覆盖层）全都管不到它们。
    本用例把它们钉成"env 只在这里参与一次"。
    """
    parsed = Settings.from_env(
        {
            "SILICONFLOW_API_KEY": "sk-x",
            "SILICONFLOW_BASE_URL": "https://mirror.example/v1",
            "OCR_PYTHON": ".venv-ocr/Scripts/python.exe",
        }
    )
    assert parsed.siliconflow_api_key == "sk-x"
    assert parsed.siliconflow_base_url == "https://mirror.example/v1"
    assert parsed.ocr_python == ".venv-ocr/Scripts/python.exe"

    # 未设置：key 为空（"配了才用"），base_url 用出厂常量（云端端点行未填时的兜底）。
    defaults = Settings.from_env({})
    assert defaults.siliconflow_api_key is None
    assert defaults.siliconflow_base_url == DEFAULT_SILICONFLOW_BASE_URL
    assert defaults.ocr_python is None
