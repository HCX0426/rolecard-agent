"""rag/parser 单测：扩展名分派、PDF 抽文本、图片 OCR 后端选择与降级、不支持类型报错。

OCR 子进程路径（rapidocr）依赖独立 venv，本环境可能未装，故这里只测**合约**：
- 后端不可用（无独立 venv / 未配 key）→ OcrUnavailable 降级；
- 选择器策略：RapidOCR 优先，云端 key 兜底；
真实 OCR 在 .venv-ocr 就绪后由集成验证（scripts/smoke_check.py / 手工上传图片）。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from rolecard_agent.config import Settings
from rolecard_agent.core.services import EndpointConfig
from rolecard_agent.rag.ocr import (
    CloudApiBackend,
    LocalRapidOcrBackend,
    default_ocr_python,
    select_ocr_backend,
)
from rolecard_agent.rag.parser import (
    PARSEABLE_EXTENSIONS,
    OcrUnavailable,
    ParseError,
    parse_document,
)


def _make_pdf_bytes(text: str) -> bytes:
    """最小合法 1 页 PDF（含文本）。"""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objs) + 1
    out += f"xref\n0 {n}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode()
    return bytes(out)


def _zip_bytes(files: dict[str, str]) -> bytes:
    """把 {part 路径: XML 文本} 打包成一个 zip（OOXML 的最小载体）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>' for t in paragraphs
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    return _zip_bytes({"word/document.xml": doc})


def _make_pptx_bytes(slides: list[list[str]]) -> bytes:
    files: dict[str, str] = {}
    for i, texts in enumerate(slides, start=1):
        paras = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in texts)
        files[f"ppt/slides/slide{i}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            f"<p:cSld><p:spTree><p:sp><p:txBody>{paras}</p:txBody></p:sp></p:spTree></p:cSld>"
            "</p:sld>"
        )
    return _zip_bytes(files)


def _make_xlsx_bytes(strings: list[str]) -> bytes:
    sis = "".join(f"<si><t>{s}</t></si>" for s in strings)
    shared = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{sis}</sst>"
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData/></worksheet>"
    )
    return _zip_bytes({"xl/sharedStrings.xml": shared, "xl/worksheets/sheet1.xml": sheet})


