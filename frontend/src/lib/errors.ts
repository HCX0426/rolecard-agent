/**
 * 错误 → 人话的唯一出口（2026-10-04 审查快照「前端 74 处手写 (e as Error).message」那条）。
 *
 * 为什么要有一层而不是直接 `e.message`：错误是 ApiError 时，`status` 才是语义 ——
 * 手写 message 把它整个丢掉。status 分档：
 *   * `0` + `kind = "network"` —— 请求根本没到后端（断网 / 后端没起）。api 层包出的
 *     message 里带着底层英文原文（"Failed to fetch" 之类），透给用户是噪音，这里换成
 *     一句可行动的话；
 *   * `0` + `kind = "timeout"` —— **原样透传**，别改写。从前这一档与上一档共用
 *     `status = 0`，于是这里对超时也说"请确认服务已启动"，说的是**反话**：上传/抽取
 *     到点时后端仍在跑、而且已经把文件落了盘（`api/index.ts:74-78` 立论写得很清楚），
 *     让用户去确认服务 = 催他重启并照着假失败再点一次。api 层那句"那一端可能仍在
 *     处理：稍后刷新看看结果"本来就是为这一档写的，是被这里盖掉的；
 *   * `409`（占用）/ `429`(限流) —— 服务端 detail 本来就写了"等 N 秒再试"这类指引，
 *     原样透传，别在客户端再包一层废话；
 *   * 其余 —— 服务端 detail 原样透传。
 * 不是 ApiError 的（编程错误 / 非导出层的原生异常）取 message，空则给 fallback。
 *
 * `status = 0` 而 `kind` 缺失时**也原样透传**：分不清是超时还是断网，就不赌那一句
 * "请确认服务已启动"（赌错的代价就是说反话）。抛 ApiError(0, …) 的地方必须标 kind，
 * 这条由 `scripts/consistency` 的「status=0 的 ApiError 必带 kind」断言把守。
 */
import { ApiError } from "../api";

export function describeError(e: unknown, fallback = "操作失败"): string {
  if (e instanceof ApiError) {
    if (e.status === 0 && e.kind === "network") return "网络错误：后端连不上，请确认服务已启动";
    return e.message || fallback;
  }
  if (e instanceof Error && e.message.trim()) return e.message;
  return fallback;
}
