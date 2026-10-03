"""v2.4 限流（`api/ratelimit.py` + 准入链里那一处调用）的五条边界。

这一件与"第二个账号"同日落地，所以它自己的**默认**就是第一条要钉的东西：`=0` 时
本机单人形态逐字不变。其余四条对应设计稿里写明的那几个取舍：

  2. **只数写请求**，且只数点名的前缀 —— `GET /api/session/{tid}/turn` 是 0.8 秒一次的
     常驻读，把它算进去会当场把界面打死；
  3. **前缀按路径段边界匹配**：`/api/session` 不许吞掉 `/api/sessions`（复数是另一族）；
  4. **窗口翻篇就清零**，而 `Retry-After` 永远 ≥1（`0` 等于让客户端立刻重试，
     那正是要挡的行为）；
  5. **桶键跟着身份**：认证过按 `Actor.id`（谁付钱），匿名按来源 IP 兜底。

Traceability: 架构总览 §4「尚未关闭 / 待办」（v2.4 公网部署：限流）。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.api.ratelimit import Limiter, bucket_key, is_limited, paths_of

# -- 纯函数那一层 -------------------------------------------------------------------


def test_paths_of_normalizes_and_empty_means_nothing_managed() -> None:
    assert paths_of("/api/chat, /api/session/") == ("/api/chat", "/api/session")
    assert paths_of("") == () and paths_of(" , ,") == ()


def test_only_write_methods_and_only_listed_prefixes_enter_the_bucket() -> None:
    prefixes = paths_of("/api/chat,/api/session")
    assert is_limited("/api/chat", "POST", prefixes)
    assert is_limited("/api/session/s1/upload", "POST", prefixes)
    assert not is_limited("/api/session/s1/turn", "GET", prefixes), "常驻轮询读不许进门"
    assert not is_limited("/api/session/s1/messages", "GET", prefixes)
    assert not is_limited("/api/reachouts/read-all", "POST", prefixes), "没点名的前缀不管"


def test_the_prefix_matches_on_segment_boundary_not_substring() -> None:
    """`/api/session` 不许吞掉 `/api/sessions` —— 差一个字母，一个是写、一个是界面常读。"""
    prefixes = paths_of("/api/session")
    assert is_limited("/api/session/s1/messages/edit", "POST", prefixes)
    assert not is_limited("/api/sessions", "POST", prefixes)
    assert not is_limited("/api/sessions/abc", "DELETE", prefixes)


def test_limiter_counts_down_within_the_window_and_resets_on_the_next_one() -> None:
    lim = Limiter(3)
    t = 1000.0  # 任意时刻：窗口号 = int(t // 60)
    assert [lim.hit("k", now=t + i)[0] for i in range(4)] == [True, True, True, False]
    ok, retry = lim.hit("k", now=t + 1)
    assert not ok and retry >= 1, "Retry-After 不许是 0（0 = 让客户端立刻重试）"
    # 下一分钟：额度回来
    assert lim.hit("k", now=t + 60)[0] is True
    # 另一个键不受影响（额度是**每身份**的）
    assert lim.hit("other", now=t + 1)[0] is True


def test_limiter_is_off_when_quota_is_zero_and_never_counts() -> None:
    lim = Limiter(0)
    assert all(lim.hit("k", now=0.0)[0] for _ in range(50))
    assert lim._window == {}, "关着的时候不该留下任何桶（也就不该有内存增长）"


def test_bucket_key_follows_the_identity_and_falls_back_to_ip() -> None:
    assert bucket_key("alice", anonymous=False, origin="203.0.113.9") == "id:alice"
    assert bucket_key("anonymous", anonymous=True, origin="203.0.113.9") == "ip:203.0.113.9"


def test_concurrent_hits_do_not_lose_counts() -> None:
    """并发下计数不许丢（`R102-72`）：丢计数 = 少算 = 配额被并发打穿，限流转而过宽。

    复现手法与取证探针同形：**把线程切换间隔压到 1µs**（不加这一句，纯 Python 的紧循环
    在一整个循环里都不会被切走 —— 那个"无丢失"是假的，探针先量到过一次），然后 16 条
    线程同时打同一个键，配额取总量的一半。判据是**放行数恰好等于配额**：无锁时读-改-写
    被切走、计数丢七成，放行会远多于配额（探针实测配额 32001 时桶里只剩 6.6k~9.7k）。
    """
    per_minute = 200
    runners = 16
    per_thread = 40
    lim = Limiter(per_minute)
    start = threading.Barrier(runners)
    outcomes: list[bool] = []
    collect = threading.Lock()

    def worker() -> None:
        local: list[bool] = []
        start.wait()  # 尽量让所有线程真的同时打
        for _ in range(per_thread):
            local.append(lim.hit("id:same", now=1000.0)[0])
        with collect:
            outcomes.extend(local)

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(0.000001)
    try:
        threads = [threading.Thread(target=worker) for _ in range(runners)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old_interval)

    total = runners * per_thread
    allowed = sum(outcomes)
    assert allowed == per_minute, f"放行 {allowed} ≠ 配额 {per_minute}（丢计数就是放多了）"
    assert total - allowed == total - per_minute  # 其余全被拒：一个不丢、一个不多


# -- 准入链那一层 -------------------------------------------------------------------


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    per_minute: int,
    paths: str = "/api/chat,/api/session",
    peer: tuple[str, int] = ("203.0.113.9", 50000),
) -> TestClient:
    """一个"公网来源"的客户端：`AUTH_MODE=on` + 使用者凭据，对端是外部 IP。

    对端刻意用文档地址段（203.0.113.x）而不是 127.0.0.1：本机回环在这个产品里从不设卡，
    要验的就是"远端那个人"这一路。
    """
    monkeypatch.setenv("AUTH_MODE", "on")
    monkeypatch.setenv("AUTH_CREDENTIALS", "alice:pw")
    monkeypatch.setenv("AUTH_API_KEYS", "k-abcdef")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", str(per_minute))
    monkeypatch.setenv("RATE_LIMIT_PATHS", paths)
    return TestClient(create_app(sqlite_path=tmp_path / "app.db"), client=peer)


def test_over_quota_gets_a_readable_429_with_retry_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """超额度 = 429 + `Retry-After`，**读请求照旧**（限的是"太密"，不是"这个人"）。

    `/api/chat` 这里不真发（那要一个模型），只发它会先撞上限流 —— 正是要顺序正确：
    挡住一个"贵"请求必须在它开始驱动图**之前**。
    """
    c = _client(monkeypatch, tmp_path, per_minute=2)
    auth = ("alice", "pw")
    assert c.post("/api/chat", json={"thread_id": "t", "text": "hi"}, auth=auth).status_code != 429
    assert c.post("/api/chat", json={"thread_id": "t", "text": "hi"}, auth=auth).status_code != 429
    blocked = c.post("/api/chat", json={"thread_id": "t", "text": "hi"}, auth=auth)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert "RATE_LIMIT_PER_MINUTE" in blocked.text, "文案要指路，别让人去翻代码"
    # 同一个人的读请求不受影响（额度只管受管的写路径）
    assert c.get("/api/roles", auth=auth).status_code == 200


def test_loopback_is_never_limited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """本机来源免限流：桌面壳、控制台、脚本与探针全从 127.0.0.1 来。

    这条不是"漏了"，是刻意的 —— 与认证那条"本机来源永远放行"同一份信任模型：主人坐
    在键盘前不该被自己的机器挡在门外。公网部署下要保护的是**远端那个人**。
    """
    monkeypatch.setenv("AUTH_MODE", "auto")
    monkeypatch.setenv("AUTH_CREDENTIALS", "alice:pw")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("RATE_LIMIT_PATHS", "/api/chat")
    c = TestClient(create_app(sqlite_path=tmp_path / "app.db"), client=("127.0.0.1", 50000))
    codes = [
        c.post("/api/chat", json={"thread_id": "t", "text": "hi"}).status_code
        for _ in range(4)
    ]
    assert 429 not in codes, f"本机来源被限流了：{codes}"


def test_two_identities_do_not_share_a_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 打满不许伤到 B —— 这正是"key 跟着身份走"之后要保住的那条边界。"""
    monkeypatch.setenv("AUTH_MODE", "on")
    monkeypatch.setenv("AUTH_CREDENTIALS", "alice:pw,bob:pw")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("RATE_LIMIT_PATHS", "/api/chat")
    c = TestClient(create_app(sqlite_path=tmp_path / "app.db"), client=("203.0.113.9", 50000))
    body = {"thread_id": "t", "text": "hi"}
    assert c.post("/api/chat", json=body, auth=("alice", "pw")).status_code != 429
    assert c.post("/api/chat", json=body, auth=("alice", "pw")).status_code == 429
    assert c.post("/api/chat", json=body, auth=("bob", "pw")).status_code != 429