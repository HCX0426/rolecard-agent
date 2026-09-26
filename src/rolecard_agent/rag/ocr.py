"""v2.2 可插拔 OCR 后端：本地 Paddle（首选）+ 云端 API key（兜底）。

设计（面试可讲，对应"离线优先 + 云端兜底 + 隐私分级"）：

- OCR 是重依赖，且涉及"图片是否离开本机"的隐私问题，所以做成后端可插拔：
  - `LocalPaddleBackend`：子进程调用**独立 venv** 里的 PaddleOCR（requirements-ocr.txt 安装
    约定）。离线、数据不出本机；numpy/OpenCV/onnxruntime 的摩擦被隔离在独立进程之外。
  - `CloudApiBackend`：走 HTTP 的 OCR API（默认 OCR.space，仅需 api_key，无 SDK）。数据会
    发往第三方——因此只作兜底，且 `available()` 严格要求显式配了 key，绝不悄悄外发。
- 选择器 `select_ocr_backend`：按「服务」页签的 OCR 端点序逐个试可用（Paddle / 本地视觉
  模型 / 云端），不可用就顺延；都没有 → 返回 None，调用方降级为 `OcrUnavailable`，而不是把重型
  依赖拖进主进程或偷偷把图片发到外网。

错误语义（与解析层一致）：后端"不可用"抛 `OcrUnavailable`（调用方降级为 pending）；"可用但
调用失败"抛 `ParseError`（调用方据上下文决定 500 还是提示）。
"""

from __future__ import annotations

import base64
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from rolecard_agent.config import Settings
from rolecard_agent.core.paths import default_ocr_python
from rolecard_agent.rag.parser import OcrUnavailable, ParseError

# ocr.py 位于 <root>/src/rolecard_agent/rag/，故项目根为 parents[3]；worker 在 <root>/scripts。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_OCR_WORKER = _PROJECT_ROOT / "scripts" / "ocr_worker.py"

# M3：整图 base64 内联进请求体前先卡大小，避免超大扫描件爆内存/超上下文窗口。
MAX_OCR_IMAGE_BYTES = 15 * 1024 * 1024  # 15 MB

# L8：Paddle worker 的子进程超时（秒）——OCR 是重活，但也不允许无限挂起。
_OCR_PROC_TIMEOUT_SECONDS = 120

# L10：进程级共享 httpx 连接池 —— 此前每次调用都新建 Client，握手/TLS 成本白扔。
# httpx.Client 并发请求安全；单请求 timeout 参数覆盖池默认值。
_HTTP_CLIENT: httpx.Client | None = None


def _http() -> httpx.Client:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.Client(timeout=30.0)
    return _HTTP_CLIENT


def _read_image_b64(path: Path) -> str:
    """读取图片并 base64 编码；过大直接抛可读 `ParseError`（不静默吞掉）。"""
    size = Path(path).stat().st_size
    if size > MAX_OCR_IMAGE_BYTES:
        raise ParseError(
            f"图片过大（{size // 1024 // 1024} MB），超过 OCR 上限 "
            f"{MAX_OCR_IMAGE_BYTES // 1024 // 1024} MB，请压缩或裁剪后重试。"
        )
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


class OcrBackend(Protocol):
    """一个 OCR 后端：能回答"是否就绪"并产出图片文本。"""

    name: str

    def available(self) -> bool:
        """本后端是否已配置就绪（可执行 / 已配 key）。未就绪不应调用 `ocr`。"""
        ...

    def ocr(self, image_path: Path) -> str:
        """把图片解析为纯文本；不可用或失败抛 `OcrUnavailable` / `ParseError`。"""
        ...


