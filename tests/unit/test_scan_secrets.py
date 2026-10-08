"""密钥扫描的 fail-closed 出口与扫描范围（`scripts/tools/scan_secrets.py`）。

测试**不出网、不装二进制、不依赖 gitleaks**：量的是两条纪律本身 ——「各种跑不成的形状都得
退 2」（与依赖审计同一条：扫不成 ≠ 干净）与「范围由 git 名单决定」。真装通一次的读数记在
提交信息里，不挂进用例：那会造出一个"别人的机器跑不了"的测试。

2026-10-09 补的每一格都是一次实测换来的：
  * gitleaks 吊死：从前 `subprocess.run` 没有超时，吊着整条 CI job 到顶穿被杀，而 GitHub
    对 cancelled job 不传日志（现场三趟全这么丢的）。
  * 工具起不来：只兜 `TimeoutExpired` 的那一版，`OSError` 会让脚本抛 traceback —— 这条线的
    退出码是判据（0 干净 / 1 发现 / 2 扫不成），traceback 等于新造第四种。
  * 范围：从前 `--no-git` 直扫工作树（盘上全扫、**不看 .gitignore**，脚本注释写着"尊重
    .gitignore"是假的），仓内 `.venv` 的第三方夹具刷出 23 条假阳；想用目录黑名单挡，实测
    两种写法都连带命中 tracked 的 `shell/build/icon.ico` —— 过度匹配就是把该扫的东西静默
    免检。现在扫由 git 名单圈出的影子树。
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "tools" / "scan_secrets.py"


def _load():
    spec = importlib.util.spec_from_file_location("scan_secrets_under_test", str(_SCRIPT))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scan_secrets_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class Proc:
    """顶替 `subprocess.CompletedProcess` 的最小形状。"""

    def __init__(self, code: int, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = code, out, err


def _fake_repo(tmp_path: Path) -> Path:
    (tmp_path / ".gitleaks.toml").write_text("[allowlist]\n", encoding="utf-8")
    return tmp_path


def _stub_pipeline(
    scan,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: object,
    files: tuple[str, ...] = ("a.py", "pkg/b.py"),
) -> Path:
    """把"名单 + 影子树"两格换成替身，只留被测的那一段。

    必须一起换：不换成 `_tracked_files` 的替身，测试里那发 `subprocess.run` 替身会**先接住
    git 那一发**，于是"范围定不下来"的路径被误当成被测对象 —— 三条退码用例全绿着测错东西。
    """
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(scan, "_tool_path", lambda: repo / "gitleaks")
    monkeypatch.setattr(scan, "_tracked_files", lambda _r: list(files))
    shadow = tmp_path / "shadow"
    shadow.mkdir(exist_ok=True)
    monkeypatch.setattr(scan, "_shadow_tree", lambda _r, _f: shadow)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_k: object) -> object:
        if isinstance(result, BaseException):
            if cmd[0].endswith("gitleaks") or cmd[0].endswith("gitleaks.exe"):
                raise result
            return Proc(0, "a.py\npkg/b.py\n")
        calls.append(list(cmd))
        return result

    monkeypatch.setattr(scan.subprocess, "run", fake_run)
    return shadow


# --------------------------------------------------------------------------- fail-closed


def test_缺配置文件按红退2(tmp_path: Path) -> None:
    scan = _load()
    assert scan.main(["--repo", str(tmp_path)]) == 2  # 没有 .gitleaks.toml


def test_装不到工具按红退2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scan = _load()
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(scan, "_tool_path", lambda: None)
    monkeypatch.setattr(scan, "_install_to", lambda _c: None)
    assert scan.main(["--repo", str(repo)]) == 2


def test_挂死变一次干净的红而不是无限等(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """gitleaks 吊着不动：300s 到点必须退 2，而不是 traceback、更不是无限等。"""
    scan = _load()
    _stub_pipeline(
        scan,
        monkeypatch,
        tmp_path,
        subprocess.TimeoutExpired(cmd="gitleaks", timeout=300),
    )
    assert scan.main(["--repo", str(tmp_path)]) == 2
    assert "300s" in capsys.readouterr().err


def test_工具起不来也走退2而不是抛traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """退码是判据（0/1/2），traceback 那条形状等于新造第四种 —— 只兜超时不够。"""
    scan = _load()
    _stub_pipeline(scan, monkeypatch, tmp_path, OSError("[Errno 8]  exec format error"))
    assert scan.main(["--repo", str(tmp_path)]) == 2
    assert "起不来" in capsys.readouterr().err


def test_真有发现退1_没有发现退0_其它码按扫不成(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan = _load()
    for code, want in ((0, 0), (1, 1), (137, 2)):
        # 循环变量要当下绑住（本仓老写法）：直接 `lambda: Proc(code)` 会被 ruff B023 拦住 ——
        # 三格共享最后一个 code 时测的就不是三格了。
        _stub_pipeline(scan, monkeypatch, tmp_path, (lambda c: Proc(c))(code))
        assert scan.main(["--repo", str(tmp_path)]) == want, f"gitleaks 退 {code} 该映射成 {want}"


# --------------------------------------------------------------------------- 扫描范围


def test_名单由_git_现答而不是抄一份规则(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_tracked_files` 的命令行就是"范围"的全部定义：tracked + 未忽略的未跟踪。

    少了 `--others --exclude-standard` 就漏掉本次要发但还没入库的文件（那种文件恰恰最可能
    带刚贴进去的 key）；少了 `--exclude-standard` 就把 .gitignore 的东西扫进来，退回上一版
    那个"盘上全扫"的形状。
    """
    scan = _load()
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_k: object) -> Proc:
        seen.append(list(cmd))
        return Proc(0, "a.py\npkg/b.py\n")

    monkeypatch.setattr(scan.subprocess, "run", fake_run)
    assert scan._tracked_files(tmp_path) == ["a.py", "pkg/b.py"]
    joined = " ".join(seen[0])
    for flag in ("ls-files", "--cached", "--others", "--exclude-standard"):
        assert flag in joined, f"范围的定义少了 {flag}：{joined}"


