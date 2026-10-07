// sessions 这一族的后端契约类型（快照 P3-1 第四刀从 `api/index.ts`
// 按路由域搬出，**正文逐字未改**，连注释都跟着它讲的那个名字走）。
// `index.ts` 以 `export *` 再导出本文件：消费者的 `from "../api"` 一字不动，
// 住址只活在实现里（数一份清单就守一份清单，facade 用通配是不给第二份清单留门）。


export interface ThreadRow {
  thread_id: string;
  title: string | null;
  role_id: string;
  role_name: string | null;
  updated_at: string;
  /** 会话级对话模式（后端返回有效值：会话覆盖 or 全局默认）。 */
  agent_mode?: string;
  /** 这条是不是"那个角色的固定线"（`s_proactive_<role>`）。旗标由后端给 ——
   *  那个 id 形状的事实归 `core/reachout/inbox.py`，前端自己拼就等于第二个真相源。 */
  is_proactive?: boolean;
  /** 这条线程一个字都还没写过。侧栏用它把空白线程藏起来（旧实现拿"有没有标题"猜，
   *  而重命名过的空线程、以及深链刚建的线程都会猜错）。 */
  is_blank?: boolean;
}

export interface MessageRow {
  role: "user" | "assistant" | "tool";
  content: string;
  /** 消息在 checkpoint 中的寻址 id：编辑 / 删除按它定位（LangGraph RemoveMessage）。 */
  id?: string;
  /** 思考过程（仅思考模型；随 checkpoint 一起回放，因此刷新后仍在）。 */
  reasoning?: string;
  /** 这一轮是**被叫停**的半句（随 checkpoint 回放，R26-13 尾）——刷新后仍标得出"没说完"。
   *  旧消息与正常收尾都没有这个键。 */
  stopped?: boolean;
  tools?: (string | null)[];
  name?: string;
  /** 工具行入参摘要（AI 消息 tool_calls 按 id 配对）：历史里"搜了什么"可见。 */
  args?: Record<string, unknown>;
  /** 消息创建时间（本地时间字符串）；旧 checkpoint 消息没有该字段。 */
  ts?: string;
  /** 用户消息附带的图片（data URL；多模态传图，2026-09-18）。回放时用户气泡显示小图。 */
  image?: string;
}

/** 历史消息的分页响应（默认只回最近 500 条，`truncated` 为真时前端要如实说明）。 */
export interface MessagePage {
  messages: MessageRow[];
  total: number;
  limit: number;
  truncated: boolean;
  /** 这一条会话此刻有没有"正在生成、还没进检查点"的那一句（R26-38）。
   *  `null`/缺省 = 没人在生成；有则 `text` 是**已经投送出去**的那段（与桌宠屏幕上已有的
   *  字一致，守卫扣住的尾巴不在里面）。空串是"她在打字，还没出字"。
   *  随 `?limit=1` 那个探针一起回，所以对话界面不必多打一次请求就知道该镜像什么。 */
  inflight?: { text: string } | null;
}

/** `GET /api/session/{tid}/turn` 的回体：只问"此刻有没有人在说、说到哪儿"。
 *  与 `MessagePage.inflight` 同一个语义、同一个出处（后端那份进程内登记），
 *  区别只在它**不碰检查点**，所以可以按心跳去问。 */
export interface TurnProbe {
  inflight: { text: string } | null;
}

/** 会话的上下文预算事实（`GET /api/session/{id}/context`）。 */
export interface SessionContext {
  /** 最近一轮被裁掉的历史条数（0 = 没裁，界面不该提示）。 */
  trimmed: number;
  /** 最近一轮实际送进 prompt 的条数。 */
  kept: number;
  /** 当前配置的字符预算。可能与历史那一轮不同（操作员改过配置），所以一起给出。 */
  budget: number;
}