class LocalPaddleBackend:
    """子进程调用独立 venv 里的 PaddleOCR worker（见 scripts/ocr_worker.py）。

    绝不进主环境：OCR 栈的 numpy/OpenCV/onnxruntime 与主环境 chromadb 的 numpy 冲突，
    因此只在 `OCR_PYTHON`（默认 .venv-ocr/Scripts/python.exe）指向的独立进程里跑。
    """

    name = "paddle"

    def __init__(self, *, exe: str | None = None) -> None:
        # 只认调用方传进来的 `exe`（生产路径由 `select_ocr_backend` 交 `settings.ocr_python`），
        # 不再自己偷偷读 OCR_PYTHON：那会让"运行环境页/覆盖层改过的值"与"这里实际用的解释器"
        # 是两个事实面（架构审计报告 P1-4）。None = 自动发现默认 .venv-ocr。
        self._exe = exe or default_ocr_python()

    def available(self) -> bool:
        if not self._exe or not Path(self._exe).exists():
            return False
        return _OCR_WORKER.exists()

    def ocr(self, image_path: Path) -> str:
        # 把解释器路径绑成局部 str：`available()` 已经保证它存在，但 `self._exe` 的静态
        # 类型仍是 `str | None`，直接放进 argv 会过不了类型检查（而且这里确实需要一个非空值）。
        exe = self._exe
        if not exe or not self.available():
            raise OcrUnavailable(
                "OCR 后端未配置：按 requirements-ocr.txt 在独立 venv 安装 paddleocr，"
                "并设置 OCR_PYTHON 指向其 python（默认 .venv-ocr/Scripts/python.exe）。"
            )
        try:
            proc = subprocess.run(
                [exe, str(_OCR_WORKER), str(image_path)],
                capture_output=True,
                # 显式 UTF-8：worker 已 reconfigure 为 UTF-8；不能用 text=True（那样按 locale
                # 解码，中文 Windows = GBK，中文 OCR 文本会乱码）。
                encoding="utf-8",
                errors="replace",
                timeout=_OCR_PROC_TIMEOUT_SECONDS,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - 启动失败 = 解析失败，由调用方决定降级
            raise ParseError(f"OCR 子进程启动失败：{exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
            raise ParseError(f"OCR 失败（退出码 {proc.returncode}）：{detail}")
        return (proc.stdout or "").strip()


class VisionModelBackend:
    """本地视觉模型直读图片（usage=ocr 的模型后端行 → Ollama /api/chat + images）。

    为什么存在：「服务」页允许把模型后端引用进 OCR 优先级（EndpointConfig.kind=local
    且带 model）——界面允许了，选择器就必须消费，否则"显示可用实则被跳过"是在骗人。
    与 CloudApiBackend 同层：本地视觉模型不外发数据，天然排在云 OCR 之前合规。

    语义对齐协议：不可达/模型缺失 → `OcrUnavailable`（顺延下一个候选）；
    可达但识别失败 → `ParseError`（调用方决定 500 还是提示）。
    """

    name = "vl"

    def __init__(self, *, base_url: str | None, model: str, timeout: float = 90.0) -> None:
        self._base = (base_url or "http://127.0.0.1:11434").rstrip("/")
        self._model = model
        self._timeout = timeout

    def available(self) -> bool:
        # 判定原语在 core/probes.py（与服务页探测共用一份，防止两处答案打架）。
        from rolecard_agent.core.probes import vision_model_ready

        return vision_model_ready(self._base, self._model)

    def ocr(self, image_path: Path) -> str:
        b64 = _read_image_b64(image_path)
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": "请把图片中的全部文字按原样转录出来；若图中没有文字，"
                    "就用一句中文客观描述图片内容。",
                    "images": [b64],
                }
            ],
            "stream": False,
        }
        try:
            resp = _http().post(f"{self._base}/api/chat", json=payload, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailable(f"本地视觉模型不可达：{exc}") from exc
        if resp.status_code != 200:
            raise ParseError(f"视觉模型返回 HTTP {resp.status_code}")
        text = str((resp.json().get("message") or {}).get("content") or "").strip()
        if not text:
            raise ParseError("视觉模型没有返回任何内容。")
        return text


class CloudApiBackend:
    """云端 OCR（默认 OCR.space）：仅需 api_key，无 SDK。仅在服务页把它排进 OCR 序时才会被用。

    隐私红线：图片会发往第三方。所以 `available()` 严格要求该端点行引用的后端**显式配了
    key**（模型页填写）；未配则不启用，绝不悄悄把用户上传的病历图片发出去。
    调用失败抛 `ParseError`（不静默返回空）。
    """

    name = "cloud"

    def __init__(self, *, api_key: str | None = None, api_url: str | None = None) -> None:
        # `provider` 参数已删：它被存进 `self._provider` 然后**从没被读过**（§3 的死开关），
        # 而端点行本来就自带 base_url —— 提供方由那一行的 base_url 表达，不需要第二个名字。
        self._key = api_key
        self._url = api_url or "https://api.ocr.space/parse/image"

    def available(self) -> bool:
        return bool(self._key)

    def ocr(self, image_path: Path) -> str:
        if not self._key:
            raise OcrUnavailable("云端 OCR 未配置 API Key（去模型页为该后端填写凭据）。")
        try:
            b64 = _read_image_b64(image_path)
        except Exception as exc:
            raise ParseError(f"读取图片失败：{exc}") from exc
        try:
            res = _http().post(
                self._url,
                data={
                    "apikey": self._key,
                    "base64image": f"data:image/png;base64,{b64}",
                    "language": "chs",
                    "isOverlayRequired": "false",
                    "scale": "true",
                },
                timeout=30.0,
            )
            res.raise_for_status()
            payload = res.json()
        except Exception as exc:  # noqa: BLE001 - 网络/HTTP 失败 = 解析失败
            raise ParseError(f"云端 OCR 请求失败：{exc}") from exc
        # OCR.space 响应：ParsedResults[].ParsedText
        results = payload.get("ParsedResults") or []
        text = "\n".join((r.get("ParsedText") or "").strip() for r in results).strip()
        if not text:
            # 可能配额耗尽 / 参数错误：把 ErrorMessage 透出成可读失败，而不是静默返回空。
            err = payload.get("ErrorMessage") or payload.get("ErrorDetails")
            if err:
                raise ParseError(f"云端 OCR 返回错误：{err}")
        return text


def select_ocr_backend(
    settings: Settings,
    *,
    order: Sequence[str],
    endpoints: Any,
) -> OcrBackend | None:
    """按「服务」页签的 OCR 端点序选一个就绪后端 —— **运行期唯一事实面**（架构审计报告 P1-5）。

    `paddle` 探本地解释器（`Settings.ocr_python`）；带模型的本地引用行 = 本地视觉模型，
    探 Ollama 与模型在位；云端行按**行内** key/api_url 实例化（可并存多个云端 OCR 账号，
    谁排前面谁先被用）。不可用的候选顺延下一个；全部不可用返回 None，调用方应降级为
    `OcrUnavailable`（保持 pending，不假装已读）。

    为什么不再有"按 `Settings.ocr_backend` 走 paddle/cloud 档位"的第二条路径：`seed_once()`
    恒播种一条启用的内置行 ⇒ **走装配根的进程**里 `order` 永不为空 ⇒ 那条分支**从不执行**。
    曾经与它一起挂在 .env.example 与运行环境页"可改"清单上的
    `OCR_BACKEND`/`OCR_API_KEY`/`OCR_API_URL` 已随该收口删除（架构审计报告 P1-5）——
    保存它们当时什么都不发生。

    凭据因此只有一个家：模型页的云端后端行（`has_key` 掩码纪律在那边）。
    云端 OCR 会把图片外发第三方 —— 它**只能**由操作员显式在模型页建凭据行、再在服务页引用，
    绝不从 env 默认启用（隐私红线，与 `seed_once` 的"OCR 默认无云端引用"同一条理由）。

    ⚠️ 与 `make_embedder` 同一条限定：`storage.db.bootstrap()` 不播这些行，装配根之外要自己
    `seed_once()`。而这里比嵌入更阴 —— 空 `order` 不抛，返回 None 降级成"OCR 不可用"，
    报告静静留在 pending，看起来像"这张图没识别出来"。
    """
    for cid in order:
        if cid == "paddle":
            paddle = LocalPaddleBackend(exe=settings.ocr_python)
            if paddle.available():
                return paddle
            continue
        cfg = endpoints.get(cid)
        if cfg is None or getattr(cfg, "stale", False):
            continue
        # 视觉模型行（usage=ocr 的后端引用）：本地直读，不外发数据。
        # available() 探 Ollama 与模型在位；不可用顺延下一候选（降级语义与视图一致）。
        if getattr(cfg, "kind", "") == "local" and cfg.model:
            vl = VisionModelBackend(base_url=cfg.base_url, model=cfg.model)
            if vl.available():
                return vl
            continue
        if cfg.api_key:
            return CloudApiBackend(api_key=cfg.api_key, api_url=cfg.base_url)
    return None
