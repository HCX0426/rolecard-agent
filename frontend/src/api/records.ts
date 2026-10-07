// records 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。
// ReportRecord 的 indices 用的是知识域那份指标行 —— 跨文件的类型引用照原样写 import
// （不是把 IndexRow 挪过来凑齐一个域：一个类型只有一个家，谁用谁 import）。
import type { IndexRow } from "./knowledge";



export interface GenericRecord {
  id: string;
  domain: string;
  label: string;
  value_text: string | null;
  value_num: number | null;
  unit: string | null;
  note: string | null;
  created_at: string;
}

export interface ReportRecord {
  report_id: string;
  report_type: string;
  check_time: string;
  institution: string | null;
  note: string | null;
  indices: IndexRow[];
}
