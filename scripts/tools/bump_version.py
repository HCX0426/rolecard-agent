"""版本单源 + 一键改号 + CHANGELOG 草稿（2026-10-04 审查快照「版本四处手抄」那一格）。

**单源是 `pyproject.toml` 的 `version`**，另外三处是派生：`api/main.py` 的 `API_VERSION`、
`shell/package.json`、`frontend/package.json`。派生的意思是"只许从这里来" —— 从前四处各写
一遍、靠 `version parity` 那条尺子事后对齐；尺子能抓住漂，但抓不住"改的时候漏了一处"
这件事本身，0.3.0 就是这么过来的。

为什么仍要一个脚本而不是"运行时读包元数据"：`importlib.metadata.version()` 读的是**安装时**
的元数据，改完 `pyproject` 不重装就还是旧号 —— 那等于把"漂"从四个文件搬进解释器缓存里，
门禁只会更难判。两份 `package.json` 更没法运行时派生（npm 与 electron-builder 直接读文件）。
所以这里选的是**机械写入 + 尺子事后核对**：写入路径只有一条命令，核对路径还是那条尺子。

CHANGELOG 是**机械草稿 + 人工编辑**：从提交信息按约定式前缀归类（feat→新增、fix→修复、
perf/refactor→变更，其余前缀不进草稿但计数），起点是 `CHANGELOG.md` 上一次被改动的提交
（没有 tag 时），所以"这一版改了什么"不用人去翻 `git log`。草稿是给发布者省事的，
**不是**给使用者看的成品 —— 提交信息写给改动者，发布说明写给用的人。

用法：

    python scripts/tools/bump_version.py --check        # 四处是不是同一个号（只读）
    python scripts/tools/bump_version.py 0.4.0          # 改号 + 插一节 CHANGELOG 草稿
    python scripts/tools/bump_version.py 0.4.0 --dry-run

写完请跑 `scripts/check_consistency.py`（`version parity` 与 `changelog` 两条尺子都在那里）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Keep a Changelog 的六节（顺序即渲染顺序）；草稿只用得上其中三节。
_SECTIONS = ("新增", "变更", "弃用", "移除", "修复", "安全")

#: 约定式提交前缀 → Keep a Changelog 的节。**刻意只映射这四个**：`docs`/`chore`/`ci`/`test`
#: 是改动者的杂事，不是使用者看得见的变化 —— 把它们倒进发布说明，等于让读者在噪音里找变化。
#: （它们不进草稿，但会被计数并在结尾报一句"另有 N 条未列入"，免得读者以为历史只有这些。）
_SECTION_OF_PREFIX = {
    "feat": "新增",
    "fix": "修复",
    "perf": "变更",
    "refactor": "变更",
}

_PREFIX_RE = re.compile(r"^([a-z]+)(?:\([^)]*\))?!?:")

_CHANGELOG = "CHANGELOG.md"
_RELEASE_RE = re.compile(r"(?m)^## \[([^\]]+)\]")

CHANGELOG_HEADER = """# 变更史

