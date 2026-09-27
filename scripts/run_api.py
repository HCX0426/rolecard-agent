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

import contextlib
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
                # provider 记**供应商身份**（界面显示"硅基流动"），不是端点风格；
                # 客户端风格由目录决定（siliconflow → OpenAI 兼容）。
                "provider": "siliconflow",
                "base_url": os.environ.get("SMOKE_BASE_URL", DEFAULT_BASE_URL),
                "model": os.environ.get("SMOKE_MODEL", DEFAULT_MODEL),
                "api_key": key,
            }
        }
    )
    os.environ["MODEL_DEFAULT"] = "siliconflow"


def _resolve_data_paths() -> None:
    """让数据路径与启动 CWD 解耦：相对路径一律按数据根解析。

    否则从非项目根目录启动（某些终端/IDE 的默认工作目录，或本项目的自动化启动脚本）
    时，config 里的 `./data/...` 会落到错误位置，表现为"/api/knowledge 返回 []、会话与
    知识全空"等假性故障——审计中已踩到并定位。落进启动器，保证无论从哪里启动都指向同一
    份真实数据。

    数据根由 `core/paths.user_data_root()` 决定：**开发态是仓库 `data/`，打包态是
    `%LOCALAPPDATA%\rolecard-agent`**。后者不是可选项 —— 安装目录可能不可写，而且升级是
    整目录替换，库放进去等于"更新一次丢一次"。

    规则：环境变量未设置 → 用数据根下的默认绝对路径（推导在 `core/paths.data_paths()`，
    配置层与启动器共用同一份，不再各写一遍）；已设置且为绝对路径 → 原样保留
    （用户显式覆盖优先）；已设置但为相对路径 → 按 `path_from_config` 的基准解析（CWD 无关）。

    而那个根本身可以被 `DATA_ROOT` 整份搬走（M4）：**换身份 = 换一个根，只需要这一个变量**。
    只换三条里的某一条，症状就是 §4.1 那句"记忆没了、向量库还在"—— 比不换更像数据损坏，
    所以最后会出声一句（`split_root_notice`）。
    """
    from rolecard_agent.core.paths import (
        DATA_PATH_ENVS,
        data_paths,
        path_from_config,
        split_root_notice,
    )

    defaults = data_paths()
    for key, default_abs in defaults.items():
        val = os.environ.get(key)
        if not val:
            os.environ[key] = str(default_abs)
        else:
            # 相对值按**仓库根**（开发态）解析，不是按数据根：`.env.example` 里的
            # `./data/...` 就是这么约定的，换基准会让"没改过路径"的人凭空丢库。
            os.environ[key] = str(path_from_config(val))
    notice = split_root_notice(
        sqlite_path=os.environ["SQLITE_PATH"],
        chroma_path=os.environ["CHROMA_PATH"],
        upload_dir=os.environ["UPLOAD_DIR"],
        workspace_dir=os.environ["WORKSPACE_DIR"],
    )
    if notice:
        print(f"[run_api] 注意：{notice}")
    # 确保落点目录存在：fresh clone / 首次启动时不因父目录缺失而 500（sqlite 的 connect
    # 不会自动建父目录）。
    for key in DATA_PATH_ENVS:
        p = Path(os.environ[key])
        (p.parent if key == "SQLITE_PATH" else p).mkdir(parents=True, exist_ok=True)
    # 但"建出来"这件事在一种情况下必须出声：仓库那份开发态库已经在 2026-09-25 被**有意**
    # 隔离成陈旧快照（真数据在安装目录下那份），于是这一次 mkdir 会让一个 0 行的空库
    # 重新出现在 `data/sqlite/` 里。静默发生的恢复看起来就像"我的库被清空了"（09-26 轮
    # R26-18）。只提示、不改路径：开发态根本身就是合法的，问题从来是"没人告诉你它被建了"。
    dev_db = Path(os.environ["SQLITE_PATH"])
    marker = dev_db.parent / "_stale-dev-snapshot-20260924"
    if not dev_db.exists() and marker.exists():
        print(
            f"[run_api] 注意：正在**新建一份空的开发态库** {dev_db}\n"
            f"[run_api]       原来那份已被有意隔离到 {marker}；真实数据在安装目录下\n"
            f"[run_api]       %LOCALAPPDATA%\\rolecard-agent\\sqlite\\app.db（实验取数请用它，"
            f"或设 LIVE_DB_PATH）。",
            file=sys.stderr,
            flush=True,
        )


