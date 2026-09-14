"""启动管理控制台 + 流式对话服务（M4 演示入口）。

用法（PowerShell）：

    # 有本地 Ollama：直接启动（Settings 默认后端 local）
    .venv\\Scripts\\python.exe scripts\\run_api.py

    # 没跑 Ollama？配任意 OpenAI 兼容端点（密钥只经环境变量，绝不落盘）：
    $env:SILICONFLOW_API_KEY = "<你的 key>"
    .venv\\Scripts\\python.exe scripts\\run_api.py

然后浏览器打开 http://127.0.0.1:8000/ —— 新建会话、流式对话、页面切角色、启停插件。
端口可用环境变量 RUN_API_PORT 覆盖（默认 8000）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 直跑脚本时 src/ 不在 sys.path（pytest 由 pyproject 的 pythonpath 兜底，直跑没有）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash"


def _maybe_configure_cloud_backend() -> None:
    """有 SILICONFLOW_API_KEY 且未显式配置 MODEL_BACKENDS 时，注册硅基流动后端并设为默认。

    只写环境变量 —— Settings.from_env() 负责解析，代码与提交物里永远没有密钥。
    """
    key = os.environ.get("SILICONFLOW_API_KEY")
    if not key or os.environ.get("MODEL_BACKENDS"):
        return  # 无 key 或调用方已显式配置 → 走 Settings 默认（Ollama local）
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


def main() -> None:
    _maybe_configure_cloud_backend()
    import uvicorn

    host = "127.0.0.1"  # 演示绝不绑 0.0.0.0：Ollama 式端口无鉴权，公网暴露会被白嫖
    port = int(os.environ.get("RUN_API_PORT", "8000"))
    print(f"rolecard-agent 控制台: http://{host}:{port}/", flush=True)
    uvicorn.run("rolecard_agent.api.main:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    main()
