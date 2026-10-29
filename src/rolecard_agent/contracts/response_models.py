"""API 响应模型 —— **自动生成，勿手改**。

生成器：`scripts/tools/gen_response_models.py`（从运行时真实返回反推字段集）。
漂移判据：同一个脚本的 `--check`（CI 每次 push 比对现形状 vs 入库模型）。
手改这份文件 = 下一次生成被覆盖；要改字段集，改端点再重跑生成器。

不可建模（形状随数据变化）的端点：
  - /api/roles

配套：引用这些模型的端点必须带 `response_model_exclude_unset=True` ——
否则模型会把端点**没给**的缺省键补成 null（实测 pets 的 rows 多出
'listening': None，响应与端点真返回不再逐字一致）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, RootModel, model_serializer, model_validator

#: 记录「端点真给了哪些键」的内部键名（序列化时按它裁剪）。
_GIVEN_KEYS = "__given_keys__"


class ServicesViewRoot(BaseModel):
    """自动生成：ServicesViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    services: list[ServicesViewServicesItem] | None


class ServicesViewServicesItem(BaseModel):
    """自动生成：ServicesViewServicesItem（servicesItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    key: str | None
    title: str | None
    hint: str | None
    effective: str | None
    effective_kind: str | None
    degraded_from: str | None
    readonly: bool | None
    candidates: list[ServicesViewServicesItem_candidatesItem] | None
    default_backend: str | None
    order_only: bool | None


class ServicesViewServicesItem_candidatesItem(BaseModel):
    """自动生成：ServicesViewServicesItem_candidatesItem。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: str | None
    label: str | None
    kind: str | None
    base_url: str | None
    model: str | None
    enabled: bool | None
    builtin: bool | None
    ref_backend: str | None
    stale: bool | None
    key_masked: str | None
    available: bool | None
    reason: str | None
    order: int | None


class PetsViewRoot(BaseModel):
    """自动生成：PetsViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    packs: list[PetsViewPacksItem] | None
    skipped: list[PetsViewSkippedItem] | None
    user_dir: str | None
    cubism_core: bool | None
    cubism_core_path: str | None


class PetsViewPacksItem(BaseModel):
    """自动生成：PetsViewPacksItem（packsItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: str | None
    label: str | None
    kind: str | None
    rows: PetsViewPacksItem_rows | None
    motions: PetsViewPacksItem_motions | None
    source: str | None
    entry: str | None
    sheet_url: str | None


class PetsViewPacksItem_rows(BaseModel):
    """自动生成：PetsViewPacksItem_rows（packsItem_rows 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    listening: int | None
    thinking: int | None


class PetsViewPacksItem_motions(BaseModel):
    """自动生成：PetsViewPacksItem_motions（packsItem_motions 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    pass


class PetsViewSkippedItem(BaseModel):
    """自动生成：PetsViewSkippedItem（skippedItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: str | None
    reason: str | None


class ToolsCatalogRoot(BaseModel):
    """自动生成：ToolsCatalogRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    kernel: list[ToolsCatalogKernelItem] | None
    domains: ToolsCatalogDomains | None


class ToolsCatalogKernelItem(BaseModel):
    """自动生成：ToolsCatalogKernelItem（kernelItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    name: str | None
    description: str | None


class ToolsCatalogDomains(BaseModel):
    """自动生成：ToolsCatalogDomains（domains 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    health: list[ToolsCatalogDomains_healthItem] | None


class ToolsCatalogDomains_healthItem(BaseModel):
    """自动生成：ToolsCatalogDomains_healthItem（domains_healthItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    name: str | None
    description: str | None


class KnowledgeViewItem(BaseModel):
    """自动生成：KnowledgeViewItem（Item 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    scope: str | None
    chunks: int | None
    sources: list[str] | None
    embedder: str | None


