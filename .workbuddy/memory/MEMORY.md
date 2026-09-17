# rolecard-agent · 项目长期记忆

> 只记"下次还会用到"的事实与约定。日常进展写 `YYYY-MM-DD.md`。

## 环境（Windows + WorkBuddy 沙箱）

- **Bash 工具的 PATH 是断的**，所有命令都要先补：
  `export PATH="/c/Users/hcx/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:$PATH"`
  根因：宿主 `BASH_ENV` 指向的 shim 第 3 行用 `dirname` 计算自身目录，而该目录不在 PATH 上
  → 命令替换失败 → `cd: null directory` → coreutils（`ls/find/head/sed/awk/wc`）全部不可用，
  连带 `safe-bin/` 与沙箱钩子也不会被 source。**这不是项目问题，别去查代码。**
- PowerShell 工具在本机**不回显 stdout**（exit 0 但无输出）→ 一律用 Bash + 上面的 PATH。
- **本机有 Ollama**：`D:\code\Ollama\ollama.exe`，模型在 `OLLAMA_MODELS=D:\code\Ollama\models`
  （`qwen2.5:7b` 支持 tools、`qwen2.5vl:7b`）。服务不自动启动 →
  `cd /d/code/Ollama && OLLAMA_MODELS="D:\\code\\Ollama\\models" ./ollama.exe serve`（后台），
  就绪探针 `curl http://127.0.0.1:11434/api/tags`。**找本机服务先查环境变量再猜安装路径。**
- **坑：用 Python 的 `Path.write_text()` 改仓库文件会在 Windows 上写入 CRLF**，而
  `scripts/check_consistency.py` 的 `line endings` 断言要求 `src/ tests/ scripts/` 全 LF。
  批量改完文件后必须归一：
  `p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n"))`
  （用 `Edit` / `Write` 工具改文件则不受影响。）

## 验证命令（改完必跑，五条）

```bash
export PATH="/c/Users/hcx/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:$PATH"
cd /c/Users/hcx/Desktop/rolecard-agent
./.venv/Scripts/python.exe -m ruff check .            # 零告警
./.venv/Scripts/python.exe -m mypy                    # 50 文件零问题
./.venv/Scripts/python.exe -m pytest -p no:cacheprovider -W ignore \
    --cov=rolecard_agent --cov-fail-under=85          # 覆盖率门槛是真在拦人
./.venv/Scripts/python.exe scripts/check_consistency.py
./.venv/Scripts/python.exe scripts/smoke_check.py     # 12/12
cd frontend && npm test && npm run build
```

**覆盖率是硬约束**：新增未覆盖代码会把总量压到 85% 以下并让 CI 红（实测曾掉到 84.66%）。
加功能就要同时补测试，或者先补掉现存盲区（`api/routers/services.py` 曾是 38%）。

## 项目约定（改动时不要违背）

- **角色要能检索，`search_knowledge` 必须进 `tool_whitelist`**：`knowledge_scopes` 只授权
  "集合"，工具可见性由白名单决定——两个都配了角色才真的检索得到（elysia 卡踩过）。
- **知识库页 vs 数据页**：RAG 语料（设定集/文档）→ 知识库作用域（chroma）；数据页签只管
  health 插件的结构化指标。新领域数据要走"新建领域插件"而非塞进数据页。
- 停/查端口进程：`netstat -ano | grep :8000 | grep LISTENING` 的 PID 带 CRLF，必须
  `tr -d '\r\n '` 再传给 taskkill，且要 `MSYS_NO_PATHCONV=1 taskkill /F /PID $PID`。
- **后台服务必须用 Bash 工具的 run_in_background 启动**（`exec` 直接替换进程）：
  普通会话里 `(cmd &)` 起的子进程会随会话结束被杀。
- 分层硬规则：`src/rolecard_agent/core/` 内**不出现 `health`**（已机器校验：
  `scripts/check_consistency.py` 的 `check_core_no_health_token()`，注释也算——注释里的
  域词是概念泄漏的早期信号）。
