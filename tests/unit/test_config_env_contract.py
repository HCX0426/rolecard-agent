"""config env 契约的 AST 读法与三条相关断言的行为（P3-9 第五格，2026-10-08）。

钉的是**换掉旧版前缀白名单/重复正则**时一起换掉的那些语义：

  读法 —— 表只认 `for … in ((…),)` 里的元组（散文里的同形元组不是契约）、分支键
          （`src.get("KEY")` 独立分支）与表分开可辨、注释不参与（删除说明不是契约）；
  覆盖 —— 没有前缀的键（GIZMO_LIMIT 这种谁也没前缀的）照样被问；**注释行算已暴露**
          （`.env.example` 文件头自己宣布的口径）；
  金丝雀 —— 表读不出来（结构变了）时**红而不是绿**：键变少的契约只会更绿，塌了却全绿
          是最坏的那种坏；
  分支覆盖 —— 新分支键没进值漂移对也不在豁免表 → 红（清单是断言不是备忘；
          R28-14b 那族从前手写漏 OBS_EMIT_RAW_TEXT 就是这么静默的）；
  入口 —— run_api.py 读的 env 必须说过；**解析不出一个名字也红**（空集合把下面算成
          全绿是恒绿尺子）。

夹具走 `monkeypatch.setattr(ccfg, "ROOT", tmp_path)`（与 test_app_icon_corners 同款），
真仓库那一臂不打补丁：只测夹具的断言等于没测。
"""

from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import consistency.checks_config as ccfg  # noqa: E402

_REAL_CFG = (REPO / "src" / "rolecard_agent" / "config.py").read_text(encoding="utf-8")

# 夹具要过金丝雀（下限 _MIN_MAPPING_PAIRS=40），所以主表按 40+1 对生成：
# GIZMO_LIMIT 一把 + KEY_01..KEY_40 填充。填充键的字段名不在真 Settings 上，
# 值漂移那问按 hasattr 跳过，正好让每条红臂只红在它自己那条断言上。
_N = 40
_FILLER = [(f"KEY_{i:02d}", f"key_{i:02d}") for i in range(1, _N + 1)]


def _cfg(*extra_table: tuple[str, str], branch: str = "") -> str:
    """合成 config.py：`for env_key, field in (…):` 的表 + 可选的独立分支读法。"""
    pairs = [("GIZMO_LIMIT", "gizmo_limit"), *_FILLER, *extra_table]
    rows = "".join(f'        ("{k}", "{v}"),\n' for k, v in pairs)
    branch_line = f'    if raw := src.get("{branch}"):\n        data["x"] = raw\n' if branch else ""
    return (
        "import os\n"
        "def from_env():\n"
        "    src = os.environ\n"
        "    data = {}\n"
        "    for env_key, field in (\n"
        f"{rows}"
        "    ):\n"
        "        if (v := src.get(env_key)) is not None:\n"
        "            data[field] = v\n"
        f"{branch_line}"
    )


def _example(gizmo_line: str = "# GIZMO_LIMIT=9") -> str:
    fillers = "".join(f"# KEY_{i:02d}=1\n" for i in range(1, _N + 1))
    return f"{gizmo_line}\n{fillers}"


def _write_repo(root: pathlib.Path, cfg: str, example: str, run_api: str | None = None) -> None:
    (root / "src" / "rolecard_agent").mkdir(parents=True, exist_ok=True)
    (root / "src" / "rolecard_agent" / "config.py").write_text(cfg, encoding="utf-8")
    (root / ".env.example").write_text(example, encoding="utf-8")
    if run_api is not None:
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        (root / "scripts" / "run_api.py").write_text(run_api, encoding="utf-8")


def _silence(monkeypatch) -> None:
    monkeypatch.setattr(ccfg, "out", lambda *a, **k: None)


# ---------------------------------------------------------------- A. 读法（纯函数）


