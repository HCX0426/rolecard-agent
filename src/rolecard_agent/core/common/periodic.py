"""固定间隔后台任务的**公共骨架** —— 循环、置旗、join、错误留痕（2026-10-24，2a 刀）。

为什么抽出来：`ReachoutScheduler` 的 `start / stop / _loop` 三件是"daemon 线程 + 协作式
停止 + 停机竞态守则"的完整一次实现，而 2026-10 的渠道适配器（`features/channels/`）是
第二个同形状宿主 —— 循环骨架再抄一遍，就是又一条"漏过一次的缝"（线程池上下文传播那条
缝的教训同源）。骨架只管三件事：

  * 按 tick 间隔循环（`Event.wait(interval)` —— 置旗后**最多**再等一个间隔就醒，
    而不是睡满整个间隔才看到旗）；
  * 单次 tick 抛错**不打死循环**，但一定带异常文本留痕（只有 `fatal: False` 没有原文的
    事件，故障时在日志里查不到任何线索 —— `reachout_tick_error` 那条注释的教训）；
  * `stop()` 置旗后 **join 等线程真退出**（R26 那族停机竞态：一次 tick 里有真模型调用
    与检查点写库，不 join 就是让它往已关的连接上写）。

每拍做什么（`tick_once`）与停机时的额外收尾（子类 override `stop` 先 `super()` 再关
自己的池）都是子类的事。**第一次 start 只起一条线程**：同一名任务被 start 两次就是两条
循环（"同一句主动消息冒两遍"那族症状），骨架从形状上不给这个机会 —— 重复 start 直接
覆盖句柄前先判存活，正在跑时是 no-op。
"""

from __future__ import annotations

import threading
from typing import Any

from rolecard_agent.base.observability import TraceEvent, Tracer


class PeriodicTask:
    """按固定间隔跑 `tick_once` 的后台 daemon 任务。子类实现 `tick_once` 即可。"""

    def __init__(
        self,
        *,
        name: str,
        tick_interval: float,
        tracer: Tracer,
        error_event: str,
        node: str,
        join_timeout: float = 35.0,
    ) -> None:
        self._name = name
        self._tick_interval = tick_interval
        self._tracer = tracer
        self._error_event = error_event
        self._node = node
        self._join_timeout = join_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        """起调度线程。已在跑时是 no-op（重复 start = 两条循环，见模块 docstring）。"""
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        thread.start()
        self._thread = thread

    def stop(self, *, join_timeout: float | None = None) -> None:
        """停调度：置旗后**等线程真的退出**。

        join 超时默认给足一个 tick + 余量（构造参数 `join_timeout`）；`join_timeout`
        覆写参数是**测试注入口**——等不起默认时长的用例传小值，生产路径不传。
        超时后仍强收（daemon 线程随进程走），至少给了它一次干净收场的机会。
        tick 内的协作式取消走 `_stopping()`：每次**重活前**查旗，正在干的那一拍主动放弃。
        """
        self._stop.set()
        thread = self._thread
        timeout = self._join_timeout if join_timeout is None else join_timeout
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _stopping(self) -> bool:
        """tick 内部的协作式取消点：True = 该收手了（正在干的这一拍主动放弃）。"""
        return self._stop.is_set()

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_interval):
            try:
                self.tick_once()
            except Exception as exc:  # noqa: BLE001 - 调度循环绝不能被一个错误打死
                # 打死不等于闭嘴：异常文本必须跟着事件走，否则这类故障在日志里查不到线索。
                self._tracer.emit(
                    TraceEvent(
                        event=self._error_event,
                        node=self._node,
                        detail={"fatal": False, "error": f"{type(exc).__name__}: {exc}"},
                    )
                )
            if self._stop.is_set():
                break  # stop() 置旗后不再开下一轮 tick：join 才等得到头

    def tick_once(self) -> Any:  # pragma: no cover - 形状声明，子类必覆写
        """一拍要做的事。签名与返回值归子类定 —— 骨架只保证"异常不杀死循环"。"""
        raise NotImplementedError