class KnowledgeScopesViewRoot(BaseModel):
    """自动生成：KnowledgeScopesViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    scopes: list[str] | None


class LocalServiceViewRoot(BaseModel):
    """自动生成：LocalServiceViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    base_url: str | None
    running: bool | None
    resident: list[dict[str, Any]] | None
    resident_bytes: int | None
    pinned: bool | None
    is_local: bool | None
    model: str | None


class McpServersViewRoot(BaseModel):
    """自动生成：McpServersViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    servers: list[McpServersViewServersItem] | None
    effective_count: int | None


class McpServersViewServersItem(BaseModel):
    """自动生成：McpServersViewServersItem（serversItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: str | None
    display_name: str | None
    transport: str | None
    url: str | None
    headers: McpServersViewServersItem_headers | None
    enabled: bool | None


class McpServersViewServersItem_headers(BaseModel):
    """自动生成：McpServersViewServersItem_headers（serversItem_headers 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    Authorization: str | None


class RagMetricsViewRoot(BaseModel):
    """自动生成：RagMetricsViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    samples: int | None
    rerank_enabled: bool | None
    embedder: str | None
    p50: RagMetricsViewP50 | None
    p95: RagMetricsViewP95 | None
    p99: RagMetricsViewP99 | None


class RagMetricsViewP50(BaseModel):
    """自动生成：RagMetricsViewP50（p50 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    embed_ms: Any
    vector_ms: Any
    rerank_ms: Any
    total_ms: Any


class RagMetricsViewP95(BaseModel):
    """自动生成：RagMetricsViewP95（p95 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    embed_ms: Any
    vector_ms: Any
    rerank_ms: Any
    total_ms: Any


class RagMetricsViewP99(BaseModel):
    """自动生成：RagMetricsViewP99（p99 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    embed_ms: Any
    vector_ms: Any
    rerank_ms: Any
    total_ms: Any


class ReachoutsViewRoot(BaseModel):
    """自动生成：ReachoutsViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    items: list[ReachoutsViewItemsItem] | None
    unread: int | None
    unread_by_role: ReachoutsViewUnread_by_role | None
    file_watch_pending: int | None
    merge_days: int | None
    quiet: list[ReachoutsViewQuietItem] | None


class ReachoutsViewItemsItem(BaseModel):
    """自动生成：ReachoutsViewItemsItem（itemsItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: int | None
    role_id: str | None
    role_name: str | None
    text: str | None
    state: str | None
    created_at: str | None
    read_at: Any
    seen_at: Any
    dismissed_at: Any
    thread_id: str | None


class ReachoutsViewUnread_by_role(BaseModel):
    """自动生成：ReachoutsViewUnread_by_role（unread_by_role 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    sample_reachable: int | None


class ReachoutsViewQuietItem(BaseModel):
    """自动生成：ReachoutsViewQuietItem（quietItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    role_id: str | None
    role_name: str | None
    why: str | None
    next_ok_at: str | None
    streak: int | None
    unread: int | None


class RecordsViewRoot(BaseModel):
    """自动生成：RecordsViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    items: list[RecordsViewItemsItem] | None
    total: int | None
    limit: int | None
    offset: int | None


class RecordsViewItemsItem(BaseModel):
    """自动生成：RecordsViewItemsItem（itemsItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    report_id: str | None
    report_type: str | None
    check_time: str | None
    institution: str | None
    note: str | None
    indices: list[RecordsViewItemsItem_indicesItem] | None


class RecordsViewItemsItem_indicesItem(BaseModel):
    """自动生成：RecordsViewItemsItem_indicesItem（itemsItem_indicesItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    index_id: str | None
    index_name: str | None
    index_value: float | None
    value_text: str | None
    unit: str | None
    ref_range: str | None
    is_verified: int | None
    source: str | None
    raw_text: str | None