def test_真config的表与分支具体长这样() -> None:
    table, keys = ccfg.config_env_contract(_REAL_CFG)
    assert ("MODEL_DEFAULT", "model_default") in table
    assert ("SQLITE_PATH", "sqlite_path") in table
    # from_env 独立分支的那六个键：表里没有、靠 src.get("KEY") 读出来
    branch = keys - {k for k, _ in table}
    assert branch == {
        "MCP_SERVERS",
        "MODEL_BACKENDS",
        "MODEL_FALLBACKS",
        "MODEL_THINKING",
        "MODEL_THINKING_MODELS",
        "OBS_EMIT_RAW_TEXT",
    }, branch
    # 删除说明是注释不是契约：旧版前缀白名单在这里翻过车（虚盖 SILICONFLOW_API_KEY）
    assert "SILICONFLOW_API_KEY" not in keys


def test_表只认For里的元组_散文里的同形元组不算() -> None:
    cfg = (
        'TABLE = ("LOOSE_ONE", "loose_one")\n'  # 赋值、非 For —— 不是契约
        "def f():\n"
        '    for k, v in (("INSIDE_FOR", "inside_for"),):\n'
        "        pass\n"
    )
    table, _ = ccfg.config_env_contract(cfg)
    assert table == [("INSIDE_FOR", "inside_for")], table


def test_变量拼出来的键读不出来_也长不成KEY的不算() -> None:
    cfg = (
        'def f(src):\n'
        '    x = src.get("lower_case")\n'  # 小写不是 env 键形状
        '    y = src.get("A")\n'  # 单字母不满足 [A-Z][A-Z0-9_]+
        '    src.get(name)\n'  # 变量参数：读不出来（漏在明处）
        '    return x, y\n'
    )
    _, keys = ccfg.config_env_contract(cfg)
    assert keys == set(), keys


# ------------------------------------------------- B. 契约接线（夹具三臂 + 金丝雀）


def test_没有前缀的键也照样被问_缺了就红(tmp_path, monkeypatch) -> None:
    _silence(monkeypatch)
    monkeypatch.setattr(ccfg, "ROOT", tmp_path)
    # GIZMO_LIMIT 不匹配旧版任何一个前缀 —— 白名单时代这条红不了（12 个漏盖键的同形）
    _write_repo(tmp_path, _cfg(), example="OTHER_KEY=1\n")
    ccfg.fails.clear()
    ccfg.check_config_contract()
    assert any("GIZMO_LIMIT" in f for f in ccfg.fails), ccfg.fails


def test_注释行算已暴露_同键补齐就绿(tmp_path, monkeypatch) -> None:
    """`.env.example` 文件头宣布的口径：活键或注释都算"说出来过"。

    MAX_UPLOAD_BYTES 这族（真仓库里 4 个键）就是这个形状 —— 注释行给抄写形状、
    出厂默认与代码一致。若这条按"只认活键"写，真文件当场红，而红的是口径不是问题。
    """
    _silence(monkeypatch)
    monkeypatch.setattr(ccfg, "ROOT", tmp_path)
    _write_repo(tmp_path, _cfg(), example=_example())
    ccfg.fails.clear()
    ccfg.check_config_contract()
    assert not ccfg.fails, ccfg.fails


def test_金丝雀_表塌到下限以下就红而不是绿(tmp_path, monkeypatch) -> None:
    """结构变了（表读不出来）→ 键变少 → 契约只会更绿，所以读法本身要有下限。"""
    _silence(monkeypatch)
    monkeypatch.setattr(ccfg, "ROOT", tmp_path)
    tiny = (
        "def from_env():\n"
        '    for env_key, field in (("ONLY_ONE", "only_one"),):\n'
        "        pass\n"
    )
    _write_repo(tmp_path, tiny, example="# ONLY_ONE=1\n")
    ccfg.fails.clear()
    ccfg.check_config_contract()
    # fails 的措辞是契约（out 的 detail 只是给人看的显示，别把断言钉在显示上）
    assert any("config mapping table unreadable" in f for f in ccfg.fails), ccfg.fails


