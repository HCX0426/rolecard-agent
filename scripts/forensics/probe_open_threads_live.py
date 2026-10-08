""""未收尾话题"的实机验收：拿真模型跑三份对话，看它**该空的时候空不空，该抓的时候抓不抓**。

这一源的成败不在"能不能挖出话题"（任何模型都能编三条），而在"没话的时候它闭不闭嘴"。
所以三份样本里两份是**全都收尾了**与**纯寒暄**，正确答案都是空；只有一份真留了个尾巴。

同时打印**模型的原始回话**：本地那次三份全空，分不清是"判不出"还是"把预算花在思考上、
正文为空"（qwen3-vl 在 §12.12 里就是这个行为）—— 两者的修法完全不同。

跑法（后端配置从**库副本**读，不碰真库；模型自己连）：

    SQLITE_PATH=build/scratch-stop.db \\
    OPEN_THREADS_PROBE_BACKEND=siliconflow \\
    PYTHONIOENCODING=utf-8 PYTHONPATH=src \\
        .venv/Scripts/python.exe scripts/probe_open_threads_live.py
"""

from __future__ import annotations

import os
from pathlib import Path

from rolecard_agent.base.identity import resolve_instance_identity
from rolecard_agent.base.text import text_of
from rolecard_agent.config import Settings
from rolecard_agent.core.agent.graph import build_model
from rolecard_agent.core.model_settings import ModelSettingsService
from rolecard_agent.core.open_threads import _PROMPT, MAX_OPEN_THREADS, parse_open_threads
from rolecard_agent.storage.db import connect

CASES: list[tuple[str, str, bool]] = [
    (
        "留了尾巴（期望：至少一条）",
        "- 用户：我下周要体检，结果出来跟你说\n- 你：好，我等你说\n- 用户：嗯还在上班",
        True,
    ),
    (
        "故事讲到一半被打断（期望：至少一条）",
        "- 用户：昨天发生了一件特无语的事\n- 你：怎么说？\n- 用户：算了，回头再说\n- 你：好",
        True,
    ),
    (
        "明确说回头再聊（期望：至少一条）",
        "- 用户：我最近在考虑换工作，还没想好，回头跟你聊\n- 你：行，想聊随时来",
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
    env = Settings.from_env()
    db = os.environ.get("SQLITE_PATH") or str(Path("build/scratch-stop.db").resolve())
    conn = connect(Path(db))
    # 本机主人那一族凭据（M2d）：这台机器带着谁的 key 就花谁的。
    settings = ModelSettingsService(conn).effective_settings(
        env, user_id=resolve_instance_identity(env)
    )
    name = os.environ.get("OPEN_THREADS_PROBE_BACKEND") or settings.model_default
    backend = settings.backend(name)
    model = build_model(settings, name)
    print(f"后端：{name} / {backend.model} / 库副本：{db}\n")
    wrong = 0
    for label, turns, expect_any in CASES:
        prompt = _PROMPT.format(limit=MAX_OPEN_THREADS, n=len(turns.splitlines()), turns=turns)
        raw = text_of(model.invoke(prompt))
        got = parse_open_threads(raw)
        ok = bool(got) if expect_any else not got
        wrong += 0 if ok else 1
        print(f"[{'OK ' if ok else '不OK'}] {label}")
        print(f"       原始回话 {raw[:90]!r}")
        print(f"       解析出来 {got or '（空）'}")
    conn.close()
    print(f"\n判错 {wrong} / {len(CASES)} 份。")
    print("读法：三份全空 + 原始回话也空 = 模型没产出（多半是思考 token 吃满，§12.12 那个行为）；")
    print("     原始回话有内容而解析为空 = 它不守 `OPEN <…>` 格式，那是提示词/解析的活。")


if __name__ == "__main__":
    main()
