"""「一处只答一次」这一族的用例：密钥名单与布尔写法（审计 `R28-14` 的②③）。

为什么给两条"看起来不会错"的东西写用例：这一族的失败形状从来不是崩，是**两份清单各自自洽**。
`settings.py` 那份 `_SECRET_FIELDS` 与可编辑注册表的 `kind="secret"` 各自都"说得通"，
于是将来新增一个 key 只标在其中一处时，另一处照发明文 —— 症状是"浏览器里看见密钥"，
而它是从一次无害的加字段开始烂的。布尔那两个集合同理：一处认 `yes` 一处不认，
报出来的话是"界面拒绝了我在 .env 里写得通的写法"，没人会怀疑到清单上。
"""

from __future__ import annotations

import pytest

from rolecard_agent.config import (
    FALSY_STRINGS,
    SECRET_FIELD_NAMES,
    TRUTHY_STRINGS,
    env_falsy,
    env_truthy,
)
from rolecard_agent.core import runtime_settings
from rolecard_agent.core.runtime_settings import RUNTIME_FIELDS, FieldSpec


def test_secret_named_list_covers_the_registry() -> None:
    assert runtime_settings.misdeclared_secrets(RUNTIME_FIELDS) == []


def test_a_secret_marked_only_in_the_registry_is_caught() -> None:
    """把同一个字段只标在注册表那一侧 ⇒ 必须点名，而不是静默按非密钥渲染。"""
    fields = RUNTIME_FIELDS + (FieldSpec("brand_new_api_key", "BRAND_NEW_API_KEY", "secret"),)
    assert runtime_settings.misdeclared_secrets(fields) == ["brand_new_api_key"]
    with pytest.raises(RuntimeError, match="SECRET_FIELD_NAMES"):
        runtime_settings._assert_secret_registry_agrees(fields)  # noqa: SLF001


def test_the_masking_side_reads_the_same_list() -> None:
    """界面掩码那一侧引用的必须是同一份对象，而不是"内容一样"的第二份清单。"""
    from rolecard_agent.api.routers import settings as settings_router

    assert settings_router._SECRET_FIELDS is SECRET_FIELD_NAMES  # noqa: SLF001


@pytest.mark.parametrize("text", sorted(TRUTHY_STRINGS))
def test_every_truthy_spelling_is_understood_the_same_way(text: str) -> None:
    assert env_truthy(text) is True
    spec = next(f for f in RUNTIME_FIELDS if f.kind == "bool")
    assert runtime_settings._parse(spec, text) is True  # noqa: SLF001


@pytest.mark.parametrize("text", sorted(FALSY_STRINGS))
def test_every_falsy_spelling_is_understood_the_same_way(text: str) -> None:
    assert env_falsy(text) is True
    spec = next(f for f in RUNTIME_FIELDS if f.kind == "bool")
    assert runtime_settings._parse(spec, text) is False  # noqa: SLF001


def test_the_two_parsers_share_one_set_of_strings() -> None:
    """两处解析走的是同一组集合，不是"两份内容恰好一样"的集合。

    这条用例的全部意义在最后三行：把共享集合临时改小，`_parse` 的行为**必须跟着变**。
    如果它里面还留着自己的字面量元组，它对 "yes" 的判断不受影响 —— 那正是"两份清单"的病灶，
    而只有在这种时刻才会现形。
    """
    import rolecard_agent.config as config_module

    assert runtime_settings.env_truthy is env_truthy
    spec = next(f for f in RUNTIME_FIELDS if f.kind == "bool")
    assert runtime_settings._parse(spec, "yes") is True  # noqa: SLF001
    monkeypatch = pytest.MonkeyPatch()
    with monkeypatch.context():
        monkeypatch.setattr(config_module, "TRUTHY_STRINGS", frozenset({"1"}))
        assert env_truthy("yes") is False
        with pytest.raises(ValueError):
            runtime_settings._parse(spec, "yes")  # noqa: SLF001
    monkeypatch.undo()
