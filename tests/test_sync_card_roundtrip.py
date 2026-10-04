"""卡的同步与"最后一次真改动"的时刻（10-02 轮 `R102-24` / `R102-25`）。

四条各挡一个坏法，全部走端点，不 import 私有的拼装函数：

  * **交出去的载荷必须是对面收得下的** —— 那三列在库里是 JSON **文本**，而写入端校验用的
    `RoleCardUpdate` 要 **list**。从前没人翻译：每张带白名单的卡在 import 端必炸，
    而 `apply_import` 把异常折进 errors、HTTP 照回 200 —— 台账实测 3/3 张卡全灭、
    `written` 里 card 一格都没有。这条用例原来不存在的原因是**它手抄了三键的载荷**，
    从没真用过 `/api/sync/export` 交出的那一份。
  * **重复导入不许盖时刻** —— 内容一字未变也要写一次 UPDATE，就会把那张卡的
    `updated_at` 顶到"刚刚"；而卡类冲突按新者胜**自动执行**，于是两台机器互相盖个没完。
  * **开机播种不许盖时刻** —— `seed_builtins` 从前无条件 `updated_at = CURRENT_TIMESTAMP`，
    实测两张没人动过的出厂卡在一次真启动里从 03:12:07 变成 06:08:29，字段一字未改。
  * **写不进去要能在返回值里看见** —— `reconcile` 是登录时自动跑的那一次，没人盯着向导，
    它那一版整个没有 errors 这一格。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app

CARD_WITH_LIST = {
    "role_id": "withlist",
    "role_name": "带白名单的卡",
    "system_prompt": "她只准用两把工具",
    "tool_whitelist": ["web_search", "memory_save"],
    "exemplars": [{"user": "问跑步", "assistant": "每周三次"}],
    "knowledge_scopes": ["medical"],
    "description": "同步验收用的那张",
    "pet_pack": "hiyori",
}


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> TestClient:
    monkeypatch.setenv("AUTH_MODE", "off")
    return TestClient(create_app(sqlite_path=tmp_path / name))


def _card_row(client: TestClient, role_id: str) -> dict[str, Any]:
    conn = client.app.state.ctx.conn
    row = conn.execute(
        "SELECT updated_at, tool_whitelist, exemplars, knowledge_scopes, pet_pack, description "
        "FROM role_card WHERE role_id = ?",
        (role_id,),
    ).fetchone()
    return dict(row) if row else {}


def test_a_card_exported_by_the_source_is_accepted_by_the_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """源侧 `export` 交出的那张，对面 `import` 必须真的收得下（含三列 JSON 与 pet_pack）。"""
    src = _app(tmp_path, monkeypatch, "src.db")
    dst = _app(tmp_path, monkeypatch, "dst.db")
    with src, dst:
        created = src.post("/api/roles", json=CARD_WITH_LIST)
        assert created.status_code == 201, created.text
        idents = [
            {"kind": str(r["kind"]), "ident": str(r["ident"])}
            for r in src.get("/api/sync/inventory").json()["items"]
            if r["kind"] == "card"
        ]
        exported = src.post("/api/sync/export", json={"idents": idents}).json()["items"]
        card = next(i for i in exported if i["ident"] == "withlist")
        # 载荷里那三列必须是**结构**，不是库里的文本
        assert isinstance(card["payload"]["tool_whitelist"], list), card["payload"]
        assert card["payload"].get("pet_pack") == "hiyori", "形象包没进载荷 ⇒ 对面那张永远没形象"

        out = dst.post("/api/sync/import", json={"items": exported}).json()
        assert out["errors"] == [], out
        assert out["written"].get("card", 0) >= 1, f"卡没落进对面：{out}"
        got = _card_row(dst, "withlist")
        assert '"web_search"' in str(got["tool_whitelist"]) and "memory_save" in str(
            got["tool_whitelist"]
        ), got
        assert got["pet_pack"] == "hiyori", got
        assert "medical" in str(got["knowledge_scopes"]), got


def test_reimporting_an_unchanged_card_does_not_move_its_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二遍一模一样 ⇒ skipped，且 `updated_at` 一格都不许动（否则 LWW 会乒乓）。"""
    src = _app(tmp_path, monkeypatch, "src2.db")
    dst = _app(tmp_path, monkeypatch, "dst2.db")
    with src, dst:
        src.post("/api/roles", json=CARD_WITH_LIST)
        idents = [
            {"kind": str(r["kind"]), "ident": str(r["ident"])}
            for r in src.get("/api/sync/inventory").json()["items"]
            if r["kind"] == "card"
        ]
        exported = src.post("/api/sync/export", json={"idents": idents}).json()["items"]
        first = dst.post("/api/sync/import", json={"items": exported}).json()
        assert first["written"].get("card", 0) >= 1, first
        stamp = _card_row(dst, "withlist")["updated_at"]
        second = dst.post("/api/sync/import", json={"items": exported}).json()
        assert second["written"].get("card", 0) == 0, f"没变也算写了：{second}"
        assert second["skipped"].get("card", 0) >= 1, second
        assert _card_row(dst, "withlist")["updated_at"] == stamp, "重复导入把时刻顶到刚刚了"


