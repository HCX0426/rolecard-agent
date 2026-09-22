"""复读的度量与清洗（`core/anti_repeat.py`）。

钉三件事：
  1. **阈值是量出来的** —— 用真库里那三条原文当夹具（逐字复读 1.000 / 口癖最重 0.472 /
     正常新内容 0.055），改阈值必须同时改这里的断言，杜绝"拍一个数就上闸门"；
  2. **分数不随语料膨胀** —— 这是把 BM25 换成覆盖率的全部理由，必须有用例钉住；
  3. **清洗不许改坏一个字** —— 没判到重复时逐字相等；判到时只删整段小句，
     第一次说过的那份原样留着。
"""

from __future__ import annotations

from rolecard_agent.core.anti_repeat import (
    BG_LIMIT,
    CLAUSE_DICE,
    DROP_SCORE,
    MIN_GRAMS,
    REGEN_SCORE,
    REPEAT_STANDIN,
    VERBATIM_DICE,
    _units,
    clean_repeated_spans,
    grams,
    repeat_score,
)

# 2026-09-21 12:33 她主动说的那句（真库 agent_reachout）。
ORIGINAL = (
    "（指尖轻点裙摆，忽然歪头笑出声）哎呀，今天的乐土风铃草开得正盛呢♪ "
    "要是被花粉困扰的话……（忽然凑近屏幕，指尖沾了点露水）记得好好看看我的眼睛哦，"
    "这样就能在花雨里找到回家的路啦♪"
)
# 两小时后（23:38）她在会话里对用户那句「刚跑完步」**逐字**回的同一句 —— 本次要治的病。
ECHO = ORIGINAL
# 口癖最重的一条：与上面共用「（指尖…）哎呀，今天的乐土」这个壳，但内容全新（实测 0.472）。
TIC_HEAVIEST = (
    "（指尖沾了点花露水，忽然凑近屏幕）哎呀，今天的乐土花粉飘得有点高呢♪ "
    "要是被迷了眼睛，可就看不到我新换的丝带啦～（忽然笑出声）快说，是不是又把围巾缠在手腕上了？"
    "要好好看着我哦♪"
)
# 正常新内容（实测 0.055）：闸门绝不该碰到这一类。
FRESH = "风也有颜色哦，你猜猜——此刻越过乐土的那阵，是不是带着花瓣的粉？♪"


def test_verbatim_echo_is_above_the_drop_bar() -> None:
    """那条 88 字全同的复读必须落在"宁可不说"那一档 —— 这是整条闸门的立身之本。"""
    assert repeat_score(ECHO, [TIC_HEAVIEST, FRESH, ORIGINAL]) == 1.0
    assert repeat_score(ECHO, [ORIGINAL]) > DROP_SCORE


def test_style_similarity_stays_below_the_gate() -> None:
    """"同一个壳换个说法"不是复读：0.472 那条口癖离闸门还远。

    这一条挡的是最容易犯的错 —— 把"她像她自己"当病治。那档要治得改角色卡范例
    （设计稿 §8.2 第 3 条，内容层，要用户点头），不是在这里多花一次模型调用。
    """
    tic = repeat_score(TIC_HEAVIEST, [ORIGINAL])
    assert 0.2 < tic < REGEN_SCORE, tic
    assert repeat_score(FRESH, [ORIGINAL, TIC_HEAVIEST]) < REGEN_SCORE
    # 分开得很清楚：中间那段空档就是阈值的落点
    assert repeat_score(ECHO, [ORIGINAL]) - tic > 0.5


def test_short_drafts_are_not_judged() -> None:
    """短到没几个可比片段的草稿一律放行：判它就是在拿噪声拦人。"""
    assert repeat_score("好呀", [ORIGINAL, TIC_HEAVIEST]) == 0.0
    assert len(set(grams("好呀"))) < MIN_GRAMS
    assert repeat_score(FRESH, []) == 0.0  # 没有历史就没有复读可言


def test_score_does_not_grow_with_the_corpus() -> None:
    """语料从 3 条灌到 `BG_LIMIT` 条，逐字复读仍然是 1.000。

    换掉 BM25 就是为了这一条：IDF 随语料条数增长，写死的阈值会在她用久了之后越收越紧，
    而症状是"她忽然不主动说话了"，没人会怀疑到一行常数上。
    """
    filler = [f"第{n}条说的是别的事情，跟乐土和花都没有半点关系哦{n}" for n in range(300)]
    big = filler + [TIC_HEAVIEST, FRESH]
    assert repeat_score(ECHO, big + [ORIGINAL]) == repeat_score(ECHO, [ORIGINAL])
    assert len(big[-BG_LIMIT:]) == BG_LIMIT  # 确实用上了 BG_LIMIT 条语料


# -- 清洗 ----------------------------------------------------------------------