class SessionsViewItem(BaseModel):
    """自动生成：SessionsViewItem（Item 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    thread_id: str | None
    title: str | None
    role_id: str | None
    role_name: str | None
    agent_mode: str | None
    updated_at: str | None
    is_proactive: bool | None
    is_blank: bool | None


class SettingsMemoryViewRoot(BaseModel):
    """自动生成：SettingsMemoryViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    enabled: bool | None
    role_id: Any
    content: str | None
    items: list[SettingsMemoryViewItemsItem] | None
    active_count: int | None
    limit: int | None
    over_limit: bool | None
    extract_turns: int | None


class SettingsMemoryViewItemsItem(BaseModel):
    """自动生成：SettingsMemoryViewItemsItem（itemsItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: int | None
    user_id: str | None
    uid: str | None
    role_id: str | None
    text: str | None
    source: str | None
    pinned: bool | None
    hit_count: int | None
    importance: int | None
    last_hit_at: Any
    invalidated_at: Any
    superseded_by: Any
    created_at: str | None


class ModelProvidersViewRoot(BaseModel):
    """自动生成：ModelProvidersViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    providers: list[ModelProvidersViewProvidersItem] | None


class ModelProvidersViewProvidersItem(BaseModel):
    """自动生成：ModelProvidersViewProvidersItem（providersItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: str | None
    label: str | None
    needs_key: bool | None
    base_url_hint: str | None
    style: str | None
    default_base_url: str | None


class ModelSettingsViewRoot(BaseModel):
    """自动生成：ModelSettingsViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    default: str | None
    providers: list[ModelSettingsViewProvidersItem] | None
    fallbacks: list[str] | None


class ModelSettingsViewProvidersItem(BaseModel):
    """自动生成：ModelSettingsViewProvidersItem（providersItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: str | None
    provider: str | None
    label: str | None
    base_url: str | None
    style: str | None
    needs_key: bool | None
    has_key: bool | None
    key_masked: str | None
    models: list[ModelSettingsViewProvidersItem_modelsItem] | None


class ModelSettingsViewProvidersItem_modelsItem(BaseModel):
    """自动生成：ModelSettingsViewProvidersItem_modelsItem。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    name: str | None
    model: str | None
    num_ctx: int | None
    supports_vision: bool | None
    supports_tools: bool | None
    repeat_penalty: float | None
    frequency_penalty: float | None
    presence_penalty: float | None
    used_by: list[str] | None
    is_default: bool | None


class RuntimeSettingsViewRoot(BaseModel):
    """自动生成：RuntimeSettingsViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    note: str | None
    groups: list[RuntimeSettingsViewGroupsItem] | None


class RuntimeSettingsViewGroupsItem(BaseModel):
    """自动生成：RuntimeSettingsViewGroupsItem（groupsItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    key: str | None
    label: str | None
    items: list[RuntimeSettingsViewGroupsItem_itemsItem] | None


class RuntimeSettingsViewGroupsItem_itemsItem(BaseModel):
    """自动生成：RuntimeSettingsViewGroupsItem_itemsItem（groupsItem_itemsItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    key: str | None
    field: str | None
    label: str | None
    value: str | None
    default: str | None
    changed: bool | None
    overridden: bool | None
    override_value: str | None
    kind: str | None
    choices: list[str] | None
    note: str | None


class ShellReleaseViewRoot(BaseModel):
    """自动生成：ShellReleaseViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    available: bool | None
    configured: bool | None


class SyncInventoryViewRoot(BaseModel):
    """自动生成：SyncInventoryViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    items: list[SyncInventoryViewItemsItem] | None
    skipped: list[SyncInventoryViewSkippedItem] | None
    kinds: list[str] | None


class SyncInventoryViewItemsItem(BaseModel):
    """自动生成：SyncInventoryViewItemsItem（itemsItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    kind: str | None
    ident: str | None
    hash: str | None
    at: str | None
    preview: str | None
    head: str | None
    count: int | None


