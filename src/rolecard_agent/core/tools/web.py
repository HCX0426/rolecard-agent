"""联网工具：web_search（搜索）与 web_fetch（读网页正文）。

## 为什么是"直连轮子"而不是 langchain-community

`langchain-community` 官方已宣布 **sunset（不再积极维护）**，langchain 团队的迁移建议
就是改用独立集成包。所以这里直接用三个独立、活跃维护的轮子：

  * 搜索（本地、免 key）：`ddgs`（DuckDuckGo 多后端搜索）
  * 搜索（云端）：`tavily-python`（为 AI 检索设计的搜索 API）
  * 正文抽取：`trafilatura`（网页 → 主文本的事实标准，RAG 项目首选）

`web_search_backend=auto` 的语义与项目既有模式一致（嵌入 / OCR 都是 auto）：
**有 key 走云端、没 key 走本地**——`TAVILY_API_KEY` 存在 → tavily，否则 → ddgs。

## 两个安全边界（这部分没有轮子，必须自己写）

1. **SSRF**：`web_fetch` 的 URL 来自模型（因而也来自网页/文档里的提示注入）。只允许
   http/https，且拒绝回环 / 私网 / 本地域名 —— 否则"读网页"会变成"读内网服务"。
2. **不可信内容**：搜索结果与网页正文都会进入 prompt，等于把提示注入的攻击面从用户消息
   扩大到"模型读到的任何内容"（这是接任何外部知识源的通病，不止 MCP）。缓解：结果带来源
   标注、正文按字符截断、工具只读（idempotent=True，执行器不重试也无副作用）。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx
import trafilatura
from ddgs import DDGS
from langchain_core.tools import tool
from tavily import TavilyClient

from rolecard_agent.core.tools.errors import ToolExecutionError

# 正文给模型的字符上限：再长也只是稀释上下文（上限约束与解析层同一哲学）。
FETCH_MAX_CHARS = 6000
DOWNLOAD_MAX_BYTES = 2 * 1024 * 1024
_UA = "Mozilla/5.0 (compatible; rolecard-agent/0.3)"

# L10：进程级共享 httpx 连接池 —— web_fetch 此前每次新建 Client，握手/TLS 成本白扔。
# httpx.Client 并发请求安全；单请求 timeout / follow_redirects 覆盖默认值。
_HTTP_CLIENT: httpx.Client | None = None


def _http() -> httpx.Client:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.Client(timeout=20.0)
    return _HTTP_CLIENT


class WebToolError(ToolExecutionError):
    """联网工具的可读失败。message 面向操作员，绝不含内部栈。"""


def _host_is_public(url: str) -> bool:
    """只允许公网 http(s)。拒绝 file:// 等 scheme 与回环 / 私网目标（SSRF 边界）。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(parsed.hostname, port)
    except (socket.gaierror, OSError):
        return False
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
            return False
    return True


def _search_ddgs(query: str) -> str:
    # 模块级导入的 DDGS：测试通过 monkeypatch(web, "DDGS", ...) 注入替身。
    raw = DDGS().text(query, max_results=5)
    if not raw:
        return f"没有找到与「{query}」相关的结果。"
    lines = ["（来源：DuckDuckGo，结果为网页摘要，时效与准确性请自行判断）"]
    for i, item in enumerate(raw, 1):
        title = str(item.get("title") or "")[:120]
        href = str(item.get("href") or "")
        body = str(item.get("body") or "")[:300]
        lines.append(f"{i}. {title}\n   链接：{href}\n   摘要：{body}")
    return "\n".join(lines)


def _search_tavily(query: str, api_key: str) -> str:
    raw = TavilyClient(api_key=api_key).search(query, max_results=5)
    results = raw.get("results") or []
    if not results:
        return f"没有找到与「{query}」相关的结果。"
    lines = ["（来源：Tavily，结果为网页摘要，时效与准确性请自行判断）"]
    for i, item in enumerate(results, 1):
        title = str(item.get("title") or "")[:120]
        url = str(item.get("url") or "")
        content = str(item.get("content") or "")[:300]
        lines.append(f"{i}. {title}\n   链接：{url}\n   摘要：{content}")
    return "\n".join(lines)


def make_web_tools(*, settings) -> list:
    """联网工具。`web_search` 永远注册：后端缺失时返回可读的降级说明，
    而不是让白名单引用一个不存在的工具（一致性校验要求声明的工具必须真实存在）。

    两层访问控制（用户 2026-09-17：纯白名单无总闸、纯全局太粗）：
      * 总闸 `web_search_enabled=False` → 两个工具都返回可读的关闭说明；
      * 域名白名单 `web_allowed_domains`（子域匹配）只管 **web_fetch 的抓取目标**——
        搜索源（引擎本身）与搜索行为不受它管；公网边界仍由 _host_is_public 把守。
    """

    def _disabled_note() -> str | None:
        if not settings.web_search_enabled:
            return "联网功能已被管理员关闭（WEB_SEARCH_ENABLED=0）。"
        return None

    def _domain_allowed(url: str) -> bool:
        allowed = [
            d.strip().lower()
            for d in (settings.web_allowed_domains or "").split(",")
            if d.strip()
        ]
        if not allowed:
            return True  # 名单为空 = 不限（公网边界交给 _host_is_public）
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in allowed)

    @tool("web_search")
    def web_search(query: str) -> str:
        """搜索互联网获取时效性信息（新闻、价格、版本动态等）。输入是简洁的搜索关键词。"""
        if note := _disabled_note():
            return note
        backend = (settings.web_search_backend or "auto").lower()
        key = settings.tavily_api_key or ""
        try:
            if backend == "ddgs":
                return _search_ddgs(query)
            if backend == "tavily":
                if not key:
                    return "搜索后端配置为 tavily，但未设置 TAVILY_API_KEY。"
                return _search_tavily(query, key)
            # auto：有 key 走云端，没 key 走本地
            if key:
                return _search_tavily(query, key)
            return _search_ddgs(query)
        except WebToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - 外部搜索服务的失败要变成可读答复
            raise WebToolError(f"搜索失败（{type(exc).__name__}）：{exc}") from exc

    @tool("web_fetch")
    def web_fetch(url: str) -> str:
        """读取一个公开网页的正文（自动去除导航/广告等噪音）。
        输入必须是以 http:// 或 https:// 开头的完整网址。"""
        if note := _disabled_note():
            return note
        if not _host_is_public(url):
            return "已拒绝：只允许读取公网 http/https 网页（内网与本地地址不可访问）。"
        if not _domain_allowed(url):
            return "已拒绝：该域名不在联网白名单内（WEB_ALLOWED_DOMAINS）。"
        try:
            resp = _http().get(
                url,
                timeout=20,
                follow_redirects=True,
                headers={"User-Agent": _UA},
            )
            resp.raise_for_status()
            html = resp.text[:DOWNLOAD_MAX_BYTES]
        except Exception as exc:  # noqa: BLE001 - 网络失败要变成可读答复
            raise WebToolError(f"网页读取失败（{type(exc).__name__}）：{exc}") from exc

        text = trafilatura.extract(html, include_comments=False) or ""
        if not text.strip():
            return f"该网页没有提取到正文（可能是纯脚本渲染或非文章页面）：{url}"
        text = text.strip()
        if len(text) > FETCH_MAX_CHARS:
            text = text[:FETCH_MAX_CHARS] + f"\n…（正文过长，已截断，原文约 {len(text)} 字）"
        return f"（来源：{url}）\n{text}"

    return [web_search, web_fetch]


__all__ = ["WebToolError", "make_web_tools"]
