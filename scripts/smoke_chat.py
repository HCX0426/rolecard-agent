"""Live smoke test for the M4 chat SSE path - runs against a REAL model, not ScriptedChat.

用法（PowerShell，密钥只经环境变量传入，绝不写进任何文件或提交物）：

    $env:SILICONFLOW_API_KEY = "<你的 key>"
    .venv\\Scripts\\python.exe scripts\\smoke_chat.py

行为：
  * 用 `SILICONFLOW_API_KEY` 动态注册一个 OpenAI 兼容后端（base_url=api.siliconflow.cn），
    并设 `MODEL_DEFAULT=siliconflow` —— 演示了"切后端只改配置、代码一行不动"（US-8）。
  * 在临时目录建库（不碰 data/），走完整链路：create_app → 建会话 → 两次 SSE 对话。
  * 第一条消息验证纯文本令牌流；第二条引导模型调用 list_roles / list_domains，
    验证工具回路（tool_call → tool_result → 汇总回答）。

这是手工冒烟工具，不属于离线测试套件 —— pytest 仍然不测 LLM 本身。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# 直跑脚本时 src/ 不在 sys.path（pytest 由 pyproject 的 pythonpath 兜底，直跑没有）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash"


def _configure_backend() -> None:
    key = os.environ.get("SILICONFLOW_API_KEY")
    if not key:
        raise SystemExit(
            "缺少 SILICONFLOW_API_KEY 环境变量。请先：$env:SILICONFLOW_API_KEY = '<key>'"
        )
    os.environ["MODEL_BACKENDS"] = json.dumps(
        {
            "siliconflow": {
                "provider": "openai",
                "base_url": os.environ.get("SMOKE_BASE_URL", DEFAULT_BASE_URL),
                "model": os.environ.get("SMOKE_MODEL", DEFAULT_MODEL),
                "api_key": key,
            }
        }
    )
    os.environ["MODEL_DEFAULT"] = "siliconflow"


def _stream_chat(client: object, thread_id: str, message: str) -> None:
    print(f"[user] {message}")
    buf = ""
    with client.stream(  # type: ignore[attr-defined]
        "POST", "/api/chat", json={"thread_id": thread_id, "message": message}
    ) as res:
        if res.status_code != 200:
            print(f"  HTTP {res.status_code}: {res.read()[:200]!r}")
            return
        for chunk in res.iter_text():
            buf += chunk
            while (idx := buf.find("\n\n")) >= 0:
                frame, buf = buf[:idx], buf[idx + 2 :]
                for line in frame.split("\n"):
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    kind = event["type"]
                    if kind == "token":
                        print(event["text"], end="", flush=True)
                    elif kind == "message_replace":
                        print(f"\n  [权威替换] {event['text']}", flush=True)
                    elif kind == "tool_call":
                        args = json.dumps(event["args"], ensure_ascii=False)
                        print(f"\n  [工具调用] {event['name']} {args}")
                    elif kind == "tool_result":
                        print(f"\n  [工具结果] {str(event['content'])[:120]}")
                    elif kind == "error":
                        print(f"\n  [错误] {event['detail']}")
                    elif kind == "end":
                        print("\n")


def main() -> None:
    _configure_backend()
    # 延迟导入：确保 Settings.from_env() 读到的是上面刚设置的环境变量。
    from fastapi.testclient import TestClient

    from rolecard_agent.api.main import create_app

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        # ignore_cleanup_errors：sqlite 连接在进程退出前仍持有 smoke.db，且本机的
        # safe-delete 会把"进程占用"升级成硬错误。临时目录留几个字节在 %TEMP% 无所谓。
        app = create_app(sqlite_path=Path(tmp) / "smoke.db")
        with TestClient(app) as client:
            session = client.post("/api/session", json={}).json()
            thread_id = str(session["thread_id"])
            print(f"[会话] {thread_id} · 角色：{session['role_name']}")
            _stream_chat(client, thread_id, "用一句话介绍你自己，并说明你只能做什么。")
            _stream_chat(client, thread_id, "这个系统里现在有哪些角色？请调用工具查一下再回答。")


if __name__ == "__main__":
    main()
