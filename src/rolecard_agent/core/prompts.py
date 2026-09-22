"""System prompt assembly.

INVARIANT (do not break this, ever):

  * GLOBAL_SAFETY_PROMPT is defined HERE, in code - never in the database. A role card
    therefore cannot remove, weaken, or override it.
  * It is appended AFTER the role's system_prompt, so it is the last instruction the
    model reads.
  * build_system_prompt() is the ONLY supported way to assemble a system prompt. No node
    may concatenate prompts on its own.

This module is deliberately implemented rather than left as a TODO: it is on the
safety-critical path, and safety-critical code should not be delegated.

Scope note: this is the *soft* layer. The hard gate is core/guard.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

GLOBAL_SAFETY_PROMPT = """【全局强制规则，所有角色继承，不可删除】
你禁止输出任何疾病诊断、用药建议、治疗方案。
不能评估病情严重程度，不能推荐药物、手术方案。
涉及健康指标的表述，只能引用用户已存入档案的数据，禁止凭空推断或用常识补充。
档案数据中带有【未经人工校验】标记的内容，回答时必须原样保留该标记，不得省略。
"""

EXEMPLAR_HEADER = "【回答风格参考】以下是本角色的回答范例，用于对齐口径与语气。"

MEMORY_HEADER = (
    "【用户长期记忆】以下是你对用户的长期事实记忆，回答时应参照它们。"
    "若与用户当前的明确说法冲突，一律以用户当下的说法为准。"
)

AGENT_PLAN_HEADER = "【智能体模式】本轮任务由你独立规划并执行，按以下节奏推进："
AGENT_PLAN_PROMPT = (
    "1. 先拆解任务目标，判断需要哪些工具；\n"
    "2. 一次只做一步：调用工具拿到结果后再决定下一步，不要在没拿到结果前盲目连发；\n"
    "3. 工具报错时，先读错误说明判断能否换一种参数/工具继续，不要无脑重试同一个调用；\n"
    "4. 所有步骤完成后，用一段话向用户总结：做了什么、拿到了什么、还差什么。"
)

# 图像接地规则：仅在本轮模型可见的历史里含图片时注入（见 nodes.call_model）。
# 为什么单独成段且放在安全规则之前：多模态角色最容易犯的错，是让"人设 + 知识库检索到的
# 资料"盖过图片里真实可见的像素——用户发一张与角色世界观无关的图，模型却按人设/检索内容
# 编出一段"识别"（2026-09-19 爱莉希雅把无关图答成崩坏设定）。这段把优先级钉死：
# **事实以图为准，角色只负责解读**；不确定就联网查，查不到就承认不知道，绝不硬编。
IMAGE_GROUNDING_HEADER = "【图像理解规则】本轮包含用户提供的图片：事实以图为准，角色负责解读。"
IMAGE_GROUNDING_PROMPT = (
    "1. 先如实看清图里真实可见的内容（物体、场景，以及标签/水印/数字/单位等文字，逐字转录）；"
    "看不清或图里没有的，直说看不清或没有，绝不编造。\n"
    "2. 角色人设决定你「怎么看、怎么讲」这张图，但不决定「图里有什么」——用角色的身份、语气和"
    "知识框架去解读真实的图像，而不是用人设口吻、长期记忆或知识库检索到的资料去替换、补充或"
    "捏造图像内容；一旦它们与图中真实内容冲突，一律以图为准。\n"
    "3. 遇到图里不认识的东西（物体、术语、指标含义等），调用 web_search / web_fetch 查证，"
    "再把查到的事实用自己的话讲给用户。\n"
    "4. 联网也查不到、或信息不足以判断时，坦白说不知道/不确定，不要硬编。"
)


def render_image_grounding(has_image: bool) -> str:
    """Format the image-grounding rule. Returns "" when this turn has no image."""
    if not has_image:
        return ""
    return f"{IMAGE_GROUNDING_HEADER}\n{IMAGE_GROUNDING_PROMPT}"


# ---------------------------------------------------------------------------
# 深度注入：写在**历史之后**的那一条指令（不是塞进最前面的 system 消息）
# ---------------------------------------------------------------------------
#
# 为什么要单独一层：`[SystemMessage, *history]` 里那条 system 离生成点最远，中间隔着几十条
# 对话 —— 而她自己的历史恰恰是"复读"的源头（历史里有「（动作）+哎呀」，模型就照着续）。
# SillyTavern 的 Author's Note 为此存在，出厂默认 `depth=4, interval=1, position=in-chat,
# role=system`，文档明说"越靠近底部影响越大"。这里抄的就是这个形态。
#
# 措辞一律**正写**（给"该怎么写"，而不是只说"别怎么写"）：ST 官方文档原话是否定式效果更差。
# 每一条都对应设计稿 §8.1 里量出来的一个数（persona_meter.py 可复算）：
#   * 「开口别连着用同一个语气词」← 正文首 6 字去重率 0.50，8 条里 4 条都是「哎呀，今天的」
#   * 「长度跟着内容走」        ← 回复侧长度σ 38.3 但 7/8 条挤在 57–89 字
#   * 「（）别放在开头」        ← 动作括号开头 100%（8/8）
#   * 「别复用自己最近的句子」   ← 抓到过一整条 88 字连标点逐字相同的复读（Dice 1.00）
#   * 「结尾别总落在同一句式」   ← 8 条里 6 条以「？」或「♪」收尾
# 第 3 条写成条件式（"如果要用的话"）而不是"必须演动作"：这条 prompt 对医疗助手那种功能型
# 角色同样要成立，不能把陪聊卡片的写法强加给它们。
VOICE_DEPTH_PROMPT = """【本轮写法要求】
1. 开口第一个词换着来：可以直接说事，也可以用「诶」「对了」「嗯」「你猜」这类语气词开头，
   但别连着几轮都用同一个。