def test_text_extensions_read_directly(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello 世界", encoding="utf-8")
    (tmp_path / "b.md").write_text("# title", encoding="utf-8")
    assert parse_document(tmp_path / "a.txt") == "hello 世界"
    assert parse_document(tmp_path / "b.md") == "# title"


def test_pdf_extraction(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(_make_pdf_bytes("Hello PDF World 123"))
    out = parse_document(f)
    assert "Hello PDF World 123" in out


def test_pdf_empty_text_is_empty_string(tmp_path: Path) -> None:
    # 扫描件 / 无文本层：解析不报错，返回空串（调用方据其决定不索引）。
    f = tmp_path / "scan.pdf"
    f.write_bytes(b"%PDF-1.4\n%%EOF")  # 极简、无文本对象
    # pypdf 对残缺 PDF 可能抛错；允许 ParseError 或返回空串，关键是"不崩成 500 的意外异常"
    try:
        text = parse_document(f)
        assert text == ""
    except ParseError:
        pass


def test_unsupported_extension_raises_parse_error(tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"data")
    with pytest.raises(ParseError):
        parse_document(f)


def test_image_without_configured_backend_raises_ocr_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置任何 OCR 后端（无独立 venv / 无 OCR_PYTHON / 无云端 key）→ OcrUnavailable 降级。

    强制"自动发现不到默认解释器"，使断言不依赖本机是否装了 .venv-ocr。
    """
    monkeypatch.delenv("OCR_PYTHON", raising=False)
    # 注意：ocr.py 现以 `from rolecard_agent.core.paths import default_ocr_python` 绑定，
    # 必须 patch ocr 模块里的引用，patch parser 里的原函数不会生效。
    monkeypatch.setattr("rolecard_agent.rag.ocr.default_ocr_python", lambda: None)
    img = tmp_path / "scan.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")  # 假 PNG 头，仅用于触发扩展名分派
    with pytest.raises(OcrUnavailable):
        parse_document(img)


def test_image_with_missing_ocr_exe_raises_ocr_unavailable(tmp_path: Path) -> None:
    img = tmp_path / "scan.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(OcrUnavailable):
        parse_document(img, ocr_python="C:/no/such/python.exe")


def test_parseable_extensions_constant() -> None:
    assert ".txt" in PARSEABLE_EXTENSIONS
    assert ".md" in PARSEABLE_EXTENSIONS
    assert ".pdf" in PARSEABLE_EXTENSIONS
    assert ".png" in PARSEABLE_EXTENSIONS
    assert {".docx", ".pptx", ".xlsx"} <= PARSEABLE_EXTENSIONS
    assert ".bin" not in PARSEABLE_EXTENSIONS


# -- OCR 后端选择策略：RapidOCR 优先，云端 key 兜底 ----------------------------------------


def test_rapidocr_interpreter_comes_from_settings_not_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-4：OCR 解释器只认 `Settings.ocr_python`，构造函数不再自己 `os.environ.get`。

    两个事实面的症状是这样的：运行环境页/覆盖层改了 OCR_PYTHON，实际执行的却还是 .env 里
    那把 —— 因为 `LocalRapidOcrBackend.__init__` 在 Settings 之外又读了一遍 env，而那条路径
    既不受覆盖层管，也不在配置契约里。
    """
    monkeypatch.setenv("OCR_PYTHON", "C:/from/env/python.exe")
    monkeypatch.setattr(LocalRapidOcrBackend, "available", lambda self: True)
    chosen = select_ocr_backend(
        Settings(ocr_python="C:/from/settings/python.exe"), **_ocr_strategy(_rapidocr_row())
    )
    assert isinstance(chosen, LocalRapidOcrBackend)
    assert chosen._exe == "C:/from/settings/python.exe"  # noqa: SLF001
    # 未显式给出解释器（None）= 自动发现默认路径，同样**不是**去读 env。
    assert LocalRapidOcrBackend()._exe == default_ocr_python()  # noqa: SLF001


def _rapidocr_row() -> EndpointConfig:
    return EndpointConfig(
        id="rapidocr",
        label="rapidocr",
        kind="local",
        base_url=None,
        api_key=None,
        model=None,
        enabled=True,
        builtin=True,
    )


def _cloud_row(eid: str = "ocrspace", *, api_key: str | None = "sk-x") -> EndpointConfig:
    return EndpointConfig(
        id=eid,
        label=eid,
        kind="cloud",
        base_url="https://ocr.example/parse",
        api_key=api_key,
        model=None,
        enabled=True,
        builtin=False,
    )


def _ocr_strategy(*rows: EndpointConfig) -> dict[str, Any]:
    """工厂参数（「服务」页的 OCR 端点序 + 行配置）—— P1-5 后它是唯一入口。"""
    return {
        "order": [r.id for r in rows],
        "endpoints": {r.id: r for r in rows},
    }


def test_select_ocr_backend_follows_the_service_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """两者都可用时，**排在前面**的那条行被选中（出厂播种是 rapidocr 在前 = 离线优先）。"""
    monkeypatch.setattr(LocalRapidOcrBackend, "available", lambda self: True)
    monkeypatch.setattr(CloudApiBackend, "available", lambda self: True)
    backend = select_ocr_backend(Settings(), **_ocr_strategy(_rapidocr_row(), _cloud_row()))
    assert isinstance(backend, LocalRapidOcrBackend)

    flipped = select_ocr_backend(Settings(), **_ocr_strategy(_cloud_row(), _rapidocr_row()))
    assert isinstance(flipped, CloudApiBackend)
    # 云端行的连接配置来自**行**（模型页的引用后端），不再来自 OCR_API_KEY 那把 env key。
    assert flipped._url == "https://ocr.example/parse"  # noqa: SLF001


def test_select_ocr_backend_skips_unready_row_to_next_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RapidOCR 未装独立 venv → 顺延到下一条就绪的行（启停与优先级都在服务页）。"""
    monkeypatch.setattr(LocalRapidOcrBackend, "available", lambda self: False)
    monkeypatch.setattr(CloudApiBackend, "available", lambda self: True)
    backend = select_ocr_backend(Settings(), **_ocr_strategy(_rapidocr_row(), _cloud_row()))
    assert isinstance(backend, CloudApiBackend)


def test_select_ocr_backend_none_when_nothing_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """所有候选都不就绪 → None（调用方降级为 OcrUnavailable，不假装已读）。"""
    monkeypatch.setattr(LocalRapidOcrBackend, "available", lambda self: False)
    assert select_ocr_backend(Settings(), **_ocr_strategy(_rapidocr_row())) is None


def test_select_ocr_backend_never_sends_images_to_a_keyless_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """隐私红线的机器形态：云端行没配 key 就是不可用，绝不外发图片。

    取代旧的"显式 ocr_backend=paddle 时不回退云端" —— 现在这条不靠档位，而是靠
    「服务」页要不要把云端行排进序 + 模型页有没有填 key（两个动作都是显式的）。
    """
    monkeypatch.setattr(LocalRapidOcrBackend, "available", lambda self: False)
    monkeypatch.setattr(CloudApiBackend, "available", lambda self: bool(self._key))
    backend = select_ocr_backend(
        Settings(), **_ocr_strategy(_rapidocr_row(), _cloud_row(api_key=None))
    )
    assert backend is None


def test_settings_no_longer_advertise_ocr_backend_switches() -> None:
    """P1-5：`ocr_backend` / `ocr_api_key` / `ocr_api_url` / `ocr_provider` 已不是配置项。

    它们在 .env.example 与运行环境页上被宣传为"可改"，而工厂里对应的分支生产上从不执行
    （服务页恒有一条启用的内置行）—— 一个改了不生效的开关比没有开关更糟。
    删掉字段本身，比留着并给它加一条"仅首次启动生效"的注释诚实。
    """
    names = set(Settings.model_fields)
    assert not {"ocr_backend", "ocr_api_key", "ocr_api_url", "ocr_provider"} & names
    assert "ocr_python" in names  # 唯一仍归 env 的 OCR 项：本地 RapidOCR 的解释器路径


# -- Office OOXML：docx / pptx / xlsx（zip + XML，零依赖） --------------------------------


def test_docx_extraction(tmp_path: Path) -> None:
    f = tmp_path / "报告.docx"
    f.write_bytes(_make_docx_bytes(["随访须知", "每半年复查一次超声。"]))
    out = parse_document(f)
    assert "随访须知" in out
    assert "每半年复查一次超声。" in out
    assert "\n" in out  # 段落被保留为换行


def test_pptx_extraction(tmp_path: Path) -> None:
    f = tmp_path / "幻灯片.pptx"
    f.write_bytes(_make_pptx_bytes([["第一页标题"], ["第二页要点", "复查频率"]]))
    out = parse_document(f)
    assert "第一页标题" in out
    assert "复查频率" in out


def test_pptx_slide_order_is_numeric_not_lexicographic(tmp_path: Path) -> None:
    """**≥10 页的页序回归**（代码审查报告（第二轮）M9）。

    `slide10.xml` 在字典序里排在 `slide2.xml` 前面（'1' < '2'），于是 10 页以上的 PPT
    会被抽出错乱的页序 —— 第 10 页的数值出现在第 1 页附近，结构化抽取拿到的上下文是错的，
    而且是**静默**的语义错误。修复方式是按编号排序。
    """
    slides = [[f"第{i}页"] for i in range(1, 13)]  # 1..12，确保跨过两位数的分界
    f = tmp_path / "long.pptx"
    f.write_bytes(_make_pptx_bytes(slides))
    out = parse_document(f)

    positions = [out.index(f"第{i}页") for i in range(1, 13)]
    assert positions == sorted(positions), f"页序错乱：{positions}"
    # 明确钉住修复前出错的相邻对：第 2 页必须排在第 10 页之前
    assert out.index("第2页") < out.index("第10页")


def test_xlsx_extraction(tmp_path: Path) -> None:
    f = tmp_path / "指标.xlsx"
    f.write_bytes(_make_xlsx_bytes(["血糖", "6.1"]))
    out = parse_document(f)
    assert "血糖" in out and "6.1" in out


def test_broken_office_file_raises_parse_error(tmp_path: Path) -> None:
    """非 zip / 损坏的 .docx → ParseError（而不是 zipfile.BadZipFile 冒出来）。"""
    f = tmp_path / "坏.docx"
    f.write_bytes(b"not a zip at all")
    with pytest.raises(ParseError):
        parse_document(f)


def test_docx_without_document_xml_raises(tmp_path: Path) -> None:
    f = tmp_path / "缺件.docx"
    f.write_bytes(_zip_bytes({"other.xml": "<x/>"}))
    with pytest.raises(ParseError):
        parse_document(f)


def test_missing_file_raises_a_readable_parse_error(tmp_path: Path) -> None:
    """文件不存在 → `ParseError`（可读），**不是**让 `FileNotFoundError` 冒出去。

    为什么这条重要：调用方只把 `ParseError` 翻译成有说明的响应，其它异常一律变成
    "500 没有任何解释"。而这条路径在真实部署里成立 —— 台账记着某个文件，磁盘上却没有
    （人工删过 / 数据卷被重置）。实测：不拦住它时，"重复上传同一个已登记过的文件"会 500。
    """
    missing = tmp_path / "从未存在过.txt"
    with pytest.raises(ParseError, match="不存在或不可读"):
        parse_document(missing)


def test_a_directory_is_not_silently_treated_as_a_file(tmp_path: Path) -> None:
    """传进来一个目录也要是可读错误，而不是 IsADirectoryError 之类。"""
    with pytest.raises(ParseError, match="不存在或不可读"):
        parse_document(tmp_path)


# -- XML 实体加固（代码审查报告（第二轮）A3 残留） --------------------------------


_BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE w:document [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
  <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">
  <!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">
]>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>&e;</w:t></w:r></w:p></w:body>
</w:document>"""


def test_docx_with_a_dtd_is_rejected_before_parsing(tmp_path: Path) -> None:
    """billion laughs（实体递归展开）必须在**解析之前**被拒。

    修复前这里走的是标准库 `ET.fromstring`，注释里写的理由是"攻击面已由解压规模上限收窄"
    —— 但 zip 的规模上限管的是**解压后**的字节数，而实体展开的放大发生在**解析期**：
    压缩后几 KB 的部件可以展开成几百 MB 的内存。现在带 DTD / 实体声明的部件一律拒绝，
    且拒绝发生在调用 expat 之前，不依赖 expat 的版本或默认限额。
    """
    f = tmp_path / "炸弹.docx"
    f.write_bytes(_zip_bytes({"word/document.xml": _BILLION_LAUGHS}))
    with pytest.raises(ParseError, match="DTD / 实体声明"):
        parse_document(f)


def test_a_doctype_hidden_behind_a_long_comment_is_still_rejected(tmp_path: Path) -> None:
    """**绕过回归**：用超长注释把 DOCTYPE 推到"只扫开头 N 字节"的窗口之外。

    XML 允许在 DOCTYPE 前放任意长度的注释，所以任何"只看前 N 字节"的实现都有一个可绕过的
    窗口。这里刻意填 8KB 注释 —— 远大于常见的 4KB 窗口，用来钉住"必须扫全量"这个结论。
    """
    padding = "<!-- " + ("x" * 8192) + " -->"
    payload = '<?xml version="1.0"?>\n' + padding + _BILLION_LAUGHS.split("\n", 1)[1]
    f = tmp_path / "填充.docx"
    f.write_bytes(_zip_bytes({"word/document.xml": payload}))
    with pytest.raises(ParseError, match="DTD / 实体声明"):
        parse_document(f)


def test_pptx_and_xlsx_parts_are_checked_too(tmp_path: Path) -> None:
    """加固落在**共用的部件解析入口**上：不是只补了 docx 这一条路径。"""
    pptx = tmp_path / "幻灯片.pptx"
    pptx.write_bytes(
        _zip_bytes(
            {
                "ppt/slides/slide1.xml": (
                    '<?xml version="1.0"?><!DOCTYPE p:sld [<!ENTITY x "y">]>'
                    '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    "<a:p><a:r><a:t>hi</a:t></a:r></a:p></p:sld>"
                )
            }
        )
    )
    with pytest.raises(ParseError, match="DTD / 实体声明"):
        parse_document(pptx)

    xlsx = tmp_path / "表.xlsx"
    xlsx.write_bytes(
        _zip_bytes(
            {
                "xl/worksheets/sheet1.xml": (
                    '<?xml version="1.0"?><!DOCTYPE worksheet [<!ENTITY x "y">]>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/'
                    'spreadsheetml/2006/main"><sheetData/></worksheet>'
                )
            }
        )
    )
    with pytest.raises(ParseError, match="DTD / 实体声明"):
        parse_document(xlsx)


def test_a_literal_doctype_in_escaped_text_is_not_mistaken_for_markup(tmp_path: Path) -> None:
    """反向保护：正文里**转义后**的 `<` 不会被误判。

    合法 XML 里出现在文本内容中的 `<` 必须写成 `&lt;`，所以"描述一段 XML"的文档
    （比如本项目自己的技术文档）不会被这条加固拒掉。这条断言防的是"加固过度"，
    过度拦截会让真实用户的文档解析失败 —— 那和漏拦一样是缺陷。
    """
    doc = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>示例写法：&lt;!DOCTYPE foo&gt; 不要照抄</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    f = tmp_path / "文档.docx"
    f.write_bytes(_zip_bytes({"word/document.xml": doc}))
    out = parse_document(f)
    assert "DOCTYPE" in out  # 文本被正常抽出（转义还原）
