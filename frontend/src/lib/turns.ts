/**
 * 消息"轮次"切分与选择扩展 —— 编辑/删除功能的纯函数层。
 *
 * ## 为什么需要
 *
 * 后端 `expand_to_turns`（api/deps.py）执行同一条规则："选中一问或一答 = 选中整轮
 * （用户消息 + 助手回答 + 期间的工具消息）"。前端要做**选择联动**（勾选一侧自动勾上
 * 配对侧），就必须有同一套切分逻辑——两边规则必须一致，否则会出现"界面显示选了 2 条、
 * 后端删了 4 条"的错位。与后端一样，这里也是纯函数 + 测试。
 */

export interface TurnMessage {
  role: string;
  id?: string;
}

/** 把消息序列切成轮次（下标数组）。规则与后端 `group_turns` 一致：每条用户消息开新轮；首条非用户消息自成一轮。 */
export function groupTurns<T extends TurnMessage>(messages: T[]): number[][] {
  const turns: number[][] = [];
  messages.forEach((m, index) => {
    if (m.role === "user" || turns.length === 0) turns.push([index]);
    else turns[turns.length - 1].push(index);
  });
  return turns;
}

/**
 * 把选中的 id 扩展为整轮的 id 集合（保持原顺序）。
 * 未知 id 被忽略——由后端负责报 404，这里不替它做校验。
 */
export function expandSelection<T extends TurnMessage>(
  messages: T[],
  selected: string[],
): string[] {
  const wanted = new Set(selected);
  const order = new Map<string, number>();
  const out: string[] = [];
  messages.forEach((m, index) => {
    if (m.id !== undefined) order.set(m.id, index);
  });
  for (const turn of groupTurns(messages)) {
    const ids = turn.map((i) => messages[i].id).filter((id): id is string => id !== undefined);
    if (ids.some((id) => wanted.has(id))) out.push(...ids);
  }
  return [...new Set(out)].sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0));
}

/** 对话渲染用的消息面（结构约束，避免 lib 层依赖 api 层具体类型）。 */
export interface TurnMessageLike {
  role: string;
  id?: string;
  content?: string;
  reasoning?: string;
  tools?: (string | null)[];
  name?: string;
  args?: Record<string, unknown>;
  ts?: string;
}

/** 一轮过程里的一个步骤（思考 / 工具 / 中间轮正文）。 */
export type TurnStep<T extends TurnMessageLike> =
  | { kind: "think"; text: string }
  | { kind: "tool"; msg: T }
  | { kind: "text"; text: string };

export interface BuiltTurn<T extends TurnMessageLike> {
  key: string;
  user: T | null;
  steps: TurnStep<T>[];
  answer: T | null;
}

/** 把消息序列切成"可渲染的一轮"：提问 → 过程步骤（思考/工具/中间正文）→ 最终回答。
 *
 * 在 `groupTurns`（下标分组，与后端 `expand_to_turns` 同规则）**之上**构建，
 * 保证"什么算一轮"只有一处定义——此前 ChatPage 内另写了一套同名分组，两处规则
 * 一旦漂移就会出现"界面选中 1 条、后端删掉 4 条"的错位。
 */
export function buildTurns<T extends TurnMessageLike>(messages: T[]): BuiltTurn<T>[] {
  return groupTurns(messages).map((idx, n) => {
    const first = messages[idx[0]];
    const turn: BuiltTurn<T> = {
      key: first.id ?? `turn-${n}`,
      user: null,
      steps: [],
      answer: null,
    };
    for (const i of idx) {
      const m = messages[i];
      if (m.role === "user") {
        turn.user = m;
      } else if (m.role === "tool") {
        turn.steps.push({ kind: "tool", msg: m });
      } else if (m.tools?.length) {
        // 中间轮（还要继续调工具）：思考进过程；若有前言正文也按过程小字展示
        if (m.reasoning) turn.steps.push({ kind: "think", text: m.reasoning });
        if ((m.content ?? "").trim()) turn.steps.push({ kind: "text", text: m.content ?? "" });
      } else {
        if (m.reasoning) turn.steps.push({ kind: "think", text: m.reasoning });
        turn.answer = m;
      }
    }
    return turn;
  });
}
