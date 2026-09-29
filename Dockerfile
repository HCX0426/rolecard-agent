# rolecard-agent 运行时镜像。
# 前端构建产物 frontend/dist 已入库，所以镜像里不需要 node —— 构建一次产物，处处可跑。
# 依赖用 requirements（v1 无锁文件工具链约束）；数据目录挂卷，绝不把演示库打进镜像。
FROM python:3.13-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# 先装依赖（利用层缓存），再拷代码
# requirements-rag.txt（chromadb / pypdf）必须一起装：知识库、上传解析、检索工具都依赖它，
# 漏装会让容器内 /api/knowledge 与上传直接 ImportError（v2.4 部署前的审查发现）。
# requirements-cloud.txt（langchain-openai）同理必须装：容器里没有 Ollama，云端 key 本是
# 镜像的主用例 —— 漏装的症状是"配任何 OpenAI 兼容端点（硅基流动/DeepSeek/…）保存即 500"
# （CI 首跑实测，run 36416026240；本机没红只是 .venv 恰好装过它）。它是可选 extra 的原因
# 在 pyproject：纯本地安装保持离线可用 —— 那是**开发机**的取舍，不是容器的。
COPY requirements.txt requirements-api.txt requirements-rag.txt requirements-cloud.txt ./
RUN pip install --no-cache-dir \
    -r requirements.txt -r requirements-api.txt -r requirements-rag.txt \
    -r requirements-cloud.txt

COPY src/ ./src/
COPY frontend/dist/ ./frontend/dist/
COPY scripts/ ./scripts/

EXPOSE 8000
# 容器内绑 0.0.0.0 是标准做法；但 create_app 有公网护栏：非回环 + AUTH_MODE=off 会**拒绝启动**
# （把"忘记配鉴权就公网裸奔"变成起不来的硬失败）。公网部署必须显式开鉴权：
#   docker run -e AUTH_MODE=on -e AUTH_CREDENTIALS=<user>:<pass> -p 8000:8000 \
#     -v rolecard-data:/app/data <image>
# RUN_API_HOST 让护栏看到实际绑定地址（uvicorn 的 --host 不会自动进环境变量）。
ENV RUN_API_HOST=0.0.0.0
# 入口走 scripts/run_api.py，**不是**直调 uvicorn（09-28 轮 `R28-30`）：那三条启动期动作只长在
# 启动器里 —— `_maybe_configure_cloud_backend()`（给了 SILICONFLOW_API_KEY 就注册云端后端并设为
# 默认）、`_resolve_data_paths()`（数据路径与 CWD 解耦）、`force_utf8_stdio()`（打包/容器里
# stdout 不是终端，不钉 UTF-8 日志里的中文全变成 U+FFFD）。CMD 直调 uvicorn 时
# `docker run -e SILICONFLOW_API_KEY=…` **静默落回 Ollama 默认后端**：镜像里没有 Ollama，
# 于是症状是"配了 key 但什么都调不通"，而容器日志里一句相关的话都没有。
CMD ["python", "scripts/run_api.py"]