- `ModelSettingsService.save()` 的回退链语义：**显式给链 = 严格校验（手滑大声拒绝）；
  缺省 = 保留当前值但修剪为存活后端子集**（与运行时 `resolve_fallbacks` 丢弃未知名对齐）。
  改这两处任何一边都要同步另一边的语义假设。
- 破坏性管理动作三件套：**先只读盘点 → 前端二次确认 → 写审计**
  （`reset_knowledge_scope`、`cleanup_orphan_uploads` 都是这个形状）。
- 每一层都要"失败时说人话"：`ParseError` / `ExtractError` / `HealthInvalidReport` 等
  都带可读中文原因，绝不把栈或内部路径抛给用户。
- HTTP 状态语义：**找不到 = 404（`KeyError`）**、**规则不允许 = 400（`ValueError`）**。
  服务层用异常类型承载这个区分，路由据此映射 —— 两者混用会让 404 分支变成死代码。
- 工具重试只对**显式声明 `idempotent=True`** 的只读工具开放（默认不重试）。
- 前端：`vitest` 用 esbuild 转译、**不做类型检查**，所以测试文件靠 `tsconfig.include` 纳入
  `tsc`；测试默认环境是 `node`，需要 DOM 的文件首行写 `// @vitest-environment jsdom`。
- 类型：`Settings` / `AppContext` 的字段都要"有人读"，一致性脚本会检查死配置。
- **思考面板（用户定案）**：唯一实现 `components/chat/ThinkingPanel.tsx`（UI 组件不放 `lib/`）。
  流式期间默认展开（"正在思考"是过程信号）；**流一结束就折叠**，展开是用户的点击动作
  ——"刚结束的一轮保持展开"已被用户否决。折叠层数要与内容层数匹配：单步思考直接是
  那一层 `details`，不套「过程」外壳；多步（思考×N/工具混合）才合并 + 逐段折叠。

## 反复出现的失败模式（写测试时优先覆盖这些）

1. **测试替身不忠实 → 假阳性**：真实 `build_model` 对未知后端名抛 `KeyError`，
   而 fake factory 曾经对任何名字都返回模型 → "降级"测试从未走到降级分支。
   写 fake 时必须照抄真实实现的契约（含异常）。
2. **乐观 UI 被回放冲掉（本项目已踩三次：错误文案、裁剪提示、思考过程）**：ChatPage 流结束
   后用 checkpoint 回放**整体替换**消息区 —— 任何"只挂在临时气泡上"的内容都会当场消失。
   **判据：只要这个信息回答完还要能看，就必须进回放数据或独立 state**：
   错误→toast；裁剪→独立 notice + 独立端点；思考→`serialize_message` 带 `reasoning`，
   回放的助手消息再渲染一次。
3. **按字典序排序 OOXML 部件**：`slide10.xml` 会排在 `slide2.xml` 前 → 页序错乱。
   凡 `sorted()` 排带编号的东西，先想一遍是不是要数字序。
4. **只在开头扫 N 字节的安全检查**：XML 允许 DOCTYPE 前塞注释填充 → 有绕过窗口。
   安全校验要么扫全量，要么有明确的上界理由。
5. **流式解析把半帧当整帧**：SSE 的帧边界跨 chunk 时，半帧必须留在缓冲区。
6. **角色范例不能是评测题的答案**：范例（few-shot）原样进 system prompt，若它恰好是某道
   评测题的完整答案，模型会背范例而不是调工具（数值/标记断言都"过"，只有工具调用失败，
   极难排查）。一致性脚本 `exemplar leaks eval answers` 可机器拦。
7. **单次评测通过率是抽样值**：7B 模型 temperature=0.3 下，同一评测集相邻两遍可出现
   100% 与 57%。评测必须 `--runs N` 聚合，回归门按**均值**卡。