def test_booting_does_not_restamp_cards_nobody_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开机播种是幂等的：**内容没变就不该盖 `updated_at`**（R102-25）。

    两半都要有，缺任一半这条用例就是空的：

    * 没变 => 时刻不许动。从前一次真启动把两张没人动过的出厂卡从 03:12:07 改成
      06:08:29，其余字段一字未改，于是"B 只是开了机"在结构上就能吃掉 A 的手改
      （卡类冲突按新者胜**自动执行**，`features/sync.py` 的 `auto_moves`）。
    * 真变了 => 时刻必须动、内容要回出厂值。否则这个守卫就写成"永远不写"，
      出厂卡的提示词改了也发不出去。

    第一版在这里踩过一个坑：两次启动落在同一秒里，`CURRENT_TIMESTAMP` 一字不差，
    于是把守卫摘掉用例照样绿 —— **时刻分辨率是秒的判据，必须先把时刻倒回过去**。
    """
    old = "2026-01-01 00:00:00"
    first = _app(tmp_path, monkeypatch, "boot.db")
    with first:
        conn = first.app.state.ctx.conn
        conn.execute("UPDATE role_card SET updated_at = ?", (old,))
        conn.commit()
        seeded = {
            str(r["role_id"]): (str(r["updated_at"]), str(r["system_prompt"]))
            for r in conn.execute(
                "SELECT role_id, updated_at, system_prompt FROM role_card ORDER BY role_id"
            ).fetchall()
        }
        assert seeded and all(v[0] == old for v in seeded.values()), seeded

    again = _app(tmp_path, monkeypatch, "boot.db")
    with again:
        conn = again.app.state.ctx.conn
        after_untouched = {
            str(r["role_id"]): (str(r["updated_at"]), str(r["system_prompt"]))
            for r in conn.execute(
                "SELECT role_id, updated_at, system_prompt FROM role_card ORDER BY role_id"
            ).fetchall()
        }
        assert after_untouched == seeded, f"开机给未改动的卡盖了时刻：{after_untouched}"
        victim = next(iter(after_untouched))
        conn.execute(
            "UPDATE role_card SET system_prompt = ?, updated_at = ? WHERE role_id = ?",
            ("被操作员改过的一句", old, victim),
        )
        conn.commit()

    third = _app(tmp_path, monkeypatch, "boot.db")
    with third:
        row = third.app.state.ctx.conn.execute(
            "SELECT system_prompt, updated_at FROM role_card WHERE role_id = ?", (victim,)
        ).fetchone()
        assert str(row["system_prompt"]) == seeded[victim][1], (
            "改了内容的卡没被发回出厂提示词 —— 那道守卫写过头，变成永远不写了"
        )
        assert str(row["updated_at"]) != old, f"内容真变了却没盖时刻：{row['updated_at']}"


def test_reconcile_carries_the_failures_it_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对账的返回值里必须有 errors 这一格，而且**真坏的那条要落在里面**。

    对账发生在登录时，没有人看着向导那两段读数；从前这个键整个缺席 ⇒ "一条都没写进去"
    与"今天没有东西要写"在返回值上一字不差。
    """
    client = _app(tmp_path, monkeypatch, "rec.db")
    bad_payload = {**CARD_WITH_LIST, "role_id": "badshape", "tool_whitelist": '["a", "b"]'}
    with client:
        mine = client.get("/api/sync/inventory").json()["items"]
        cards = [i for i in mine if i["kind"] == "card"]
        # 对面：我这些卡它都有且一字不差（指纹相同 ⇒ 不推），另有一条**它独有**的，
        # 而那条的载荷是"没翻译过"的旧形状（库里那种 JSON 文本）⇒ 落到本机必炸。
        theirs = cards + [
            {"kind": "card", "ident": "badshape", "hash": "f" * 16,
             "at": "2026-10-01 00:00:00", "preview": "对面那张带文本白名单的卡"}
        ]

        def fake_get(url: str, **kw: Any) -> _Resp:
            return _Resp({"items": theirs, "skipped": []})

        def fake_post(url: str, **kw: Any) -> _Resp:
            if url.endswith("/api/sync/export"):
                return _Resp({
                    "items": [{"kind": "card", "ident": "badshape", "payload": bad_payload}],
                    "absent": 0,
                })
            return _Resp({"written": {}, "skipped": {}, "errors": []})

        monkeypatch.setattr("httpx.get", fake_get)
        monkeypatch.setattr("httpx.post", fake_post)
        out = client.post(
            "/api/sync/reconcile",
            json={"base_url": "http://cloud.test:8123", "user": "local-user", "secret": "s"},
        )
        assert out.status_code == 200, out.text
        body = out.json()
        assert "errors" in body, f"对账的返回值里没有 errors 这一格：{sorted(body)}"
        assert body["pulled"] >= 1, body
        local_errors = body["errors"]["local"]
        assert local_errors, f"对面那份坏形状被静默吞了：{body}"
        assert any(str(e.get("kind")) == "card" for e in local_errors), local_errors


class _Resp:
    """`httpx.get/post` 的最小替身：只回 JSON，不带真连接。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return None
