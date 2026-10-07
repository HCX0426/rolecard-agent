// knowledge 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


export interface IndexRow {
  index_id: string;
  index_name: string;
  index_value: number | null;
  value_text: string | null;
  unit: string | null;
  ref_range: string | null;
  is_verified: number | boolean;
  source: string;
  raw_text: string | null;
}

export interface KnowledgeScope {
  scope: string;
  chunks: number;
  sources: string[];
  embedder: string;
}

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

/** 结构化抽取：写入档案的指标行 */
export interface ExtractWritten {
  index_name: string;
  index_value: number | null;
  value_text: string | null;
  unit: string | null;
}

/** 抽取时没能通过校验 / 两次识别不一致的项 —— 交给人确认，未写库 */
export interface ExtractConflict {
  index_name: string;
  reason: string;
  primary: Record<string, unknown> | null;
  verify: Record<string, unknown> | null;
}

export interface ExtractResult {
  mode: string; // cross（双模型）| self（同模型复查，弱校对）| off
  report_type: string;
  check_time: string;
  institution: string | null;
  written: ExtractWritten[];
  conflicts: ExtractConflict[];
  notes: string[];
  /** 降级原因：no_model / no_text / already_extracted */
  skipped?: string;
  detail?: string;
  report_id?: string;
}
