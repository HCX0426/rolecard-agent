"""身份接缝（§4.1「本地为底、登录换身份」的第一步）。

要买的东西一句话：**"这次请求是谁"在接入层只有一处回答，而且它真的能换一个人。**
今天 `app_user` 里只有播种那一行，所以这条链在生产上恒等于 `local-user` —— 但"恒等于"
和"写死了所以将来改不动"是两件事，前者有测试钉着、后者什么都没有。

六条判据，每条都在挡一个具体的坏法：

  1. 匿名请求 = 本机那份（v1 的默认形态，零配置可用）；
  2. **别人名下的会话读不到，而且回 404 不回 403** —— 403 等于承认"这条存在，只是你不能看"，
     而 `thread_id` 是 `s_<12 hex>` 这种可猜形状，泄露存在性就是把别人的线变成靶子；
  3. 认证成 `app_user` 里**确实有行**的那个人 ⇒ 看到的就是他那份（换身份的接缝通了）；
  4. 用户名对不上任何一行 ⇒ 回落本机那份，**不凭空造一个身份**（否则陌生用户名写进库里的
     数据，没有任何地方认识它）；
  5. 身份记在**本请求的视图**上，不写回共享的 `app.state.ctx`（并发请求互相看见对方的身份）；
  6. 列表端点按身份过滤，不是"先全查出来再在前端藏起来"。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.core.identity import DEFAULT_USER_ID, resolve_identity


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mode: str = "on",
    creds: str = "u1:pw,local-user:pw",
) -> TestClient:
    monkeypatch.setenv("AUTH_MODE", mode)
    monkeypatch.setenv("AUTH_CREDENTIALS", creds)
    return TestClient(create_app(sqlite_path=tmp_path / "app.db"))


def _own_a_thread(client: TestClient, thread_id: str, user_id: str) -> None:
    """直接落库造"某个人名下的一条会话"，并把 `app_user` 那行补上（有外键）。"""
    conn = client.app.state.ctx.conn
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
        "VALUES (?, 'local', ?)",
        (user_id, user_id),
    )
    conn.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, title) "
        "VALUES (?, ?, 'general_assistant', ?)",
        (thread_id, user_id, f"{user_id} 的线"),
    )
    conn.commit()


# -- 1 & 2：本机那份 / 不是你的就读不到 --------------------------------------------


def test_anonymous_requests_read_the_local_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _client(monkeypatch, tmp_path, mode="off")
    _own_a_thread(client, "s_local_line", DEFAULT_USER_ID)
    assert client.get(f"/api/session/{'s_local_line'}/turn").status_code == 200


def test_someone_elses_thread_is_a_404_not_a_403(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """404 是**故意**的：状态码本身不能承认这条线程存在。"""
    client = _client(monkeypatch, tmp_path, mode="off")
    _own_a_thread(client, "s_foreign_line", "u1")
    res = client.get("/api/session/s_foreign_line/turn")
    assert res.status_code == 404
    # 回的不许是"这条存在但不是你的"（那是 403 的说法）：detail 里只有对方自己报上来的 id
    assert res.json() == {"detail": "对话不存在：s_foreign_line"}
    # 也不许从列表里漏出去
    assert [s["thread_id"] for s in client.get("/api/sessions").json()] == []


# -- 3：换身份这条路真的通 ------------------------------------------------------------


def test_authenticating_as_a_known_user_switches_the_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """这一条是整件事的验收：同一个人、同一份库，凭据不同 ⇒ 看到的是**两份数据集**。

    为什么现在就能测：`app_user` 加一行、`AUTH_CREDENTIALS` 里用户名对上它，身份就换了 ——
    不需要等 §4.1 那台服务器，也不需要先给那 15 张表补列（补列是 M2 的迁移，改的是"还有哪些
    东西跟着身份走"，不是"身份能不能被认出来"）。
    """
    client = _client(monkeypatch, tmp_path)
    _own_a_thread(client, "s_foreign_line", "u1")
    _own_a_thread(client, "s_local_line", DEFAULT_USER_ID)

    as_u1 = client.get("/api/sessions", headers={"authorization": _basic("u1", "pw")}).json()
    assert [s["thread_id"] for s in as_u1] == ["s_foreign_line"]
    assert client.get(
        "/api/session/s_foreign_line/turn", headers={"authorization": _basic("u1", "pw")}
    ).status_code == 200
    # 反过来也一样：u1 读不到本机那份
    assert client.get(
        "/api/session/s_local_line/turn", headers={"authorization": _basic("u1", "pw")}
    ).status_code == 404


def test_a_stranger_username_invents_no_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`AUTH_CREDENTIALS` 里有、`app_user` 里没有 ⇒ 回落到本机那份，而不是凭空多出一个主人。"""
    client = _client(monkeypatch, tmp_path, creds="nobody:pw")
    _own_a_thread(client, "s_local_line", DEFAULT_USER_ID)
    ids = [s["thread_id"] for s in client.get(
        "/api/sessions", headers={"authorization": _basic("nobody", "pw")}
    ).json()]
    assert ids == ["s_local_line"]


# -- 5：身份是请求级的，不是进程级的 --------------------------------------------------


def test_identity_is_carried_per_request_not_on_the_shared_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """共享的那份 `AppContext` 永远不许被某一次请求写脏。

    为什么单独立一条：`get_context` 从前直接 `cast` 出 `app.state.ctx` 那个**全应用共享**的
    实例，谁在上面记"这次是谁"，两个并发请求就会互相看见对方的身份 —— 与 `_begin_db_request`
    注释里那条"在事件循环线程里 rollback，清的是别的线程的连接"同一族错法。
    """
    from rolecard_agent.api.auth import Actor
    from rolecard_agent.api.deps import AppContext

    client = _client(monkeypatch, tmp_path)
    _own_a_thread(client, "s_foreign_line", "u1")
    shared: AppContext = client.app.state.ctx
    assert shared.current_user() == DEFAULT_USER_ID

    as_u1 = AppContext(runtime=shared.runtime, actor=Actor(id="u1", kind="basic"))
    assert as_u1.current_user() == "u1"
    # 上面那次解析没留下任何痕迹：共享视图仍是本机那份
    assert shared.current_user() == DEFAULT_USER_ID


# -- 6：解析函数本身的三条形状 --------------------------------------------------------


def test_resolve_identity_only_matches_real_users(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    client = _client(monkeypatch, tmp_path)
    conn = client.app.state.ctx.conn
    assert resolve_identity(conn, None) == DEFAULT_USER_ID
    assert resolve_identity(conn, "ghost") == DEFAULT_USER_ID
    _own_a_thread(client, "s_foreign_line", "u1")
    assert resolve_identity(conn, "u1") == "u1"
