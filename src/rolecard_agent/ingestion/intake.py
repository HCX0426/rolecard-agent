"""Single entry point for document intake. Routes by file type:

  pdf (text layer) / docx / pptx / xlsx  ->  parsers.py   (native parsing, no OCR)
  images / scanned pdf                   ->  ocr.py       (PaddleOCR, DEFERRED)

Both paths converge on: raw text -> LLM structured extraction -> indices + vector store.
Keeping one entry point means adding a new format never touches the agent layer.
"""
