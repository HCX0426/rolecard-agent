"""删会话**先落备份**（2026-10-04 快照「会话删除（单删/清空/修剪）无先备份」那一格）。

三条删会话路径原本只有两条接了备份：整份替换有 `dump_before_clear`、retention 有
`dump_before_delete` —— 而"用户在界面上删掉一段对话"这个**最常见、最不可逆**的动作反倒
什么都没有。这里钉的是补上的那一半，四条判据各自对应一种"做错了会很安静"的形状：

  * 删了就得有地方能读回来（备份内容逐列在，且**在 DELETE 之前**已经落盘）；
  * 0 行不落空文件（否则备份目录会被"什么都没删"的删除刷满）；
  * `backup_dir=None` 真的不备份（sync 那条自己备过，重复落 = 两个事实源）；
  * 给了目录却不给时刻 → 当场报错（同一次删除的几个备份文件必须共用一个后缀）。

时刻的判据还有一条藏在顺序里：备份写失败就必须**不删**（宁可删不掉，不可删了找不回）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rolecard_agent.base.identity import DEFAULT_TENANT_ID, DEFAULT_USER_ID
from rolecard_agent.storage import threads
from rolecard_agent.storage.db import bootstrap, connect
from rolecard_agent.storage.threads import (
    create_thread,
    delete_thread_everywhere,
)


@pytest.fixture
def env(tmp_path: Path):
    c = connect(tmp_path / "app.db")
    bootstrap(c, enabled_domains=("health",))
    c.execute(
        "INSERT OR IGNORE INTO tenant (tenant_id, display_name) VALUES (?, '本地演示')",
        (DEFAULT_TENANT_ID,),
    )
    c.execute(
        "INSERT OR IGNORE INTO app_user (user_id, tenant_id, display_name) VALUES (?, ?, ?)",
        (DEFAULT_USER_ID, DEFAULT_TENANT_ID, "本地用户"),
    )
    c.commit()
    try:
        yield c, tmp_path / "backups"
    finally:
        c.close()


def _seed(c, tid: str, *, title: str = "第一次聊的那个话题") -> None:
    create_thread(c, thread_id=tid, user_id=DEFAULT_USER_ID, role_id="girl", tool_epoch=3)
    c.execute("UPDATE session_thread SET title = ? WHERE thread_id = ?", (title, tid))
    c.commit()


def test_删之前先把行逐列落盘并能在删后读回(env) -> None:
    c, backup = env
    _seed(c, "s_keep", title="能被找回的那一句")
    stats = delete_thread_everywhere(c, "s_keep", backup_dir=backup, stamp="20261007-000000")

    assert stats["session_thread"] == 1, stats  # 真的删了
    assert c.execute("SELECT 1 FROM session_thread WHERE thread_id='s_keep'").fetchone() is None

    written = {p.name for p in backup.glob("*.jsonl")}
    assert "session_thread-20261007-000000.jsonl" in written, written
    row = json.loads(
        (backup / "session_thread-20261007-000000.jsonl").read_text(encoding="utf-8")
    )
    assert row["thread_id"] == "s_keep"
    assert row["title"] == "能被找回的那一句", "逐列原样，不许只存 id"
    # 「逐列」判成一个与列名无关的断言：备份里的键集合必须**等于**表上的列集合，
    # 少一列（或只存个 id）都算丢数据 —— 写死列名反而会跟着 schema 漂移而静默变弱。
    cols = {r[1] for r in c.execute("PRAGMA table_info(session_thread)").fetchall()}
    assert set(row) == cols, set(row) ^ cols


def test_日志说得清是谁触发的备份(env, capsys) -> None:
    """备份那一声必须报对来源：`[retention]` 与 `[session-delete]` 是两件不同的事。

    同一条机械从前只会念 "retention"，而删会话现在也走它 —— 排障的人看到"retention 删了
    这些行"会去查保留策略，而真相是某人按了删除键。反过来 retention 自己的前缀也不许被
    这次改动带跑（它的默认值仍是 `retention`，那三处调用点一个字没动）。
    """
    c, backup = env
    _seed(c, "s_log")
    delete_thread_everywhere(c, "s_log", backup_dir=backup, stamp="LOG1")
    err = capsys.readouterr().err
    assert "[session-delete]" in err, err
    assert "[retention]" not in err, err


def test_备份名单覆盖被删的每一张表(env, monkeypatch) -> None:
    """载体表**逐张**都要落，漏一张 = 那类数据静默消失。

    判据写成"备份过的表集合 == 真正删过的表集合"，不写死表名、也不需要往载体表里塞行
    （那些表各有 NOT NULL 形状，第一版就是试图塞行结果一张都塞不进去 —— 那条空转守卫
    当场把它报出来了）。名单现数现用，将来新增载体表自动进这条断言。
    """
    c, backup = env
    _seed(c, "s_multi")
    dumped: list[str] = []
    real = threads.dump_before_delete

    def spy(conn, **kw: object) -> int:
        dumped.append(str(kw["table"]))
        return real(conn, **kw)

    monkeypatch.setattr(threads, "dump_before_delete", spy)
    stats = delete_thread_everywhere(c, "s_multi", backup_dir=backup, stamp="S1")
    assert set(dumped) == set(stats), (dumped, stats)
    # 顺序也判：每张表都是"先备份、后删"，所以备份名单里必有它
    assert "session_thread" in dumped


def test_备份写不进去就一条都不删(env) -> None:
    """**宁可删不掉，不可删了找不回**：备份失败必须把删除挡在前面。

    这一条是"先落盘再删"那个顺序的全部意义 —— 反过来的话，崩溃的后果是"行没了 + 没有
    任何地方能找回"，而那正是这一格要修的缺陷本身。
    """
    c, backup = env
    _seed(c, "s_blocked")
    backup.mkdir(parents=True, exist_ok=True)
    # 用一个目录占住要写的文件名，write_text 必炸（跨平台都比"改权限"可靠）
    (backup / "session_thread-BAD.jsonl").mkdir()
    with pytest.raises(OSError):
        delete_thread_everywhere(c, "s_blocked", backup_dir=backup, stamp="BAD")
    held = c.execute("SELECT 1 FROM session_thread WHERE thread_id='s_blocked'").fetchone()
    assert held is not None, "备份失败却把行删了"


def test_零行不落空文件(env) -> None:
    """删一条不存在的会话不该在备份目录里刷出一个空文件（备份目录也是要被读的）。"""
    c, backup = env
    stats = delete_thread_everywhere(c, "s_ghost", backup_dir=backup, stamp="S0")
    assert stats["session_thread"] == 0
    if backup.exists():
        assert list(backup.glob("*.jsonl")) == []


def test_不给目录就绝不备份(env) -> None:
    """sync 那条路径自己先 `dump_before_clear` 过；这里再落一遍就是两个事实源。"""
    c, backup = env
    _seed(c, "s_sync")
    delete_thread_everywhere(c, "s_sync")  # backup_dir 默认 None
    assert not backup.exists()
    assert c.execute("SELECT 1 FROM session_thread WHERE thread_id='s_sync'").fetchone() is None


def test_给了目录没给时刻当场报错(env) -> None:
    """时刻由调用方**一次算好**传下来：各表自己取 now 会写出四个不同文件名，
    还原时得靠猜哪几个是同一次删除 —— 那正是"备份存在但拼不回来"的形状。"""
    c, backup = env
    _seed(c, "s_stamp")
    with pytest.raises(ValueError, match="stamp"):
        delete_thread_everywhere(c, "s_stamp", backup_dir=backup)
    # 报错发生在删之前：行还在
    row = c.execute("SELECT 1 FROM session_thread WHERE thread_id='s_stamp'").fetchone()
    assert row is not None


def test_备份超过五份会自动轮转(env) -> None:
    """只增不减的备份目录 = 拿一个新问题换掉刚修好的那个问题。"""
    c, backup = env
    for i in range(7):
        tid = f"s_rot{i}"
        _seed(c, tid)
        delete_thread_everywhere(c, tid, backup_dir=backup, stamp=f"ST{i}")
    kept = sorted(p.name for p in backup.glob("session_thread-*.jsonl"))
    assert len(kept) == 5, kept  # RETENTION_BACKUP_KEEP
    assert kept[-1].endswith("ST6.jsonl"), kept
