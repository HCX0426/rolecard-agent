"""路由的访问分级（P0-3 第三步的前半）：哪条端点属于"操作员面"，一张清单说清。

## 为什么要有这张表

以前"哪些端点能影响主机"这个问题**没有答案**：`local_service.py` 里那个 `require_loopback`
是一个路由文件自带的局部约定，既不是共享词汇，也没人保证新端点会表态。审计 P0-3 的
"operator 鉴权未做"说的就是这个缺口。

## 三条规则，顺序即优先级

1. `PUBLIC_EXACT` 命中 = **public**（探活这类必须免鉴权的）；
2. `USER_ROUTES` 命中 = **user**（使用者在自己界面上的日常动作）；
3. 非 `/api/` 前缀 = **public**（控制台页面与静态资源；它们由认证中间件按 `AUTH_MODE` 整体管，
   这里不重复设卡）—— 但 `/docs`·`/openapi.json`·`/redoc` 显式排除在豁免之外：
   它们把整个攻击面打印给人看。
4. **其余一律 operator**。默认最严是刻意的：想让一条端点变得**更宽松**必须在下面写一行，
   忘了表态的后果是"被当成操作员端点"，而不是"被当成随便谁都能碰"。

与 `tests/unit/test_route_access.py` 配对：那条用例会枚举运行时路由，断言清单里
**没有一条是死的**（前缀写错/端点改名都会红），并把"未表态 = operator"钉成断言。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from rolecard_agent.api.auth import (
    LOOPBACK_HOSTS,
    ROLE_OPERATOR,
    ROLE_USER,
    is_loopback,
    split_exempt_entry,
)

PUBLIC = "public"
USER = "user"
OPERATOR = "operator"

#: 必须免鉴权的探活端点（容器 healthcheck / 反代；也在默认 `AUTH_EXEMPT_PATHS` 里）。
PUBLIC_EXACT: tuple[str, ...] = ("/api/health",)

#: 会公开整个端点结构与参数模型的文档路由 —— 它们不在 `/api/` 下，但**不**算 public。
DOC_PATHS: tuple[str, ...] = ("/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc")

_ANY = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

#: 使用者的日常动作。**逐条写明为什么在这一档**，因为这张表就是产品边界的说明书。
USER_ROUTES: tuple[tuple[str, frozenset[str]], ...] = (
    # 对话链路本身：开会话、发消息、流式、改/删单条消息、上传附件、看上下文占用。
    ("/api/chat", _ANY),
    ("/api/prompt/enhance", _ANY),
    ("/api/session", _ANY),
    ("/api/sessions", _ANY),
    # 自己的数据：报告/索引/域记录/知识库与检索质量指标。
    ("/api/records", _ANY),
    ("/api/domains", _ANY),
    ("/api/knowledge", _ANY),
    ("/api/uploads", _ANY),
    ("/api/rag/metrics", _ANY),
    # 角色卡与插件目录：建角色、改角色是这产品的日常用法（插件启停在下面另论）。
    ("/api/roles", _ANY),
    ("/api/plugins", frozenset({"GET"})),
    ("/api/tools/catalog", _ANY),
    # 桌宠形象包的清单与素材（09-30）：只读两条，且**必须表态** —— 不写这一行它默认算
    # operator，那么开着鉴权的远端实例上，桌宠那只形象会直接退化成 SVG 兜底（图取不到），
    # 症状长得像"素材坏了"而不是"这条端点没表态"。
    ("/api/pets", frozenset({"GET"})),
    # 收件箱与审批的**读侧**：红点计数是界面常驻轮询，批准/拒绝不在这一档。
    ("/api/reachouts", _ANY),
    ("/api/approvals", frozenset({"GET"})),
    # 记忆是"关于我这个人的事实"，用户可看可改可清（设置里的记忆卡）。
    ("/api/settings/memory", _ANY),
    # 上行同步（M7）：清单/计划/导入三条都是"把自己的数据搬给自己在另一台的账号"，
    # 写入侧盖的是**对面认出的那个人**的章，所以它是使用者动作，不是操作员动作。
    # 反过来说，它比一般使用者端点多了"让本机去敲一个用户给的地址"这一步 —— 地址要过
    # `validate_base_url`，凭据只在请求内存活、不进审计与日志（见 routers/sync.py 的三条口径）。
    ("/api/sync", _ANY),
    # 模型清单与供应商模板：对话页的模型菜单要读它，不读就没法换模型。
    ("/api/settings/models", frozenset({"GET"})),
    ("/api/settings/model-providers", _ANY),
)

# **不**在清单里的都是 operator，包括这些值得点名的：
#   · `/api/settings/runtime`（覆盖配置、保存即热重建）、`/api/settings/models` 的 PUT（凭据）
#   · `/api/services/**`（后端选型与凭据引用）、`/api/mcp/**`（接入外部 server = operator 动作）
#   · `/api/plugins/{id}/toggle`（能力开关，绝不做成 LLM/匿名可调）
#   · `/api/workspace/dir`·`/api/workspace/tree`（授权边界与全盘浏览）、`/api/audit`（管理面）
#   · `/api/approvals/{id}/decide`（拍板命令执行；另有 b 那次加的一次性决定令牌）
#   · `/api/local-service/**`（显存与本机推理服务；另有 `require_loopback` 叠加）
#   · `/api/shell-release/**`（往外发可执行文件，不给匿名下载）
#   · `/api/audit`（谁在什么时候动过什么 —— 运维视角）


def classify(path: str, method: str = "GET") -> str:
    """这条 (method, path) 属于哪一档。未知的一律 operator。"""
    if path in PUBLIC_EXACT:
        return PUBLIC
    for prefix, methods in USER_ROUTES:
        if method in methods and (path == prefix or path.startswith(f"{prefix}/")):
            return USER
    if path.startswith(DOC_PATHS):
        return OPERATOR
    if not path.startswith("/api/"):
        return PUBLIC  # 控制台页面与静态资源
    return OPERATOR


def operator_level_exempts(entries: list[str]) -> list[str]:
    """`AUTH_EXEMPT_PATHS` 里命中**操作员级**端点的那几条（原样返回，供启动告警点名）。

    豁免是"匿名可达"的显式让步，让步让到管理面上必须**大声说**：这条检查在启动时
    跑一遍名单，命中 operator 档（按 `classify` 的口径，非回环来源必须持 operator
    凭据的那些）就把条目交回去打告警 —— 报警不拦，宽豁免还有 403 分级层兜着。
    """
    hits: list[str] = []
    for entry in entries:
        _, path = split_exempt_entry(entry)
        if classify(path) == OPERATOR:
            hits.append(entry)
    return hits


def allowed(
    path: str,
    method: str,
    *,
    ip: str,
    authenticated: bool,
    enforce: bool,
    role: str = ROLE_USER,
    roles_in_effect: bool = False,
) -> bool:
    """这一档是否放行。

    `enforce=False`（`AUTH_MODE=off`）时一律放行 —— 那档的语义就是"这是我本机的单人使用"，
    而且非回环绑定已经被启动护栏拒绝（`cf887c8`），在这里再拦一次等于用一条永远为真的条件
    伪装成安全检查。真正的收紧发生在 `on`/`auto`：操作员端点要 **本机来源** 或
    **操作员凭据**。

    两条判定的分工要说清，别以为重复：
      * **本机来源**始终放行 —— 桌面壳与本机界面就走 127.0.0.1，owner 坐在键盘前不需要凭据；
        这次要关的洞从来不是"本机能碰本机"，而是"远端/局域网里任何带凭证的人"。
      * **带凭证**不再自动等于操作员：`roles_in_effect` 为真（配置里出现了 `operator:` 凭据）
        时，只有 operator 族的凭据进得了管理面；使用者凭据可以在自己界面上聊天、改数据、
        看收件箱，动配置就是 403。
    """
    if not enforce:
        return True
    if classify(path, method) != OPERATOR:
        return True
    if is_loopback(ip):
        return True
    if not authenticated:
        return False
    return (not roles_in_effect) or role == ROLE_OPERATOR


# ---------------------------------------------------------------- 出站目标（R102-58）


def allowed_hosts(raw: str) -> frozenset[str]:
    """允许清单原文 → 主机名集合（逗号分隔，可写 `host` 或 `host:端口`，一律只取主机名）。

    端口不参与判定：判的是"数据该不该去这台机器"，而端口是它可以随便换的。写成
    `example.com:8443` 与 `example.com` 是同一台 —— 若把端口算进判据，运营方改个端口
    就会静默失效（`R102-42` 那一族"知识只写了一半调用点"）。
    """
    out: set[str] = set()
    for entry in (raw or "").split(","):
        text = entry.strip()
        if not text:
            continue
        # `urlsplit` 对**裸主机名**不认（`example.com` 会被当成 path），补 `//` 让它按 netloc
        # 解析；已经带了 scheme 的整段 URL（`https://example.com`）必须原样解，否则
        # `//https://example.com` 的 netloc 是 "https:" —— 主机名会变成协议名。
        host = (urlsplit(text if "://" in text else f"//{text}").hostname or "").strip().lower()
        if host:
            out.add(host)
    return frozenset(out)


def outbound_target_allowed(
    url: str,
    *,
    ip: str,
    authenticated: bool,
    enforce: bool,
    role: str = ROLE_USER,
    roles_in_effect: bool = False,
    allowlist: str = "",
) -> bool:
    """这条**出站**目标这一档能不能去敲（`R102-58`）。

    受管的是"把本机这份数据推到某个地址"那几条（`/api/sync/*`）—— 它们属 **user 档**
    （见 `USER_ROUTES` 的自述："多了一样让本机去敲一个用户给的地址"），于是从前**任意公网
    http/https 地址都收**：多凭据部署里，持 user 凭据的人可以把该身份的记忆与会话全量推到
    自己控制的服务器。判据收在这里而不是散在路由里：`AUTH_MODE=off` 一律放行（那一档的
    语义就是"我本机单人用"，与 `allowed()` 同一条信任模型，单机形态零感知）；开启认证后
    按档算 —— 回环目标或落在 operator 允许清单里 → 放行；否则只有**操作员这一档**
    （本机来源 / operator 凭据）能敲。

    地址本身能不能用（scheme、空值）仍归 `validate_base_url`，这里只管"谁有资格敲它"。
    """
    if not enforce:
        return True
    host = (urlsplit(url).hostname or "").strip().lower()
    if not host:
        return False
    if host in LOOPBACK_HOSTS or host in allowed_hosts(allowlist):
        return True
    if is_loopback(ip):
        return True
    if not authenticated:
        return False
    return (not roles_in_effect) or role == ROLE_OPERATOR
