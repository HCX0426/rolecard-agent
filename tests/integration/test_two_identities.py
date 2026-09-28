"""两个身份互不可见（M6）：一台实例、一份库、两次登录，逐面走一遍。

M1~M4 每步各自有验收，但那些是"按模块"的 —— 这一支是**按表面**的：同一个人格在
角色卡 / 会话与检查点 / 记忆 / 收件箱 / 模型凭据 / 事件轴六个面上各留一份数据，
然后从对面打进来，要求三件事同时成立：**看不见（列表里没有）、进不去（404 而不是 403）、
改不掉（对方的那一份事后仍在）**。第三条是前两条的照妖镜 —— 只断言状态码，
会把"守卫写了但写在了另一条 SQL 上"当成通过。

**这一支不测的**：`/api/knowledge*`、`/api/uploads/*`、插件启停、运行环境覆盖、审计流水、以及
`/api/services` 的**能力三类（ocr/embedding/rerank）** —— 它们今天是**设备级**的。
在"一台实例一个主人 + 一个数据根"（M3 前半 + M4）这个形态下这是自洽的：整份根就是那个人的。
要让同一个库里住两个身份各自的知识与文件，得先把运行期那份 `Settings` 与 chroma 的
collection 变成按身份的（§4.1 记的那条尾巴），那时这几个面才谈得上归属。
**例外的一支（多租户 B1b 已收）**：`/api/services` 的模型推理序列 = chat 引用行，按人
（默认/回退链花谁的 key 由谁定）—— 它是这一族里唯一跟着身份走的面，用例在
`test_chat_pools_are_per_identity_while_capabilities_stay_device_local`。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app

A = "local-user"  # 本机那份（也是这台实例的主人）
B = "u1"  # 第二个登录者（`app_user` 里真有这一行，否则按 M1 的语义会回落到 A）
KEY_A = "sk-aaaaaaaaaaaaaaaa"
KEY_B = "sk-bbbbbbbbbbbbbbbb"


def _as(user: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:pw".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """一台实例，两个人各建一张卡、一条会话、一条记忆、一条主动开口、一组凭据。"""
    monkeypatch.setenv("AUTH_MODE", "on")
    monkeypatch.setenv("AUTH_CREDENTIALS", f"{A}:pw,{B}:pw")
    client = TestClient(create_app(sqlite_path=tmp_path / "app.db"))
    with client:
        conn = client.app.state.ctx.conn
        conn.execute(
            "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES ('local', '本机')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) "
            "VALUES (?, 'local', '第二个')",
            (B,),
        )
        conn.commit()
        for who, tag, key in ((A, "a", KEY_A), (B, "b", KEY_B)):
            head = _as(who)
            assert client.post(
                "/api/roles",
                json={"role_id": f"r_{tag}", "role_name": f"卡{tag}", "system_prompt": "x"},
                headers=head,
            ).status_code in (200, 201), who
            made = client.post("/api/session", json={"role_id": f"r_{tag}"}, headers=head)
            assert made.status_code in (200, 201), made.text
            assert client.post(
                "/api/settings/memory/item", json={"text": f"事实{tag}"}, headers=head
            ).status_code == 200
            saved = client.put(
                "/api/settings/models",
                json={
                    "default": f"m{tag}",
                    "backends": [
                        {
                            "name": f"m{tag}",
                            "provider": "siliconflow",
                            "base_url": "https://api.siliconflow.cn/v1",
                            "model": f"Model-{tag}",
                            "api_key": key,
                            "usage": "chat",
                        }
                    ],
                    "fallbacks": [],
                },
                headers=head,
            )
            # 500 的响应体里带"模型后端构建失败：<原因>"——只断状态码会把真正的原因吞掉
            # （CI 的 Linux runner 上就因为这行只报 500，多花一轮才拿到 traceback）。
            assert saved.status_code == 200, saved.text
        # 直接写库的那一段放在所有 HTTP 之后：主线程与请求线程各持一条 sqlite 连接，
        # 一个没提交的事务会把请求侧的写入挡成 `database is locked`。
        for who, tag in ((A, "a"), (B, "b")):
            conn.execute(
                "INSERT INTO agent_reachout (role_id, role_name, text, user_id, state) "
                "VALUES (?, ?, ?, ?, 'unread')",
                (f"r_{tag}", f"卡{tag}", f"她主动说的{tag}", who),
            )
        conn.commit()
        yield client


def _ids(client: TestClient, user: str, path: str, key: str) -> set[str]:
    body: Any = client.get(path, headers=_as(user)).json()
    rows = body if isinstance(body, list) else body.get("items") or body.get("sessions") or []
    return {str(row[key]) for row in rows}


def test_role_cards_are_disjoint_both_ways(client: TestClient) -> None:
    assert {r for r in _ids(client, A, "/api/roles", "role_id")} >= {"r_a"}
    assert "r_b" not in _ids(client, A, "/api/roles", "role_id")
    assert "r_a" not in _ids(client, B, "/api/roles", "role_id")
    # 读、改、删三条路都要 404，而不是"承认它存在"
    assert client.get("/api/roles/r_b", headers=_as(A)).status_code == 404
    assert client.patch(
        "/api/roles/r_b", json={"description": "偷改"}, headers=_as(A)
    ).status_code == 404
    assert client.delete("/api/roles/r_b", headers=_as(A)).status_code == 404
    mine = [r for r in client.get("/api/roles", headers=_as(B)).json() if r["role_id"] == "r_b"]
    assert mine and mine[0].get("description") != "偷改"


def test_threads_and_checkpoints_do_not_leak(client: TestClient) -> None:
    a_lines = _ids(client, A, "/api/sessions", "thread_id")
    b_lines = _ids(client, B, "/api/sessions", "thread_id")
    assert a_lines and b_lines and not (a_lines & b_lines)
    foreign = next(iter(b_lines))
    for path in (
        f"/api/session/{foreign}",
        f"/api/session/{foreign}/messages",
        f"/api/session/{foreign}/context",
        f"/api/session/{foreign}/turn",
    ):
        assert client.get(path, headers=_as(A)).status_code == 404, path
    # 写侧四条路（改名 / 叫停 / 删历史 / 删会话）同样进不去
    assert client.patch(
        f"/api/session/{foreign}", json={"title": "偷改"}, headers=_as(A)
    ).status_code == 404
    assert client.post(f"/api/session/{foreign}/stop", headers=_as(A)).status_code == 404
    assert client.post(
        f"/api/session/{foreign}/messages/delete",
        json={"message_ids": ["whatever"]},
        headers=_as(A),
    ).status_code == 404
    assert client.delete(f"/api/session/{foreign}", headers=_as(A)).status_code == 404
    assert foreign in _ids(client, B, "/api/sessions", "thread_id"), "他的线被删掉了"


def test_memory_items_are_owner_scoped(client: TestClient) -> None:
    a_view = client.get("/api/settings/memory", headers=_as(A)).json()
    b_view = client.get("/api/settings/memory", headers=_as(B)).json()
    assert "事实b" not in str(a_view) and "事实a" not in str(b_view)
    b_id = next(int(str(i["id"])) for i in b_view["items"] if i["text"] == "事实b")
    assert client.patch(
        f"/api/settings/memory/item/{b_id}", json={"text": "偷改"}, headers=_as(A)
    ).status_code == 404
    assert client.delete(f"/api/settings/memory/item/{b_id}", headers=_as(A)).status_code == 404
    after = client.get("/api/settings/memory", headers=_as(B)).json()
    assert [i["text"] for i in after["items"] if i["id"] == b_id] == ["事实b"]


def test_inboxes_do_not_cross_and_read_all_is_scoped(client: TestClient) -> None:
    a_items = client.get("/api/reachouts", headers=_as(A)).json()["items"]
    b_items = client.get("/api/reachouts", headers=_as(B)).json()["items"]
    assert [str(i["text"]) for i in a_items] == ["她主动说的a"]
    assert [str(i["text"]) for i in b_items] == ["她主动说的b"]
    # A 点"全部已读"不许替 B 决定他看过了什么（M2c 那条口径，在这里从对面再验一次）
    client.post("/api/reachouts/read-all", headers=_as(A))
    still = client.get("/api/reachouts", headers=_as(B)).json()
    assert still["unread"] == 1, still
    foreign = int(str(b_items[0]["id"]))
    assert client.post(f"/api/reachouts/{foreign}/read", headers=_as(A)).status_code == 404
    assert client.delete(f"/api/reachouts/{foreign}", headers=_as(A)).status_code == 404


def test_model_credentials_never_spend_the_other_key(client: TestClient) -> None:
    a_groups = client.get("/api/settings/models", headers=_as(A)).json()["providers"]
    b_groups = client.get("/api/settings/models", headers=_as(B)).json()["providers"]
    assert [str(m["model"]) for g in a_groups for m in g["models"]] == ["Model-a"]
    assert [str(m["model"]) for g in b_groups for m in g["models"]] == ["Model-b"]
    assert KEY_B not in repr(a_groups) and KEY_A not in repr(b_groups)
    assert client.delete("/api/settings/models/m_b", headers=_as(A)).status_code == 404
    assert client.patch(
        "/api/settings/models/m_b/context", json={"num_ctx": 8192}, headers=_as(A)
    ).status_code == 404
    left = client.get("/api/settings/models", headers=_as(B)).json()["providers"]
    assert [str(m["model"]) for g in left for m in g["models"]] == ["Model-b"]
    assert [g["has_key"] for g in left] == [True], "他的凭据被动过"


def test_chat_pools_are_per_identity_while_capabilities_stay_device_local(
    client: TestClient,
) -> None:
    """多租户 B1b（方案 A）：对话默认/回退链按人，能力三类仍是设备级。

    夹具里各身份都已 `PUT /api/settings/models` 写了 `ma` / `mb` 两个 chat 池。这里
    A 再动一次自己的对话序列，要求三件事同时成立：A 的序列真的变了、B 的序列一个字没动
    （DELETE/INSERT 的范围是带主人的）、能力三类两人看到的仍是同一份（设备级没被带偏）。
    """
    res = client.put("/api/services/models", json={"order": ["ma"]}, headers=_as(A))
    assert res.status_code == 200, res.text
    aa = client.get("/api/settings/models", headers=_as(A)).json()
    ab = client.get("/api/settings/models", headers=_as(B)).json()
    assert aa["default"] == "ma" and aa["fallbacks"] == []
    assert ab["default"] == "mb" and ab["fallbacks"] == [], (
        "A 存一次对话序列把 B 的序列抹了或写成了 A 的"
    )
    # 服务页：模型推理节各看各的，能力三类两人一致
    svc_a = {s["key"]: s for s in client.get("/api/services", headers=_as(A)).json()["services"]}
    svc_b = {s["key"]: s for s in client.get("/api/services", headers=_as(B)).json()["services"]}
    assert svc_a["models"]["effective"] == "ma"
    assert svc_b["models"]["effective"] == "mb", "服务页的'当前默认'对不上 B 自己的序列"
    assert svc_a["ocr"]["candidates"] == svc_b["ocr"]["candidates"]
    assert svc_a["embedding"]["candidates"] == svc_b["embedding"]["candidates"]


def test_the_timeline_of_her_card_is_not_readable(client: TestClient) -> None:
    assert client.get("/api/roles/r_b/timeline", headers=_as(A)).status_code == 404
    assert client.get("/api/roles/r_a/timeline", headers=_as(B)).status_code == 404
    ok = client.get("/api/roles/r_a/timeline", headers=_as(A))
    assert ok.status_code == 200


def test_an_instance_owned_by_the_second_person_can_talk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`IDENTITY_USER_ID` 真的指到第二个人时，那台实例必须**开得出会话**。

    上面那些用例都是"一台实例、两次登录"，主人始终是演示身份 `local-user`；M5/M7 那台
    云端实例是另一种形状：**主人就是第二个人**。那一版 `app_user` 里只播种了演示身份，
    而 `session_thread.user_id` 的外键指向它 —— 两实例端到端探针第一次真的把
    `IDENTITY_USER_ID` 设成 `u1`，症状是 `POST /api/session` 当场 500 IntegrityError。
    所以这一条测的不是同步，是"那台实例本来能不能用"。
    """
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.setenv("IDENTITY_USER_ID", B)
    client = TestClient(create_app(sqlite_path=tmp_path / "cloud.db"))
    with client:
        conn = client.app.state.ctx.conn
        rows = conn.execute("SELECT user_id FROM app_user").fetchall()
        seeded = {str(r["user_id"]) for r in rows}
        assert seeded == {A, B}, "实例主人那一行没补上，外键就没有对象可指"
        made = client.post("/api/session", json={"role_id": "general_assistant"})
        assert made.status_code == 201, made.text
        owner = str(conn.execute(
            "SELECT user_id FROM session_thread WHERE thread_id = ?",
            (made.json()["thread_id"],),
        ).fetchone()["user_id"])
        assert owner == B
        # 整份数据集都是他的：出厂卡也挂在这个主人名下（§4.1"换的是整份数据集"）
        assert client.get("/api/roles").json(), "主人不是演示身份时，出厂卡没播种到他名下"


