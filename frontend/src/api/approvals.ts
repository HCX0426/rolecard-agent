// approvals 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


export type ApprovalStatus = "pending" | "approved" | "rejected" | "done";

export interface ApprovalResult {
  exit_code: number | null;
  output: string;
  duration_ms: number;
  output_bytes: number;
}

export interface ApprovalRow {
  id: number;
  command: string;
  cwd: string | null;
  role_id: string | null;
  role_name: string | null;
  thread_id: string | null;
  status: ApprovalStatus;
  result: ApprovalResult | null;
  /** 这条待批下发的**一次性决定令牌**：批准/拒绝必须原样带回（后端读完即清空）。
   *  它不是登录凭据 —— 单机形态本来就不登录；它证明的是"这一条你确实看到过"。 */
  decide_token: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApprovalsPage {
  items: ApprovalRow[];
  /** 待批（pending）条数：侧栏红点计数用，不是"未读"，所以不叫 unread。 */
  pending: number;
}
