"""ci.yml 三层墙钟的**顺序**判据（2026-10-09 一整晚"丢现场"换来的最后一格）。

形状：每步上限（STEP_TIMEOUTS）< 进程内 `--deadline-minutes` < **外部** `timeout` < job
`timeout-minutes`。最后两个关系就是这档用例量的东西 —— 它们错一次，整套取证机制就退回到
"job 被 runner 无声杀掉、GitHub 对 cancelled 不传日志"那一晚（三趟现场全那么丢的）。

为什么值得钉成用例而不是留在探针里：这格判据在本晚**真的错过两次** ——
  * 全量档那趟从前是 deadline 24 + job 30，而 setup 估 6m：24+6 **正好顶到上限**，外部层
    还没建立、内部层永远轮不到先说话；
  * `--ci` 档我一度按"估的开销 118~146s"写下"job 必须抬到 35m"，实测（绿 run 逐步时间戳）
    其实是 42~63s —— 若照估的那组数改了 job 预算，就是把一次拍脑袋固化进防线。
读代码看不出"外部 1320s 会不会晚于 runner"，只有把三行数拿来算才知道。算这件事就该每次
push 都跑一遍，不该只活在 `build/` 下那个没人执行的探针里。

pyyaml 不在的话这档**报错**而不是跳过：判据跑不了 ≠ 判据通过（与本仓"分母为 0 也红"同一条）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _steps() -> list[dict[str, object]]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 环境缺依赖时的形状
        raise AssertionError(f"pyyaml 不可用，三层墙钟的顺序判据跑不了：{exc}") from exc
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    out: list[dict[str, object]] = []
    for job_name, job in wf["jobs"].items():
        budget = job.get("timeout-minutes")
        if budget is None:
            raise AssertionError(f"job {job_name} 没有 timeout-minutes：默认 6 小时 = 没有防线")
        for step in job.get("steps", []):
            run = step.get("run")
            if isinstance(run, str) and "scripts/gate.py" in run:
                out.append({"job": job_name, "run": run, "budget": float(budget)})
    if not out:
        raise AssertionError("ci.yml 里一个调 gate.py 的 run 步骤都没扫到：分母为 0 不等于全绿")
    return out


@pytest.mark.parametrize("case", _steps(), ids=lambda c: str(c["job"]))
def test_三层墙钟由内到外依次早于runner(case: dict[str, object]) -> None:
    run = _executable(str(case["run"]))  # 只看会执行的行：注释里也写着 `--deadline-minutes 21`
    budget = float(case["budget"])  # type: ignore[arg-type]

    internal = re.search(r"--deadline-minutes\s+([\d.]+)", run)
    external = re.search(r"timeout\s+--kill-after=(\d+)s\s+(\d+)s", run)
    assert internal and external, (
        f"[{case['job']}] 三层不齐：内 {bool(internal)} 外 {bool(external)} —— "
        "外部那层（不依赖我们自己配合的唯一一把刀）没建立"
    )
    i_sec = float(internal.group(1)) * 60.0
    kill = float(external.group(1))
    e_sec = float(external.group(2))

    assert i_sec < e_sec, (
        f"[{case['job']}] 内部 {i_sec:.0f}s 没有早于外部 {e_sec:.0f}s："
        "进程内看门狗永远轮不到先说话，最好的那档现场（主线程栈）白写"
    )
    assert e_sec + kill < budget * 60.0, (
        f"[{case['job']}] 外部 {e_sec:.0f}s(+kill {kill:.0f}s) 塞不进 job {budget * 60:.0f}s："
        "runner 会先动手 ⇒ cancelled ⇒ 日志不上传，外部层等于不存在（这正是它要修的形状）"
    )
    # setup/收尾的真实开销必须**留得下余量**：`--ci` 档按实测最坏那一侧（42~63s + Post 1~4s）
    # 扣掉，还得有正数剩余；全量档的 setup 是估的（pip + 两份 npm ci ≈ 360s），同样必须剩正数。
    setup_worst = 63.0 + 4.0 if case["job"] == "gate" else 360.0 + 10.0
    headroom = budget * 60.0 - (setup_worst + e_sec + kill)
    assert headroom > 0, (
        f"[{case['job']}] 给 setup/收尾留的余量是 {headroom:.0f}s（≤0）："
        "runner 抢在外部层之前动手，白设"
    )


def _executable(run: str) -> str:
    """只留**会执行的行**。第一版把这档判据直接跑在整段 `run:` 上，结果抓到的是我自己写在
    注释里的那句"别用 `if ! timeout`" —— 注释不是执行路径，判据扫它就会把"解释了为什么不做"
    读成"做了"（这一发是它自己撞出来的，不是我想到的）。
    """
    return "\n".join(
        ln for ln in run.splitlines() if not ln.strip().startswith("#")
    )


def test_gate步骤不许挂step级超时() -> None:
    """**step 级 `timeout-minutes` 是第四把刀，而且它最钝**（2026-10-09 悬案，10-10 定案）。

    形状（`ENGI-34` 的取证结论）：ci.yml 那年代的三层是 job `25m` / **step `15m`** /
    内部 `--deadline-minutes 21` —— step 15m 先于内部 21m 开火，而 **GitHub 在 step 超时
    那一刻直接杀、不改判 failure、日志不入库**（同一个 run 37830147312：门禁 job 卡在
    `gate.py --ci` 那一步整整 30 分钟、`conclusion=cancelled`、日志端点是 404 BlobNotFound）。
    三层本意是「内层先动、外层兜底」，多出这一层之后变成「最弱（无日志）那层先动」——
    整晚丢掉的四份现场里就有它一份。

    所以这条钉的是**这一层不存在**：调 `gate.py` 的步骤要么不设 step 级 timeout，要么设得
    **严格晚于外部那层**（外部开火 → 退 124 → job 是 failure → 日志一定上传；step 开火 →
    什么都不会留下）。两个当前步骤都是 `None`，但没有机器看着它 —— 这条用例就是那个机器。

    为什么它值得独立一支而不是并进上面那条：上面那条量的是「内→外→job」三段数的**大小关系**，
    而这一条量的是**这一层压根不该在**（多一层 = 多一个能抢先杀 job 的东西）。两者的反面
    形状不同，红起来指向的修法也不同。
    """
    import yaml

    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for job_name, job in wf["jobs"].items():
        budget = job.get("timeout-minutes")
        for step in job.get("steps") or []:
            run = step.get("run")
            if not isinstance(run, str) or "scripts/gate.py" not in run:
                continue
            st = step.get("timeout-minutes")
            if st is None:
                continue
            # 允许的唯一形状：设得**晚于**外部那层（那等于给外部层再兜一道，不影响取证）。
            external = re.search(r"timeout\s+--kill-after=(\d+)s\s+(\d+)s", _executable(run))
            ext_total = (float(external.group(2)) + float(external.group(1))) if external else None
            if ext_total is None or float(st) * 60.0 <= ext_total:
                offenders.append(
                    f"[{job_name}] 步骤 {str(step.get('name'))[:36]!r} 挂 step 级 "
                    f"timeout-minutes={st}（job={budget}，外部层合计 {ext_total}s）"
                )
    assert not offenders, (
        "调 gate.py 的步骤挂上了 step 级超时 —— GitHub 在那一刻**直接杀、不传日志**，"
        "这一层会抢在内部看门狗与外部墙钟之前把现场吃掉（ENGI-34 定案）：\n  "
        + "\n  ".join(offenders)
    )


def test_外部开火时点名那句走得到了() -> None:
    """`|| code=$?` 那一格是实测换来的，不是风格。

    Actions 的 `run:` 默认带 `-e`。三形状对照本机 git-bash 跑过：
      * `timeout …` 换行 `code=$?` ⇒ 124 那一刻**当场中止**，点名那句打不出来；
      * `if ! timeout …; then code=$?` ⇒ `$?` 被条件重置成 **0** ⇒ **防线开火、job 却是绿的**；
      * `code=0` + `timeout … || code=$?` ⇒ 点名那句走到、退码也保得住。
    这一格就是把第三种钉住：谁改回前两种，这里红。
    """
    for case in _steps():
        body = _executable(str(case["run"]))
        assert re.search(r"\|\|\s+code=\$\?", body), f"[{case['job']}] 丢了 `|| code=$?`"
        assert not re.search(r"if\s+!\s+timeout", body), (
            f"[{case['job']}] 换回了 `if ! timeout`：实测那条把 $? 读成 0，防线开火 job 却绿"
        )
        assert "command -v timeout" in body, (
            f"[{case['job']}] 预检没了：runner 上没有 GNU timeout 时这层会**静默不存在**，"
            "而与『存在但没开火』在一趟绿 run 里长得一模一样"
            "（`.gitleaks.toml` 的 [global] 死表就是这个形状）"
        )
