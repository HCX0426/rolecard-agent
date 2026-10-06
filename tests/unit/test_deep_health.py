"""深探（`/api/health/deep`）：它存在的理由是"进程活着"与"依赖能用"之间那道缝。

验收那句是判据的锚：**把 chroma 目录改坏，`/api/health` 照样 200，而深探报红** ——
所以每条探针都要有一条红臂一条绿臂，外加"红的时候回 503"与"一条红不污染另外两条的读数"。
"""

from __future__ import annotations

import pathlib
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from rolecard_agent.api.routers.health import health_deep, require_operator
from rolecard_agent.core.readiness import _models_check, readiness_report


def _good_db(path: pathlib.Path) -> pathlib.Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('x')")
    conn.commit()
    conn.close()
    return path


def _report(tmp_path: pathlib.Path, *, backends: list[dict[str, object]] | None) -> dict:
    return readiness_report(
        sqlite_path=tmp_path / "app.db",
        chroma_path=tmp_path / "chroma",
        model_backends=backends,
    )


def test_三项全过才算_ok(tmp_path: pathlib.Path) -> None:
    _good_db(tmp_path / "app.db")
    report = _report(tmp_path, backends=[{"id": "local"}])
    assert report["status"] == "ok", report
    assert {name: item["ok"] for name, item in report["checks"].items()} == {
        "sqlite": True,
        "chroma": True,
        "models": True,
    }


def test_库文件被弄坏就报红且说得出是哪一项(tmp_path: pathlib.Path) -> None:
    """验收那条的 sqlite 版：文件还在、内容不是库 —— `quick_check` 必须把它判死。"""
    (tmp_path / "app.db").write_bytes("这不是一个 sqlite 库".encode() * 40)
    report = _report(tmp_path, backends=[{"id": "local"}])
    assert report["status"] == "degraded"
    assert report["checks"]["sqlite"]["ok"] is False
    assert report["checks"]["sqlite"]["detail"], "红要说得出为什么"
    # 另外两项**不受牵连**：深探要的是一张表，不是一起变红。
    assert report["checks"]["models"]["ok"] is True


def test_向量库目录被改坏就报红(tmp_path: pathlib.Path) -> None:
    """验收那条的 chroma 版：本该是目录的位置放了一个文件。

    （这就是"chroma 数据目录被改坏"最小可信的复现：真实损坏形态很多，但探针只承诺
    "开不了/心跳不通 ⇒ 红"，不承诺识别某一种具体的坏法。）
    """
    _good_db(tmp_path / "app.db")
    (tmp_path / "chroma").write_text("我不是目录", encoding="utf-8")
    report = _report(tmp_path, backends=[{"id": "local"}])
    assert report["status"] == "degraded"
    assert report["checks"]["chroma"]["ok"] is False
    assert report["checks"]["chroma"]["detail"]
    assert report["checks"]["sqlite"]["ok"] is True, "库是好的，不许被向量库牵连"


def test_库文件不在也是红且话不一样(tmp_path: pathlib.Path) -> None:
    """"文件不在"与"文件坏了"是两种现场：深探不做启发式，如实分开说。"""
    report = _report(tmp_path, backends=[{"id": "local"}])
    assert report["checks"]["sqlite"]["ok"] is False
    assert "不存在" in report["checks"]["sqlite"]["detail"]


def test_模型配置读不出来与一条都没配是两种话() -> None:
    """`None` = 读配置本身失败；`[]` = 没配。把两者说成同一句，排障的人会去改错东西。"""
    unreadable = _models_check(None)
    empty = _models_check([])
    assert unreadable["ok"] is False and empty["ok"] is False
    assert unreadable["detail"] != empty["detail"]
    assert "读不出来" in unreadable["detail"] and "都没有" in empty["detail"]


def test_长异常文本被截断但结论不丢() -> None:
    """detail 有长度上限（异常里可能带整条路径与栈），截断只针对文本，`ok` 照样在。"""
    from rolecard_agent.core.readiness import _DETAIL_MAX, _detail

    assert len(_detail("x" * 5000)) == _DETAIL_MAX


