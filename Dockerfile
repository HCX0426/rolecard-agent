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
COPY requirements.txt requirements-api.txt requirements-rag.txt ./
RUN pip install --no-cache-dir \
    -r requirements.txt -r requirements-api.txt -r requirements-rag.txt

COPY src/ ./src/
COPY frontend/dist/ ./frontend/dist/
COPY scripts/ ./scripts/

EXPOSE 8000
# 容器内绑 0.0.0.0 是标准做法；数据（SQLite + 上传）必须挂卷：
#   docker run -p 8000:8000 -v rolecard-data:/app/data <image>
CMD ["uvicorn", "--factory", "rolecard_agent.api.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
