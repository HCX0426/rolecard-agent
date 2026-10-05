"""同步三方向的两实例端到端探针（M7 上行 + M8 下行与登录对账）。

单测里那些 `monkeypatch.setattr("httpx.post", ...)` 只证明"挑对了要推什么"，
它证明不了三件只有两台真机器才能证明的事：

  * **对面认的是它自己的身份** —— B 收到 import 之后，写进去的行必须盖 B 那台解析出来的
    人（`IDENTITY_USER_ID=u1`）的章。要是它读了载荷里的 `user_id`，M1~M6 那整套归属在
    跨机器这一环就漏了。
  * **真走一遍认证与超时** —— Basic 口令、`validate_base_url`、对面那台的鉴权中间件，
    这些在 monkeypatch 下永远不会红。
  * **重放不长双份** —— 同一发 apply 打两次，对面的清单必须一字不差还是那些。

读数一律按 **ID 多重集** 比（不是"条数对不对"）：条数相等而身份换了，正是覆盖事故的形状。

跑法（仓库根）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/probe_sync_push.py

红线照旧：自抢空闲端口、只杀自己 spawn 的 pid、两份数据根都在 build/ 下、不碰 :8000。
这里连写都只写在 build/ 下的新根上，真库一个字节都不碰。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VENV_PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

# 结论行带对勾，而 Windows 控制台默认 codepage 是 GBK：不重配编码，最后一句 print 会抛
# UnicodeEncodeError 退场（`R26-24` 那一族症状，门禁的 console encoding 一条就是为它写的）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 结论行带对勾，而 Windows 控制台默认 codepage 是 GBK：不重配编码，最后一句 print 会抛
# UnicodeEncodeError 退场（`R26-24` 那族症状，门禁的 console encoding 一条就是为它写的）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

CARD_ID = "r_pushed"
MEMORY_UID = "uid-push-1"
MEMORY_TEXT = "用户住在上海，跑步习惯是每周三次。"
REACHOUT_TEXT = "今天的风很适合出门跑两步。"
THREAD_ID = "s_pushprobe01"
MESSAGES = [
    {"role": "user", "text": "帮我看看这周的安排"},
    {"role": "assistant", "text": "周三晚上你留了空档。"},
    {"role": "user", "text": "那就周三去跑步"},
    {"role": "assistant", "text": "好，我把闹钟挪到周三十九点。"},
]
KINDS = ["card", "memory", "reachout", "thread"]

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  <- {detail}" if not ok else ""))
    if not ok:
        FAILS.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start(name: str, port: int, extra: dict[str, str]) -> tuple[subprocess.Popen, Any]:
    root = ROOT / "build" / f"sync_push_{name}"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "PYTHONIOENCODING": "utf-8",
        "RUN_API_PORT": str(port),
        "MEMORY_EXTRACT_AUTO": "0",
        "MODEL_PIN_ON_STARTUP": "0",
        "DATA_ROOT": str(root),
        **extra,
    }
    log = open(root / "api.log", "wb")  # noqa: SIM115
    proc = subprocess.Popen(  # noqa: S603
        [VENV_PY, "scripts/run_api.py"], env=env, cwd=str(ROOT),
        stdout=log, stderr=subprocess.STDOUT,
    )
    return proc, log


def wait_health(base: str, proc: subprocess.Popen) -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"后端提前退出：{base}，看 build/sync_push_*/api.log")
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=3) as r:  # noqa: S310
                if r.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.5)
    raise RuntimeError(f"90s 内没起来：{base}")


def req(method: str, url: str, body: object = None, token: str | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    r = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": token} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:  # noqa: S310
            return resp.status, _loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, _loads(exc.read().decode("utf-8", "replace"))


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def pairs(base: str, token: str | None) -> list[tuple[str, str]]:
    """对面这份清单的 **ID 多重集**（(kind, ident) 排序后的列表）。

    用列表而不是集合：集合看不见"同一个 uid 长出了两行"，而那正是重放要防的事。
    """
    code, out = req("GET", f"{base}/api/sync/inventory", token=token)
    if code != 200 or not isinstance(out, dict):
        return []
    return sorted(
        (str(row.get("kind")), str(row.get("ident"))) for row in out.get("items") or []
    )


def written_total(out: Any) -> int:
    if not isinstance(out, dict):
        return -1
    return sum(int(v) for v in (out.get("written") or {}).values())


def seed_a(a_base: str) -> None:
    """在 A 上造出要上行的一份数据。

    走 A 自己的 `/api/sync/import`：那正是 B 稍后要走的同一段代码（`_write_card` /
    `memory.restore_row` / `inbox.restore_row` / `_write_thread`），比手工 INSERT 更
    接近真链路，而且**会话历史只有这条路能不接模型就落进检查点**。
    """
    code, out = req(
        "POST",
        f"{a_base}/api/sync/import",
        {
            "items": [
                {
                    "kind": "card",
                    "ident": CARD_ID,
                    "payload": {
                        "role_id": CARD_ID,
                        "role_name": "上行专测卡",
                        "system_prompt": "本机写的那份人设",
                    },
                },
                {
                    "kind": "memory",
                    "ident": MEMORY_UID,
                    "payload": {"text": MEMORY_TEXT, "role_id": CARD_ID},
                },
                {
                    "kind": "reachout",
                    "ident": f"{CARD_ID}|2026-09-27 01:00:00|x",
                    "payload": {
                        "role_id": CARD_ID,
                        "role_name": "上行专测卡",
                        "text": REACHOUT_TEXT,
                        "created_at": "2026-09-27 01:00:00",
                    },
                },
                {
                    "kind": "thread",
                    "ident": THREAD_ID,
                    "payload": {
                        "thread_id": THREAD_ID,
                        "title": "上行专测会话",
                        "current_role_id": CARD_ID,
                        "messages": MESSAGES,
                    },
                },
            ]
        },
    )
    check("A 侧播种（4 类各落 1 条）", code == 200 and written_total(out) == 4, out)


def main() -> int:
    a_port, b_port = free_port(), free_port()
    a_base, b_base = f"http://127.0.0.1:{a_port}", f"http://127.0.0.1:{b_port}"
    token = "Basic " + base64.b64encode(b"u1:pw").decode()
    a, la = start("a", a_port, {"AUTH_MODE": "off"})
    b, lb = start(
        "b",
        b_port,
        {
            "AUTH_MODE": "on",
            "AUTH_CREDENTIALS": "u1:pw",
            "IDENTITY_USER_ID": "u1",
            # 云端那台按设计**只存盘、不开口**（`R26-45` 之后定的部署形态：会不会说话是
            # 实例的属性，不是浏览器的状态）。探针带着它跑，这条形态才是被真跑过的，
            # 而不是只写在文档里。
            "REACHOUT_ENABLED": "0",
            "API_ALLOW_ORIGINS": a_base,
        },
    )
    print(f"A(本机)={a_base} pid={a.pid}  B(云端)={b_base} pid={b.pid}")
    target = {"base_url": b_base, "user": "u1", "secret": "pw"}
    try:
        wait_health(a_base, a)
        wait_health(b_base, b)
        seed_a(a_base)
        mine = pairs(a_base, None)
        seeded = {("card", CARD_ID), ("memory", MEMORY_UID), ("thread", THREAD_ID)}
        check("A 的清单里卡/记忆/会话都在", seeded <= set(mine), mine)
        check("A 的清单里主动消息也在", any(k == "reachout" for k, _ in mine), mine)

        # ① 计划：出厂内置卡两边同名同内容 ⇒ 落在"相同"，不进冲突（否则第一屏全是噪音）
        code, plan = req("POST", f"{a_base}/api/sync/plan", target)
        counts = plan.get("counts", {}) if isinstance(plan, dict) else {}
        only_local = {
            (str(row.get("kind")), str(row.get("ident")))
            for row in (plan.get("only_local") or [] if isinstance(plan, dict) else [])
        }
        check("① 计划：播种那几条都是本机独有", code == 200 and seeded <= only_local,
              {"counts": counts, "only_local": sorted(only_local)})
        check("① 计划：零冲突", counts.get("conflicts") == 0, counts)
        check("① 计划：内置卡算『相同』", counts.get("same", 0) >= 2, counts)

        # ② 上行：真走一次 HTTP + Basic 认证 + 对面的鉴权中间件
        code, applied = req(
            "POST", f"{a_base}/api/sync/apply", {**target, "kinds": KINDS, "mode": "merge"}
        )
        sent = applied.get("sent") if isinstance(applied, dict) else None
        check("② 上行：推的就是计划里那些条", code == 200 and sent == len(only_local),
              {"sent": sent, "only_local": len(only_local)})
        check("② 对面落了同样多条", written_total(
            (applied or {}).get("remote") if isinstance(applied, dict) else None) == sent, applied)

        # ③ B 那侧的归属：盖的是 B 自己认出的那个人，不是载荷里带来的
        first = pairs(b_base, token)
        check("③ 对面 u1 看得见推来的三条", seeded <= set(first), first)
        _, hist = req("GET", f"{b_base}/api/session/{THREAD_ID}/messages", token=token)
        texts = [str(m.get("content") or "") for m in (hist or {}).get("messages", [])] \
            if isinstance(hist, dict) else []
        check("③ 会话历史在对面读得回来，一字不差", texts == [m["text"] for m in MESSAGES], hist)
        _, box = req("GET", f"{b_base}/api/reachouts", token=token)
        check("③ 主动消息进了对面的收件箱",
              REACHOUT_TEXT in json.dumps(box, ensure_ascii=False), box)
        _, roles_b = req("GET", f"{b_base}/api/roles", token=token)
        check("③ 推来的卡在对面的角色列表里",
              any(isinstance(r, dict) and r.get("role_id") == CARD_ID for r in roles_b or []),
              roles_b)

        # ④ 重放不长双份（ID 多重集必须一模一样）
        req("POST", f"{a_base}/api/sync/apply", {**target, "kinds": KINDS, "mode": "merge"})
        check("④ 重放后对面清单一字未变", pairs(b_base, token) == first,
              (first, pairs(b_base, token)))

        # ⑤ 冲突：改本机那张卡 ⇒ 判一条冲突 ⇒ 裁决「保留本机这份」真的写进对面
        req("PATCH", f"{a_base}/api/roles/{CARD_ID}", {"system_prompt": "本机后来改过的人设"})
        _, plan2 = req("POST", f"{a_base}/api/sync/plan", target)
        c2 = plan2.get("counts", {}) if isinstance(plan2, dict) else {}
        check("⑤ 改过的卡判成 1 条冲突", c2.get("conflicts") == 1, c2)
        req("POST", f"{a_base}/api/sync/apply", {
            **target, "kinds": ["card"], "mode": "merge",
            "resolutions": {f"card:{CARD_ID}": "mine"}})
        _, roles_c = req("GET", f"{b_base}/api/roles", token=token)
        prompt = next((str(r.get("system_prompt")) for r in roles_c or []
                       if isinstance(r, dict) and r.get("role_id") == CARD_ID), "")
        check("⑤ 裁决「保留本机这份」写进了对面", "后来改过" in prompt, prompt)

        # ⑥ 没勾的类不动对面：对面自己建的卡，本机只推记忆时不许碰它
        req("POST", f"{b_base}/api/roles", {"role_id": "r_his_own", "role_name": "对面自有卡",
                                            "system_prompt": "在对面建的"}, token=token)
        req("POST", f"{a_base}/api/sync/apply", {**target, "kinds": ["memory"], "mode": "merge"})
        check("⑥ 只勾记忆时，对面的自有卡还在",
              ("card", "r_his_own") in pairs(b_base, token))

        # ⑦ 审计与响应里不许出现推过去的原文与口令
        _, audit = req("GET", f"{b_base}/api/audit?limit=80", token=token)
        blob = json.dumps(audit, ensure_ascii=False)
        check("⑦ 对面审计不含记忆原文", MEMORY_TEXT not in blob)
        check("⑦ 对面审计不含推来的会话标题", "上行专测会话" not in blob)
        check("⑦ 对面审计不含口令", '"pw"' not in blob and "Basic " not in blob)

        # ---- 下行与登录对账（M8）：同一套引擎的另外两个方向 ------------------------
        req("POST", f"{b_base}/api/roles",
            {"role_id": "r_his_own2", "role_name": "对面后来建的卡", "system_prompt": "云端独有的"},
            token=token)
        code, pulled = req("POST", f"{a_base}/api/sync/pull",
                           {**target, "kinds": KINDS, "mode": "merge"})
        names = json.dumps(req("GET", f"{a_base}/api/roles")[1], ensure_ascii=False)
        check("⑧ 下行：对面那份里本机没有的并回来了",
              code == 200 and "r_his_own2" in names, {"pulled": pulled, "roles": names[:80]})
        _, again = req("POST", f"{a_base}/api/sync/pull",
                       {**target, "kinds": KINDS, "mode": "merge"})
        check("⑧ 下行也是幂等的（第二遍 pulled=0）",
              isinstance(again, dict) and again.get("pulled") == 0, again)
        _, denied = req("POST", f"{a_base}/api/sync/pull", {**target, "mode": "replace"})
        check("⑧ 下行不给『整份替换』这一档（它清的是本机这份）",
              denied == 400 or (isinstance(denied, dict)
                                and "整份替换" in str(denied.get("detail"))), denied)

        # 真分歧：同一张卡两边各改过一遍，本机最后改 ⇒ 对账按新者胜，把本机这份推回去
        req("POST", f"{a_base}/api/sync/import", {"items": [
            {"kind": "memory", "ident": "uid-push-2",
             "payload": {"text": "对账前本机又写的一条", "role_id": CARD_ID}}]})
        req("PATCH", f"{b_base}/api/roles/{CARD_ID}",
            {"system_prompt": "对面后来又改的人设"}, token=token)
        time.sleep(1.5)  # 新者胜按秒比：同一秒里两边都改过 = 真歧义，策略会留给人（这是对的）
        req("PATCH", f"{a_base}/api/roles/{CARD_ID}", {"system_prompt": "本机最后改的人设"})
        code, rec = req("POST", f"{a_base}/api/sync/reconcile", target)
        check("⑨ 登录对账：双向都动了且卡按新者胜",
              code == 200 and (rec or {}).get("pushed", 0) >= 1
              and "本机最后改的人设" in json.dumps(
                  req("GET", f"{b_base}/api/roles", token=token)[1], ensure_ascii=False), rec)
        _, rec2 = req("POST", f"{a_base}/api/sync/reconcile", target)
        check("⑨ 对账是幂等的（跑两遍第二遍 pushed=pulled=0）",
              isinstance(rec2, dict) and rec2.get("pushed") == 0 and rec2.get("pulled") == 0, rec2)
        _, audit_b = req("GET", f"{b_base}/api/audit?limit=40", token=token)
        blob_b = json.dumps(audit_b, ensure_ascii=False)
        check("⑨ 对面审计里有 sync_export 这扇门的记录（只记结构）",
              "sync_export" in blob_b and "本机最后改的人设" not in blob_b)
    finally:
        for proc, log in ((a, la), (b, lb)):
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], check=False)
            log.close()
        print("两个后端都已 terminate（自己 spawn 的 pid）")

    print("=" * 56)
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：{FAILS}")
        return 1
    print("✅ 上行/下行/对账两实例端到端全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
