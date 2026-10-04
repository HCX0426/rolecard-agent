"""M2a：`role_card` 按身份读写（架构总览 §4.1）。

三条判据，各自挡一种坏法：

  1. **两个主人各看各的卡** —— 归属过滤不是"传了个参数"，是真的看不见（读、改、删都算）；
  2. **schema 里的默认值与 `DEFAULT_USER_ID` 不许漂** —— 那个字面量是**老库升上来时**
     `ADD COLUMN ... DEFAULT 'local-user'` 用来回填的值。它一旦和常量漂开，老库里的卡
     就会全体变成"没人看得见"，而症状是"角色列表空了"，没人会往默认值上想；
  3. **老库（没有 `user_id` 那一版）升上来之后，原有那些卡对本机主人仍然可见** ——
     零迁移那条路（`storage.db.reconcile_columns`）在真数据上的验收。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from rolecard_agent.base.identity import DEFAULT_USER_ID
from rolecard_agent.roles.models import RoleCardCreate, RoleCardUpdate
from rolecard_agent.roles.service import RoleCards, RoleNotFound
from rolecard_agent.storage.db import connect, reconcile_columns

REPO = Path(__file__).resolve().parents[2]
ROLE_SCHEMA = REPO / "src" / "rolecard_agent" / "roles" / "schema.sql"


def _card(role_id: str) -> RoleCardCreate:
    return RoleCardCreate(role_id=role_id, role_name=role_id, system_prompt="x")


@pytest.fixture
def store(tmp_path: Path):
    from rolecard_agent.roles.service import RoleCardService

    conn = connect(tmp_path / "app.db")
    from rolecard_agent.storage.db import bootstrap

    bootstrap(conn, enabled_domains=("health",))
    yield RoleCardService(conn)
    conn.close()


def test_two_owners_do_not_see_each_others_cards(store) -> None:
    mine = store.scoped(DEFAULT_USER_ID)
    theirs = store.scoped("someone-else")

    mine.create(_card("my_girl"))
    theirs.create(_card("her_girl"))

    assert [r.role_id for r in mine.list_roles()] == ["my_girl"]
    assert [r.role_id for r in theirs.list_roles()] == ["her_girl"]
    # 看不见 = 读、改、删都当作不存在（不是"能读到但不许改"）
    with pytest.raises(RoleNotFound):
        mine.get("her_girl")
    with pytest.raises(RoleNotFound):
        mine.delete("her_girl")
    with pytest.raises(RoleNotFound):
        mine.update("her_girl", RoleCardUpdate(role_name="改名"))
    # 改名的守卫在读取之前：不是他的那张卡，连"改成了什么"都探不出来
    assert theirs.get("her_girl").role_name == "her_girl"


def test_a_new_card_always_carries_the_views_owner(store) -> None:
    """归属**不能从入参进来**：`RoleCardCreate` 里没有 `user_id` 这个字段。

    如果哪天有人给创建模型加了一列 `user_id`，这条会红 —— 那等于允许调用方指定
    "这张卡是谁的"，是这条链最坏的一种洞。
    """
    assert "user_id" not in RoleCardCreate.model_fields
    card = store.scoped("alice").create(_card("alice_only"))
    assert card.user_id == "alice"


def test_every_schema_user_id_default_matches_the_code_constant() -> None:
    """所有 schema 里 `user_id` 那个字面量默认值都必须仍是 `DEFAULT_USER_ID`。

    为什么扫全部而不是只看一张表：这些字面量是**老库升上来时用来回填**的值 —— SQLite 不允许
    "带非常量默认的 ADD COLUMN"再挂外键，所以只能写字面量。它一旦与常量漂开，老库升完级那些行
    就变成“没人看得见”，而症状是“列表空了” —— 最不像默认值出问题的那一类。
    """
    found = 0
    for sql in sorted(Path("src/rolecard_agent").rglob("*.sql")):
        text = sql.read_text(encoding="utf-8")
        for m in re.finditer(r"user_id\s+TEXT NOT NULL DEFAULT '([^']*)'", text):
            found += 1
            assert m.group(1) == DEFAULT_USER_ID, (
                f"{sql.name} 的 user_id 默认值与 base.identity 漂开了：{m.group(1)!r}"
            )
    assert found >= 2, f"只扫到 {found} 处 user_id 默认值，这条守卫该跟着形状走"


def test_an_old_database_gets_the_column_and_keeps_its_cards(tmp_path: Path) -> None:
    """没有归属那一版的库升上来：列长出默认值，旧行**对本机主人可见**。

    这是"零迁移"这条路在角色卡上的真验收 —— 补列器只保证形状，**看得见**才是用户要的。
    """
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE role_card ("
        " role_id TEXT PRIMARY KEY, role_name TEXT NOT NULL, system_prompt TEXT NOT NULL,"
        " temperature REAL NOT NULL DEFAULT 0.7, is_builtin INTEGER NOT NULL DEFAULT 0,"
        " reachout_keep INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP,"
        " updated_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO role_card (role_id, role_name, system_prompt) VALUES ('wan', '苏晚晴', 'x')"
    )
    conn.commit()
    conn.close()

    upgraded = connect(legacy)
    added = reconcile_columns(
        upgraded, files=[ROLE_SCHEMA, REPO / "src" / "rolecard_agent" / "core" / "schema.sql"]
    )
    upgraded.commit()
    assert "role_card.user_id" in added

    cards = RoleCards(upgraded, DEFAULT_USER_ID)
    assert [r.role_id for r in cards.list_roles()] == ["wan"]
    assert cards.get("wan").user_id == DEFAULT_USER_ID
    upgraded.close()


def test_bound_user_is_scoped_to_one_turn() -> None:
    """绑过就是这一轮的主人，出了作用域必须复位 —— 异常路径也要复。

    不复位的后果不是"读错一个人"这么轻：同一个工作线程被下一轮复用，那一轮就顶着
    上一轮的主人跑，而表现是"偶尔串数据"，最难查的那一类。
    """
    from rolecard_agent.base.identity import active_user_id, bound_user

    assert active_user_id("owner") == "owner"  # 没绑过 = 这台实例的主人
    with bound_user("u1"):
        assert active_user_id("owner") == "u1"
        with bound_user("u2"):  # noqa: SIM117 - 嵌套本身就是要测的事
            assert active_user_id("owner") == "u2"
        assert active_user_id("owner") == "u1"
    assert active_user_id("owner") == "owner"

    with pytest.raises(ZeroDivisionError), bound_user("u1"):
        raise ZeroDivisionError
    assert active_user_id("owner") == "owner"


def test_binding_an_empty_owner_is_the_same_as_binding_nothing() -> None:
    from rolecard_agent.base.identity import active_user_id, bound_user

    with bound_user(""):
        assert active_user_id("owner") == "owner"


def test_fallback_sentinel_catches_quiet_fallbacks_and_stays_silent_when_bound() -> None:
    """「没绑过→静默回落实例主人」这条**能被抓到**（快照"身份显式化"那格的哨兵）。

    这一族缺陷最怕的不是回落本身（后台调度器替实例主人冒话时回落是**设计如此**，
    交接第四节量过 18 次触发全是这一类或测试自证），而是**该绑的没绑**：请求期漏了
    图入口的绑定，症状是"偶尔读到别人的记忆/花别人的 key"，而它不报错、只悄悄换个人。
    哨兵把"这条同步路径有没有回落"变成能断言的事实：绑齐了零记录，漏绑了记录到回落的那个
    fallback 值 —— 于是新加一条读路径时可以要求它"在这个块里跑、fell 必须为空"。
    """
    from rolecard_agent.base.identity import (
        active_user_id,
        bound_user,
        capturing_identity_fallback,
    )

    # 全程绑定了主人：哨兵一次都不响。
    with capturing_identity_fallback() as fell, bound_user("alice"):
        assert active_user_id("local-user") == "alice"
    assert fell == []

    # 没绑：回落到 fallback，且**回落到了谁**被如实记下来。
    with capturing_identity_fallback() as fell:
        assert active_user_id("local-user") == "local-user"
    assert fell == ["local-user"]

    # 出了作用域钩子必须复位：下一条路径的回落不该记进上一条的收集器。
    with capturing_identity_fallback() as second:
        assert second == []
    active_user_id("local-user")  # 没有活动收集器，也不许炸
