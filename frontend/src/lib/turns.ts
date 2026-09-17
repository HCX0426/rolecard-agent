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
