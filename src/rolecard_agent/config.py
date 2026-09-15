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

from pydantic import BaseModel, Field, ValidationError

DEFAULT_LOCAL_BACKEND = {
    "provider": "ollama",
    # provider=ollama 走 langchain-ollama 的 Ollama 原生端点（/api/chat），base_url 不带 /v1；
    # OpenAI 兼容端点（http://localhost:11434/v1/chat/completions）是 provider=openai 时用的。
    # 带错 /v1 的症状是 Ollama 返回 "404 page not found"——端点风格由 provider 决定。
    "base_url": "http://localhost:11434",
    # qwen2.5vl:7b = 文本对话 + 结构化抽取 + 图片直读三合一：8GB 显存只能常驻一个 7B，
    # 选 vl 版（qwen2.5:7b 的超集）避免「对话模型与抽取模型互相挤出显存」的切换开销。
    "model": "qwen2.5vl:7b",
    "api_key": "ollama",
}

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


class Settings(BaseModel):
    """Resolved configuration. Build with `Settings.from_env()`; treat as immutable."""

    model_backends: dict[str, ModelBackend] = Field(
        default_factory=lambda: {"local": ModelBackend(**DEFAULT_LOCAL_BACKEND)}
    )
    model_default: str = "local"
    model_fallbacks: list[str] = Field(default_factory=list)

    sqlite_path: Path = Path("./data/sqlite/app.db")
    chroma_path: Path = Path("./data/chroma")
    upload_dir: Path = Path("./data/uploads")  # v1 M5 上传入口的真实落点（登记 intake 任务）

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
            ("EXTRACT_BACKEND", "extract_backend"),
            ("EXTRACT_VERIFY", "extract_verify"),
            ("OBS_BACKEND", "obs_backend"),
            ("OBS_LOG_PATH", "obs_log_path"),
            ("LANGSMITH_API_KEY", "langsmith_api_key"),
            ("LANGSMITH_PROJECT", "langsmith_project"),
        ):
            if value := src.get(env_key):
                data[field] = value

        if raw := src.get("MODEL_FALLBACKS"):
            data["model_fallbacks"] = [n.strip() for n in raw.split(",") if n.strip()]

        if raw := src.get("OBS_EMIT_RAW_TEXT"):
            data["obs_emit_raw_text"] = raw.strip().lower() in {"1", "true", "yes", "on"}

        try:
            return cls(**data)
        except ValidationError as exc:
            raise ValueError(f"invalid configuration: {exc}") from exc
