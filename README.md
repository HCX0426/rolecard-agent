# rolecard-agent

[![CI](https://github.com/HCX0426/rolecard-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/HCX0426/rolecard-agent/actions/workflows/ci.yml)
[![Release](https://github.com/HCX0426/rolecard-agent/actions/workflows/release.yml/badge.svg)](https://github.com/HCX0426/rolecard-agent/releases)
![GitHub Release](https://img.shields.io/github/v/release/HCX0426/rolecard-agent)
[![coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/HCX0426/rolecard-agent/main/docs/coverage-badge.json)](docs/gate-readings.json)

**角色卡驱动的本地优先对话 Agent**：给 AI 一张持续人设卡，配上工具、知识库与健康档案插件；
本地 Ollama 或任意 OpenAI 兼容云端皆可驱动，带 Web 控制台与桌宠桌面壳，可一键公网部署。
门禁现值见 [docs/gate-readings.json](docs/gate-readings.json)（自动写入，本页不抄数字）。

- **人设即配置**：角色卡声明人设、示例对话、可用工具白名单与知识范围；运行时切换，历史不丢。
- **工具与知识可插拔**：领域插件显式注册、界面启停即时生效；RAG 检索作为内核工具挂载。
- **本地优先**：默认对接 Ollama（`qwen3-vl:8b`，8GB 显存可跑）；数据全部落在本地 SQLite + Chroma。
- **可靠抽取**：上传报告自动走 schema 约束 + 确定性校验 + 原文锚定 + 第二模型交叉验证，
  只有两模型一致的项才入库，其余进「待确认」由人裁决。

---

## 快速开始

```bash
# 1. 环境（Python ≥ 3.13）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip

# 2. 依赖（锁安装：requirements.lock 覆盖全部运行时族，pip-compile 从 requirements.txt 等
#    五份输入产出；改了它们要重新 compile，lockfile parity 尺子盯着）
.\.venv\Scripts\python.exe -m pip install -r requirements.lock

# 3. 本地模型（可选 —— 不装 Ollama 也是完整可用的一等形态，
#    在设置页加一个 OpenAI 兼容后端即可；数据不出机器的边界由默认本地后端承担）
ollama pull qwen3-vl:8b

# 4. 配置
copy .env.example .env

# 5. 建表 + 初始化内置角色
.\.venv\Scripts\python.exe scripts/init_db.py

# 6. 一致性自检（文档与代码是否同步，退出码可用于 CI）
.\.venv\Scripts\python.exe scripts/check_consistency.py

# 7. 启动（控制台 + 流式对话）
.\.venv\Scripts\python.exe scripts/run_api.py
#    打开 http://127.0.0.1:8000/ —— 新建会话即可对话；换端口用环境变量 RUN_API_PORT
```

> 默认角色「通用助手」是纯对话；要玩健康档案功能切到「健康档案管理员」。
> 改了前端再构建：`cd frontend && npm ci && npm run build`（普通演示不需要 node，dist 已入库）。

---

## 界面预览

控制台六个页签 —— 刻意把最易混淆的三件事分开：
**数据**（领域数据，随域归属）· **知识库**（RAG 检索，内核能力）· **插件**（能力开关）。

| 对话 | 数据（可补录） | 知识库（RAG + 检索延迟） |
| --- | --- | --- |
| ![对话页](docs/assets/console-chat.png) | ![数据页](docs/assets/console-data.png) | ![知识库页](docs/assets/console-knowledge.png) |

| 角色卡（含范例 exemplars） | 插件（纯能力开关） | 设置（模型热切换 + 回退链） |
| --- | --- | --- |
| ![角色卡页](docs/assets/console-roles.png) | ![插件页](docs/assets/console-plugins.png) | ![设置页](docs/assets/console-settings.png) |

---

## 部署

**Docker**（dist 已入库，镜像里没有 node；以非 root 运行，带 HEALTHCHECK）：

```bash
docker build -t rolecard-agent .
docker run -p 8000:8000 -v rolecard-data:/app/data \
  -e AUTH_MODE=on -e AUTH_CREDENTIALS=<user>:<pass> rolecard-agent
```

> 镜像绑 0.0.0.0，而「非回环 + 无鉴权」是**拒绝启动**的硬护栏 —— 公网部署必须显式给鉴权。
> 容器形态不带本地 OCR（按设计裁剪）：需要 OCR 时在「服务」页配云端端点。

**公网（TLS + 域名）**：`docker-compose.yml` + `deploy/Caddyfile` 已就绪 —— 证书归 Caddy、
应用不直接暴露端口。四样环境变量：`ROLECARD_DOMAIN`（解析到这台机器的域名）、
`ROLECARD_CREDENTIALS`（`operator:you:<长密码>`，带前缀才有管理面）、可选
`SILICONFLOW_API_KEY` 与 `ROLECARD_RATE_LIMIT`；完整验证步骤见 `docker-compose.yml` 顶部注释。

**桌面壳**：`shell/` 是 Electron 桌宠壳，`npm run package` 打出随包后端的 NSIS 安装包
（GitHub Release 提供成品）。

---

## 架构一页

![架构总览](docs/assets/architecture.svg)

<sub>分层：客户端 → 接入层 → 内核 harness → 能力（RAG / 文档摄取）→ 领域插件 → 外部依赖。
核心约束：`core/` 内不出现任何领域专有名词（机器校验）；检索是内核能力，领域只声明作用域；
工具经「启用插件 → 角色白名单」两阶段过滤后才 bind 给模型 —— 看不到即调不到。</sub>

**插件开发**只需三步：在 `domains/<name>/` 实现 `models.py` / `service.py` / `tools.py` /
`schema.sql`，在 `domains/registry.py` 注册，初始化时装载 schema。启停在管理侧完成，
不暴露为 LLM 可调用的工具。

---

## 里程碑

| # | 内容 | 状态 |
| --- | --- | --- |
| **M1** | 对话内核：状态图、按角色 bind_tools、SQLite 检查点、可观测 | ✅ |
| **M2** | 角色卡 CRUD + 插件启停（tool_epoch 实时生效）+ 两阶段工具过滤 + 审计 | ✅ |
| **M3** | 领域插件（health 档案：结构化查询 / 对比 / 报告检索） | ✅ |
| **M4** | FastAPI 接入层 + SSE 流式对话 + 聊天 UI | ✅ |
| **M5** | 控制台前端工程化（六页签，dist 由 FastAPI 托管） | ✅ |

v2 系列：RAG 检索（v2.1）、文档摄取与可插拔 OCR（v2.2）、前端完善（v2.3）已落地；
公网部署面已写好（见上），生产化替换（Postgres/Milvus/Redis，v2.5）未开始。

---

## 质量与运维

- **门禁**：ruff + mypy（含 Linux 档）+ 后端 pytest + 前端 vitest + 一致性核查（70 条断言：
  依赖方向、文档链接、能力矩阵、路由访问分级…），全部离线；覆盖率阈值 90%（`pyproject` 单源）。
  现值见 [docs/gate-readings.json](docs/gate-readings.json)（门禁自动写入，本页不抄数字）。
- **安全边界**：路径穿越三重收敛、上传解压双闸、XFF 伪造链防护、命令执行审批（一次性令牌）、
  限流、来源标识护栏；密钥只写不回读，凭据不进审计与日志。
- **运维接口**：`/api/health`（免鉴权探活，编排器用）与 `/api/health/deep`（真依赖逐项，
  operator 凭据）刻意分开；`/api/metrics` 输出 Prometheus 文本（只数事件名，不含用户文本）。
- **依赖**：锁安装（`requirements.lock`），`pyproject.toml` 是唯一事实来源；OCR 依赖独立 venv。

---

## 文档

| 文件 | 内容 |
| --- | --- |
| [`docs/架构总览.md`](docs/架构总览.md) | 单一事实源：定位/双壳 · 技术栈 · 模块地图 · 关键不变式 · 安全模型 |
| [`docs/需求与验收标准.md`](docs/需求与验收标准.md) | PRD、用户故事、评测集设计、v1/v2 边界 |
| [`docs/前端设计.md`](docs/前端设计.md) | 前端分层/组件约定 · IA · 深色/响应式 |
| [`docs/开发流程.md`](docs/开发流程.md) | 设计先行四步 + 门禁分层用法 + 提交约定 |
| [`docs/架构审计索引.md`](docs/架构审计索引.md) | 历史审查台账的编号索引（含归档件） |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 协作规约：铁律、接口契约、错误码、环境准备 |

---

## 免责声明

本项目为技术学习与工程实践作品，演示数据全部虚构。
系统设计上禁止输出任何疾病诊断、用药建议或治疗方案，仅对已入库档案数据做汇总与查询。
AI 自动提取的指标默认标记为「未经人工校验」，不构成任何医疗建议。
