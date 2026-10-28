// services 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


/** 「服务」页的载荷契约（`R102-22`：从前这 23 个键全部声明在 ServicesPanel.tsx 里，
 * `api/index.ts` 头部自称"与后端契约一一对应"却看不见它们 —— 改一个字段名，契约测试与这里两处
 * 都不红，界面静默显示"—"。键面 = `GET /api/services` 的实测响应。 */
import type { components } from "./schema.gen";

export type ServiceEndpoint = components["schemas"]["ServicesViewServicesItem_candidatesItem"];

export type ServiceCategoryView = components["schemas"]["ServicesViewServicesItem"];

export type ServicesView = components["schemas"]["ServicesViewRoot"];
