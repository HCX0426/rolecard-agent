"""CI 里「在宿主机 python 上 import 项目包」这把尺子（10-03 我自己写出来的那发红）。

`R102-39` 的落点本身是对的（别在 ci.yml 抄第二份 base_url），但它改成了在 **runner 宿主机**
上 `python3 -c "from rolecard_agent.config import …"`，并在注释里断言"runner 只做标准库 import，
装不装依赖无关" —— 假的：`config.py` 模块级 `from pydantic import …`。镜像臂因此红在
`ModuleNotFoundError: No module named 'pydantic'`，而它前面四问（健康 200 / 无凭据 401 /
建会话读回 / 首页托管）全过，症状看着像"云端后端存不进去"。

两臂都必须能红（`R102-73` 那条教训：只测 deny 那一半的护栏救不了任何人）：
非标准库 ⇒ 红；全标准库 ⇒ 绿；**一个 `python -c` 都没扫到 ⇒ 也红**（分母为 0 与"量了说干净"
长得一模一样）。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

import check_consistency as cc  # noqa: E402


def _write(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    p = tmp_path / "ci.yml"
    p.write_bytes(body.encode("utf-8"))
    return p


def test_宿主机上import项目包_必须红(tmp_path: pathlib.Path) -> None:
    """这一条就是 10-03 真实发生过的那次红。"""
    p = _write(
        tmp_path,
        "steps:\n"
        "  - run: |\n"
        '      SF=$(python3 -c "from rolecard_agent.config import '
        'DEFAULT_SILICONFLOW_BASE_URL; print(DEFAULT_SILICONFLOW_BASE_URL)")\n',
    )
    got = cc.check_ci_host_python_stdlib_only(p, report=False)
    assert got, "项目包被在宿主机上 import 却没判红 ⇒ 这把尺子是摆设"
    assert any("rolecard_agent" in g for g in got), got


def test_容器里读同一个常量_必须绿(tmp_path: pathlib.Path) -> None:
    """修法本身也要被钉住：读同一份事实面，但读的地方是被测容器。"""
    p = _write(
        tmp_path,
        "steps:\n"
        "  - run: |\n"
        '      SF=$(docker exec rc python -c "from rolecard_agent.config import '
        'DEFAULT_SILICONFLOW_BASE_URL; print(DEFAULT_SILICONFLOW_BASE_URL)")\n'
        "      uid=$(docker exec rc python -c 'import os; print(os.getuid())')\n",
    )
    assert cc.check_ci_host_python_stdlib_only(p, report=False) == []


def test_宿主机只用标准库_必须绿(tmp_path: pathlib.Path) -> None:
    p = _write(
        tmp_path,
        "steps:\n"
        "  - run: |\n"
        "      tid=$(curl -s x | python3 -c 'import json,sys; "
        'print(json.load(sys.stdin)["thread_id"])\')\n'
        '      SF=$(docker exec rc python -c "import rolecard_agent.config")\n',
    )
    assert cc.check_ci_host_python_stdlib_only(p, report=False) == []


def test_一个都没扫到时不许假装干净(tmp_path: pathlib.Path) -> None:
    """分母为 0 的那一半：扫不到东西与"扫到了都干净"输出长得一样，所以它必须自己出声。"""
    p = _write(tmp_path, "steps:\n  - run: echo hi\n")
    got = cc.check_ci_host_python_stdlib_only(p, report=False)
    assert got and "没扫到" in got[0], got


def test_注释里的写法不算一次真执行(tmp_path: pathlib.Path) -> None:
    """第一版的假阳：把「解释这条尺子由来的那句注释」读成了一次宿主机 import。"""
    p = _write(
        tmp_path,
        "steps:\n"
        "  - run: |\n"
        '      # 从前这里是 python3 -c "from rolecard_agent.config import X"，那是错的\n'
        '      SF=$(docker exec rc python -c "from rolecard_agent.config import X")\n',
    )
    assert cc.check_ci_host_python_stdlib_only(p, report=False) == []


def test_from导入只取模块不取符号名() -> None:
    """`from X import Y` 里只有 X 是模块；把 Y 也报成模块会让读数混进噪声。"""
    assert cc._imported_modules("from rolecard_agent.config import DEFAULT_X; print(1)") == [
        "rolecard_agent.config"
    ]
    assert cc._imported_modules("import json,sys") == ["json", "sys"]
    assert cc._imported_modules("import os") == ["os"]


def test_仓库里那份ciyml此刻是绿的() -> None:
    """正向那一半：真文件必须过 —— 否则这条断言只是在测夹具。"""
    assert cc.check_ci_host_python_stdlib_only(report=False) == []
