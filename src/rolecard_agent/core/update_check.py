"""「检查更新」的比对逻辑：当前版本 vs 最新版本，双事实源，离线可测。

为什么住在 core 而不是 router（本仓铁律：api 薄，业务逻辑与 SQL 都不进 api/）：
这份判据壳（electron-updater）与网页（设置→关于）都要读同一个答案，两边各比一次
版本就是两个事实面 —— 而"同一个事实两个家，就一定会在某个下午对不上"是本仓
用一整族 single-source 尺子反复钉过的教训（B5 那张自相矛盾的架构图是最新一例）。

双事实源的优先级（设计稿《更新机制与OCR随包》§3.1）：

  1. **本地 `shell_release_dir`**（配了且目录里有产物）—— 自托管部署方的产物就是
     它自己认可的"最新"，不越过它去问公网；
  2. **GitHub Releases**（配了 `update_repo` 且出网成功）—— 装机用户的正路；
  3. 都没有 ⇒ `source="none"`，如实报告，不猜。

**出网判据不进测试**（与 `scripts/tools/audit_deps.py` 同一条纪律）：fetcher 是注入的，
测试永远喂假数据；默认实现才碰 httpx。断网 / GitHub 限流 / 仓库不存在，全部收敛成
`error` 字段，端点本身不 500 —— "检查更新失败"不该长得像"应用坏了"。

**限流是真实发生过的**（2026-10-25 用户实测 403）：GitHub 对匿名 API 的配额是
**每出口 IP 每小时 60 次**——同一网络里所有访问 GitHub 的东西共享它（浏览器开的
github.com、别的工具、这台机上跑的其他 agent），用完就是 403，等整点才回。两个对策
（2026-10-25）：

  1. **进程级缓存**：同一进程内同一版本的检查结果缓存 30 分钟（`_CACHE_TTL`）。壳的
     开机检查、网页的关于页、用户手点"检查更新"三处共享这一份 —— 不再是每处各打一次；
  2. **403/429 翻人话**：错误文案里写明"GitHub 匿名限流（每小时 60 次/出口 IP），
     稍后再试或直接去发布页看"——而不是甩一个 `HTTPStatusError: 403` 让人以为是坏了。

缓存键是 `(current, repo)`：升级后 current 变了，缓存自然失效，不会把旧读数端给新版本。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rolecard_agent.core.artifacts import parse_artifact_name

#: `fetch(url) -> 解析后的 JSON`。测试注入这份；默认实现见 `github_fetcher()`。
Fetcher = Callable[[str], dict[str, Any]]

#: GitHub latest-release 的 URL 形状。owner/repo 来自配置，模板只在这里出现一次。
_LATEST_RELEASE_API = "https://api.github.com/repos/{repo}/releases/latest"
_RELEASE_PAGE = "https://github.com/{repo}/releases/latest"

#: tag 前缀的 v：v0.4.1 与 0.4.1 是同一个版本，比较前都剥掉。
_TAG_V = re.compile(r"^v(?=\d)")


def parse_version(text: str) -> tuple[int, ...]:
    """`"0.4.1"` / `"v0.4.1"` → `(0, 4, 1, 1)`；预发布号（P10④ 起 version parity 允许
    `0.5.1-beta.1` 这种形状）→ `(0, 5, 1, 0)`。**末位是正式度旗标**（正式 1 / 预发布
    0）：预发布要排在同号正式版之前，而 Python 元组比较里 `(0,5,1)` 是 `(0,5,1,x)`
    的前缀、前缀恒小 —— 追加哨兵排不进中间，给**每一侧**都补旗标才判得对
    （`-1` 哨兵的第一版就栽在这，实测（0,5,1) vs (0,5,1,-1) 是 False 不是 True）。
    真解析不出（形状外，如任意文本 tag）回空 tuple —— "比不了"由 `_has_update`
    如实兜成 False，不装懂。仍然刻意不引 `packaging`（dev 族依赖，不进运行时五族）。
    本接口只跟 GitHub **latest release**（永远排除 prerelease）比，beta↔beta 的
    排序在壳侧 electron-updater 的 beta.yml 那条链上，不经这里。
    """
    cleaned = _TAG_V.sub("", text.strip())
    head, sep, _pre = cleaned.partition("-")
    parts = head.split(".")
    if not parts or not all(re.fullmatch(r"\d+", p) for p in parts):
        return ()
    return (*tuple(int(p) for p in parts), 0 if sep else 1)


def _has_update(latest_tuple: tuple[int, ...], current: str) -> bool:
    """latest 严格比 current 新吗？**任一侧解析不出 ⇒ 比不了 ⇒ 如实 False**。

    为什么要有这层而不是直接 `>`：空 tuple 在 Python 里比**任何** tuple 都小，
    latest 是形状外文本（解析不出）时裸比较会把"比不了"误判成"有更新" ——
    两处调用点的形状守卫之外，这里再兜一道（0.5.1-beta.1 一 bump 就在 CI
    抓出来过：test_update_check_current_version_is_api_version）。
    """
    if not latest_tuple or not parse_version(current):
        return False
    return latest_tuple > parse_version(current)


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    """一次检查的全部读数。`to_api()` 是它对外唯一的形状。"""

    current: str
    latest: str | None = None
    has_update: bool = False
    #: github | release_dir | disabled | none
    source: str = "none"
    #: 人去的页（release 页 / 下载链接），没有就 None —— 界面据此决定按钮画不画
    url: str | None = None
    file_name: str | None = None
    size_bytes: int | None = None
    error: str | None = None

    def to_api(self) -> dict[str, object]:
        return {
            "current": self.current,
            "latest": self.latest,
            "has_update": self.has_update,
            "source": self.source,
            "url": self.url,
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "error": self.error,
        }


def _from_release_dir(current: str, directory: Path) -> UpdateInfo | None:
    """本地托管目录里有产物 ⇒ 以文件名为事实源。没有产物回 None（交给下一源）。"""
    from rolecard_agent.core.artifacts import ARTIFACT_GLOB  # noqa: PLC0415

    candidates: list[tuple[tuple[int, ...], Path]] = []
    for path in sorted(directory.glob(ARTIFACT_GLOB)):
        if not path.is_file():
            continue
        parsed = parse_artifact_name(path.name)
        if parsed is None:
            continue
        version = parse_version(parsed[0])
        if version:
            candidates.append((version, path))
    if not candidates:
        return None
    version_tuple, newest = max(candidates, key=lambda item: item[0])
    # 末位是正式度旗标，不是版本段 —— 回字符串时剥掉。
    latest = ".".join(str(n) for n in version_tuple[:-1])
    return UpdateInfo(
        current=current,
        latest=latest,
        has_update=_has_update(version_tuple, current),
        source="release_dir",
        url="/api/shell-release/download",
        file_name=newest.name,
        size_bytes=newest.stat().st_size,
    )


def github_fetcher() -> Fetcher:
    """默认的出网实现。单独成函数 = 测试永不 import 到它，判据测试全注入。"""
    import httpx  # noqa: PLC0415 - 只有真出网这条路径需要它

    def fetch(url: str) -> dict[str, Any]:
        response = httpx.get(
            url,
            timeout=8.0,
            headers={
                "Accept": "application/vnd.github+json",
                # GitHub 对无 UA 的 API 请求直接拒（默认 UA 是 python-httpx 也能过，
                # 但给一个可辨识的：这是版本检查，不是爬虫，限流时好归因）。
                "User-Agent": "rolecard-agent-update-check",
            },
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    return fetch


#: 检查结果的进程级缓存：(current, repo) → (时刻, 读数)。同进程内三处触发（壳开机 /
#: 网页关于页 / 手动点）共享一份，30 分钟内的重复检查不再打 GitHub —— 匿名配额
#: 每出口 IP 每小时 60 次，同一网络里人人共享，省着点用。
_CACHE_TTL_SECONDS = 1800.0
_CACHE: dict[tuple[str, str], tuple[float, UpdateInfo]] = {}


def _cache_get(key: tuple[str, str]) -> UpdateInfo | None:
    import time  # noqa: PLC0415 - 时钟只在运行时路径需要

    hit = _CACHE.get(key)
    if hit is None:
        return None
    stamp, info = hit
    if time.monotonic() - stamp > _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return info


def _cache_put(key: tuple[str, str], info: UpdateInfo) -> None:
    import time  # noqa: PLC0415

    _CACHE[key] = (time.monotonic(), info)


def _humanize_fetch_error(exc: Exception) -> str:
    """把出网失败翻成**可行动**的人话（限流单独点名，其余归因为网络/配置）。"""
    text = str(exc)
    kind = type(exc).__name__
    if "403" in text or "429" in text or "rate" in text.lower():
        return (
            "GitHub 匿名限流（每出口 IP 每小时 60 次，同一网络里所有访问 GitHub 的"
            "程序共享）—— 稍后再试，或直接去发布页看最新版本"
        )
    return f"问 GitHub 失败：{kind}: {text[:160]}"


def check_update(
    *,
    current: str,
    enabled: bool,
    repo: str,
    release_dir: Path | None = None,
    fetcher: Fetcher | None = None,
) -> UpdateInfo:
    """按 §模块文档的优先级跑一遍。任何一步失败都收敛进 `error`，不抛。

    GitHub 源的结果带 30 分钟进程级缓存（`_CACHE`）：同版本同仓库的重复检查不再
    打 API。成功与"限流"读数都进缓存 —— 限流那条 30 分钟内重试也大概率还是 403，
    缓存它就是替用户省一次注定失败的出网。
    """
    if not enabled:
        return UpdateInfo(current=current, source="disabled")
    if release_dir is not None and release_dir.is_dir():
        local = _from_release_dir(current, release_dir)
        if local is not None:
            return local
    if not repo:
        return UpdateInfo(current=current, source="none", error="未配置更新源")
    key = (current, repo)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    fetch = fetcher or github_fetcher()
    try:
        body = fetch(_LATEST_RELEASE_API.format(repo=repo))
    except Exception as exc:  # noqa: BLE001 - 出网失败的面太宽（DNS/超时/401/5xx/JSON），全收进读数
        failed = UpdateInfo(
            current=current,
            source="none",
            error=_humanize_fetch_error(exc),
        )
        _cache_put(key, failed)  # 限流读数也缓存：30 分钟内重试大概率还是 403
        return failed
    tag = str(body.get("tag_name") or "")
    latest = _TAG_V.sub("", tag)
    latest_tuple = parse_version(latest)
    if not latest_tuple:
        return UpdateInfo(
            current=current,
            source="github",
            error=f"release tag 不是版本号形状：{tag!r}",
        )
    info = UpdateInfo(
        current=current,
        latest=latest,
        has_update=_has_update(latest_tuple, current),
        source="github",
        url=str(body.get("html_url") or _RELEASE_PAGE.format(repo=repo)),
    )
    _cache_put(key, info)
    return info
