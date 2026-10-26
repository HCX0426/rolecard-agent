// extensions 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


/** 已注册域的清单行（2026-10-24 域常开：GET /api/domains，原 /api/plugins 退役）。
 *  域常开后没有 enabled 概念——工具可见性的唯一边界是角色卡白名单。 */
import type { components } from "./schema.gen";

export type DomainInfo = components["schemas"]["DomainsViewItem"];

export interface ToolEntry {
  name: string;
  description: string;
}

export interface ToolCatalog {
  kernel: ToolEntry[];
  domains: Record<string, ToolEntry[]>;
}
