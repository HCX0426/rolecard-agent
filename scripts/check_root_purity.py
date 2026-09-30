"""真库纯度检查（只读）：找出"测试夹具被当成用户数据留在真库里"的那些行。

为什么立这条（09-30 实测，`R28-49`）：2026-09-24 把日常数据根从仓库 `data/` 切到安装目录
`%LOCALAPPDATA%\\rolecard-agent` 的那一刻，**旧根里的测试痕迹就跟着升级成了生产数据**——
`ingestion_task` 里 5 行（2 行 `file_hash` 是 `deadbeef`/`cafe` 的演示残留 + 3 行 `source_file`
指向仓库旧根的绝对路径，原件根本不在新根里），以及真库 chroma 的 `health_reports` 作用域里
8 条向量，内容全是同一组假数（"空腹血糖 6.1 mmol/L""血红蛋白 145 g/L"）。那 8 条里有 3 条
在 `ingestion_task` 里**连行都没有** —— 是 `R28-19` 那个 `dangling` 的反方向：向量在、台账没有。

判据只报**确证的负面**（占位哈希 / 命中已知夹具名 / 跨根路径），启发式的（正文里出现夹具数值）
走 warn 不进红 —— 与门禁一贯的那条纪律同形。全程 `mode=ro`，一个字节都不写。

跑法：
    .venv\\Scripts\\python.exe scripts/check_root_purity.py            # 默认查安装根
    .venv\\Scripts\\python.exe scripts/check_root_purity.py --root D:\\别的根
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys
from dataclasses import dataclass, field

# 演示/种子用的占位哈希：真上传不可能长这样（sha256 是 64 位十六进制）。
PLACEHOLDER_HASHES = frozenset({"deadbeef", "cafe", "deadbeefdeadbeef"})
# 已知测试夹具的文件名（探针与单测里出现过的那批；按 basename 精确匹配）。
FIXTURE_SOURCES = frozenset(
    {
        "report.txt",
        "verify.png",
        "冒烟报告.txt",
        "复查图片.png",
        "抽取审计.txt",
        "血糖复查.png",
        "审计图片.png",
        "审计.pdf",
        "审计.docx",
    }
)
# 夹具正文里的数（启发式：这些串出现在**用户**的健康档案或记忆里就该看一眼）。
FIXTURE_STRINGS = (
    "空腹血糖 6.1 mmol/L",
    "血红蛋白 145 g/L",
    "结石直径 6.1 mm",
    "胆囊结石，结石直径 6.1",
)


@dataclass
class Report:
    red: list[str] = field(default_factory=list)
    warn: list[str] = field(default_factory=list)


def _is_placeholder(file_hash: object) -> bool:
    return isinstance(file_hash, str) and file_hash.strip().lower() in PLACEHOLDER_HASHES


def _basename(source_file: object) -> str:
    """上传落盘时会被冠上 8 位前缀（`47ba2914_report.txt`），匹配夹具名之前先剥掉。"""
    if not isinstance(source_file, str) or not source_file.strip():
        return ""
    name = pathlib.PurePath(source_file.replace("\\", "/")).name
    head, sep, tail = name.partition("_")
    if sep == "_" and len(head) == 8 and all(ch in "0123456789abcdef" for ch in head.lower()):
        return tail
    return name


def classify_ingestion(rows: list[tuple[str, str, str, str]], root: pathlib.Path) -> Report:
    """`rows` = (task_id, user_id, source_file, file_hash) 四元组，`root` = 这个数据根自己。"""
    rep = Report()
    for task_id, user_id, source_file, file_hash in rows:
        if _is_placeholder(file_hash):
            rep.red.append(f"{task_id}: file_hash 是占位值 {file_hash!r}（user={user_id}）")
            continue
        name = _basename(source_file)
        if name in FIXTURE_SOURCES:
            rep.red.append(f"{task_id}: source 命中已知测试夹具 {name!r}")
        elif source_file and not pathlib.Path(source_file).is_absolute():
            rep.warn.append(f"{task_id}: source_file 不是绝对路径（{source_file!r}）")
        elif source_file:
            resolved = pathlib.Path(source_file.replace("\\", "/"))
            if not resolved.exists():
                rep.red.append(f"{task_id}: 台账说原件在 {resolved.name}，但那台机器上没有这个文件")
            elif root not in resolved.parents and root != resolved.parent:
                rep.red.append(
                    f"{task_id}: source_file 指向另一个数据根（{resolved.parent}，这一根是 {root}）"
                )
    return rep


def classify_chroma(sources: list[str], scope: str) -> Report:
    rep = Report()
    for name in sources:
        base = _basename(name)
        if base in FIXTURE_SOURCES:
            rep.red.append(f"chroma[{scope}]: source {base!r} 是夹具向量，检索会把它当用户事实")
    return rep


def classify_text_columns(
    conn: sqlite3.Connection, root_table_cols: list[tuple[str, str, str]]
) -> Report:
    """启发式那一半：用户侧正文里出现夹具原句 ⇒ warn（不拦，但要看得见）。"""
    rep = Report()
    for table, col, label in root_table_cols:
        try:
            rows = conn.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL").fetchall()  # noqa: S608
        except sqlite3.Error:
            continue
        for (value,) in rows:
            text = str(value)
            for needle in FIXTURE_STRINGS:
                if needle in text:
                    rep.warn.append(f"{table}.{col}（{label}）正文里出现夹具原句 {needle!r}")
                    break
    return rep


def _sqlite_rows(db: pathlib.Path) -> list[tuple[str, str, str, str]]:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ingestion_task)")}
        if not cols:
            return []
        return [
            (str(t), str(u), str(s or ""), str(h or ""))
            for t, u, s, h in conn.execute(
                "SELECT task_id, user_id, source_file, file_hash"
                " FROM ingestion_task ORDER BY task_id"
            )
        ]
    finally:
        conn.close()


def _chroma_sources(chroma: pathlib.Path) -> dict[str, list[str]]:
    db = chroma / "chroma.sqlite3"
    if not db.exists():
        return {}
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        meta: dict[int, dict[str, str]] = {}
        for rid, key, sval in conn.execute("SELECT id, key, string_value FROM embedding_metadata"):
            meta.setdefault(rid, {})[key] = sval or ""
        out: dict[str, list[str]] = {}
        for (eid,) in conn.execute("SELECT id FROM embeddings"):
            m = meta.get(eid, {})
            scope = m.get("scope") or "(无 scope)"
            out.setdefault(scope, []).append(m.get("source", ""))
        return out
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def scan(root: pathlib.Path) -> Report:
    total = Report()
    db = root / "sqlite" / "app.db"
    if not db.exists():
        total.warn.append(f"{db} 不存在 —— 这一根上没有库")
        return total
    verdict = classify_ingestion(_sqlite_rows(db), root)
    total.red += verdict.red
    total.warn += verdict.warn
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        targets = [
            ("role_memory_item", "text", "长期记忆"),
            ("medical_index", "raw_text", "指标原文"),
            ("medical_index", "value_text", "指标值"),
            ("domain_data", "value_text", "领域数据"),
        ]
        total.warn += classify_text_columns(conn, targets).warn
    finally:
        conn.close()
    for scope, sources in _chroma_sources(root / "chroma").items():
        total.red += classify_chroma(sources, scope).red
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="真库纯度检查（只读）")
    default = pathlib.Path.home() / "AppData" / "Local" / "rolecard-agent"
    parser.add_argument("--root", default=str(default), type=pathlib.Path)
    args = parser.parse_args()
    root: pathlib.Path = args.root
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rep = scan(root)
    print(f"数据根 = {root}")
    for line in rep.red:
        print(f"  ✗ {line}")
    for line in rep.warn:
        print(f"  ⚠ {line}")
    if not rep.red and not rep.warn:
        print("  ✓ 没有占位哈希、没有夹具向量、没有跨根台账")
    print(f"读数：红 {len(rep.red)} / 启发式 {len(rep.warn)}")
    return 1 if rep.red else 0


if __name__ == "__main__":
    raise SystemExit(main())
