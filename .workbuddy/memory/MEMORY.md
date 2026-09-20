# rolecard-agent · 项目长期记忆

> 只记"下次还会用到"的事实与约定。日常进展写 `YYYY-MM-DD.md`。

## 环境与沙箱（Windows + WorkBuddy）
- Bash PATH 断，先补：`export PATH="/c/Users/hcx/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:$PATH"`（根因 shim 第3行 dirname 算错自身目录）。PowerShell 不回显 stdout→用 Bash。
- 本机 Ollama：`D:\code\Ollama\ollama.exe`，`OLLAMA_MODELS=D:\code\Ollama\models`；服务不自动起，启动：`cd /d/code/Ollama && OLLAMA_MODELS="D:\\code\\Ollama\\models" ./ollama.exe serve`。
- 默认本地模型 `qwen3-vl:8b`（思考+识图+工具一体）。`qwen2.5:7b`/`qwen2.5vl:7b`/`local_vl` 已退役。
- CRLF 坑：Python `Path.write_text()` 在 Windows 写 CRLF，违反 `check_consistency` 的 LF 断言→批量改后用 `p.write_bytes(p.read_bytes().replace(b"\r\n",b"\n"))` 归一。
- 沙箱 MSYS git 会把 LF 工作树存成 CRLF blob → **不要在此环境 git commit**；改动留工作树交用户本地提交。判定 LF 唯一可信：`read_bytes()`。

## 验证命令（改完必跑）
```bash
export PATH=".../PortableGit/versions/1.2.0/usr/bin:$PATH"
cd /c/Users/hcx/Desktop/rolecard-agent
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m mypy
./.venv/Scripts/python.exe -m pytest -p no:cacheprovider -W ignore --cov=rolecard_agent --cov-fail-under=85
./.venv/Scripts/python.exe scripts/check_consistency.py
./.venv/Scripts/python.exe scripts/smoke_check.py
cd frontend && npm test && npm run build
```
覆盖率 85% 是硬门槛；`npm run build`=`tsc --noEmit && vite build`，tsc 红则 dist 不更新。

## 项目约定（不要违背）
- 角色要检索：`search_knowledge` 必须进 `tool_whitelist`（工具可见性由白名单决定，`knowledge_scopes` 只授权集合）。
- 知识库页（RAG/chroma）vs 数据页（域结构化指标）；新领域数据走"新建域插件"。
- 分层硬规则：`core/` 不出现域专名（含注释），机器校验 `check_core_no_health_token()`。
- api 层不 import 具体域模块；需域名/方法→加进 `core.domain_service.DomainQueryService` 协议。
- `ModelSettingsService.save()`：显式给回退链=严格校验；缺省=修剪为存活后端子集。
- run_command 审批（v2.6）：批准一次性（done 后只回放不重跑）；rejected 终态。审批/执行分写 operator/agent 审计。
- 破坏性管理动作三件套：只读盘点→前端二次确认→写审计。
- HTTP 语义：找不到=404(KeyError)，规则不允许=400(ValueError)。
- 工具重试仅对 `idempotent=True` 只读工具开放。
- 思考面板唯一实现 `components/chat/ThinkingPanel.tsx`；流结束即折叠，展开是用户点击。
- 新增 `Settings` 字段须同时改三处：字段(中文rationale)+env映射表+`.env.example`。
- 索引身份：`KnowledgeBase.index(scope, source, ...)` 中 `source` 是身份（唯一稳定），`source_name` 只是显示名；**绝不用文件名当身份**。

## 高频失败模式（写测试优先覆盖）
1. 测试替身不忠实→假阳性：fake factory 须照抄真实实现契约（含异常）。
2. 乐观 UI 被 checkpoint 回放整体替换冲掉：要留存的信息须进回放数据或独立 state（错误→toast；思考→`serialize_message` 带 reasoning）。
3. 字典序排 OOXML 部件：`slide10` 会排在 `slide2` 前→要数字序。
4. 评测必须 `--runs N` 聚合（7B 单次通过率是抽样），回归门按均值。
5. 同一 UI 元素两份实现→文字/行为漂移：用户先后看到的同一东西只允许一处实现。

## 关键事实
- ChatOllama 采样参数只能构造期设置：`.bind(temperature=)` 进顶层被 Ollama 忽略；角色 temp 走 `core/graph._init_model(..., temperature)`。
- chroma 1.5.9：新集合 `configuration={"hnsw":{"space":"cosine"}}`；HashEmbedder 相似度无标定意义，阈值按嵌入器标定或相对裁剪。
- SQLite 残留事务：清理必须在持连接的线程（`ThreadLocalConnection._current()` 代际检查）。
- 长 heredoc 不可靠→多行补丁用 Write 写临时脚本。
- 桌宠壳：`shell/`（Electron），`ROLECARD_PET=0` 关桌宠窗；前端 `pages/PetPage.tsx`；壳可起停本地 Ollama（D③-b）。

## 项目定位与用户意向
- 定位：自用/给朋友用，非公网部署；安全项(H1/H2/H3)用户接受暂缓。审查优先功能正确性/架构/性能。
- 文案：用户可见用「对话」非「会话」（标识符 session/thread_id 不动）。
- 2026-09-20：用户拿 **AstrBot** 对标本项目，关注**桌宠形态**与"项目是否只适合学习"的实用性疑虑。本项目已有 Electron 桌宠壳 + PetPage（D② 桌宠全系列已落地），与 AstrBot 桌宠方案属同类雏形而非"有无"差距。
