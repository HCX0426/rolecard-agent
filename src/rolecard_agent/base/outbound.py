"""外发请求的**一条规则**：打到本机/内网端点的、或者带着凭据的，一律不读系统代理。

httpx 默认 `trust_env=True`，会吃机器上的 `HTTP_PROXY` / `HTTPS_PROXY`。对这一族请求来说
那是错的：

  * **带凭据的那几发**（模型测连与拉列表带着已存的 api_key；上行同步带着用户刚输的
    登录口令）—— 多一跳代理就是多一个能看见它的地方；
  * **打到本机的探活**（Ollama 的 `/api/tags`、`/api/show`、`/api/generate`）—— 问的是
    "这台机器上有没有在跑"，绕道一台互联网代理不但无意义，还会把"没跑"报成"连不上"。

通用联网抓取（`core/tools/web.py`）**故意不走这里**：那一条本来就要出网，用户设的代理
可能正是他要的路径。分界不是"要不要代理"，而是**这条请求的目标与凭据该不该经过第三者**。

假客户端（测试里 stub 掉 `httpx.get` / `httpx.post` 的那些）跟着这里的签名走：本模块只是
把 kwargs 原样递给 httpx，所以 stub 仍然打在真位置上。
"""

from __future__ import annotations

from typing import Any

import httpx


def get(url: str, **kwargs: Any) -> httpx.Response:
    """`httpx.get` + `trust_env=False`（见模块 docstring 那条分界）。"""
    kwargs.setdefault("trust_env", False)
    return httpx.get(url, **kwargs)


def post(url: str, **kwargs: Any) -> httpx.Response:
    """`httpx.post` + `trust_env=False`。"""
    kwargs.setdefault("trust_env", False)
    return httpx.post(url, **kwargs)


__all__ = ["get", "post"]
