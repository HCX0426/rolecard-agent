"""chroma 偶发（`R102-41`）现场取证层的用例。

这一层的存在理由：那发偶发**不可请求**（约 1.3-2%/轮），而每次红完只剩同一句
`Nothing found on disk`，`tmp_path` 随后就被回收 ⇒ 十五批取证批批从头。
所以它必须在红的那一刻把"**元数据说这个段在、盘上那个目录没了**"钉下来。

关键那臂是真把段目录抽走（用真 chromadb 造形状，不是我自己拼一个看着像的库）——
本仓口径：判据读的是被测系统**当下真实的 schema**，chroma 1.5.9 的 `segments` 表没有 `path` 列、
段目录名就是 `segment_id`，这两条都是我 10-03 现读来的，写死进用例才不会被下一次"按旧版猜"带走。
"""

from __future__ import annotations

import pathlib
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "forensics"))

import chroma_flake_evidence as cfe  # noqa: E402
import pytest_with_evidence as pwe  # noqa: E402

FLAKE_TEXT = "Error creating hnsw segment reader: Nothing found on disk"


@pytest.fixture
def ascii_root() -> Iterator[pathlib.Path]:
    """一个**纯 ASCII** 的临时根（resolve 之后的形状 —— 也就是真正交给 chroma 的那一条）。

    为什么不用 pytest 的 `tmp_path`（10-03 现学的，代价是一次假诊断）：`tmp_path` 按测试函数名
    生成，而本仓用例名是中文 ⇒ 路径里带非 ASCII。A/B 实测（同机同版本 chromadb）：
    ASCII 根下 vector 段目录里是 4 个 hnsw 文件，**非 ASCII 根下目录存在但一个文件都没有**，
    而 `segments` 表照旧登记那一段。也就是说拿 `tmp_path` 造 chroma 形状，造出来的是
    "元数据有段、盘上没文件"这一族，用它去测一个专门抓这族的取证层，判据当场失真。
    这条库行为已登记为台账 `R102-78`（未收口）：它是 `R102-41` 的一个**相邻缺陷**，不是那一发本身
    （在册那发发生在 ASCII 路径与 Linux runner 上）。

    **两侧都 resolve**（2026-10-08 CI 的 Windows 臂照出来的第二发）：runner 的 `TEMP` 是 **8.3
    短名** —— `gettempdir()` 给 `C:\\Users\\RUNNER~1\\…`，`resolve()` 展开成长名
    `C:\\Users\\runneradmin\\…`。原版拿**未 resolve 的 `d`** 去比**已 resolve 的基目录**，两条
    形状对不上 ⇒ `is_relative_to` 恒 False，夹具在测试第一行之前就炸（本机用户名短名与长名相同，
    所以这一发只在别人机器上照得出来）。

    为什么判 ASCII 也判 resolve 之后的那条：chroma 拿到的就是**交出去的那个字符串**，判据必须问
    被测系统真正会读的那一条。短名 ASCII **不等于**长名 ASCII —— 中文用户名的 8.3 短名可能是
    `张~1`，长名也可能整段非 ASCII。所以这里先 resolve、再判、再把 resolve 后的那条交出去。
    判不出 ASCII 根时**跳过并写明缺什么**（与"环境不满足时 skip 而非红"同一条口径）：这台机器的
    临时根天生不 ASCII，是环境不给这条用例的地基，不是取证层坏了 —— 报成断言失败只会让它在别人的
    机器上变成没人看的噪音。
    """
    base = pathlib.Path(tempfile.gettempdir()).resolve()
    d = pathlib.Path(tempfile.mkdtemp(prefix="rc_cfe_")).resolve()
    if not d.is_relative_to(base):
        shutil.rmtree(d, ignore_errors=True)
        pytest.skip(f"mkdtemp 没落在临时根里：{d}（基：{base}）—— 环境形状不对，不拿来当真诊断")
    if not all(ord(ch) < 128 for ch in str(d)):
        shutil.rmtree(d, ignore_errors=True)
        pytest.skip(f"临时根解析后不是 ASCII：{d} —— 这台机器给不出这条用例要的形状")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _chroma_with_two_collections(base: pathlib.Path):
    """真起一个持久化目录、写两个集合，返回 (client, persist 目录, 两个 vector 段 id)。"""
    chromadb = pytest.importorskip("chromadb")
    persist = base / "chroma"
    client = chromadb.PersistentClient(path=str(persist))
    for name in ("alpha", "beta"):
        col = client.get_or_create_collection(name)
        col.add(ids=["1"], documents=["hello"])
    con = sqlite3.connect(f"file:{(persist / 'chroma.sqlite3').as_posix()}?mode=ro", uri=True)
    try:
        vector_ids = [
            str(row[0])
            for row in con.execute("SELECT id, type FROM segments")
            if "vector" in str(row[1])
        ]
    finally:
        con.close()
    return client, persist, vector_ids