class SyncInventoryViewSkippedItem(BaseModel):
    """自动生成：SyncInventoryViewSkippedItem（skippedItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    kind: str | None
    ident: str | None
    reason: str | None
    preview: str | None


class UpdateCheckViewRoot(BaseModel):
    """自动生成：UpdateCheckViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    current: str | None
    latest: Any
    has_update: bool | None
    source: str | None
    url: Any
    file_name: Any
    size_bytes: Any
    error: Any


class OrphanUploadsViewRoot(BaseModel):
    """自动生成：OrphanUploadsViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    orphans: list[OrphanUploadsViewOrphansItem] | None
    total_bytes: int | None
    scanned: int | None
    referenced: int | None
    dangling: list[str] | None


class OrphanUploadsViewOrphansItem(BaseModel):
    """自动生成：OrphanUploadsViewOrphansItem（orphansItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    name: str | None
    size: int | None
    companion: bool | None


class WorkspaceDirViewRoot(BaseModel):
    """自动生成：WorkspaceDirViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    path: str | None
    overridden: bool | None


class WorkspaceTreeViewRoot(BaseModel):
    """自动生成：WorkspaceTreeViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    path: str | None
    parent: str | None
    entries: list[WorkspaceTreeViewEntriesItem] | None
    truncated: bool | None


class WorkspaceTreeViewEntriesItem(BaseModel):
    """自动生成：WorkspaceTreeViewEntriesItem（entriesItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    name: str | None
    is_dir: bool | None
    size: int | None


class HealthViewRoot(BaseModel):
    """自动生成：HealthViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    status: str | None
    version: str | None
    auth_mode: str | None
    error_count_recent: int | None
    build: HealthViewBuild | None
    max_upload_bytes: int | None
    max_image_bytes: int | None


class HealthViewBuild(BaseModel):
    """自动生成：HealthViewBuild（build 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    sha: str | None
    built: str | None
    dirty: bool | None


class HealthDeepViewRoot(BaseModel):
    """自动生成：HealthDeepViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    status: str | None
    checks: HealthDeepViewChecks | None


class HealthDeepViewChecks(BaseModel):
    """自动生成：HealthDeepViewChecks（checks 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    sqlite: HealthDeepViewChecks_sqlite | None
    chroma: HealthDeepViewChecks_chroma | None
    models: HealthDeepViewChecks_models | None


class HealthDeepViewChecks_sqlite(BaseModel):
    """自动生成：HealthDeepViewChecks_sqlite（checks_sqlite 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    ok: bool | None
    detail: str | None


class HealthDeepViewChecks_chroma(BaseModel):
    """自动生成：HealthDeepViewChecks_chroma（checks_chroma 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    ok: bool | None
    detail: str | None


class HealthDeepViewChecks_models(BaseModel):
    """自动生成：HealthDeepViewChecks_models（checks_models 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    ok: bool | None
    detail: str | None


class ApprovalsViewRoot(BaseModel):
    """自动生成：ApprovalsViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    items: list[ApprovalsViewItemsItem] | None
    pending: int | None


class ApprovalsViewItemsItem(BaseModel):
    """自动生成：ApprovalsViewItemsItem（itemsItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: int | None
    command: str | None
    cwd: str | None
    role_id: str | None
    role_name: str | None
    thread_id: str | None
    status: str | None
    result: ApprovalsViewItemsItem_result | None
    has_token: bool | None
    decide_token_ttl_seconds: int | None
    created_at: str | None
    updated_at: str | None
    decide_token: str | None


class ApprovalsViewItemsItem_result(BaseModel):
    """自动生成：ApprovalsViewItemsItem_result（itemsItem_result 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    exit_code: int | None
    output: str | None
    output_bytes: int | None
    duration_ms: int | None


class AuditViewItem(BaseModel):
    """自动生成：AuditViewItem（Item 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: int | None
    ts: str | None
    actor: str | None
    action: str | None
    target: str | None
    detail_json: str | None


