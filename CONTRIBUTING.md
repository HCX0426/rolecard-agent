# 贡献指南（适用于人，也适用于模型）

## 0. 这份文档为什么存在

这个仓库的**骨架由强模型一次性写好，细节可能由多个较弱或免费的模型分批补全**。

弱模型在代码库里最典型的失败方式不是"写不出来"，而是**局部正确、全局漂移**：签名被悄悄改了、约束被绕过了、两个文件对同一个概念用了不同定义。

所以下面这些规则的唯一目的是：**让任何一次局部修改都无法破坏全局。**
动手之前先读完第 1 节和第 4 节。

---

## 1. 铁律（违反即拒绝合并）

| # | 规则 | 为什么 |
| --- | --- | --- |
| 1 | **不改公共函数签名**（名字、参数、返回类型） | 签名是文件之间唯一的契约。改了它，别的文件就静默错位 |
| 2 | **不改 `core/prompts.py` 的 `GLOBAL_SAFETY_PROMPT`，也不改它的拼接顺序** | 顺序反了 = 安全层静默失效，而且不会有任何报错 |
| 3 | **不改表结构与列名** | 改了要动 DDL、模型、服务三层，且已入库数据会失配 |
| 4 | **不在 `domains/` 下定义 user / tenant** | 它们是内核概念，只存在 `core/schema.sql` |
| 5 | **不让 LLM 调用 `switch_role` 或插件启停** | 自我授权：让被约束方改自己的权限边界 |
| 6 | **不把异常堆栈返回给调用方** | 堆栈只进日志；对外只给 error code + 可读消息 |
| 7 | **不新增外部依赖** | 要加先改 `pyproject.toml` 与对应 `requirements-*.txt`，并在 PR 说明理由 |
| 8 | **不跨文件"顺手重构"** | 一次只动一个模块。跨模块改动必须由一个了解全局的人来做 |
| 9 | **不提交 `.env`、真实数据、任何密钥** | 演示数据必须全部虚构 |
| 10 | **不删掉 TODO 却不实现它** | 删掉等于把缺口藏起来 |

---

## 2. 工作流：测试先行

这个项目**先写测试，再写实现**。原因很直接：测试把"对不对"变成了二元反馈（红 / 绿），而二元反馈是弱模型最擅长优化的目标。没有测试，弱模型只能靠猜。

```
1. 读目标文件的 docstring —— 不变量与契约都写在那里
2. 写 / 补测试，把期望行为写成断言
3. 实现，直到测试变绿
4. 跑 pytest
5. 跑 ruff check . && ruff format .
6. 跑 python scripts/check_consistency.py
7. 提交
```

第 5、6 步不是可选的美化：`check_consistency.py` 专门用来抓"文档与代码不同步"和"旧命名残留"，它是这个仓库唯一的自动护栏。

---

## 3. TODO 粒度规约

**一个 TODO = 一个可独立验证的行为。** 不要写"实现整个服务层"。

```python
# TODO(m1): 按角色白名单过滤工具列表
#   in : tools: list[BaseTool], role: RoleCard, enabled_domains: set[str]
#   out: list[BaseTool]  —— 只留下「已启用插件」∩「角色白名单」内的工具
#   done-when: tests/unit/test_registry.py::test_whitelist_filters_before_bind 通过
```

判断标准：**如果一条 TODO 需要读超过两个文件才能开始做，它就太粗了。**

---

## 4. 不可改清单

| 路径 | 为什么不能动 |
| --- | --- |
| `core/prompts.py` | 安全规则的唯一来源与拼接顺序 |
| `core/schema.sql` | 内核数据契约（identity / session / plugin / audit） |
| `roles/schema.sql` | 角色卡数据契约 |
| `core/guard.py` 的 `check()` 契约 | 硬拦截的接口；改语义会让 fail-closed 失效 |
| `pyproject.toml` 的 `[tool.*]` 段 | 工具链配置；改了全仓库行为都会变 |
| `scripts/check_consistency.py` 的断言集合 | 护栏本身；要放宽必须单独说明理由 |

**可以改**：任何文件的**函数体**、docstring 里的错别字、新增测试、新增 TODO。

---

## 5. 接口契约（HTTP，M4）

