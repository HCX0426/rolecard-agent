"""通用领域记录那族端点的错误映射（覆盖率基线点名的 `api/routers/domains.py` 65%）。

路由文件自己 docstring 写的契约是 **找不到 = KeyError → 404、规则不允许 = ValueError → 400**。
这个文件不测领域服务（那半住在 `core/domain/domain_data.py` 的用例里），只钉**这条 HTTP 边界上
两种异常确实翻成了两个码** —— 因为翻错的时候没人会看见：

* 把 KeyError 也翻成 400：界面弹"请输入正确的域"，而用户什么都没输错，是记录不在；
* 把 ValueError 也翻成 404："label 不能为空"这种**写给用户看的话**被吞成"找不到"，
  用户只会反复点保存钮；
* 两个码在**同一条路由上**必须不同 —— 混起来最省事的那种"统一 400"正是这条用例要拦的。

不测"不可修改的字段"那类分支：路由的 body 模型只收五个可改字段，那种 ValueError
从这条 HTTP 路进不来（要测它该去 core 那层测，不在这里造一条进不去的路）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app


@pytest.fixture
def client(tmp_path):
    # 不起 lifespan：调度器与本文件无关，而少一个后台线程就少一处时序变量
    c = TestClient(create_app(sqlite_path=tmp_path / "app.db"))
    # 出厂不带任何代码侧的域（2026-11-07 域包物理删除）：本文件要验的"域存在时"的前提
    # 由这一行自己建 —— 通用记录通路的入口闸要求域在清单上活着。
    c.post("/api/settings/domains", json={"id": "notes"})
    return c


def test_未知域给_404_且话说出口(client: TestClient) -> None:
    """`GET /api/domains/{未知}/records`：清单上没有这个名字 ⇒ 404，并明确说"不存在或已删除"。

    2026-11-07 起，记录通路的入口闸查的是 `plugin` 清单（与界面/`list_domains` 同一事实源）：
    域不存在与域被删掉是同一种答案，不再靠 id 形状去猜 —— 那会让"删了域、端点还在服务"
    的两套真相活下来。id 形状仍然由 `check_domain` 在**新建**那一侧把关。
    """
    resp = client.get("/api/domains/no_such_domain/records")
    assert resp.status_code == 404, resp.text
    assert "不存在或已删除" in resp.json()["detail"], resp.text


def test_空标签与两个值都缺各说一句人话(client: TestClient) -> None:
    """create 的两种拒绝原因不许共用一句 —— 用户要改的不是同一件东西。"""
    blank = client.post(
        "/api/domains/notes/records",
        json={"label": "   ", "value_num": 1},
    )
    assert blank.status_code == 400, blank.text
    assert "label 不能为空" in blank.json()["detail"], blank.text

    empty = client.post("/api/domains/notes/records", json={"label": "一条记录"})
    assert empty.status_code == 400, empty.text
    assert "至少填一个" in empty.json()["detail"], empty.text


def test_同一条_patch_路由上两种异常两个码(client: TestClient) -> None:
    """边界契约的全部意义在这里：一条改不出名堂（400）、一条对象不存在（404）。"""
    nothing = client.patch("/api/domains/notes/records/abc", json={})
    assert nothing.status_code == 400, nothing.text
    assert "没有任何要更新的字段" in nothing.json()["detail"], nothing.text

    missing = client.patch("/api/domains/notes/records/abc", json={"note": "改一下"})
    assert missing.status_code == 404, missing.text
    assert "记录不存在" in missing.json()["detail"], missing.text
    assert nothing.status_code != missing.status_code, "两个码混成一个，这条路由就撒谎了"


def test_删除不存在的记录是_404_不是_500(client: TestClient) -> None:
    resp = client.delete("/api/domains/notes/records/abc")
    assert resp.status_code == 404, resp.text
    assert "记录不存在" in resp.json()["detail"], resp.text
    # 同一族的另一半：**域不在清单上**时也是 404，但话说的是"域不存在或已删除" ——
    # "域写错了"与"记录没了"仍然是两句不同的话，只是都归在 404（域不活 => 它的记录不服务）。
    bad_domain = client.delete("/api/domains/no_such_domain/records/abc")
    assert bad_domain.status_code == 404, bad_domain.text
    assert "不存在或已删除" in bad_domain.json()["detail"], bad_domain.text


def test_一轮增删改之后错误映射不吞审计(client: TestClient) -> None:
    """正向一条：真删成功时是 204，且**不**留下那条记录 —— 防"错误分支测绿了，
    成功分支的返回形状被顺手改坏却没人看"。"""
    created = client.post("/api/domains/notes/records", json={"label": "体重", "value_num": 71.5})
    assert created.status_code == 201, created.text
    rid = str(created.json()["id"])

    listed = client.get("/api/domains/notes/records").json()
    assert [r["id"] for r in listed] == [rid], listed

    # 成功的那条 patch：错误分支测绿了而成功形状被改坏，也没人会看
    patched = client.patch(f"/api/domains/notes/records/{rid}", json={"note": "晨起空腹"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["note"] == "晨起空腹", patched.text
    assert patched.json()["label"] == "体重", "没给的字段不许被动到（exclude_unset 的判据）"

    gone = client.delete(f"/api/domains/notes/records/{rid}")
    assert gone.status_code == 204, gone.text
    assert client.get("/api/domains/notes/records").json() == []
    # 删完再改它：仍是 404（而不是"改成功了"或 500）
    assert client.patch(f"/api/domains/notes/records/{rid}", json={"note": "x"}).status_code == 404


# ---------------------------------------------------------------- 域本身的增删（资料页那两个钮）


def _ids(client: TestClient) -> list[str]:
    return [d["id"] for d in client.get("/api/domains").json()]


def test_新建数据域立刻可用且再撞名报错(tmp_path) -> None:
    """用户建的纯数据域：建完就能挂记录（通用通路本就按 domain_data 表工作），
    撞现役域名是 409 而不是静默合并 —— 显式新建撞名必须让用户知道撞了谁。"""
    client = TestClient(create_app(sqlite_path=tmp_path / "d.db"))
    assert client.post("/api/settings/domains", json={"id": "trip_notes"}).status_code == 201
    assert "trip_notes" in _ids(client)

    made = client.post(
        "/api/domains/trip_notes/records", json={"label": "民宿", "value_text": "青旅"}
    )
    assert made.status_code == 201, made.text

    clash = client.post("/api/settings/domains", json={"id": "trip_notes"})
    assert clash.status_code == 409, clash.text
    # 坏 id 形状仍走"规则不允许"（400），与记录通路同一条尺子
    assert client.post("/api/settings/domains", json={"id": "2 Bad"}).status_code == 400


def test_删域清掉记录并从清单消失(tmp_path) -> None:
    """删一个域 = 清单行退役 + 它的记录一起走：只摘名字留着数据，重建同名会看见旧数据。"""
    client = TestClient(create_app(sqlite_path=tmp_path / "del.db"))
    client.post("/api/settings/domains", json={"id": "books"})
    client.post("/api/domains/books/records", json={"label": "三体", "value_num": 1})

    assert client.delete("/api/settings/domains/books").status_code == 204
    assert "books" not in _ids(client)
    # 名字没了，端点也不该再为它服务（入口闸同一张清单）：数据已随删除清空，404 而不是空数组
    assert client.get("/api/domains/books/records").status_code == 404
    # 再删一次：404（"没这个域"），不是假装成功
    assert client.delete("/api/settings/domains/books").status_code == 404


def test_用户建的域重启后还在_删了不会自己回来(tmp_path) -> None:
    """域清单完全归用户：建了要跨重启活着，删了也必须**持久**（不自愈、不复活）。

    2026-11-07 出厂域清空之后，这条判据从"播种行删了不复活"换成用户自建域的同一件事：
    写进 `plugin` 表的行重启后仍在；退役是标记而不是 DELETE，重启后清单读侧仍过滤掉它。
    """
    db = tmp_path / "boot.db"
    first = TestClient(create_app(sqlite_path=db))
    assert first.post("/api/settings/domains", json={"id": "books"}).status_code == 201
    assert "books" in _ids(first)

    again = TestClient(create_app(sqlite_path=db))
    assert "books" in _ids(again), "用户建的域重启后不该消失"
    assert again.delete("/api/settings/domains/books").status_code == 204

    third = TestClient(create_app(sqlite_path=db))
    assert "books" not in _ids(third), "删掉的域重启后又回来了"
    # 反向那一半：重新建一个同名域 = 就地复活，不是主键冲突 500
    assert third.post("/api/settings/domains", json={"id": "books"}).status_code == 201
    assert "books" in _ids(third)
