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
| 2 | **不改 `core/agent/prompts.py` 的 `GLOBAL_SAFETY_PROMPT`，也不改它的拼接顺序** | 顺序反了 = 安全层静默失效，而且不会有任何报错 |
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

**第 6 步的输出不许和 `| grep`、`&&` 串在同一条命令里再决定要不要提交。** 判据要单独落盘、单独读
退出码：`python scripts/check_consistency.py > build/cons.log 2>&1; echo exit=$?`。
理由不是理论 —— 09-30 有两次把红的推上了 `main`，同一条形状：一次是 `... | grep FAIL && git commit`
（`grep` 在**找到**失败行时返回 0，于是"检查没过"被翻译成"可以提交"），一次是 `... | grep assertions: && git add && git commit && git push`
（`grep` 抓到了那行计数就返回 0，根本没看它是 38 还是 39）。**管道末端那条命令的退出码不是检查的退出码。**

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
| `core/agent/prompts.py` | 安全规则的唯一来源与拼接顺序 |
| `core/schema.sql` | 内核数据契约（identity / session / plugin / audit） |
| `roles/schema.sql` | 角色卡数据契约 |
| `core/agent/guard.py` 的 `check()` 契约 | 硬拦截的接口；改语义会让 fail-closed 失效 |
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
- [ ] 已启用本地 git 钩子（一次性：`git config core.hooksPath .githooks`）—— 改前端源码时
      pre-commit 会自动重建 `frontend/dist` 并随提交入库；dist 必须与源码同一次提交
      （clone 免 node 可跑 + 随包后端托管都靠它，CI 的回写 bot 只兜 main）。

---

## 8. 环境准备

### 8.1 结论：三层环境，各管一段

| 环境 | 用途 | Python | 何时需要 |
| --- | --- | --- | --- |
| `.venv` | 主服务（M1~M4 全部） | 3.13 | **现在** |
| `.venv-ocr` | RapidOCR 独立环境 | 3.13 | v2.2 |
| 容器 | 部署 | — | v2.4 |

**为什么 OCR 必须单独一个环境**：OCR 引擎会拉入 `opencv`（本机实测 113 MB）、`onnxruntime` 与一批
二进制依赖，装进主环境会显著拖慢每次依赖解析，并让主服务的升级被它锁住。它本来就该是独立进程。
（2026-10-02 换后端时订正：从前这里写的是"`paddleocr` 会拉 `paddlex`…与主环境的 numpy 版本主张
冲突"——那条**被迫**的理由在 RapidOCR 上已不成立（它的 numpy 约束与主环境兼容，主环境本来就有
onnxruntime）。现在维持隔离的两条理由是选择：运行树不该带这一族；随包形态按设计不含 OCR。
现行完整口径以 `requirements/requirements-ocr.txt` 开头那一段为准，别在两处各写一套。）

### 8.2 主环境：`.venv` + pip + 锁安装（2026-10-07 拍板「上锁文件」）

> 历史：曾推荐 uv 并提交 `uv.lock`（2026-09-17 弃用——本机无 uv、锁死资产）；此后一段时间
> 版本不锁是刻意取舍，范围镜像 + `dependency parity` 保证包集合一致，版本漂移由 CI 每次
> 重装暴露。**那段取舍已废止**：无锁时代的真代价是「fresh install 不可重现」——CI 装到的
> 版本与本机、与上周各不相同，坏在传递依赖的升级上时无人能指认。现在的口径：**镜像管
> "要什么"，锁管"装什么"** —— `requirements*.txt` 仍是唯一事实来源 pyproject 的 pip 安装
> 镜像（dependency parity 看着），`pip-compile` 从它们产出两把锁：
> `requirements/requirements.lock`（运行时五族 + dev，README / CI 三臂用）、`requirements/requirements-runtime.lock`
> （纯运行时五族，install.bat / Dockerfile 用）。改了 requirements*.txt **必须重新 compile**
> 刷新锁，`lockfile parity` 尺子逐约束比对（镜像的每条约束 ∈ 锁的 pin）会当场红。
> ⚠️ 刷新后若头部被本机 pip 配置写进 `--index-url`/`--trusted-host`，删掉再提交——锁不钉镜像源。
>
> **安装器可以不同，锁必须同一把（2026-10-10）**：CI 四个 arm 改用
> `uv pip install --system -r <锁>` 提速 —— 本机对照实测冷装 pip 303.9s vs uv 27.8s（约
> 11×），包集逐条比对零缺包零版本差异，uv 装出的 venv 上 pytest / 一致性 70/70 /
> `create_app` 全部真跑通。**本地开发路径（README / install.bat / Dockerfile / 你自己的
> `.venv`）仍用 pip**：锁的契约本来就是"pip 装得上"，`lock-refresh` 那两条验证作业也照旧
> 用 pip（那是它的职责）。P1-12 钉的是**锁这个产物与"按锁安装"**，安装器不是契约面 ——
> 那条决策的备注里原话就是「其定案理由『本机无 uv』已不成立」。`installer scope parity`
> 那条尺子认的是"被认识的安装器 verb"（`_INSTALL_VERBS`），换第三个安装器时它会红，
> 提醒你把 verb 登记进去 —— 不要让它靠子串侥幸通过。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip

