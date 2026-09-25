"""`core/open_threads.py` 的解析与失败面。

钉的是这一源唯一要紧的品格：**不凑数**。所以断言几乎全在"什么情况下必须返回空"上，
而不是"能不能挖出话题"上 —— 挖得出来挖不出来是模型的事，那是提示词的活；
而"模型胡说时界面跟着胡说"是代码的责任。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from rolecard_agent.core.open_threads import (
    MAX_OPEN_THREADS,
    find_open_threads,
    parse_open_threads,
)


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.last_prompt = ""

    def invoke(self, prompt: Any) -> AIMessage:
        self.calls += 1
        self.last_prompt = str(prompt)
        return AIMessage(content=self.text)


class _Boom:
    def invoke(self, _prompt: Any) -> Any:
        raise RuntimeError("后端没起来")


# ------------------------------------------------------------------ 解析


def test_only_open_lines_survive() -> None:
    got = parse_open_threads(
        "OPEN 周五那家新开的店到底去没去\n"
        "OPEN：体检报告出来了没有\n"
        "我觉得这些都挺好的\n"
        "OPEN 妈妈手术的日子定了吗"
    )
    assert got == ["周五那家新开的店到底去没去", "体检报告出来了没有", "妈妈手术的日子定了吗"]


def test_nothing_means_nothing() -> None:
    """`NONE`、空串、通篇废话 —— 全都是空清单，不是"挑一条最像的"。"""
    assert parse_open_threads("NONE") == []
    assert parse_open_threads("") == []
    assert parse_open_threads("抱歉，我没有发现未收尾的话题。") == []
    assert parse_open_threads(AIMessage(content="NONE")) == []


def test_capped_at_three_and_overlong_topics_are_trimmed() -> None:
    many = "\n".join(f"OPEN 话题{i}" for i in range(9))
    assert len(parse_open_threads(many)) == MAX_OPEN_THREADS
    long_one = parse_open_threads("OPEN " + "很" * 80)
    assert len(long_one) == 1 and len(long_one[0]) <= 40, "超长的整条截断，不收两条"


def test_decoration_is_stripped_but_an_empty_shell_is_dropped() -> None:
    assert parse_open_threads('OPEN "下周去复查\""') == ["下周去复查"]
    assert parse_open_threads("OPEN") == []
    assert parse_open_threads("OPEN    ") == []


# ------------------------------------------------------------------ 调用面


def test_no_history_means_no_model_call() -> None:
    fake = _Reply("OPEN 不该被问出来")
    assert find_open_threads("", fake) == []
    assert find_open_threads("   ", fake) == []
    assert fake.calls == 0, "没内容就别花这一次调用"


def test_a_failing_model_reports_none_rather_than_an_empty_answer() -> None:
    """这一源坏了的正确表现仍然是"不抛"，但**必须与"扫过了、没有"分得开**。

    `[]` = 调用成功、判没有没收尾的事；`None` = 这次调用没成功。混成一个 `[]` 的时候，
    调用方只能"失败也写扫描时刻"，于是一次网络抖动把这一源关掉整个缓存周期
    （09-26 轮 R26-11）。不抛这一半没变：异常绝不该让一轮开口失败。
    """
    assert find_open_threads("用户：我下周要复查", _Boom()) is None


def test_the_prompt_carries_the_recent_lines_verbatim() -> None:
    """素材是宿主给的那段文本，本模块不重抄一份格式化逻辑（措辞只该有一处）。"""
    fake = _Reply("NONE")
    find_open_threads("- 用户：周五要去体检\n- 你：好，结果出来跟我说", fake)
    assert fake.calls == 1
    assert "周五要去体检" in fake.last_prompt
    assert "结果出来跟我说" in fake.last_prompt
