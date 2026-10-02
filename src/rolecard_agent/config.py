"""Env-driven configuration.

Same codebase, three deployment topologies - only the config differs:
  A all-local | B app on cloud + model at home | C all-cloud.
Ollama exposes BOTH an OpenAI-compatible endpoint (/v1/chat/completions) and a native one
(/api/chat); provider decides which style is used - `ollama` speaks native (base_url WITHOUT
/v1), `openai` speaks OpenAI-compatible (base_url WITH /v1). A "local model" and a "cloud API"
are the SAME provider to this code - only base_url changes.

Deliberately dependency-free: no pydantic-settings. Parsing is ~40 lines and keeping it
in-house means one less package for the free-model contributors to keep aligned.

Contract owner: this file. .env.example mirrors it, and scripts/check_consistency.py asserts
the two key sets stay in sync.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Any：num_ctx 是 int|None，dict[str,str] 会让 mypy 逐字段校验失败；展开时由 pydantic 把关。
DEFAULT_LOCAL_BACKEND: dict[str, Any] = {
    "provider": "ollama",
    # provider=ollama 走 langchain-ollama 的 Ollama 原生端点（/api/chat），base_url 不带 /v1；
    # OpenAI 兼容端点（http://localhost:11434/v1/chat/completions）是 provider=openai 时用的。
    # 带错 /v1 的症状是 Ollama 返回 "404 page not found"——端点风格由 provider 决定。
    "base_url": "http://localhost:11434",
    # 2026-09-17 终版：默认本地模型 = **qwen3-vl:8b**（对话 + 工具调用 + 识图 + 思考一体，
    # 一行多用，不再为"视觉"单独配后端行）。
    # 沿革（结论，不是过程）：qwen2.5:7b / qwen2.5vl:7b / local_vl **均已退役** ——
    # vl 版官方模板不支持 tools（bind_tools 400），而带工具的档案管理员是对话主路径；
    # 两个 7B 也不能同时驻留 8GB 显存。**qwen2.5:7b 已不再使用**，配置与文档都不应再引用它。
    "model": "qwen3-vl:8b",
    # L11：不设 api_key（ModelBackend 默认 None）。此前占位 "ollama" 会被 deep_check
    # 拼成 `Bearer ollama` 发出去（无效凭据还进日志）；graph.build_model 与 deep_check
    # 都按真值判断，None = 不带认证头。Ollama 本来就不需要 key。
}

# 多模态后端（vl = 文本版超集）按需添加：图片直读对话 / 备用抽取。
# 配置示例见 .env.example 的 MODEL_BACKENDS 注释 —— 不进默认后端集，避免
# "定义了但没人消费"的死配置（那是 P15 类问题）。

#: 已经退役、任何配置示例与文档都**不该再出现**的本地模型名（09-28 轮 `R28-09`）。
#: 写成数据而不是散文，是为了让门禁能查：`.env.example` 曾经把 `qwen2.5vl:7b` 当示例、
#: 又写"对话模型必须是文本版 qwen2.5:7b" —— 照官方引导配出来的第一步就是已知会 400 的
#: `bind_tools`，而这份文件是新用户唯一会照着敲的东西。
#: 上面那段沿革注释是**结论的来源**，这一份是它的机器可读形式；两处冲突时以这里为准。
RETIRED_LOCAL_MODELS: tuple[str, ...] = ("qwen2.5:7b", "qwen2.5vl:7b")

# Measured advice, not a hard limit of the framework: a longer chain makes a failure harder to
# localise, and it hides "the answer got worse after degrading" from whoever reads the logs
# (实施计划.md §8.5).
MAX_FALLBACKS = 2

# SiliconFlow 的 OpenAI 兼容端点（嵌入 bge-m3 / 重排 bge-reranker 共用一个 base_url）。
# 之所以是**一个常量**而不是散在各工厂里的字面量：以前 `os.environ.get("SILICONFLOW_BASE_URL",
# "https://api.siliconflow.cn/v1")` 在 rag/retriever.py 里写了 5 遍，改默认值得找 5 处，
# 而漏掉一处就是"有的调用走了新端点、有的没有"（架构审计报告 P1-4）。
DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"

#: **「哪些 Settings 字段是密钥」只在这里答一次**（`R28-14` 的②）。
#: 从前有两份平行清单：界面侧 `_SECRET_FIELDS` 列 5 项，而可编辑注册表按
#: `FieldSpec(kind="secret")` 标 2 项 —— 两个口径各自成立就意味着**第三个字段**（比如将来新增的
#: 某个 key）可以只在一处被标成密钥，另一处照发明文。现在 `runtime_settings` 在装配时就拿这一份
#: 校验自己（不在这里登记的 secret 字段 ⇒ 直接抛），界面侧只引用它。
SECRET_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "tavily_api_key",
        "saucenao_api_key",
        "langsmith_api_key",
        "auth_credentials",
        "auth_api_keys",
    }
)

#: 布尔型取值的写法集合（`R28-14` 的③）：`config.py` 读 env 与 `runtime_settings` 读界面提交，
#: 从前各列一遍同一族字符串。两处集合一旦不同，症状是"在 .env 里写 yes 有效、在界面上写 yes 报
#: 错"（或反过来）—— 这种不一致没人会在第一次撞上时怀疑到"两份清单"上。
TRUTHY_STRINGS: frozenset[str] = frozenset({"1", "true", "yes", "on"})
FALSY_STRINGS: frozenset[str] = frozenset({"0", "false", "no", "off"})


def env_truthy(raw: str) -> bool:
    """这个字符串按 env 的写法算不算"开"。界面上那一份解析走 `runtime_settings._parse`，
    它判的是同一组集合 —— 两处各列一遍的结果是"在 .env 里写 yes 有效、在界面上写 yes 报错"。
    """
    return raw.strip().lower() in TRUTHY_STRINGS


def env_falsy(raw: str) -> bool:
    """`env_truthy` 的另一半：三态解析（开 / 关 / 不合法）里"关"那一档也只有一个出处。"""
    return raw.strip().lower() in FALSY_STRINGS


class ModelBackend(BaseModel):
    """One callable model endpoint.

    `provider` names the LangChain integration to use (`ollama`, `openai`, ...). It defaults
    to `ollama` so the common case needs no extra field, and it exists because the provider
    package must actually be installed - a cloud backend pointing at an OpenAI-compatible
    endpoint needs `langchain-openai`, which the local-only v1 install does not ship.
    """

    # `extra="forbid"`（09-26 轮 S-1）：`MODEL_BACKENDS` 是一段**手写 JSON**，从前拼错一个
    # 字段名会被 pydantic 静默丢掉、那一项用默认值跑起来 —— 症状是"我明明设了 num_ctx"而
    # 它从来没生效，且没有任何地方会红。改成当场报错：写配置文件的人越早听见越好。
    model_config = ConfigDict(extra="forbid")

    model: str
    base_url: str | None = None
    api_key: str | None = None
    provider: str = "ollama"
    # 本地 Ollama 的**实际上下文窗口**（tokens，映射到 ChatOllama 的 num_ctx）。
    # None = 用引擎/模型默认（Ollama 常默认为 2048，比模型自带的 32k 小得多）。
    # 只对 native 风格（Ollama）生效：云端窗口由服务商固定，传了也会被忽略。
    num_ctx: int | None = None
    # 模型页是云端端点配置的唯一事实面；usage 标记该行服务谁：
    # chat=对话推理（对话菜单/角色路由只消费这类）| embedding | rerank | ocr（凭据行）。
    usage: str = "chat"
    # 后端能力位（换模型对所有角色统一生效的关键，用户 2026-09-19）：
    # - supports_vision：能否收图。云端模型无法像 Ollama 那样探测视觉，故显式声明。
    #   消费方两处：① 模型页/对话页渲染「视觉」徽标；② **调用前拦截的一半证据**
    #   （P1-2）：声明 false **且** Ollama `/api/show` 实测也不含 vision 才拦；声明 true
    #   直接放行且不去探测。它**不**控制发图按钮 disabled —— 判定只在一处（后端），
    #   界面复制一份就是第二个事实面。
    # - supports_tools：工具调用是否可用。某些云端 VLM（如 SiliconFlow Qwen3-VL-30B-A3B）
    #   一旦请求附 tools 就返回空，标 false 后 call_model 这轮不绑工具，模型仍能正常答。
    supports_vision: bool = False
    supports_tools: bool = True
    # 采样惩罚（设计稿 §8.2 那条"我们只暴露了 num_ctx/temperature，惩罚项没露"的补课）。
    # **三个都默认 None = 不传 = 引擎默认**，不是"出厂给个更聪明的值"：小模型上调惩罚容易
    # 伤连贯（§8.2 原话），所以界面只负责"能设"，推荐值得等 `persona_meter` 的数出来再说。
    # 语义按 provider 分家，见 `core/graph.py::_init_model`：
    # - repeat_penalty：Ollama 专有（≥0，1=不惩罚）。OpenAI 兼容体里没有这个标准字段，
    #   给云端传它等于赌服务商实现 —— 所以云端那一栏在界面上**不出现**。
    # - frequency_penalty / presence_penalty：两类都有，但区间不同（Ollama ≥0；
    #   OpenAI 兼容 −2..2），校验也按这个分（见 `model_settings.set_sampling`）。
    repeat_penalty: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None


class McpServerConfig(BaseModel):
    """One external MCP server whose tools become domain="mcp" tools.

    `id` is the stable identity used for the tool-name prefix (collision avoidance) and the
    audit `target` - never the display name. A server is either stdio (a local process the user
    installs) or http (a remote third-party service, deferred scope).
    """

    id: str
    transport: str = "stdio"  # "stdio" | "http"
    # stdio: a local process launched by us.
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # http: a remote MCP endpoint.
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class Settings(BaseModel):
    """Resolved configuration. Build with `Settings.from_env()`; treat as immutable."""

    model_backends: dict[str, ModelBackend] = Field(
        default_factory=lambda: {"local": ModelBackend(**DEFAULT_LOCAL_BACKEND)}
    )
    model_default: str = "local"
    model_fallbacks: list[str] = Field(default_factory=list)

    # 单次模型调用的超时（秒）。没有它，Ollama 挂起时 SSE 对话与抽取会**无限等待** ——
    # 后果不只是卡一个请求：线程池被占满，且 `with_fallbacks` 永远触发不了（主模型
    # "既不返回也不失败"）。设成 0 或负数 = 不设超时（保持旧行为，仅调试用）。
    model_timeout_seconds: float = 120.0

    # 启动即把默认本地模型 `keep_alive=-1` 常驻显存（免首条消息冷加载）。
    # 为什么可关：预热是**真 POST /api/generate**，会把 8B 钉进显存与任何在跑的推理争 GPU；
    # 测试套件与只做 API 装配的场景必须能关掉它（见 tests/conftest.py 的离线纪律）。
    # 只在进程构建期读取，改了要重启 —— 与其它"随进程构建"的项同类，不在线可改。
    model_pin_on_startup: bool = True

    # 思考（reasoning）模式：这里列出的**模型名**在 ollama 风格后端上会以
    # `reasoning=True` 调用（langchain-ollama ≥1.1 把思考内容放进
    # AIMessage.additional_kwargs['reasoning_content']，由 SSE 的 thinking 事件透出）。
    # 为什么按模型名而不是全局开关：对不支持思考的模型传 reasoning=True 会直接 400
    # （实测 qwen2.5:7b）。**列进来不会让她变慢，不列也不会变快** —— 思考模型在引擎侧
    # 一律在想（`think:false` 与 `/no_think` 实测都关不掉，见 09-26 轮 R26-29），这一档
    # 只决定那份思考**显不显示**（不显示时它照旧生成、照旧占首字，只是屏幕上看不见）。
    # **默认就列上随包那个模型**：藏起来并不能省下一秒，只会让用户对着一块不动的泡
    # （09-26 用户就是为这个提的"要让她看起来在打字"）。名单只对精确同名的 model 生效，
    # 换成别的模型的人不受这颗默认值影响。例：MODEL_THINKING_MODELS=qwen3:8b
    # 随包那个模型默认要显示思考过程（上面那段"刻意没有裸 on"的理由）。这个名字**不写第二遍**
    # （`R28-13`）：换默认本地模型时，这里跟着 `DEFAULT_LOCAL_BACKEND` 走，不用记得改两处。
    model_thinking_models: list[str] = Field(
        default_factory=lambda: [str(DEFAULT_LOCAL_BACKEND["model"])]
    )

    # 思考模式**总开关**（用户 2026-09-17）：auto = 按 MODEL_THINKING_MODELS 名单自动；
    # off = 名单内的模型也不传 reasoning —— 那是**把它藏起来**，不是把它停掉（性能上
    # 零收益，见上面那条）。刻意没有裸 "on"：对不在名单里的模型传 reasoning=True 会直接
    # 400，想给新模型开思考 = 把它加进 MODEL_THINKING_MODELS（名单本身就是安全护栏）。
    model_thinking: str = "auto"

    # 送给模型的**历史字符预算**（近似上下文窗口，见 core/nodes.trim_history）。
    # 没有它，会话轮次无上限 → 几十轮后必然超窗，用户只看到"模型调用失败"。
    # 用字符而不是 token：中文场景下字符数是保守代理，且不必引入分词器依赖。
    # 24000 是给 7B 本地模型（通常 32k 上下文）留出 system + 回答余量的取值。
    # 设成 0 或负数 = 不裁剪（仅调试用）。
    context_max_chars: int = 24000

    # 单次**工具执行**的总时长上限（秒，见 core/nodes._invoke_tool）。
    # 与 model_timeout_seconds 是两件事：后者管模型调用，本项管工具整体跑多久。
    # 设成 0 或负数 = 不设上限（仅调试用）。
    tool_timeout_seconds: float = 120.0

    # v2.5 跨会话记忆（core/memory.py + core/prompts.py）总开关。True = 面板可管理记忆、
    # 记忆文本注入每轮 system prompt、memory_save 工具可用（AI 检测到用户明确说出的
    # 可复用事实时写入）；False = 上述全部关闭。默认开：记忆是本项目"跨会话"体验的
    # 一部分，关闭是显式选择（隐私 / 干净上下文）。
    memory_enabled: bool = True

    # 「提取精华」的自动兜底节奏：一个会话自上次提取起攒够多少**个用户轮次**才再提一次
    # （一次提取 = 一次真模型调用；界面上的手动按钮不受这个数限制）。
    # 设 0 = 关掉自动提取，只留手动。
    # 12 → 5（2026-09-22 实测改的）：日常闲聊一段 8 轮的对话里就有 8 条值得记的事实，
    # 而 12 轮 = 24 条消息，**实际几乎没有任何一条会话攒到过** —— 于是 `role_memory_item`
    # 长期是 0 条，她没有任何关于你的事实可用，只能凭人设编（那是"没活人感"最硬的一条成因）。
    # 代价要说清：提取跑在响应流完之后的后台线程，不占用户那一轮的延迟。
    # 它会不会与下一次对话抢那块 8GB 显存？实测过，不会：本地 8B 连跑六轮，
    # 提取在第 5 轮流结束后起，紧接着那一轮的首字 3.7s，与没有提取时的 3.5~4.4s 同档
    # （审计 §12.7）。所以这个数不必为了"怕竞争"调回 12 —— 那等于回到 0 记忆。
    memory_extract_turns: int = 5

    # 自动提取跑在**后台线程**（响应已经流完才发起），因此它可以失败而不影响对话；
    # 失败只留一条 trace。这条闸存在是因为"后台偷偷调模型"这件事本身该能一键停。
    memory_extract_auto: bool = True

    # 记忆那两步（「提取精华」+「整理记忆」）用哪个后端（后端**名**，见模型页）。
    # 空 = 提取跟随这条会话/角色的后端、整理跟随默认后端。
    # 为什么单独一个旋钮（审计 §12.5）：为了把"对话用谁"与"记忆用谁"分开 —— 档案角色留在本地
    # 陪聊，不代表它那份长期记忆也该由 8B 来判。
    # 依据要在 2026-09-24 更正两次：①"本地 8B 提 0 条"复现不了（最可能是 120 秒出厂超时撞上
    # 那次约 122 秒的提取，被当成提不出）；②把指令的捏造口子与"自动提取自我叠加"都修掉之后，
    # 同一段八轮对话两边各 9 / 10 条、都 0 捏造 0 同义 —— 这个旋钮**不是**"有没有记忆"的开关。
    # 它剩下的理由是取舍：本地一次约 122 秒、云端 10–20 秒（自动那条虽然不拖慢对话，但会长时间
    # 占着"一次只跑一个"的标记），以及「整理记忆」的"谁顶替谁"判断更吃模型强度（§12.9）。
    # 隐私口径：用户 2026-09-24 明确"健康数据也可以云端分析"，所以这个默认值的语义是
    # **"没配就是不动"** —— 填了才出网，不填行为与今天完全一致。
    memory_extract_backend: str = ""

    # 多模型比对（`compare_model_answers`，core/consensus.py）**全局总闸**。开 = 白名单里
    # 有它的角色可以一次问 N 个后端并聚合；关 = 工具一律返回关闭说明。
    # 为什么值得一个闸（而不是只靠角色白名单）：一次调用 = N 次真实模型调用 + 1 次聚合，
    # 且同一问题会被发给**多个供应商**（含云端）—— 成本与隐私都是"联网总闸"同一类。
    # 默认开：它是显式触发的工具（模型只在用户要核查时调），不是每轮都跑的隐式行为。
    consensus_enabled: bool = True

    # v2.5 Agent 模式的**全局默认**：chat = 普通对话（每轮一问一答）/
    # agent = 多步自主任务（规划 → 调工具 → 总结，步数上限放大、注入规划指令）。
    # 会话可单独覆盖（对话页切换，session_thread.agent_mode）；
    # 值为 NULL 的会话 = 跟随本项，改这里对所有"没单独设过"的会话即时生效。
    agent_default_mode: str = "chat"

    # v2.5 角色主动开口（架构计划 B）**全局总闸**：True = 允许角色主动来找用户
    # （是否真的开口还要看该角色卡 reachout_enabled）；False = 所有角色都不主动
    # （调度停、铃铛不亮）。默认开 —— 主动沟通是定位招牌；关闭是显式选择（不被打扰）。
    # 「运行环境」页可热切（runtime override），保存下一轮调度即生效。
    reachout_enabled: bool = True

    # 同一角色两次"冒话"的最小间隔（分钟）。抑制层之一：防角色刷屏。
    # 锚点是**她在那条线里最后一次说话**（主动开口与回答用户都算，09-26 修的）：原先只看
    # 主动开口那一张表，于是"她刚在对话里答完一句"不压这个钟，一分钟后又能"主动"冒一句、
    # 还因为没话可接而另起话题。
    # 「运行环境」页可热切（调度每 tick 读当前值），排查时调小它就能立刻复现一次开口。
    reachout_interval_minutes: int = 60

    # 收件箱一摞消息合并成一行覆盖多少天（1/3/7 三档；纯展示口径，落在前端）。
    # 为什么是"桶宽"而不是"合并条数"：用户抱怨的是"一天开口 5 次就是 5 行，历史糊成流水账"
    # —— 那是**时间**密度问题，按条数合并会让三天前的五条挤成一行而今天的五条占五行。
    # 后端不参与分组：`created_at` 已经在响应里，折叠只做在界面（理由见设计稿 §1）。
    reachout_merge_days: int = 1

    # 文件事件触发（架构总览 §5 第四类触发源，core/file_watch.py）：全局总闸。
    # True = 每 tick 轮询任务目录（size+mtime 基线 diff），有变化时该次开口以 file_event
    # 触发（绕过 per-role 间隔一次，静默时段/未读上限不放松），变更清单作为说话素材。
    # 默认关 —— 扫描有 IO 成本，且主动素材门控是显式选择；「运行环境」页可热切。
    file_watch_enabled: bool = False

    # 单轮用户消息允许的**图步数上限**（LangGraph `recursion_limit`，
    # 见 core/graph.build_graph_config）。
    # 一次"模型 → 工具 → 模型"消耗 2 步。不设时 LangGraph 用默认的 10007 —— 而本图是个环：
    # 模型只要持续返回 tool_calls（提示注入、工具反复报错被重试），这一轮就永远不会终止，
    # 云端后端等于数千次真实计费调用、SSE 长时间无响应。25 步 ≈ 12 轮工具调用，正常任务
    # 1-3 轮就够。设成 0 或负数 = 用 LangGraph 默认值（仅调试用）。
    agent_max_steps: int = 25

    sqlite_path: Path = Path("./data/sqlite/app.db")
    chroma_path: Path = Path("./data/chroma")
    upload_dir: Path = Path("./data/uploads")  # v1 M5 上传入口的真实落点（登记 intake 任务）

    # 里程碑 D②-4：**桌面壳安装包**的托管目录。分发形态 A 下这台机器不是公网服务器，
    # 但 B/S 用户仍然要能拿到壳 —— 所以由本应用自己的后端托管一份产物（默认不托管）。
    # None = 未配置 ⇒ `/api/shell-release` 报"没有产物"，界面连下载入口都不渲染（不留死按钮）。
    # 配置了目录但里面没有匹配的 exe，同样是 available=False：**"能下载"这句话由文件是否存在
    # 决定，而不是由配置决定**，否则部署方忘了拷产物就会得到一个点了没反应的按钮。
    shell_release_dir: Path | None = None

    # v2.4 工作区文件工具（core/tools/files.py，fs_read / fs_write / fs_list）的路径边界。
    # 角色"读写电脑文件"只允许发生在这个目录里 —— 与上传目录同一套 rigor（H1）。
    workspace_dir: Path = Path("./data/workspace")

    # v2.6 命令执行（架构计划 C·§6.2，core/tools/run.py 的 run_command）：
    # - run_tools_enabled：**总闸**。False = run_command 一律返回可读的关闭说明
    #   （注册不受影响，白名单引用的工具必须真实存在 —— 与 web_search 同一哲学）。
    # - run_approval：manual（默认）—— 模型提议的命令先进审批队列，人批准后才在后台
    #   执行一次（状态机见 core/approvals.py）；auto = 绿色通道，无审批直接执行
    #   （仅自研/可信任务目录用）。两条都可经「运行环境」页热切。
    run_tools_enabled: bool = True
    run_approval: str = "manual"

    # v2.6 MCP 扩展通道（架构计划 C·§6.1，core/tools/mcp.py）：把外部 MCP server 暴露的
    # 工具作为「域工具」(domain="mcp") 注册，复用角色白名单 / 超时 / 熔断 / 审计。权限模型
    # （白名单 / 路径边界 / 档位）仍在本家（铁律：guard 与路径守卫不委托）。
    # 值为 JSON 数组；空 = 不加载任何 MCP 工具。缺 langchain_mcp_adapters 依赖时该字段
    # 即使被设置也会被静默跳过（loader 打 warning），不阻塞启动。
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)

    # v2.4 联网工具（core/tools/web.py）：
    # - web_search_backend：auto（默认，有 TAVILY_API_KEY 走云端 tavily，否则本地 ddgs）/
    #   tavily / ddgs / off（off 时 web_search 返回可读的未配置说明，不报 500）。
    # - tavily_api_key：云端搜索 key。auto 模式下有 key 即用云端（网页内容外发到第三方，
    #   与 OCR 云端兜底同一告知义务：.env.example 里已写明）。
    # - web_search_enabled：**总闸**（用户 2026-09-17：纯白名单无总闸、纯全局太粗）。
    #   False = web_search / web_fetch 一律返回可读的关闭说明。
    # - web_allowed_domains：域名白名单（逗号分隔，子域匹配）。管的是 **web_fetch 的抓取
    #   目标**；空 = 总闸开着即不限（公网边界仍由 _host_is_public 把守），非空 = 名单外
    #   域名一律拒绝。搜索源（搜索引擎本身）不受此名单管。
    web_search_backend: str = "auto"
    web_search_enabled: bool = True
    web_allowed_domains: str = ""
    tavily_api_key: str | None = None
    # 反向图搜（image_search）用的 SauceNAO key（动漫/插画角色识别的事实标准）。None/空 =
    # 工具返回可读的"未配置"说明。与 web_search 同受 web_search_enabled 总闸管；图会外发到
    # saucenao.com（第三方），故默认不设 key、不静默上传。
    saucenao_api_key: str | None = None

    # v2.2 OCR：本地 RapidOCR 的解释器，必须是【独立 venv / 进程】的 python。OCR 那一族
    # （cv2 / omegaconf 等）绝不进主环境（现行理由见 requirements-ocr.txt 头部：不是版本冲突，
    # 而是运行树不该带它、且随包形态按设计不含 OCR）。None = 让解析器自动发现默认路径
    # （.venv-ocr/Scripts/python.exe）。
    # 「用哪个 OCR 后端」不在这里配：那是「服务」页的 OCR 端点序（运行期唯一事实面，
    # 见 rag/ocr.select_ocr_backend 与架构审计报告 P1-5）。曾有过的 ocr_backend /
    # ocr_api_key / ocr_provider / ocr_api_url 四项生产上从不被读（内置行恒在 ⇒ env 分支
    # 不可达），却挂在 .env.example 与"可改"清单里 —— 已随那次收口删除。
    ocr_python: str | None = None

    # 云端嵌入/重排的行内未填 base_url 时的兜底端点（`Settings.siliconflow_base_url`）。
    # 注意**只有端点**留在这里：凭据的家是模型页（DB 是事实面、`has_key` 只写不回读），
    # 曾经并存的 `SILICONFLOW_API_KEY` → Settings 一条路径随 P1-5 一并去掉，
    # 该 env 变量现在只被 scripts/run_api.py 用作"首启注册一个硅基流动后端行"的引导输入。
    siliconflow_base_url: str = DEFAULT_SILICONFLOW_BASE_URL

    # v2.1 检索的**绝对相似度下限**（余弦相似度，越大越相关；0 = 不过滤）。
    # 为什么默认关闭：绝对阈值必须按嵌入器标定，而实测离线默认的 HashEmbedder 下
    # 「复查频率是多少」对正确文档只有 0.163，完全无关的「编程语言排行榜」却有 0.358
    # —— 任何固定的绝对阈值都会砍掉正确结果、留下噪音（审查报告 P1-6 的实测数据）。
    # 用云端嵌入器（bge-m3，无关文本相似度约 0.3-0.5）时可以设 0.35 左右。
    # 注意：只有 cosine/ip 度量的集合能算绝对相似度；l2 需要「向量已归一化」这个前提，
    # 而云端嵌入器不保证，所以新建集合会按 cosine 建（见 rag/retriever 的度量说明）。
    rag_min_similarity: float = 0.0

    # v2.3 结构化抽取（报告文本 → 指标行）：auto = 优先 provider=ollama 的**本地**后端
    # （报告内容不出本机），否则用默认后端；也可填具体后端名强制指定。
    # 由 domains/health/extract.py 消费。
    extract_backend: str = "auto"
    # 抽取的第二遍（"AI 校对"）：auto = 有不同 provider 的第二后端就交叉验证，否则降级为
    # 同模型复查（弱校对，界面会如实标注）；off = 不做第二遍，只做确定性与原文锚定校验。
    extract_verify: str = "auto"

    obs_backend: str = "local"
    obs_emit_raw_text: bool = False
    obs_log_path: Path | None = None

    # v2.4 接入层认证（api/auth.py）：三档开关 + 两种凭证。
    # off（默认，本地零配置）/ auto（回环放行、外部要求）/ on（一律要求，部署用）。
    # 凭证为纯配置：Basic 用 "user:pass" 列表、API Key 用 key 列表，逗号分隔。
    auth_mode: str = "off"
    auth_credentials: str = ""
    auth_api_keys: str = ""
    # 可信反代的**直连来源网段**（CIDR，逗号分隔）。空 = 不信任任何代理，
    # `X-Forwarded-For` 一律忽略（见 auth.client_ip）。
    # 为什么必须有这一项：XFF 是客户端可自由设置的头。旧实现无条件采信它，导致
    # `auto` 档被 `X-Forwarded-For: 127.0.0.1` 完全绕过（代码审查报告（第二轮）H2）。
    #: 允许哪些**浏览器 origin** 跨域访问这套 API（M5 的本地/云端切换用）。
    #: 空 = 不加 CORS 中间件 = 今天的默认（同源才通）。为什么默认关：一旦放开成 `*`，
    #: 任何网页都能在你已登录的浏览器里驱动这个后端 —— 那是把桌宠变成靶子。
    #: 逗号分隔的精确 origin（例：`http://127.0.0.1:8123`）。改了要重启。
    api_allow_origins: str = ""
    auth_trusted_proxies: str = ""
    # 这台实例的主人是谁（`core/identity.resolve_instance_identity`）。空 = 本机那份
    # (`local-user`)；把它设成 `app_user` 里的另一个 id，这台实例就替那个人服务。
    # **刻意不做成运行期可改项**：按架构总览 §4.1，"换身份"是换一份完整数据集，
    # 不是热切一个 `WHERE` 过滤器 —— 半换的状态（会话是 A 的、后台调度替 B 冒话）比不换更糟。
    identity_user_id: str = ""
    # 豁免路径前缀（逗号分隔）：探活端点必须免鉴权，否则容器健康检查永远失败。
    auth_exempt_paths: str = "/api/health"

    # 来源标识护栏（`R102-45`，api/auth.py `origin_guard_violation`）：off 档的信任模型是
    # "TCP 对端是 127.0.0.1 = 本人"，但浏览器发起的 DNS rebinding 与跨源请求，对端同样是
    # 127.0.0.1 —— 实测可读全库数据并替用户批准命令（架构审计 2026-10-02 轮，主控复跑全链）。
    # 护栏在认证**之前**再验一层"来源是谁"（off 档校验 Host 回环、全档校验 Origin 同源）。
    # `=0` 是回滚开关，一键回旧行为；为什么默认开，见架构总览 §6「单机形态的可选硬化」。
    local_origin_enforce: bool = True

    # 三张只增表的 retention（`R102-29`；2026-10-02 拍板：分表定档）。0 = 该档永不清理
    # （旧行为）。清理在 bootstrap 里做、量回报给 schema-migrate 事件流（`R102-64`）；
    # agent_reachout 走 per-role `reachout_keep` 的既有机制（出厂默认改 200，只对新角色生效）。
    audit_log_retention_days: int = 90
    audit_log_max_rows: int = 10_000
    approval_done_retention_days: int = 30

    # v2.4 限流（api/ratelimit.py）：按**身份**给"贵"的写请求封顶。
    # 0 = 关（默认）：本产品主形态是"一个人、一台机器"，今天加节流只会误伤主人自己的
    # 桌宠与脚本 —— 判据与 09-27 那条决定一致（限流保护的对象随"谁付钱"变），等这台
    # 机器上真的有第二个账号再打开。
    rate_limit_per_minute: int = 0
    # 受管路径前缀（逗号分隔）。默认这两条盖住"驱动模型或子进程"的全部写请求：
    # `/api/chat`（整张图 + SSE）、`/api/session/*/upload|messages/edit|distill`（解析/OCR/
    # embedding/生成）。**只数写方法**，所以 `/api/session/{tid}/turn` 那种 0.8 秒一次的
    # 轮询读不进门。要连域数据写入一起管就把它加在这里（如 `/api/domains`）。
    rate_limit_paths: str = "/api/chat,/api/session"

    # RESERVED for the v2.4 cloud observability backend. Parsed here so the .env contract is
    # stable from day one, but nothing reads them yet - `make_tracer` only implements `local`
    # and emits a `tracer_fallback` event if you ask for anything else. Listed in
    # scripts/check_consistency.py's reserved set so the dead-config check stays honest.
    langsmith_api_key: str | None = None
    langsmith_project: str = "rolecard-agent"

    def backend(self, name: str | None = None) -> ModelBackend:
        """Resolve a backend by name, falling back to `model_default`.

        Raises KeyError with a readable message instead of returning None: a missing
        backend is a configuration error, and failing at startup beats a confusing failure
        three tool calls later.
        """
        key = name or self.model_default
        if key not in self.model_backends:
            known = ", ".join(sorted(self.model_backends))
            raise KeyError(f"unknown model backend {key!r}; configured: {known}")
        return self.model_backends[key]

    def backend_name(self, name: str | None = None) -> str:
        """这一轮**实际会服务**的那台后端名：声明的还在就用它，被删了就落回默认。

        与 `backend()` 的区别是**不抛**：用量账本要用它，而"角色引用了一台已被删掉的后端"
        在账上的正确形状是"默认那台花的"，不是"那台不存在的花的"，更不是 NULL。
        记成 NULL 会让"没设置角色级"的那些轮次在按后端分组的用量页上凭空消失 ——
        角色级选择用得越多，那个洞越像 bug。
        """
        return name if name in self.model_backends else self.model_default

    def resolve_fallbacks(self, primary: str | None = None) -> list[str]:
        """Ordered backend names to try after the primary one fails.

        候选池是**整份全局优先级**（`[model_default, *model_fallbacks]`），不是只有
        `model_fallbacks` 那一段：角色挑了优先级里靠后的一台时，操作员排在最前面的那台
        仍然得是它的备胎 —— 只从 fallbacks 里挑会让全局默认从这条降级路径上凭空消失，
        而那台恰恰是用户最信任的一份。默认自己当 primary 时两者等价（默认被"不回退到自己"
        那条规则丢掉）。

        Drops the primary (falling back to yourself is not a fallback), drops unknown names
        (a typo must not become a runtime crash mid-conversation), and caps the chain at
        `MAX_FALLBACKS`. Pure and dependency-free so it is cheap to test - the actual
        `with_fallbacks` wiring lives in `core/graph.build_model`.
        """
        head = primary or self.model_default
        seen: list[str] = []
        for name in (self.model_default, *self.model_fallbacks):
            if name != head and name in self.model_backends and name not in seen:
                seen.append(name)
        return seen[:MAX_FALLBACKS]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Parse settings from environment variables.

        A malformed `MODEL_BACKENDS` is a hard error: silently falling back to the local
        default would hide a real misconfiguration until much later.
        """
        src = os.environ if env is None else env
        data: dict[str, object] = {}

        if raw := src.get("MODEL_BACKENDS"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"MODEL_BACKENDS is not valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("MODEL_BACKENDS must be a JSON object of name -> config")
            # Merged onto the built-in `local` backend rather than replacing it: adding a
            # cloud endpoint should not silently remove the offline one, and `local` is also
            # the natural fallback target.
            merged = {"local": ModelBackend(**DEFAULT_LOCAL_BACKEND)}
            # 把 pydantic 的报错翻成人话（与下面 MCP_SERVERS 同形）：`extra="forbid"` 之后
            # 一个拼错的字段名会当场抛，裸的 ValidationError 对写配置的人没有指向性。
            try:
                for name, cfg in parsed.items():
                    if name == "local":
                        # Partial override: someone writing {"local": {"model": "..."}} means
                        # "the usual Ollama, different model" - not "and drop the base_url".
                        merged["local"] = ModelBackend(**{**DEFAULT_LOCAL_BACKEND, **cfg})
                    else:
                        merged[name] = ModelBackend(**cfg)
            except ValidationError as exc:
                raise ValueError(f"invalid MODEL_BACKENDS config: {exc}") from exc
            data["model_backends"] = merged

        if raw := src.get("MCP_SERVERS"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"MCP_SERVERS is not valid JSON: {exc}") from exc
            if not isinstance(parsed, list):
                raise ValueError("MCP_SERVERS must be a JSON array of server configs")
            try:
                data["mcp_servers"] = [McpServerConfig(**c) for c in parsed]
            except ValidationError as exc:
                raise ValueError(f"invalid MCP_SERVERS config: {exc}") from exc

        for env_key, field in (
            ("MODEL_DEFAULT", "model_default"),
            ("MODEL_TIMEOUT_SECONDS", "model_timeout_seconds"),
            ("MODEL_PIN_ON_STARTUP", "model_pin_on_startup"),
            ("WORKSPACE_DIR", "workspace_dir"),
            ("RUN_TOOLS_ENABLED", "run_tools_enabled"),
            ("RUN_APPROVAL", "run_approval"),
            ("WEB_SEARCH_BACKEND", "web_search_backend"),
            ("WEB_SEARCH_ENABLED", "web_search_enabled"),
            ("WEB_ALLOWED_DOMAINS", "web_allowed_domains"),
            ("TAVILY_API_KEY", "tavily_api_key"),
            ("SAUCENAO_API_KEY", "saucenao_api_key"),
            ("CONTEXT_MAX_CHARS", "context_max_chars"),
            ("TOOL_TIMEOUT_SECONDS", "tool_timeout_seconds"),
            ("AGENT_MAX_STEPS", "agent_max_steps"),
            ("MEMORY_ENABLED", "memory_enabled"),
            ("MEMORY_EXTRACT_TURNS", "memory_extract_turns"),
            ("MEMORY_EXTRACT_AUTO", "memory_extract_auto"),
            ("MEMORY_EXTRACT_BACKEND", "memory_extract_backend"),
            ("CONSENSUS_ENABLED", "consensus_enabled"),
            ("AGENT_DEFAULT_MODE", "agent_default_mode"),
            ("REACHOUT_ENABLED", "reachout_enabled"),
            ("REACHOUT_INTERVAL_MINUTES", "reachout_interval_minutes"),
            ("REACHOUT_MERGE_DAYS", "reachout_merge_days"),
            ("FILE_WATCH_ENABLED", "file_watch_enabled"),
            ("SQLITE_PATH", "sqlite_path"),
            ("CHROMA_PATH", "chroma_path"),
            ("UPLOAD_DIR", "upload_dir"),
            ("SHELL_RELEASE_DIR", "shell_release_dir"),
            ("OCR_PYTHON", "ocr_python"),
            ("SILICONFLOW_BASE_URL", "siliconflow_base_url"),
            ("RAG_MIN_SIMILARITY", "rag_min_similarity"),
            ("EXTRACT_BACKEND", "extract_backend"),
            ("EXTRACT_VERIFY", "extract_verify"),
            ("OBS_BACKEND", "obs_backend"),
            ("OBS_LOG_PATH", "obs_log_path"),
            ("AUTH_MODE", "auth_mode"),
            ("AUTH_CREDENTIALS", "auth_credentials"),
            ("AUTH_API_KEYS", "auth_api_keys"),
            ("AUTH_TRUSTED_PROXIES", "auth_trusted_proxies"),
            ("API_ALLOW_ORIGINS", "api_allow_origins"),
            ("AUTH_EXEMPT_PATHS", "auth_exempt_paths"),
            ("LOCAL_ORIGIN_ENFORCE", "local_origin_enforce"),
            ("RATE_LIMIT_PER_MINUTE", "rate_limit_per_minute"),
            ("RATE_LIMIT_PATHS", "rate_limit_paths"),
            ("IDENTITY_USER_ID", "identity_user_id"),
            ("LANGSMITH_API_KEY", "langsmith_api_key"),
            ("LANGSMITH_PROJECT", "langsmith_project"),
        ):
            # 空串视为"未设置"（`AUTH_CREDENTIALS=` 不该覆盖默认值），但 **`"0"` 必须保留** ——
            # 旧写法 `if value := ...` 用真值判断，`MODEL_TIMEOUT_SECONDS=0` 会被静默忽略，
            # 于是"设 0 = 不设超时"这个文档承诺的调试开关根本不可用（审查报告 L1）。
            if (value := src.get(env_key)) is not None and value != "":
                data[field] = value

        if raw := src.get("MODEL_FALLBACKS"):
            data["model_fallbacks"] = [n.strip() for n in raw.split(",") if n.strip()]

        if raw := src.get("MODEL_THINKING_MODELS"):
            data["model_thinking_models"] = [n.strip() for n in raw.split(",") if n.strip()]

        if (v := src.get("MODEL_THINKING")) is not None and v != "":
            data["model_thinking"] = v

        if raw := src.get("OBS_EMIT_RAW_TEXT"):
            data["obs_emit_raw_text"] = env_truthy(raw)

        try:
            # `data` 是按 env 契约逐项组装的普通 dict（值是 str / list[str] / dict …），
            # 每一项的正确性由 pydantic 在**这一行**校验 —— 那正是它的职责。
            # mypy 无法验证"dict[str, object] 展开后逐字段类型正确"，所以显式忽略：
            # 失败会被下面的 ValidationError 接住并翻译成可读的配置错误。
            return cls(**data)  # type: ignore[arg-type]
        except ValidationError as exc:
            raise ValueError(f"invalid configuration: {exc}") from exc
