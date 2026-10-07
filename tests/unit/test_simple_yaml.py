"""`simple_yaml` 这个 YAML 子集读法的行为（P3-9 解析器化的第三格，服务 compose 那批判据）。

钉的是**正则版做错或做不到**的几种形状：端口映射 `8000:8000` 是**标量**不是映射、
缩进变了照样读得到、注释里的 `#` 不算数据、以及**解析不了要大声抛**而不是静默返回空
（静默返回空在 compose 那批判据里就等于"判绿"，判据变摆设）。

刻意不引 PyYAML（不在 requirements 里 ⇒ 本机绿 / CI 红），所以这里也**不拿 PyYAML 当
参照物**，只按"我们自己的文件用到哪几种形状"来钉。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from consistency.core import YamlSubsetError, simple_yaml  # noqa: E402


def test_嵌套映射按缩进读() -> None:
    assert simple_yaml("services:\n  app:\n    environment:\n      AUTH_MODE: on\n") == {
        "services": {"app": {"environment": {"AUTH_MODE": "on"}}}
    }


def test_端口映射是标量不是映射() -> None:
    """`8000:8000` 冒号后没有空格 —— 按 YAML 的规矩它是一个标量。

    判成映射会把 `expose: ["8000"]` 读成 `{"8000": "8000"}`，端口那条尺子读到的形状整个不对。
    """
    doc = simple_yaml("expose:\n  - 8000\nports:\n  - 8000:8000\n  - \"9000:9000\"\n")
    assert doc["ports"] == ["8000:8000", "9000:9000"]


def test_序列里的单级映射() -> None:
    assert simple_yaml("volumes:\n  - rolecard-data: /app/data\n") == {
        "volumes": [{"rolecard-data": "/app/data"}]
    }


def test_引号标量() -> None:
    assert simple_yaml("x: 'single'\ny: \"double\"\n") == {"x": "single", "y": "double"}


def test_注释整行与行尾都不算数据() -> None:
    assert simple_yaml("# 整行注释\n\na: 1  # 行尾注释\nb: \"带 # 号的串\"\n") == {
        "a": "1",
        "b": "带 # 号的串",
    }


def test_缩进变化照样读得到() -> None:
    """正则版认死 `app:` 两空格、`environment:` 四空格 —— 缩进一变就整段取不到。"""
    doc = simple_yaml("services:\n    app:\n        environment:\n            K: v\n")
    assert doc["services"]["app"]["environment"] == {"K": "v"}


def test_空值与空文档() -> None:
    assert simple_yaml("") == {}
    assert simple_yaml("volumes:\n  rolecard-data:\n") == {"volumes": {"rolecard-data": None}}


def test_超出子集的写法要大声抛() -> None:
    """流式 `{a: 1}` 不在子集里 —— 抛出去让调用方红，比静默返回 {} 判绿强。"""
    with pytest.raises(YamlSubsetError):
        simple_yaml("a: {b: 1}\n")


def test_真仓库的compose读得出来() -> None:
    """正向那一半：真文件必须解析得出 app 段（只测夹具等于没测）。"""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    app = simple_yaml(compose)["services"]["app"]
    assert "AUTH_MODE" in app["environment"]
