"""前后端接线的静态护栏。

为什么需要：`frontend/src/api.ts` 的 `request()` 曾经只设 `Content-Type` 却没有
`JSON.stringify(body)` —— 于是 fetch 把对象 body 退化成 `"[object Object]"`，
**页面 GET 全正常、所有写操作静默 422**。pytest 与 smoke_check 都直接打 API，
对"前端接线"这一类故障完全看不见（README 截图也看不出来，因为截图都是只读视图）。

这条断言把该故障钉死：谁把序列化删掉，测试立刻红。
"""

from __future__ import annotations

from pathlib import Path

API_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api.ts"


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
