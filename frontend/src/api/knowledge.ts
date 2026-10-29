// knowledge 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


import type { components } from "./schema.gen";

export type IndexRow = components["schemas"]["RecordsViewItemsItem_indicesItem"];

export type KnowledgeScope = components["schemas"]["KnowledgeViewItem"];

/** 检索延迟分位（每阶段，单位 ms；无样本时各字段为 null）。 */
export interface RagStageMs {
  embed_ms: number | null;
  vector_ms: number | null;
  rerank_ms: number | null;
  total_ms: number | null;
}

export interface RagMetrics {
  samples: number;
  rerank_enabled: boolean;
  embedder: string;
  p50: RagStageMs;
  p95: RagStageMs;
  p99: RagStageMs;
}

/** 结构化抽取响应里 written[] 的元素 —— 运行时只回 4 键（index_name/index_value/value_text/unit）。

 从前手写这份抄成了 9 键的库内指标行（`RecordsViewItemsItem_indicesItem`），而抽取端点
 回的是窄投影（写库的那份 8 键 payload 不出门）—— 第四批手模把它拉回运行时事实。 */
export type ExtractWritten = components["schemas"]["ExtractWrittenModel"];

/** 抽取时没能通过校验 / 两次识别不一致的项 —— 交给人确认，未写库 */
export type ExtractConflict = components["schemas"]["ExtractConflictModel"];

export type ExtractResult = components["schemas"]["ExtractOutcomeView"];