class DomainsViewItem(BaseModel):
    """自动生成：DomainsViewItem（Item 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: str | None
    display_name: str | None


class RoleTimelineViewRoot(BaseModel):
    """自动生成：RoleTimelineViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    role_id: str | None
    items: list[RoleTimelineViewItemsItem] | None
    next_cursor: str | None
    truncated: bool | None


class RoleTimelineViewItemsItem(BaseModel):
    """自动生成：RoleTimelineViewItemsItem（itemsItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    kind: str | None
    at: str | None
    text: str | None
    verb: str | None
    from_text: str | None
    thread_id: str | None
    ref_id: int | None


class ChannelsViewRoot(BaseModel):
    """自动生成：ChannelsViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    channels: list[ChannelsViewChannelsItem] | None


class ChannelsViewChannelsItem(BaseModel):
    """自动生成：ChannelsViewChannelsItem（channelsItem 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    id: str | None
    label: str | None
    enabled: bool | None
    running: bool | None
    blocked_reason: str | None
    role_id: str | None
    inflight_turns: int | None
    messages_sent: int | None


class SessionThreadViewRoot(BaseModel):
    """自动生成：SessionThreadViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    thread_id: str | None
    user_id: str | None
    role_id: str | None
    role_name: str | None
    model_name: Any
    agent_mode: str | None


class SessionTurnViewRoot(BaseModel):
    """自动生成：SessionTurnViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    inflight: Any


class SessionMessagesViewRoot(BaseModel):
    """自动生成：SessionMessagesViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    messages: list[dict[str, Any]] | None
    total: int | None
    limit: int | None
    truncated: bool | None
    inflight: Any


class SessionContextViewRoot(BaseModel):
    """自动生成：SessionContextViewRoot（ 的对象形状）。"""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data: Any) -> Any:
        """缺键补 None —— 必填字段遇到响应里没有的键时不该炸。

        同时记下**端点真给了哪些键**（_GIVEN_KEYS）：序列化时按它裁剪，
        于是响应与端点真返回逐字一致（补出来的键不会被吐出去）。

        为什么不用 exclude_unset：补的键进了 __fields_set__ ⇒ exclude_unset
        挡不住它（实测 pets 的 rows 多出 'listening': None）。
        """
        if isinstance(data, dict):
            known = set(cls.model_fields)
            given = {k: v for k, v in data.items() if k != _GIVEN_KEYS}
            missing = {k: None for k in known if k not in given}
            merged = {**given, **missing}
            merged[_GIVEN_KEYS] = set(given)
            return merged
        return data

    @model_serializer(mode="wrap")
    def _drop_filled(self, handler: Any):
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    trimmed: int | None
    kept: int | None
    budget: int | None


class ServicesView(RootModel[ServicesViewRoot]):
    """/api/services 的顶层形状（数组包一层只为给它一个名字）。"""


class PetsView(RootModel[PetsViewRoot]):
    """/api/pets 的顶层形状（数组包一层只为给它一个名字）。"""


class ToolsCatalog(RootModel[ToolsCatalogRoot]):
    """/api/tools/catalog 的顶层形状（数组包一层只为给它一个名字）。"""


class KnowledgeView(RootModel[list[KnowledgeViewItem]]):
    """/api/knowledge 的顶层形状（数组包一层只为给它一个名字）。"""


class KnowledgeScopesView(RootModel[KnowledgeScopesViewRoot]):
    """/api/knowledge/scopes 的顶层形状（数组包一层只为给它一个名字）。"""


class LocalServiceView(RootModel[LocalServiceViewRoot]):
    """/api/local-service 的顶层形状（数组包一层只为给它一个名字）。"""


class McpServersView(RootModel[McpServersViewRoot]):
    """/api/mcp/servers 的顶层形状（数组包一层只为给它一个名字）。"""


class RagMetricsView(RootModel[RagMetricsViewRoot]):
    """/api/rag/metrics 的顶层形状（数组包一层只为给它一个名字）。"""


