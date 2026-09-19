"""接入层认证：解析调用方身份，并决定是否放行。

## 为什么是中间件 + 依赖，而不是只用依赖

FastAPI 的 `dependencies=` 只作用于**路由**，而控制台页面是 `StaticFiles` **mount** 上去的
ASGI 应用 —— 它完全不经过路由的依赖系统。只加依赖的话，API 被保护了、页面却谁都能打开。
所以：

  * **中间件负责"拦"**：覆盖所有请求（API + 静态资源），解析身份并决定是否 401；
  * **依赖负责"取值"**：端点通过 `Depends(get_actor)` 拿到中间件解析好的 `Actor`，
    写进审计日志 —— 这样 `actor` 不再是写死的 `"operator"`。

## 凭证形态：Basic 与 API Key 同时支持

  * `Authorization: Basic <base64>` —— 浏览器原生弹框，前端一行不改，SPA 与 API 一起受保护；
  * `X-API-Key: <key>` —— 给脚本、CI、监控用（浏览器不好带自定义头）。

两者由同一个依赖解析，返回同一个 `Actor`。将来换成 JWT 只改 `_parse_*` 这几个函数，
端点签名不动 —— 这与项目其它可插拔点（`model_resolver` / `enabled_domains`）是同一个套路。

## 三档开关与 fail-closed

`off`（默认，本地零配置）/ `auto`（回环放行、外部要求）/ `on`（一律要求）。
**`on` 却没配任何凭证 = 拒绝所有人**：静默放行比拒绝危险得多，配置错了必须当场暴露。

## `auto` 的"回环"判定只信 TCP 对端

`auto` 的语义是"从本机来的请求放行"。"本机"取自 `request.client.host`，**不是**
`X-Forwarded-For` —— 后者是客户端可自由设置的头，采信它等于给 `auto` 开一个后门
（`-H 'X-Forwarded-For: 127.0.0.1'` 即可全量访问管理面）。确实在反代后面时，把反代
网段配进 `AUTH_TRUSTED_PROXIES`，`client_ip` 才会解析 XFF。详见 `client_ip` 的 docstring。
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hmac
import ipaddress
from dataclasses import dataclass
from typing import Any

from rolecard_agent.config import Settings

ANONYMOUS_KIND = "anonymous"
BASIC_KIND = "basic"
APIKEY_KIND = "apikey"

_WWW_AUTHENTICATE = 'Basic realm="rolecard-agent", charset="UTF-8"'


@dataclass(frozen=True, slots=True)
class Actor:
    """一次请求的调用方身份。

    `id` 会写进审计日志，所以**绝不能是完整凭证**：Basic 记用户名，API Key 只记前 4 位
    （够你在日志里区分是哪个 key，又不足以让人拿去用）。
    """

    id: str = "anonymous"
    kind: str = ANONYMOUS_KIND

    @property
    def is_anonymous(self) -> bool:
        return self.kind == ANONYMOUS_KIND


def _split_list(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _match_api_key(candidate: str, allowed: list[str]) -> str | None:
    """常量时间比对，命中返回该 key 的可记录前缀。"""
    for key in allowed:
        if hmac.compare_digest(candidate, key):
            return key[:4]
    return None


def _parse_basic(header: str, credentials: list[str]) -> Actor | None:
    """`Authorization: Basic base64(user:pass)` —— 与 `auth_credentials` 里的 "user:pass" 比对。"""
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    user, sep, password = decoded.partition(":")
    if not sep:
        return None
    for entry in credentials:
        want_user, want_sep, want_pass = entry.partition(":")
        if not want_sep:
            continue
        if hmac.compare_digest(user, want_user) and hmac.compare_digest(
            password, want_pass
        ):
            return Actor(id=user, kind=BASIC_KIND)
    return None


def resolve_actor(
    *,
    authorization: str | None,
    api_key: str | None,
    settings: Settings,
) -> Actor:
    """从请求头解析身份。

    解析不出（或压根没配凭证）就是 anonymous —— 是否放行交给 `auth_required` 决定，
    这样"解析"和"拦截"两件事可以各自单测。
    """
    credentials = _split_list(settings.auth_credentials)
    keys = _split_list(settings.auth_api_keys)

    if authorization:
        actor = _parse_basic(authorization, credentials)
        if actor is not None:
            return actor
    if api_key and keys:
        prefix = _match_api_key(api_key, keys)
        if prefix is not None:
            return Actor(id=f"key:{prefix}", kind=APIKEY_KIND)
    return Actor()


def client_ip(headers: Any, *, peer: str = "", trusted: Any = ()) -> str:
    """真实来源 IP —— **默认只认直连对端**，只有可信反代才采信 `X-Forwarded-For`。

    ## 为什么默认不采信 XFF

    `X-Forwarded-For` 是**客户端可以自由设置**的请求头。旧实现无条件取它的链首作为来源
    IP，于是 `auto` 档（"回环放行、外部要求"）被一行请求头直接绕过：

        curl -H 'X-Forwarded-For: 127.0.0.1' http://host:8000/api/roles   # -> 200

    这不是配置问题而是实现问题：`auto` 的设计意图是"只有真的从本机来才放行"，而"本机"
    的定义必须是**TCP 对端地址**（`request.client.host`），不能是请求体里的字符串。

    ## 可信反代怎么支持

    反代场景下 `request.client.host` 是反代自己，此时才需要 XFF。做法是把反代的网段显式
    配进 `AUTH_TRUSTED_PROXIES`：只有直连对端命中该网段（即"这个请求确实是从我们的反代
    进来的"）才解析 XFF。配置为空 = 谁都不信，XFF 被完全忽略。
    """
    if not is_trusted_peer(peer, trusted):
        return peer
    forwarded = ""
    try:
        forwarded = headers.get("x-forwarded-for") or ""
    except Exception:  # noqa: BLE001 - headers 形态异常时退回直接连接
        return peer
    if forwarded:
        return forwarded.split(",")[0].strip()
    return peer


def _address(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(ip)
    except ValueError:
        return None


def parse_trusted_proxies(raw: str | None) -> tuple[Any, ...]:
    """`AUTH_TRUSTED_PROXIES` → 网段元组。非法条目跳过（配置噪音不该让服务起不来）。"""
    nets: list[Any] = []
    for item in _split_list(raw):
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def is_trusted_peer(peer: str, trusted: Any = ()) -> bool:
    """直连对端是否落在可信代理解析网段内。未配置 = 不信任任何代理。"""
    if not trusted:
        return False
    addr = _address(peer)
    if addr is None:
        return False
    with contextlib.suppress(ValueError):
        return any(addr in net for net in trusted)
    return False


def is_loopback(ip: str) -> bool:
    addr = _address(ip)
    return addr is not None and addr.is_loopback


def auth_required(*, mode: str, ip: str, path: str, exempt: list[str]) -> bool:
    """这个请求是否必须带凭证。

    fail-closed：`on` 档即使一个凭证都没配，也照样要求 —— 那样所有人都会被拒，
    但"配置了认证却因配置不全而静默放行"是更糟的结果。
    """
    normalized = (mode or "off").strip().lower()
    if normalized == "off":
        return False
    if any(path.startswith(prefix) for prefix in exempt):
        return False
    if normalized == "on":
        return True
    if normalized == "auto":
        return not is_loopback(ip)
    # 未知档位按最严处理
    return True


def unauthorized_response() -> tuple[int, dict[str, str], str]:
    """401 响应：带 `WWW-Authenticate` 浏览器才会弹框（否则用户只会看到一个干巴巴的 401）。"""
    return 401, {"WWW-Authenticate": _WWW_AUTHENTICATE}, "Unauthorized"
