"""Session persistence. SqliteSaver keyed by thread_id.

Note: InMemorySaver (formerly MemorySaver) is dev-only - state is lost on restart.

`SqliteSaver.setup()` MUST be called before the first write; it is what creates the
checkpoint tables. Forgetting it produces a confusing "no such table" error on the first
conversation rather than at startup, which is why the factory here always calls it.
"""

from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver


def make_checkpointer(conn: sqlite3.Connection) -> SqliteSaver:
    """Wrap an existing connection.

    Takes a connection rather than a path so the caller controls when it is opened and
    closed - the same connection also backs the kernel tables, and SQLite cannot have two
    writers fighting over one file.
    """
    saver = SqliteSaver(conn)
    saver.setup()
    return saver
