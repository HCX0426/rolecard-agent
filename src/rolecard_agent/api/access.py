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

from rolecard_agent.api.auth import is_loopback

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
    # 收件箱与审批的**读侧**：红点计数是界面常驻轮询，批准/拒绝不在这一档。
    ("/api/reachouts", _ANY),
    ("/api/approvals", frozenset({"GET"})),
    # 记忆是"关于我这个人的事实"，用户可看可改可清（设置里的记忆卡）。
    ("/api/settings/memory", _ANY),
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


def allowed(
    path: str, method: str, *, ip: str, authenticated: bool, enforce: bool
) -> bool:
    """这一档是否放行。

    `enforce=False`（`AUTH_MODE=off`）时一律放行 —— 那档的语义就是"这是我本机的单人使用"，
    而且非回环绑定已经被启动护栏拒绝（`cf887c8`），在这里再拦一次等于用一条永远为真的条件
    伪装成安全检查。真正的收紧发生在 `on`/`auto`：操作员端点**必须**是本机来源或已认证身份。
    """
    if not enforce:
        return True
    if classify(path, method) != OPERATOR:
        return True
    return authenticated or is_loopback(ip)
