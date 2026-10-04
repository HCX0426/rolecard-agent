"""api/errors.py 的「边界输入错误 → HTTP」映射（+ 它的家在哪儿这件事）。

`value_error_to_http` 是路由层十几处 `raise HTTPException(400, str(exc))` 的统一入口。
2026-10-04 错误收口时它随三个收口 helper 一起搬进 `api/errors.py`：`role_error_to_http`
与 `plugin_error_to_http` 被注册表（`_FAMILIES`）**取代**（路由的对应 catch 已删），
只有 ValueError 这支留作显式翻译 —— 裸 ValueError 不进注册表的理由写在模块文档里
（pydantic.ValidationError 是它的子类，全局翻译会把框架 bug 伪装成"用户输入错"）。
状态码与 detail 透传不能被「顺手改成别的码」，这里直接钉。
"""

from __future__ import annotations

from rolecard_agent.api.errors import _FAMILIES, value_error_to_http


def test_value_error_maps_to_400_with_detail() -> None:
    exc = value_error_to_http(ValueError("不支持在线修改的配置项：WEB_SEARCH_ENABLED"))
    assert exc.status_code == 400
    assert exc.detail == "不支持在线修改的配置项：WEB_SEARCH_ENABLED"


def test_registry_covers_every_formerly_hand_mapped_family() -> None:
    """注册表要接住从前散落手写的那批码 —— 少一条就是某个路由退回手写 catch。

    码本身也钉死：它们不是选择题（404/409/403 各自的判据见 `_FAMILIES` 第三列）。
    """
    mapping = {cls.__name__: status for cls, status, _why in _FAMILIES}
    assert mapping["RoleNotFound"] == 404
    assert mapping["RoleAlreadyExists"] == 409
    assert mapping["BuiltinRoleProtected"] == 409
    assert mapping["UnknownPlugin"] == 404
    assert mapping["ModelSettingsError"] == 400
    assert mapping["ApprovalNotFound"] == 404
    assert mapping["ApprovalAlreadyDecided"] == 400
    assert mapping["ApprovalUnauthorised"] == 403  # 缺口是"没持凭据"，不是"没登录"
    assert mapping["IngestionNotFound"] == 404
    assert mapping["UploadRejected"] == 400
    assert mapping["UploadUnreadable"] == 500  # 已登记但读不出来：要人来看
    # 泛族在后、具体族在前无所谓 —— Starlette 按异常类的 MRO 找 handler，具体盖泛族。
    assert mapping["RoleError"] == 400
    assert mapping["PluginError"] == 400