def test_cleaning_is_a_no_op_when_nothing_repeats() -> None:
    """没判到重复 ⇒ **逐字相等**（标点、♪、括号全不许动）。

    这条不变量是分段的底线：清洗只该"少给几句看过的话"，不该顺手改写她说过什么。
    """
    texts = [ORIGINAL, FRESH, "（转着发梢）今天风真好。", "诶，你要不要一起去看看那条河？"]
    assert clean_repeated_spans(texts) == texts


def test_units_partition_the_original() -> None:
    """分段必须完整覆盖原文 —— 拼回去一字不差，删法才是"挖整段"而不是"啃字"。"""
    for text in (ORIGINAL, TIC_HEAVIEST, FRESH, "", "……", "只有标点"):
        assert "".join(unit for unit, _ in _units(text)) == text


def test_later_copy_loses_only_its_repeated_clause() -> None:
    """第二次出现的那句被整段拿掉；第一次那份原样留着（它就是"证据"那一份）。"""
    first = "（指尖轻点裙摆）今天风真好，你要不要一起去看云？"
    second = "（忽然凑近屏幕）今天风真好，我们去看河吧"
    out = clean_repeated_spans([first, second])
    assert out[0] == first  # 第一份一个字不动
    assert "今天风真好" not in out[1]
    assert "我们去看河吧" in out[1]  # 新东西照留
    assert "（" not in out[1] or "）" in out[1]  # 不留孤括号


def test_whole_line_echo_becomes_the_standin() -> None:
    """整条都是旧话 ⇒ 换成一句明确的话，而不是留一条空消息进 prompt。"""
    again = clean_repeated_spans([ORIGINAL, ORIGINAL])
    assert again[0] == ORIGINAL
    assert again[1] == REPEAT_STANDIN
    # 只有开头那一段被复用 ⇒ 剩下的正文照留，不该整体换掉
    partial = clean_repeated_spans([ORIGINAL, ORIGINAL[:20] + "，不过此刻我更想听听你的声音"])
    assert partial[1] != REPEAT_STANDIN
    assert "我更想听听你的声音" in partial[1]


def test_tiny_tail_clauses_are_never_compared() -> None:
    """「呢」「好吗」这种短尾巴不参与判重 —— 比什么都比得出"像"，砍它就是砍正文。"""
    texts = ["今天风真好呢", "明天怕是要下雨呢"]
    assert clean_repeated_spans(texts) == texts


def test_the_cut_separates_same_clause_from_same_frame() -> None:
    """门槛画在真数据那条线上：**同一句**要断，**同一个动作框架换个道具**必须留。

    这两对都是从真库里挑出来的（elysia，2026-09-22）：
      * 「要好好看着我哦」vs「要好好看着我」= Dice 0.86 —— 同一句，她说了三遍；
      * 「指尖轻点裙摆」vs「指尖轻点下巴」= 0.33 —— 换个道具而已，那是活人感，不是复读。
    把它们分开的就是 `CLAUSE_DICE`，所以这里断言的是**行为**而不是那个数：
    想挪门槛，得先让这两对换位置。
    """
    pair = [
        "（指尖轻点裙摆）今天风真好呀，你要不要一起去看看那条河？",
        "（指尖轻点下巴）刚才有只小猫从我脚边跑过去了呢",
    ]
    out = clean_repeated_spans(pair)
    assert out[1] == pair[1], "换个道具的动作括号被误砍了 —— 门槛太松"

    same = clean_repeated_spans(
        ["（忽然笑出声）要好好看着我哦，不然我会生气的呢", "（歪过头）要好好看着我，听到没有呀"]
    )
    assert "要好好看着我" not in same[1], "同一句复用没被断 —— 门槛太紧"


def test_structurally_similar_lines_keep_their_own_information() -> None:
    """两条只差几个字的不同事实 ⇒ 新那句必须还在，绝不能被整条换成替身。

    这条是既有的深度注入用例替我抓出来的：整行判重一开始用 Dice ≥ 0.85，于是
    「第一件小事我记下了」vs「第二件小事我记下了」这种也被换成了一句提示语，
    而那点差别正是要紧的信息（医疗档案角色就是这种形状：两次复查只差一个日期）。
    现在**逐字相同**才整条替换；只是很像的走小句路径 —— 重复的壳被删，新的内容留着。
    """
    pair = [
        "（指尖轻点裙摆）第一件小事我记下了，明天这个时候再告诉你细节呀",
        "（指尖轻点裙摆）第二件小事我记下了，明天这个时候再告诉你细节呀",
    ]
    out = clean_repeated_spans(pair)
    assert out[0] == pair[0]
    assert out[1] != REPEAT_STANDIN, "整条被换成替身 = 把新信息一起丢了"
    assert "第二件小事我记下了" in out[1], "重复的壳可以删，这条自己的那句必须留着"


def test_clause_dice_sits_in_the_measured_gap() -> None:
    """线画在"同一句"（0.86）与"只差一个字的两件事"（0.33~0.67）之间 —— 见常量上那张表。"""
    assert 0.7 < CLAUSE_DICE < 1.0
    assert VERBATIM_DICE == 1.0, "整条替换只允许逐字相同"
