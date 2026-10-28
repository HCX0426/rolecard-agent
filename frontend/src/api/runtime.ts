// runtime 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


/** 运行环境（env + DB 覆盖）的展示项。kind=ro 表示不可在线修改；secret 永不回明文。 */
import type { components } from "./schema.gen";

export type RuntimeItem = components["schemas"]["RuntimeSettingsViewGroupsItem_itemsItem"];

export type RuntimeGroup = components["schemas"]["RuntimeSettingsViewGroupsItem"];

export type RuntimePayload = components["schemas"]["RuntimeSettingsViewRoot"];
