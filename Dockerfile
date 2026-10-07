# rolecard-agent 运行时镜像。
# 前端构建产物 frontend/dist 已入库，所以镜像里不需要 node —— 构建一次产物，处处可跑。
# 依赖**锁安装**（2026-10-07 拍板「上锁文件」）：requirements-runtime.lock 由 pip-compile
# 从运行时五族的 requirements 镜像产出（输入面钉在 check_consistency 的 LOCK_SURFACES），
# fresh install 逐字节可重现；数据目录挂卷，绝不把演示库打进镜像。
FROM python:3.13-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# 先装依赖（利用层缓存），再拷代码。装的是**锁**（requirements-runtime.lock = 运行时五族
# 的全量 pin），但"为什么五族一份都不能少"的历史还在下面 —— 锁治的是"装到的版本会漂"，
# 治不了"某条路径忘了装某一族"，那是 installer scope parity / 能力矩阵两条尺子的辖区。
# requirements-rag（chromadb / pypdf）必须一起装：知识库、上传解析、检索工具都依赖它，
# 漏装会让容器内 /api/knowledge 与上传直接 ImportError（v2.4 部署前的审查发现）。
# requirements-cloud（langchain-openai）同理必须装：容器里没有 Ollama，云端 key 本是
# 镜像的主用例 —— 漏装的症状是"配任何 OpenAI 兼容端点（硅基流动/DeepSeek/…）保存即 500"
# （CI 首跑实测，run 36416026240；本机没红只是 .venv 恰好装过它）。它是可选 extra 的原因
# 在 pyproject：纯本地安装保持离线可用 —— 那是**开发机**的取舍，不是容器的。
# requirements-mcp（langchain-mcp-adapters）同理要装（10-01 补）：镜像是 B/S 形态，用户在
# 设置页接 MCP server 是合法的 operator 动作，而未装时 loader 只打一行 warning 就跳过工具 ——
# 界面照常摆着入口、交通灯照常画，工具永远加载不出来（"格子骗人"的 fail-open）。
COPY requirements-runtime.lock ./
RUN pip install --no-cache-dir -r requirements-runtime.lock

# 形态自报（审查快照决策七"容器无本地 OCR"那一格的收口）：容器里没有 .venv-ocr、也没有随包
# worker，所以"本地 OCR 不可用"在这里是**设计**而不是缺装 —— rag/ocr.py 据此把服务页那格的
# 指引换成可操作的那条：模型页建云端 OCR 凭据行 → 服务页的 OCR 端点序里引用它（图片会外发
# 第三方，只允许操作员显式配置，绝不从 env 默认启用）。删掉这一行谁都不会当场炸，只会让容器
# 里的人重新拿到"装 .venv-ocr / 重打这一包"这种在镜像里做不到的建议 —— 所以
# capability-matrix 那条尺子盯着这一个字面量，它必须与 base/paths.py 的常量同名。
ENV ROLECARD_RUNTIME_FORM=container

COPY src/ ./src/
COPY frontend/dist/ ./frontend/dist/
COPY scripts/ ./scripts/

# 以**非 root**跑（09-28 轮 `R28-29`）。为什么现在改：镜像里那三条写入（sqlite / chroma /
# uploads）全落在 /app/data，root 跑起来的容器对编排器是黑盒 —— 探针看不到"活着但没就绪"，
# 而数据卷的属主是 root，等于把"这台机器上的数据归谁"写死成"归容器里那个万能账号"。
# uid 固定成 10001 而不是让系统挑：挂卷的人要能对着这一个数字 chown。
# ⚠️ 换这一步之前**已有的** `rolecard-data` 卷还是 root 属主，非 root 进程打不开它，
# 症状是启动即报 sqlite 无法打开 —— 修一次就够：
#   docker run --rm --user root -v rolecard-data:/app/data <image> \
#     sh -c 'chown -R 10001:10001 /app/data'
# （本仓库还没把镜像发给任何人，所以这条迁移目前没有真实受众。）
RUN groupadd --system --gid 10001 rolecard \
    && useradd --system --uid 10001 --gid rolecard --home-dir /app --no-create-home rolecard \
    && mkdir -p /app/data \
    && chown -R 10001:10001 /app/data
USER 10001:10001

EXPOSE 8000
# 容器内绑 0.0.0.0 是标准做法；但 create_app 有公网护栏：非回环 + AUTH_MODE=off 会**拒绝启动**
# （把"忘记配鉴权就公网裸奔"变成起不来的硬失败）。公网部署必须显式开鉴权：
#   docker run -e AUTH_MODE=on -e AUTH_CREDENTIALS=<user>:<pass> -p 8000:8000 \
#     -v rolecard-data:/app/data <image>
# RUN_API_HOST 让护栏看到实际绑定地址（uvicorn 的 --host 不会自动进环境变量）。
ENV RUN_API_HOST=0.0.0.0
# 存活探针（`R28-29` 的另一半）：编排器据此判断"进程在 ≠ 能服务"，`restart: unless-stopped`
# 这类策略也才有东西可读。判据用**应用自己那条豁免路径** `/api/health`，所以：
#   * 不需要在镜像里装 curl（slim 基座没有，装它等于给攻击面多加一个能出网的二进制）；
#   * AUTH_MODE=on 时也不会永远 unhealthy —— `AUTH_EXEMPT_PATHS` 默认就含这一条，
#     这条探针顺带把"豁免确实生效"钉在镜像里（CI 的 docker job 会等它变 healthy）。
# 端口读 `RUN_API_PORT`（shell 形式才展开得到环境变量；exec 形式的 CMD 不走 shell）。
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ.get('RUN_API_PORT','8000')+'/api/health',timeout=4)" || exit 1
# 入口走 scripts/run_api.py，**不是**直调 uvicorn（09-28 轮 `R28-30`）：那三条启动期动作只长在
# 启动器里 —— `_maybe_configure_cloud_backend()`（给了 SILICONFLOW_API_KEY 就注册云端后端并设为
# 默认）、`_resolve_data_paths()`（数据路径与 CWD 解耦）、`force_utf8_stdio()`（打包/容器里
# stdout 不是终端，不钉 UTF-8 日志里的中文全变成 U+FFFD）。CMD 直调 uvicorn 时
# `docker run -e SILICONFLOW_API_KEY=…` **静默落回 Ollama 默认后端**：镜像里没有 Ollama，
# 于是症状是"配了 key 但什么都调不通"，而容器日志里一句相关的话都没有。
CMD ["python", "scripts/run_api.py"]
