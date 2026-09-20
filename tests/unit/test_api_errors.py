"""api/deps.py 的「领域/输入错误 → HTTP」映射。

`value_error_to_http` 是收口 §5 冗余后新增的统一入口：路由层十几处重复的
`raise HTTPException(status_code=400, detail=str(exc)) from exc` 现在都走它。这里直接钉住
它的状态码与 detail 透传 —— 语义不能被「顺手改成别的码」。
"""

from __future__ import annotations

from rolecard_agent.api.deps import value_error_to_http


def test_value_error_maps_to_400_with_detail() -> None:
    exc = value_error_to_http(ValueError("不支持在线修改的配置项：WEB_SEARCH_ENABLED"))
    assert exc.status_code == 400
    assert exc.detail == "不支持在线修改的配置项：WEB_SEARCH_ENABLED"
