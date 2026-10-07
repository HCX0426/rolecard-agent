"""v2.2 文档 / 图片解析：把上传文件变成可入检索索引的纯文本。

统一入口 `parse_document(path)`，按扩展名分派：
- `.txt` / `.md`：直接按 UTF-8 读取（v2.1 已有行为，这里收敛到同一入口）。
- `.pdf`：用 `pypdf` 抽文本。纯 Python、轻量，可与主环境共存（chromadb 已带入 numpy /
  onnxruntime，pypdf 不新增二进制依赖）。
- Office OOXML（`.docx` / `.pptx` / `.xlsx`）：本质是 ZIP + XML，用**标准库** `zipfile` +
  `xml.etree.ElementTree` 抽文本，**零新增依赖**（刻意不引 python-docx / openpyxl / lxml，
  避免再给主环境加二进制依赖 —— 与 pypdf 的取舍一致）。
- 图片（`.png/.jpg/.jpeg/.bmp/.gif/.tiff/.webp`）：走 OCR。按 `requirements-ocr.txt` 的硬规则，
  OCR 必须在【独立 venv / 进程】里跑（运行树不该带 cv2/omegaconf 那一族），因此通过
  子进程调用一个独立的 OCR Python（`OCR_PYTHON`，默认 `.venv-ocr/Scripts/python.exe`）。未配置
  或该 venv 不可用 → 抛 `OcrUnavailable`，由上传端点降级为 pending，**绝不**把 OCR 栈拖进主环境。

解析失败一律抛 `ParseError`（可读原因、不含栈），调用方据其决定 500 还是降级。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from rolecard_agent.rag.errors import OcrUnavailable, ParseError

if TYPE_CHECKING:
    # 只在类型检查时导入：ocr 反过来还要用本模块的 backend 协议，把具体后端留在类型侧，
    # 运行期只在函数里取 `LocalRapidOcrBackend`（见 `_ocr_text`）。有了它，`backend`
    # 参数才能标注成 `OcrBackend | None` 而不是 `object`（后者让 mypy 完全看不到
    # `available()` / `ocr()` 这两个方法）。
    from rolecard_agent.rag.ocr import OcrBackend

TEXT_EXTS: frozenset[str] = frozenset({".txt", ".md"})
PDF_EXTS: frozenset[str] = frozenset({".pdf"})
IMAGE_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}
)
DOCX_EXTS: frozenset[str] = frozenset({".docx"})
PPTX_EXTS: frozenset[str] = frozenset({".pptx"})
XLSX_EXTS: frozenset[str] = frozenset({".xlsx"})
OFFICE_EXTS: frozenset[str] = DOCX_EXTS | PPTX_EXTS | XLSX_EXTS
PARSEABLE_EXTENSIONS: frozenset[str] = TEXT_EXTS | PDF_EXTS | OFFICE_EXTS | IMAGE_EXTS

# OOXML 命名空间（各部件 XML 的标签都带前缀，须以完整限定名查找）。
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SS_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# 部件名 → 编号（排序用）。见 `_numbered`：字典序会把 slide10 排到 slide2 前面。
_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml")
_SHEET_RE = re.compile(r"xl/worksheets/sheet(\d+)\.xml")


def parse_document(
    path: str | Path, *, ocr_python: str | None = None, backend: OcrBackend | None = None
) -> str:
    """把文件解析为纯文本。返回空字符串表示无文本（不报错，由调用方决定如何处理）。

    相对路径按当前工作目录解析；上传端点传入的是已落盘的绝对 / 相对路径。
    `backend` 为上层按策略选好的 OCR 后端（见 rag/ocr.select_ocr_backend）；未传时图片走
    本地 RapidOCR 默认路径（仍离线优先）。

    **文件不存在时抛 `ParseError` 而不是让 `FileNotFoundError` 冒出去**：调用方（上传 /
    抽取端点）只把 `ParseError` 翻译成可读响应，其它异常会变成没有任何说明的 500。
    这条路径是真实存在的：台账记着某个文件，而它在磁盘上被人工删除或卷被重置过 ——
    上一轮把"重复上传不重复落盘"做完之后，这个组合就会命中（实测过 500）。
    """
    p = Path(path)
    if not p.is_file():
        raise ParseError(f"文件不存在或不可读：{p.name}（可能已被删除或卷未挂载）")
    suffix = p.suffix.lower()
    if suffix in TEXT_EXTS:
        return p.read_text(encoding="utf-8", errors="ignore")
    if suffix in PDF_EXTS:
        return _parse_pdf(p)
    if suffix in DOCX_EXTS:
        return _parse_docx(p)
    if suffix in PPTX_EXTS:
        return _parse_pptx(p)
    if suffix in XLSX_EXTS:
        return _parse_xlsx(p)
    if suffix in IMAGE_EXTS:
        return _parse_image(p, ocr_python=ocr_python, backend=backend)
    fallback = suffix or "(无扩展名)"
    raise ParseError(f"不支持的文件类型：{fallback}")


def _parse_pdf(p: Path) -> str:
    """用 pypdf 抽取每页文本。导入延迟到调用点——主环境未装 pypdf 时不影响模块导入。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 依赖缺失由 requirements 保证
        raise ParseError("未安装 pypdf：pip install -r requirements-rag.txt") from exc
    try:
        reader = PdfReader(str(p))
        parts: list[str] = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text)
        return "\n\n".join(parts)
    except ParseError:
        raise
    except Exception as exc:  # pypdf 可能抛各种内部错误（加密 / 损坏）
        raise ParseError(f"PDF 解析失败：{exc}") from exc