def test_签名判据两臂() -> None:
    assert cfe.hits_signature(FLAKE_TEXT) is True
    assert cfe.hits_signature("chromadb.errors.InternalError: x") is True
    assert cfe.hits_signature("assert 1 == 2") is False, "别的失败不该被当成那一发偶发"


def test_取证层与门禁共用同一份签名判据() -> None:
    """**清单与判法都必须只有一份出处**（两处各抄一份 ⇒ 改了判据漏了另一处）。

    2026-10-09 升级：这条从前只钉"清单对象是同一个"（`pwe.CHROMA_FLAKE_SIGNATURES is
    cfe…`），而我那轮新加的判定顺手写了 `any(sig in text …)` —— 清单共用而**匹配逻辑各写一遍**，
    两侧照样能各判各的、还都看着合理。改成钉**函数本身**之后，清单由构造函数唯一：所有消费者
    （wrapper、conftest 那个钩子）拿到的都是同一个函数，它读的是同一个模块全局 —— 判法相同
    **蕴含**清单相同，所以那条旧的清单比对连同它的再导出一起删掉（留着就得为"只给测试用"
    而多保一个 F401 的 `noqa`，那是用注释掩盖结构问题）。
    """
    assert pwe.hits_signature is cfe.hits_signature
    # conftest 那个失败时刻钩子也**不许自己算**：它在红的那一刻决定要不要落现场，
    # 与 wrapper 判的不是同一件事时，就会出现"一边认偶发、一边不认"的半张读数
    # （账本 ENGI-24① 记的正是这个形状）。
    #
    # 这里用 **AST** 而不是扫源码字符串（2026-10-09 我自己撞出来的）：第一版写的是
    # `assert "CHROMA_FLAKE_SIGNATURES" not in conftest_src` 之类，而我在钩子的 docstring 里
    # **解释这个缺陷时不得不提那个名字** ⇒ 判据当场红，且报的是"conftest 里有自抄匹配"这个
    # **假**因。与本晚 `.gitleaks.toml` 那条 `[global]`/`paths` 被散文注释误匹配是同一族：
    # 用字符串形状当判据，就会被自己的注释喂假信号。AST 看的是**真调用点**：
    # docstring 是 Expr 常量，冒充不了一次函数调用。
    assert _hook_calls_shared_predicate(), "钩子里找不到对共用判据的调用（自己算或干脆没判）"


def _hook_calls_shared_predicate() -> bool:
    """真 conftest 的那个钩子里，是否存在 `_flake.hits_signature(...)` 这个**调用**。"""
    import ast

    src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hook = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "pytest_runtest_makereport"),
        None,
    )
    if hook is None:
        return False
    for node in ast.walk(hook):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "hits_signature"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_flake"
        ):
            return True
    return False


