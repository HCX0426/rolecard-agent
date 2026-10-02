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

## 凭证分族：同一条列表里区分「使用者」与「操作员」

`Actor.role` 取自凭据串上的 `operator:` 前缀（见 `OPERATOR_PREFIX`）。它只为一件事服务：
`access.py` 那张表里判为 operator 的端点（改配置、动本机、看审计）从此不接受"任何带了凭证的人"，
而只接受操作员凭据或本机来源。**没有声明任何 operator 凭据时这套区分关闭**（`roles_declared`）
—— 否则 `AUTH_MODE=on` + 一条 `alice:pw` 的老配置会在升级后把主人锁在自己的设置页外。

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
from urllib.parse import urlsplit

from rolecard_agent.config import Settings

ANONYMOUS_KIND = "anonymous"
BASIC_KIND = "basic"
APIKEY_KIND = "apikey"

# 凭证分族（P0-3 第三步）。两档角色就对应 `access.py` 的两个门槛：使用者能用自己的界面，
# 操作员才能动配置与本机。为什么不做成 RBAC 矩阵：这产品没有第二个租户，也没有第三种人；
# 多一档只会让"谁该被挡在外面"这件事重新变成没有答案的问题。
ROLE_USER = "user"
ROLE_OPERATOR = "operator"

#: 凭据串上的操作员标记：Basic 写 `operator:bob:pw`，API Key 写 `operator:sk-...`。
#: 为什么用前缀而不是再加一个 env 字段：`AUTH_CREDENTIALS`/`AUTH_API_KEYS` 已经是
#: "五处必须同步"的配置契约（不变式 10），加字段要多维护四处，前缀只改一处解析。
OPERATOR_PREFIX = "operator:"

_WWW_AUTHENTICATE = 'Basic realm="rolecard-agent", charset="UTF-8"'


@dataclass(frozen=True, slots=True)
class Actor:
    """一次请求的调用方身份。

    `id` 会写进审计日志，所以**绝不能是完整凭证**：Basic 记用户名，API Key 只记前 4 位
    （够你在日志里区分是哪个 key，又不足以让人拿去用）。

    `role` 是**凭据自带的族**（见 `OPERATOR_PREFIX`）。匿名身份是 user —— 它能不能进任何
    门由 `auth_required` 决定，而不是靠角色。
    """

    id: str = "anonymous"
    kind: str = ANONYMOUS_KIND
    role: str = ROLE_USER

    @property
    def is_anonymous(self) -> bool:
        return self.kind == ANONYMOUS_KIND


