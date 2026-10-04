"""上传 → 解析 → 建索引的整条写链（Router 长成事实 service 的收口：upload_report 搬家）。

从前这段（流式落盘 + sha256 幂等 + 解析/OCR + 建索引 + intake 状态机推进 + 给模型的
说明文案）整段长在 `api/routers/sessions.py::upload_report` 里。越层的代价不是读不出来，
是**非 HTTP 宿主复用不了**：桌宠壳传张图、`scripts/` 的真机探针要走同一条链，只能再抄
一遍，而抄的那一份不跟着幂等与状态机一起改（本仓 `R28-14` 那一族的形状）。归属照旧三层：
语句在 `IngestionService` / `storage`，顺序与事实（消毒、幂等、状态推进、文案）在本模块，
HTTP 语义（400/500/201、说明往哪条会话插）留在路由。

四条不能动的判据（搬家时逐条对着原件搬，注释也搬）：

  1. **先流式落 spill 再查幂等**：只有确实是新文件才 rename 到正式名（重复上传零写入）；
     整文件不进内存（20MB 峰值消失）；
  2. **名字消毒在落盘之前**：超长名让文件系统报错，发生在写盘之后就成 500 而不是 400；
  3. **`OcrUnavailable` ≠ `ParseError`**：前者是"这台机器没配 OCR"—— 图片保持 pending，
     交一段**说明**给路由插回会话（200）；后者是真失败，`record_failure` 记账后抛
     `UploadUnreadable`，路由翻 500。两条路都必须留痕，静默降级会让"为什么没解析出来"
     只能靠猜；
  4. **每条说明都插回该会话的检查点** —— 注入留在路由（它持写锁、拿 graph，属于投送管线）。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from rolecard_agent.base.observability import TraceEvent
from rolecard_agent.core.ingestion import (
    INGESTION_FAILED,
    INGESTION_PENDING,
    IngestionService,
)
from rolecard_agent.core.uploads import parsed_text_path
from rolecard_agent.rag.parser import (
    IMAGE_EXTS,
    PARSEABLE_EXTENSIONS,
    OcrUnavailable,
    ParseError,
    parse_document,
)
from rolecard_agent.rag.retriever import EmbedError, KnowledgeDimensionMismatch

if TYPE_CHECKING:
    from rolecard_agent.rag.retriever import KnowledgeBase

#: 单次上传的字节上限（与 OCR / 抽取的输入闸同量级：base64 再放大 33% 也吃得下）。
UPLOAD_MAX_BYTES = 20 * 1024 * 1024

#: 文件名消毒（审查报告 A4）：模型/浏览器给的 `filename` 不可信。截断 + 去掉控制字符/
#: 分隔符 —— 超长名或含 `NUL` 的名字会让 `write_bytes` 抛 OSError，用户拿到的是一个
#: 没有任何说明的 500。
_FILENAME_MAX = 120
_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f<>:\"|?*\\/]")


class UploadRejected(Exception):
    """这一份上传没资格进流水线（超限 / 空文件）—— 路由翻 400，什么都没登记。"""


class UploadUnreadable(Exception):
    """登记成功但读不出来（解析硬失败 / 索引失败）—— 路由翻 500，台账已 record_failure。"""


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    """一次上传的结果：响应体的四个键 + 要插回会话的那句说明。

    `parsed` 只在 OCR 不可用那条早退路上取值（`False`），其余路径不带这个键 ——
    与搬家前的响应体逐键一致，前端一个字不用改。
    """

    task_id: str
    reused: bool
    file: str
    status: str
    note: str
    parsed: bool | None = None

    def response(self) -> dict[str, object]:
        out: dict[str, object] = {
            "task_id": self.task_id,
            "reused": self.reused,
            "file": self.file,
            "status": self.status,
        }
        if self.parsed is not None:
            out["parsed"] = self.parsed
        return out


def sanitize_filename(name: str) -> str:
    """把模型/浏览器给的名字收成**可以安全落盘**的一段：去路径成分、去脏字符、截断。

    截断时**保住扩展名**（解析分派完全依赖后缀，丢后缀等于把文件变成"不支持的类型"）——
    这半句是这条函数最容易被"简化"掉的一半，测试钉着它。
    """
    cleaned = _UNSAFE_NAME_CHARS.sub("_", name).strip(" .")
    if not cleaned:
        cleaned = "report.bin"
    if len(cleaned) <= _FILENAME_MAX:
        return cleaned
    suffix = Path(cleaned).suffix[:16]
    return cleaned[: _FILENAME_MAX - len(suffix)] + suffix


def source_kind(path: Path) -> str:
    """这个文件的来源标签：图片 = `"ocr"`（要走 OCR 才有文本），否则 `"parsed"`。"""
    return "ocr" if path.suffix.lower() in IMAGE_EXTS else "parsed"


def read_source_text(path: Path, *, backend: Any = None) -> str | None:
    """现场（重新）解析一份已落盘的文件；读不出来返回 `None`。

    给"抽取时发现没有 .parsed.txt 副本"那条兜底路用（上传于旧版本的文件）：
    OCR 没配 / 解析失败都算"没有文本"，调用方按 `no_text` 跳过 —— 那里不需要区分
    是哪一种失败（跳过与 500 的分界由调用方自己定）。
    """
    try:
        return parse_document(path, backend=backend)
    except (ParseError, OcrUnavailable):
        return None


def ingest_upload(
    *,
    reader: IO[bytes],
    filename: str,
    thread_id: str,
    user_id: str,
    upload_dir: Path,
    ingestion: IngestionService,
    knowledge: KnowledgeBase,
    knowledge_scope: str,
    ocr_candidates: Callable[[], Any] | None,
    tracer: Any,
    max_bytes: int = UPLOAD_MAX_BYTES,
) -> UploadOutcome:
    """存文件 + 登记 intake 任务（幂等键 sha256），并把**能读的**部分一路推到 indexed。

    `reader` 是原样的字节流（FastAPI 的 `UploadFile.file`）：本模块不认识 UploadFile，
    非 HTTP 宿主给一个 `open(...)` 的句柄一样跑。
    `ocr_candidates` 是**延迟求值**的 OCR 后端工厂（只有图片才问，且答案可能每次不同）。
    `thread_id` 只服务一处：解析副本写失败那条 tracer 事件要带"这是谁的会话"。
    """
    upload_dir.mkdir(parents=True, exist_ok=True)
    # 去掉任何路径成分再消毒（审查报告 A4）。
    safe_name = sanitize_filename(Path(filename or "report.bin").name)

    # M5：流式读 + 增量哈希 + 增量落盘到临时 spill，**不再把整文件读进内存**（20MB 峰值
    # 消失）。先落 spill 是为了拿到 sha256 去做幂等查重；最终按幂等结果 rename 到正式路径
    # 或丢弃，避免重复落盘（正常重复上传零写入）。
    spill = upload_dir / f".{uuid.uuid4().hex[:12]}.part"
    hasher = hashlib.sha256()
    size = 0
    try:
        with spill.open("wb") as out:
            while True:
                chunk = reader.read(1 << 20)  # 1 MB 一块
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise UploadRejected(f"文件超过 {max_bytes // (1024 * 1024)}MB 上限。")
                hasher.update(chunk)
                out.write(chunk)
    except Exception:
        spill.unlink(missing_ok=True)
        raise
    if size == 0:
        spill.unlink(missing_ok=True)
        raise UploadRejected("空文件。")
    file_hash = hasher.hexdigest()

    # 幂等：同一份字节只登记一次、只落盘一次。
    prior = ingestion.find_by_hash(user_id, file_hash)
    if prior is not None:
        task_id = str(prior["task_id"])
        reused = True
        existing = prior
        target = Path(str(prior["source_file"] or ""))
        if not target.is_file():
            # 台账在、文件没了（人工删除 / 数据卷重置）。**自愈**：把这次上传的字节写下来，
            # 并把台账指过去 —— 否则"重复上传同一个文件"会一路走到解析失败（实测 500）。
            # 注意这不是"重复落盘"：只有在原文件确实缺失时才写，正常重复上传仍然零写入。
            target = upload_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
            spill.replace(target)  # 原子改名到正式路径
            ingestion.relink_source(task_id, str(target))
        else:
            spill.unlink(missing_ok=True)  # 已有文件：丢弃 spill（零写入）
        if existing["status"] == INGESTION_FAILED:
            # **failed 必须是一条可走出的路**：重传同一份文件 = 用户在重试。状态机唯一允许的
            # 回边是 failed → pending，而此前没有任何代码执行它 —— 于是任务永远停在 failed，
            # 哪怕这次已经重新解析并入库成功，台账还在说"失败"（与事实背离）。
            # 显式重启后走下面的正常推进链。
            ingestion.advance(task_id, INGESTION_PENDING)
            existing["status"] = INGESTION_PENDING
    else:
        target = upload_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        spill.replace(target)
        task_id = ingestion.create(user_id=user_id, source_file=str(target), file_hash=file_hash)
        existing = ingestion.get(task_id)
        if str(existing["source_file"] or "") != str(target):
            # 输掉了并发竞态：另一路上传先立了台账，create 的约束回读返回的是**它的**行 ——
            # 自己刚落盘的那份就是孤儿文件，丢弃，统一用赢家的那份。没这一步，并发双击/
            # 前端重试会各留一份文件。
            target.unlink(missing_ok=True)
            target = Path(str(existing["source_file"]))
            reused = True
        else:
            reused = False

    # v2.2：统一解析入口——.txt/.md/.pdf 直接抽文本入检索索引；图片走 OCR 子进程
    # （独立 venv，见 requirements-ocr.txt）；其余类型保持 pending。
    suffix = target.suffix.lower()
    if suffix in PARSEABLE_EXTENSIONS:
        try:
            # 仅图片需要选 OCR 后端：按「服务」页签的端点顺序（默认本地 RapidOCR 优先）。
            backend = (
                ocr_candidates()
                if suffix in IMAGE_EXTS and ocr_candidates is not None
                else None
            )
            text = parse_document(target, backend=backend)
        except OcrUnavailable:
            # 后端未配置：图片保持 pending，明确告知模型不可读（不把 OCR 栈拖进主环境）。
            return UploadOutcome(
                task_id=task_id,
                reused=reused,
                file=safe_name,
                status=str(existing["status"]),
                note=(
                    f"[用户上传了图片报告：{safe_name}，已登记 intake 任务 {task_id}"
                    f"（status={existing['status']}）。本地 OCR 未配置"
                    "（本地 RapidOCR 不可用，且「服务」页没有就绪的云端 OCR / 视觉模型端点），"
                    "当前不能读取图片内容，不要假装已经读过。]"
                ),
                parsed=False,
            )
        except ParseError as exc:
            # 解析硬失败 → 任务标记 failed（否则永远停在 pending），并返回可读的 500。
            ingestion.record_failure(task_id, str(exc))
            raise UploadUnreadable(str(exc)) from exc

        if text.strip():
            # 存一份解析文本：结构化抽取复用它，避免对同一张图片再跑一次 OCR（OCR 很贵）。
            try:
                parsed_text_path(target).write_text(text, encoding="utf-8")
            except OSError as exc:
                # 不再静默吞掉（审查报告 E2）：有现场重解析兜底，但"为什么抽取又跑了 OCR"
                # 必须能在轨迹里查到原因。
                tracer.emit(
                    TraceEvent(
                        event="parsed_text_write_failed",
                        thread_id=thread_id,
                        error=f"{type(exc).__name__}: {exc}",
                        detail={"target": target.name},
                    )
                )
            try:
                # 索引身份用 task_id（按内容 hash 去重 → 同一份字节重建、不同内容彼此
                # 独立），**不能用 safe_name**：同名文件会互相覆盖，旧文档索引静默丢失
                # （审查报告 P0）。文件名只作展示名，引用里显示的仍是它。
                chunks = knowledge.index(
                    knowledge_scope, task_id, text, source_name=safe_name
                )
            except (KnowledgeDimensionMismatch, EmbedError) as exc:
                # 两者都是"管理员可修复"的状态，且都发生在**索引没被破坏**之后
                # （index() 已改为先嵌入再写库）。给出可操作的原因。
                ingestion.record_failure(task_id, str(exc))
                raise UploadUnreadable(str(exc)) from exc
            chain: tuple[str, ...] = ("parsed", "extracted", "indexed")
            note = (
                f"[用户上传了文档：{safe_name}（{chunks} 段），已建立检索索引"
                f"（任务 {task_id}，status=indexed）。注意：能否检索到取决于当前角色的"
                "knowledge_scopes 授权；未授权时请提示用户切换角色，不要假装已经读过。]"
            )
        else:
            # 解析出空文本（扫描件 / 无文本层的 PDF）：解析到 parsed 即止，不入索引。
            chain = ("parsed",)
            note = (
                f"[用户上传了文件：{safe_name}，已解析但未提取到文本（可能为扫描件）。"
                f"已登记任务 {task_id}（status={existing['status']}），暂不入检索。]"
            )
        if (existing["status"] or "pending") == "pending":
            # 幂等：重复上传同一文件会复用已 indexed 的任务，不能再推进状态机。
            for next_status in chain:
                ingestion.advance(task_id, next_status)
            existing = ingestion.get(task_id)
    else:
        note = (
            f"[用户上传了报告文件：{safe_name}，已登记 intake 任务 {task_id}"
            f"（status={existing['status']}）。文件类型暂不支持自动解析（v2.2 支持 "
            ".txt/.md/.pdf/.docx/.pptx/.xlsx 及图片 OCR），当前不能读取其中内容，"
            "不要假装已经读过。]"
        )
    return UploadOutcome(
        task_id=task_id,
        reused=reused,
        file=safe_name,
        status=str(existing["status"]),
        note=note,
    )
