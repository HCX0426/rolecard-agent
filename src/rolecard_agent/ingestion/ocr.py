"""PaddleOCR backend. DEFERRED (planned, not dropped).

Must run in a SEPARATE venv / process: PaddleOCR pulls its own numpy/opencv/onnxruntime
stack, and paddlepaddle wheels lag Python releases (hence Python 3.11, not 3.12/3.13).
"""
