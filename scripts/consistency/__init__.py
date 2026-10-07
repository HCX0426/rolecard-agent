"""一致性断言包（快照批次 5「拆包」那一格的第一刀，自 scripts/check_consistency.py 拆出）。

入口仍是 `scripts/check_consistency.py`（薄包装器）；判据本体按 `registry.CHECKS`
的顺序执行。这一包不许 import `rolecard_agent.*`（尺子要能在干净克隆里独立跑）。
"""