.\.venv\Scripts\python.exe -m pip install -r requirements/requirements.lock
# （锁覆盖运行时五族 + dev，含 pyinstaller / pip-tools / packaging；缺族的的历史教训
#   记在 Dockerfile 注释与 installer scope parity 尺子里 —— 锁治版本漂，不治漏装一族）

.\.venv\Scripts\python.exe scripts\init_db.py
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\check_consistency.py
```

### 8.3 OCR 独立环境：`.venv-ocr`（OCR 引擎必须隔离）

```powershell
python -m venv .venv-ocr
.\.venv-ocr\Scripts\python.exe -m pip install -U pip
.\.venv-ocr\Scripts\python.exe -m pip install -r requirements/requirements-ocr.txt
# 主服务通过 OCR_PYTHON=/path/to/.venv-ocr/Scripts/python.exe 调用（默认自动发现）
```

### 8.4 国内网络（很实际的一条）

OCR 那一份会拉 `onnxruntime` 与 `opencv` 两个大轮子（合计 150 MB 级），直连 PyPI 会慢到让人以为卡死：

```powershell
# pip 走清华镜像（onnxruntime / opencv 这类大轮子直连 PyPI 极慢）：
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

### 8.5 依赖的单一事实来源

- **`pyproject.toml` 是唯一事实来源**（`dependencies` + 四组 extras：`api` / `rag` / `cloud` / `dev`）。
- `requirements*.txt` 是 **pip 安装镜像**，按范围拆开：`requirements/requirements.txt` 只含 v1 内核，
  `-api` 接入层 / `-rag` 向量检索 / `-cloud` 云端 provider / `-dev` 开发工具 / `-ocr` RapidOCR（独立环境）。
  `scripts/check_consistency.py` 会断言每一组 extras 与对应镜像文件的**约束逐条一致**，改了一边不改另一边会被拦下。
- **锁文件（2026-10-07 起）**：`requirements/requirements.lock` 与 `requirements/requirements-runtime.lock` 由
  `pip-compile` 从镜像产出（刷新：`.venv\Scripts\python.exe -m piptools compile --output-file=requirements/requirements.lock requirements/requirements.txt requirements/requirements-api.txt requirements/requirements-rag.txt requirements/requirements-cloud.txt requirements/requirements-mcp.txt requirements/requirements-dev.txt`，runtime 锁去掉 dev 那份；生成后删掉本机 pip 配置写进来的 `--index-url`/`--trusted-host` 两行）。
  CI / 镜像 / 打包臂只从锁安装；镜像与锁的覆盖关系钉在 `check_consistency.py` 的
  `LOCK_SURFACES`，由 `lockfile parity` 尺子逐约束对账。
- **数据库有迁移，别再写"删库重建"**（本条 2026-09-25 订正"v1 不做迁移"；2026-10-02 再订正：
  下面三句教的是上一代机制——`R102-30`）。现行机制分两层：
  ① **列级迁移全部声明驱动**：`storage/db.py::reconcile_columns` 按 `core/schema.sql` 与
  `domains/*/schema.sql` 的声明补列（**核心表与域表都覆盖**），bootstrap 在跑 DDL 前后各调一次。
  加列通常**只改 schema 声明一处**，不用手写 ALTER。
  ② `_migrate` 只管"补列器补不了"的形状迁移（当前 6 处 `ALTER`、其中 `ADD COLUMN` 3 处；
  整表重建与搬层各自带前置 DROP 与显式事务）。
  **仍然要三处一起改的只剩一种**：`model_settings.py::migrate_to_provider_layers` 里那份
  `model_backend__layers` 内联 DDL（它抄了 schema 的十列，漏改它会在老库搬层时炸
  `no such column`）——分叉已由一致性断言 `shape migration DDL` 看住（`R102-11`），改列时两边
  一起改、改漏当场红。"从老形状库跑 bootstrap 再比列集合"的测试在
  `tests/integration/test_legacy_schema_upgrade.py`（`R26-04` 补的那条门禁）。

### 8.6 不要做的事

- ❌ 不要把 OCR 依赖装进 `.venv`
- ❌ 不要把 `.venv` 当部署产物（部署走容器）
- ❌ 不要在两处维护依赖清单（依赖只在 `pyproject.toml` + 镜像 `requirements*.txt` 里维护；
  两把锁 `requirements/requirements.lock` / `requirements/requirements-runtime.lock` 是 `pip-compile` 的**产物**，
  改了镜像必须重新 compile 刷新——手改锁文件等于伪造事实面，`lockfile parity` 尺子会红）
- ❌ 不要绕过锁安装（CI / 镜像 / 打包臂只从锁装；安装器用 pip 还是 uv 都行——见 §8.2
  「安装器可以不同，锁必须同一把」）
