/**
 * 错误 → 人话的唯一出口（2026-10-04 审查快照「前端 74 处手写 (e as Error).message」那条）。
 *
 * 为什么要有一层而不是直接 `e.message`：错误是 ApiError 时，`status` 才是语义 ——
 * 手写 message 把它整个丢掉。status 分三档：
 *   * `0` —— 请求根本没到后端（断网 / 后端没起）。api 层包出的 message 里带着底层
 *     英文原文（"Failed to fetch" 之类），透给用户是噪音，这里换成一句可行动的话；
 *   * `409`（占用）/ `429`(限流) —— 服务端 detail 本来就写了"等 N 秒再试"这类指引，
 *     **原样透传**，别在客户端再包一层废话；
 *   * 其余 —— 服务端 detail 原样透传。
 * 不是 ApiError 的（编程错误 / 非导出层的原生异常）取 message，空则给 fallback。
 */
import { ApiError } from "../api";

export function describeError(e: unknown, fallback = "操作失败"): string {
  if (e instanceof ApiError) {
    if (e.status === 0) return "网络错误：后端连不上，请确认服务已启动";
    return e.message || fallback;
  }
  if (e instanceof Error && e.message.trim()) return e.message;
  return fallback;
}