def _load_dotenv() -> None:
    """读取仓库根的 `.env`（若存在），把 KEY=VALUE 写进 os.environ。

    为什么手写 ~20 行而不引 python-dotenv：配置契约仍是"环境变量"（config.py 不读文件），
    `.env` 只是本地启动时设置环境变量的便捷容器 —— 引一个依赖来省 20 行不值。规则：
    ① 真实环境变量优先（`.env` 只填空位，不覆盖已在 shell 里 export 的值）；
    ② 空值/注释跳过；③ 剥一层成对引号。`.env` 已在 .gitignore，密钥不会入库。

    落点由 `core/paths.dotenv_path()` 决定：开发态是仓库根，打包态是用户数据目录旁边
    （安装目录既可能不可写，也会被升级整目录替换，不能当配置位）。
    """
    from rolecard_agent.core.paths import dotenv_path

    env_file = dotenv_path()
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value


def force_utf8_stdio() -> None:
    """把 stdout/stderr 钉成 UTF-8。**打包态不修这一条，日志里所有中文都是 U+FFFD。**

    实测链（09-26，装在 `%APPDATA%` 那份的 `backend.log`，1622 行里带中文的 3 行全坏）：
    壳用 `spawn(stdio:["ignore","pipe","pipe"])` 接管后端的输出，并且先
    `stream.setEncoding("utf8")` 再落盘；而 Python 在 **stdout 不是终端** 时按 ANSI 代码页
    编码（本机 cp936）⇒ cp936 的字节被当 UTF-8 解。受害者正是这一族里唯一"给人读"的字段：
    主动开口那句静默原因（`S-8` 的日志出口）与启动横幅。

    修在**生产端**而不是让壳改成"解不开就当 GBK"：谁接管都按 UTF-8 说得清自己，dev 终端、
    PowerShell、Electron、Docker 四条路共用一份行为。`errors="replace"` 是为了让编码问题
    永远只是掉个别字符，不再像 `R26-24` 那次把整次打包的退出码带崩。
    """
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure"):
            # 被换成没有 `reconfigure` 的对象（测试替身 / 已被别处重包一层）时不改，
            # 但绝不能为这件事炸启动 —— 日志编码不值得让服务器起不来。
            continue
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    force_utf8_stdio()
    _load_dotenv()
    _resolve_data_paths()
    _maybe_configure_cloud_backend()
    # 桌面壳 spawn 时会注入 ROLECARD_PARENT_PID：壳被硬杀/崩溃时，这里负责让后端跟着走，
    # 不留孤儿占着端口（本地模型场景还占着几 GB 显存）。没注入 = 不装，人手工起的服务器
    # 不该被一个看门狗杀掉。
    from rolecard_agent.core import parent_watch

    parent_watch.start()
    import uvicorn

    # 默认绑 127.0.0.1（演示绝不裸奔公网）。RUN_API_HOST 可覆盖（如反向代理场景绑 0.0.0.0），
    # 但 create_app 有护栏：非回环 + AUTH_MODE=off 会拒绝启动 —— 换绑不会绕过鉴权。
    host = os.environ.get("RUN_API_HOST", "127.0.0.1")
    port = int(os.environ.get("RUN_API_PORT", "8000"))
    print(f"rolecard-agent 控制台: http://{host}:{port}/", flush=True)
    # RUN_API_RELOAD=1：**开发用**代码热重载 —— watchfiles 监听 src/ 下 .py 变化，
    # 存盘即自动重启 worker。默认关：reload 的本质是"改码即杀进程重启"，运行时状态
    # （连接池、内存缓存）每次存盘都重建一遍，使用/演示场景没必要付这个成本。
    # 只监听 src/：data/（sqlite/chroma 持续写入）与 .venv 不进 watch 范围，避免噪音重启。
    if os.environ.get("RUN_API_RELOAD") == "1":
        uvicorn.run(
            "rolecard_agent.api.main:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            reload_dirs=[str(Path(__file__).resolve().parents[1] / "src")],
        )
        _shutdown_chat_pool()
        _shutdown_approval_executor()
        return
    uvicorn.run("rolecard_agent.api.main:create_app", factory=True, host=host, port=port)
    # 走到这里 = 服务已退出（Ctrl+C / 收到停止信号）。`_CHAT_POOL` 是**进程级**资源，
    # 不能在某个 app 的 lifespan 里关（同进程里可能还有别的 app 实例，测试就是这样
    # 互相干扰的）—— 真实的进程退出路径才是关它的地方（审查报告 P2：客户端生命周期）。
    _shutdown_chat_pool()
    _shutdown_approval_executor()


def _shutdown_approval_executor() -> None:
    """释放命令审批的后台执行池（core/tools/run.py，进程级资源）。"""
    with contextlib.suppress(Exception):
        from rolecard_agent.core.tools.run import shutdown_approval_executor

        shutdown_approval_executor()


def _shutdown_chat_pool() -> None:
    """释放对话线程池（同步 graph.stream 靠它执行，线程池是 module-level 的）。"""
    with contextlib.suppress(Exception):
        from rolecard_agent.api.chat import _CHAT_POOL

        _CHAT_POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
