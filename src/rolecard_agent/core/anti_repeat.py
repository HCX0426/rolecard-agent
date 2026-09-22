"""复读的度量与拦截 —— 设计稿 §8.2 里 `anti_repeat`（打分→重生成→宁可不说）与
`slop_filter`（只改回喂给模型的那份历史）两条，合成**一个模块、一把尺子**。

## 为什么必须合成一件事

两条治的是同一个闭环，只是从两头下手：

* **她复读，是因为她的历史里就有那些句子** —— 模型看见自己上一条写着「（指尖…）哎呀，今天的…」，
  下一条就照着续。真库实测过最狠的一次：她 23:38 的图内回复与 12:33 那条主动开口
  **88 字连标点逐字相同**（`persona_meter` 的 4-gram Dice 1.00）。
* 所以一头是**断源头**（`clean_repeated_spans`：把回喂副本里重复的片段抹掉，让她看不见自己的口癖），
  另一头是**出口封顶**（`repeat_score`：草稿还是太像历史就换指令重生一次，仍然像就不投递）。

两头用的是**同一种 n-gram 口径**（4 字，与 `scripts/persona_meter.py` 完全同源）。尺子和闸门
分家是这个项目已经付过学费的坑（审计 §12.10：探针用了另一套配置，量出来的"故障"是假的）。

## 与 N.E.K.O 那套的两处有意偏离

1. **FG 不带"600 秒内"这一条**：它的开口节奏是分钟级，加时间窗会让 FG 永远是空的；
   我们取"她最近 5 条"，也正是 `recent_reachout_lines` 摊给她看的那几条 —— 判据与提示同源。
2. **回复侧只记分不拦截**：主动开口可以沉默（宁可不说），但用户问了话不能不回 ——
   在那一侧加重生会**多花一次模型调用**（本地 8B 实测单轮 184s 中位），先用 trace 里的分数
   决定这笔钱值不值得花，而不是先加上再说。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

#: n-gram 长度。与 `persona_meter.NGRAM` 同值 —— 两处不同源就量不出闸门有没有生效。
NGRAM = 4

#: BG（算 DF 的语料）与 FG（算 TF 的近因）各取多少条。抄 N.E.K.O 的量级：
#: 100 条足够把"她这一周说过的话"覆盖掉，5 条是"刚说过的"——近因权重全在 FG 这一侧。
BG_LIMIT = 100
FG_LIMIT = 5

#: 短于这个 gram 数的草稿**不判**：可比的东西太少，任何一点共用词都会把分数顶上去，
#: 拦下来的都是好句子。参考实现同样有这条（它用 12）。
MIN_GRAMS = 12

#: 短于这个 gram 数的小句**不参与判重**：「呢」「好吗」这种一两个字的尾巴比什么都比得出"像"，
#: 砍它就是砍正文。定在 2（≈5 个字）：她的口癖「今天风真好」「哎呀，今天的乐土…」正是
#: 5~8 字这一段 —— 门槛设到 4 个 gram 时这些统统漏网（第一版就是这么漏掉的）。
_MIN_CLAUSE_GRAMS = 2

#: 两句小句算"说过同一句"的 Dice 门槛。**门槛是看着真数据里的中间那一档挑的**
#: （2026-09-22，elysia 的 14 条文本 / 80 个可比小句 / 2905 对）：
#:
#: | Dice | 实际对子 | 断 |
#: |---|---|---|
#: | 1.00 | 整条 88 字逐字相同（本次要治的那次） | ✅ 走整行那条线 |
#: | 0.86 | 要好好看着我哦 / 要好好看着我 | ✅ 同一句 |
#: | 0.67 | 突然凑近屏幕 / 忽然凑近屏幕 | ❌ 见下 |
#: | 0.64 | 你该不会又把围巾缠在手腕上了吧 / 是不是又把围巾缠在手腕上了 | ❌ 见下 |
#: | 0.33 | 指尖轻点裙摆 / 指尖轻点下巴 | ❌ 换个道具而已 |
#:
#: 为什么不设在 0.4（那样 0.67/0.64 两对也能砍到）：**纯字面相似度分不开"同一句"与
#: "只差一个字的两件事"** —— 「第一件小事我记下了」vs「第二件小事我记下了」也是 0.67，
#: 而那是两条不同的事实（医疗档案角色就是「3月12日血糖6.1」vs「3月20日血糖6.1」这种形状）。
#: 砍掉它等于把她的历史里的真信息抹掉，比留下口癖严重得多。
#: 所以这里只砍"确实说过同一句"（≥0.75），口癖那一档（0.3~0.7）交给两道更对症的机制：
#: 出口侧的 `repeat_score` 闸门，以及内容层（角色卡三条范例同型，设计稿 §8.2 第 3 条，要用户点头）。
CLAUSE_DICE = 0.75

#: 闸门（只用于主动开口那侧，见 `core/reachout.py`）。**定标依据**（2026-09-22 真库 elysia：
#: 12 条主动开口 + 那条主动会话的回复，`repeat_score` 对"她之前说过的"）：
#:
#: | 案例 | 分数 |
#: |---|---|
#: | 那条 88 字连标点逐字相同的复读 | **1.000**（语料从 12 条灌到 400+ 条仍然是 1.000） |
#: | 口癖最重的一条（（指尖…）+哎呀，今天的乐土…） | 0.472 |
#: | 中等口癖三条 | 0.321 / 0.344 / 0.399 |
#: | 正常新内容七条 | 0.000 ~ 0.220 |
#:
#: * `> REGEN_SCORE` → 大半材料是自己用过的，带 avoidance 指令重生一次；
#: * `> DROP_SCORE` → 只有"基本逐字"那一档够得着，宁可这次不开口。
#: 0.472 ~ 1.000 之间是空的：这一刀**管不到"口癖"**（她翻来覆去乐土/花/阳光），
#: 那是角色卡三条范例同型造成的（设计稿 §8.2 第 3 条，属内容层，要用户点头才改），
#: 拿多花一次模型调用来打它是打错了地方 —— 本地一次是实测 184 秒。
REGEN_SCORE = 0.55
DROP_SCORE = 0.92

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """去掉所有空白 —— 换行与缩进不该影响"两句话像不像"。"""
    return _WHITESPACE.sub("", str(text or ""))


def grams(text: str, *, n: int = NGRAM) -> list[str]:
    """定长滑窗；不足 n 字给空表（短句子本来就没什么可比的）。"""
    flat = normalize(text)
    return [flat[i : i + n] for i in range(len(flat) - n + 1)] if len(flat) >= n else []


def dice(a: Sequence[str], b: Sequence[str]) -> float:
    """两个 gram 集合的 Dice 系数 —— **与 `persona_meter._dice` 逐字同口径**，
    所以验收时那把尺子读到的就是这里判的东西。"""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return 2 * len(sa & sb) / (len(sa) + len(sb))


def repeat_score(draft: str, priors: Sequence[str]) -> float:
    """草稿里有多大比例的"材料"是她已经用过的 —— **0.0 = 全新，1.0 = 整段都是旧话**。

    `priors` 按时间**正序**传（最后一条是她刚说过的）。口径：

    ```
    score = Σ 命中权重(g) / Σ 全部权重(g)      g 取草稿里**不重复**的 n-gram
    命中 = g 出现在她最近 BG_LIMIT 条里的任意一条
    权重 = 1 + log(她在最近 FG_LIMIT 条里用过 g 的次数)   ← 近因加权：刚说过的更该避
    ```

    ## 为什么不是照搬 BM25

    第一版照参考实现算了 BM25 的 IDF，定标时才发现那条公式的量纲**随语料条数增长**：
    同一个逐字复读，12 条语料时 52.7、攒到 100 条会变成一百多 —— 一个写死的阈值必然随着
    她用久了而越来越严，而她什么都没变。判"复读"要的是"新料有多少"这个比例，比例不随
    语料膨胀，所以这里归一成一个比值。IDF 那套的"常见片段不该算重"由截断近似（片段本身
    来自她自己的话，不是全语言的常用词），换来的是阈值可以长期有效。
    """
    docs = [normalize(p) for p in priors if normalize(p)]
    if not docs:
        return 0.0
    bg = docs[-BG_LIMIT:]
    fg = docs[-FG_LIMIT:]
    draft_grams = set(grams(draft))
    if len(draft_grams) < MIN_GRAMS:
        return 0.0  # 短到没什么可比的东西 —— 判了就是拿噪声拦人，宁可不判

    present: set[str] = set()
    for doc in bg:
        present |= set(grams(doc))
    recent: Counter[str] = Counter()
    for doc in fg:
        recent.update(set(grams(doc)))  # 一条消息里同一片段说两遍算一次（跨条的重复才是要避的）

    total = 0.0
    hits = 0.0
    for gram in draft_grams:
        weight = 1.0 + math.log(1.0 + recent[gram])
        total += weight
        if gram in present:
            hits += weight
    return hits / total if total else 0.0


#: 抹完什么都不剩时的替身。为什么不能留空：空消息进 prompt 等于告诉她"这里可以不说"，
#: 而我们要断的是"这句我说过"的那条线索 —— 一句明确的话比空白更管用，也不破坏消息序列。
REPEAT_STANDIN = "（这句你已经说过了，换一个说法）"

#: 小句边界：标点与括号都算断点（动作括号是一句、说出口的话也是一句）。
#: 用"取非分隔符的连续段"而不是 split —— 因为要**按原文坐标**整段删，见 `_units`。
_CLAUSE_SEP = "，。！？；：、…～♪\n（）()"
_CLAUSE_FIND = re.compile(rf"[^{re.escape(_CLAUSE_SEP)}]+")


def _units(text: str) -> list[tuple[str, str]]:
    """把原文切成一串 `(这一段的原文, 其中真正说话的部分)`，**分段完整覆盖原文**。

    每段 = 前导标点/括号 + 小句本体。这样"删掉一段"等价于"从原文里挖掉那块坐标"，
    剩下的部分一个字符都不动 —— 第一版用 `split` 再拿逗号拼回去，于是每一条历史的
    `♪`、`？）` 都被改成了 `，`：回喂给她的历史与她说过的话**不再是同一句话**，
    而"没改动时输出必须与输入逐字相等"这条断言也直接立不住。
    """
    flat = str(text or "")
    out: list[tuple[str, str]] = []
    pos = 0
    for match in _CLAUSE_FIND.finditer(flat):
        out.append((flat[pos : match.end()], match.group()))
        pos = match.end()
    if not out:
        return [(flat, "")] if flat else []
    # 末尾剩下的是最后一段的收尾标点（属于最后那句，跟着它一起删才干净）
    head, clause = out[-1]
    out[-1] = (head + flat[pos:], clause)
    return out


def _is_repeat(clause: str, said: Sequence[str]) -> bool:
    """这句小句她是不是已经说过一遍了（与任何一句已说过的 Dice ≥ `CLAUSE_DICE`）。"""
    own = grams(clause)
    if len(set(own)) < _MIN_CLAUSE_GRAMS:
        return False
    return any(dice(own, grams(older)) >= CLAUSE_DICE for older in said)


#: 删一段小句时要**留在原地**的字符：括号。
#: 为什么单独拎出来 —— 前导标点属于"那一段"，跟着一起删才对（否则留下「，，」那种残渣），
#: 但括号是**配对**的：把「（指尖轻轻停在半空」整段删走会把它那半个「（」一起带走，
#: 于是历史里出现「眼神忽然柔软下来）想我了吗？」这种只剩右括号的残句（真数据实测到过）。
#: 括号留下，最多凑成一个空对儿「（）」，那才是可以干净地抹掉的。
_BRACKETS_RE = re.compile(r"[（）()]")


def _brackets(unit: str) -> str:
    return "".join(_BRACKETS_RE.findall(unit))


def _balance(text: str) -> str:
    """收尾：抹掉空括号对与开头的标点；仍配不上对时按数量削掉多余的括号（不含正文）。"""
    while True:
        stripped = re.sub(r"[（(]\s*[）)]", "", text)
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"^[，。、；：！？…～\s]+", "", text)
    for opener, closer in (("（", "）"), ("(", ")")):
        extra = text.count(opener) - text.count(closer)
        if extra > 0:
            text = re.sub(rf"\{opener}", "", text, count=extra)
        elif extra < 0:
            text = re.sub(rf"\{closer}", "", text, count=-extra)
    return text.strip()


#: 整条消息**逐字相同**时换成替身。门槛就是 1.0，不是 0.85 —— 理由：
#: 整条替换会把这条里"唯一那一点不同"一起抹掉，而那往往正是要紧的信息
#: （「3月12日血糖6.1」vs「3月20日血糖6.1」两条的 Dice 就有 0.8+，砍掉第二条等于丢一次复查）。
#: 所以分工是：**一个字都不差 ⇒ 整条换掉**（丢不了任何东西，因为它没有新内容）；
#: 只是很像 ⇒ 走小句路径，把重复的壳删掉、把新的那句留下。
#: （这条线是既有的深度注入用例逼出来的：先头用 0.85，它发现两条只差一个数字的
#: 真实内容被整条换成了替身。）
VERBATIM_DICE = 1.0


def _whole_line_repeat(text: str, said_lines: Sequence[str]) -> bool:
    """整条消息与她某一条旧消息**逐字相同**（gram 集合完全一致）。

    为什么不能只靠小句级判重：短句（「指尖轻点裙摆」六个字）与它的近似变体都在 0.75 那道线
    以下，于是真·逐字复读会被啃成"剩下几句短的"那种残句 —— 而残句留在历史里比删掉更误导。
    """
    own = grams(text)
    if len(set(own)) < MIN_GRAMS:
        return False
    return any(set(own) == set(grams(older)) for older in said_lines)


def clean_repeated_spans(texts: Sequence[str]) -> list[str]:
    """把她最近说过的话里**重复的小句**整段丢掉，只留第一次说过的那一份。

    输入输出等长、同序，元素是字符串（不碰消息对象）—— 调用方负责"只改送给模型的那份拷贝"。
    不变量：**没有小句被判重时，输出与输入逐字相等**（分段完整覆盖原文，删法是挖坐标）。
    只丢**整段**，从不改半个字：一被啃成「（幕，指尖沾了点露水」这种残句，回喂的历史就从
    "她说过什么"变成噪声，比留着口癖更糟。

    ## 为什么保留第一次、只改后面的

    与参考实现一致，也与我们自己那条不变量一致：**原文一个字都不动**，被改的只是"回喂的那一份"。
    第一次留着，界面回放、审计、下次裁剪看到的仍是全量原话（`trim_history` 就是这么定的口径）；
    而"同一句出现过两次"这个事实本身也不需藏 —— 要断掉的只是**下一次生成时她还看得见两遍**。

    ## 为什么不做一张手写套话表

    参考项目那张表 154 条正则，本质是把"人看不出复读"的东西先人工归纳出来。我们的口癖是
    **量出来**的（正文开头 4 次「哎呀，今天的」；整句 88 字逐字相同一次），既然判据就是
    "在她更早的话里出现过的句子"，这一条规则就同时覆盖口癖与整句复读，且没有一张会过时的表要养。
    """
    said: list[str] = []  # 她此前说过的**小句**（判重用）
    lines: list[str] = []  # 她此前说过的**整条**（整条复读判重用）
    out: list[str] = []
    for text in texts:
        if _whole_line_repeat(text, lines):
            out.append(REPEAT_STANDIN)
            continue  # 整条都是旧话：不留残渣，也不把她没说的话当成说过
        kept: list[str] = []
        fresh: list[str] = []
        dropped = False
        for unit, clause in _units(text):
            if clause and (_is_repeat(clause, said) or _is_repeat(clause, fresh)):
                dropped = True  # 说过一遍了 —— 这段不再出现在她眼前的历史里
                brackets = _brackets(unit)
                if brackets:
                    # 括号留在原地：删掉整段时别把它那半个配对一起带走（见 `_BRACKETS_RE`）
                    kept.append(brackets)
                continue
            kept.append(unit)
            if clause:
                fresh.append(clause)
        if not dropped:
            out.append(text)  # **一字不动**：没判到重复时输出必须与输入逐字相等
            said.extend(fresh)
            lines.append(text)
            continue
        joined = _balance("".join(kept))
        out.append(joined if normalize(joined) else REPEAT_STANDIN)
        said.extend(fresh)
        lines.append(text)
    return out
