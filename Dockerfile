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
CMD ["uvicorn", "--factory", "rolecard_agent.api.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