本文件按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 组织，版本号遵循
[语义化版本](https://semver.org/lang/zh-CN/)。

> **它是机械草稿加人工编辑的产物**：`scripts/tools/bump_version.py` 从提交信息按约定式前缀
> 生成一节草稿，发布前由人改写成给使用者看的话 —— 提交信息是写给改动者的，不是给用的人看的。
> 版本号的**唯一出处是 `pyproject.toml`**（另外三处由那条命令派生），四处一致性由
> `scripts/check_consistency.py` 的 `version parity` 与 `changelog` 两条尺子把着。

## [Unreleased]
"""


class VersionPlaceMissing(RuntimeError):
    """某一处的版本声明读不到 —— 不跳过、不猜，当场报出来。

    静默跳过是这族缺陷的老形状：少读一处，`None` 与 `None` 相等，尺子反而更绿。
    """


@dataclass(frozen=True)
class Place:
    """一处版本声明：文件 + 定位它那个号的正则（**第 1 个捕获组必须是号本身**）。"""

    path: str
    pattern: re.Pattern[str]
    what: str

    def read(self, root: Path) -> str:
        text = (root / self.path).read_text(encoding="utf-8")
        found = self.pattern.search(text)
        if found is None:
            raise VersionPlaceMissing(f"{self.path} 里找不到版本声明（{self.what}）")
        return found.group(1)

    def rewrite(self, root: Path, new: str) -> bool:
        """把这处的号改成 `new`；返回是否真的改了（幂等：已经是它就是 False）。"""
        path = root / self.path
        text = path.read_text(encoding="utf-8")
        found = self.pattern.search(text)
        if found is None:
            raise VersionPlaceMissing(f"{self.path} 里找不到版本声明（{self.what}）")
        if found.group(1) == new:
            return False
        start, end = found.span(1)
        path.write_text(text[:start] + new + text[end:], encoding="utf-8", newline="")
        return True


#: 四处声明。顺序即打印顺序；第一处是**源**，其余是派生。
PLACES: tuple[Place, ...] = (
    Place("pyproject.toml", re.compile(r'(?m)^version = "([^"]+)"'), "单源"),
    Place(
        "src/rolecard_agent/api/main.py",
        re.compile(r'(?m)^API_VERSION = "([^"]+)"'),
        "/api/health 与 OpenAPI 读的那个号",
    ),
    Place("shell/package.json", re.compile(r'(?m)^\s*"version":\s*"([^"]+)"'), "桌面壳/安装包名"),
    Place("frontend/package.json", re.compile(r'(?m)^\s*"version":\s*"([^"]+)"'), "前端包"),
)


def collect(root: Path) -> dict[str, str]:
    """四处现在各是什么号（读不到就抛，不返回半份）。"""
    return {place.path: place.read(root) for place in PLACES}


def drifted(places: dict[str, str]) -> bool:
    return len(set(places.values())) > 1


def classify(subject: str) -> str | None:
    """一条提交信息落到哪一节；不属于这四类前缀（或没有前缀）→ None。"""
    found = _PREFIX_RE.match(subject.strip())
    return _SECTION_OF_PREFIX.get(found.group(1)) if found else None


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else ""


def draft_anchor(root: Path) -> str | None:
    """草稿的起点：最近的 tag；没有 tag 就退回"`CHANGELOG.md` 上一次被改动的那笔提交"。

    为什么退回这一格而不是"仓库第一次提交"：本仓起步时没有 tag，若一路回溯到 847 条提交，
    草稿会把整部历史倒进一节里，读者等于没有变更史。上一次改 CHANGELOG 的地方正是上一版
    的分界点，所以它就是"这一版改了什么"的天然起点（也正因为如此，这条命令**必须**
    与 `CHANGELOG.md` 同笔提交，否则起点会落在自己身上）。
    """
    tag = _git(root, "describe", "--tags", "--abbrev=0").strip()
    if tag:
        return tag
    sha = _git(root, "log", "-1", "--format=%H", "--", _CHANGELOG).strip()
    return sha or None


def draft_sections(root: Path, since: str | None) -> tuple[dict[str, list[str]], int]:
    """从提交信息归类出一节草稿；返回（分节条目，未列入的提交数）。"""
    rev = f"{since}..HEAD" if since else "HEAD"
    subjects = [line for line in _git(root, "log", rev, "--pretty=format:%s").splitlines() if line]
    sections: dict[str, list[str]] = {}
    skipped = 0
    for subject in subjects:
        section = classify(subject)
        if section is None:
            skipped += 1
            continue
        sections.setdefault(section, []).append(subject)
    return sections, skipped


def render_section(
    version: str, date: str, sections: dict[str, list[str]], skipped: int = 0
) -> str:
    lines = [f"## [{version}] - {date}"]
    for name in _SECTIONS:
        items = sections.get(name) or []
        if not items:
            continue
        lines.append(f"### {name}")
        lines.extend(f"- {item}" for item in items)
    if len(lines) == 1:
        lines.append("- （没有从提交信息里归类出条目 —— 发布前手写这一节）")
    if skipped:
        lines.append(f"\n<!-- 草稿：另有 {skipped} 条 docs/chore/ci/test 类提交未列入 -->")
    return "\n".join(lines) + "\n"


def release_versions(text: str) -> list[str]:
    return _RELEASE_RE.findall(text)


def insert_section(text: str, version: str, block: str) -> str:
    """把新版本那一节插在 `## [Unreleased]` 之后（最新版在最上面）。

    已经有这个号的节就**原样不动**：那一节可能已经被人改写成给使用者看的话了，
    机械覆盖等于把人工编辑抹掉（幂等的那一半比"能重复跑"更重要）。
    """
    if version in release_versions(text):
        return text
    marker = "## [Unreleased]"
    at = text.find(marker)
    if at < 0:
        raise VersionPlaceMissing(f"{_CHANGELOG} 里没有 `{marker}` 那一节，不知道新版插哪")
    end = text.find("\n", at)
    head, tail = text[: end + 1], text[end + 1 :]
    return f"{head}\n{block}{tail}"


def changelog_path(root: Path) -> Path:
    return root / _CHANGELOG


def ensure_changelog(root: Path) -> str:
    path = changelog_path(root)
    if not path.exists():
        path.write_text(CHANGELOG_HEADER, encoding="utf-8", newline="")
    return path.read_text(encoding="utf-8")


def bump(
    root: Path,
    new: str,
    *,
    date: str | None = None,
    sections: dict[str, list[str]] | None = None,
    skipped: int = 0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """改号 + 插一节 CHANGELOG。`sections=None` 时从提交信息现取（测试里直接喂）。"""
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        raise ValueError(f"版本号得是 x.y.z（收到 {new!r}）")
    before = collect(root)
    if set(before.values()) == {new}:
        return {"changed": [], "version": new, "already": True, "sections": {}}
    if sections is None:
        sections, skipped = draft_sections(root, draft_anchor(root))
    changed = [place.path for place in PLACES if not dry_run and place.rewrite(root, new)]
    block = render_section(new, date or dt.date.today().isoformat(), sections, skipped)
    if not dry_run:
        text = ensure_changelog(root)
        updated = insert_section(text, new, block)
        if updated != text:
            changelog_path(root).write_text(updated, encoding="utf-8", newline="")
            changed.append(_CHANGELOG)
    return {"changed": changed, "version": new, "already": False, "sections": sections}


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="版本单源改号 + CHANGELOG 草稿")
    parser.add_argument("version", nargs="?", help="新版本号，如 0.4.0")
    parser.add_argument("--check", action="store_true", help="只报四处现在各是什么号")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要发生什么")
    parser.add_argument("--date", default=None, help="CHANGELOG 节上的日期（默认今天）")
    args = parser.parse_args(argv)

    # `root` 可注入是为了让用例拿一个 tmp 目录当仓库跑整条命令 —— 版本改写是要动真文件的，
    # 一条只能在真仓库上跑的写入路径没法测（而它恰恰是最不该靠"我看着跑对了"的那类）。
    root = root or Path(__file__).resolve().parents[2]
    places = collect(root)
    current = places["pyproject.toml"]

    if args.check or not args.version:
        for place in PLACES:
            mark = "源  " if place.path == "pyproject.toml" else "派生"
            print(f"  {mark} {places[place.path]:>8}  {place.path}  # {place.what}")
        if drifted(places):
            print(f"四处不是同一个号：{sorted(set(places.values()))}", file=sys.stderr)
            return 1
        print(f"四处一致：{current}")
        return 0

    result = bump(root, args.version, date=args.date, dry_run=args.dry_run)
    if result["already"]:
        print(f"四处已经是 {args.version}，没动任何文件")
        return 0
    verb = "会改" if args.dry_run else "已改"
    print(f"{verb}：{args.version}（原 {current}）")
    for name in result["changed"]:
        print(f"  - {name}")
    for section, items in (result["sections"] or {}).items():
        print(f"  CHANGELOG 草稿 · {section}：{len(items)} 条")
    if not args.dry_run:
        after = collect(root)
        if drifted(after):
            print(f"改完仍然不一致：{after}", file=sys.stderr)
            return 1
        print("四处已一致 —— 别忘了跑 scripts/check_consistency.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
