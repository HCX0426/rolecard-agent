"""请求体上限（2026-10-02 轮 `R102-45` 家族的收尾项，10-03 拍板：64 MiB + 413）。

量到的事实（`build/probe_body_size.py`，真后端、真 socket）：传输层没有任何上限，
**20MB 的请求体 → 后端工作集 +40.7MB、进程峰值 +60.7MB**（≈3× body，请求后不回落）。
线性成立意味着"一条大 body = 一次无界的内存承诺"，而它只需要一个 POST。

两道拦法，缺一都不算闭：

  1. **`Content-Length` 超上限 → 立刻 413**，一个字节都不读（绝大多数客户端走这条）；
  2. **分块 / 不带长度的 body 在流进来的路上计数**，越过上限就当场 413 并**丢掉内层应用
     的响应**（`send` 被接管）。只做第 1 条等于告诉后面读代码的人"这里有上限"，而
     `Transfer-Encoding: chunked` 照样能灌满内存 —— 那正是本仓那一族"宣称 > 判据"。

`MAX_BODY_BYTES=0` = 不设上限（回到今天的行为，给排障留的一键）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from typing import Any

# 不 import starlette 的类型（`starlette` 不在 requirements 的声明面里，它是 fastapi 的
# 传递依赖 —— 直接依赖它等于给自己加一条没登记过的边）。ASGI 这三件本来就是鸭子类型。
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


def _header(scope: Scope, name: str) -> str | None:
    """从 ASGI scope 的原始头里取一个值（大小写不敏感，重复取第一个）。"""
    want = name.encode("latin-1").lower()
    pairs: Sequence[tuple[bytes, bytes]] = scope.get("headers") or []
    for key, value in pairs:
        if key.lower() == want:
            return value.decode("latin-1")
    return None


def length_too_large(content_length: str | None, limit: int) -> bool:
    """`Content-Length` 是否已经超了上限。纯判据，便于单测与变异。

    非法值（非数字）不当超限 —— 那是协议层的题，由后面的计数那一道兜住，而不是在这里
    把一个可能合法的请求误杀（本仓的规矩：判据不许在"未知"上判红）。
    """
    if limit <= 0 or not content_length:
        return False
    try:
        declared = int(content_length)
    except ValueError:
        return False
    return declared > limit


class BodyCapMiddleware:
    """最外层的那道闸：请求体超过 `limit` 字节就 413，且不再往内层送 body。"""

    def __init__(
        self, app: Callable[[Scope, Receive, Send], Awaitable[None]], *, limit: int
    ) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.limit <= 0:
            await self.app(scope, receive, send)
            return

        if length_too_large(_header(scope, "content-length"), self.limit):
            await self._reject(send)
            return

        state = {"seen": 0, "tripped": False, "rejected": False}

        async def counted() -> Message:
            message = await receive()
            if message["type"] == "http.request" and not state["tripped"]:
                state["seen"] += len(message.get("body", b""))
                if state["seen"] > self.limit:
                    state["tripped"] = True
                    # 给内层一个"body 到此为止"的收尾，免得它挂在半句上等超时。
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def guarded(message: Message) -> None:
            if state["tripped"]:
                if not state["rejected"]:
                    state["rejected"] = True
                    await self._reject(send)
                return  # 内层那半截响应已经不算数了
            await send(message)

        await self.app(scope, counted, guarded)

    async def _reject(self, send: Send) -> None:
        """一条可读的 413：说清上限是多少、上限住在哪配置里。"""
        payload = (
            f'{{"detail":"请求体超过上限 {self.limit} 字节'
            f"（上限见配置 MAX_BODY_BYTES，0=不设上限）。"
            f'整份替换与知识库导入请分批推送。"}}'
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
