"""安装面 / 依赖 / 锁 / 能力矩阵这一族判据（P3-9 按主题细分，从 checks.py 平移）。

按主题拆出，正文与执行顺序由 registry.CHECKS 唯一决定；本模块只负责"这一族住哪"。
import 形状与 checks.py 相同：同目录 import + `.core` 结算面。判据正文一字未改 ——
行为等价由门禁实跑全部 CHECKS 证明。
"""
from __future__ import annotations

import json
import re
import sys
import tomllib

from packaging.requirements import InvalidRequirement, Requirement

from .core import ROOT, fails, out, warns
from .platform_pins import CRASHES_ELSEWHERE, installed_edges, lock_findings, parse_lock

RUNTIME_REQ_FILES = (
    "requirements/requirements.txt",
    "requirements/requirements-api.txt",
    "requirements/requirements-rag.txt",
    "requirements/requirements-cloud.txt",
    "requirements/requirements-mcp.txt",
)

#: 被认识的**安装器 verb**（2026-10-10 加 uv）：这条尺子问的是"这条安装命令装齐了没有"，
#: 而"用哪个安装器装"是装配面的自由（P1-12 钉的是锁与"按锁安装"）。列在这里而不是继续
#: 靠 `pip install` 子串，是为了让"换一个尺子不认识的安装器"变成**红**而不是静默跳过 ——
#: 反面用例：把 CI 那条改成 `poetry install -r requirements.lock`，installer scope 必须红。
#: `uv pip install` 含 `pip install` 子串，写一条就够；分开写只是把"两个都算"讲明白。
_INSTALL_VERBS = ("pip install",)

LOCK_SURFACES: dict[str, tuple[str, ...]] = {
    "requirements/requirements.lock": (
        "requirements/requirements.txt",
        "requirements/requirements-api.txt",
        "requirements/requirements-rag.txt",
        "requirements/requirements-cloud.txt",
        "requirements/requirements-mcp.txt",
        "requirements/requirements-dev.txt",
    ),
    "requirements/requirements-runtime.lock": (
        "requirements/requirements.txt",
        "requirements/requirements-api.txt",
        "requirements/requirements-rag.txt",
        "requirements/requirements-cloud.txt",
        "requirements/requirements-mcp.txt",
    ),
}

def _package_names(lines: list[str]) -> set[str]:
    """requirements 风格的那些行 → 发行包名集合（注释与 extras/版本比较符都剥掉）。"""
    names: set[str] = set()
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(re.split(r"[<>=!\[:]", line, maxsplit=1)[0].strip().lower())
    return names

def _runtime_declared_names() -> set[str]:
    """那五份运行时 requirements 里点名装过的发行包。"""
    declared: set[str] = set()
    for name in RUNTIME_REQ_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        declared |= _package_names(path.read_text(encoding="utf-8", errors="ignore").splitlines())
    return declared

def check_pyproject() -> None:
    try:
        pp = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        out("pyproject.toml", False, str(exc))
        fails.append(f"pyproject.toml unreadable: {exc}")
        return
    proj = pp["project"]
    ok = proj["name"] == "rolecard-agent" and proj["requires-python"] == ">=3.13"
    detail = f"name={proj['name']} py={proj['requires-python']} deps={len(proj['dependencies'])}"
    out("pyproject.toml", ok, detail)
    if not ok:
        fails.append("pyproject.toml name / requires-python drifted")

    tool = pp.get("tool", {})
    ok = "ruff" in tool and "pytest" in tool
    out("pyproject tooling", ok, f"ruff={'ruff' in tool} pytest={'pytest' in tool}")
    if not ok:
        fails.append("pyproject.toml is missing ruff / pytest config")

