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

from pydantic import BaseModel, Field, ValidationError

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

# Measured advice, not a hard limit of the framework: a longer chain makes a failure harder to
# localise, and it hides "the answer got worse after degrading" from whoever reads the logs
# (实施计划.md §8.5).
MAX_FALLBACKS = 2


class ModelBackend(BaseModel):
    """One callable model endpoint.

    `provider` names the LangChain integration to use (`ollama`, `openai`, ...). It defaults
    to `ollama` so the common case needs no extra field, and it exists because the provider
    package must actually be installed - a cloud backend pointing at an OpenAI-compatible
    endpoint needs `langchain-openai`, which the local-only v1 install does not ship.
    """

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

    # 思考（reasoning）模式：这里列出的**模型名**在 ollama 风格后端上会以
    # `reasoning=True` 调用（langchain-ollama ≥1.1 把思考内容放进
    # AIMessage.additional_kwargs['reasoning_content']，由 SSE 的 thinking 事件透出）。
    # 为什么按模型名而不是全局开关：对不支持思考的模型传 reasoning=True 会直接 400
    # （实测 qwen2.5:7b），而思考 token 会显著拉长首字延迟 —— 所以只对显式列出的
    # 思考模型启用。例：MODEL_THINKING_MODELS=qwen3:8b
    model_thinking_models: list[str] = Field(default_factory=list)

    # 思考模式**总开关**（用户 2026-09-17）：auto = 按 MODEL_THINKING_MODELS 名单自动；
    # off = 名单内的模型也不开 reasoning（临时不想要思考 token / 首字延迟时用，无需
    # 改名单）。刻意没有裸 "on"：对不在名单里的模型传 reasoning=True 会直接 400，
    # 想给新模型开思考 = 把它加进 MODEL_THINKING_MODELS（名单本身就是安全护栏）。
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

    # 同一角色两次主动开口的最小间隔（分钟）。抑制层之一：防角色刷屏。
    # 低频配置走 env 即可（默认 60 分钟/角色）；改它需重启。
    reachout_interval_minutes: int = 60

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

    # v2.2 OCR 后端（可插拔，Paddle 优先 / 云端 key 兜底）：
    # - ocr_python：本地 Paddle 的解释器，必须是【独立 venv / 进程】的 python。PaddleOCR 自带
    #   numpy / OpenCV / onnxruntime，与主环境依赖摩擦（见 requirements-ocr.txt），故绝不进主
    #   环境。None = 让解析器自动发现默认路径（.venv-ocr/Scripts/python.exe）。
    # - ocr_backend：auto（默认，Paddle 优先，不可用时若有 key 回退云端）/ paddle / cloud。
    # - ocr_api_key：云端 OCR（默认 OCR.space）的 key。仅作兜底、且要求显式配置——
    #   未配则不启用，绝不悄悄把用户上传的图片发往第三方。
    # - ocr_provider / ocr_api_url：云端提供方与可选端点覆盖（默认 ocrspace 官方端点）。
    ocr_python: str | None = None
    ocr_backend: str = "auto"
    ocr_api_key: str | None = None
    ocr_provider: str = "ocrspace"
    ocr_api_url: str | None = None

    # v2.1 RAG 嵌入后端：auto（有 key 走 siliconflow bge-m3，否则 hash 离线兜底）/
    # siliconflow / chroma_default / hash。由 rag/retriever.make_embedder 消费。
    embedding_backend: str = "auto"

    # v2.1 检索重排：auto（默认开启——有 SILICONFLOW_API_KEY 走 bge-reranker 精排，否则静默
    # 等于 off，不破坏离线）/ off（强制关闭）/ siliconflow（强制开启，缺 key 启动即报错）。
    rag_rerank: str = "auto"

    # 检索的**绝对相似度下限**（余弦相似度，越大越相关；0 = 不过滤）。
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
    auth_trusted_proxies: str = ""
    # 豁免路径前缀（逗号分隔）：探活端点必须免鉴权，否则容器健康检查永远失败。
    auth_exempt_paths: str = "/api/health"

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

    def resolve_fallbacks(self, primary: str | None = None) -> list[str]:
        """Ordered backend names to try after the primary one fails.

        Drops the primary (falling back to yourself is not a fallback), drops unknown names
        (a typo must not become a runtime crash mid-conversation), and caps the chain at
        `MAX_FALLBACKS`. Pure and dependency-free so it is cheap to test - the actual
        `with_fallbacks` wiring lives in `core/graph.build_model`.
        """
        head = primary or self.model_default
        seen: list[str] = []
        for name in self.model_fallbacks:
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
            for name, cfg in parsed.items():
                if name == "local":
                    # Partial override: someone writing {"local": {"model": "..."}} means
                    # "the usual Ollama, different model" - not "and drop the base_url".
                    merged["local"] = ModelBackend(**{**DEFAULT_LOCAL_BACKEND, **cfg})
                else:
                    merged[name] = ModelBackend(**cfg)
            data["model_backends"] = merged

        for env_key, field in (
            ("MODEL_DEFAULT", "model_default"),
            ("MODEL_TIMEOUT_SECONDS", "model_timeout_seconds"),
            ("WORKSPACE_DIR", "workspace_dir"),
            ("RUN_TOOLS_ENABLED", "run_tools_enabled"),
            ("RUN_APPROVAL", "run_approval"),
            ("WEB_SEARCH_BACKEND", "web_search_backend"),
            ("WEB_SEARCH_ENABLED", "web_search_enabled"),
            ("WEB_ALLOWED_DOMAINS", "web_allowed_domains"),
            ("TAVILY_API_KEY", "tavily_api_key"),
            ("CONTEXT_MAX_CHARS", "context_max_chars"),
            ("TOOL_TIMEOUT_SECONDS", "tool_timeout_seconds"),
            ("AGENT_MAX_STEPS", "agent_max_steps"),
            ("MEMORY_ENABLED", "memory_enabled"),
            ("AGENT_DEFAULT_MODE", "agent_default_mode"),
            ("REACHOUT_ENABLED", "reachout_enabled"),
            ("REACHOUT_INTERVAL_MINUTES", "reachout_interval_minutes"),
            ("SQLITE_PATH", "sqlite_path"),
            ("CHROMA_PATH", "chroma_path"),
            ("UPLOAD_DIR", "upload_dir"),
            ("OCR_PYTHON", "ocr_python"),
            ("OCR_BACKEND", "ocr_backend"),
            ("OCR_API_KEY", "ocr_api_key"),
            ("OCR_PROVIDER", "ocr_provider"),
            ("OCR_API_URL", "ocr_api_url"),
            ("RAG_EMBEDDING", "embedding_backend"),
            ("RAG_RERANK", "rag_rerank"),
            ("RAG_MIN_SIMILARITY", "rag_min_similarity"),
            ("EXTRACT_BACKEND", "extract_backend"),
            ("EXTRACT_VERIFY", "extract_verify"),
            ("OBS_BACKEND", "obs_backend"),
            ("OBS_LOG_PATH", "obs_log_path"),
            ("AUTH_MODE", "auth_mode"),
            ("AUTH_CREDENTIALS", "auth_credentials"),
            ("AUTH_API_KEYS", "auth_api_keys"),
            ("AUTH_TRUSTED_PROXIES", "auth_trusted_proxies"),
            ("AUTH_EXEMPT_PATHS", "auth_exempt_paths"),
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
            data["obs_emit_raw_text"] = raw.strip().lower() in {"1", "true", "yes", "on"}

        try:
            # `data` 是按 env 契约逐项组装的普通 dict（值是 str / list[str] / dict …），
            # 每一项的正确性由 pydantic 在**这一行**校验 —— 那正是它的职责。
            # mypy 无法验证"dict[str, object] 展开后逐字段类型正确"，所以显式忽略：
            # 失败会被下面的 ValidationError 接住并翻译成可读的配置错误。
            return cls(**data)  # type: ignore[arg-type]
        except ValidationError as exc:
            raise ValueError(f"invalid configuration: {exc}") from exc