统一前缀 `/api`。**所有响应都是 JSON；错误一律走下面的统一结构。**

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/chat` | SSE 流式对话；body 带 `thread_id`、`message` |
| GET | `/api/roles` | 角色列表 |
| POST | `/api/roles` | 新建 / 更新角色 |
| POST | `/api/session/role` | 切换某 thread 的角色（**运营动作，不是 LLM 工具**） |
| GET | `/api/plugins` | 插件列表与启用状态 |
| POST | `/api/plugins/toggle` | 启用 / 停用插件 |
| POST | `/api/upload` | 文档摄取入口（v1 为占位，v2.2 实现） |

### 统一错误结构

```json
{
  "error": {
    "code": "TOOL_NOT_ALLOWED",
    "message": "当前角色无权调用该工具",
    "request_id": "req_7f3a..."
  }
}
```

| code | HTTP | 含义 |
| --- | --- | --- |
| `BAD_REQUEST` | 400 | 入参校验失败 |
| `ROLE_NOT_FOUND` | 404 | 角色不存在 |
| `THREAD_NOT_FOUND` | 404 | 会话不存在 |
| `TOOL_NOT_ALLOWED` | 403 | 工具不在当前角色白名单内 |
| `TOOL_OFFLINE` | 409 | 工具所属插件已停用（跨 `tool_epoch` 恢复的历史调用） |
| `GUARD_BLOCKED` | 422 | 输出被安全审核拦下 |
| `MODEL_BACKEND_ERROR` | 502 | 模型后端全部失败（含回退链） |
| `INTERNAL` | 500 | 未归类错误 —— 对外只给这个 code，细节进日志 |

**消息里绝不能出现堆栈、SQL、文件路径。**

---

## 6. 评测集用例格式

用例存放在 `tests/eval/cases/*.json`，一个文件一个领域。**断言必须是确定性的，不允许引入 LLM 当裁判。**

```json
{
  "id": "health-001",
  "path": "tool_selection",
  "role_id": "medical_archivist",
  "enabled_domains": ["health"],
  "input": "上次检查的结石直径是多少",
  "expect_tools": ["query_health_record"],
  "expect_absent_tools": ["compare_health_index"],
  "assertions": [
    {"kind": "answer_contains_value", "value": 6.0, "tolerance": 0.01},
    {"kind": "answer_contains_marker", "marker": "未经人工校验"}
  ],
  "notes": "指标为 AI 提取，标记必须原样出现"
}
```

`path` 取值（**每一类都要有用例**）：
`tool_selection` / `no_tool` / `multi_tool` / `authz_denied` / `guard_blocked` / `no_data` / `role_fidelity`

`role_fidelity` 用确定性断言描述"像不像那个角色"：回答**不该出现什么**（诊断口吻、省略未校验标记）
以及**该用什么口径**。主观描述没有通过率，"不该出现什么"有。

`assertions[].kind` 取值：
`tool_called` / `tool_not_called` / `answer_contains_value` / `answer_contains_marker` / `blocked`

**两条硬性纪律：**

1. **先冻结用例，再改 prompt。** 任何为了让某条用例变绿而改 prompt 的动作，都要单独记录 —— 否则就是把测试当成了实现的一部分。
2. **留一组 holdout（20~30%）**，调优期间不看，最后才跑。没有 holdout 的通过率没有意义。

---

## 7. 完成定义（DoD）

一个任务算完成，必须同时满足：

- [ ] 目标文件的 TODO 已实现，且 TODO 注释已删除（不是注释掉）
- [ ] 有对应的单元测试或评测用例，且**在改动之前就是红的**
- [ ] `pytest` 全绿
- [ ] `ruff check .` 无告警
- [ ] `python scripts/check_consistency.py` 退出码为 0
- [ ] 没有新增依赖；如新增，已同步 `pyproject.toml` 与对应的 `requirements-*.txt`
- [ ] 没有改动第 4 节的任何文件
- [ ] 提交信息符合 Conventional Commits（`feat:` / `fix:` / `docs:` / `test:` / `refactor:` / `chore:`）

---

## 8. 环境准备

### 8.1 结论：三层环境，各管一段

| 环境 | 用途 | Python | 何时需要 |
| --- | --- | --- | --- |
| `.venv` | 主服务（M1~M4 全部） | 3.13 | **现在** |
| `.venv-ocr` | PaddleOCR 独立环境 | 3.13 | v2.2 |
| 容器 | 部署 | — | v2.4 |

**为什么 OCR 必须单独一个环境**：`paddleocr` 会拉入 `paddlex` 和一批二进制依赖（opencv / onnxruntime），装进主环境会显著拖慢每次依赖解析，并让主服务的升级被它锁住。它本来就该是独立进程。

### 8.2 推荐：用 `uv`（venv 的超集）

裸 `venv + pip` 只提供**隔离**，不提供**可复现**：没有锁文件，`pip install` 出来的东西会随时间漂移；本机也只有一个托管的 3.13 与 conda。`uv` 三件事一起解决——拿到 Python、生成真锁文件、解析快一个量级。

```powershell
winget install --id astral-sh.uv -e        # 或 pipx install uv

uv python install 3.13                     # uv 自己管理 Python，不依赖系统安装
uv venv --python 3.13
uv sync --extra api --extra dev            # 读 pyproject，生成 uv.lock
uv run python scripts/init_db.py
uv run pytest
uv run python scripts/check_consistency.py
```

### 8.3 兜底：纯 venv，用 conda 提供 Python 3.13

```powershell
conda create -n rc313 python=3.13 -y
conda run -n rc311 python -m venv .venv
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt -r requirements-api.txt
```

### 8.4 国内网络（很实际的一条）

`paddleocr` 会拉 `paddlex` 与一批二进制包，直连 PyPI 会慢到让人以为卡死：

```powershell
$env:UV_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
# 或 pip：
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

### 8.5 依赖的单一事实来源

- **`pyproject.toml` 是唯一事实来源**（`dependencies` + 四组 extras：`api` / `rag` / `cloud` / `dev`）。
- `requirements*.txt` 是给不用 uv 的人准备的**镜像**，按范围拆开：`requirements.txt` 只含 v1 内核，
  `-api` 接入层 / `-rag` 向量检索 / `-cloud` 云端 provider / `-dev` 开发工具 / `-ocr` PaddleOCR（独立环境）。
  `scripts/check_consistency.py` 会断言每一组 extras 与对应镜像文件的**包名集合一致**，改了一边不改另一边会被拦下。
- **v1 不做数据库迁移**：`bootstrap()` 用的是 `CREATE TABLE IF NOT EXISTS`，所以给已有库加列**不会生效**。
  改了 schema 就要重建库（删掉 `data/sqlite/app.db` 再跑 `init_db.py`）。生产要引入 Alembic 之类的迁移工具——
  这是有意留到 v2 的取舍，不是遗漏。

### 8.6 不要做的事

- ❌ 不要把 OCR 依赖装进 `.venv`
- ❌ 不要把 `.venv` 当部署产物（部署走容器）
- ❌ 不要提交 `uv.lock` 之外的锁文件，也不要在两处维护依赖清单
