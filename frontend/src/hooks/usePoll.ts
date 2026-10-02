import { useEffect, useRef } from "react";

/** 轮询的唯一样板（`R102-61`）：立即打一拍 + `setInterval` + cleanup 清表。
 *
 * 为什么要有这一条：同形状的 effect 从前抄在 4 处（App 的未读/待批、PetPage 的收件箱与
 * 角色表），**防竞态知识只写了一半** —— App 两处带 `cancelled` 旗（卸载后不再打拍），
 * PetPage 两处没有，下一个照抄的人抄到哪半看运气。这里统一三件事：
 * 立即一拍、卸载后不再打拍、`enabled` 门（App 的"抽屉开着不打扰"语义）。
 * fn 经 ref 恒取最新闭包：调用方传行内箭头也安全，不要求 useCallback。
 *
 * 与 `useSessionMirror`（setTimeout 自续、带退避）刻意不同族：那一个要的是"上一拍收完
 * 才排下一拍"，这里要的是固定节奏 —— 各留注释互指，别合并。
 */
export function usePoll(
  fn: (isLive: () => boolean) => void | Promise<void>,
  intervalMs: number,
  enabled = true,
): void {
  const latest = useRef(fn);
  useEffect(() => {
    latest.current = fn;
  });
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      void Promise.resolve(latest.current(() => !cancelled)).catch(() => {
        /* 轮询的失败由调用方自己静默：后端没起/网络抖动不值得打扰用户 */
      });
    };
    tick();
    const timer = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [enabled, intervalMs]);
}