def _split_list(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _entry_role(entry: str) -> tuple[str, str]:
    """拆 `operator:` 前缀 → (角色, 去掉前缀后的凭据串)。"""
    if entry.lower().startswith(OPERATOR_PREFIX):
        return ROLE_OPERATOR, entry[len(OPERATOR_PREFIX):].strip()
    return ROLE_USER, entry


def roles_declared(settings: Settings) -> bool:
    """这套配置里**有没有**任何一条操作员凭据 —— 决定角色要不要真的生效。

    一条都没有 = 这个部署没打算区分两种人：所有已认证身份都按操作员看待。这条兼容规则是
    必须的，否则 `AUTH_MODE=on` + 一条 `alice:pw` 的老配置在升级后会把人锁在自己的设置页
    外 —— 单人自用是本产品的主形态，"改安全逻辑改到主人进不来"是这次改动最不该发生的失败。
    一旦写下第一条 `operator:`，分族立刻生效，没标记的凭据就只算使用者。
    """
    entries = [*_split_list(settings.auth_credentials), *_split_list(settings.auth_api_keys)]
    return any(entry.lower().startswith(OPERATOR_PREFIX) for entry in entries)


def _eq(a: str, b: str) -> bool:
    """常量时间比两个字符串 —— **必须按 UTF-8 字节比**，不能把 str 直接递给
    `hmac.compare_digest`：它对含非 ASCII 的字符串直接抛
    `TypeError: comparing strings with non-ASCII characters is not supported`。
    症状不是"登录失败"而是 **HTTP 500**（M5 的双实例端到端第一次跑就撞上了：
    弹层里填一个中文口令，对面日志里一条 401 都没有，只有 traceback）。
    中文口令与中文账号都是合法输入，"错"与"抛"必须分得开。
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _match_api_key(candidate: str, allowed: list[str]) -> tuple[str, str] | None:
    """常量时间比对。命中返回 (可记录的 key 前缀, 角色)。"""
    for entry in allowed:
        role, key = _entry_role(entry)
        if key and _eq(candidate, key):
            return key[:4], role
    return None


def basic_header(user: str, secret: str) -> str:
    """造一个 `Authorization: Basic …` 头（上行同步拿它去敲对面的门）。

    与 `_parse_basic` 住在同一个文件是刻意的：两边对『user:secret 怎么变成一字节串』
    必须同一条规则 —— 非 ASCII 的账号在浏览器侧走 TextEncoder，这里也必须 UTF-8，
    否则就会出现『界面里登得进去、后端去敲门被拒』这种两头都自证清白的死局。
    """
    token = base64.b64encode(f"{user}:{secret}".encode()).decode("ascii")
    return f"Basic {token}"


def _parse_basic(header: str, credentials: list[str]) -> Actor | None:
    """`Authorization: Basic base64(user:pass)` —— 与 `auth_credentials` 里的 "user:pass" 比对。

    操作员凭据写成 `operator:bob:pw`（三段）：开头那段是族标记，剩下的仍然是 `user:pass`。
    """
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
        role, rest = _entry_role(entry)
        want_user, want_sep, want_pass = rest.partition(":")
        if not want_sep:
            continue
        if _eq(user, want_user) and _eq(password, want_pass):
            return Actor(id=user, kind=BASIC_KIND, role=role)
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
        matched = _match_api_key(api_key, keys)
        if matched is not None:
            prefix, role = matched
            return Actor(id=f"key:{prefix}", kind=APIKEY_KIND, role=role)
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


def unauthorized_response(*, challenge: bool = True) -> tuple[int, dict[str, str], str]:
    """401 响应：带 `WWW-Authenticate` 浏览器才会弹框（否则用户只会看到一个干巴巴的 401）。

    `challenge=False` 那一支是给"**客户端已经带了凭据**"的：那枚头的全部意义是"你可以
    重试并附上凭据"，而它已经附了 —— 再弹一次框只会盖住界面自己那句"账号或密码不对"。
    真实代价（M5 双实例端到端量到的）：`fetch` 拿到带 challenge 的 401 时 Chrome 会去
    弹那个原生框，headless 下那个请求**永远不返回**，界面上就是"连接中…"卡死；
    有头下则是用户在自己刚填过的框上再叠一层浏览器弹框。
    """
    headers = {"WWW-Authenticate": _WWW_AUTHENTICATE} if challenge else {}
    return 401, headers, "Unauthorized"


# ---------------------------------------------------------------- 来源标识护栏（R102-45）

#: 回环主机名（端口一律剥掉再比）。两个用法共用这一份，避免"回环叫什么"出现第二处答案：
#:   * off 档下 Host 头允许的值（`origin_guard_violation`）—— 合法客户端（壳/控制台/脚本/
#:     探针/健康检查）全部以 `127.0.0.1` / `localhost` / `[::1]` 进来，rebinding 浏览器发的
#:     Host 是攻击者域名；
#:   * 出站目标的默认放行面（`access.outbound_target_allowed`，`R102-58`）—— `AUTH_MODE`
#:     开启后，user 档只许把数据推给回环或 operator 允许清单里的地址。
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def hostname_of_netloc(netloc: str) -> str:
    """`127.0.0.1:8000` → `127.0.0.1`；`[::1]:8000` → `::1`（IPv6 的方括号形态）。"""
    if netloc.startswith("["):
        return netloc[1 : netloc.index("]")] if "]" in netloc else netloc.lstrip("[")
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


def origin_guard_violation(
    *,
    host_header: str | None,
    origin: str | None,
    sec_fetch_site: str | None,
    auth_mode: str,
) -> str | None:
    """来源标识护栏的判据：返回拒绝理由，`None` = 放行（`R102-45`）。

    认证（`auth_required`）与 `client_ip` 验的是"带没带凭据 / 从哪连的"；这一层补的是
    "**来源是谁**"。两层判定：

    ① **Host 只在本机形态（off 档）校验**：rebinding 的浏览器把攻击者域名解析到 127.0.0.1，
       发出的 Host 就是那个域名 —— 合法本机客户端的 Host 恒为回环名，两者一比即分。
       on/auto 档跳过：那两档 Host 本来就可能是公网域名，边界是认证本身，不是 Host。
    ② **Origin 全档校验**：带 `Origin` 的请求必须同源（origin 的主机名 == Host 的主机名）。
       跨源盲 POST 的 Host 是对的（就是 127.0.0.1:8000），能挡它的只有 Origin。
       `Origin: null`（file:// 壳的两扇窗是合法的 null）放行，但 `Sec-Fetch-Site: cross-site`
       在场时仍拒 —— 沙箱 iframe 伪造的假 null 恒带这个头（Chromium 保证），合法壳不带。
    """
    host_name = hostname_of_netloc(host_header).lower() if host_header else None
    if auth_mode == "off" and host_name and host_name not in LOOPBACK_HOSTS:
        return (
            f"Forbidden: 本机服务只认本机来源 —— Host「{host_name}」不是回环地址。"
            "请用 http://127.0.0.1 访问（见架构总览 §6「单机形态的可选硬化」）。"
        )
    if origin:
        if origin.strip().lower() == "null":
            if (sec_fetch_site or "").strip().lower() == "cross-site":
                return "Forbidden: 跨源请求被拒绝（伪造的空 Origin）。"
            return None
        origin_host = hostname_of_netloc(urlsplit(origin.strip()).netloc).lower()
        if host_name and origin_host != host_name:
            return f"Forbidden: 跨源请求被拒绝 —— Origin「{origin}」不属于本服务。"
    return None