# ---------------------------------------------------------------- Office OOXML（zip + XML，零依赖）


# 单个上传文件的解压规模上限（所有条目 uncompressed 总和）。20MB 的 zip 在理论上可以
# 解出 GB 级内容（zip bomb）—— 这个校验把 DoS 挡在解析之前。
_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200


def _open_ooxml(p: Path) -> zipfile.ZipFile:
    """打开 OOXML(zip)；非 zip / 损坏 / 解压规模超限 → 可读 ParseError，而不是原始异常。

    解压规模校验（总 uncompressed 字节数 + 压缩比）是上传恶意的 zip 炸弹时唯一的防线：
    攻击者控制压缩比，所以"压缩后 ≤ 20MB"不能说明解压后的大小。
    """
    try:
        z = zipfile.ZipFile(p)
        infos = z.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise ParseError(f"不是有效的 Office 文件（{p.suffix} 损坏或非 OOXML）：{exc}") from exc
    total = sum(i.file_size for i in infos)
    packed = sum(i.compress_size for i in infos) or 1
    if total > _MAX_UNCOMPRESSED_BYTES or total / packed > _MAX_COMPRESSION_RATIO:
        z.close()
        raise ParseError(
            f"文件解压规模异常（解压后 {total // (1 << 20)}MB / 压缩比 "
            f"{total // packed}x），已拒绝处理 —— 请确认来源可信后重新导出。"
        )
    return z


def _reject_doctype(xml: bytes, part: str) -> None:
    """拒绝带 DTD / 实体声明的 XML 部件。

    ## 为什么这是完备的（而且不需要 defusedxml）

    标准库 `xml.etree.ElementTree` 基于 expat，历史上受三类攻击：billion laughs
    （实体递归展开）、quadratic blowup、外部实体（XXE）。**这三类都必须先声明
    `<!DOCTYPE` 与 `<!ENTITY`** —— 没有 DOCTYPE 就没有 DTD，没有 DTD 就没有实体可展开，
    也就没有可检索的外部资源。所以"拒绝声明"比"限制展开"更彻底：它在攻击发生**之前**
    就结束了，不依赖 expat 的版本或默认限额。

    合法 OOXML 部件永远是普通 XML 文档，**不含 DOCTYPE，也不含实体声明**（ECMA-376 定义的
    部件都是 schema-validated 的普通文档，不靠 DTD）。因此这条拒绝规则不会误伤真实文件 ——
    测试 `test_docx_with_a_dtd_is_rejected` 与既有的正常解析用例一起守住这一点。

    为什么不用 defusedxml（取舍写在这里，而不是留给后人猜）：本模块的既定选择是
    OOXML 解析**零新增依赖**（见模块 docstring —— 刻意不引 python-docx / openpyxl / lxml）。
    为一条"合法输入永不需要"的能力引入一个依赖，不如直接把该输入类别拒掉。

    ## 为什么必须扫全量而不是只看开头

    XML 允许在 DOCTYPE 之前放任意长度的注释与处理指令，所以"只扫前 N 字节"是一个
    **可绕过的窗口**：攻击者用注释把 DOCTYPE 推到扫描窗口之外即可。这里对整段字节做
    子串查找（C 层实现，O(n)，且 n 已被 zip 规模上限约束在 64MB 内），不留窗口。

    ## 为什么不做大小写归一

    XML 规范里这两个关键字**必须是大写**（`<!DOCTYPE` / `<!ENTITY`）。写成小写的形式
    不是合法的 XML 声明，expat 会直接拒 —— 也就是说"绕过大小写检查"的路根本不存在，
    而 `.upper()` 会把整段字节**复制一份**（64MB 的部件就多 64MB 峰值内存）。
    精确匹配既更省内存，也更符合规范。
    """
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ParseError(
            f"OOXML 部件 {part} 含有 DTD / 实体声明，已拒绝解析（合法 OOXML 不含这些）。"
        )


def _xml_root(xml: bytes, *, part: str = "?") -> ET.Element:
    """解析 XML 部件：先拒声明，再解析。规模上限见 `_open_ooxml`。

    `part` 两处的报错都用它 —— 从前只有 `_reject_doctype` 用了，损坏那支的消息漏了它，
    于是"是哪个文件坏了"这个唯一可操作的信息在最常见的失败里反而丢掉（补错误路径用例
    时按 docstring 的许诺断言，当场撞红）。
    """
    _reject_doctype(xml, part)
    try:
        return ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ParseError(f"OOXML 部件 {part} XML 损坏：{exc}") from exc