def test_deleting_my_session_does_not_touch_the_other_persons_checkpoints(
    client: TestClient,
) -> None:
    """多租户 B3：检查点按线程走，删自己的照删、删不动别人的。

    结论要钉的不是"能删"（那条早有），而是"检查点**不用加 user 列**"这句：检查点按
    `thread_id` 键，而线程 id 本身就带着归属（`get_thread` 校验），所以"删自己的会话
    不会清掉别人的检查点 / 自己的照删"在现有结构下自然成立。没有这一条，后人看着
    `checkpoints` 无 user 列会以为漏了，再去给它加一列（第二份真相）。
    """
    from langchain_core.messages import AIMessage

    from rolecard_agent.core.graph import build_graph_config

    rt = client.app.state.ctx.runtime
    graph = rt.state["graph"]

    tids: dict[str, str] = {}
    for who, tag in ((A, "a"), (B, "b")):
        one = next(
            r for r in client.get("/api/sessions", headers=_as(who)).json()
        )
        tids[tag] = str(one["thread_id"])
        # 直写一条消息进各自的检查点（与 cloud_only_shape 同一路写法）：不跑图，只为留下
        # 可数的 checkpoint 行。
        graph.update_state(
            build_graph_config(tids[tag], rt.effective),
            {"messages": [AIMessage(content=f"线{tag}")]},
        )

    def _cp_rows(tid: str) -> int:
        n = client.app.state.ctx.conn.execute(
            "SELECT COUNT(*) AS n FROM checkpoints WHERE thread_id = ?", (tid,)
        ).fetchone()["n"]
        return int(n)

    assert _cp_rows(tids["a"]) > 0 and _cp_rows(tids["b"]) > 0
    assert client.delete(f"/api/session/{tids['a']}", headers=_as(A)).status_code == 204
    assert _cp_rows(tids["a"]) == 0, "删自己的会话，自己的检查点没跟着清"
    assert _cp_rows(tids["b"]) > 0, "删自己的会话把别人的检查点清掉了"
    # 对面那条连删都进不去（404），自然更碰不到它的检查点 —— 读侧 404 纪律的老话
    assert client.delete(f"/api/session/{tids['b']}", headers=_as(A)).status_code == 404
    assert _cp_rows(tids["b"]) > 0