8. **插入测试时锚点过窄**：以 `def test_x(` 单行作 `old_string` 插入，会吃掉下一个测试的
   函数签名。锚点必须含完整函数签名（def 行 + 参数行）。
9. **"没有就跳过"的断言会假绿**：`ui_smoke` 的思考断言只在模型真输出思考时才跑
   （没有就 print 一句跳过、计为通过）→ 它红了好几轮没人知道，"12/12" 是跳过换来的。
   凡条件跳过的断言：断言要拆细（一个复合断言 `visible && open` 失败时分不清哪半错了），
   并且要么用可控替身覆盖该分支，要么让跳过留下显眼痕迹。
10. **同一 UI 元素有两份实现 → 文字/行为必然漂移**：思考面板曾在流式气泡（`lib/ThinkingPanel`，
   标题"思考过程"）与回放（`ProcessPanel`，标题"过程 · 思考 ×1"/"思考 1"）各写一份，
   于是"经历流式→回放"的同一个东西在用户眼里变了样，冒烟也按其中一份写死了文字。
   **判据：用户会先后看到的同一个东西，只允许有一处实现**（组件复用，而不是复制样式）。

## 项目定位与审查优先级取舍（2026-09-17 用户明确）

- **本工具定位：自用 / 给朋友用**，不是面向公网的部署。
- **安全与隐私类审查项暂不计入优先级**：鉴权默认关闭（H1）、api_key 明文落盘（H2）、
  OCR 云端复用 base_url 的隐私外泄面（H3）——用户明确接受，暂缓。
  → 审查报告 审查报告（已归档移除） 中这三项标记为"用户接受/暂缓"。
- **审查修复队列优先功能正确性 / 架构 / 性能**：H5（effective 重新并入 env 后端→UI 删后端不生效）、
  H4（路由直写 SQL 违分层）、M 系列（同步阻塞、资源泄漏、前端 key={i}、base_url 无校验等）、
  L 系列（冗余/命名/可维护性）。

## 文案与术语（2026-09-17 用户明确）

- **用户可见文本统一用「对话」**，不用「会话」（页面名与主按钮早已是「对话」）。
  代码标识符与注释不跟着改：`sessions` / `thread_id` / `/api/sessions` 是契约。
- 改完文案的复查手法：真机抓 `body.innerText` + 所有 `[title]`/`[aria-label]`/`[placeholder]`
  是否还含旧词 —— 只 grep 源码会把注释/docstring 一起算进去，判不准"用户还看不看得见"。
11. **子代理的可量化断言必须自己复跑**：审查/调研报告里"零覆盖""不生效""从不被调用"最容易错
    （只看了一半代码）。本项目实测否掉过两条：`.env 是死配置`（`run_api.py:83` 有 `_load_dotenv`）、
    `core/services.py 零覆盖`（实际 96%）。只信能自己跑出来的证据。

## 索引身份 vs 展示名（2026-09-17 P0 修复定下的契约）

- `KnowledgeBase.index(scope, source, text, *, source_name=None)`：**`source` 是索引身份**
  （分块 id 由它推导、旧分块按它清理，必须唯一稳定），`source_name` 只是给人看的名字。
  上传路径传 `task_id`，文件名走 `source_name`。**永远不要用文件名当身份**——同名文件会
  静默互相覆盖（`Hit.source_key` 保留身份以便排障）。
- 新增 `Settings` 字段必须**同时**改三处：字段（带中文 rationale）、env 映射表、`.env.example`
  ——一致性脚本会拦 `.env.example` 漏键。
- 超时/参数的传法**因客户端而异**：`ChatOllama` 只认 `client_kwargs`（直接传 `timeout` 会被
  静默丢弃），`init_chat_model` 的 openai 兼容路径才认 `timeout`。断言要读**真实客户端**
  （`model._client._client.timeout`），只断言 kwargs 字典会漏掉这类 bug。
