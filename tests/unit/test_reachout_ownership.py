"""M2c：收件箱与时间轴按归属走（架构总览 §4.1）。

五条判据：

  1. 两个主人的收件箱互不可见 —— 未读数、列表、按角色的角标都算；
  2. **`mark_all_read` / `clear_all_inboxes` 不越界** —— 这两条是"一键全清"，
     不隔离的话 A 点一下就把 B 的未读全标完、把 B 的抽屉全划掉；
  3. 按 id 的那一族有归属守卫（`id` 是自增整数，可枚举可猜）；
  4. 事件轴三条源（收件箱/记忆/会话）一起按主人过滤 —— 只补一条就等于没补；
  5. `role_proactive_state` **刻意没有** `user_id`：它按 `role_id` 主键，归属从角色卡继承；
     这条守卫是防止后来者"顺手补一列"造成两处真相。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rolecard_agent.base.identity import DEFAULT_USER_ID, ensure_identity_row
from rolecard_agent.core import timeline
from rolecard_agent.core.memory import GLOBAL_BUCKET, add_item
from rolecard_agent.core.reachout import (
    clear_all_inboxes,
    delete_reachout,
    list_reachouts,
    mark_all_read,
    mark_read,
    record_reachout,
)
from rolecard_agent.roles.models import RoleCard
from rolecard_agent.roles.service import RoleCardService
from rolecard_agent.storage.db import bootstrap, connect

OTHER = "someone-else"
ROLE = "elysia"


def _card(role_id: str = ROLE) -> RoleCard:
    return RoleCard(role_id=role_id, role_name="爱莉希雅", system_prompt="x")


@pytest.fixture
def db(tmp_path: Path):
    conn = connect(tmp_path / "app.db")
    bootstrap(conn, enabled_domains=("health",))
    # `bootstrap` 建表但不播种身份，而 `session_thread.user_id` 是指向外键的 ——
    # 先把默认那一份身份种下来（`ensure_identity_row` 就是装配根平时干的事），
    # 再补第二个租户/身份，测试里才真有两个主人可写。
    ensure_identity_row(conn)
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name)"
        " VALUES (?, 'local', '另一个')",
        (OTHER,),
    )
    # `session_thread.current_role_id` 也有外键：先建出这张卡。它归本机主人，
    # 另一个人的线程引用同一个 role_id 也合法（外键只查存在性）。
    RoleCardService(conn).scoped(DEFAULT_USER_ID).create(_card())
    conn.commit()
    yield conn
    conn.close()


def _seed_pair(db) -> None:
    """两个人各一条主动开口。"""
    record_reachout(db, _card(), "我今天想到一件事", user_id=DEFAULT_USER_ID)
    record_reachout(db, _card(), "对方那条不该被看见", user_id=OTHER)


def test_two_owners_see_only_their_own_inbox(db) -> None:
    _seed_pair(db)
    mine = list_reachouts(db, user_id=DEFAULT_USER_ID)
    theirs = list_reachouts(db, user_id=OTHER)
    assert [i["text"] for i in mine["items"]] == ["我今天想到一件事"]
    assert [i["text"] for i in theirs["items"]] == ["对方那条不该被看见"]
    assert mine["unread"] == 1 and theirs["unread"] == 1
    assert mine["unread_by_role"] == {"elysia": 1}


def test_read_all_and_clear_all_stay_inside_one_owner(db) -> None:
    """"一键全清"是最需要归属守卫的两条：不隔离就等于替别人做决定。"""
    _seed_pair(db)
    assert mark_all_read(db, user_id=DEFAULT_USER_ID) == 1
    after = list_reachouts(db, user_id=OTHER)
    assert after["unread"] == 1, "把别人的未读一起标完了"
    # 已读的行仍可被划掉（"读过了但不想在抽屉里再看见"是正常动作），所以这里该是 1 ——
    # 要紧的是它只清自己那一摞：对方那条既没被标读也没被划掉。
    assert clear_all_inboxes(db, user_id=DEFAULT_USER_ID) == 1
    assert list_reachouts(db, user_id=OTHER)["items"], "把别人的收件箱划空了"


def test_a_foreign_row_id_cannot_be_read_or_dismissed(db) -> None:
    _seed_pair(db)
    foreign = int(list_reachouts(db, user_id=OTHER)["items"][0]["id"])
    assert mark_read(db, foreign, user_id=DEFAULT_USER_ID) is False
    assert delete_reachout(db, foreign, user_id=DEFAULT_USER_ID) is False
    # 对方那条仍是未读：守卫是挡在写之前，不是写完再撤
    assert list_reachouts(db, user_id=OTHER)["unread"] == 1


def test_the_timeline_filters_all_three_sources_by_owner(db) -> None:
    """轴上三条源一起按主人过滤 —— 只补收件箱、留着记忆或会话，等于没补。"""
    _seed_pair(db)
    add_item(db, user_id=DEFAULT_USER_ID, bucket=GLOBAL_BUCKET, text="用户每周三练琴")
    db.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, title)"
        " VALUES ('s_mine', ?, 'elysia', '我这条线')",
        (DEFAULT_USER_ID,),
    )
    db.execute(
        "INSERT INTO session_thread (thread_id, user_id, current_role_id, title)"
        " VALUES ('s_theirs', ?, 'elysia', '别人这条线')",
        (OTHER,),
    )
    db.commit()

    kinds = [str(i["kind"]) for i in timeline.build(
        db, user_id=DEFAULT_USER_ID, role_id=ROLE
    )["items"]]
    assert "thread" in kinds and "reachout" in kinds
    texts = " ".join(
        str(i["text"]) for i in timeline.build(db, user_id=DEFAULT_USER_ID, role_id=ROLE)["items"]
    )
    assert "别人这条线" not in texts and "对方那条不该被看见" not in texts


def test_proactive_state_owner_lives_in_the_key_since_b2() -> None:
    """B2 之后归属写在**主键里**：`(user_id, role_id)`，不再"从角色卡继承"。

    这条断言的前身是 `test_proactive_state_deliberately_has_no_owner_column`——那句
    "真要同一库里住多个主人，得先把主键改成 (user_id, role_id)"写在这条 docstring 里、
    等着的就是 B2 这一天。翻过来钉新形状：列在、而且进了主键。
    """
    conn = connect(":memory:")
    bootstrap(conn, enabled_domains=())
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(role_proactive_state)")}
    assert "user_id" in cols and "role_id" in cols
    pk = [str(r[1]) for r in conn.execute("PRAGMA table_info(role_proactive_state)") if r[5] > 0]
    assert pk == ["user_id", "role_id"], f"主键 (user_id, role_id) 没落位：{pk}"
    conn.close()
