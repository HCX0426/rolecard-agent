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
