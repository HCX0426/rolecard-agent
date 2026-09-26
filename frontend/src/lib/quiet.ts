import type { QuietStatus } from "../api";

const DAY_MS = 86_400_000;

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** 「下一次大约 16:34 / 明天 08:00」。给不出时刻（要他回话、正在对话）就是空串。 */
export function formatNextOk(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "";
  const hm = `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
  const days = Math.round((startOfDay(t) - startOfDay(now)) / DAY_MS);
  if (days === 0) return `今天 ${hm}`;
  if (days === 1) return `明天 ${hm}`;
  return `${t.getMonth() + 1}月${t.getDate()}日 ${hm}`;
}

/** 一个角色此刻的静默状态 → 界面那一行（运行环境页与收件箱抽屉共用同一句措辞）。
 *
 * 那句 `why` **原样透出**，不在这里重新造一遍：它是 `core/reachout._gate` 算出来的那一句，
 * 同一个字符串也会进 tracer 的 `reachout_quiet` 事件。界面上换个说法、日志里留原话，
 * 就成了"屏幕说不一致"的那种查不出来的分歧（`R26-35` 修的就是这一族）。
 * `why === null` 写成肯定句 —— 它是"她现在随时能开口"，不是"没算出来"。
 */
export function quietLine(r: QuietStatus, now: Date = new Date()): string {
  const head = r.why ? `${r.role_name} 静默中 · ${r.why}` : `${r.role_name} 现在随时能开口`;
  const when = formatNextOk(r.next_ok_at, now);
  return when ? `${head}　⇒ 下一次大约 ${when}` : head;
}
