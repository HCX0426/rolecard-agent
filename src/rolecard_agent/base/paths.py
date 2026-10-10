"""项目内本地路径发现（core 层，向下无依赖）。

此前 `default_ocr_python` 定义在 `rag/parser.py`，导致 `core/models/services.py` 反向 import `rag`
（core→rag 跨层耦合，见审查 M10）。OCR 解释器只是"项目根的独立 venv"路径问题，与 rag 无关，
故下沉到 core 层；`rag` 仍可 import core（向下依赖合规）。

里程碑 D②-4（后端打进桌面安装包）把另外两样也放进来：`bundle_root()`（**随包资源**在哪）与
`user_data_root()`（**用户数据**在哪）。分开的理由是硬的：安装目录可能根本不可写
（Program Files），而且升级是整目录替换 —— 数据库与知识库放进去等于"更新一次丢一次"。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 唯一出处是 `base/app_identity.py`（`R102-67`）：那个零依赖常量模块还要喂给
# scripts/forensics/scratch_db.py（它必须在不可安装环境独立跑，只能 import 零依赖件）。
from rolecard_agent.base.app_identity import APP_NAME as _APP_NAME  # noqa: E402

IS_WINDOWS = sys.platform.startswith("win")


def is_frozen() -> bool:
    """是否跑在打包出来的可执行文件里（PyInstaller onedir 会同时设这两个）。"""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


#: 运行形态的自报标记，唯一写手是 `Dockerfile` 的那行 `ENV`（门禁的形态矩阵那条尺子盯着它）。
#: **刻意不写进 `.env.example`**：它是形态自报，不是给人调的旋钮 —— 让操作员能改，等于让人
#: 能把"我在容器里"这件事说错，而 `rag/ocr.py` 服务页那格的文案正是照它分岔的。
RUNTIME_FORM_ENV = "ROLECARD_RUNTIME_FORM"


def runtime_form() -> str:
    """这一进程跑在哪种形态里：`container` / `desktop`（冻结态装机版）/ `source`（开发态）。

    为什么要一个形态判据（2026-10-04 快照"容器形态静默缺本地 OCR"那一格，决策七）：
    "本地 OCR 不可用"在三种形态里**原因不同、可操作的路也不同** —— 开发态是没装 `.venv-ocr`
    （能装）、装机版是这一包没带 worker（重打）、容器里是镜像**按设计**不装 OCR 那一族
    （装不了，唯一的路是云端兜底）。从前服务页那格只写前两种，容器里的人拿到的是一句
    在镜像里做不到的建议。

    判据是**自报**而不是探测，两条理由都是硬的：
      * `/proc/1/cgroup` 那类探测在 cgroup v2 下已经不区分容器与宿主 —— 看着更硬，
        其实更容易说错话；
      * `/.dockerenv` 会让答案取决于**宿主环境**：门禁自己的 CI job 若在容器里跑，
        "开发态"的用例会被判成容器态 —— 一个换台机器就换答案的判据写不出可复现的用例。
    自报 + 尺子盯住 Dockerfile 那一行，才是可测的那一种（判据与文案同一条纪律）。
    """
    if os.environ.get(RUNTIME_FORM_ENV, "").strip().lower() == "container":
        return "container"
    return "desktop" if is_frozen() else "source"


def repo_root() -> Path:
    """仓库根（开发态）：本文件在 `<root>/src/rolecard_agent/base/paths.py`。"""
    return Path(__file__).resolve().parents[3]


def bundle_root() -> Path:
    """随包只读资源的落点：冻结态是 `sys._MEIPASS`（onedir 下即 `_internal/`），开发态是仓库根。

    前端构建产物（`frontend/dist`）这类"打进包里但不该被改"的东西都从这里找。
    """
    if is_frozen():
        return Path(str(sys._MEIPASS))  # type: ignore[attr-defined]  # PyInstaller 注入，typeshed 不认识
    return repo_root()


def console_dist_dir() -> Path:
    """控制台构建产物（`frontend/dist`）的落点 —— **"随包素材"该去哪儿找的唯一答案**。

    桌宠形象包要扫这里（随包那份 `dist/pets/`），静态托管也挂这里，所以两边必须问同一个
    函数：各写一份 `bundle_root() / "frontend" / "dist"`，就会有"界面能打开但素材清单扫不到"
    这种只对其中一边红的问题。`FRONTEND_DIST` 那一条部署期覆盖也在这里读一次。
    """
    return Path(os.environ.get("FRONTEND_DIST") or (bundle_root() / "frontend" / "dist"))


def _platform_data_root() -> Path:
    """没有显式覆盖时的数据根：**永远可写**，sqlite / chroma / uploads 的默认父目录。

    开发态保持仓库 `data/`（现有约定与测试都指这儿，改它会让"跑一次测试"污染安装包目录）；
    冻结态换到系统数据目录（Windows 用 `%LOCALAPPDATA%`，其他平台 `~/.local/share`）。
    安装目录本身不能当数据根：它可能不可写，而且升级是整目录替换。
    """
    if not is_frozen():
        return repo_root() / "data"
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / _APP_NAME
    return Path.home() / ".local" / "share" / _APP_NAME


#: 一个数据根下的四样东西（M4）。它们**必须**同根：只换库不换索引，症状就是
#: §4.1 那句"记忆没了、向量库还在"的半吊子状态 —— 比不换更像数据损坏。
#: 工作区也算一样：文件工具与命令执行都落在那儿，两个实例共用它 = 第二个人读得到
#: 第一个人的文件。
DATA_PATH_ENVS = ("SQLITE_PATH", "CHROMA_PATH", "UPLOAD_DIR", "WORKSPACE_DIR")


def data_paths() -> dict[str, Path]:
    """`{环境变量名: 该数据根下的默认绝对路径}` —— 这些路径的**唯一**推导处。

    以前这段推导长在 `scripts/run_api.py` 的启动器里，于是"绕过启动器直接
    `Settings.from_env()`"的那些进程（测试、脚本、第二实例若手工起）拿到的是字段默认值
    `./data/...`，按 CWD 解析 —— 换身份时很容易只换掉其中一条。推导下移到 `core` 之后，
    配置层与启动器读的是同一份规则。
    """
    root = user_data_root()
    return {
        "SQLITE_PATH": root / "sqlite" / "app.db",
        "CHROMA_PATH": root / "chroma",
        "UPLOAD_DIR": root / "uploads",
        "WORKSPACE_DIR": root / "workspace",
    }


def user_data_root() -> Path:
    """用户数据根，可被 `DATA_ROOT` 整体搬走（M4：换身份 = 换一个根，只需要这一个变量）。

    相对值按"平台根的父目录"解析（开发态=仓库根，打包态=`%LOCALAPPDATA%`），不递归回本函数，
    所以 `DATA_ROOT=./x` 这种写法不会把自己绕死。建议给绝对路径 —— 第二实例本来就是新进程，
    写全一个路径比记一条解析规则便宜。
    """
    raw = (os.environ.get("DATA_ROOT") or "").strip()
    if not raw:
        return _platform_data_root()
    given = Path(raw).expanduser()
    if given.is_absolute():
        return given
    base = repo_root() if not is_frozen() else _platform_data_root().parent
    return base / given


def split_root_notice(
    *,
    sqlite_path: str | Path = "",
    chroma_path: str | Path = "",
    upload_dir: str | Path = "",
    workspace_dir: str | Path = "",
) -> str | None:
    """这些数据路径有没有"只搬走了一半"——搬走了一半就出声，别让人猜。

    判据不是"路径必须等于默认布局"（合法地各自指到别处是允许的），而是**一致性**：
    有的在这根下、有的不在，就是 §4.1 那句"记忆没了、向量库还在"的形状 —— 换身份时
    只换了 `SQLITE_PATH`，索引、上传件与工作区还留在上一份数据那边。那种状态比不换更糟，
    因为它看起来像数据损坏。

    只出声不失败：探针与实验**故意**只换库（拿副本做实验、索引读公共那份），门禁与测试也
    都这么跑；把"知道自己在干什么"的用法变成红，换来的只会是所有人学会忽略这行字。
    """
    given = dict(
        zip(
            DATA_PATH_ENVS,
            (sqlite_path, chroma_path, upload_dir, workspace_dir),
            strict=True,
        )
    )
    if not any(str(v) for v in given.values()):
        return None
    try:
        root = user_data_root().resolve()
    except OSError:
        return None

    def inside(raw: str | Path) -> bool:
        text = str(raw)
        if not text:
            return True  # 没配 = 用字段默认，不参与"半搬"的判定
        try:
            return Path(text).resolve().is_relative_to(root)
        except OSError:
            return False

    flags = {name: inside(value) for name, value in given.items()}
    if all(flags.values()) or not any(flags.values()):
        return None
    outside = [name for name, ok in flags.items() if not ok]
    return (
        f"数据根被搬走了一半：{', '.join(outside)} 不在 {root} 下面。"
        f"换身份 / 起第二实例请设一个 DATA_ROOT（三样路径由它一起决定），"
        f"或把三条都显式指到同一个根 —— 只换库会让索引与上传件留在上一份数据那边。"
    )


def path_from_config(value: str | Path) -> Path:
    """配置（`.env` / 界面里的运行环境）里的路径 → 绝对路径。

    相对路径的基准**故意不是数据根**：`.env.example` 写的是 `./data/sqlite/app.db`，那是
    按仓库根成立的历史约定，换成按数据根解析会静默变成 `<repo>/data/data/...` —— 用户看
    着"路径没改过"却发现库空了。打包态没有仓库根，才退到数据根。
    """
    path = Path(value)
    if path.is_absolute():
        return path
    return (user_data_root() if is_frozen() else repo_root()) / path


def dotenv_path() -> Path:
    """`.env` 的落点：开发态在仓库根；冻结态在用户数据根旁边（安装目录不是配置位）。

    冻结态为什么不直接读 `user_data_root()/.env`：那个目录是 `…/rolecard-agent`，本身就是
    数据根，配置混进数据里会让"备份/清空数据"这类操作多一个坑。
    """
    if not is_frozen():
        return repo_root() / ".env"
    return user_data_root().parent / f"{_APP_NAME}.env"


def bundled_ocr_worker() -> Path | None:
    """随包 OCR worker 的可执行文件（冻结态）；开发态返回 None。

    装完的运行期布局是 `resources/rolecard-backend/_internal`（= `sys._MEIPASS`）与
    `resources/ocr-worker/ocr-worker.exe` 并排，所以 `resources` 就是 `_MEIPASS` 往上两层。
    找不到返回 None 而不是抛 —— "这一包没带 OCR"是一个合法状态，调用方要据此降级并说清原因。

    为什么是独立 exe 而不是拷 `.venv-ocr`：venv 的 `python.exe` 依赖构建机上的 base 解释器，
    拷进包等于把"只有我这台机器成立的前提"发给装机的人（见 `packaging/ocr-worker.spec` 头部）。
    """
    if not is_frozen():
        return None
    resources = Path(str(sys._MEIPASS)).resolve().parents[1]  # type: ignore[attr-defined]
    name = "ocr-worker.exe" if IS_WINDOWS else "ocr-worker"
    cand = resources / "ocr-worker" / name
    return cand if cand.exists() else None


def default_ocr_python() -> str | None:
    """默认 OCR 解释器：项目根下的独立 venv（requirements/requirements-ocr.txt 的安装约定）。

    冻结态这里仍然返回 None —— 但**那不再等于"装机版没有本地 OCR"**：装机版走
    `bundled_ocr_worker()` 那个自包含的 exe（10-03 起随包，见 `packaging/ocr-worker.spec`）。
    从前这一格写着"打包态必然 None，表现是本地 OCR 不可用"，那是当时的设计；
    留着不改就是一句会说错话的注释（判据与文案同一条纪律）。
    """
    cand = repo_root() / ".venv-ocr" / "Scripts" / "python.exe"
    return str(cand) if cand.exists() else None
