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
"""

from __future__ import annotations

import base64
import binascii
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


def client_ip(headers: Any) -> str:
    """真实来源 IP：反代后面 `request.client.host` 是反代自己，必须看 `X-Forwarded-For`。

    只取链路上第一个（最原始的客户端）；伪造这个头在"反代会覆写它"的前提下才可信 ——
    这也是为什么 `auto` 只作为便利档，真正的公网部署应当显式设 `on`。
    """
    forwarded = ""
    try:
        forwarded = headers.get("x-forwarded-for") or ""
    except Exception:  # noqa: BLE001 - headers 形态异常时退回直接连接
        forwarded = ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    return ""


def _is_loopback(ip: str) -> bool:
    if not ip:
        return False
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


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
        return not _is_loopback(ip)
    # 未知档位按最严处理
    return True


def unauthorized_response() -> tuple[int, dict[str, str], str]:
    """401 响应：带 `WWW-Authenticate` 浏览器才会弹框（否则用户只会看到一个干巴巴的 401）。"""
    return 401, {"WWW-Authenticate": _WWW_AUTHENTICATE}, "Unauthorized"
