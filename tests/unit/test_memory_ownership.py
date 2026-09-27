"""M2b：记忆按归属读写（架构总览 §4.1）。

四条判据，各自对应一种坏法：

  1. **两个主人的记忆桶互不可见** —— 包括注入侧（`memory_for_turn`）：一条不属于他的事实
     进不了 prompt，否则她说的话会带着别人的事实；
  2. **按 id 改的那一族有归属守卫** —— `id` 是本机自增整数，可枚举可猜；
  3. **每条事实带一个跨机器稳定的 `uid`**，且老库升上来时补齐（上行三条语义的前提，
     见 §4.1；没有它，两台上长出的相同 id 会指不同条目）；
  4. **`memory_save` 工具写进的是这一轮的主人**，而不是这台实例的主人 —— 工具签名里
     没有 user_id（有它模型就能自己填"我是谁"），所以靠 `bound_user` 绑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rolecard_agent.config import Settings
from rolecard_agent.core.identity import DEFAULT_USER_ID, bound_user
from rolecard_agent.core.memory import (
    GLOBAL_BUCKET,
    add_item,
    delete_item,
    edit_item,
    get_item,
    list_items,
    make_memory_tool,
    memory_for_turn,
    render_memory,
)
from rolecard_agent.storage.db import bootstrap, connect

OLD_ITEM_TABLE = """
CREATE TABLE role_memory_item (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id        TEXT NOT NULL,
    text           TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'manual',
    pinned         INTEGER NOT NULL DEFAULT 0,
    hit_count      INTEGER NOT NULL DEFAULT 0,
    last_hit_at    TIMESTAMP,
    invalidated_at TIMESTAMP,
    superseded_by  INTEGER,
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


@pytest.fixture
def db(tmp_path: Path):
    conn = connect(tmp_path / "app.db")
    bootstrap(conn, enabled_domains=("health",))
    yield conn
    conn.close()


def test_two_owners_do_not_see_each_others_memories(db) -> None:
    mine = DEFAULT_USER_ID
    add_item(db, user_id=mine, bucket=GLOBAL_BUCKET, text="用户青霉素过敏")
    add_item(db, user_id="someone-else", bucket=GLOBAL_BUCKET, text="对方住在新加坡")

    assert [i["text"] for i in list_items(db, user_id=mine, bucket=GLOBAL_BUCKET)] == [
        "用户青霉素过敏"
    ]
    # 注入侧同源：面板读不到的东西，prompt 里也不该出现
    text, _ids = render_memory(db, user_id=mine, bucket=GLOBAL_BUCKET)
    assert "新加坡" not in text
    turned = memory_for_turn(db, Settings(memory_enabled=True), None, user_id=mine)
    assert "新加坡" not in turned and "青霉素" in turned
    # 全局桶（"关于用户自己"那个空 role_id 的桶）也是每人一份
    assert list_items(db, user_id="someone-else", bucket=GLOBAL_BUCKET)[0]["text"] == (
        "对方住在新加坡"
    )


def test_a_foreign_item_id_reads_as_missing_and_cannot_be_edited(db) -> None:
    """`id` 是自增整数：光"猜到一个数"就能读改别人的事实，那是最难看出的洞。"""
    theirs = add_item(db, user_id="someone-else", bucket=GLOBAL_BUCKET, text="对方的事实")
    assert theirs is not None
    foreign = int(theirs["id"])

    assert get_item(db, foreign, user_id=DEFAULT_USER_ID) is None
    assert edit_item(db, user_id=DEFAULT_USER_ID, item_id=foreign, text="改掉它") is None
    assert delete_item(db, user_id=DEFAULT_USER_ID, item_id=foreign) is False
    # 原样还在，且没被改名（守卫挡在写之前，不是"写了再回滚"）
    back = get_item(db, foreign, user_id="someone-else")
    assert back is not None and back["text"] == "对方的事实"


def test_every_item_carries_a_stable_cross_machine_uid(db) -> None:
    a = add_item(db, user_id=DEFAULT_USER_ID, bucket=GLOBAL_BUCKET, text="用户住在上海")
    b = add_item(db, user_id=DEFAULT_USER_ID, bucket=GLOBAL_BUCKET, text="用户养了一只猫")
    assert a is not None and b is not None
    assert a["uid"] and b["uid"] and a["uid"] != b["uid"]
    # 重新读回来还是同一个：上行时它就是"同一条"的依据
    again = get_item(db, int(a["id"]), user_id=DEFAULT_USER_ID)
    assert again is not None and again["uid"] == a["uid"]
    # 重复写同一句话不新建条目（`add_item` 的字面判重），uid 也就不该换
    dup = add_item(db, user_id=DEFAULT_USER_ID, bucket=GLOBAL_BUCKET, text="用户住在上海")
    assert dup is not None and dup["id"] == a["id"] and dup["uid"] == a["uid"]


def test_an_old_database_gets_uid_backfilled_once(tmp_path: Path) -> None:
    """没有归属那一版的库升上来：列长出来、老行补上 uid，**再跑一次不改变它们**。

    为什么幂等要单独钉：这条补值走 `_migrate`，而它在每次启动都跑；不幂等的话
    同一条事实每次重启换一个 uid —— 那正是上行去重最不能有的行为。
    """
    db = tmp_path / "legacy.db"
    conn = connect(db)
    conn.executescript(OLD_ITEM_TABLE)
    conn.execute("INSERT INTO role_memory_item (role_id, text) VALUES ('', '老库里的事实')")
    conn.commit()
    conn.close()

    upgraded = connect(db)
    bootstrap(upgraded, enabled_domains=("health",))
    rows = list_items(upgraded, user_id=DEFAULT_USER_ID, bucket=GLOBAL_BUCKET)
    assert [r["text"] for r in rows] == ["老库里的事实"]
    first = rows[0]["uid"]
    assert first, "老行没被补上 uid，上行时它就永远对不上账"

    bootstrap(upgraded, enabled_domains=("health",))  # 再启动一次
    assert list_items(upgraded, user_id=DEFAULT_USER_ID, bucket=GLOBAL_BUCKET)[0]["uid"] == first
    upgraded.close()


def test_the_memory_tool_writes_for_whomever_this_turn_belongs_to(db) -> None:
    """`memory_save` 的归属来自这一轮的绑定，不来自实例默认。

    这条同时是 M3 后半的验收：工具签名里没有 `user_id`（有它模型就能自己填"我是谁"），
    所以主人只能由节点入口绑进来（`core/identity.bound_user`）。
    """
    tool = make_memory_tool(settings=Settings(memory_enabled=True), conn=db)
    with bound_user("u1"):
        tool.invoke({"fact": "用户下周要体检"})
    assert [i["text"] for i in list_items(db, user_id="u1", bucket=GLOBAL_BUCKET)] == [
        "用户下周要体检"
    ]
    # 实例主人那份里不该有它
    assert list_items(db, user_id=DEFAULT_USER_ID, bucket=GLOBAL_BUCKET) == []
