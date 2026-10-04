"""解析与 OCR 的**共用异常**（rag 包内的叶子模块）。

为什么单独立一个模块：`parser.py` 与 `ocr.py` 从前互相 import —— `ocr` 顶层要这两个异常名，
`parser` 在函数里要 `LocalRapidOcrBackend`，方向于是成了**环**。导入不炸（一半是延迟 import），
但"谁先被 import 谁"从此取决于调用顺序，一次顺手把 lazy import 提到顶层就当场炸，而且炸在
运行时而不是评审时（`pyproject` 的「包内无循环导入」契约现在盯着这件事）。

异常是两侧共同的**对外契约**（上传/抽取端点只把 `ParseError` 翻译成可读响应），
所以它不属于任何一侧，属于中间那个谁都可以依赖的叶子。`parser.py` 再导出这两个名字，
外部调用点（`api/routers/{records,sessions}.py`、测试）一个字都不必改。
"""

from __future__ import annotations


class ParseError(Exception):
    """解析失败（含 OCR 不可用）。携带可读原因，绝不含栈或内部路径。"""


class OcrUnavailable(ParseError):
    """OCR 后端未配置 / 不可用：图片当前无法解析，应保持 pending。"""


__all__ = ["OcrUnavailable", "ParseError"]