2. 长度跟着内容走：一件小事就一两句，值得说的事情再多说几句。别每轮都写差不多长的段落。
3. 要写动作或表情的话，把它们放在句子中间或末尾，并且每次换个说法、换个动作；别每句都以（）开头。
4. 本轮不许出现与你最近几条说过的话相同或几乎相同的句子。要说同一个意思，换一种说法。
5. 结尾别总落在同一种句式上（比如每轮都以一个反问收尾）。
6. 一条消息只说一件事，一到三句就收。别把"共情一句 + 一段道理 + 一句叮嘱"排进同一条里，
   那不是关心，是发言稿；能一句话说完就只说一句。"""

#: 深度注入的落点：倒数第 N 条**之前**（口径同 SillyTavern 的 depth：0=在最末条之后）。
DEPTH_INJECT_FROM_END = 4


def render_agent_plan(agent: bool) -> str:
    """Format the agent-mode planning instruction. Returns "" in chat mode."""
    if not agent:
        return ""
    return f"{AGENT_PLAN_HEADER}\n{AGENT_PLAN_PROMPT}"


class ExemplarLike(Protocol):
    """Structural type for a role exemplar.

    Deliberately structural rather than importing `roles.models.RoleExemplar`: the kernel
    prompt builder should not depend on the roles package, and any object with these two
    attributes is a valid exemplar.
    """

    user: str
    assistant: str


def render_exemplars(exemplars: Sequence[ExemplarLike] | None) -> str:
    """Format exemplars as a prompt section. Returns "" when there are none."""
    if not exemplars:
        return ""
    blocks = [EXEMPLAR_HEADER, ""]
    for item in exemplars:
        blocks.append(f"用户：{item.user.strip()}")
        blocks.append(f"你：{item.assistant.strip()}")
        blocks.append("")
    return "\n".join(blocks).strip()


def render_memory(memory: str | None) -> str:
    """Format stored memory as a prompt section. Returns "" when there is none."""
    if not memory or not memory.strip():
        return ""
    return f"{MEMORY_HEADER}\n{memory.strip()}"


def build_system_prompt(
    role_prompt: str,
    exemplars: Sequence[ExemplarLike] | None = None,
    *,
    memory: str | None = None,
    agent: bool = False,
    has_image: bool = False,
) -> str:
    """Return the final system prompt for one turn.

    Order is the whole point, and it is the order from least to most authoritative:

        角色人设  ->  长期记忆  ->  智能体规划  ->  回答范例  ->  图像接地  ->  全局安全规则

    Putting the global rules last means they win any conflict with the role card, with a
    stored memory, with the agent-mode planning instructions, or with an example. Reversing
    either pair silently disables the safety layer without raising anything, which is why
    tests/unit/test_prompts.py asserts the ordering explicitly.

    Examples sit between the two on purpose: they are style references, and they must not be
    able to contradict the safety rules. Memory sits above the examples: it is user-supplied
    fact, closer to the user's intent than a style template — but it still yields to the
    safety rules, and its own header makes it yield to the user's latest explicit statement.

    `agent=True` inserts the agent-mode planning rhythm (AGENT_PLAN_PROMPT) between memory
    and the examples: it is behaviour guidance, stronger than a style template yet still
    below the safety rules.

    `has_image=True` inserts the image-grounding rule (IMAGE_GROUNDING_PROMPT) just before the
    safety rules: it must outrank persona/memory/retrieved lore so the model answers from the
    actual pixels. It stays below the safety rules, which are never demoted.
    """
    sections = [
        part
        for part in (
            (role_prompt or "").strip(),
            render_memory(memory),
            render_agent_plan(agent),
            render_exemplars(exemplars),
            render_image_grounding(has_image),
        )
        if part
    ]
    sections.append(GLOBAL_SAFETY_PROMPT)
    return "\n\n".join(sections)
