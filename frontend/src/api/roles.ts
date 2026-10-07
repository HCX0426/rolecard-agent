// roles 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


export interface RoleCard {
  role_id: string;
  role_name: string;
  system_prompt: string;
  temperature: number;
  model_name: string | null;
  tool_whitelist: string[] | null;
  exemplars: { user: string; assistant: string }[] | null;
  knowledge_scopes: string[] | null;
  description: string | null;
  is_builtin: boolean;
  /** 角色主动开口资格（架构计划 B）：还需全局 REACHOUT_ENABLED 开着才生效。 */
  reachout_enabled?: boolean;
  /** 关系驱动主动开口（架构总览 §5）：回忆触发开关，默认开。 */
  recall_enabled?: boolean;
  /** 关系驱动主动开口（架构总览 §5）：时段规律触发开关，默认开。 */
  time_pattern_enabled?: boolean;
  /** 「关系数值到阈值就想开口」这一档的开关，默认开。关掉才轮得到后面几档（09-26 轮 R26-23）。 */
  affinity_enabled?: boolean;
  /** 文件事件触发（架构总览 §5）：该角色可否被任务目录变化触发，默认开。 */
  file_watch_enabled?: boolean;
  /** 收件箱自动保留条数：0 = 不自动删（默认）；N>0 = 只留最近 N 条投递记录。 */
  reachout_keep?: number;
  /** 桌宠形象包 id（空/缺省 = 没配过 → 落默认包）。可选值来自 `/api/pets`，见 `pets/registry.ts`。 */
  pet_pack?: string;
}

export interface KnowledgeScopes {
  scopes: string[];
}
