# tests/ 分层

什么测试放哪层——落位错了评审第一句就会问这个。

| 层 | 放什么 | 例 |
|---|---|---|
| 根目录 `test_*_api.py` | **API 契约面**：起 TestClient 打真路由，断言状态码 / 响应形状 / 用户可见文案 | `test_api.py`、`test_distill_api.py` |
| `unit/` | 单模块秒级用例；渲染快照金样也在这层比对 | `test_settings_runtime.py`（含 runtime_view_golden.json） |
| `integration/` | 跨两层以上的行为与形状迁移（装配、双后端、库迁移） | 7 支跨层用例 |
| `eval/` | 评测集与质量工件（nightly 消费，证据落 docs/eval/） | 评测跑批与判定 |
| `fixtures/` | 共享替身与样例数据 | — |

铁律（`tests/conftest.py` 头注释写透了）：**全程离线** —— 模型调用一律走 ScriptedChat 替身，
不预热、不出网；要真实模型证据的走 nightly 评测，不进单元门禁。

想改 X 先找 test_X：按文件名 grep（`test_<域>_api.py` 管路由契约，`unit/test_<模块>.py` 管实现）。
