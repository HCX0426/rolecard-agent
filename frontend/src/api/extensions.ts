// extensions 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。

/** 已注册域的清单行（2026-10-24 域常开：GET /api/domains，原 /api/plugins 退役）。
 *  域常开后没有 enabled 概念——工具可见性的唯一边界是角色卡白名单。
 *
 *  2026-11-07：出厂不再带任何代码侧的域，采样器于是采不到"带字段的域行"样本，
 *  生成的 `DomainsViewItem` 退化成 `additionalProperties: true`（一个空对象）。
 *  这里按 `/api/domains` 的**运行时真相**手写两格 —— 后端 `list_domains` 恒给这两个键，
 *  与采样期有没有域无关。 */
export interface DomainInfo {
  id: string;
  display_name: string;
}

/** 工具目录里的一行（内核与域两类共用这个形状：name + description + 可选 domain）。
 *
 *  同上：域分组为空之后，`ToolsCatalogDomains_healthItem`（那个带域名的生成类型）没了，
 *  真实形状就是这三格。 */
export interface ToolEntry {
  name: string;
  description: string;
  domain?: string;
}

export type ToolCatalog = {
  kernel: ToolEntry[];
  domains: Record<string, ToolEntry[]>;
};
