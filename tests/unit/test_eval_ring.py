"""评测环自己能不能立起来 —— 一条不花模型调用的守卫。

来历（2026-09-26）：填"数据引用正确率"那格时才发觉 `scripts/run_eval.py` **从服务策略那次
重构起就跑不起来**了。成因不在评测环里，在一句被当真的话：`make_embedder` 的注释写着
"`seed_once()` 恒为每类服务播种一条启用的内置行 ⇒ 生产上 `order` 永不为空"。
那句话对**走装配根的进程**成立，而 `run_eval.py` 与 `seed_demo_data.py` 是自己
`connect + bootstrap` 建库的 —— `storage.db.bootstrap` 根本不播「服务」页那些行，
播种是 `core/bootstrap.py` 的 Runtime 干的。于是脚本拿到一张空的端点表，
`make_embedder` 大声失败。

"大声失败"是它唯一的优点：这一族问题通常连失败都没有。真正让它躲了一个多月的是
**没有任何一条不依赖模型的东西会去碰这条路径** —— 门禁里没有，冒烟里也没有。

所以这个文件只做一件事：按脚本的方式建一份库，然后要求那条路径走得通。
它跑 1 秒、不联网、不调模型，因此可以进 fast 门禁；下次谁再动服务播种，这里当场红。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("run_eval_ring", ROOT / "scripts" / "run_eval.py")
assert _spec and _spec.loader
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def test_the_scripts_own_bootstrap_leaves_a_usable_embedding_endpoint(tmp_path: Path) -> None:
    """脚本自己建的那份库里，嵌入端点必须挑得出来（`seed_once()` 不能只由装配根调）。"""
    from rolecard_agent.core.bootstrap import candidate_ids
    from rolecard_agent.core.services import ServiceEndpointService
    from rolecard_agent.rag.retriever import HashEmbedder, make_embedder
    from rolecard_agent.storage.db import connect

    db = tmp_path / "ring.db"
    conn = connect(db)
    ev._seed_demo_data(conn)  # 与 run_eval / seed_demo_data 完全同一条路径

    services = ServiceEndpointService(conn)
    order = candidate_ids(services, "embedding")
    assert order, (
        "「服务」页是空的 —— 这就是 09-26 那次评测环跑不起来的原样。"
        " `storage.db.bootstrap` 不播端点，脚本必须自己调 seed_once()"
    )
    embedder = make_embedder(
        object(), order=order, endpoints=services.endpoint_map("embedding")
    )
    assert isinstance(embedder, HashEmbedder), "离线环里嵌入器必须是内置 hash 那档（不调云端）"
    conn.close()


def test_seeding_the_knowledge_documents_does_not_need_a_model(tmp_path: Path) -> None:
    """`_seed_knowledge` 是那条路径上真正会炸的地方，而且它不需要模型。

    它当初的坏法是 `make_embedder` 签名变了而调用方还按老样子调 —— 那类漂移由 mypy 拦住了
    一次（S-6 把 `scripts/` 纳进类型检查），但**端点表为空**这一类运行时失败类型看不出来，
    所以这条用例是 mypy 之外的另一半。
    """
    from rolecard_agent.config import Settings
    from rolecard_agent.storage.db import connect

    db = tmp_path / "ring2.db"
    conn = connect(db)
    ev._seed_demo_data(conn)
    conn.close()
    settings = Settings.from_env()
    object.__setattr__(settings, "chroma_path", tmp_path / "chroma")
    ev._seed_knowledge(db, settings)  # 不抛就是过
    assert (tmp_path / "chroma").exists()


def test_the_seeded_facts_are_what_the_citation_cases_assert_against(tmp_path: Path) -> None:
    """种进去的数与用例里断言的数必须同源 —— 否则"引用错"与"数据变了"分不开。

    这条不是可选的整洁：cite-002 第一版三遍全挂，我去查了模型说了什么，才发现它照实报了
    「参考区间 0-5」而我的断言把那个 5 当成了旧报告的 5.0。如果当时库里种的区间不是 0-5，
    那个坑要等到有人改数据才炸。
    """
    from rolecard_agent.domains.health.service import HealthQueryService
    from rolecard_agent.storage.db import connect

    db = tmp_path / "ring3.db"
    # 必须用项目的 connect（它设 row_factory）：裸 sqlite3.connect 会让 `_migrate`
    # 里的 `r["name"]` 当场 TypeError —— 那是用例的错，不是被测码的错。
    conn = connect(db)
    ev._seed_demo_data(conn)
    # `check_time` 在报告上、数值在指标上 —— 这一句要连表（写错表名是我想当然）
    rows = conn.execute(
        "SELECT r.check_time, i.index_name, i.index_value, i.ref_range"
        " FROM medical_index i JOIN medical_report r ON r.report_id = i.report_id"
        " ORDER BY r.check_time, i.index_name"
    ).fetchall()
    conn.close()
    # 排序按 (检查时间, 指标名)：中文按码位，"尿酸"排在"结石直径"前面 —— 断言按集合写，
    # 不锁顺序（锁顺序的断言会因为排序规则变化而红，那与"数据对不对"无关）
    assert {(r[0], r[1], float(r[2])) for r in rows} == {
        ("2025-05-01", "结石直径", 5.0),
        ("2026-03-12", "结石直径", 6.0),
        ("2026-03-12", "尿酸", 488.0),
    }, "种的数据变了 ⇒ tests/eval/cases/health.json 里的引用断言要一起改"
    assert {str(r[3]) for r in rows} == {"0-5", "208-428"}, (
        "参考区间是反向断言的雷区：0-5 里躺着 5 与 0，208-428 里躺着 428。"
        "改了它 ⇒ 用例里那些「禁某个数」的断言要重新核一遍，别等模型替你红"
    )
    assert HealthQueryService  # 域服务在位（这条路径要能 import 到它）


def test_every_citation_case_names_numbers_that_exist_in_the_seed(tmp_path: Path) -> None:
    """用例里出现的每个"必须被引用到的数"，都得在种的数据里 —— 反过来不要求。

    这是"尺子量的是不是真东西"的最小检查：断言一个种进去没有的数，那条用例永远红，
    于是它会被当成模型的错，而实际是评测集自己坏了。
    """
    import json

    from rolecard_agent.storage.db import connect

    db = tmp_path / "ring4.db"
    conn = connect(db)
    ev._seed_demo_data(conn)
    values = {float(r[0]) for r in conn.execute("SELECT index_value FROM medical_index")}
    ranges = {
        float(x.strip())
        for (r,) in conn.execute("SELECT ref_range FROM medical_index")
        for x in str(r or "").split("-")
        if x.strip().replace(".", "", 1).isdigit()
    }
    conn.close()
    cases = json.loads((ROOT / "tests" / "eval" / "cases" / "health.json").read_text("utf-8"))
    for case in cases:
        for a in case.get("assertions") or []:
            if a.get("kind") not in {"answer_contains_value", "answer_value_near_marker"}:
                continue
            v = float(a["value"])
            assert v in values, (
                f"{case['id']} 断言要引 {v}，可种的数据里没有这个数：{sorted(values)}"
            )
    # 反向断言禁掉的数，不许落在"正确回答合法会说出口"的那批数里（参考区间就是踩过的坑）
    for case in cases:
        for a in case.get("assertions") or []:
            if a.get("kind") != "answer_absent_value":
                continue
            v, tol = float(a["value"]), float(a.get("tolerance", 0.05))
            for legal in values | ranges:
                assert abs(legal - v) > tol, (
                    f"{case['id']} 禁了 {v}±{tol}，但 {legal} 是记录里合法会出现的数"
                    "（参考区间上限也算）—— 这条会把正确的回答判成错"
                )


@pytest.mark.parametrize("kind", ["answer_contains_value", "answer_absent_value"])
def test_value_assertions_ignore_numbers_hiding_in_dates(kind: str) -> None:
    """两个方向都不许把日期里的数字当引用（否则一个只报日期的回答能骗过正向断言）。"""
    text = "看到 2026-03-12 有复查记录。"
    fails, cit = ev._check_assertions(
        [{"kind": kind, "value": 3.0, "tolerance": 0.01}], invoked=[], final_text=text
    )
    assert cit == (0, 1) if kind == "answer_contains_value" else cit == (1, 1)
    if kind == "answer_contains_value":
        assert fails and "未包含" in fails[0], "日期里的 3 不算引用到 3.0"
    else:
        assert not fails, "反向断言也不该因为日期里躺着一个 3 就误判"
