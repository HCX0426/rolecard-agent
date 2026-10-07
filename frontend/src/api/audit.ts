// audit 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


export interface AuditRow {
  // 后端明确带下来的唯一 id（`R102-17`）：展开态的数据锚点就是它 —— 从前拿
  // (ts,action) 当键，同一秒同动作的 5 条一起开。
  id: number;
  ts: string;
  actor: string;
  action: string;
  target: string | null;
  detail_json: string | null;
}
