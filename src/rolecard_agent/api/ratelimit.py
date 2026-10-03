"""请求节流（v2.4 公网硬化）：按**身份**给"贵"的写请求封顶。

## 为什么键是"身份"而不是"IP"

这台实例上真正会被烧掉的两样东西都跟着人走：**服务器资源**（一次 `/api/chat` 驱动整张
图 + SSE 长期占着 worker，上传解析还会拉起 OCR 与 embedding 子进程）与**你自己那份 key
的额度**（key 存在服务器那份 `model_provider` 里，A 打穿配额就是烧 B 的钱）。所以桶的
键是"这次请求是谁" —— IP 只在匿名时兜底（`AUTH_MODE=off` 的本机形态、或反代没传身份）。

## 为什么默认关（`RATE_LIMIT_PER_MINUTE=0`）

本产品的主形态是"一个人、一台机器"。今天加节流只会误伤主人自己的桌宠与脚本调用 ——
这条判断与 09-27 拍的那条一致：**限流保护的对象随"谁付钱"变**，不随"要不要做"变。
所以这一件与"第二个账号"同日落地：那天把它打开就行，不必再改代码。

## 三条边界（说清，免得被当成比实际更全的东西）

* **本机来源不设卡**（调用点在 `api/main.py` 的准入链里判 `is_loopback(origin)`）：桌面壳、
  控制台、脚本与探针全从 127.0.0.1 来 —— 与认证那条"本机来源永远放行"同一份信任模型。
  主人坐在键盘前不该被自己的机器挡在门外；公网那档要保护的是**远端那个人**。
* **只数写请求**（POST/PUT/PATCH/DELETE），且只数配置里点名的前缀。轮询读不进门 ——
  `GET /api/session/{tid}/turn` 是 0.8 秒一次的常驻读，把它算进来会当场把界面打死。
* 数的是**请求数**，不是**并发数**：它挡"猛刷"，挡不住"开 50 条 SSE 挂着不动"。
  并发闸要动线程池与长连接生命周期，是另一件事，**这一版没做**（不假装做了）。
* 计数在**进程内存**里：`run_api.py` 单进程（桌面壳自带后端也是）时是对的；哪天起多
  worker，额度会变成"每 worker 一份"，那时该换成共享计数（Redis 之类）。

与 `tests/unit/test_ratelimit.py` 配对：那条用例钉的就是上面这几条边界。
"""

from __future__ import annotations

import threading
import time

#: 会真正花掉资源的方法。读（GET/HEAD）一律不进门。
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 内存里最多留多少个桶。超了就按当前窗口清一遍 —— 公开部署下匿名来源的 IP 会一直变，
#: 不清就是一条只涨不落的字典（这条不是洁癖：这一版存在的理由就是"公网那档"）。
_MAX_KEYS = 4096


def paths_of(raw: str | None) -> tuple[str, ...]:
    """配置串 → 前缀元组。

    尾部斜杠统一去掉，于是 `/api/session/` 与 `/api/session` 一个意思；空项丢掉，
    所以 `RATE_LIMIT_PATHS=` （或全逗号）等于"什么都没管"，而不是"管住空前缀 = 全部"。
    """
    return tuple(p.strip().rstrip("/") for p in (raw or "").split(",") if p.strip())


def is_limited(path: str, method: str, prefixes: tuple[str, ...]) -> bool:
    """这条请求进不进桶。

    前缀按**路径段边界**匹配：`/api/session` 命中 `/api/session/...`，但**不**命中
    `/api/sessions`（复数是另一族端点，且都是读）。少了这个边界，`/api/session` 会把
    `/api/sessions/...` 一起吞掉 —— 那正好是界面最常打的那几条。
    """
    if method.upper() not in WRITE_METHODS:
        return False
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


class Limiter:
    """固定窗口计数器：每个键每分钟 `per_minute` 次配额，窗口按整分钟切。

    为什么用固定窗口而不是滑动窗口/令牌桶：这一层的目的是"别让一个人把机器打穿"，
    它得**解释得清、算得便宜**。固定窗口的已知代价是边界处可能过两倍额度（某分钟的
    最后几秒与下一分钟的头几秒各来一整桶）—— 对"防猛刷"够用，对"精确计费"不够用，
    而后者不是这里的目标（真计费看 `token_usage_day` 那本账）。
    """

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._window: dict[str, tuple[int, int]] = {}  # 键 -> (窗口号, 本窗口已用)
        # 读-改-写必须原子（`R102-72`）：`hit` 从 FastAPI 线程池被并发调用，而
        # "get 计数 → 判断 → set 计数+1"之间会被切走 —— 实测（`build/` 探针，16 线程 ×
        # 2000 发，切换间隔压到 1µs）**丢了 70% 以上的计数**，方向恰恰是最坏的那个：
        # 丢计数 = 少算 = 配额被并发打穿（限流转而过宽），而且额度越大丢得越多。
        # 锁只包这一段临界区；`per_minute <= 0` 的短路在锁外（默认关闭时连锁都不碰）。
        self._lock = threading.Lock()

    def hit(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """记一次并回答 `(放行?, 还要等几秒)`。`per_minute <= 0` = 关着，永远放行。

        `now` 可注入：用例不该靠 `sleep` 去跨窗口（那会让"窗口切换"这条分支没人测）。
        """
        if self.per_minute <= 0:
            return True, 0
        stamp = time.monotonic() if now is None else now
        bucket = int(stamp // 60)
        with self._lock:
            window, used = self._window.get(key, (bucket, 0))
            if window != bucket:
                window, used = bucket, 0
            if used >= self.per_minute:
                # 窗口余量取整后可能是 0（比如离切换只剩 10 毫秒），但 `Retry-After: 0`
                # 等于让客户端立刻重试 —— 那正是要挡的行为，所以下限给 1 秒。
                return False, int(60 - (stamp % 60)) or 1
            if len(self._window) >= _MAX_KEYS:
                # 清掉已经翻篇的窗口；当前窗口的桶留着（正在被数的那批不因清理而清零）。
                self._window = {k: v for k, v in self._window.items() if v[0] == bucket}
            self._window[key] = (window, used + 1)
            return True, 0


def bucket_key(actor_id: str, *, anonymous: bool, origin: str) -> str:
    """这次请求记在谁的账上。

    认证过的按**身份**（`Actor.id`：Basic 的用户名 / API Key 的前 4 位）—— 这是"谁付钱"
    那一问的答案。匿名（本机 `AUTH_MODE=off`、或 `auto` 档下的回环来源）按来源 IP 兜底：
    本机形态下大家共用 `127.0.0.1` 一个桶，正合"这台机器就一个人用"。
    """
    return f"ip:{origin}" if anonymous else f"id:{actor_id}"