def _text_from_xml(xml: bytes, *, para_tag: str, text_tag: str, part: str) -> str:
    """按段落聚合：每段落内所有 <t> 顺序拼接为一行，丢弃空段落。docx 与 pptx 共用此式。

    `part` 只用于报错信息：DTD 拒绝与 XML 损坏两条消息都带上它（补错误路径用例时发现
    "损坏"那支从前漏传了 —— 于是"是哪个部件坏了"这个唯一可操作的信息，反而在最常见的
    那种失败里丢掉）。告诉操作员**是哪个部件**有问题，比一句笼统的"XML 损坏"有用得多。
    """
    lines: list[str] = []
    for para in _xml_root(xml, part=part).iter(para_tag):
        line = "".join((t.text or "") for t in para.iter(text_tag)).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _parse_docx(p: Path) -> str:
    """docx 正文：`word/document.xml` 的段落（表格单元格内也是 <w:p>，故一并覆盖）。"""
    with _open_ooxml(p) as z:
        try:
            xml = z.read("word/document.xml")
        except KeyError as exc:
            raise ParseError("不是有效的 .docx：缺少 word/document.xml") from exc
    return _text_from_xml(
        xml, para_tag=f"{_W_NS}p", text_tag=f"{_W_NS}t", part="word/document.xml"
    )


def _numbered(names: list[str], pattern: re.Pattern[str]) -> list[str]:
    """按**编号**排序匹配到的 OOXML 部件，而不是按字典序。

    `sorted()` 是字典序：`slide10.xml` 排在 `slide2.xml` **前面**（'1' < '2'）。≥10 页的
    PPT 因此会被抽出错乱的页序 —— 第 10 页的数值出现在第 1 页之后，结构化抽取拿到的
    上下文是错的，而这是**静默**的语义错误（审查报告 M9）。
    """
    def key(name: str) -> int:
        found = pattern.search(name)
        return int(found.group(1)) if found else 0

    return sorted((n for n in names if pattern.fullmatch(n)), key=key)


def _parse_pptx(p: Path) -> str:
    """pptx 各页：按 slide 序号排序，逐页抽 <a:t> 文本，页间空行分隔。"""
    with _open_ooxml(p) as z:
        slides = _numbered(z.namelist(), _SLIDE_RE)
        if not slides:
            raise ParseError("不是有效的 .pptx：没有 ppt/slides/slide*.xml")
        parts = [
            _text_from_xml(
                z.read(name), para_tag=f"{_A_NS}p", text_tag=f"{_A_NS}t", part=name
            )
            for name in slides
        ]
    return "\n\n".join(x for x in parts if x)


def _parse_xlsx(p: Path) -> str:
    """xlsx 文本：共享字符串（+ 内联字符串）。**只做文本抽取，不重建表结构行列**。"""
    with _open_ooxml(p) as z:
        names = z.namelist()
        sheets = _numbered(names, _SHEET_RE)
        if not sheets:
            raise ParseError("不是有效的 .xlsx：没有 xl/worksheets/sheet*.xml")
        lines: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared = _xml_root(z.read("xl/sharedStrings.xml"), part="xl/sharedStrings.xml")
            for si in shared.iter(f"{_SS_NS}si"):
                s = "".join((t.text or "") for t in si.iter(f"{_SS_NS}t")).strip()
                if s:
                    lines.append(s)
        for name in sheets:  # 少数写入器用 inlineStr
            sheet = _xml_root(z.read(name), part=name)
            for is_el in sheet.iter(f"{_SS_NS}is"):
                s = "".join((t.text or "") for t in is_el.iter(f"{_SS_NS}t")).strip()
                if s:
                    lines.append(s)
    return "\n".join(lines)


def _parse_image(
    p: Path, *, ocr_python: str | None = None, backend: OcrBackend | None = None
) -> str:
    """图片 OCR：优先用上层按策略选好的 `backend`（见 rag/ocr.select_ocr_backend）；
    否则按 `ocr_python` 构造本地 RapidOCR 后端，再不行自动发现默认 .venv-ocr 解释器（仍离线优先）。

    后端不可用 → 抛 `OcrUnavailable`（调用方降级为 pending）；可用但调用失败 → 抛 `ParseError`。
    后端选择逻辑在 rag/ocr.py，避免主环境直接依赖 OCR 栈。
    """
    if backend is None and ocr_python is not None:
        from rolecard_agent.rag.ocr import LocalRapidOcrBackend

        backend = LocalRapidOcrBackend(exe=ocr_python)
    if backend is None:
        from rolecard_agent.rag.ocr import LocalRapidOcrBackend

        backend = LocalRapidOcrBackend()  # 自动发现默认路径（首选）
    if not backend.available():
        raise OcrUnavailable(
            "本地 OCR 未配置：按 requirements-ocr.txt 在独立 venv 安装 rapidocr，"
            "并设置 OCR_PYTHON 指向其 python（默认 .venv-ocr/Scripts/python.exe）；"
            "或在「服务」页把一个已配凭据的云端 OCR / 视觉模型端点排进 OCR 序。"
        )
    try:
        return backend.ocr(p)
    except OcrUnavailable:
        raise
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - 后端意外异常统一成解析失败
        raise ParseError(f"OCR 失败：{exc}") from exc
