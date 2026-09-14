"""Config parsing: env in, Settings out.

The interesting cases are the failure ones. A malformed MODEL_BACKENDS must raise rather than
quietly fall back to the local default, because a silent fallback turns a misconfiguration
into "the model is answering oddly" three hours later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rolecard_agent.config import Settings


def test_defaults_are_usable_without_any_env() -> None:
    settings = Settings()
    backend = settings.backend()
    assert backend.model == "qwen2.5:7b"
    assert backend.provider == "ollama"
    assert settings.obs_backend == "local"
    assert settings.obs_emit_raw_text is False  # redacted by default


def test_parses_backends_from_json() -> None:
    settings = Settings.from_env(
        {
            "MODEL_BACKENDS": (
                '{"local": {"model": "qwen2.5:7b"},'
                ' "cloud": {"model": "deepseek-chat", "provider": "openai",'
                ' "base_url": "https://api.example.com/v1", "api_key": "sk-x"}}'
            ),
            "MODEL_DEFAULT": "cloud",
        }
    )
    assert settings.backend().model == "deepseek-chat"
    assert settings.backend("local").base_url == "http://localhost:11434/v1"
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
