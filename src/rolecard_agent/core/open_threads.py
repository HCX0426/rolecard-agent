"""「未收尾话题」—— 主动开口的第五个由头（设计稿 §8.2 第 5 条 / §8.5 的 P2）。

前四个由头（定时 / 关系值 / 回忆命中 / 文件变化）说的都是"什么时候该说话"，唯独没有
"**记着哪件事说到一半**"。而人身上最像活人的那一句往往是："上次你说要试的那家店，后来去了吗。"

这一源的全部价值在于它**稀有**：设计稿原话是"默认返回 `[]`、宁可漏报也不要凑数、至多 3 条"。
编出来的"没收尾的话题"比没有更假 —— 用户一眼就能看出她在硬找话头，而且那会直接教她
往对话里塞不存在的事（§12.5 那条"提取侧不许补背景"是同一件事的另一面）。

所以这里的形状是：**判据写在提示里、条数封顶写在解析里、失败一律退回空**。
缓存与"什么时候值得花这一次调用"在 `core/reachout.py`（那是调度侧的开销问题），
这个模块只管"看着这段对话，说出真正没收尾的那几件"。
"""

from __future__ import annotations

import re
from typing import Any

from rolecard_agent.core.text import text_of

#: 至多几条。设计稿给的是 3，而实践中 1 条就够她开口 —— 多了读起来像清单不像话。
MAX_OPEN_THREADS = 3
#: 一条话题的长度上限（字符）。超了就是模型在复述整段对话，不是"一件可问的事"。
_MAX_TOPIC_CHARS = 40

_OPEN = re.compile(r"^OPEN\s*[:：]?\s*(\S.*)$")

_PROMPT = """你在帮一个角色整理"她记得、但对方说到一半就没了下文"的事。
看下面的对话，判断有没有这样的事。

算"没了下文"要同时满足三条：
1. 那件事是**用户自己说出来的**，不是你根据语气推测的；
2. 到对话最后一句为止它还没有结果 —— 没人回答它，事情本身也没落地（"回头再说"之后就没再提，算没结果）；
3. 它具体到可以再问一次（"下周要去体检"算，"最近挺忙"不算）。

对照着看（这些只是样子，别把它们当内容）：
- 用户说下个月要给猫做绝育，后面再没提过 → 一条：猫绝育那事后来定了没
- 用户说面试完了、也说了结果不理想 → 不算，已经有下文了
- 用户只说了"在吗""随便聊聊" → 不算，没有具体的事

判到就写，判不到就回 NONE —— 两种都是正确答案，**别为了有内容而勉强凑一条**。
一行一条，最多 {limit} 条，格式 `OPEN <不超过 20 字的一件事>`；一条都没有就只回 `NONE`。
只写事本身，不要解释、不要加引号、不要把原话整句抄下来。

最近对话（{n} 行）：
{turns}
"""


def parse_open_threads(reply: Any, *, limit: int = MAX_OPEN_THREADS) -> list[str]:
    """把模型的回话收成话题清单。认不出的行**整行丢掉**，不猜。

    `NONE` / 空回复 / 全是垃圾 → `[]`。这一条是"宁可漏报"在代码里的落点：
    解析器宽松一寸，界面上就多一寸假话。
    """
    text = text_of(reply)
    out: list[str] = []
    for line in text.splitlines():
        match = _OPEN.match(line.strip())
        if not match:
            continue
        topic = match.group(1).strip().strip("“”\"'。；;，, ")
        if not topic or topic.upper() == "NONE":
            continue
        out.append(topic[:_MAX_TOPIC_CHARS])
        if len(out) >= limit:
            break
    return out


def find_open_threads(turns: str, model: Any, *, limit: int = MAX_OPEN_THREADS) -> list[str]:
    """一次模型调用扫这段最近对话，返回真正没收尾的那几件事（没有就 `[]`）。

    `turns` 是宿主给的"你们最近聊过什么"那段文本（措辞与截断的唯一出处在
    `reachout.format_thread_lines`，这里不重抄一份）。空文本 = 没东西可判 = **不发这次调用**。

    调用本身失败也回 `[]` 且不往上抛：这一源是锦上添花，它坏了的正确表现是
    "她这次没提这个"，不是"这一轮开口失败"。
    """
    turns = (turns or "").strip()
    if not turns or model is None:
        return []
    prompt = _PROMPT.format(limit=limit, n=len(turns.splitlines()), turns=turns)
    try:
        reply = model.invoke(prompt)
    except Exception:  # noqa: BLE001 - 由头缺一个不是故障，见上
        return []
    return parse_open_threads(reply, limit=limit)
