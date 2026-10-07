// memory 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


/**
 * 一次模型调用的账本（`core/memory_distill.py::_report`）。
 *
 * `tokens` 可能是 null：模型没报 usage 时后端**不编一个数**，界面也就不显示成本，
 * 而不是显示 0（0 会被读成"这次没花钱"）。
 */
export interface DistillReport {
  added: number;
  updated: number;
  /** 有几条字面上看着像同一件事。只是提示：合并要人在记忆卡上发起「整理记忆」。 */
  similar: number;
  merged: number;
  invalidated: number;
  noop: number;
  skipped: number;
  detail: string;
  tokens: number | null;
  before: number | null;
  after: number | null;
}

export interface DistillOutcome {
  report: DistillReport;
  /** 距上次提取又攒了几轮（0 = 刚提取过）。 */
  turns_since: number;
}

/** 整理完顺手回一份当前桶的记忆视图：界面不用再发一次 GET 就能刷新列表。 */
export interface ConsolidateOutcome extends DistillOutcome {
  enabled: boolean;
  role_id: string | null;
  content: string;
  items: {
    id: number;
    text: string;
    source: string;
    pinned: boolean;
    hit_count: number;
    /** 显著性档位（0 次要 / 1 一般 / 2 要紧）—— 与记忆面板那三档下拉同源。 */
    importance: number;
    last_hit_at: string | null;
    created_at: string | null;
  }[];
  active_count: number;
  limit: number;
  over_limit: boolean;
  extract_turns: number;
}
