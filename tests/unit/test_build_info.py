"""`core/build_info.py` 与 `/api/health` 里那一格 `build` —— "这一包是谁"的判据地基。

为什么给一个这么小的模块写单测（与 `test_scratch_db.py` 同一个理由）：它的失败形状不是崩，
是**安静地相等**。读不到身份时如果回空串，`probe_package_artifact.py` 第③层就会拿空串比空串，
"身份一致 ✓"打印出来，而屏幕上那句结论的意思其实是"我什么也没证明"。10-01 那轮打包链盘点
（P0-1）要堵的正是这一形，所以这里钉的全是"**读不到必须说读不到**"，外加冻结态那条只有真打
过包才走得到的分支。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.core import build_info


def _frozen(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """把"跑在打包出来的 exe 里"这件事假出来：`is_frozen` 与 `bundle_root` 一起换。"""
    monkeypatch.setattr(build_info, "is_frozen", lambda: True)
    monkeypatch.setattr(build_info, "bundle_root", lambda: root)


def test_dev_identity_is_a_real_sha_plus_a_dirty_flag() -> None:
    info = build_info.read_build_info()
    assert info.known, "开发态读不到 HEAD —— 判据会全体退化成 unknown"
    assert len(info.fingerprint) == build_info.FINGERPRINT_LENGTH
    assert all(c in "0123456789abcdef" for c in info.fingerprint)
    assert isinstance(info.dirty, bool)


def test_baked_fingerprint_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _frozen(monkeypatch, tmp_path)
    (tmp_path / build_info.BUILD_INFO_NAME).write_text(
        json.dumps(
            {
                "git_sha": "abc1234567890def",
                "built_utc": "2026-10-01T05:00:00+00:00",
                "dirty": False,
            }
        ),
        encoding="utf-8",
    )
    info = build_info.read_build_info()
    assert info.fingerprint == "abc123456789"
    assert info.built_utc == "2026-10-01T05:00:00+00:00"
    assert info.dirty is False


def test_missing_baked_file_is_unknown_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """这条是整份测试的意义所在：读不到时**不许**回空串。"""
    _frozen(monkeypatch, tmp_path)
    info = build_info.read_build_info()
    assert info.fingerprint == build_info.UNKNOWN
    assert info.known is False
    # 判据拿它去比 HEAD：空串比空串会"相等"，而 unknown 比真 sha 一定不等 —— 这就是不许回空串的理由
    assert info.fingerprint != build_info._git("rev-parse", "HEAD")[: build_info.FINGERPRINT_LENGTH]


def test_truncated_or_broken_payload_degrades_to_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _frozen(monkeypatch, tmp_path)
    target = tmp_path / build_info.BUILD_INFO_NAME
    target.write_text(json.dumps({"git_sha": "abc123"}), encoding="utf-8")
    assert build_info.read_build_info().fingerprint == build_info.UNKNOWN
    target.write_text("{ not json", encoding="utf-8")
    assert build_info.read_build_info().fingerprint == build_info.UNKNOWN


def test_dirty_flag_absent_is_none_not_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"没记"与"记了说干净"是两件事：把它们混成一件事，脏树打包就会看起来是干净的。"""
    _frozen(monkeypatch, tmp_path)
    (tmp_path / build_info.BUILD_INFO_NAME).write_text(
        json.dumps({"git_sha": "ffff111122223333"}), encoding="utf-8"
    )
    assert build_info.read_build_info().dirty is None


def test_health_reports_the_fingerprint(tmp_path: Path) -> None:
    client = TestClient(create_app(sqlite_path=tmp_path / "app.db"))
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    build = body["build"]
    assert build["sha"] == build_info.read_build_info().fingerprint
    assert build["dirty"] in (True, False)


# ---- 烤这一格的那一侧：`scripts/build_sidecar.py::_write_build_info ----

_SIDECAR_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_sidecar.py"


def _load_build_sidecar():
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_sidecar_under_test", str(_SIDECAR_SCRIPT))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dirty_flag_only_asks_about_paths_that_go_into_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10-01 第十七包就是这么被冤枉的：脏的只有装机脚本，旗却写 `dirty: true`。

    这一问的本意是"装进去的那份后端是不是这个 commit"，所以它必须带 pathspec；
    不带就是把"这台机器上还有别处在改"当成"包与 HEAD 不符"。
    """
    mod = _load_build_sidecar()
    seen: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str:
        seen.append(tuple(args))
        if args[:1] == ("rev-parse",):
            return "deadbeefcafe"
        if args[:1] == ("status",):
            # 带 pathspec 的那一问：干净；整棵树那一问：脏（里面有装机脚本）。
            return "" if "--" in args else " M scripts/install_package.ps1"
        return ""

    monkeypatch.setattr(mod, "_git", fake_git)
    monkeypatch.setattr(mod, "BUILD_INFO", tmp_path / "build_info.json")
    mod._write_build_info()

    status_calls = [c for c in seen if c[:1] == ("status",)]
    assert status_calls, "没有问过 git status"
    asked = status_calls[0]
    assert "--" in asked, "git status 没带 pathspec —— 会把包外的改动算成包不干净"
    assert "src" in asked and "packaging" in asked
    payload = json.loads((tmp_path / "build_info.json").read_text(encoding="utf-8"))
    assert payload["dirty"] is False, "包外有改动不该让包变脏"


def _no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """把"这台机器上没有 git 这个二进制"假出来（镜像里就是这个形状）。"""
    import subprocess

    def boom(*a: object, **kw: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(subprocess, "run", boom)
    build_info._dev_identity.cache_clear()  # noqa: SLF001


def test_missing_git_answers_unknown_instead_of_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/api/health` 抛 500 的代价在容器里是**永远不健康**，而不是"少一格信息"。

    10-01 CI 镜像那一臂实测：`python:3.13-slim` 里没有 git，`.dockerignore` 也不带 `.git`，
    于是 `read_build_info` 里那句 `subprocess.run(["git", …])` 直接穿出 ASGI，
    `HEALTHCHECK` 每一次都拿到 500。本模块的规矩从头是"问不到就说问不到、不抛"。
    """
    _no_git(monkeypatch)
    try:
        info = build_info.read_build_info()
    except Exception as exc:  # noqa: BLE001 - 这条用例的意义就是"这里不许抛"
        raise AssertionError(f"问不到 git 时 read_build_info 抛了：{exc!r}") from exc
    assert not info.known
    assert info.fingerprint == build_info.UNKNOWN
    # 「没记」不等于「记了说干净」：把问不到写成 False 是最省事的假绿。
    assert info.dirty is None
    build_info._dev_identity.cache_clear()  # noqa: SLF001


def test_health_endpoint_answers_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_git(monkeypatch)
    try:
        client = TestClient(create_app(sqlite_path=tmp_path / "app.db"))
        response = client.get("/api/health")
    finally:
        build_info._dev_identity.cache_clear()  # noqa: SLF001
    assert response.status_code == 200, response.text
    assert response.json()["build"]["sha"] == build_info.UNKNOWN