def test_定不出范围按扫不成而不是当成干净(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scan = _load()
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(scan, "_tool_path", lambda: repo / "gitleaks")
    monkeypatch.setattr(
        scan, "_tracked_files", lambda _r: (_ for _ in ()).throw(RuntimeError("没装 git"))
    )
    assert scan.main(["--repo", str(repo)]) == 2
    assert "定不出扫描范围" in capsys.readouterr().err


def test_空名单也按扫不成(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """一份"什么都没跟踪"的仓库不是干净的仓库 —— 是范围没定下来。"""
    scan = _load()
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(scan, "_tool_path", lambda: repo / "gitleaks")
    monkeypatch.setattr(scan, "_tracked_files", lambda _r: [])
    assert scan.main(["--repo", str(repo)]) == 2
    assert "空的" in capsys.readouterr().err


def test_扫的是影子树不是工作树(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--source` 必须落在影子树上，且带着 `--no-git`：这两件一起才是"只扫要发的那一份"。"""
    scan = _load()
    calls: list[list[str]] = []
    shadow = _stub_pipeline(scan, monkeypatch, tmp_path, Proc(0))
    real_run = scan.subprocess.run

    def spy(cmd: list[str], **_k: object) -> object:
        calls.append(list(cmd))
        return real_run(cmd, **_k)

    monkeypatch.setattr(scan.subprocess, "run", spy)
    assert scan.main(["--repo", str(tmp_path)]) == 0
    detect = calls[0]
    assert detect[detect.index("--source") + 1] == str(shadow), "扫的还是工作树"
    assert "--no-git" in detect


def test_影子树按名单建且保留结构(tmp_path: Path) -> None:
    scan = _load()
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "a.py").write_text("A", encoding="utf-8")
    (repo / "pkg" / "b.py").write_text("B", encoding="utf-8")
    (repo / "不在名单里.py").write_text("NO", encoding="utf-8")
    root = scan._shadow_tree(repo, ["a.py", "pkg/b.py", "ghost.py"])
    try:
        assert (root / "a.py").read_text(encoding="utf-8") == "A"
        assert (root / "pkg" / "b.py").read_text(encoding="utf-8") == "B"
        assert not (root / "不在名单里.py").exists(), "名单外的东西不该进范围"
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def test_拷不动的文件不许被静默跳过(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """名单里有一个文件没进影子树 = 那份源码**免检**，比假阳危险。

    `shutil.copy2` 抛错（权限/磁盘）时从前是 traceback 一路冲出 `main()`：门禁确实会红，
    但那份建到一半的影子树（含要发布源码的副本）留在 %TEMP% 里等系统回收。现在两条都不留。
    """
    import shutil

    scan = _load()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("A", encoding="utf-8")
    real_copy = shutil.copy2

    def boom(src: object, dst: object, **_k: object) -> None:
        raise PermissionError("假装的权限问题")

    monkeypatch.setattr(scan.shutil, "copy2", boom)
    with pytest.raises(PermissionError):
        scan._shadow_tree(repo, ["a.py"])
    # 建到一半的那份必须被收掉，不留源码副本。
    leftovers = [p for p in Path(__import__("tempfile").gettempdir()).glob("gitleaks-scope-*")]
    assert not any((p / "a.py").exists() for p in leftovers), "失败的影子树没被清干净"
    monkeypatch.setattr(scan.shutil, "copy2", real_copy)


def test_扫完把影子树收掉(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """影子树里是**要发布的源码副本**，留在 %TEMP% 等回收等于这条红线自己泄一份副本。"""
    scan = _load()
    made: list[Path] = []
    real_mkdtemp = Path

    def make_shadow(_repo: Path, _files: list[str]) -> Path:
        root = Path(__import__("tempfile").mkdtemp(prefix="gitleaks-scope-"))
        (root / "a.py").write_text("A", encoding="utf-8")
        made.append(root)
        return root

    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(scan, "_tool_path", lambda: repo / "gitleaks")
    monkeypatch.setattr(scan, "_tracked_files", lambda _r: ["a.py"])
    monkeypatch.setattr(scan, "_shadow_tree", make_shadow)
    monkeypatch.setattr(scan.subprocess, "run", lambda *_a, **_k: Proc(0))
    assert real_mkdtemp is not None
    assert scan.main(["--repo", str(repo)]) == 0
    assert made and not made[0].exists(), "扫完了影子树还在"


# --------------------------------------------------------------------------- 装工具那一段


def test_资产表按平台齐全且格式对得上现实() -> None:
    """Windows 那份是 **zip**、Linux/Darwin 才是 tar.gz —— 第一版按"以为的形状"写成
    `windows_x86_64.tar.gz` ⇒ 404，而 404 在 fail-closed 下与"离线"退出码长得一模一样。
    名字与格式照 GitHub release 的资产表现读钉死（v8.18.4，2026-10-09 用 API 现读）。
    """
    scan = _load()
    assert set(scan._ASSET_BY_OS) == {"Windows", "Linux", "Darwin"}
    win, win_fmt = scan._ASSET_BY_OS["Windows"]
    assert win_fmt == "zip" and win.endswith("_windows_x64.zip"), win
    for osname, (asset, fmt) in scan._ASSET_BY_OS.items():
        if osname == "Windows":
            continue
        assert fmt == "tar" and asset.endswith(".tar.gz"), f"{osname}: {asset}"


def test_能从两种归档里取出那个二进制(tmp_path: Path) -> None:
    """`_extract_binary` 对 zip 与 tar.gz 都只**读**那一个成员，从不整包解压。

    落点永远是调用方给的路径 —— 归档里写逃逸路径也到不了盘上（测试直接把恶意名字塞进去）。
    """
    scan = _load()

    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../evil/gitleaks.exe", b"ZIP-BINARY")
        zf.writestr("README.md", b"noise")
    assert scan._extract_binary(z, "zip", "gitleaks.exe") == b"ZIP-BINARY"

    tz = tmp_path / "a.tar.gz"
    with tarfile.open(tz, "w:gz") as tf:
        info = tarfile.TarInfo("../../evil/gitleaks")
        data = b"TAR-BINARY"
        info.size = len(data)
        tf.addfile(info, __import__("io").BytesIO(data))
    assert scan._extract_binary(tz, "tar", "gitleaks") == b"TAR-BINARY"
    assert scan._extract_binary(tz, "tar", "不存在的名字") is None


def test_放行清单住在顶层allowlist而不是global表(tmp_path: Path) -> None:
    """gitleaks v8 对未知表**静默忽略**：从前那份把 allowlist 写在 `[global]` 下面，
    七条放行一条都没跑过（A/B 实测：旧写法报 1 条，顶层 `[allowlist]` 报 0 条），
    而 CI 一直绿 —— 绿得没有任何原因。

    判据**读 TOML 结构不读文本**：这份配置的注释里写着 `[global]` 与 `paths` 两个词，
    为的正是解释它们为什么不在 —— 子串匹配会把自己那份历史当成缺陷（第一版就是这么红的）。
    """
    import tomllib

    cfg = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    assert "global" not in cfg, "旧形状 [global] 回来了：viper 的表名，v8 不认，放行静默失效"
    allow = cfg.get("allowlist")
    assert isinstance(allow, dict), "放行清单不住在顶层 [allowlist] 里 —— v8 会整个忽略它"
    assert len(allow.get("regexes") or []) >= 7, "那七条放行少了：它们是唯一挡住文档样例的形状"
    assert "paths" not in allow, (
        "paths 黑名单回来了：它匹配绝对路径、实测会连带命中 tracked 的 shell/build/icon.ico"
        "（过度匹配 = 把该扫的东西静默免检）。范围归 git 名单管，不在这里。"
    )
