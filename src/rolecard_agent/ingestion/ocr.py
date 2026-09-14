"""PaddleOCR backend. DEFERRED (planned, not dropped).

Must run in a SEPARATE venv / process: PaddleOCR pulls its own numpy/opencv/onnxruntime
stack. It still runs fine on the project's 3.13 baseline: paddleocr/paddlex are pure-python,
paddlepaddle ships cp313 wheels, and OpenCV uses cp37-abi3.
"""