def _load_conftest():
    """按**文件路径**加载真 conftest。

    不写 `import conftest`：`tests/` 有 `__init__.py`（是个包），跑测试时它的模块名其实是
    `tests.conftest` —— 直接 `import conftest` 在某些入口下能成、在另一些下 ModuleNotFoundError
    （本晚我已经为"凭印象写 import"红过一次）。按路径加载不依赖 sys.path 的形状。
    它自己会把 `src/`、`scripts/forensics/` 插进 sys.path，所以加载完就能用。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "conftest_under_test", str(ROOT / "tests" / "conftest.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_钩子对setup阶段的在册偶发也落现场(tmp_path: pathlib.Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """**行为臂（ENGI-24① 那格的两处叠加缺陷，一次全钉）**。

    run 37860489135 的 Windows 臂那次 chroma `(code: 5) database is locked` 是
    `ERROR at setup of …` —— 发生在 **fixture 阶段**，而旧钩子第一句就是
    `if call.when != "call": return` ⇒ 现场从没落盘；就算过了那道闸，旧钩子喂判据的是
    `str(exc)`，而**在册的三条签名里有一条是类名**（`chromadb.errors.InternalError`），
    `str()` 永远不含自己的类名 ⇒ 那个调用点上它**物理不可能命中**（实测两半各问一遍，
    见 `build/measure_hook_text_asymmetry.py`：wrapper 认、钩子不认）。
    两处叠在一起 = 这一发形状本层**从没可能**看见，而它正是这层存在的理由。
    """
    import sys
    from types import SimpleNamespace

    import chromadb.errors as cherrors

    conftest = _load_conftest()
    monkeypatch.setattr(conftest, "BUILD_DIR", tmp_path)
    exc = cherrors.InternalError(
        "Query error: Database error: error returned from database: (code: 5) database is locked"
    )
    try:
        raise exc
    except cherrors.InternalError:
        excinfo = pytest.ExceptionInfo.from_exception(sys.exc_info()[1])

    item = SimpleNamespace(nodeid="tests/test_api_edges.py::test_upload", funcargs={})
    # setup 阶段 + 只带类名才认得出的那条签名：旧写法在这两处**都**看不见
    conftest.pytest_runtest_makereport(item, SimpleNamespace(when="setup", excinfo=excinfo))

    ev = list(tmp_path.glob("r102-41-evidence-*.txt"))
    assert len(ev) == 1, (
        f"setup 阶段的在册 chroma 偶发没落现场（{len(ev)} 份）—— "
        "「红跑不许没有现场」这层守卫在它最需要的形状上失明"
    )
    text = ev[0].read_text(encoding="utf-8")
    assert "chromadb.errors.InternalError" in text, f"现场里没有类名那一半：{text[:200]}"


def test_钩子对不在册的失败不落现场(tmp_path: pathlib.Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """反向臂（上一发不许是"什么都落"换来的）：真 bug 不该被当成那一发偶发去取证。"""
    import sys
    from types import SimpleNamespace

    conftest = _load_conftest()
    monkeypatch.setattr(conftest, "BUILD_DIR", tmp_path)
    try:
        raise AssertionError("1 == 2，这是真红了")
    except AssertionError:
        excinfo = pytest.ExceptionInfo.from_exception(sys.exc_info()[1])
    item = SimpleNamespace(nodeid="tests/unit/test_x.py::test_y", funcargs={})
    conftest.pytest_runtest_makereport(item, SimpleNamespace(when="call", excinfo=excinfo))
    assert not list(tmp_path.glob("r102-41-evidence-*.txt")), (
        "不在册的失败也被取证 = 现场堆满无关文件，下一次真偶发淹在里面"
    )


def test_钩子没有异常时不做事(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`excinfo is None`（绿/跳过）必须直接返回 —— 它跑在**每一个**报告上，白干活就是全套件税。"""
    from types import SimpleNamespace

    conftest = _load_conftest()
    monkeypatch.setattr(conftest, "BUILD_DIR", tmp_path)
    item = SimpleNamespace(nodeid="tests/unit/test_z.py::test_ok", funcargs={})
    conftest.pytest_runtest_makereport(item, SimpleNamespace(when="call", excinfo=None))
    assert not list(tmp_path.glob("r102-41-evidence-*.txt"))


def test_wrapper的判定跟着共用清单实时走(monkeypatch: pytest.MonkeyPatch) -> None:
    """**行为臂**（变异实测教出来的）：光钉"`pwe.hits_signature is cfe.hits_signature`"不够 ——

    那两发变异里，①（conftest 自抄）被抓到，②（wrapper 自己另算一份却**仍保留**那个 import）
    照绿：函数同一性成立，判定却走了另一条代码。而"判法相同"这件事**没法靠读源码字符串钉**
    （本文件里的注释本身就写着 `any(sig in text …)` 作为反面教材，扫字符串会把自己的历史当缺陷 ——
    与 `.gitleaks.toml` 那个死表同款误报）。所以问一个可证的**行为**问题：换掉共用清单，
    wrapper 的答案必须跟着变。
      * 真走 `hits_signature(text)`：函数在调用时读 cfe 的模块全局 ⇒ 立刻反映新清单；
      * 自抄一份 `from … import CHROMA_FLAKE_SIGNATURES` + 自己 `any(...)`：那份绑定在
        import 时就冻结了 ⇒ 换清单它不认，这一发红。
    """
    monkeypatch.setattr(cfe, "CHROMA_FLAKE_SIGNATURES", ("哨兵签名XYZ",))
    assert pwe._is_chroma_flake("前缀 哨兵签名XYZ 后缀") is True, (
        "换了共用清单，wrapper 却说不是偶发 ⇒ 它手里有第二份判法"
    )
    assert pwe._is_chroma_flake(FLAKE_TEXT) is False, (
        "旧清单的形状还认 ⇒ 同上：判法被抄了第二份"
    )
    # 空原因仍是"没证据"，与清单内容无关（fail-closed 那一侧不能被这个改动带跑）
    assert pwe._is_chroma_flake("   ") is False