def test_新分支键没人管就红_豁免了就绿(tmp_path, monkeypatch) -> None:
    """清单是断言不是备忘：手写清单漏一个（R28-14b 漏 OBS_EMIT_RAW_TEXT）洞是静默的。"""
    _silence(monkeypatch)
    monkeypatch.setattr(ccfg, "ROOT", tmp_path)

    # 红臂：分支键既不在值漂移对也不在 JSON 豁免表
    _write_repo(
        tmp_path,
        _cfg(branch="GIZMO_MOTOR"),
        example=_example() + "# GIZMO_MOTOR=1\n",
    )
    ccfg.fails.clear()
    ccfg.check_config_contract()
    assert any("GIZMO_MOTOR" in f for f in ccfg.fails), ccfg.fails
    assert not any("GIZMO_LIMIT" in f for f in ccfg.fails), "红的必须是分支覆盖这一条"

    # 绿臂：换成 JSON 豁免表里的键（那族由 `env example json valid` 另一条尺子管）
    _write_repo(
        tmp_path,
        _cfg(branch="MODEL_BACKENDS"),
        example=_example() + "# MODEL_BACKENDS={}\n",
    )
    ccfg.fails.clear()
    ccfg.check_config_contract()
    assert not ccfg.fails, ccfg.fails


# ------------------------------------------------------- C. 入口那条（run_api.py）


def test_入口键缺了就红_注释补齐就绿(tmp_path, monkeypatch) -> None:
    _silence(monkeypatch)
    monkeypatch.setattr(ccfg, "ROOT", tmp_path)
    run_api = 'import os\ndef main():\n    return os.environ.get("GIZMO_KEY")\n'

    _write_repo(tmp_path, cfg="x = 1\n", example="OTHER=1\n", run_api=run_api)
    ccfg.fails.clear()
    ccfg.check_entrypoint_env_documented()
    assert any("GIZMO_KEY" in f for f in ccfg.fails), ccfg.fails

    _write_repo(tmp_path, cfg="x = 1\n", example="# GIZMO_KEY=\n", run_api=run_api)
    ccfg.fails.clear()
    ccfg.check_entrypoint_env_documented()
    assert not ccfg.fails, ccfg.fails


def test_冒烟参数豁免_但读不到名字时必须红(tmp_path, monkeypatch) -> None:
    """SMOKE_* 是 `--smoke` 校对参数（与取证私有开关同口径豁免）；
    而"一个名字都没解析出来"是解析被跳过，空集合把下面算成全绿 = 恒绿尺子。"""
    _silence(monkeypatch)
    monkeypatch.setattr(ccfg, "ROOT", tmp_path)

    # 绿臂：只有冒烟参数
    _write_repo(
        tmp_path,
        cfg="x = 1\n",
        example="ANY=1\n",
        run_api="import os\nk = os.environ.get('SMOKE_BASE_URL')\n",
    )
    ccfg.fails.clear()
    ccfg.check_entrypoint_env_documented()
    assert not ccfg.fails, ccfg.fails

    # 红臂一：文件在，但语法错误被解析器跳过 → read={}
    _write_repo(tmp_path, cfg="x = 1\n", example="ANY=1\n", run_api="def broken(:\n")
    ccfg.fails.clear()
    ccfg.check_entrypoint_env_documented()
    assert any("entrypoint unreadable" in f for f in ccfg.fails), ccfg.fails

    # 红臂二：文件没了 → 同样不许静默绿
    _write_repo(tmp_path, cfg="x = 1\n", example="ANY=1\n")
    (tmp_path / "scripts" / "run_api.py").unlink()
    ccfg.fails.clear()
    ccfg.check_entrypoint_env_documented()
    assert any("entrypoint unreadable" in f for f in ccfg.fails), ccfg.fails


# --------------------------------------------------------------- D. 真仓库那一臂


def test_真仓库此刻三条断言全绿(monkeypatch) -> None:
    """正向那一半：夹具全绿而真文件红 = 判据是在夹具上调出来的假绿。"""
    _silence(monkeypatch)
    ccfg.fails.clear()
    ccfg.check_config_contract()
    ccfg.check_startup_env_documented()
    ccfg.check_entrypoint_env_documented()
    assert not ccfg.fails, ccfg.fails


def test_真仓库的映射表对数在下限之上() -> None:
    table, _ = ccfg.config_env_contract(_REAL_CFG)
    assert len(table) >= ccfg._MIN_MAPPING_PAIRS, f"现网表只剩 {len(table)} 对"
