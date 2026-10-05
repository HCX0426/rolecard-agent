"""尺子本身的可测部分（scripts/persona_meter.py 的「语义空转」那一族）。

为什么要给脚本写测试：这把尺子的存在理由就是"别用感觉说话"，而它自己新加的那一列如果
只在真库上跑一次看看数，就等于**没钉住**——下一个人改坏了，没人知道那列已经不再量东西了。

钉的核心那一件事：设计稿 §8.1 记的症状是"句式指标全绿，读原话却是乐土/星星/月光来回八遍"。
所以这里要的不是"分数好看"，而是**旧列漏掉、新列抓到**。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "persona_meter.py"


def _meter():
    spec = importlib.util.spec_from_file_location("persona_meter_test", str(SCRIPT))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 同一堆意象、逐字不重复的八句（照着 §8.1 那批本地读物的形状造）。
DRIFTING = [
    {"text": "（仰头看星星）今晚的月光落在乐土的湖面上，像一匹铺开的银绸子。"},
    {"text": "（伸手接住）那些棉花糖一样的云飘过乐土的时候，萤火虫也提着灯来了。"},
    {"text": "（转了个圈）乐土的星星掉进湖底了，月光替它们盖了床薄被子。"},
    {"text": "（蹲下身）你听，萤火虫在讲乐土的故事，讲到一半就变成棉花糖了。"},
    {"text": "（指着湖面）月光底下那盏是琉璃盏吧，乐土的星星都爱往里面跳。"},
    {"text": "（打了个哈欠）我梦见乐土的棉花糖堆成山，山顶上还挂着半轮月亮。"},
    {"text": "（拍拍手）萤火虫围着琉璃盏转圈的时候，乐土的月光最像过年。"},
    {"text": "（歪头）星星要是掉进棉花糖里，乐土的湖面会不会甜得发慌。"},
]
VARIED = [
    {"text": "（翻开本子）你上次说周三要交周报，我把日子记下来了，需要我提醒你不？"},
    {"text": "（倒了杯水）楼下那家诊所九点开门，你要空腹去抽血的话最好八点到。"},
    {"text": "（敲敲键盘）这份报告里甘油三酯那项比去年高了零点八，饮食上要改点什么吗？"},
    {"text": "（合上电脑）明天降温六度，你骑电动车的话记得把手套塞进包里。"},
]


def test_drifting_imagery_is_caught_by_the_new_columns_only() -> None:
    """旧列全绿、新列报红：这正是补这一族指标的全部理由。

    两列的**绝对值都不设门槛**（这把尺子不拦任何东西），断言的是相对关系：
    同一段语料里，"换着说同一堆意象"与"四件不相干的事"必须拉开一个量级。
    实测：词面复用 0.262 vs 0.014（≈19 倍），型例比 0.777 vs 0.990。
    """
    m = _meter()
    out, base = m.measure(DRIFTING), m.measure(VARIED)
    assert out["判为逐字复读条数"] == 0, "前提：逐字那把尺子确实漏了（否则这条测试没意义）"
    assert out["正文首6字去重率"] == 1.0, "前提：句式也是满分的"
    assert out["词面复用均值"] > base["词面复用均值"] * 5, (out, base)
    assert out["型例比"] < base["型例比"], (out, base)
    assert "乐土" in out["高频意象"], out["高频意象"]


def test_varied_topics_do_not_get_flagged() -> None:
    """四个完全不同的话题：不该有哪个字组占到半数以上。"""
    out = _meter().measure(VARIED)
    assert out["高频意象"] == "", out["高频意象"]
    assert out["词面复用均值"] < 0.05, out["词面复用均值"]


def test_lexical_metrics_survive_empty_and_tiny_samples() -> None:
    m = _meter()
    assert m.lexical_metrics([]) == {"型例比": 0.0}
    one = m.lexical_metrics([{"text": "早"}])
    assert one["词面复用均值"] == 0.0  # 一条没有"更早"可比，不该凭空报个分
    assert one["高频意象"] == ""


def test_imagery_threshold_is_a_share_of_the_sample() -> None:
    """门槛按**比例**而不是绝对条数：样本从 8 条涨到 20 条，"反复用"的标准不该悄悄变松。"""
    m = _meter()
    rows = [{"text": "乐土的湖边今天很安静呀，风吹过来的时候我在想事情。"}] * 3
    rows += [{"text": f"第{i}句说的是别的玩意儿，跟前面那些毫不相干的内容哦"} for i in range(12)]
    out = m.lexical_metrics(rows)
    assert "乐土" not in out["高频意象"], out["高频意象"]  # 3/15 远不到 60%
    # 8/8 全在念它 ⇒ 必须报出来。（不用"同一句复制 N 遍"当样本：那种语料里
    # 「土的」「候我」这类**跨词边界的碎片**会盖过真意象 —— 这是字组法的已知脾气，
    # 见 `lexical_metrics` 的说明；真语料是一句句不同的，才轮到共同的意象浮上来。）
    assert "乐土" in m.lexical_metrics(DRIFTING)["高频意象"]
