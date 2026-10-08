"""消息时间戳的唯一出口（"双时区体系"那一格的收口，2026-10-07）。

**政策：新写入一律 UTC**。先量出的现场：库表列全是 SQLite `CURRENT_TIMESTAMP`
（UTC），而消息的 `created_at`（住 checkpoint 的 `additional_kwargs`，经
`api/message_view.py` 的 `ts` 出 API）是本地 naive 串。两族并存时谁也拦不住谁：
后端今天没有跨族比较（提取游标是序号，不比时间），但任何一处"拿消息时间去比
库表时间"的新代码都会**静默算错** —— 本地 naive 与 UTC 差着时区偏移，比较不报
错、结果错；消息随 checkpoint 跨机同步后，本地串被新机器当新机器的本地解释，
钟面漂一个时区。

**纪元怎么认：格式即纪元**。本模块写出的是 UTC ISO-8601（`2026-10-07T02:30:00Z`，
带 T 带 Z）；纪元前的存量串是本地 naive（`2026-10-07 10:30:00`，空格分隔、无时区
—— 写它时的 docstring 理由是"自用单时区"，checkpoint 跨机与公网部署把它变成了
lie）。消费者按形状分族解析（`parse_message_ts`），**不依赖任何存储的"纪元标记"**
—— 没有读取者的标记是 dead config，本仓不造；格式本身就是那个标记。

边界（量过才写）：`features/reachout`、`core/memory` 等读的 `created_at` 是
**库表列**（DB UTC、空格形状，SQLite `CURRENT_TIMESTAMP` 给的），不走本模块 ——
它们的钟面换算出口在前端 `lib/quiet.ts::formatUtcNaive`（`R102-18`）。
本模块只管消息这一族。
"""

from __future__ import annotations

from datetime import UTC, datetime

#: 纪元后的形状：UTC ISO-8601，秒级（与旧格式同精度）。带 T 带 Z —— 形状本身就是
#: "这是 UTC"的标记，前端 `new Date(...)` 与本模块的解析都认它。
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> str:
    """现在，UTC，ISO Z 形状（秒级）。消息时间戳只从这里出。"""
    return datetime.now(UTC).strftime(TS_FORMAT)


def parse_message_ts(ts: str) -> datetime:
    """消息时间串 → **带时区**的 datetime（跨族可比、可减）。

    两族各按各自的真实语义解，跨族差值/比较因此正确：
      * 纪元后（带 T）：UTC —— 无 Z 的 T 串按政策也当 UTC（本模块不产它，
        容忍它是为了别把"格式差半格"读成另一个时区）；
      * 纪元前（空格分隔）：**本地** naive —— 那是旧写法的真实语义，
        `.astimezone()` 按本机时区补齐。

    解析不了抛 `ValueError` —— 调用方决定怎么降级，这里不替它装作成功。
    """
    if "T" in ts:
        parsed = datetime.fromisoformat(ts)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").astimezone()
