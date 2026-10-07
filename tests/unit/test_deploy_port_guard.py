"""端口那两问的行为（P3-9 解析器化的第四格）。

为什么单独钉这个函数：换掉的两条正则有**实测的三发漏检** —— 对现网 compose 做了变异
（`scripts/tools/probe_deploy_porthole.py` 留档），旧尺子三发全绿，而那条断言的结论文本
当时正写着"应用端口不 publish"。判据是摆设而报告正常，是最贵的一种坏：下一个人以为有防线。

这里不复现"改文件再跑整份一致性"那种慢法，而是把纯函数按形状喂：同样的判别、十倍的
速度、离线、且失败信息直指是哪一条规矩。

四条规矩对应四发变异：
  M1 `- "8080:8000"`      宿主侧不是 8000 的 publish —— 旧正则锚在字面 8000，漏
  M2 `- "127.0.0.1:8000:8000"` 绑回环的 publish —— 一样漏（暴露与绑不绑回环无关）
  M3 整段 `expose:` 删掉   —— 旧写法取到空集就 `if exposed and upstream:` 跳过，静默绿
  M4 expose 与 Caddyfile 端口漂移 —— 这一发旧尺子**本来就拦得住**，留在这里是为了证明
     重写没把已有的牙磨掉（只证新能力不证旧能力，是这类"加固"最常见的自欺）
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from consistency.checks_runtime import _deploy_port_findings  # noqa: E402
from consistency.core import YamlSubsetError  # noqa: E402

CADDY = "{$ROLECARD_DOMAIN} {\n\treverse_proxy app:8000 {\n\t}\n}\n"
BASE = (
    "services:\n"
    "  app:\n"
    "    environment:\n"
    '      AUTH_MODE: "on"\n'
    "    expose:\n"
    '      - "8000"\n'
    "  caddy:\n"
    "    image: caddy:2\n"
    "    ports:\n"
    '      - "80:80"\n'
    '      - "443:443"\n'
)


def test_现网形状不误报() -> None:
    """基线：caddy 有 ports、app 只有 expose —— 不许红（否则第一发真红会被当成噪声）。"""
    assert _deploy_port_findings(BASE, CADDY) == []


def test_真实compose文件本身不误报() -> None:
    """拿仓库里那份真文件过一遍：尺子先是给自己家调的，它红在真文件上就是改错了。"""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert _deploy_port_findings(compose, caddy) == []


def test_M1_宿主侧非8000的publish也拦() -> None:
    """旧 A 的漏检：`- "?8000:\\d+"` 锚在冒号左边字面是 8000。"""
    mutated = BASE.replace(
        '    expose:\n      - "8000"\n',
        '    ports:\n      - "8080:8000"\n    expose:\n      - "8000"\n',
    )
    problems = _deploy_port_findings(mutated, CADDY)
    assert any("publish" in p and "app" in p for p in problems), problems


def test_M2_绑回环的publish也拦() -> None:
    """暴露这件事与"绑没绑回环"无关：旧写法同样看不见。"""
    mutated = BASE.replace(
        '    expose:\n      - "8000"\n',
        '    ports:\n      - "127.0.0.1:8000:8000"\n    expose:\n      - "8000"\n',
    )
    problems = _deploy_port_findings(mutated, CADDY)
    assert any("publish" in p for p in problems), problems


def test_M3_expose整段没了不再等于不问() -> None:
    """旧 B 的静默绿：`if exposed and upstream:` —— 取到空集就整问跳过。"""
    mutated = BASE.replace('    expose:\n      - "8000"\n', "")
    problems = _deploy_port_findings(mutated, CADDY)
    assert any("读不出 expose 序列" in p for p in problems), problems


def test_M4_端口漂移这条旧牙还在() -> None:
    """重写不能只添新牙、磨旧牙。"""
    mutated = BASE.replace('      - "8000"\n', '      - "9000"\n')
    problems = _deploy_port_findings(mutated, CADDY)
    assert any("app:8000" in p and "app:9000" in p for p in problems), problems


def test_M5_expose写了但下面是空的也出声() -> None:
    """`expose:` 这一格在而里面没条目 —— 读出来是 None，不等于"没东西可查"。

    这一发是被**变异测出来的**：把旧写法 ② 装回去之后本仓用例全绿，说明我原先只测了
    "整段消失"，漏了"在但为空"（旧写法 `if exposed and upstream:` 在这里照样跳过）。
    空与缺是两件事，与 `_compose_service_env` 的 `None` / `{}` 分法同一条纪律。
    """
    problems = _deploy_port_findings("services:\n  app:\n    expose:\n", CADDY)
    assert any("expose" in p and "app:8000" in p for p in problems), problems


def test_子集之外的compose大声抛而不是判绿() -> None:
    """`expose: []` 是流式语法，`simple_yaml` 直接抛 —— 这是**要的**行为。

    静默返回空集在部署这一族等于"配了没用还报告正常"（本文件 `_compose_app_env` 的
    docstring 已把这条定成规矩）。这里把它钉住：换 compose 解析口径的人若想改成
    "读不出就当没有"，这条会红。
    """
    with pytest.raises(YamlSubsetError):
        _deploy_port_findings("services:\n  app:\n    expose: []\n", CADDY)


def test_expose形状不是序列时出声() -> None:
    """把 expose 写成标量（`expose: 8000`）是配错了，不能读成序列再悄悄通过。"""
    problems = _deploy_port_findings('services:\n  app:\n    expose: "8000"\n', CADDY)
    assert any("读不出 expose 序列" in p for p in problems), problems


def test_caddy自己publish公网口是允许的() -> None:
    """规矩是"除 caddy 之外不许 publish"，不是"不许有 ports" —— 别把它写成第二条禁令。"""
    assert _deploy_port_findings(BASE, CADDY) == []
    mutated = BASE.replace('      - "80:80"\n', '      - "8443:8443"\n')
    assert _deploy_port_findings(mutated, CADDY) == []


def test_services整段没了也出声不静默() -> None:
    """compose 里没有 `services:`（或没有 app 那段）而 Caddyfile 打着 app:8000 ——
    这不能读成"没东西可查"：反代打不通是事实，尺子必须说。旧写法正是在这里返回空集
    然后 `if exposed and upstream:` 跳过，等于"什么都没配"被当成"配置是对的"。
    """
    assert _deploy_port_findings("volumes:\n  rolecard-data:\n", CADDY), "静默了"
    problems = _deploy_port_findings("services:\n  app:\n", CADDY)
    assert problems, problems


def test_没有caddy插值时不硬要求expose() -> None:
    """Caddyfile 那侧没有 `reverse_proxy app:` 时，缺 expose 不该红（不是本档的部署形状，
    也不该逼着一个没反代的 compose 去写 expose —— 尺子只说它看得见的事）。"""
    assert _deploy_port_findings("services:\n  app:\n    image: x\n", "") == []
