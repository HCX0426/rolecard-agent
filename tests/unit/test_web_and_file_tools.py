"""联网与工作区工具的测试（core/tools/web.py / files.py）。

两个工具家族的安全件必须有机器守护：
  * web_fetch 的 **SSRF 边界** —— URL 来自模型（因而也来自网页里的提示注入），
    不设边界就是"读内网服务"；
  * fs_* 的 **路径越界边界** —— 与上传路径守卫同一套 rigor（H1）。

搜索后端全部 mock：这些测试验证的是"分派与格式"，不是 DuckDuckGo 的可用性。
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
    (search, _fetch) = make_web_tools(settings=settings)
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
    (search, _fetch) = make_web_tools(settings=settings_with_key)
    out = search.invoke({"query": "q"})
    assert "Tavily" in out and "真我" in out


def test_search_backend_failure_becomes_readable(settings: Settings, monkeypatch) -> None:
    """搜索服务挂了 → 可读失败（走 WebToolError），不是把栈抛给模型。"""

    class BoomDDGS:
        def text(self, query: str, max_results: int = 5):
            raise ConnectionError("network down")

    import rolecard_agent.core.tools.web as web

    monkeypatch.setattr(web, "DDGS", BoomDDGS, raising=False)
    (search, _fetch) = make_web_tools(settings=settings)
    with pytest.raises(Exception, match="搜索失败"):
        search.invoke({"query": "q"})


# -- web_fetch：SSRF 边界 --------------------------------------------------------


def test_fetch_rejects_non_http_schemes(settings: Settings) -> None:
    (_search, fetch) = make_web_tools(settings=settings)
    out = fetch.invoke({"url": "file:///etc/passwd"})
    assert "已拒绝" in out


def test_fetch_rejects_loopback_and_private_targets(settings: Settings) -> None:
    """SSRF 边界：回环 / 私网目标一律拒绝 —— 否则"读网页"会变成"读内网服务"。"""
    (_search, fetch) = make_web_tools(settings=settings)
    for url in (
        "http://localhost:8000/api/health",
        "http://127.0.0.1:11434/api/tags",
        "http://192.168.1.1/admin",
        "http://10.0.0.5/x",
    ):
        assert "已拒绝" in fetch.invoke({"url": url}), url


def test_fetch_extracts_main_text(settings: Settings, monkeypatch) -> None:
    import rolecard_agent.core.tools.web as web

    class FakeResp:
        text = (
            "<html><body><article>爱莉希雅是人之律者，"
            "逐火十三英桀第二位。</article></body></html>"
        )

        def raise_for_status(self) -> None:
            return None

    # L10：web_fetch 走共享 Client（web._http()），patch 共享实例而不是模块函数。
    class FakeClient:
        is_closed = False

        def get(self, *a, **k):
            return FakeResp()

    # 这两个测试测的是抽取/截断逻辑，不是 SSRF 边界（后者有专门测试）——
    # 屏蔽 _host_is_public 的 live DNS 解析，避免网络抖动造成假失败。
    monkeypatch.setattr(web, "_host_is_public", lambda url: True)
    monkeypatch.setattr(web, "_HTTP_CLIENT", FakeClient())
    monkeypatch.setattr(
        web.trafilatura, "extract", lambda html, **k: "爱莉希雅是人之律者，逐火十三英桀第二位。"
    )
    (_search, fetch) = make_web_tools(settings=settings)
    out = fetch.invoke({"url": "https://example.com/elysia"})
    assert out.startswith("（来源：https://example.com/elysia）")
    assert "人之律者" in out


def test_fetch_truncates_long_pages(settings: Settings, monkeypatch) -> None:
    import rolecard_agent.core.tools.web as web

    class FakeResp:
        text = "<html></html>"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        is_closed = False

        def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(web, "_host_is_public", lambda url: True)
    monkeypatch.setattr(web, "_HTTP_CLIENT", FakeClient())
    monkeypatch.setattr(web.trafilatura, "extract", lambda html, **k: "字" * 99999)
    (_search, fetch) = make_web_tools(settings=settings)
    out = fetch.invoke({"url": "https://example.com/long"})
    assert "已截断" in out
    assert len(out) < 99999


# -- 工作区文件工具 ---------------------------------------------------------------


def test_fs_write_read_roundtrip(settings: Settings) -> None:
    (fs_read, fs_write, _fs_list) = make_file_tools(settings=settings)
    assert "已写入" in fs_write.invoke({"path": "notes/todo.md", "content": "第一行\n第二行"})
    out = fs_read.invoke({"path": "notes/todo.md"})
    assert "第一行" in out and "todo.md" in out


def test_fs_path_escape_is_rejected(settings: Settings) -> None:
    """**H1 同款边界**：`..` 跳出工作区必须被拦 —— 这是"读写用户磁盘"的守门测试。"""
    (fs_read, fs_write, _fs_list) = make_file_tools(settings=settings)
    for bad in ("../outside.txt", "a/../../escape.txt", "C:/Windows/win.ini"):
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


# -- 联网总闸 + 域名白名单（用户 2026-09-17 开工的功能①） --------------------------


def test_web_master_switch_disables_both_tools() -> None:
    """总闸：WEB_SEARCH_ENABLED=0 → web_search / web_fetch 都返回可读关闭说明。"""
    s = Settings(web_search_enabled=False, web_search_backend="auto")
    (search, fetch) = make_web_tools(settings=s)
    assert "已被管理员关闭" in search.invoke({"query": "x"})
    assert "已被管理员关闭" in fetch.invoke({"url": "https://example.com/"})


def test_fetch_domain_whitelist(settings: Settings, monkeypatch) -> None:
    """白名单（子域匹配）：名单外域名拒绝，名单内/子域放行；空名单 = 不限。"""
    import rolecard_agent.core.tools.web as web

    class FakeResp:
        text = "<html><body><article>正文</article></body></html>"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        is_closed = False

        def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(web, "_host_is_public", lambda url: True)  # 本测试只测白名单层
    monkeypatch.setattr(web, "_HTTP_CLIENT", FakeClient())
    monkeypatch.setattr(web.trafilatura, "extract", lambda html, **k: "正文")
    s = settings.model_copy(update={"web_allowed_domains": "example.com"})
    (_search, fetch) = make_web_tools(settings=s)

    assert "已拒绝：该域名不在联网白名单内" in fetch.invoke({"url": "https://other.org/a"})
    assert "正文" in fetch.invoke({"url": "https://example.com/a"})
    assert "正文" in fetch.invoke({"url": "https://www.example.com/a"})  # 子域匹配

    # 空名单 = 不限（回到公网边界单层把关）
    s_open = settings.model_copy(update={"web_allowed_domains": ""})
    (_search, fetch_open) = make_web_tools(settings=s_open)
    assert "正文" in fetch_open.invoke({"url": "https://anything.org/a"})
