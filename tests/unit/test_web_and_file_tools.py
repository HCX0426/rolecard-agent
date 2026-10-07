"""联网与工作区工具的测试（core/tools/web.py / files.py）。

两个工具家族的安全件必须有机器守护：
  * web_fetch 的 **SSRF 边界** —— URL 来自模型（因而也来自网页里的提示注入），
    不设边界就是"读内网服务"；
  * fs_* 的 **路径越界边界** —— 与上传路径守卫同一套 rigor（H1）。

搜索后端全部 mock：这些测试验证的是"分派与格式"，不是 DuckDuckGo 的可用性。
"""

from __future__ import annotations

import base64
import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from rolecard_agent.base.scopes import turn_image_ctx
from rolecard_agent.config import Settings
from rolecard_agent.core.tools.files import make_file_tools
from rolecard_agent.core.tools.web import make_web_tools


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(workspace_dir=tmp_path / "workspace", web_search_backend="auto")
    return s


# -- web_search ----------------------------------------------------------------


def test_search_dispatches_to_ddgs_when_no_api_key(settings: Settings, monkeypatch) -> None:
    """auto 模式 + 无 key → 本地 ddgs（免 key 即用的那条路）。"""

    class FakeDDGS:
        def text(self, query: str, max_results: int = 5):
            assert query == "崩坏3 爱莉希雅"
            return [
                {
                    "title": "爱莉希雅 - 萌娘百科",
                    "href": "https://example.com/a",
                    "body": "人之律者",
                },
            ]

    import rolecard_agent.core.tools.web as web

    monkeypatch.setattr(web, "DDGS", FakeDDGS, raising=False)
    (search, _fetch, _img) = make_web_tools(settings=settings)
    out = search.invoke({"query": "崩坏3 爱莉希雅"})
    assert "爱莉希雅" in out and "example.com/a" in out
    assert "DuckDuckGo" in out  # 来源标注（不可信内容必须可溯源）


def test_search_prefers_tavily_when_key_present(settings: Settings, monkeypatch) -> None:
    """auto + 有 key → 云端 tavily 优先（本地/云端都要考虑的那条路）。"""
    settings_with_key = settings.model_copy(update={"tavily_api_key": "tvly-test"})

    class FakeClient:
        def __init__(self, api_key: str) -> None:
            assert api_key == "tvly-test"

        def search(self, query: str, max_results: int = 5):
            return {"results": [{"title": "官方设定", "url": "https://x.io/1", "content": "真我"}]}

    import rolecard_agent.core.tools.web as web

    monkeypatch.setattr(web, "TavilyClient", FakeClient, raising=False)
    (search, _fetch, _img) = make_web_tools(settings=settings_with_key)
    out = search.invoke({"query": "q"})
    assert "Tavily" in out and "真我" in out


def test_search_backend_failure_becomes_readable(settings: Settings, monkeypatch) -> None:
    """搜索服务挂了 → 可读失败（走 WebToolError），不是把栈抛给模型。"""

    class BoomDDGS:
        def text(self, query: str, max_results: int = 5):
            raise ConnectionError("network down")

    import rolecard_agent.core.tools.web as web

    monkeypatch.setattr(web, "DDGS", BoomDDGS, raising=False)
    (search, _fetch, _img) = make_web_tools(settings=settings)
    with pytest.raises(Exception, match="搜索失败"):
        search.invoke({"query": "q"})


# -- web_fetch：行文抽取 / 截断 / 重定向边界 ----------------------------------------
#
# web_fetch 现在是**流式读 + 逐跳校验重定向**（审查报告 P1-3 / P2），所以替身必须支持
# `client.stream(...)` 这个上下文管理器，而不是简单的 `client.get(...)`。


class _FakeStream:
    """httpx 流式响应的最小替身：可当重定向，也可当正文。"""

    def __init__(
        self, *, url: str, status: int = 200, body: bytes = b"", location: str | None = None
    ) -> None:
        self.url = url
        self.status_code = status
        self.headers = {"location": location} if location else {}
        self.encoding = "utf-8"
        self._body = body

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self) -> Iterator[bytes]:
        # 故意切块：代码应当**边读边停**，而不是先整包进内存再截断。
        for i in range(0, len(self._body), 4096):
            yield self._body[i : i + 4096]


