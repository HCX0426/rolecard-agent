// memory 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


/**
 * 一次模型调用的账本（`core/memory_distill.py::_report`）。
 *
 * `tokens` 可能是 null：模型没报 usage 时后端**不编一个数**，界面也就不显示成本，
 * 而不是显示 0（0 会被读成"这次没花钱"）。
 */
import type { components } from "./schema.gen";

export type DistillReport = components["schemas"]["MemoryReportView"];

export type DistillOutcome = components["schemas"]["DistillOutcomeView"];

/** 整理完顺手回一份当前桶的记忆视图：界面不用再发一次 GET 就能刷新列表。

 * 第四批起指向后端手模：旧写法 `extends DistillOutcome` 继承来一个后端**从不返回**
 * 的 `turns_since` 必填键（它只属于会话提取那条路），items[] 也只手抄了 8/13 个键——
 * 契约对账把这些谎言摆出来了。 */
export type ConsolidateOutcome = components["schemas"]["MemoryConsolidatedView"];

/** 记忆面板的整包响应（R1031-30）：GET/PUT `/api/settings/memory` 的精确契约。
 *  从前这份住在 MemoryPage 的手抄里（迁移尺扫不到 pages/）—— 后端上精确手模
 *  `SettingsMemoryView` 之后，这里以别名收编：一个事实只住一个地址。 */
export type MemoryPayload = components["schemas"]["SettingsMemoryView"];

/** 记忆一行（13 键恒有，见后端手模 `MemoryRowView`）。 */
export type MemoryItem = components["schemas"]["MemoryRowView"];