def test_second_login_can_open_a_session_and_write_domain_records(
    client: TestClient,
) -> None:
    """多租户 B4：第二个登录者**真能用** —— 开会话、写域记录各留一份自己的数据。

    M1–M8 都是"看不见别人"，这一条验的是正方向：B 不是只能隔着玻璃看 A 的东西，
    他自己该有完整的可用性（`R26-44` 留给 B4 的尾巴：会话 + 域记录两件都落在他名下）。
    """
    made = client.post("/api/session", json={"role_id": "r_b"}, headers=_as(B))
    assert made.status_code == 201, made.text
    owner = client.app.state.ctx.conn.execute(
        "SELECT user_id FROM session_thread WHERE thread_id = ?",
        (made.json()["thread_id"],),
    ).fetchone()["user_id"]
    assert str(owner) == B

    rec = client.post(
        "/api/domains/health/records",
        json={"label": "心率", "value_text": "72", "unit": "bpm"},
        headers=_as(B),
    )
    assert rec.status_code == 201, rec.text
    rid = str(rec.json()["id"])
    assert rid in {
        str(r["id"]) for r in client.get(
            "/api/domains/health/records", headers=_as(B)
        ).json()
    }
    assert rid not in {
        str(r["id"]) for r in client.get(
            "/api/domains/health/records", headers=_as(A)
        ).json()
    }, "B 写的记录出现在 A 的读侧"


def test_a_turn_reads_the_memory_of_its_own_owner(client: TestClient) -> None:
    """R28-04：注入的记忆与回声按**本轮主人**取，不是按实例主人。

    `Runtime.chat_memory` 原先三处写死 `self.identity` —— 单机形态无感，第二个身份一存在
    就是串数据：B 的对话里被注入 A 的事实，A 的 hit_count 还替 B 的读取涨。这里绕不开的
    只有模型（不真跑一轮），身份绑法走节点入口同一根管子 `bound_user`。
    """
    from rolecard_agent.core.identity import bound_user

    rt = client.app.state.ctx.runtime
    with bound_user(B):
        for_b = rt.chat_memory("r_b", "t_not_proactive")
    with bound_user(A):
        for_a = rt.chat_memory("r_a", "t_not_proactive")

    assert "事实b" in for_b and "事实a" not in for_b, "B 的这一轮读到了 A 的记忆"
    assert "事实a" in for_a and "事实b" not in for_a, "A 的这一轮读到了 B 的记忆"
