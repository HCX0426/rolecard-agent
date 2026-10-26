"""自动生成响应模型的兜底分支（P1 契约链的覆盖率地板用例）。

为什么需要：`response_models.py` 是生成器写的，每个模型都带同一对机制 ——
before-validator「缺键补 None + 记下真给的键」与 wrap-serializer「按真给的键裁剪」。
端点集成用例只走"正常 JSON 进来"这一条路，三处兜底分支永远不会被真流量踩到：

① validator 收到**非 dict**（RootModel 包装失败、单测直接喂标量）⇒ 原样放行，
   让 Pydantic 自己报类型错（生成器不能替它吞）；
② serializer 的 `given is None`（实例是 `model_construct` 旁路 validator 造的）
   ⇒ 不裁剪、原样吐；
③ validator 里 `k != _GIVEN_KEYS` 为真的那一支（有人把内部键混进响应）⇒ 剔掉，
   内部键绝不允许借 extra="allow" 漏给前端。

这三条全是"生成物不该漏出去的东西"的守卫。集成用例结构上到不了（端点不会返回
非 dict、也不会自己塞 _GIVEN_KEYS），所以在这里逐个模型直接打 —— 参数化到
**生成类的全体**：生成器哪天改模板（漏写分支/换机制），这里跟着红，
而不是等到某天真数据踩上才在响应里看见内部键。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, RootModel, ValidationError

from rolecard_agent.api import response_models as rm
from rolecard_agent.api.response_models import _GIVEN_KEYS

#: 全体"带补键机制"的生成模型（RootModel 包装与常量不在此列）。
MECHANIC_CLASSES = sorted(
    (
        (name, cls)
        for name, cls in vars(rm).items()
        if isinstance(cls, type)
        and issubclass(cls, BaseModel)
        and not issubclass(cls, RootModel)
        and "_fill_missing" in cls.__dict__
    ),
    key=lambda item: item[0],
)


def _sample_payload(cls: type[BaseModel]) -> dict:
    """每个字段给一个"值可空"形状都吃得下的样本：{} —— 补键机制会把缺的补成 None。"""
    return {}


@pytest.mark.parametrize(
    "cls", [cls for _, cls in MECHANIC_CLASSES], ids=[n for n, _ in MECHANIC_CLASSES]
)
class TestGeneratedModelFallbacks:
    def test_nondict_passes_through_to_pydantic(self, cls: type[BaseModel]):
        """① 非 dict 进 validator ⇒ 放行 ⇒ Pydantic 自己判非法（不静默接受）。"""
        with pytest.raises(ValidationError):
            cls.model_validate(42)

    def test_construct_instance_dumps_without_trimming(self, cls: type[BaseModel]):
        """② 旁路 validator 造的实例没有 _GIVEN_KEYS ⇒ serializer 原样吐，不炸。"""
        obj = cls.model_construct(**_sample_payload(cls))
        assert isinstance(obj.model_dump(), dict)

    def test_internal_key_never_escapes(self, cls: type[BaseModel]):
        """③ 响应里混进 _GIVEN_KEYS（extra=allow 的口子）⇒ validator 剔掉、序列化后不见。"""
        payload = {_GIVEN_KEYS: "poison", **_sample_payload(cls)}
        obj = cls.model_validate(payload)
        dumped = obj.model_dump()
        assert _GIVEN_KEYS not in dumped
        assert (getattr(obj, "__pydantic_extra__", None) or {}).get(_GIVEN_KEYS) != "poison"

    def test_roundtrip_shape(self, cls: type[BaseModel]):
        """机制的正路（回归底座）：补的键不外漏，真给的键原样回。"""
        given = {f: None for f in cls.model_fields}
        obj = cls.model_validate(given)
        assert obj.model_dump() == given
