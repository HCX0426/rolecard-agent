// roles 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。
//
// P1（2026-10-25）：`RoleCard` 改为**引用生成类型** —— `schema.gen.ts` 由后端 OpenAPI
// 契约生成（`npm run api:types`），后端改字段而前端没重新生成时，`tsc` 直接红。
// 手写那份字段清单从此是第二份事实，删掉。
//
// 生成类型与手写那份的差异（有意保留生成的那侧）：生成物把后端**全部**字段都列出来，
// 包括前端此前没声明的 `user_id` / `created_at` / `updated_at`（响应里本来就有，
// 此前只是类型层没承认）。前端只读用不到它们，多出来不碍事；反过来漏字段才是 bug。
import type { components } from "./schema.gen";

export type RoleCard = components["schemas"]["RoleCard"];

export interface KnowledgeScopes {
  scopes: string[];
}
