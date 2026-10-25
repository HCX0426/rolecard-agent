"""API 响应模型 —— **自动生成，勿手改**。

生成器：`scripts/tools/gen_response_models.py`（从运行时真实返回反推字段集）。
漂移判据：同一个脚本的 `--check`（CI 每次 push 比对现形状 vs 入库模型）。
手改这份文件 = 下一次生成被覆盖；要改字段集，改端点再重跑生成器。

不可建模（形状随数据变化）的端点：（无）

配套：引用这些模型的端点必须带 `response_model_exclude_unset=True` ——
否则模型会把端点**没给**的缺省键补成 null（实测 pets 的 rows 多出
'listening': None，响应与端点真返回不再逐字一致）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, RootModel, model_serializer, model_validator

#: 记录「端点真给了哪些键」的内部键名（序列化时按它裁剪）。
_GIVEN_KEYS = "__given_keys__"


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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    packs: list[PetsViewPacksItem] | None
    skipped: list[dict[str, Any]] | None
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    listening: Any
    thinking: Any


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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    pass


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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    name: Any
    description: Any


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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    servers: list[dict[str, Any]] | None
    effective_count: int | None


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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    items: list[dict[str, Any]] | None
    unread: int | None
    unread_by_role: ReachoutsViewUnread_by_role | None
    file_watch_pending: int | None
    merge_days: int | None
    quiet: list[dict[str, Any]] | None


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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    pass


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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    items: list[dict[str, Any]] | None
    total: int | None
    limit: int | None
    offset: int | None


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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    enabled: bool | None
    role_id: Any
    content: str | None
    items: list[dict[str, Any]] | None
    active_count: int | None
    limit: int | None
    over_limit: bool | None
    extract_turns: int | None


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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    key_masked: Any
    models: list[Any] | None


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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    items: list[SyncInventoryViewItemsItem] | None
    skipped: list[dict[str, Any]] | None
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    current: str | None
    latest: str | None
    has_update: bool | None
    source: str | None
    url: str | None
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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    orphans: list[dict[str, Any]] | None
    total_bytes: int | None
    scanned: int | None
    referenced: int | None
    dangling: list[dict[str, Any]] | None


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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    status: str | None
    version: str | None
    auth_mode: str | None
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    items: list[dict[str, Any]] | None
    pending: int | None


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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
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
    def _drop_filled(self, handler: Any) -> Any:
        """把"补出来的键"裁掉 —— 只留端点真给过的。"""
        data = handler(self)
        given = (self.__pydantic_extra__ or {}).get(_GIVEN_KEYS)
        if given is None:
            return data
        return {k: v for k, v in data.items() if k in given}

    role_id: str | None
    items: list[dict[str, Any]] | None
    next_cursor: Any
    truncated: bool | None


class PetsView(RootModel[PetsViewRoot]):
    """/api/pets 的顶层形状（数组包一层只为给它一个名字）。"""


class ToolsCatalog(RootModel[ToolsCatalogRoot]):
    """/api/tools/catalog 的顶层形状（数组包一层只为给它一个名字）。"""


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


class SessionsView(RootModel[list[dict[str, Any]]]):
    """/api/sessions 的顶层形状（数组包一层只为给它一个名字）。"""


class SettingsMemoryView(RootModel[SettingsMemoryViewRoot]):
    """/api/settings/memory 的顶层形状（数组包一层只为给它一个名字）。"""


class ModelProvidersView(RootModel[ModelProvidersViewRoot]):
    """/api/settings/model-providers 的顶层形状（数组包一层只为给它一个名字）。"""


class ModelSettingsView(RootModel[ModelSettingsViewRoot]):
    """/api/settings/models 的顶层形状（数组包一层只为给它一个名字）。"""


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
