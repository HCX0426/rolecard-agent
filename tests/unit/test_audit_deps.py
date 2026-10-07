"""依赖漏洞扫描的判定（`scripts/tools/audit_deps.py`）：豁免表不是"忽略名单"。

这里**不出网**：真扫描要问 OSV，那是 CI 的一步与一条脚本命令，不是一条用例（本仓铁律：
测试全离线）。用例量的是判定本身 —— 三件事各自都有"做错了会很安静"的形状：

  * 不在表上的告警要红（新 CVE 必须被人看见一次）；
  * 在表上但上游已出修复版本 → **也要红**（豁免的前提是"没得升"，前提没了欠条就该销 ——
    否则它退化成永久通行证，而永久通行证的下场是没人再读）；
  * **扫不成不等于干净**（没装 pip-audit / 断网 / OSV 挂了都长得像"这次没问题"）。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "tools" / "audit_deps.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_deps_under_test", str(_SCRIPT))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_deps_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _finding(pkg: str, vid: str, fixes: list[str] | None = None, aliases: list[str] | None = None):
    return {"dependencies": [
        {
            "name": pkg,
            "version": "1.0.0",
            "vulns": [{"id": vid, "fix_versions": fixes or [], "aliases": aliases or []}],
        }
    ]}


def test_同一条告警的两份记录只数一次() -> None:
    """实测本机环境里 PYSEC-2026-311 出现了两次（两个 dist 记录同版本）。

    不去重就会数出"5 条告警"这种比现实更吓人的数 —— 而数字一旦比现实吓人，
    人就学会不看它了。
    """
    audit = _load()
    payload = {
        "dependencies": [
            {"name": "chromadb", "version": "1.5.9", "vulns": [{"id": "V-1", "fix_versions": []}]},
            {"name": "chromadb", "version": "1.5.9", "vulns": [{"id": "V-1", "fix_versions": []}]},
        ]
    }
    assert len(audit.flatten(payload)) == 1


def test_不在表上的告警必须红() -> None:
    audit = _load()
    findings = audit.flatten(_finding("somepkg", "PYSEC-2026-9999"))
    verdict = audit.classify(findings, entries={})
    assert len(verdict["red"]) == 1
    assert "不在豁免表上" in verdict["red"][0]["why"]


def test_在册豁免且上游仍没得升_才算豁免() -> None:
    audit = _load()
    entry = {"id": "PYSEC-2026-1", "理由": "已是最新版", "入库": "2026-10-07"}
    findings = audit.flatten(_finding("chromadb", "PYSEC-2026-1"))
    verdict = audit.classify(findings, {"PYSEC-2026-1": entry})
    assert verdict["red"] == [] and len(verdict["exempt"]) == 1


def test_上游出了修复版本时豁免当场失效() -> None:
    """这一条是这张表不烂掉的唯一理由：前提没了就红，而不是继续替它挡着。"""
    audit = _load()
    entry = {"id": "PYSEC-2026-1", "理由": "没得升", "入库": "2026-10-07"}
    findings = audit.flatten(_finding("chromadb", "PYSEC-2026-1", fixes=["2.0.0"]))
    verdict = audit.classify(findings, {"PYSEC-2026-1": entry})
    assert len(verdict["red"]) == 1, "带 fix 版本的告警不许继续被豁免挡着"
    assert "前提没了" in verdict["red"][0]["why"] and "2.0.0" in verdict["red"][0]["why"]


def test_扫不出来的在册豁免会进复查清单() -> None:
    """环境会变（包升上去了）。静默留着一条没人复查的欠条，就是表的烂掉方式。"""
    audit = _load()
    entry = {"id": "PYSEC-2026-404", "理由": "以前有", "入库": "2026-10-07"}
    verdict = audit.classify([], {"PYSEC-2026-404": entry})
    assert [row["id"] for row in verdict["stale"]] == ["PYSEC-2026-404"]


def test_空理由的豁免条目自己就是红(tmp_path: pathlib.Path) -> None:
    """与 `requirements 分类` 那条尺子同一口径：空理由不算理由。"""
    audit = _load()
    path = tmp_path / "allow.json"
    path.write_text(
        json.dumps({"豁免": [{"id": "X-1", "理由": "   ", "入库": ""}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    _, problems = audit.load_allowlist(path)
    assert any("理由" in p for p in problems), problems
    assert any("入库" in p for p in problems), problems


def test_重复的豁免条目会被点名(tmp_path: pathlib.Path) -> None:
    audit = _load()
    path = tmp_path / "allow.json"
    one = {"id": "X-1", "理由": "r", "入库": "2026-10-07"}
    path.write_text(json.dumps({"豁免": [one, dict(one)]}, ensure_ascii=False), encoding="utf-8")
    _, problems = audit.load_allowlist(path)
    assert any("重复" in p for p in problems), problems


def test_豁免表不见了要出声(tmp_path: pathlib.Path) -> None:
    audit = _load()
    _, problems = audit.load_allowlist(tmp_path / "没有这个文件.json")
    assert problems and "不见了" in problems[0]


def test_入库那张真表自洽() -> None:
    """真文件跑一遍判据：每条都有理由与入库日期，且都写着"上游没得升"的前提。

    这条不查网络 —— 它查的是这张表**自己**有没有烂掉（空字段、缺 id）。
    """
    audit = _load()
    entries, problems = audit.load_allowlist(audit.ALLOWLIST_PATH)
    assert not problems, problems
    assert entries, "豁免表是空的，那扫描就没有任何在册项可言"
    for vid, entry in entries.items():
        assert entry.get("上游修复版本") is None, f"{vid} 已出修复版本，该从豁免里销掉"


def test_扫不成不等于干净(monkeypatch, capsys) -> None:
    """没装 pip-audit / 断网 / OSV 挂了，输出都长得像"这次没问题" —— 必须红给 2。"""
    audit = _load()
    monkeypatch.setattr(audit, "run_pip_audit", lambda: (None, "No module named pip_audit", 2))
    rc = audit.main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "跑不起来" in err and "不等于没漏洞" in err


def test_有红项时退出码为_1(monkeypatch) -> None:
    audit = _load()
    monkeypatch.setattr(audit, "run_pip_audit", lambda: (_finding("x", "PYSEC-NEW-1"), "", 1))
    assert audit.main([]) == 1


def test_全绿时退出码为_0(monkeypatch) -> None:
    audit = _load()
    monkeypatch.setattr(audit, "run_pip_audit", lambda: ({"dependencies": []}, "", 0))
    assert audit.main([]) == 0


def test_真命令的_json_形状打得出机器读的结果(monkeypatch, capsys) -> None:
    """`--json` 是给 CI 与脚本看的：blocked 布尔 + 点名那条红项。"""
    audit = _load()
    monkeypatch.setattr(audit, "run_pip_audit", lambda: (_finding("x", "PYSEC-NEW-1"), "", 1))
    rc = audit.main(["--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocked"] is True and payload["red"][0]["id"] == "PYSEC-NEW-1"
