"""前后端接线的静态护栏。

为什么需要：`frontend/src/api.ts` 的 `request()` 曾经只设 `Content-Type` 却没有
`JSON.stringify(body)` —— 于是 fetch 把对象 body 退化成 `"[object Object]"`，
**页面 GET 全正常、所有写操作静默 422**。pytest 与 smoke_check 都直接打 API，
对"前端接线"这一类故障完全看不见（README 截图也看不出来，因为截图都是只读视图）。

这条断言把该故障钉死：谁把序列化删掉，测试立刻红。
"""

from __future__ import annotations

import re
from pathlib import Path

API_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api.ts"
STREAM_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "stream.ts"


def test_sse_event_vocabulary_matches_the_parser() -> None:
    """内核声明的轮次事件名 == 前端 `ChatEvent` 联合里声明的那些（一个不多一个不少）。

    为什么单独钉这条（架构审计报告 §7 / D 的前置）：事件名是**跨语言的线协议**，改一边不会
    让另一边编译失败，症状只是"那一类东西再也不显示了"——比如 guard 改写后不替换气泡，
    用户看到半截违规文本还以为是模型的问题。`core/turn.py` 的 `EVENT_TYPES` 与
    `frontend/src/api.ts` 的 `ChatEvent` 各自是唯一声明处，这里做双向差分。
    """
    from rolecard_agent.core.turn import EVENT_TYPES

    api_src = API_TS.read_text(encoding="utf-8")
    block = re.search(r"export type ChatEvent =(.+?\};)", api_src, flags=re.S)
    assert block, "frontend/src/api.ts 里找不到 ChatEvent 联合 —— 词表源头挪位置了？"
    declared_frontend = set(re.findall(r'type:\s*"([a-z_]+)"', block.group(1)))
    assert set(EVENT_TYPES) == declared_frontend, (
        f"内核发了前端没声明的：{sorted(set(EVENT_TYPES) - declared_frontend)}；"
        f"前端声明了内核不发的：{sorted(declared_frontend - set(EVENT_TYPES))}"
    )
    # 解析方（stream.ts 的 switch）必须处理除"收尾类"之外的每一个事件。
    stream_src = STREAM_TS.read_text(encoding="utf-8")
    handled = set(re.findall(r'case "([a-z_]+)"', stream_src))
    no_render = {"end"}  # end 只由调用方结束"生成中"态，没有要渲染的载荷
    assert declared_frontend - no_render <= handled, (
        f"前端解析器漏了这些事件：{sorted(declared_frontend - no_render - handled)}"
    )


def test_json_request_bodies_are_serialised() -> None:
    """JSON 请求体必须显式序列化 —— 否则写操作（启停插件 / 改角色 / 切模型）全部 422。"""
    src = API_TS.read_text(encoding="utf-8")
    assert "JSON.stringify(body)" in src, (
        "frontend/src/api.ts 的 request() 缺少 JSON.stringify(body)：fetch 不会自动序列化"
        "对象，body 会变成 '[object Object]'，服务端 JSON 解析失败 → 所有写操作 422。"
    )


def test_formdata_is_not_json_stringified() -> None:
    """FormData 必须走 multipart 原样提交 —— 序列化它会破坏文件上传的边界。"""
    src = API_TS.read_text(encoding="utf-8")
    assert "body instanceof FormData" in src, "FormData 分支丢失：multipart 上传会被当成 JSON"