class _Ctx:
    """够了的最小上下文：端点只用 `settings` / `model_settings` / `current_user`。"""

    def __init__(self, tmp_path: pathlib.Path, *, backends: object) -> None:
        self.settings = SimpleNamespace(
            sqlite_path=tmp_path / "app.db", chroma_path=tmp_path / "chroma"
        )
        self.model_settings = SimpleNamespace(list_backends=lambda *, user_id: backends)

    def current_user(self) -> str:
        return "u1"


def test_红的时候端点回_503_并带上那张表(tmp_path: pathlib.Path) -> None:
    """编排器/告警只看状态码；人看 body。两个都不能少。"""
    ctx = _Ctx(tmp_path, backends=[{"id": "local"}])  # 库文件不存在 ⇒ 红
    resp = health_deep(ctx=ctx)  # type: ignore[arg-type]
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 503
    assert b'"degraded"' in resp.body and b"sqlite" in resp.body


def test_全绿的时候端点就回那张表(tmp_path: pathlib.Path) -> None:
    _good_db(tmp_path / "app.db")
    resp = health_deep(ctx=_Ctx(tmp_path, backends=[{"id": "local"}]))  # type: ignore[arg-type]
    assert isinstance(resp, dict)
    assert resp["status"] == "ok"


def test_读配置抛异常不会把整张表带走(tmp_path: pathlib.Path, capsys) -> None:
    """深探的调用方在排障：读配置失败要变成**一条红的 detail**，不是 500。"""

    def boom(*, user_id: str) -> list[dict[str, object]]:
        raise RuntimeError("库锁住了")

    _good_db(tmp_path / "app.db")
    ctx = _Ctx(tmp_path, backends=[])
    ctx.model_settings = SimpleNamespace(list_backends=boom)
    resp = health_deep(ctx=ctx)  # type: ignore[arg-type]
    assert isinstance(resp, JSONResponse) and resp.status_code == 503
    assert b"models" in resp.body
    assert "deep_health.models_unreadable" in capsys.readouterr().err


def test_门禁只放操作员与本地匿名() -> None:
    """判据与装配根那句同形：off 档没有凭据、对端是回环 ⇒ 匿名也是本人；
    开了鉴权之后匿名不再等于本人，这条路自然收紧。"""
    assert require_operator(SimpleNamespace(is_anonymous=True, role="anonymous"))  # type: ignore[arg-type]
    assert require_operator(SimpleNamespace(is_anonymous=False, role="operator"))  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as got:
        require_operator(SimpleNamespace(is_anonymous=False, role="user"))  # type: ignore[arg-type]
    assert got.value.status_code == 403


def test_验收_chroma_改坏后_health_仍_200_而深探报红(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """快照那句验收的**端到端**版本，两半放进同一个进程里量。

    坏法用的是"软损坏"：目录形状还在、里面那个 sqlite 不再是库（覆盖成垃圾）。
    这一版是被现实改出来的 —— 第一版我把"本该是目录的位置放一个文件"，结果**应用根本起不来**
    （bootstrap 里建向量库那一步就抛 `os error 183`）。那是有价值的发现，也是判据的一部分：
    **最狠的坏法是 loud 的**（进程起不来，容器会明确失败），深探要接住的是**起得来但用不了**
    那一类 —— 磁盘写坏、卷没挂上、库文件被截断。所以下面两段都在：先断言"起得来"，
    再断言"起来之后改坏，免鉴权那条不受影响、深探报红"。
    """
    from fastapi.testclient import TestClient

    from rolecard_agent.api.main import create_app

    chroma = tmp_path / "chroma"
    monkeypatch.setenv("CHROMA_PATH", str(chroma))
    app = create_app(sqlite_path=tmp_path / "app.db")
    with TestClient(app) as client:
        inner = chroma / "chroma.sqlite3"
        assert inner.exists(), "bootstrap 应当已经把向量库建出来（否则这条用例的前提不成立）"
        inner.write_bytes(b"garbage" * 200)  # 软损坏：目录还在，内容不是库

        alive = client.get("/api/health")
        deep = client.get("/api/health/deep")
    assert alive.status_code == 200, alive.text
    assert alive.json()["status"] == "ok", "免鉴权那条答的是另一个问题，不许被向量库牵连"
    assert deep.status_code == 503, deep.text
    body = deep.json()
    assert body["status"] == "degraded"
    assert body["checks"]["chroma"]["ok"] is False, body
    assert body["checks"]["chroma"]["detail"], "红要说得出为什么"