class _FakeStreamingClient:
    """按 URL 返回预设响应的 httpx.Client 替身（并记录实际请求过哪些 URL）。"""

    is_closed = False

    def __init__(
        self, responses: dict[str, _FakeStream] | None = None, default: _FakeStream | None = None
    ) -> None:
        self._responses = responses or {}
        self._default = default
        self.requested: list[str] = []

    def stream(self, _method: str, url: str, **kwargs: Any) -> Any:
        self.requested.append(url)
        resp = self._responses.get(url)
        if resp is None:
            if self._default is None:
                # 没预设 = 测试认定不该请求它（例如重定向里的内网那一跳）
                raise AssertionError(f"不该请求这个地址：{url}")
            resp = self._default
        chosen = resp

        @contextlib.contextmanager
        def _cm() -> Iterator[_FakeStream]:
            yield chosen

        return _cm()


# -- web_fetch：SSRF 边界 --------------------------------------------------------


def test_fetch_rejects_non_http_schemes(settings: Settings) -> None:
    (_search, fetch, _img) = make_web_tools(settings=settings)
    out = fetch.invoke({"url": "file:///etc/passwd"})
    assert "已拒绝" in out


def test_fetch_rejects_loopback_and_private_targets(settings: Settings) -> None:
    """SSRF 边界：回环 / 私网目标一律拒绝 —— 否则"读网页"会变成"读内网服务"。"""
    (_search, fetch, _img) = make_web_tools(settings=settings)
    for url in (
        "http://localhost:8000/api/health",
        "http://127.0.0.1:11434/api/tags",
        "http://192.168.1.1/admin",
        "http://10.0.0.5/x",
    ):
        assert "已拒绝" in fetch.invoke({"url": url}), url


# -- P2-5：rebinding 的 TOCTOU 窗口（校验用的解析 == 建连用的解析）------------------

# 这族用例的共同打法：**生产实现整个跑起来**（`_PinningTransport.handle_request` 里的
# 解析、校验、SNI、URL 重写一步不落），只把"真去建连"那一格 —— 基类
# `httpx.HTTPTransport.handle_request` —— 换成记录器。判据本体若被替成假 Transport，
# 测的就不是生产那几行（生产改了形状也不会红），所以换的是最外层的那一步。


def _record_connecting(monkeypatch) -> list[tuple[str, str]]:
    """把基类"建连"那一步换成记录器；返回记下 (重写后 host, sni_hostname) 的 list。"""
    connected: list[tuple[str, str]] = []

    def fake_handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        connected.append((str(request.url.host), str(request.extensions.get("sni_hostname"))))
        return httpx.Response(200, content=b"<html></html>", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle)
    return connected


def test_rebinding_between_check_and_connect_is_blocked(monkeypatch) -> None:
    """**rebinding 的签名形状**：请求前的检查解析出公网、建连时解析出内网 ⇒ 必须拒。

    改前是"各解析一次"：`_host_is_public` 那次答 `93.184.216.34`（放行），httpx 建连
    这次答 `169.254.169.254`（劫持成真）—— 两次之间没有任何约束，60s TTL 的常见配置
    足够翻脸（快照 P2-5 记的 TOCTOU）。收口后校验与建连共用**同一次解析**：Transport
    拿到的那批地址就是建连要用的，第 2 次解析是内网 ⇒ `SsrfBlocked`，压根不建连。

    `getaddrinfo` 换成"第一次公网、之后全内网"的脚本化替身 —— 正是老代码会被劫持的
    那个世界，而被测的是生产 Transport 本体。
    """
    import socket as _socket

    from rolecard_agent.core.tools import web

    answers = iter(
        [
            [(2, 1, 6, "", ("93.184.216.34", 443))],  # 请求前那层：公网，放行
            [(2, 1, 6, "", ("169.254.169.254", 443))],  # 建连时这次：云元数据
        ]
    )
    monkeypatch.setattr(_socket, "getaddrinfo", lambda *_a, **_k: next(answers))
    connected = _record_connecting(monkeypatch)

    # 老代码的终点就在这里：`_host_is_public` 答 True 之后就没人再查过第二次解析。
    assert web._host_is_public("https://evil.example/x") is True, "夹具：请求前那层应当放行"
    with pytest.raises(web.SsrfBlocked):
        web._PinningTransport().handle_request(httpx.Request("GET", "https://evil.example/x"))
    assert connected == [], "被劫持的解析居然走到了建连那一步"