def check_requirements_scope() -> None:
    """requirements/requirements.txt is the v1 kernel set. v2 deps must stay out of it."""
    lines = [
        line.split("#", 1)[0]
        for line in (ROOT / "requirements" / "requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    body = "\n".join(lines)
    leaked = [d for d in ("rapidocr", "opencv", "chromadb", "fastapi", "uvicorn") if d in body]
    out("requirements.txt scope", not leaked, f"leaked: {leaked}" if leaked else "v1 core only")
    if leaked:
        fails.append(f"requirements.txt pulls v2-only deps: {leaked}")

def _cmd_covers(req: str, cmd: str) -> bool:
    """这条 pip install 命令装没装 `req` 这一份：点名了它，或点了覆盖它的锁。

    锁时代（2026-10-07 拍板「上锁文件」）：一条 `pip install -r <锁>` 命令按 LOCK_SURFACES
    记它覆盖的镜像 —— 锁治"版本会漂"，"漏装一族"仍由安装面看着（LOCK_SURFACES 声明的
    覆盖面由 `lockfile parity` 对着真锁逐约束对账，这里只是消费那份声明）。
    """
    if req in cmd:
        return True
    return any(lock in cmd and req in surface for lock, surface in LOCK_SURFACES.items())

def check_installer_scope() -> None:
    """四个安装入口必须装**同一组运行时依赖**（09-28 轮 `R28-11`/`R28-12` 那一族的闸）。

    同一个坑这轮踩了三次，每次都是"某一条安装路径少装一族，而本机 .venv 恰好装过"：
      * 镜像少 `requirements-cloud.txt` —— CI 第一发真跑就红在"配任何 OpenAI 兼容端点保存即 500"，
        因为容器里没有 Ollama，云端 key 本来就是主用例；
      * `install.bat` 只装 base+api+rag，cloud 那一族是结尾一句 `echo` 提示；
      * README 快速开始第 2 步同病（决策 5 那轮补的）。
    少装一族的症状永远不在装的人自己身上（他的机器早就装过了），所以这条只能机器查。

    判据：`RUNTIME_REQ_FILES` 是"任何一条能跑起完整产品的路径都得有"的那五份，
    逐个入口现读它引用了哪些 `requirements*.txt`。dev / ocr / package **不在表内**：
    那几份按形态有意分开装（dev 不进生产运行树、OCR 栈必须独立 venv、package 只有打包机要）。

    **`requirements-mcp.txt` 于 2026-10-01 从"按形态分开装"挪进这张表**，理由不是口味而是两个
    出货形态都已经装了它：随包后端 09-29 起带它（`R28-34`：spec 缺一条就拒绝出产物），镜像
    10-01 起带它（`R28-53`：这一族病第三次红，前两次都是"本机绿、产物缺"）。既然发出去的两份
    都装着，"源装形态可以不装"就只是同一条产品能力的第三种拼法 —— 让它继续留在表外的代价，
    正是这条检查存在要防的那件事。

    另一半（同一轮 `R28-32` 加的）：**磁盘上每一份 requirements*.txt 都要在这两张表里之一**。
    新增一族却没人分类，症状与"少装一族"完全一样而方向相反 —— 它被某条路径默默需要着，
    却没有任何一处写着"这条路径装过它"。空理由不算理由，照样红。
    """
    # 判据读的是**每一条真跑 pip 的命令**，不是"文件里提过这个名字"。
    # 为什么不是全文压扁比一次（第一版就是这么写的，被一次变异当场否证）：Dockerfile 的
    # `COPY requirements.txt ... requirements-mcp.txt ./` 那一行**六个文件名都在**，而下一行
    # `RUN pip install` 只装四份 —— 全文比的话这一路永远绿，而那正是 `R28-53` 的缺陷本尊
    # （镜像里 MCP 永远 fail-open）。所以：从 `pip install` 那一行起，把行尾 `\` 的续行接上，
    # 只对**这一条命令**问它装齐了没有；一条文件里有几条就挨个问几条（ci.yml 有三条）。
    surfaces = {
        "install.bat": "install.bat",
        "Dockerfile": "Dockerfile",
        "ci.yml": ".github/workflows/ci.yml",
        "README": "README.md",
    }

    def _pip_commands(text: str) -> list[str]:
        """每条 `pip install` / `uv pip install` 命令，续行已接上（续行符 \\ 或 Windows 的 ^）。

        **注释行一律跳过**：Dockerfile 里"为什么装这一族"那段散文就写着 pip install 这几个字，
        把它当命令读，这条尺子会把自己的解释文字报成缺陷（第一趟就是这么红的）。

        2026-10-10 加 uv：CI 四臂改用 `uv pip install --system -r <锁>` 提速（冷装 pip 303.9s
        vs uv 27.8s，本机实测；锁与约束一行没动）。**判据从"必须含 `pip install` 字面"改成
        "含一个被认识的安装器 verb"** —— 前者当时确实是靠 `uv pip install` 里那个 `pip install`
        子串侥幸通过的，那等于"换了安装器还能绿"是靠巧合而不是靠设计；后者的反面才是这条
        尺子要的：装的东西变了没被认出来，必须红。
        """
        lines = text.splitlines()
        cmds: list[str] = []
        for i, line in enumerate(lines):
            if not any(v in line for v in _INSTALL_VERBS):
                continue
            probe = line.strip()
            if probe.startswith("#") or probe.startswith("//") or probe.lower().startswith(
                ("rem ", "::")
            ):
                continue
            buf = line
            j = i
            while buf.rstrip().endswith(("\\", "^")) and j + 1 < len(lines):
                j += 1
                buf += " " + lines[j]
            cmds.append(" ".join(buf.split()))
        # 只问"装一套依赖"的那些命令：`python -m pip install --upgrade pip` 提升级 pip 自己，
        # 它本来就不该带 -r，把它算进来等于给每条 CI job 都白造一条红。
        return [c for c in cmds if "-r requirements" in c]

    def _unrecognized(text: str) -> list[str]:
        """**装了依赖却没被认识的安装器 verb 认出来**的命令（2026-10-10 加 uv 时补的这一半）。

        为什么必须有：这条尺子按 `pip install` 子串收集命令，而"某一条命令换了安装器"时，
        这一条会被**静默跳过** —— 同一个文件里其余几条照旧被收集，于是 scope 公平照样绿。
        （正面例子：`uv pip install` 恰好含子串所以过得来；反面例子：`poetry install -r
        requirements.lock` 会被整个漏掉。靠子串活着的是巧合，不是判据。）
        判据：任何含 `-r requirements` 的行，若不匹配 `_INSTALL_VERBS` 里的任一 verb → 报出来。
        """
        out: list[str] = []
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "-r requirements" not in line:
                continue
            probe = line.strip()
            if probe.startswith("#") or probe.startswith("//") or probe.lower().startswith(
                ("rem ", "::")
            ):
                continue
            buf = line
            j = i
            while buf.rstrip().endswith(("\\", "^")) and j + 1 < len(lines):
                j += 1
                buf += " " + lines[j]
            cmd = " ".join(buf.split())
            if not any(v in cmd for v in _INSTALL_VERBS):
                out.append(cmd)
        return out

    missing: list[str] = []
    for label, rel in surfaces.items():
        path = ROOT / rel
        if not path.exists():
            missing.append(f"{label} 这个入口文件不见了（{rel}）")
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        unrecognized = _unrecognized(text)
        if unrecognized:
            missing.append(
                f"{label} 有装依赖的命令没被认识的安装器认出来（{unrecognized[0][:70]}…）—— "
                f"请把 verb 加进 _INSTALL_VERBS，否则这条命令装了什么没人查"
            )
        cmds = _pip_commands(text)
        if not cmds:
            missing.append(f"{label} 里找不到任何 pip install 命令")
            continue
        for cmd in cmds:
            missing += [
                f"{label} 有一条 pip install 没装 {req}（{cmd[:70]}…）"
                for req in RUNTIME_REQ_FILES
                if not _cmd_covers(req, cmd)
            ]
    out(
        "installer scope parity",
        not missing,
        "; ".join(missing[:4])
        if missing
        else f"{len(surfaces)} 个安装入口都覆盖 {len(RUNTIME_REQ_FILES)} 份运行时依赖",
    )
    if missing:
        fails.append(f"installer surfaces miss runtime deps: {missing}")

    # 另一半：磁盘上每一份 requirements*.txt 都要在两张表里之一（空理由不算理由）。
    # 键是相对仓库根的路径（2026-10-10 起这批文件住 requirements/ 子目录）。
    SEPARATE_BY_SHAPE = {
        "requirements/requirements-dev.txt":
            "开发/CI 依赖，不进生产运行树（含 PyInstaller 与 pip-tools）",
        "requirements/requirements-ocr.txt":
            "OCR 栈不进运行树，必须独立 venv（该文件开头有现行理由）",
        "requirements/requirements-package-ocr.txt":
            "只有打随包 OCR worker 时要（PyInstaller 装进 .venv-ocr，"
            "见 scripts/tools/build_ocr_worker.py；10-03 起装机版靠那份产物才有本地 OCR）",
    }
    unclassified = [
        p.relative_to(ROOT).as_posix()
        for p in sorted((ROOT / "requirements").glob("requirements*.txt"))
        if p.relative_to(ROOT).as_posix() not in RUNTIME_REQ_FILES
        and p.relative_to(ROOT).as_posix() not in SEPARATE_BY_SHAPE
    ]
    blank = [name for name, why in SEPARATE_BY_SHAPE.items() if not why.strip()]
    ok_class = not unclassified and not blank
    out(
        "requirements 分类",
        ok_class,
        "; ".join([f"没分类：{unclassified}", f"空理由：{blank}"][:2])
        if not ok_class
        else f"{len(RUNTIME_REQ_FILES)} 份运行时 + {len(SEPARATE_BY_SHAPE)} 份按形态分开，都有理由",
    )
    if not ok_class:
        fails.append(f"requirements files missing a classification: {unclassified or blank}")

_家_RE = re.compile(r"-r (?:requirements/)?requirements(?:-([a-z_]+))?\.txt")

def _installed_families(text: str, *, where: str) -> set[str]:
    """这条安装路径**实际**装了哪几族（`-r requirements-<族>.txt`）。

    与上面那条尺子同一个纪律：只认**真跑 pip 的那条命令**，跳过注释、接上行尾续行 ——
    否则 Dockerfile 的散文（解释为什么要装这一族）会被当成命令，尺子把自己的说明
    报成缺陷。

    `-r requirements.txt`（base 那一族，没有 `-<族>` 后缀）归到族名 `txt`：第一趟跑的时候
    尺子把它整个漏掉，于是"容器少装 txt"这条假红 —— 而 base 恰恰是每一份形态都装的那族，
    漏了它等于矩阵里永远少一格。
    """
    cmds: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "pip install" not in line:
            continue
        probe = line.strip()
        if probe.startswith(("#", "//")) or probe.lower().startswith(("rem ", "::")):
            continue  # 注释行：Dockerfile 的"为什么装"那几段里就有 pip install 这串字
        buf = line
        j = i
        while buf.rstrip().endswith(("\\", "^")) and j + 1 < len(lines):
            j += 1
            buf += " " + lines[j]
        cmds.append(buf)
    families: set[str] = set()
    for cmd in cmds:
        families |= {(m or "txt") for m in _家_RE.findall(cmd)}
        # 锁时代：`-r <锁>` 命令按 LOCK_SURFACES 记它覆盖的族（族名与 `-r` 镜像同名提取，
        # requirements.txt → "txt" 的口径不变）。
        for lock, surface in LOCK_SURFACES.items():
            if lock in cmd:
                families |= {_family_of(fname) for fname in surface}
    if not families:
        raise ValueError(f"{where} 里找不到任何 `-r requirements-*.txt`（判据在空转）")
    return families

def _family_of(filename: str) -> str:
    """requirements 镜像文件名 → 族名（与 `_家_RE` 的提取口径一致：requirements.txt → "txt"）。

    接受裸名与 `requirements/` 前缀两种形状（LOCK_SURFACES 的值自 2026-10-10 起带前缀）。
    """
    m = re.search(r"(?:^|/)requirements(?:-([a-z_]+))?\.txt$", filename)
    if not m:
        raise ValueError(f"{filename} 不是 requirements 镜像文件的形状")
    return m.group(1) or "txt"

def _lock_pins(text: str) -> dict[str, str]:
    """锁文件 → {归一化包名: pin 版本}。`name==ver ; marker` 行才收；注释与 `--` 选项跳过。

    名字只留 `[]` 前的基名并按 PEP 503 归一：锁里 extras 会跟着 pin 走
    （`uvicorn[standard]==0.52.2`），镜像那侧 `Requirement.name` 本就不含 extras ——
    对账的键必须同侧剥干净，否则带 extras 的包永远"缺"。
    """
    pins: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        body = stripped.split(";", 1)[0].strip()  # 环境标记不参与对账
        if "==" not in body:
            continue
        name, _, version = body.partition("==")
        base = name.split("[", 1)[0].strip()
        pins[re.sub(r"[-_.]+", "-", base.lower())] = version.strip()
    return pins

def check_lockfile_parity() -> None:
    """每把锁必须覆盖 LOCK_SURFACES 声明的镜像面：镜像的每条约束 ∈ 锁的 pin，缺包即红。

    为什么需要（拍板「上锁文件」的另一半）：锁治"版本会漂"，可 requirements*.txt 改了
    而忘了重新 compile，CI/镜像/打包装的仍是旧世界 —— 那种漂是**静默**的（pip 不会提醒
    你锁旧了）。判据按**约束区间**比：`pydantic>=2.13` 只要求锁里 pin 的版本落进区间，
    不是逐字相等 —— 镜像管"要什么"，锁管"装什么"，两边的关系是满足而不是相同
    （逐字相等只会把"锁比镜像新了一个补丁版"这种健康状态也打成红）。

    另外两格：锁文件整个不见也红（入库产物不许静默消失）；锁头部 provenance（pip-compile
    写下的生成命令）必须包含声明的每一份输入 —— 防止有人手改覆盖面、让 LOCK_SURFACES
    说一套锁实际又是另一套。
    """
    problems: list[str] = []
    for lock_name, surface in LOCK_SURFACES.items():
        lock_path = ROOT / lock_name
        if not lock_path.exists():
            problems.append(f"{lock_name} 不见了（入库产物，pip-compile 重新生成）")
            continue
        lock_text = lock_path.read_text(encoding="utf-8", errors="ignore")
        header = "\n".join(line for line in lock_text.splitlines() if line.startswith("#"))
        pins = _lock_pins(lock_text)
        if not pins:
            problems.append(f"{lock_name} 里一个 pin 都读不到（空锁或形状变了）")
            continue
        for mirror in surface:
            mirror_path = ROOT / mirror
            if not mirror_path.exists():
                problems.append(f"{lock_name} 声明覆盖 {mirror}，那份文件不见了")
                continue
            if mirror not in header:
                problems.append(
                    f"{lock_name} 的生成命令里没有 {mirror}（头部 provenance 与 LOCK_SURFACES"
                    "不符 —— 覆盖面被手改过？重新 pip-compile）"
                )
                continue
            mirror_text = mirror_path.read_text(encoding="utf-8", errors="ignore")
            for raw in mirror_text.splitlines():
                text = raw.split("#", 1)[0].strip()
                if not text or text.startswith("-"):
                    continue
                try:
                    req = Requirement(text)
                except InvalidRequirement:
                    problems.append(f"{mirror} 有一行解析不成约束：{raw.strip()!r}")
                    continue
                name = re.sub(r"[-_.]+", "-", req.name.lower())
                pin = pins.get(name)
                if pin is None:
                    problems.append(f"{lock_name} 缺 {req.name}（{mirror} 声明了它）")
                    continue
                if not req.specifier.contains(pin, prereleases=True):
                    problems.append(
                        f"{req.name}：{mirror} 要 {req.specifier}，{lock_name} pin 的是 {pin}"
                        " —— 镜像改了没重新 compile"
                    )
    out(
        "lockfile parity",
        not problems,
        "; ".join(problems[:4])
        if problems
        else f"{len(LOCK_SURFACES)} 把锁的 pin 全部落进声明镜像的约束区间"
        f"（{sum(len(s) for s in LOCK_SURFACES.values())} 份输入对过账）",
    )
    if problems:
        fails.append(f"lockfile parity drift: {problems}")

def check_lock_platform_markers() -> None:
    """锁里的**平台标记**：Windows 上解析出的锁，Linux 也装得上吗（2026-10-08 实撞立案）。

    病根（实测，非推测）：两把锁由 `pip-compile` 在 Windows 解析（P1-12 拍板「Windows-first，
    锁在生成机平台解析」），而 pip-compile 把**传递依赖**写进锁时丢掉上游的环境标记。于是锁里
    躺着一条裸的 `pywin32==312`，Linux 侧（CI 门禁臂、镜像、发布链）`pip install -r` 在解析期
    就退 1 —— `No matching distribution found for pywin32==312 (from versions: none)`。
    当天首次 push 才照出来：锁是 10-07 落的，而 Linux 臂此前从没装过这把锁。

    判据读的是**已安装分包的元数据**（谁在什么标记下声明了谁），不是一张手抄包名名单 ——
    名单朝两个方向烂：上游改成跨平台而名单还钉着，就把那一侧该装的钉没了；上游新增一个
    Windows-only 二进制依赖而名单没它，Linux 又炸。取数与补钉的实现在 `platform_pins`。

    只判**会崩**那一族（`CRASHES_ELSEWHERE`，按 wheel 可得性实测得出）：`pefile` /
    `pywin32-ctypes` / `colorama` / `tzdata` 同样是 Windows-only 声明，但 Linux 装得上，
    漏钉只是多装一个用不上的东西。给它们顺手钉标记会**删掉 Linux 现在实际拿到的东西**
    （`tzdata` 还兜着 slim 镜像的 `zoneinfo`），那是没量过的环境改动，不归这一刀 ——
    与本仓「测量否决了修法就照实记、不在修 bug 的那把刀里顺手改运行环境」同一条纪律。

    第四格（warn 不红）：某个 pin 声明了"另一侧才装"的依赖而锁里整条没有它 —— 本轮量到的
    `uvicorn[standard]` → `uvloop` 就是这一形状。它不能判红，因为**Windows 解析根本不产出这一行**
    （实测：把这行带标记写进输入 requirements，重新 compile 之后**整条消失**，连标记都不留），
    判红等于要求一条这台机器造不出来的行；手工补进去又会被周更锁静默剥掉。所以它如实出声、
    由 ENGI-18 立案（修法在解析侧：要么跨平台合并两把锁，要么镜像自己声明带标记的 uvloop ——
    而后者对 `uvicorn[standard]` 的 extras 门无效，已实测）。

    变异实测见 `tests/unit/test_lock_platform_markers.py`（三臂：剥掉标记→点名那行；钉反方向
    →红；上游无条件声明却带标记→红），不靠"看起来对"。
    """
    edges = installed_edges()
    problems: list[str] = []
    notes: list[str] = []
    # ENGI-18 第三路（2026-10-09 拍板）：约束面是第四格（warn）的第二个读数面。文件**必须在** ——
    # Linux 装配面（ci.yml gate/full-gate、Dockerfile）的 `-c constraints-linux.txt` 引用着它，
    # 删文件不删引用，下一次 CI 装配当场红；删干净（文件+引用+本判据）才算回到"没锁版本"的旧世界，
    # 那时第四格的 noise 会自己回来。
    constraints_path = ROOT / "config" / "constraints-linux.txt"
    covered: set[str] = set()
    if constraints_path.exists():
        covered = {
            p.name
            for p in parse_lock(constraints_path.read_text(encoding="utf-8", errors="ignore"))
        }
    else:
        problems.append(
            "constraints-linux.txt 不见了 —— Linux 装配面的 `-c` 还引用着它"
            "（要删必须连装配面与本判据一起改，否则镜像/CI 装配当场红）"
        )
    for lock_name in LOCK_SURFACES:
        path = ROOT / lock_name
        if not path.exists():
            problems.append(f"{lock_name} 不见了")
            continue
        p, n = lock_findings(
            path.read_text(encoding="utf-8", errors="ignore"), edges, lock_name,
            extra_pins=covered,
        )
        problems.extend(p)
        notes.extend(n)
    ok = not problems
    pinned = [
        pin.name
        for lock_name in LOCK_SURFACES
        if (ROOT / lock_name).exists()
        for pin in parse_lock((ROOT / lock_name).read_text(encoding="utf-8", errors="ignore"))
        if pin.marker
    ]
    detail = "; ".join(problems[:4]) if problems else (
        f"{len(LOCK_SURFACES)} 把锁的平台标记都判过（会崩那族 {sorted(CRASHES_ELSEWHERE)}；"
        f"带标记的行 {len(pinned)} 条"
        + (f"；constraints 兜住 {sorted(covered)}" if covered else "")
        + "）"
    )
    out("lock platform markers", ok, detail)
    if problems:
        fails.append(f"lock platform markers: {problems}")
    for note in notes:
        warns.append(f"lock platform markers (仅提示，不拦): {note}")
        print(f"WARN lock platform markers :: {note}")


def _runtime_form_marker() -> str:
    """形态自报标记的**唯一出处**是 `base/paths.py` 的常量，所以这里 import 它，不抄字面量。

    抄一份的代价是具体的：哪天常量改了名，这条尺子会继续盯着 Dockerfile 里那个旧名字报"在"，
    而真正读它的代码已经改口 —— 守着一个没人读的字符串，比没有这条尺子更坏（判据与事实面
    必须是同一个，同 `check_installer_scope` 那条"只认真跑 pip 的命令"）。
    """
    sys.path.insert(0, str(ROOT / "src"))
    from rolecard_agent.base.paths import RUNTIME_FORM_ENV

    return RUNTIME_FORM_ENV

def _job_block(text: str, job: str) -> str | None:
    """ci.yml 里一个 job 的整块（`  <job>:` 起，到下一个同级键为止）；找不到返回 None。

    **刻意不引 PyYAML** —— 矩阵文件头写着的同一条理由：YAML 库不在任何 requirements 里，
    本机绿 CI 炸就是这么来的。ci.yml 的缩进是稳定的：`jobs:` 下每个 job 键恰好 2 空格，
    job 体内至少 4 空格，所以"下一个 2 空格的 `键:`"就是块边界；扫描从 `jobs:` 那行开始
    （`on:` 的子键也是 2 空格，先到先切会切错块）。
    """
    lines = text.splitlines()
    jobs_at = next(
        (i for i, ln in enumerate(lines) if re.match(r"^jobs:\s*$", ln)), None
    )
    if jobs_at is None:
        return None
    key = re.compile(rf"^  {re.escape(job)}:\s*(?:#.*)?$")
    start = next((i for i in range(jobs_at + 1, len(lines)) if key.match(lines[i])), None)
    if start is None:
        return None
    sibling = re.compile(r"^  [A-Za-z0-9_-]+:")
    for j in range(start + 1, len(lines)):
        if sibling.match(lines[j]):
            return "\n".join(lines[start:j])
    return "\n".join(lines[start:])

def check_capability_matrix() -> None:
    """形态 × 能力 × 依赖出处：镜像里有什么、缺什么，由**一份矩阵**说了算。

    审查快照「容器形态静默缺本地 OCR」那一格（决策七）：镜像没有 ocr 那一族，于是容器里
    的 OCR 只能走云端兜底 —— 但"缺"这件事从前**没有任何一处写着**，parity 那条守卫也
    因此查不出（它只问"五个入口装齐运行时五族了吗"，而 ocr 是按形态分开装的那一族）。
    现在矩阵把每个形态声明的能力写成布尔，`能力 = 它依赖的那些族是否都装上了`，于是
    "镜像没有本地 OCR"从静默变成**声明过的决定**；谁往镜像里加一族，这条断言会立刻
    逼他回来更新矩阵（反之亦然 —— 矩阵写了 true 而实际没装，这也红）。

    为什么矩阵是 JSON 而不是 YAML：PyYAML 不在任何 requirements 里、scripts/ 也没有
    一处 import 它，用 YAML 会让本机（.venv 恰好有）绿而 CI（按 requirements 装）在
    import 那一行炸 —— 正是本仓反复挨打的形状。矩阵要的是机器可读，不是某个格式。

    覆盖声明：三个形态**全部**接上机器检查（2026-10-07 收掉"等下一刀"那笔欠）——
    每个形态的矩阵条目带一个机器可读的 `检查点`（文件 + 可选 job 名），尺子按它去
    对应位置现读 pip install：container 对整个 Dockerfile，dev/installer 对 ci.yml 里
    **具名 job 的块**（`_job_block`，纯缩进规则不引 PyYAML）。能力层也从只有容器
    扩成三列逐格比（`installer`/`dev` 两个布尔从前只是矩阵里的数据，现在各自对着
    自己形态的实际安装问"有/没有"）。`检查点` 缺失或 job 不在文件里都判红 ——
    新增形态却不接线，这条路从此走不通。
    """
    path = ROOT / "config" / "capability-matrix.json"
    if not path.exists():
        out("capability matrix", False, "矩阵文件不见了（capability-matrix.json）")
        fails.append("capability-matrix.json is missing")
        return
    try:
        matrix = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        out("capability matrix", False, f"矩阵读不出来：{exc}")
        fails.append(f"capability-matrix.json is unreadable: {exc}")
        return

    problems: list[str] = []
    families = matrix.get("families") or {}
    forms = matrix.get("forms") or {}
    caps = matrix.get("capabilities") or {}

    # 1) 矩阵自检：每一族都得有对应文件；每一形态都得有 requires、来源与**检查点**。
    #    检查点缺失 = 矩阵声明了却没人机器查（白声明）—— 这条正是把"installer/dev 两列
    #    是纯数据"接成机器检查后留下的反向闸：新增形态却不接线，走不通。
    for family, filename in sorted(families.items()):
        if not (ROOT / filename).exists():
            problems.append(f"矩阵里的族 {family} 指向 {filename}，磁盘上没有这份文件")
    for form, spec in sorted(forms.items()):
        if not (spec.get("requires") or []):
            problems.append(f"形态 {form} 没写 requires（空矩阵等于没有矩阵）")
        if not (spec.get("来源") or "").strip():
            problems.append(f"形态 {form} 没写来源（谁装出来的？）")
        spot = spec.get("检查点") or {}
        if not (spot.get("文件") or "").strip():
            problems.append(f"形态 {form} 没写检查点的文件（矩阵声明了却没人机器查）")

    # 2) 每个形态按自己的检查点现读**实际**装了什么，与 requires 逐族比。
    #    container 对整个 Dockerfile；dev / installer 对 ci.yml 里具名 job 的块
    #    （`_job_block`：纯缩进规则，不引 PyYAML —— 理由在矩阵文件头）。
    installed_by_form: dict[str, set[str]] = {}
    for form, spec in sorted(forms.items()):
        spot = spec.get("检查点") or {}
        rel = (spot.get("文件") or "").strip()
        if not rel:
            continue  # 上面已判红
        path = ROOT / rel
        if not path.exists():
            problems.append(f"形态 {form} 的检查点 {rel} 不见了")
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        job = (spot.get("job") or "").strip()
        where = f"{form} 的检查点 {rel}" + (f" 的 {job} job" if job else "")
        if job:
            block = _job_block(text, job)
            if block is None:
                problems.append(f"{where} 里找不到这个 job（矩阵漂了）")
                continue
            text = block
        try:
            installed = _installed_families(text, where=where)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        installed_by_form[form] = installed
        declared = set(spec.get("requires") or [])
        if installed != declared:
            problems.append(
                f"形态 {form} 实际装 {sorted(installed)}，矩阵声明 {sorted(declared)}"
                f"（多出来的：{sorted(installed - declared)}；"
                f"缺的：{sorted(declared - installed)}）"
            )

    # 2b) 形态自报那一行也得在：`rag/ocr.py` 的容器文案照它分岔。删了不会有谁当场炸，
    # 只会让容器里的人重新拿到"装 .venv-ocr / 重打这一包"这种镜像里做不到的建议 ——
    # 正是这一格当初"静默"的形状，所以它归这条尺子管。（container 专属，与安装清单无关。）
    dockerfile = ROOT / "Dockerfile"
    if dockerfile.exists():
        text = dockerfile.read_text(encoding="utf-8", errors="ignore")
        marker = _runtime_form_marker()
        # 值的边界要看死：`=container-MUTATED` 也含 `=container` 这个子串，用 `in` 判会绿 ——
        # 变异实测第一趟就是这么漏过去的，而值写坏的后果是形态判据运行期根本不成立
        # （容器里照旧拿到"装 .venv-ocr"那句），正是这一格要防的"静默"。
        if not re.search(rf"(?m)^\s*ENV\s+.*\b{re.escape(marker)}=container(?=[\s\\]|$)", text):
            problems.append(
                f"Dockerfile 里找不到 `ENV {marker}=container`"
                "（形态自报，值必须正好是 container）；"
                "没有它，容器里的 OCR 指引会退回开发态/装机版那两句"
            )

    # 3) 能力层三列全覆盖：一个能力在某形态"有" ⇔ 它依赖的族都在该形态实际装上，
    #    且矩阵在**这一列**写着的那个布尔必须逐格相等。从前只有 container 列被这样问过，
    #    installer / dev 两个布尔只是数据 —— 现在它们各自对着自己的检查点。
    for cap, spec in sorted(caps.items()):
        need = set(spec.get("families") or [])
        unknown = need - set(families)
        if unknown:
            problems.append(f"能力 {cap} 依赖未登记的族 {sorted(unknown)}")
            continue
        for form in sorted(forms):
            if form not in spec:
                problems.append(f"能力 {cap} 没写形态 {form}（矩阵必须逐列齐全）")
                continue
            if form not in installed_by_form:
                continue  # 该形态的检查点坏了，上面已判红；对它无从判断能力
            has = need <= installed_by_form[form]
            said = bool(spec.get(form))
            if has != said:
                problems.append(
                    f"能力 {cap}：{form} 形态里实际{'有' if has else '没有'}"
                    f"（依赖 {sorted(need)}），矩阵却写着 {said}"
                )

    ok = not problems
    out(
        "capability matrix",
        ok,
        "; ".join(problems[:3])
        if problems
        else (
            f"{len(forms)} 个形态（各按检查点现读）× {len(caps)} 项能力三列逐格：一致"
            f"（容器本地 OCR 声明为缺、走云端兜底）"
        ),
    )
    if problems:
        fails.append(f"capability matrix out of sync: {problems}")

def _package_constraints(lines: list[str]) -> dict[str, str]:
    """requirements 风格的行 → {发行包名: 版本约束原文}（"" = 无约束）。

    2026-10-04 审查快照的升级：此前 parity 只比**包名集合**，版本约束被
    `re.split` 剥掉 —— 同一包两处钉版不一致（langgraph-checkpoint-sqlite 一处
    `==3.1.1`、一处裸奔）就这样绿着进了仓库。约束逐字比对才是"镜像"的完整语义。
    """
    pins: dict[str, str] = {}
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"([^<>=!\[:]+)\s*(.*)", line)
        base = m.group(1).strip().lower() if m else line.lower()
        rest = m.group(2).strip() if m else ""
        pins[base] = rest
    return pins

def check_dependency_parity() -> None:
    """pyproject.toml is the single source of truth; requirements*.txt mirror it.

    Drift between the two is silent: it only shows up for whoever installs the *other*
    way. That is precisely the class of mistake a weaker model introduces, so it gets an
    assertion rather than a convention.

    2026-10-04（审查快照）：比对从"包名集合"升级为"包名 → 约束"全字典 ——
    名字集合相等而约束漂移（`==3.1.1` vs 裸名）从此必红。
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def _diff(label: str, py_lines: list[str], req_file: str) -> None:
        py_pins = _package_constraints(py_lines)
        req_pins = _package_constraints(
            (ROOT / req_file).read_text(encoding="utf-8").splitlines()
        )
        drift = {
            name: {"pyproject": py_pins.get(name), "requirements": req_pins.get(name)}
            for name in sorted(set(py_pins) | set(req_pins))
            if py_pins.get(name) != req_pins.get(name)
        }
        ok = not drift
        detail = "in sync" if ok else "; ".join(
            f"{n}: {d['pyproject']!r} vs {d['requirements']!r}" for n, d in drift.items()
        )
        out(label, ok, detail)
        if not ok:
            fails.append(f"pyproject.toml / {req_file} drift: {detail}")

    _diff(
        "dependency parity",
        list(pyproject["project"]["dependencies"]),
        "requirements/requirements.txt",
    )

    # The extras map to their own requirement files. Without this the api / rag / dev
    # mirrors can drift unnoticed - the earlier version of this check covered only the base
    # set, which is precisely how a mirror silently becomes wrong. Since 2026-10-04 the
    # comparison is per-constraint, not name-sets (2026-10-04 审查快照).
    extras = pyproject["project"].get("optional-dependencies", {})
    for extra, filename in (
        ("api", "requirements/requirements-api.txt"),
        ("rag", "requirements/requirements-rag.txt"),
        ("cloud", "requirements/requirements-cloud.txt"),
        ("dev", "requirements/requirements-dev.txt"),
    ):
        if extra not in extras:
            continue
        _diff(f"extra parity: {extra}", list(extras[extra]), filename)

#: spec 里的模块名（下划线）与发行包名（连字符）之间那点形状差。PEP 503 的归一只到
#: "下划线/连字符等价"，这里就照那一条来，别写第二套映射表。
def _module_to_distribution(module: str) -> str:
    return module.lower().replace("_", "-")

def _spec_runtime_packages() -> list[str]:
    """spec 的 `RUNTIME_PACKAGES` —— **读法只有一个出处**：`scripts/check_bundle_parity.py`。

    这条尺子与那条尺子问的是同一份清单（一条对 requirements，一条对 src 的懒加载），
    AST 解析各写一遍就会有一边先漂。本脚本与它同在 `scripts/` 下，`python scripts/x.py`
    时该目录已在 `sys.path[0]`，所以直接 import。
    """
    from check_bundle_parity import spec_runtime_packages  # noqa: PLC0415 - 同目录的尺子

    return spec_runtime_packages()

def check_spec_runtime_vs_requirements() -> None:
    """随包后端要收的每一族**模块**，都必须在那五份运行时 requirements 里有**声明出处**。

    为什么单独立一条（10-01，打包链台账 R28-57）：这条链上已经有两张表 —— spec 的
    `RUNTIME_PACKAGES`（模块名，决定"打进包的是哪些族"）与 `RUNTIME_REQ_FILES`（发行名，
    决定"装的时候装什么"），而**没有任何一处对读它们**。MCP 那半年的形状就是这么来的：
    spec 从 09-29 起要求 `mcp` 这个模块，`pip` 那侧却只写了 `langchain-mcp-adapters`，
    `mcp` 全靠传递依赖带进来 —— 于是 Dockerfile 少一份 requirements 时，装完照样"成功"，
    而包里的模块数是零（`R28-53`）。这与 `R28-11`（httpx 靠传递依赖兜住）是同一条病，
    只是这次躲在打包侧。

    判据方向只查一边：spec 要的每族模块必须在运行时那几份里被点名。反方向不查 ——
    requirements 里多一条不代表 spec 就该收它（`pyinstaller`、`pytest` 那些本来就不进包）。
    """
    packages = _spec_runtime_packages()
    if not packages:
        out(
            "spec runtime deps declared",
            False,
            "没从 packaging/rolecard-backend.spec 里读出 RUNTIME_PACKAGES —— 这条尺子自己看不见了",
        )
        fails.append("spec RUNTIME_PACKAGES unreadable")
        return
    declared = _runtime_declared_names()
    missing = [pkg for pkg in packages if _module_to_distribution(pkg) not in declared]
    declared_pairs = [_module_to_distribution(m) for m in missing]
    detail = (
        f"{len(packages)} 族模块全部在 {len(RUNTIME_REQ_FILES)} 份运行时 requirements 里有声明"
        if not missing
        else f"spec 要收却没人声明的模块：{missing}（发行名 {declared_pairs}）"
    )
    out("spec runtime deps declared", not missing, detail)
    if missing:
        fails.append(f"spec RUNTIME_PACKAGES not declared in requirements: {missing}")
