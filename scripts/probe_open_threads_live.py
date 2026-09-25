""""未收尾话题"的实机验收：拿真模型跑三份对话，看它**该空的时候空不空**。

这一源的成败不在"能不能挖出话题"（任何模型都能编三条），而在"没话的时候它闭不闭嘴"。
所以三份样本里两份是**全都收尾了**与**纯寒暄**，正确答案都是空；只有一份真留了个尾巴。

跑法（要 Ollama 在跑，默认打本机的 qwen3-vl:8b）：

    PYTHONIOENCODING=utf-8 PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_open_threads_live.py

不碰任何库：这里只有模型调用，没有写盘。
"""

from __future__ import annotations

import os

from rolecard_agent.config import Settings
from rolecard_agent.core.graph import build_model
from rolecard_agent.core.open_threads import find_open_threads

CASES: list[tuple[str, str, bool]] = [
    (
        "留了尾巴（期望：至少一条）",
        "- 用户：我下周要体检，结果出来跟你说\n- 你：好，我等你说\n- 用户：嗯还在上班",
        True,
    ),
    (
        "全都收尾了（期望：空）",
        "- 用户：我下周要体检\n- 你：好，记得空腹\n- 用户：查完了，一切正常\n- 你：那就好",
        False,
    ),
    (
        "纯寒暄（期望：空）",
        "- 用户：在吗\n- 你：在呀\n- 用户：没事，就问问\n- 你：随时找我",
        False,
    ),
]


def main() -> None:
    settings = Settings.from_env()
    name = os.environ.get("OPEN_THREADS_PROBE_BACKEND") or (
        "local" if "local" in settings.model_backends else settings.model_default
    )
    model = build_model(settings, name)
    print(f"模型：{name} / {settings.backend(name).model}\n")
    misses = 0
    for label, turns, expect_any in CASES:
        got = find_open_threads(turns, model)
        ok = bool(got) if expect_any else not got
        misses += 0 if ok else 1
        print(f"[{'OK ' if ok else '不OK'}] {label}\n       → {got or '（空）'}")
    print(f"\n判错 {misses} / {len(CASES)} 份。"
          "\n注：'留了尾巴'那份只要求它**别什么都编**，不要求它一定抓到 —— 漏报是这一源允许的错法。")


if __name__ == "__main__":
    main()
