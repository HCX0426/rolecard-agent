"""尺子的共享状态与遍历助手 —— 所有判据模块从这里拿"世界"。

刻意只有这几样：全局结算面（fails/warns/passed 与 out）、仓库文件遍历
（IGNORED_DIRS 剪枝 + 缓存）、注释剥离。多一个进来的候选都要先问：
"它是三处以上判据共用的世界观吗？"——不是就留在自己的判据模块里。
"""
from __future__ import annotations

import os
import pathlib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]  # 本文件住 scripts/consistency/，比旧位置深一层

fails: list[str] = []
warns: list[str] = []
passed = 0


# Directories that must never be walked. `ROOT.rglob("*.py")` happily descends into a
# virtualenv, which made the line-budget metric report 300k lines of site-packages instead
# of the project (C24).
IGNORED_DIRS = {
    ".git",
    ".venv",
    ".venv-dev",
    ".venv-ocr",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
    "data",
    "node_modules",
    # `build/` 是本地脚手架（sidecar 解包、安装包、日志、临时克隆），`.gitignore` 整目录挡着。
    # 09-26 干净克隆彩排时我在 build/ci-clone 里放了一份仓库副本，两条检查立刻把**副本**里
    # 的脚本当成待检文件判红 —— 与 C24 那次"把 site-packages 数成项目代码"同一族：
    # 尺子必须只看这一个世界。
    "build",
    # `out`/`release`（`R102-35`）：shell 的构建产物目录，各自 .gitignore 挡着 —— 从前
    # 判据读进 gitignore 的构建树，同一份码在干净 clone 打 427、在本机打 446。
    "out",
    "release",
}


_file_walk_cache: dict[tuple[str, ...], list[pathlib.Path]] = {}


def iter_files(*suffixes: str) -> list[pathlib.Path]:
    """Repo files, skipping environments, caches and generated data.

    结果按 suffix 集合缓存，且遍历时**原地剪枝** IGNORED_DIRS 子树：8 个检查各调一次、
    每次全量 rglob（frontend/node_modules 几万文件照走，只是最后被过滤）曾把整份脚本
    拖到 17s（门禁耗时盘点）。目录在单次运行内不会变，缓存 + 剪枝都是纯收益。
    """
    key = tuple(sorted(suffixes))
    cached = _file_walk_cache.get(key)
    if cached is not None:
        return cached
    found: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 原地剪枝：巨树（node_modules / .venv / data …）整个不进入，而不是进入后再过滤。
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            if suffixes and pathlib.Path(name).suffix not in suffixes:
                continue
            found.append(pathlib.Path(dirpath) / name)
    _file_walk_cache[key] = found
    return found


def strip_comments(text: str) -> str:
    """Remove SQL (`--`) and Python (`#`) comment lines and trailing comments.

    Needed by the domain-isolation check: prose *about* a concept must not be mistaken for
    a definition *of* it. Writing "there is no user table here" kept tripping it.
    """
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(("--", "#")):
            continue
        lines.append(line.split("--", 1)[0].split("#", 1)[0])
    return "\n".join(lines)


def out(label: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
    print(f"{'OK  ' if ok else 'FAIL'} {label}{(' :: ' + detail) if detail else ''}")


# --------------------------------------------------------------- YAML subset


class YamlSubsetError(ValueError):
    """文件用了本读法之外的 YAML（锚点/流式/块标量……）—— 宁可大声说不行，不要猜。"""


def _strip_comment(line: str) -> str:
    """去掉行尾 `#` 注释，但认引号（字符串里的 `#` 是数据不是注释）。"""
    out_chars: list[str] = []
    quote: str | None = None
    for ch in line:
        if quote:
            out_chars.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out_chars.append(ch)
            continue
        if ch == "#":
            break
        out_chars.append(ch)
    return "".join(out_chars).rstrip()


def _scalar(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _parse_block(lines: list[tuple[int, str]], indent: int) -> Any:
    """解析缩进 >= indent 的那一段（映射或序列）。"""
    # 同一个累加器先当 dict、遇到 `- ` 再变 list —— 这两个形状在运行期互斥（同一层不可能
    # 又是映射又是序列），但静态上是一个名字两种类型，所以按 Any 累加、出参由调用方判形状。
    result: Any = {}
    i = 0
    while i < len(lines):
        line_indent, text = lines[i]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise YamlSubsetError(f"unexpected indentation: {text!r}")
        if text.startswith("- "):
            if not isinstance(result, list):
                if result:
                    raise YamlSubsetError("mapping and list mixed at the same level")
                result = []
            item = text[2:].strip()
            # YAML 自己的规矩：`key: value` 的冒号后面要有空格（或到行尾）。没有就是数据——
            # `8000:8000` 是**端口映射这一个标量**，不是映射。判错会把 `expose: ["8000"]`
            # 读成 `{"8000": "8000"}`，端口那条尺子读到的形状整个不对。
            if not item.startswith(("\"", "'")) and (": " in item or item.endswith(":")):
                key, _, val = item.partition(":")
                result.append({key.strip(): _scalar(val)})
            else:
                result.append(_scalar(item))
            i += 1
            continue
        key, sep, val = text.partition(":")
        if not sep:
            raise YamlSubsetError(f"expected 'key:' : {text!r}")
        key = key.strip()
        body = val.strip()
        if body:
            # 流式（`{a: 1}` / `[1, 2]`）、锚点别名（`&x` / `*x`）、块标量（`|` / `>`）
            # 都不在子集里 —— **必须大声抛**：把它们当标量静默收下，等于读出一个错的值
            # 还报告正常，比读不出来更坏（这条尺子要防的正是"配了没用"那一族）。
            if body[0] in "{[" or body.startswith(("&", "*", "|", ">")):
                raise YamlSubsetError(f"flow/anchor/block scalar not supported: {text!r}")
            result[key] = _scalar(body)
            i += 1
            continue
        children: list[tuple[int, str]] = []
        j = i + 1
        while j < len(lines) and lines[j][0] > indent:
            children.append(lines[j])
            j += 1
        if not children:
            result[key] = None
        else:
            child_indent = min(c[0] for c in children)
            result[key] = _parse_block(children, child_indent)
        i = j
    return result


def simple_yaml(text: str) -> dict:
    """读一个**YAML 子集**文档（缩进映射 / 标量序列 / 引号标量 / `#` 注释）成嵌套 dict。

    为什么不引 PyYAML：它不在任何 requirements 里 —— 判据 import 它会**本机绿（.venv 恰好
    有）、CI 红（按 requirements 装）**，正是 P2-25 给能力矩阵记过的那个陷阱。为了让一条
    *尺子*跑起来而加依赖不值当，何况要解析的文件只用到 YAML 的一成。

    **刻意不支持**：锚点/别名、流式 `{a: 1}`、块标量（`|` / `>`）、多文档。哪天真需要，
    诚实的做法是**把 PyYAML 进 requirements 然后删掉本函数**，而不是悄悄把它长大。
    """
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, stripped.strip()))
    if not lines:
        return {}
    root = _parse_block(lines, min(i for i, _ in lines))
    if not isinstance(root, dict):
        raise YamlSubsetError("document root must be a mapping")
    return root
