"""Session persistence. SqliteSaver keyed by thread_id.

Note: InMemorySaver (formerly MemorySaver) is dev-only - state is lost on restart.

`SqliteSaver.setup()` MUST be called before the first write; it is what creates the
checkpoint tables. Forgetting it produces a confusing "no such table" error on the first
conversation rather than at startup, which is why the factory here always calls it.
"""

from __future__ import annotations

import sqlite3
from typing import cast

from langgraph.checkpoint.sqlite import SqliteSaver

from rolecard_agent.storage.db import SqlConnection


def make_checkpointer(conn: SqlConnection) -> SqliteSaver:
    """Wrap an existing connection.

    Takes a connection rather than a path so the caller controls when it is opened and
    closed - the same connection also backs the kernel tables, and SQLite cannot have two
    writers fighting over one file.
    """
    # `SqliteSaver` 的签名要求真实 `sqlite3.Connection`，而应用传进来的是
    # `SqlConnection`（可能是 `ThreadLocalConnection`，它转发同一个方法子集）。
    # 运行期成立、类型系统表达不了 —— 显式 cast 并留下理由，而不是把签名放宽成 Any
    # 让整个模块失去检查。
    saver = SqliteSaver(cast("sqlite3.Connection", conn))
    saver.setup()
    return saver