def test_落盘文件带nodeid与异常原文(tmp_path: pathlib.Path) -> None:
    out = cfe.dump_evidence(tmp_path / "ev", "tests/unit/test_a.py::test_b", FLAKE_TEXT)
    text = out.read_text(encoding="utf-8")
    assert out.name.startswith("r102-41-evidence-")
    assert "test_b" in text and FLAKE_TEXT in text


def test_两次现场不互相覆盖(tmp_path: pathlib.Path) -> None:
    cfe.dump_evidence(tmp_path, "test::a", FLAKE_TEXT)
    cfe.dump_evidence(tmp_path, "test::b", FLAKE_TEXT)
    assert len(cfe.existing_evidence(tmp_path)) == 2, "覆盖掉上一次的现场就等于没取证"


def test_段目录在盘上时逐段列尺寸(ascii_root: pathlib.Path) -> None:
    client, persist, vector_ids = _chroma_with_two_collections(ascii_root)
    try:
        lines = cfe._segment_state(persist)
    finally:
        client.close()
    body = "\n".join(lines)
    assert "目录在盘" in body, body
    assert "data_level0.bin" in body, "hnsw 那件文件是该层的判据本体，得真读出来"
    assert "0 个 vector 段的目录不在盘上" in body, body


def test_把段目录抽走时它必须点名(ascii_root: pathlib.Path) -> None:
    """**这一条是这层存在的全部理由**：元数据说有、盘上没有 ⇒ 现场里必须有那个段与它的集合。"""
    client, persist, vector_ids = _chroma_with_two_collections(ascii_root)
    assert vector_ids, "造不出 vector 段，这条用例就没在量任何东西"
    victim = vector_ids[0]
    client.close()
    shutil.rmtree(persist / victim)
    lines = cfe._segment_state(persist)
    body = "\n".join(lines)
    assert victim in body, body
    assert "目录不在盘上" in body, body
    assert "1 个 vector 段的目录不在盘上" in body, body


def test_注册表为空时第二腿仍留得下盘上形状(ascii_root: pathlib.Path) -> None:
    """第一版只问注册表 —— 而 `Client.close()` 之后它是**空的**（`R102-74` 那条库行为）。

    症状会是：现场里一句"注册表为空"然后什么盘上形状都没有，看着像"没发生那发偶发"。
    所以调用方可以传扫描根（conftest 传用例的 tmp_path），从 `chroma.sqlite3` 现找。
    """
    client, persist, vector_ids = _chroma_with_two_collections(ascii_root)
    victim = vector_ids[0]
    client.close()
    shutil.rmtree(persist / victim)
    out = cfe.dump_evidence(
        ascii_root / "ev", "test::closed_registry", FLAKE_TEXT, extra_roots=[ascii_root]
    )
    body = out.read_text(encoding="utf-8")
    assert "注册表为空" in body, body
    assert victim in body and "目录不在盘上" in body, "第二腿没把形状留住，这层就白加"


def test_库读不出时如实写一行而不是留空(tmp_path: pathlib.Path) -> None:
    """空与"干净"长得一模一样（`R102` 轮那条分母教训），所以每条问不出都要有字。"""
    persist = tmp_path / "chroma"
    persist.mkdir()
    rows = cfe._segment_state(persist)
    assert any("没有 chroma.sqlite3" in row for row in rows), rows
    (persist / "chroma.sqlite3").write_bytes(b"not an sqlite file at all")
    body = "\n".join(cfe._segment_state(persist))
    assert "读不出" in body or "打不开" in body, body