class ReachoutsView(RootModel[ReachoutsViewRoot]):
    """/api/reachouts 的顶层形状（数组包一层只为给它一个名字）。"""


class RecordsView(RootModel[RecordsViewRoot]):
    """/api/records 的顶层形状（数组包一层只为给它一个名字）。"""


class SessionsView(RootModel[list[SessionsViewItem]]):
    """/api/sessions 的顶层形状（数组包一层只为给它一个名字）。"""


class SettingsMemoryView(RootModel[SettingsMemoryViewRoot]):
    """/api/settings/memory 的顶层形状（数组包一层只为给它一个名字）。"""


class ModelProvidersView(RootModel[ModelProvidersViewRoot]):
    """/api/settings/model-providers 的顶层形状（数组包一层只为给它一个名字）。"""


class ModelSettingsView(RootModel[ModelSettingsViewRoot]):
    """/api/settings/models 的顶层形状（数组包一层只为给它一个名字）。"""


class RuntimeSettingsView(RootModel[RuntimeSettingsViewRoot]):
    """/api/settings/runtime 的顶层形状（数组包一层只为给它一个名字）。"""


class ShellReleaseView(RootModel[ShellReleaseViewRoot]):
    """/api/shell-release 的顶层形状（数组包一层只为给它一个名字）。"""


class SyncInventoryView(RootModel[SyncInventoryViewRoot]):
    """/api/sync/inventory 的顶层形状（数组包一层只为给它一个名字）。"""


class UpdateCheckView(RootModel[UpdateCheckViewRoot]):
    """/api/update-check 的顶层形状（数组包一层只为给它一个名字）。"""


class OrphanUploadsView(RootModel[OrphanUploadsViewRoot]):
    """/api/uploads/orphans 的顶层形状（数组包一层只为给它一个名字）。"""


class WorkspaceDirView(RootModel[WorkspaceDirViewRoot]):
    """/api/workspace/dir 的顶层形状（数组包一层只为给它一个名字）。"""


class WorkspaceTreeView(RootModel[WorkspaceTreeViewRoot]):
    """/api/workspace/tree 的顶层形状（数组包一层只为给它一个名字）。"""


class HealthView(RootModel[HealthViewRoot]):
    """/api/health 的顶层形状（数组包一层只为给它一个名字）。"""


class HealthDeepView(RootModel[HealthDeepViewRoot]):
    """/api/health/deep 的顶层形状（数组包一层只为给它一个名字）。"""


class ApprovalsView(RootModel[ApprovalsViewRoot]):
    """/api/approvals 的顶层形状（数组包一层只为给它一个名字）。"""


class AuditView(RootModel[list[AuditViewItem]]):
    """/api/audit 的顶层形状（数组包一层只为给它一个名字）。"""


class DomainsView(RootModel[list[DomainsViewItem]]):
    """/api/domains 的顶层形状（数组包一层只为给它一个名字）。"""


class RoleTimelineView(RootModel[RoleTimelineViewRoot]):
    """/api/roles/{role_id}/timeline 的顶层形状（数组包一层只为给它一个名字）。"""


class ChannelsView(RootModel[ChannelsViewRoot]):
    """/api/channels 的顶层形状（数组包一层只为给它一个名字）。"""


class SessionThreadView(RootModel[SessionThreadViewRoot]):
    """/api/session/{thread_id} 的顶层形状（数组包一层只为给它一个名字）。"""


class SessionTurnView(RootModel[SessionTurnViewRoot]):
    """/api/session/{thread_id}/turn 的顶层形状（数组包一层只为给它一个名字）。"""


class SessionMessagesView(RootModel[SessionMessagesViewRoot]):
    """/api/session/{thread_id}/messages 的顶层形状（数组包一层只为给它一个名字）。"""


class SessionContextView(RootModel[SessionContextViewRoot]):
    """/api/session/{thread_id}/context 的顶层形状（数组包一层只为给它一个名字）。"""
