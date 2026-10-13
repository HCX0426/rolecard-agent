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
    """`"0.4.1"` / `"v0.4.1"` → `(0, 4, 1)`；解析不出数字段就回空 tuple（不参与比较）。

    刻意不引 `packaging`：它在 dev 依赖族里，而运行时五族不含 —— 为一个比较运算
    把打包链的依赖面扩大一族，是拿 `R28-34` 那道守卫换一行代码。本仓的版本号
    形状由 version parity 尺子钉死为纯数字三段（`x.y.z`），这个函数按那个形状工作，
    遇到形状外的（预发布后缀等）宁可回空 tuple 判"比不了"，不装懂。
    """
    cleaned = _TAG_V.sub("", text.strip())
    parts = cleaned.split(".")
    if not parts or not all(re.fullmatch(r"\d+", p) for p in parts):
        return ()
    return tuple(int(p) for p in parts)


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
    latest = ".".join(str(n) for n in version_tuple)
    return UpdateInfo(
        current=current,
        latest=latest,
        has_update=version_tuple > parse_version(current),
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


def check_update(
    *,
    current: str,
    enabled: bool,
    repo: str,
    release_dir: Path | None = None,
    fetcher: Fetcher | None = None,
) -> UpdateInfo:
    """按 §模块文档的优先级跑一遍。任何一步失败都收敛进 `error`，不抛。"""
    if not enabled:
        return UpdateInfo(current=current, source="disabled")
    if release_dir is not None and release_dir.is_dir():
        local = _from_release_dir(current, release_dir)
        if local is not None:
            return local
    if not repo:
        return UpdateInfo(current=current, source="none", error="未配置更新源")
    fetch = fetcher or github_fetcher()
    try:
        body = fetch(_LATEST_RELEASE_API.format(repo=repo))
    except Exception as exc:  # noqa: BLE001 - 出网失败的面太宽（DNS/超时/401/5xx/JSON），全收进读数
        return UpdateInfo(
            current=current,
            source="none",
            error=f"问 GitHub 失败：{type(exc).__name__}: {exc}",
        )
    tag = str(body.get("tag_name") or "")
    latest = _TAG_V.sub("", tag)
    latest_tuple = parse_version(latest)
    if not latest_tuple:
        return UpdateInfo(
            current=current,
            source="github",
            error=f"release tag 不是版本号形状：{tag!r}",
        )
    return UpdateInfo(
        current=current,
        latest=latest,
        has_update=latest_tuple > parse_version(current),
        source="github",
        url=str(body.get("html_url") or _RELEASE_PAGE.format(repo=repo)),
    )
