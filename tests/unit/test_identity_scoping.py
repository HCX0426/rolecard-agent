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

from rolecard_agent.core.identity import DEFAULT_USER_ID
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


def test_schema_default_matches_the_code_constant() -> None:
    """`roles/schema.sql` 里那句 `DEFAULT 'local-user'` 必须仍是 `DEFAULT_USER_ID`。

    为什么单独钉一条：SQLite 的 `ADD COLUMN` 对老库不允许带外键（见 schema 注释），所以这里
    只能写字面量 —— 一个字面量与一个常量各说一套，就是两份事实面。漂开之后的症状是
    "老库升完级角色列表空了"，最不像默认值出问题。
    """
    sql = ROLE_SCHEMA.read_text(encoding="utf-8")
    match = re.search(r"user_id\s+TEXT NOT NULL DEFAULT '([^']*)'", sql)
    assert match is not None, "role_card 的 user_id 声明变了形状，这条守卫要一起改"
    assert match.group(1) == DEFAULT_USER_ID


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