def test_pinned_transport_resolves_once_per_request(monkeypatch) -> None:
    """**窗口消掉的正向证据**：一个请求里 `getaddrinfo` 只被调用一次。

    改前每个请求两次（检查 + 建连各一次）—— 两次之间没人约束，那正是窗口的定义。
    现在校验用的就是建连要用的那批地址，一次解析闭合两件事。这一格不测"拒了什么"，
    测"为什么拒得对"：把解析拆回两次，这格立刻红。
    """
    import socket as _socket

    from rolecard_agent.core.tools import web

    calls: list[str] = []

    def fake(host, port, *_a, **_k):  # noqa: ANN001
        calls.append(str(host))
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(_socket, "getaddrinfo", fake)
    _record_connecting(monkeypatch)

    web._PinningTransport().handle_request(httpx.Request("GET", "https://example.com/elysia"))
    assert calls == ["example.com"], f"一个请求解析了 {len(calls)} 次（老形状=各解析一次）"


def test_pinned_transport_keeps_sni_and_original_host(monkeypatch) -> None:
    """**别修 SSRF 修坏公网**：URL 的 host 换成 IP，但 SNI 与 Host 头仍是原域名。

    TLS 证书校验与 SNI 必须跟着**原始域名**走，否则验的是 IP、CDN 路由与虚拟主机全塌
    —— 这是快照点名的"SNI/Host 保留"。Host 头由 httpx 在建 Request 时按当时的 URL
    生成（Transport 的重写发生在其后），这格把它钉成断言：哪天 httpx 改了生成时机，
    先红在这里。
    """
    import socket as _socket

    from rolecard_agent.core.tools import web

    monkeypatch.setattr(
        _socket,
        "getaddrinfo",
        lambda host, port, *_a, **_k: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    connected = _record_connecting(monkeypatch)

    req = httpx.Request("GET", "https://example.com/elysia")
    web._PinningTransport().handle_request(req)
    assert connected == [("93.184.216.34", "example.com")]
    assert req.headers["host"] == "example.com"


def test_ipv6_pin_leaves_the_bare_literal_to_httpx(monkeypatch) -> None:
    """IPv6 钉的是**裸地址**：httpcore 拿 `.host`/`raw_host` 喂 getaddrinfo（带方括号
    反而解析失败），而 URL 渲染时才由 httpx 自己补上括号 —— 生产别再包一层。

    （夹具选 Cloudflare 的 2606:4700::/32，不是文档段 2001:db8::/32 —— 后者被
    `ipaddress` 归入 private，拿它当"公网 IPv6"会被判据正确地拒掉。）
    """
    import socket as _socket

    from rolecard_agent.core.tools import web

    monkeypatch.setattr(
        _socket,
        "getaddrinfo",
        lambda host, port, *_a, **_k: [(10, 1, 6, "", ("2606:4700:4700::1111", port))],
    )
    connected = _record_connecting(monkeypatch)
    req = httpx.Request("GET", "https://v6.example/x")
    web._PinningTransport().handle_request(req)
    assert connected == [("2606:4700:4700::1111", "v6.example")]
    assert str(req.url) == "https://[2606:4700:4700::1111]/x"  # 渲染由 httpx 补括号


def test_host_is_public_and_transport_share_one_ruler(monkeypatch) -> None:
    """`_host_is_public` 与 Transport **用同一份公网判据**（`_public_addresses`）。

    两份口径迟早漂（本仓"两份实现"的老下场）：同一批"混一个私网"的解析同时喂给请求前
    那层（False）与建连那层（raise）—— 哪天有人只改了一处判据，这格立刻红。
    """
    import socket as _socket

    from rolecard_agent.core.tools import web

    def mixed(*_a, **_k):
        return [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(_socket, "getaddrinfo", mixed)
    assert web._host_is_public("https://multi-a.example") is False
    with pytest.raises(web.SsrfBlocked):
        web._public_addresses("multi-a.example", 443)


def test_ssrf_blocked_surfaces_as_refused_not_generic_failure(settings, monkeypatch) -> None:
    """`SsrfBlocked` 不许被"网络失败"那一族 except 吞成通用文案（安全事件要能读出来）。"""
    import rolecard_agent.core.tools.web as web

    class _Boom:
        is_closed = False  # 共享 client 的 getter 会先问这个（_FakeStreamingClient 同款）

        def stream(self, *_a, **_k):
            raise web.SsrfBlocked("evil.example 解析到了非公网地址（169.254.169.254），已拒绝。")

    monkeypatch.setattr(web, "_host_is_public", lambda url: True)  # 请求前那层放行
    monkeypatch.setattr(web, "_HTTP_CLIENT", _Boom())
    (_search, fetch, _img) = make_web_tools(settings=settings)
    out = fetch.invoke({"url": "https://evil.example/x"})
    assert "已拒绝" in out and "169.254.169.254" in out, out
    assert "网页读取失败" not in out, "把安全边界报成连通性问题，正是住在值里那一族缺陷"



def test_fetch_extracts_main_text(settings: Settings, monkeypatch) -> None:
    import rolecard_agent.core.tools.web as web

    url = "https://example.com/elysia"
    body = (
        "<html><body><article>爱莉希雅是人之律者，逐火十三英桀第二位。</article></body></html>"
    ).encode()

    # L10：web_fetch 走共享 Client（web._http()），patch 共享实例而不是模块函数。
    # 这两个测试测的是抽取/截断逻辑，不是 SSRF 边界（后者有专门测试）——
    # 屏蔽 _host_is_public 的 live DNS 解析，避免网络抖动造成假失败。
    monkeypatch.setattr(web, "_host_is_public", lambda url: True)
    monkeypatch.setattr(
        web, "_HTTP_CLIENT", _FakeStreamingClient({url: _FakeStream(url=url, body=body)})
    )
    monkeypatch.setattr(
        web.trafilatura, "extract", lambda html, **k: "爱莉希雅是人之律者，逐火十三英桀第二位。"
    )
    (_search, fetch, _img) = make_web_tools(settings=settings)
    out = fetch.invoke({"url": "https://example.com/elysia"})
    assert out.startswith("（来源：https://example.com/elysia）")
    assert "人之律者" in out


def test_fetch_truncates_long_pages(settings: Settings, monkeypatch) -> None:
    import rolecard_agent.core.tools.web as web

    url = "https://example.com/long"
    monkeypatch.setattr(web, "_host_is_public", lambda url: True)
    monkeypatch.setattr(
        web,
        "_HTTP_CLIENT",
        _FakeStreamingClient({url: _FakeStream(url=url, body=b"<html></html>")}),
    )
    monkeypatch.setattr(web.trafilatura, "extract", lambda html, **k: "字" * 99999)
    (_search, fetch, _img) = make_web_tools(settings=settings)
    out = fetch.invoke({"url": "https://example.com/long"})
    assert "已截断" in out
    assert len(out) < 99999


def test_fetch_revalidates_every_redirect_hop(settings: Settings, monkeypatch) -> None:
    """P1-3 回归：302 到内网/元数据地址必须被拦住。

    改前用 `follow_redirects=True`：只在**首跳之前**校验过一次目标，于是公网页面
    302 到 `http://127.0.0.1:11434/api/tags` 就能读本机 Ollama（或云元数据服务）。
    """
    import rolecard_agent.core.tools.web as web

    start = "https://evil.example.com/go"
    private = "http://127.0.0.1:11434/api/tags"
    client = _FakeStreamingClient(
        {
            start: _FakeStream(url=start, status=302, location=private),
            # 注意：内网那一跳**故意不预设** —— 一旦代码真的去请求它，替身会直接失败
        }
    )
    monkeypatch.setattr(
        web, "_host_is_public", lambda url: not url.startswith(("http://127.", "http://169.254."))
    )
    monkeypatch.setattr(web, "_HTTP_CLIENT", client)
    (_search, fetch, _img) = make_web_tools(settings=settings)

    out = fetch.invoke({"url": start})

    assert "已拒绝" in out
    assert not any("127.0.0.1" in u for u in client.requested)  # 内网地址根本没被访问


def test_fetch_gives_up_on_a_redirect_loop(settings: Settings, monkeypatch) -> None:
    """跳来跳去的网页要变成一句可读的话，而不是把线程拖死。"""
    import rolecard_agent.core.tools.web as web

    a, b = "https://a.example.com/x", "https://b.example.com/y"
    client = _FakeStreamingClient(
        {
            a: _FakeStream(url=a, status=302, location=b),
            b: _FakeStream(url=b, status=302, location=a),
        }
    )
    monkeypatch.setattr(web, "_host_is_public", lambda url: True)
    monkeypatch.setattr(web, "_HTTP_CLIENT", client)
    (_search, fetch, _img) = make_web_tools(settings=settings)

    with pytest.raises(Exception) as excinfo:
        fetch.invoke({"url": a})

    assert "重定向次数过多" in str(excinfo.value)
    assert len(client.requested) <= web._FETCH_MAX_REDIRECTS + 1


def test_fetch_caps_the_body_by_bytes_while_streaming(settings: Settings, monkeypatch) -> None:
    """P2 回归：字节上限要落在**读取**上，而不是整包读完之后再按字符截断。"""
    import rolecard_agent.core.tools.web as web

    url = "https://example.com/huge"
    seen: dict[str, int] = {}

    def _extract(html: str, **_kwargs: Any) -> str:
        seen["html_len"] = len(html)
        return "正文"

    client = _FakeStreamingClient(
        {url: _FakeStream(url=url, body=b"x" * (web.DOWNLOAD_MAX_BYTES + 500_000))}
    )
    monkeypatch.setattr(web, "_host_is_public", lambda url: True)
    monkeypatch.setattr(web, "_HTTP_CLIENT", client)
    monkeypatch.setattr(web.trafilatura, "extract", _extract)
    (_search, fetch, _img) = make_web_tools(settings=settings)

    fetch.invoke({"url": url})

    assert seen["html_len"] <= web.DOWNLOAD_MAX_BYTES


# -- 工作区文件工具 ---------------------------------------------------------------


def test_fs_write_read_roundtrip(settings: Settings) -> None:
    (fs_read, fs_write, _fs_list) = make_file_tools(settings=settings)
    assert "已写入" in fs_write.invoke({"path": "notes/todo.md", "content": "第一行\n第二行"})
    out = fs_read.invoke({"path": "notes/todo.md"})
    assert "第一行" in out and "todo.md" in out


def test_fs_path_escape_is_rejected(settings: Settings) -> None:
    """**H1 同款边界**：`..` 跳出工作区必须被拦 —— 这是"读写用户磁盘"的守门测试。

    绝对路径探针按平台各给各的语法：`C:/Windows/win.ini` 在 POSIX 语义里**不是**绝对路径，
    只是沙箱里一个名字带冒号的三层子目录（`Path.resolve` 不会把它带出工作区），所以它在
    Linux 上"不被拦"恰恰是正确行为；那边的逃逸语法是 `/etc/passwd`。拦 `..` 的那条规则
    两个平台共用，见前两条探针。
    """
    (fs_read, fs_write, _fs_list) = make_file_tools(settings=settings)
    absolute = "C:/Windows/win.ini" if sys.platform == "win32" else "/etc/passwd"
    for bad in ("../outside.txt", "a/../../escape.txt", absolute):
        assert "路径越界" in fs_read.invoke({"path": bad}), bad
        assert "路径越界" in fs_write.invoke({"path": bad, "content": "x"}), bad


def test_fs_write_creates_missing_dirs_and_lists(settings: Settings) -> None:
    (_read, fs_write, fs_list) = make_file_tools(settings=settings)
    fs_write.invoke({"path": "deep/nested/file.txt", "content": "内容"})
    listing = fs_list.invoke({"path": "deep/nested"})
    assert "file.txt" in listing
    root_listing = fs_list.invoke({"path": "."})
    assert "deep/" in root_listing


def test_fs_read_missing_file_is_readable_error(settings: Settings) -> None:
    (fs_read, _write, _list) = make_file_tools(settings=settings)
    assert "文件不存在" in fs_read.invoke({"path": "没有.txt"})


def test_fs_write_over_limit_is_refunded_without_touching_disk(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超限写入**整笔拒绝**：一行不落盘、不建目录 —— 上限判在落盘之前。

    被注入的角色拿着 fs_write 应当写不出无限大的文件（agent 磁盘面唯一没有
    上限的入口就是它）。用 monkeypatch 把上限缩到 10 字节来测，不为一条用例
    真造 5 MB 字符串；上限值本身在文件里是常量，与读侧的 READ_MAX_CHARS 同风格。
    """
    import rolecard_agent.core.tools.files as files_module

    monkeypatch.setattr(files_module, "WRITE_MAX_BYTES", 10)
    (_read, fs_write, _list) = make_file_tools(settings=settings)
    out = fs_write.invoke({"path": "big/overflow.txt", "content": "这一行超过十个字节"})
    assert "写入被拒绝" in out
    assert not (Path(settings.workspace_dir) / "big").exists()  # 目录都没建

    boundary = fs_write.invoke({"path": "ok.txt", "content": "1234567890"})  # 恰 10 字节
    assert "已写入" in boundary


# -- 分模块地板的靶子（2026-10-07 覆盖率地板刀） -------------------------------------
# 全局 fail_under 管平均，地板管单文件：files.py 曾是全仓最低（84.95%，差 0.05 过 85 线）。
# 下面每条都是"磁盘出错/边界输入时对模型说人话"的路径 —— 平均值看不见它们，地板会。


def test_fs_tools_bind_to_the_resolved_task_dir(settings: Settings, tmp_path: Path) -> None:
    """`dir_resolver` 每次调用实时解析任务目录 —— 两个任务共用一套工具靠它分家。

    不传时回落 settings.workspace_dir（其余用例走的都是那条路）；这条钉的是
    "范围跟着用户授权走"的那条主路径：工具**建成后**目录还能换，写与读都跟着走。
    """
    (fs_read, fs_write, _fs_list) = make_file_tools(
        settings=settings, dir_resolver=lambda: tmp_path / "另一个任务"
    )
    assert "已写入" in fs_write.invoke({"path": "a.txt", "content": "x"})
    assert (tmp_path / "另一个任务" / "a.txt").exists(), "写进了回落目录 —— dir_resolver 没生效"
    assert "文件不存在" in fs_read.invoke({"path": "b.txt"})  # 读也走同一个根


def test_fs_read_garbled_bytes_becomes_a_readable_error(settings: Settings) -> None:
    """读到非法 UTF-8 ⇒ "读取失败：…"，不许裸抛 `UnicodeDecodeError`。

    下载/粘贴来的"文本文件"里混进二进制字节是真实形状；裸异常到了模型眼里只是一串
    traceback，到人眼里是一次 500。
    """
    (fs_read, fs_write, _list) = make_file_tools(settings=settings)
    root = Path(settings.workspace_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "坏.txt").write_bytes(b"\xff\xfe\x00\x89PNG")
    assert "读取失败" in fs_read.invoke({"path": "坏.txt"})


def test_fs_read_long_file_is_truncated_with_a_notice(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超长文件截断必须带"已截断、共约 N 字符"的告示 —— 模型得知道自己看到的不是全文。

    上限用 monkeypatch 缩小（与 `WRITE_MAX_BYTES` 那条同风格），不为一条用例造 20 万字符。
    """
    import rolecard_agent.core.tools.files as files_module

    monkeypatch.setattr(files_module, "READ_MAX_CHARS", 10)
    (fs_read, fs_write, _list) = make_file_tools(settings=settings)
    fs_write.invoke({"path": "long.txt", "content": "一二三四五六七八九十一二三"})
    out = fs_read.invoke({"path": "long.txt"})
    assert "已截断" in out and "共约 13" in out


def test_fs_write_disk_error_is_reported_not_raised(settings: Settings) -> None:
    """父路径本身是个**文件**时 mkdir 必然失败 ⇒ "写入失败：…"，不许裸 OSError 冒出去。

    真实形状：工作目录某一级被手工建成了文件（或上次异常留下的半成品）——
    工具的可读失败是它对模型的全部输出，抛异常等于把执行器打红。
    """
    (_read, fs_write, _list) = make_file_tools(settings=settings)
    root = Path(settings.workspace_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "挡路.txt").write_text("我是文件", encoding="utf-8")
    assert "写入失败" in fs_write.invoke({"path": "挡路.txt/深层.txt", "content": "x"})


def test_fs_list_reports_escape_a_file_and_an_empty_dir(settings: Settings) -> None:
    """`fs_list` 三支可读失败各是各的话：越界 / 指向文件 / 空目录。

    "指向文件"必须说"目录不存在"而不是返回空列表（空列表 = 模型以为里面没东西）；
    空目录要有显式告示，别与"出错"混成一个形状。
    """
    (fs_read, fs_write, fs_list) = make_file_tools(settings=settings)
    assert "路径越界" in fs_list.invoke({"path": "../"})
    fs_write.invoke({"path": "单文件.txt", "content": "x"})
    assert "目录不存在" in fs_list.invoke({"path": "单文件.txt"})
    root = Path(settings.workspace_dir)
    (root / "空目录").mkdir(parents=True, exist_ok=True)
    assert "（空目录）" in fs_list.invoke({"path": "空目录"})


# -- 联网总闸 + 域名白名单（用户 2026-09-17 开工的功能①） --------------------------


def test_web_master_switch_disables_both_tools() -> None:
    """总闸：WEB_SEARCH_ENABLED=0 → web_search / web_fetch 都返回可读关闭说明。"""
    s = Settings(web_search_enabled=False, web_search_backend="auto")
    (search, fetch, _img) = make_web_tools(settings=s)
    assert "已被管理员关闭" in search.invoke({"query": "x"})
    assert "已被管理员关闭" in fetch.invoke({"url": "https://example.com/"})


def test_fetch_domain_whitelist(settings: Settings, monkeypatch) -> None:
    """白名单（子域匹配）：名单外域名拒绝，名单内/子域放行；空名单 = 不限。"""
    import rolecard_agent.core.tools.web as web

    monkeypatch.setattr(web, "_host_is_public", lambda url: True)  # 本测试只测白名单层
    monkeypatch.setattr(
        web,
        "_HTTP_CLIENT",
        # 本测试试多个域名（含子域），所以让替身对任意 URL 都回同一份正文。
        _FakeStreamingClient(
            default=_FakeStream(
                url="",
                body="<html><body><article>正文</article></body></html>".encode(),
            )
        ),
    )
    monkeypatch.setattr(web.trafilatura, "extract", lambda html, **k: "正文")
    s = settings.model_copy(update={"web_allowed_domains": "example.com"})
    (_search, fetch, _img) = make_web_tools(settings=s)

    assert "已拒绝：该域名不在联网白名单内" in fetch.invoke({"url": "https://other.org/a"})
    assert "正文" in fetch.invoke({"url": "https://example.com/a"})
    assert "正文" in fetch.invoke({"url": "https://www.example.com/a"})  # 子域匹配

    # 空名单 = 不限（回到公网边界单层把关）
    s_open = settings.model_copy(update={"web_allowed_domains": ""})
    (_search, fetch_open, _img) = make_web_tools(settings=s_open)
    assert "正文" in fetch_open.invoke({"url": "https://anything.org/a"})


# -- image_search（SauceNAO 反向图搜：看图认角色） ------------------------------------

_IMG_URL = "data:image/png;base64," + base64.b64encode(b"fake-png-bytes").decode()


def test_image_search_no_key_returns_readable(settings) -> None:
    """配置了图但没配 SAUCENAO_API_KEY → 可读的未配置提示，绝不静默上传。"""
    tok = turn_image_ctx.set(_IMG_URL)
    try:
        (_s, _f, img) = make_web_tools(settings=settings)  # 无 saucenao_api_key
        out = img.invoke({})
        assert "未配置" in out and "SAUCENAO_API_KEY" in out
    finally:
        turn_image_ctx.reset(tok)


def test_image_search_no_turn_image_returns_readable(settings) -> None:
    """有 key 但本轮没有图 → 可读说明（工具只能搜当前对话里的图）。"""
    tok = turn_image_ctx.set(None)
    try:
        s = settings.model_copy(update={"saucenao_api_key": "k"})
        (_s, _f, img) = make_web_tools(settings=s)
        assert "没有可检索的图片" in img.invoke({})
    finally:
        turn_image_ctx.reset(tok)


def test_image_search_disabled_by_master_gate(settings) -> None:
    """web_search_enabled=0 总闸关掉 image_search（与 web_search 同一道闸）。"""
    s = settings.model_copy(update={"web_search_enabled": False, "saucenao_api_key": "k"})
    (_s, _f, img) = make_web_tools(settings=s)
    assert "关闭" in img.invoke({})


def test_image_search_formats_saucenao_results(settings, monkeypatch) -> None:
    """命中 SauceNAO → 相似度 / 作品 / 角色 / 来源都要出现在返回里（供模型据此作答）。"""
    import rolecard_agent.core.tools.web as web

    class FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "header": {"status": 0},
                "results": [
                    {
                        "header": {"similarity": 92.0},
                        "data": {
                            "part": "异环",
                            "material": ["女主角A", "路人B"],
                            "ext_urls": ["https://pixiv.net/1"],
                            "title": "某插画",
                        },
                    }
                ],
            }

    class FakeClient:
        def post(self, *a: Any, **k: Any) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr(web, "_http", lambda: FakeClient())
    tok = turn_image_ctx.set(_IMG_URL)
    try:
        s = settings.model_copy(update={"saucenao_api_key": "k"})
        (_s, _f, img) = make_web_tools(settings=s)
        out = img.invoke({})
        assert "相似度 92" in out and "异环" in out and "女主角A" in out and "pixiv.net/1" in out
    finally:
        turn_image_ctx.reset(tok)


def test_image_search_rate_limited_status_is_readable(settings, monkeypatch) -> None:
    """SauceNAO header.status != 0（多为限流）→ 可读说明，不抛异常。"""
    import rolecard_agent.core.tools.web as web

    class FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"header": {"status": -1, "e_msg": "Rate limiting"}}

    class FakeClient:
        def post(self, *a: Any, **k: Any) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr(web, "_http", lambda: FakeClient())
    tok = turn_image_ctx.set(_IMG_URL)
    try:
        s = settings.model_copy(update={"saucenao_api_key": "k"})
        (_s, _f, img) = make_web_tools(settings=s)
        out = img.invoke({})
        assert "检索未成功" in out and "限流" in out
    finally:
        turn_image_ctx.reset(tok)


def test_decode_data_url_rejects_bad_input() -> None:
    import rolecard_agent.core.tools.web as web

    with pytest.raises(ValueError):
        web._decode_data_url("https://example.com/a.png")  # 非 data URL
    with pytest.raises(ValueError):
        web._decode_data_url("data:image/png,notbase64")  # 缺 base64 标记
