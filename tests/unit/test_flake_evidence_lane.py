"""取证包装层收成两档之后的判据（`R102-41`，10-03 批 25）。

这一层是"红跑不许没有现场"的那道守卫，所以两臂都要有牙：
命中在册签名的 chroma 偶发 ⇒ 重跑一次取证（**为取证不为转绿**）；
混进任何一条不在册的失败 ⇒ 立刻按原样红，一次都不许多跑。
后者是这条判据不能变成万能遮羞布的唯一保证（`R102-38` 那一族的教训是"看起来绿"比红贵）。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(monkeypatch, tmp_path: pathlib.Path):
    for p in (str(ROOT), str(ROOT / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(
        "pytest_with_evidence_under_test", str(ROOT / "scripts" / "pytest_with_evidence.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BUILD", tmp_path)
    return mod


FLAKE_LOG = (
    "FAILED tests/unit/test_rag.py::test_scope_isolation - "
    "chromadb.errors.InternalError: Error creating hnsw segment reader: Nothing found on disk\n"
    "1 failed, 1034 passed in 170.54s\n"
)
REAL_LOG = "FAILED tests/unit/test_x.py::y - AssertionError: 真的红了\n1 failed\n"


def test_the_two_lanes_differ_only_in_cov_and_stop_on_first_failure(monkeypatch, tmp_path) -> None:
    mod = _load(monkeypatch, tmp_path)
    fast, f1, f2 = mod.LANES["fast"]
    cov, c1, c2 = mod.LANES["coverage"]
    assert "-x" in fast and "--cov" not in fast, "快档该是裸跑 -x，不该量覆盖率"
    # 覆盖率档只说"量"（`--cov`）：**量什么范围、多少算过全在 pyproject 的 [tool.coverage.*]**。
    # 判据因此从"命令里有没有那串字面量"改成"命令里不许再有那些字面量" —— 谁把它们抄回
    # 命令行，就会再造出第二个事实面（CI/夜间臂/人肉各抄一份，漏抄的安静地量出另一个数）。
    assert "--cov" in cov and "-x" not in cov, "覆盖率档该量到底、不该一红就停"
    for banned in ("--cov-fail-under", "--cov=", "--cov-branch"):
        assert banned not in cov, f"覆盖率口径又回到命令行了（{banned}）：它住在 pyproject"
    assert len({f1, f2, c1, c2}) == 4, f"两档日志撞名了：{f1} {f2} {c1} {c2}"


def test_the_coverage_config_lives_in_pyproject(monkeypatch, tmp_path) -> None:
    """口径搬进配置之后，**配置里那三件必须在**：少了任何一件，读数就悄悄换范围。

    这一条不是给 pyproject 上保险，是给"搬了个家"这件事本身上保险：搬家时最容易
    只搬一半（比如只搬 `source` 忘了 `fail_under`，命令行删了、配置里没有 ⇒ 阈值静默消失，
    覆盖率再差也不会红 —— 那正是"看起来绿比红贵"那一族）。
    """
    import tomllib

    mod = _load(monkeypatch, tmp_path)
    assert "--cov" in mod.LANES["coverage"][0], "前提：命令只说量"
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    run = cfg["tool"]["coverage"]["run"]
    report = cfg["tool"]["coverage"]["report"]
    assert run["branch"] is True, "branch 关掉就没有【失败路径只走过一边】的信号"
    assert run["source"] == ["src/rolecard_agent"], "范围换了，历史读数就不可比"
    # 阈值活在这里一处（第七刀 85→90）。这条断言的作用是逼下一个人**来这里改**，
    # 而不是在别处加第二份配置 —— "搬了个家"是否搬全，就看改完这里红不红。
    assert report["fail_under"] == 90, "阈值不能住在别处"
    # 反向臂：命令行里不许留任何一份副本（上面那条判"在不在配置里"，这条判"有没有第二份"）
    joined = " ".join(mod.LANES["coverage"][0])
    assert "--cov-fail-under" not in joined and "rolecard_agent" not in joined


def test_unknown_lane_is_a_loud_failure_not_a_default(monkeypatch, tmp_path, capsys) -> None:
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "不存在的档"])
    calls: list[list[str]] = []
    monkeypatch.setattr(mod, "_run", lambda cmd, extra=None: calls.append(cmd) or (0, ""))
    assert mod.main() == 2
    assert calls == [], f"档位都不认识还跑了 pytest：{calls}"
    assert "不存在的档" in capsys.readouterr().err


def test_a_non_signature_failure_is_never_retried(monkeypatch, tmp_path, capsys) -> None:
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])
    calls: list[list[str]] = []

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        return 1, REAL_LOG if len(calls) == 1 else ""

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 1
    assert len(calls) == 1, "不在册的失败被重跑了 —— 这层就变成遮羞布了"
    assert "不重跑" in capsys.readouterr().out


#: fixture/收集期炸掉的形状（2026-10-09 Windows 臂真发生）：短摘要打的是 `ERROR …`，
#: 而且**原因带在册签名**。旧正则只认 `FAILED` ⇒ 一条都摘不出来 ⇒ 明明该重跑取证，
#: 却走了「不在在册签名里，不重跑」那条分支按原样红。
ERROR_LOG = (
    "ERROR tests/unit/test_api_edges.py::test_upload_stays_pending - "
    "chromadb.errors.InternalError: Query error: Database error: "
    "error returned from database: (code: 5) database is locked\n"
    "354 passed, 1 error in 233.83s\n"
)


def test_an_error_at_setup_line_is_retried_like_a_failed_one(
    monkeypatch, tmp_path, capsys
) -> None:
    """chroma 这个偶发**最常以 ERROR 出现**（fixture 期就炸），重跑通道不能只接 `FAILED`。"""
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])
    calls: list[list[str]] = []

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        if len(calls) == 1:
            return 1, ERROR_LOG
        assert "tests/unit/test_api_edges.py" in cmd, f"二跑该只跑红的那个文件：{cmd}"
        return 0, "1 passed\n"

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 0
    assert "FLAKY-RECORDED" in capsys.readouterr().out


#: 混合形状：一条在册 + 一条**真红**。旧实现问的是"整份日志里出现过签名吗"（`any`），
#: 于是这一发会整批进重跑；而重跑只跑失败的那几个文件 —— 一个"只有全套语境下才成立"的
#: 真红（跨用例污染正是这种）可以二跑绿、被记成 FLAKY-RECORDED 放行。
#: 文件头部第 1 条判据写的是「**全部**失败都带签名」，实现却更松 —— 这就是那条遮羞布。
MIXED_LOG = (
    "FAILED tests/unit/test_rag.py::test_scope_isolation - "
    "chromadb.errors.InternalError: Nothing found on disk\n"
    "FAILED tests/unit/test_x.py::y - AssertionError: 真的红了\n"
    "2 failed, 900 passed\n"
)


def test_a_mixed_batch_retries_nothing_even_with_one_flake(
    monkeypatch, tmp_path, capsys
) -> None:
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])
    calls: list[list[str]] = []

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        return 1, MIXED_LOG if len(calls) == 1 else ""

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 1
    assert len(calls) == 1, "混进真红的一批被整批重跑了：签名判据从「全部」退成了「任一」"
    out = capsys.readouterr().out
    assert "不重跑" in out
    # 不在册的那几条要**点名**，否则下一个人只能自己去 900 行里找是哪条挡了重跑
    assert "tests/unit/test_x.py" in out, out[-400:]


def test_an_unreasoned_summary_line_is_not_treated_as_a_flake(
    monkeypatch, tmp_path, capsys
) -> None:
    """空原因 = 不知道它为什么红 ⇒ 按不在册算（fail-closed），不许换来一次重跑放行。"""
    mod = _load(monkeypatch, tmp_path)
    bare = "FAILED tests/unit/test_z.py::w\n1 failed\n"
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        return 1, bare if len(calls) == 1 else ""

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 1
    assert len(calls) == 1, "没原因文本的一条红被当成 chroma 偶发重跑了"


def test_flake_retries_for_evidence_and_keeps_both_logs(monkeypatch, tmp_path, capsys) -> None:
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])
    calls: list[list[str]] = []

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        if len(calls) == 1:
            return 1, FLAKE_LOG
        assert "-x" not in cmd, "取证那一跑不该带着色 `-x`"
        assert "tests/unit/test_rag.py" in cmd, f"二跑该只跑红的那个文件：{cmd}"
        return 0, "1 passed\n"

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "FLAKY-RECORDED" in out and "机制仍未定位" in out, "放行时不许说成「已修」"
    assert (tmp_path / "gate-fast-run1.log").exists(), "首跑日志必须原样留着"
    assert (tmp_path / "gate-fast-run2-retry.log").exists()
    assert FLAKE_LOG in (tmp_path / "gate-fast-run1.log").read_text(encoding="utf-8")


def test_a_fresh_checkout_without_build_still_gets_its_evidence(
    monkeypatch, tmp_path, capsys
) -> None:
    """`build/` 是 gitignore 的 —— 全新检出里**没有**这个目录，取证日志不许因此崩。

    2026-10-09 Windows 臂实测：首跑撞上在册 chroma 偶发，本该走"重跑取证"通道，结果
    `run1.write_text` 先炸在 `FileNotFoundError: …\\build\\gate-fast-run1.log` 上 —— 取证层
    自己成了新的红，重跑根本没发生。门禁 `--ci` 照不出它纯属顺序运气（静态组的读数那一步
    先把 build/ mkdir 了），这一条臂只跑本脚本就撞上了。上面的既有用例都拿 `tmp_path`
    当 BUILD —— 那是 pytest 建好的目录，**永远照不出"目录不存在"这一格**，所以这里刻意
    指到一个不存在的子目录。
    """
    mod = _load(monkeypatch, tmp_path)
    fresh = tmp_path / "checkout-without-build"  # 刻意不建：模拟全新检出
    assert not fresh.exists()
    monkeypatch.setattr(mod, "BUILD", fresh)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast"])

    calls: list[list[str]] = []

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        calls.append(cmd)
        return (1, FLAKE_LOG) if len(calls) == 1 else (0, "1 passed\n")

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 0  # 从前这一行之前就先抛 FileNotFoundError
    assert (fresh / "gate-fast-run1.log").exists(), "首跑日志必须在（目录是被这层自己建的）"
    assert (fresh / "gate-fast-run2-retry.log").exists()
    assert "FLAKY-RECORDED" in capsys.readouterr().out


def test_the_second_run_going_red_stays_red(monkeypatch, tmp_path, capsys) -> None:
    """反向臂：二跑仍红 ⇒ 退出非 0，且不写"未知放行"那句话。"""
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "coverage"])

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002, ANN003
        return (1, FLAKE_LOG) if "--cov" in cmd else (1, FLAKE_LOG)

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "FLAKY-RECORDED" not in out and "真红" in out


#: 2026-10-10 门禁当场撞出的那一发的**原文**（不是编的：`build/gate-fast-run1.log` 里的 E 行）。
#: 126 字 —— 比短摘要行的默认回落宽度还长，这正是它被截掉的原因。
PROD_CHROMA_ERROR = (
    "chromadb.errors.InternalError: Error executing plan: Internal error: "
    "Error creating hnsw segment reader: Nothing found on disk"
)


def test_a_real_long_flake_still_matches_after_pytest_truncates(monkeypatch, tmp_path) -> None:
    """真子进程回归：**在册签名必须穿过 pytest 的列宽截断活着到达判据**。

    病根（`_run` 那行 `env=` 就是它的修法）：短摘要行 `FAILED … - 原因` 按**终端宽度**截断，
    而 captured 输出不是 tty ⇒ pytest 回落 80 列，那条 126 字的在册偶发被切成
    `chromadb.errors.Interna...`，三个签名一个不剩 ⇒ `_is_chroma_flake` 逐条问就答"不在册"
    ⇒ **该发的重跑取证从来没发生过**，而屏幕上那句"不在在册签名里，不重跑"看着完全合理
    （2026-10-10 门禁当场撞出，`build/gate-fast-run1.log` 原文为证）。

    为什么必须跑**真子进程**：既有用例全喂**手写**日志，而手写的 `FLAKE_LOG` 恰好短到签名
    没被截过 —— 这层守卫在它最该守住的那件事上从没被测过（"恒绿尺子的分母是 0"那一族）。

    **三条腿都是刻意做成跨机器确定的**（CI 第一次跑就把我第一版的两个机器假设当场照出来了，
    正是 ENGI-19 那一族，改法是量出来的不是想出来的）：
      * 整条用例先 `setenv("COLUMNS", "80")` —— 不指望"各机默认就是 80"（CI 的默认探测并不
        截断，那是机器的慷慨不是判据的形状），也不指望"各机默认很宽"；窄是钉出来的。
        于是判据腿**只在 `_run` 那道 `env=` 存在时**才读得到全签名 —— 修复在 CI 上也是有牙的；
      * 合成用例以 `tmp_path` 为 cwd、命令行用**相对文件名** —— Windows runner 上绝对路径落在
        rootdir 之外时摘要行会塌成 `FAILED ::test_it`（路径整段没了），`_failures` 那个要求
        文件名以 `.py` 结尾的锚点就什么都抓不到；
      * 判据腿把 `_run` 的回显**接进管道里**：子进程那条红若原样进父进程 stdout，会被门禁
        自己的 `_failures` 读成"又一发红"并拉进重跑清单（那个文件在仓库里根本不存在 ⇒ 收集错
        ⇒ 真红）。测一条注定失败的用例，不该在套房日志里留下它的尸体。
    """
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setenv("COLUMNS", "80")
    case = tmp_path / "test_the_real_shape.py"
    case.write_text(
        f"def test_it() -> None:\n    assert False, {PROD_CHROMA_ERROR!r}\n",
        encoding="utf-8",
    )
    argv = [
        sys.executable, "-m", "pytest", "test_the_real_shape.py",
        "-q", "-p", "no:cacheprovider",
    ]
    monkeypatch.chdir(tmp_path)  # `_run` 不接 cwd，靠进程 cwd 把相对文件名与 rootdir 对齐

    # 判据腿：走真 `_run`（它带着那道 env=），回显不许漏进套房日志
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc, log = mod._run(argv)
    assert rc != 0, "造出来的那条红没红，后面就不用判了"

    # 前提腿：同一个用例在**同样钉成 80 列**的裸子进程里确实被截（否则判据没在量截断这件事）
    narrow = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(tmp_path),
    )
    narrow_summary = "\n".join(
        ln for ln in (narrow.stdout + narrow.stderr).splitlines() if ln.startswith("FAILED ")
    )
    assert "Nothing found on disk" not in narrow_summary, (
        "80 列这一腿没复现出截断 ⇒ 判据吃的宽度形状不存在，本用例作废：" + narrow_summary[:220]
    )

    failures = mod._failures(log)
    assert failures, f"短摘要行没被抓到（形状变了？）：\n{log[-600:]}"
    for name, reason in failures:
        assert name.endswith("test_the_real_shape.py"), f"抓错了文件：{name}"
        assert mod._is_chroma_flake(reason), f"{name} 的判据读不到在册签名：{reason!r}"


# -- 分片（ENGI-35 B：Windows 臂拆并行 job）--------------------------------------

#: 固定的合成全集（120 个）：分片判据**不能**依赖仓库当前的真实文件数（那会把用例绑死在
#: "今天有 172 个文件"上，明天加一个文件就红在无关的地方）。这里喂已知清单，只问分片规则
#: 本身的性质。**为什么是 120 而不是十几个**：哈希取模在"文件数与片数同量级"时本来就可能
#: 让某一片空着，而 `_shard_selection` 对空片是**故意大声死**的（宁红不空跑）—— 第一版我
#: 喂 12 个文件跑 N=3，被这道闸拦下：红得对，错的是用例的前提，不是判据。
_SMALL_SET = [f"tests/unit/test_{n:03d}.py" for n in range(120)]


def _fixed_universe(mod, monkeypatch) -> None:
    monkeypatch.setattr(mod, "_all_test_files", lambda: list(_SMALL_SET))


def test_shards_partition_the_universe_exactly_once(monkeypatch, tmp_path) -> None:
    """**分片唯一的真风险不是慢，是"看起来都跑了"**：并集必须恒等于全集、且两片不相交。

    静态清单（每片写死一串文件）漏的是"新加的测试文件没人认领 ⇒ 那个文件从此不跑而每片全绿"，
    这条断言把"按路径哈希取模"的构造性质钉住：改片数只重分布，**不会**漏文件、不会重跑。
    """
    mod = _load(monkeypatch, tmp_path)
    _fixed_universe(mod, monkeypatch)
    for n in (1, 2, 3, 4, 5, 12):
        shards = [set(mod._shard_selection(i, n)) for i in range(n)]
        assert set().union(*shards) == set(_SMALL_SET), f"N={n} 有文件没被任何一片认领"
        for a in range(n):
            for b in range(a + 1, n):
                assert not (shards[a] & shards[b]), f"N={n} 片 {a}/{b} 重叠（那个文件跑两遍）"


def test_shard_selection_is_stable_across_calls(monkeypatch, tmp_path) -> None:
    """同一份清单必须每次挑出**同一批**文件：分片号写进 CI，飘了就等于每次跑的不是同一套。"""
    mod = _load(monkeypatch, tmp_path)
    _fixed_universe(mod, monkeypatch)
    for i in range(4):
        assert mod._shard_selection(i, 4) == mod._shard_selection(i, 4)


def test_shard_flag_is_stripped_before_pytest(monkeypatch, tmp_path) -> None:
    """`--shard` 与 `i/N` 都**不是** pytest 参数，必须在这里摘干净。

    这一格有前科：`--affected-subset` 第一版忘了摘，被当成文件清单第一项转给 pytest，
    当场 `unrecognized arguments` —— 而且**纯函数用例测不出来**（它只测"挑哪些文件"，
    测不到"命令行最后长什么样"），是真跑一趟才看见的。所以这条单独钉。
    """
    mod = _load(monkeypatch, tmp_path)
    assert mod._extra_from_argv(["--lane", "fast", "--shard", "1/4"]) == []
    assert mod._extra_from_argv(["--shard=2/4", "--lane=fast"]) == []
    # 分片号后面的位置参数不能被吃掉（那是真清单，与 `--lane` 的取值形状不同）
    assert mod._extra_from_argv(["--lane", "fast", "--shard", "0/4", "tests/x.py"]) == [
        "tests/x.py"
    ]


def test_bad_shard_argument_dies_loudly(monkeypatch, tmp_path) -> None:
    """越界/形状错的 `--shard` 必须**大声死**，不许退化成"没分片、跑全套"或"挑中 0 个文件"。

    `4/4` 若被放过去，那一片一个文件都不挑中，屏幕上"no tests ran"看着像"这片恰好空的"，
    其实是配置写错 —— 整套 CI 少跑一片而四片全绿，与本仓"分母为 0 也红"是同一条铁律。
    """
    mod = _load(monkeypatch, tmp_path)
    for bad in ("4/4", "0/0", "abc", "1/0", "-1/4"):
        with pytest.raises(SystemExit):
            mod._shard_from_argv(["--shard", bad])
    assert mod._shard_from_argv(["--lane", "fast"]) is None  # 没带 = 不分片（跑全套）
    assert mod._shard_from_argv(["--shard", "2/4"]) == (2, 4)
    assert mod._shard_from_argv(["--shard=3/4"]) == (3, 4)


def test_shard_selection_refuses_an_empty_universe(monkeypatch, tmp_path) -> None:
    """枚举到 0 个文件 ⇒ 死，不返回空清单。空清单转给 pytest 会"0 selected 全绿"。"""
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "_all_test_files", lambda: [])
    with pytest.raises(SystemExit, match="量空气"):
        mod._shard_selection(0, 4)


def test_the_enumeration_matches_pytest_own_discovery(monkeypatch, tmp_path) -> None:
    """**这条才是"不漏文件"的真判据**：分母必须来自第二个独立出处，不能自己证自己。

    上面那条用固定清单测的是**分片规则**（并集/不相交）；它证不了"规则喂进去的那份清单
    就是 pytest 会收集的那份"。口径漂移的具体形状本仓已经吃过：pytest 默认 `python_files`
    是 `test_*.py` **和** `*_test.py` 两个模式，只按其中一个枚举 ⇒ 另一种形状的测试文件
    落不进任何一片，而并集断言照样成立（两边都缺同一个文件）。
    所以这里拿 **pytest `--collect-only` 实际收集到的文件集合**与枚举对差：两个方向都必须空。
    """
    import subprocess

    mod = _load(monkeypatch, tmp_path)
    enumerated = set(mod._all_test_files())
    assert enumerated, "枚举为空：这条断言会退化成恒真"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )
    collected = {
        m.replace("\\", "/")
        for m in re.findall(r"^(\S+\.py):\s+\d+$", proc.stdout, flags=re.M)
    }
    assert proc.returncode == 0, f"pytest 收集本身失败：{proc.stdout[-400:]}"
    assert collected, "pytest 一个文件都没收集到 ⇒ 这个分母是空的，测了个寂寞"
    missing = collected - enumerated
    assert not missing, (
        "pytest 会跑、分片枚举漏掉的文件（这些文件将**不属于任何一片**）：" + f"{sorted(missing)}"
    )
    extra = enumerated - collected
    assert not extra, (
        "分片枚举里有、pytest 却不收集的文件（会把从没跑过的塞进某一片）：" + f"{sorted(extra)}"
    )


def test_shard_run_still_carries_the_subset_marker(monkeypatch, tmp_path, capsys) -> None:
    """分片那一趟**必须**带着 `[AFFECTED-SUBSET]` 记号 —— 读数机靠它拒绝把一片读成全量。

    这是分片最阴的次生风险：Windows 臂今天不经 gate 所以不写读数，将来谁把分片接到读数上，
    `N passed` 就会被读成 `backend_tests` —— 10-04 那次把 1595 洗成 32 的正是同一条路径。
    沿用同一个 token（而不是新造 `[SHARD]`）让它**结构上**不可能被当成全量。
    """
    mod = _load(monkeypatch, tmp_path)
    _fixed_universe(mod, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["x", "--lane", "fast", "--shard", "1/4"])
    calls: list[list[str]] = []

    def fake_run(cmd, extra=None):  # noqa: ANN001, ANN002
        # 真 `_run` 跑的是 `cmd + (extra or [])`：首跑的清单走 **extra**，
        # 取证二跑把文件并进 cmd —— 只收 cmd 就看不见分片挑中的文件（我第一版就漏在这）。
        calls.append(list(cmd) + list(extra or []))
        return 0, ""

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "[AFFECTED-SUBSET]" in out, out
    assert "分片 1/4" in out, out
    # 挑中的文件真的进了 pytest 命令行，而 `--shard`/`1/4` 没漏进去
    flat = [str(a) for c in calls for a in c]
    assert any("tests/unit/test_" in a for a in flat), flat
    assert not any("--shard" in a or re.fullmatch(r"\d+/\d+", a) for a in flat), flat
