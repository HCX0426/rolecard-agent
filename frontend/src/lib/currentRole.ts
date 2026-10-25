/** 「当前角色」的跨页共享状态（2026-10-25 玻璃重设计）。
 *
 *  为什么需要它：强调色（雾底/侧栏块/气泡签）取决于**此刻在和谁说话**，而
 *  "此刻的角色"住在 ChatPage 里（它管会话选择），侧栏与其它页要跟着它变。
 *  两处各拉一份会不同步（切角色后侧栏还是旧色），所以这里做**一个**事实面：
 *  ChatPage 写，全站订阅。
 *
 *  写法刻意用最轻的订阅（useSyncExternalStore），不引状态库：这个 store 全站
 *  只写一处、读几处，React 内置的就够。
 */
import { useSyncExternalStore } from "react";

import { accentFor } from "./accent";

export interface CurrentRole {
  roleId: string;
  roleName: string;
  accent: string;
}

let current: CurrentRole | null = null;
const listeners = new Set<() => void>();

function emit() {
  for (const fn of listeners) fn();
}

/** 记录当前角色（切角色/打开会话时调用）。accent 由 role_id + 显式色派生。
 *  名字带 publish 是为了不和 ChatPage 里的 state setter 撞名（同名会让 TS 少一个参数）。 */
export function publishCurrentRole(roleId: string, roleName: string, explicitAccent?: string | null) {
  const accent = accentFor(roleId, explicitAccent);
  if (current && current.roleId === roleId && current.accent === accent) return;
  current = { roleId, roleName, accent };
  emit();
  // 材质层（雾底/玻璃）读的是 CSS 变量：写它 = 整站换温度。
  document.documentElement.style.setProperty("--accent", accent);
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function getSnapshot(): CurrentRole | null {
  return current;
}

/** 订阅当前角色（无则 null）。 */
export function useCurrentRole(): CurrentRole | null {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
