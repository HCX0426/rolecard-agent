// knowledge 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


import type { components } from "./schema.gen";

/** 指标行：随它的域模型一起退役（2026-11-07）。通用记录通路用的是 `GenericRecord`
 * 的标签/数值/文本三格，不再有"指标行"这一层 —— 这里保留别名只为让既有引用编译过，
 * 指向 `DomainRecordView`。 */
export type IndexRow = components["schemas"]["DomainRecordView"];

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

