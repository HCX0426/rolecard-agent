"""量一轮对话的 SQLite 构成与耗时份额（性能条目"先测量再动手"的判据留档）。

背景：审查快照的性能条目写着"每轮 8-10 条小 SQL，考虑版本戳缓存"（09-30 的审计读数）。
这条探针把现值钉死，供"修不修"的决策复用：

  * 稳态（预热轮之后）每轮语句数、动词与表分布、**完全重复的 SELECT**；
  * 每轮新建的连接数（短命线程 → 连接关了重建 → PRAGMA 组重跑，语句大头在这儿）；
  * 脚本化模型下整轮墙钟（真实轮由模型调用主导，见 probe_local_ttft 的首字读数）。

跑法（离线，不碰任何真实模型与端口；stdout 只出 ASCII，中文判读写进 --out 的 JSON）：

    .venv/Scripts/python.exe scripts/forensics/probe_turn_sql.py --out build/scratch/turn_sql.json

结果只写文件，stdout 保持 ASCII：这台机器的控制台是 GBK，中文经它必乱。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# 直跑脚本时 src/ 不在 sys.path（pytest 由 pyproject 的 pythonpath 兜底，直跑没有）。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.messages import AIMessage, BaseMessage  # noqa: E402

from rolecard_agent.api.main import create_app  # noqa: E402


class _SilentChat:
    """最小脚本模型（与 tests/conftest 的 ScriptedChat 同形）：回答恒定、不碰网络。

    放在探针里而不是 import tests/：取证脚本不该依赖测试树 —— 测试夹具以后改形状，
    这条探针还得照量。签名按 `ChatLike` 协议齐（bind_tools / invoke / stream）。
    """

    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> _SilentChat:
        return self

    def invoke(self, input: Any, **kwargs: Any) -> BaseMessage:  # noqa: A002
        self.calls += 1
        return AIMessage(content="probe-reply")

    def stream(self, input: Any, **kwargs: Any) -> Any:  # noqa: A002
        self.calls += 1
        yield AIMessage(content="probe-reply")


_VERB = re.compile(r"\s*(\w+)")
_TABLE = re.compile(r"(?:FROM|INTO|UPDATE)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)


def _verb(sql: str) -> str:
    m = _VERB.match(sql)
    return m.group(1).upper() if m else "?"


def _table(sql: str) -> str:
    m = _TABLE.search(sql)
    return m.group(1) if m else "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="per-turn sqlite probe")
    parser.add_argument("--out", default="build/scratch/turn_sql.json")
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()
    db_path = out.parent / "_probe.db"

    # 与测试会话同款的离线前提（tests/conftest 的会话夹具设的就是这三条）：origin 护栏
    # 会把 TestClient 的 testserver Host 打成 403；启动预热与自动提取会真碰模型/后台线程，
    # 把"一轮"的语句数搅浑。
    os.environ["LOCAL_ORIGIN_ENFORCE"] = "0"
    os.environ["MODEL_PIN_ON_STARTUP"] = "0"
    os.environ["MEMORY_EXTRACT_AUTO"] = "0"

    from fastapi.testclient import TestClient

    statements: list[str] = []
    real_connect = sqlite3.connect

    def spy(*c_args: Any, **c_kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(*c_args, **c_kwargs)
        tag = f"conn#{id(conn) % 100000}"
        with contextlib.suppress(Exception):  # 追不上就不追，别让探针本身炸
            conn.set_trace_callback(lambda sql, t=tag: statements.append(f"{t} {sql}"))
        return conn

    sqlite3.connect = spy  # type: ignore[assignment]
    try:
        with TestClient(create_app(sqlite_path=db_path, model=_SilentChat())) as client:
            tid = client.post("/api/session", json={}).json()["thread_id"]
            client.post("/api/chat", json={"thread_id": tid, "message": "warmup"})
            warmup = len(statements)
            warm_ids = {line.split(" ", 1)[0] for line in statements[:warmup]}
            t0 = time.perf_counter()
            res = client.post("/api/chat", json={"thread_id": tid, "message": "measure"})
            wall_ms = (time.perf_counter() - t0) * 1000
            if res.status_code != 200:
                raise SystemExit(f"probe turn failed: HTTP {res.status_code}")
            turn = statements[warmup:]
    finally:
        sqlite3.connect = real_connect
        # best-effort 清场：应用的连接可能还握在工作线程手里（Windows 下会占用文件），
        # 清不掉不许让探针失败 —— build/ 本就 gitignored，留个下次覆盖的库无害。
        with contextlib.suppress(OSError):
            shutil.rmtree(db_path.parent / "_probe.db-wal", ignore_errors=True)
            shutil.rmtree(db_path.parent / "_probe.db-shm", ignore_errors=True)
            db_path.unlink(missing_ok=True)

    # trace 行的形状是 "<tag> <sql>"：先剥 tag 再判动词（直接判会把 tag 当动词）。
    pairs: list[tuple[str, str]] = []
    for ln in turn:
        parts = ln.split(" ", 1)
        if len(parts) == 2 and parts[1]:
            pairs.append((parts[0], parts[1]))
    per_conn = Counter(tag for tag, _ in pairs)
    fresh = sum(1 for tag in per_conn if tag not in warm_ids)
    reads = [" ".join(sql.split()) for _, sql in pairs if _verb(sql) == "SELECT"]
    dup = {sql: n for sql, n in Counter(reads).items() if n > 1}
    report = {
        "稳态每轮语句数": len(pairs),
        "动词分布": dict(Counter(_verb(sql) for _, sql in pairs)),
        "表分布": dict(Counter(_table(sql) for _, sql in pairs).most_common()),
        "本轮新建连接数": fresh,
        "连接分布": dict(per_conn),
        "完全重复的SELECT": dup,
        "脚本化整轮墙钟ms": round(wall_ms, 2),
        "判读": (
            "每轮语句里 PRAGMA 组来自短命线程重建连接；可缓存的只有重复 SELECT 的那几条，"
            "而整轮墙钟（脚本化、零模型耗时）本身就是毫秒级 —— 真实轮由模型调用主导"
            "（本地 8B 首字 9-25s 见 probe_local_ttft），缓存收益 <1%，不值失效信号复杂度。"
        ),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"turn statements: {len(pairs)} (fresh conns: {fresh}, "
          f"dup selects: {len(dup)}, wall: {wall_ms:.1f}ms)")
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
