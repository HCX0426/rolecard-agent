// reachouts 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


//: 未读红点的轮询节奏。**后端没有推送通道，如实降级为轮询**。从前是 10s、且在三个地方各写
//: 一遍字面量；收成一处并按 09-26 轮 S-4 的结论调到 3s —— 主动开口本身是分钟级事件，
//: 3s 与 10s 的差别只到"勉强能感知"，而上 SSE 要动三个文件一到两天再加断线重连，不值。
export const UNREAD_POLL_MS = 3_000;

/** 角色主动开口（收件箱）条目：独立于对话历史，未读/已读状态由 state 表达。 */
export interface ReachoutRow {
  id: number;
  role_id: string;
  role_name: string | null;
  text: string;
  state: "unread" | "read";
  created_at: string;
  /** 该角色的"主动会话"线程 id：点进去能翻历史、能直接回话。
   *  null = 这条消息还没有对应会话（本功能上线前落库的老消息）→ 界面只给"标记已读"。 */
  thread_id: string | null;
}

/** 一个"有资格主动开口"的角色此刻的状态（`S-8`）。 */
export interface QuietStatus {
  role_id: string;
  role_name: string;
  /** 闸门那句原文（「距上次说话不足 66 分钟，她连着 1 条没被回已退避」…）。
   *  null = **她现在随时能开口** —— 是肯定句，不是"算不出来"，界面要照这个口径写。 */
  why: string | null;
  /** 下一次大约能开口的时刻（ISO，带时区）。null = 这不是"等一会儿就好"的事
   *  （未读封顶要他回话或划掉、正在对话要等这一轮跑完）。 */
  next_ok_at: string | null;
  /** 她连着几条开口而对方没回（退避的指数）。 */
  streak: number;
  unread: number;
}

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

export interface TimelineEvent {
  kind: TimelineKind;
  /** "YYYY-MM-DD HH:MM:SS"（库里原文，定宽 ⇒ 界面直接切片，不再解析一遍时区）。 */
  at: string;
  text: string;
  /** 只有会话锚点带 `start` / `recent`；`correct` 只出现在"更正"那一条。 */
  verb: "start" | "recent" | "correct" | null;
  /** "更正"那条被作废的旧事实原文：划掉显示，这是 §3「失效不删」第一次被用户看见的地方。 */
  from_text: string | null;
  /** 可跳转的会话；null = 没有（记忆条目、上线前的老消息）⇒ 不给死链。 */
  thread_id: string | null;
  ref_id: number;
}

export interface TimelinePage {
  role_id: string;
  items: TimelineEvent[];
  next_cursor: string | null;
  /** 某个源扫到了上限：只说明"这里可能还有更早的"，不承诺总量（数全量是归档功能的事）。 */
  truncated: boolean;
}
