"""版本单源改号工具（`scripts/tools/bump_version.py`）的行为。

它是一件**会改真文件**的工具，所以判据放在"改完之后四处是不是同一个号、CHANGELOG 有没有
多出正确的一节"上，而不是看它打印了什么。仓库根可注入（`main(root=…)`），于是整条命令能在
一个 tmp 假仓库里跑完 —— 只能在真仓库上跑的写入路径没法测。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "tools" / "bump_version.py"
_spec = importlib.util.spec_from_file_location("bump_version_under_test", str(_SCRIPT))
assert _spec and _spec.loader
bv = importlib.util.module_from_spec(_spec)
sys.modules["bump_version_under_test"] = bv
_spec.loader.exec_module(bv)

_CHANGELOG = """# 变更史

## [Unreleased]

## [0.3.0] - 2026-01-01

### 新增
- 旧的一节
"""


def _fake_repo(root: pathlib.Path, version: str = "0.3.0") -> pathlib.Path:
    """最小可判的假仓库：四处声明 + 一份 CHANGELOG。"""
    (root / "src/rolecard_agent/api").mkdir(parents=True)
    (root / "shell").mkdir(parents=True)
    (root / "frontend").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "rolecard-agent"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "src/rolecard_agent/api/main.py").write_text(
        f'API_VERSION = "{version}"\napp = FastAPI(version=API_VERSION)\n', encoding="utf-8"
    )
    (root / "shell/package.json").write_text(
        f'{{\n  "name": "rolecard-shell",\n  "version": "{version}",\n  "main": "main.js"\n}}\n',
        encoding="utf-8",
    )
    (root / "frontend/package.json").write_text(
        f'{{\n  "name": "rolecard-frontend",\n  "version": "{version}",\n  "type": "module"\n}}\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(_CHANGELOG, encoding="utf-8")
    return root


def test_一条命令把四处声明一起改掉(tmp_path: pathlib.Path) -> None:
    repo = _fake_repo(tmp_path)
    result = bv.bump(repo, "0.4.0", date="2026-10-07", sections={"新增": ["feat: 新东西"]})
    assert sorted(result["changed"]) == sorted(
        [
            "pyproject.toml",
            "src/rolecard_agent/api/main.py",
            "shell/package.json",
            "frontend/package.json",
            "CHANGELOG.md",
        ]
    )
    assert set(bv.collect(repo).values()) == {"0.4.0"}, "四处必须都跟着走"
    # 派生处只动那个号，周围一个字符都不许碰（json 里还有别的键、py 里还有别的行）。
    assert '"main": "main.js"' in (repo / "shell/package.json").read_text(encoding="utf-8")
    assert "app = FastAPI(version=API_VERSION)" in (
        repo / "src/rolecard_agent/api/main.py"
    ).read_text(encoding="utf-8")


def test_新节插在_Unreleased_之后且在旧节之前(tmp_path: pathlib.Path) -> None:
    repo = _fake_repo(tmp_path)
    bv.bump(repo, "0.4.0", date="2026-10-07", sections={"修复": ["fix: 修好了"]})
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert bv.release_versions(text) == ["Unreleased", "0.4.0", "0.3.0"], "最新版在最上面"
    assert "## [0.4.0] - 2026-10-07" in text
    assert "### 修复\n- fix: 修好了" in text


def test_重复跑同一个号是幂等的(tmp_path: pathlib.Path) -> None:
    repo = _fake_repo(tmp_path)
    bv.bump(repo, "0.4.0", date="2026-10-07", sections={"新增": ["feat: 一"]})
    again = bv.bump(repo, "0.4.0", date="2026-10-07", sections={"新增": ["feat: 二"]})
    assert again["already"] is True and again["changed"] == []
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.count("## [0.4.0]") == 1, "第二次不许再插一节"


def test_人工编辑过的节不被机械草稿覆盖(tmp_path: pathlib.Path) -> None:
    """发布者会把草稿改写成给使用者看的话 —— 再跑一次命令不许把那些话抹掉。"""
    repo = _fake_repo(tmp_path)
    hand = _CHANGELOG.replace(
        "## [Unreleased]", "## [Unreleased]\n\n## [0.4.0] - 2026-10-07\n\n- 人写的一句话"
    )
    (repo / "CHANGELOG.md").write_text(hand, encoding="utf-8")
    bv.bump(repo, "0.4.0", date="2026-10-07", sections={"新增": ["feat: 机械的"]})
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "人写的一句话" in text and "机械的" not in text


def test_读不到声明就当场报错而不是跳过(tmp_path: pathlib.Path) -> None:
    """静默跳过是这族缺陷的老形状：少读一处，两边都是 None 反而更绿。"""
    repo = _fake_repo(tmp_path)
    (repo / "shell/package.json").write_text('{\n  "name": "rolecard-shell"\n}\n', encoding="utf-8")
    with pytest.raises(bv.VersionPlaceMissing):
        bv.collect(repo)


def test_不是_x_y_z_的号当场拒绝(tmp_path: pathlib.Path) -> None:
    repo = _fake_repo(tmp_path)
    with pytest.raises(ValueError):
        bv.bump(repo, "v0.4", sections={})


def test_归类只认四个约定式前缀() -> None:
    assert bv.classify("feat(形态): 能力矩阵") == "新增"
    assert bv.classify("fix: 修好了") == "修复"
    assert bv.classify("perf(db): 快了一点") == "变更"
    assert bv.classify("refactor: 拆开了") == "变更"
    # 改动者的杂事不进发布说明 —— 但会被计数（下一条用例）。
    assert bv.classify("chore(读数收尾): 刷数") is None
    assert bv.classify("docs(账本): 收账") is None
    assert bv.classify("没有前缀的一句话") is None


def test_未列入的提交会被计数(tmp_path: pathlib.Path) -> None:
    block = bv.render_section("0.4.0", "2026-10-07", {"新增": ["feat: 一"]}, skipped=7)
    assert "另有 7 条" in block, "不列入不等于假装那段历史没有变化"


def test_草稿在真仓库上跑得动() -> None:
    """真仓库上取一次草稿：判据是它不抛、结构对 —— 起点那一格（tag / 上次改 CHANGELOG）
    正是最容易在真环境里算错的地方（假仓库里没有 git）。"""
    root = _SCRIPT.parents[2]
    sections, skipped = bv.draft_sections(root, bv.draft_anchor(root))
    assert isinstance(sections, dict) and isinstance(skipped, int)
    assert all(isinstance(items, list) for items in sections.values())
    assert set(sections) <= set(bv._SECTIONS)  # noqa: SLF001 - 判据本身就要问那张表
