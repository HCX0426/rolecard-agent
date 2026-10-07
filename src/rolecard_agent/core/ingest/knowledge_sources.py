"""知识来源投影表（`R102-55` 根治）的读写口。

为什么要有这一层：设置页的「知识库概览」要知道"每个作用域里有哪些来源"，而这个问题的
权威数据此前只能靠**全量倒灌 chroma 的分块元数据**得到（5k 分块 66.5ms/次，线性于分块
数，页面每开一次都付）。来源清单只在分块增删时变化，于是把它落成一张投影表
（`core/schema.sql` 的 `knowledge_source`）：

  * **写**：索引层仅有的三个入口维护 —— `KnowledgeBase.index()` 记住 /
    `delete_source()` 忘掉 / `reset_scope()` 清空（经 Protocol 注入，见 rag/retriever.py；
    依赖方向保持"装配层把具体物递给服务"，`rag/` 不认识 storage）；
  * **读**：`names()` 给概览接口 —— O(来源数)，与分块数无关；
  * **存量**：bootstrap 的一次性种子从既有分块元数据回填（`heal_knowledge_sources`），
    旁路写入（如安装根的 `elysia_lore`，它根本不走上传链）也在那一次补齐。

删除按**索引身份**（source_key）精确定位；展示名不参与身份 —— 与
`KnowledgeBase.index()` 的 source/source_name 区分是同一条纪律。多用户共享一张名单
（chroma 集合本身就是共享的），刻意**不加 user_id**。
"""

from __future__ import annotations

from rolecard_agent.storage.db import SqlConnection


class KnowledgeSourceStore:
    def __init__(self, conn: SqlConnection) -> None:
        self._conn = conn

    def names(self, scope: str) -> list[str]:
        """该作用域的来源展示名（去重排序）。同一份来源以两个身份并存时只显示一次。"""
        rows = self._conn.execute(
            "SELECT DISTINCT name FROM knowledge_source WHERE scope = ? ORDER BY name",
            (scope,),
        ).fetchall()
        return [str(r["name"]) for r in rows]

    def remember(self, scope: str, source_key: str, name: str) -> None:
        """记下/更新一个来源。同身份重传 = 改名，同一行就地更新（不产生孤儿）。"""
        self._conn.execute(
            "INSERT INTO knowledge_source (scope, source_key, name) VALUES (?, ?, ?) "
            "ON CONFLICT(scope, source_key) DO UPDATE SET name = excluded.name",
            (scope, source_key, name),
        )
        self._conn.commit()

    def forget(self, scope: str, source_key: str) -> None:
        """忘掉一个来源（delete_source 的投影半边）。删到 0 行也照常提交：
        0 行的 DELETE 同样开了写事务，不能让锁留给下一个请求（`R102-42`）。"""
        self._conn.execute(
            "DELETE FROM knowledge_source WHERE scope = ? AND source_key = ?",
            (scope, source_key),
        )
        self._conn.commit()

    def forget_scope(self, scope: str) -> None:
        """清掉整个作用域的投影（reset_scope / 种子发现空集合时用）。"""
        self._conn.execute("DELETE FROM knowledge_source WHERE scope = ?", (scope,))
        self._conn.commit()