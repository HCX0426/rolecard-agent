"""请求体上限（2026-10-02 轮 `R102-45` 家族的收尾项；10-03 拍板：64 MiB + 413）。

量到的事实是这条的入场券：真后端、真 socket，20MB 请求体 → 工作集 +40.7MB、进程峰值
+60.7MB（≈3× 线性），而传输层原本**没有任何上限**。所以判据盯两道拦法，缺一不算闭：

  1. 带 `Content-Length` 的：一个字节都不读就 413；
  2. 分块 / 不带长度的：在 body 流进来的路上计数，越过上限当场 413，并**丢掉内层那半截
     响应**（不然它会带着已经作废的 200 继续往外写）。

`limit=0` = 不设上限（排障用的一键回旧行为），那条也必须有用例 —— 关掉开关把全部请求
拦掉是另一种坏。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from rolecard_agent.api.body_cap import BodyCapMiddleware, length_too_large
from rolecard_agent.api.main import create_app


def _drive(
    limit: int, chunks: list[bytes], *, headers: list[tuple[bytes, bytes]] | None = None
) -> tuple[list[dict[str, Any]], list[bytes]]:
    """直接把中间件套在一个假内层应用上，收下发给外层的消息与内层读到的 body。"""
    sent: list[dict[str, Any]] = []
    inner_bodies: list[bytes] = []
    queue = list(chunks)

    async def receive() -> dict[str, Any]:
        if queue:
            data = queue.pop(0)
            return {"type": "http.request", "body": data, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def inner(scope: Any, recv: Any, snd: Any) -> None:
        body = b""
        while True:
            message = await recv()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        inner_bodies.append(body)
        await snd({"type": "http.response.start", "status": 200, "headers": []})
        await snd({"type": "http.response.body", "body": b"ok"})

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/anything",
        "raw_path": b"/api/anything",
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(BodyCapMiddleware(inner, limit=limit)(scope, receive, send))
    return sent, inner_bodies


# ---------------------------------------------------------------- 判据本身


def test_length_too_large_table() -> None:
    assert length_too_large("11", 10)
    assert not length_too_large("10", 10)  # 等于上限放行：限的是"超过"
    assert not length_too_large(None, 10)  # 没带长度 → 交给流式那一道
    assert not length_too_large("abc", 10)  # 非法值不在这儿误杀
    assert not length_too_large("999999", 0)  # 开关关了就不判


# ----------------------------------------------------- 第 1 道：Content-Length


def test_declared_length_rejects_before_any_byte_is_read() -> None:
    sent, inner_bodies = _drive(10, [b"x" * 20], headers=[(b"content-length", b"20")])
    assert sent[0]["status"] == 413
    assert inner_bodies == [], "超限请求不该走到内层（认证/路由一段都不该碰上它）"


# ------------------------------------------------------- 第 2 道：分块 body


def test_chunked_body_is_counted_on_the_way_in() -> None:
    sent, inner_bodies = _drive(10, [b"x" * 6, b"y" * 6, b"z" * 6])
    assert sent[0]["status"] == 413
    # 内层那半截 200 必须被丢掉：屏幕上只能有一发响应，而它是 413。
    assert [m for m in sent if m["type"] == "http.response.start"] == [sent[0]]
    assert all(m.get("body") != b"ok" for m in sent)
    assert inner_bodies and len(inner_bodies[0]) <= 12, "越过上限之后还要继续喂 body 就是没拦"


def test_under_limit_reaches_the_handler_intact() -> None:
    sent, inner_bodies = _drive(100, [b"abc", b"def"])
    assert sent[0]["status"] == 200
    assert inner_bodies == [b"abcdef"]


def test_limit_zero_means_no_cap_at_all() -> None:
    sent, inner_bodies = _drive(0, [b"x" * 5000])
    assert sent[0]["status"] == 200
    assert inner_bodies == [b"x" * 5000]


# --------------------------------------------------------------- 接线（端到端）


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """64 字节的上限：真 HTTP 栈、真应用装配，只把阈值调小，好让测试能真的越过去。"""
    monkeypatch.setenv("MAX_BODY_BYTES", "64")
    app = create_app(sqlite_path=tmp_path / "app.db")
    with TestClient(app, client=("127.0.0.1", 50000), headers={"host": "testserver"}) as c:
        yield c


def test_app_boots_with_the_cap_registered(client) -> None:
    kinds = [m.cls.__name__ for m in client.app.user_middleware]
    assert "BodyCapMiddleware" in kinds, kinds


def test_oversized_body_gets_a_readable_413(client) -> None:
    response = client.post("/api/uploads/cleanup", content=b"k" * 5000)
    assert response.status_code == 413, response.text
    assert "MAX_BODY_BYTES" in response.text  # 说清上限住在哪个配置里


def test_normal_body_still_passes(client) -> None:
    response = client.post("/api/uploads/cleanup", json={"keep": 1})
    assert response.status_code != 413, response.text
