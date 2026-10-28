// reachouts 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


//: 未读红点的轮询节奏。**后端没有推送通道，如实降级为轮询**。从前是 10s、且在三个地方各写
//: 一遍字面量；收成一处并按 09-26 轮 S-4 的结论调到 3s —— 主动开口本身是分钟级事件，
//: 3s 与 10s 的差别只到"勉强能感知"，而上 SSE 要动三个文件一到两天再加断线重连，不值。
import type { components } from "./schema.gen";

export const UNREAD_POLL_MS = 3_000;

/** 角色主动开口（收件箱）条目：独立于对话历史，未读/已读状态由 state 表达。 */
export type ReachoutRow = components["schemas"]["ReachoutsViewItemsItem"];

/** 一个"有资格主动开口"的角色此刻的状态（`S-8`）。 */
export type QuietStatus = components["schemas"]["ReachoutsViewQuietItem"];

export interface ReachoutsPage {
  items: ReachoutRow[];
  unread: number;
  /** 每个角色各有几条没读 —— 后端算一次，铃铛/桌宠都读这份，不再各自 filter 一遍。 */
  unread_by_role?: Record<string, number>;
  /** 挂起的任务目录变更条数（文件事件触发开启时 >0 = 角色正攒着素材）。 */
  file_watch_pending?: number;
  /** 收件箱折叠窗口（天，1/3/7）：同一角色在一个窗口里的开口折成一行。 */
  merge_days?: number;
  /** 每个开了主动资格的角色"此刻为什么静默"（`S-8`）。跟着这一份负载走：抽屉本来
   *  每 3 秒就在读它，为了一句话再开一条 `/status` 等于多一次轮询 + 一个新的时刻源。 */
  quiet?: QuietStatus[];
}

export type TimelineKind = "reachout" | "memory" | "memory_correct" | "thread";

export type TimelineEvent = components["schemas"]["RoleTimelineViewItemsItem"];

export type TimelinePage = components["schemas"]["RoleTimelineViewRoot"];
