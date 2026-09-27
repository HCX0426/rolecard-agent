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

/** 一个角色此刻的静默状态 → 界面上的**三段**（运行环境页与收件箱抽屉共用同一个组件）。
 *
 * 为什么是三段而不是一句话（`R26-45`）：旧版把「距上次说话不足 66 分钟，她连着 1 条没被回
 * 已退避　⇒ 下一次大约 今天 23:09」拼成一条长串，在 206px 的抽屉里从**句子中间**折行，
 * 读起来像坏了。现在主句一句、退避一枚徽章、时刻另起一行 —— 三段各不折行。
 *
 * 那句 `why` 仍然**原样透出**，不在这里重新造一遍：它是 `core/reachout.quiet_gate` 算出来的
 * 那一句，同一个字符串与同一个 `streak` 也会进 tracer 的 `reachout_quiet` 事件。
 * 界面上换个说法、日志里留原话，就成了"屏幕说不一致"那种查不出来的分歧（`R26-35` 治的就是它）。
 * `why === null` 写成肯定句 —— 它是"她现在随时能开口"，不是"没算出来"。
 */
export interface QuietParts {
  head: string;
  /** 退避徽章的文案；"" = 没有退避（不渲染空徽章）。 */
  badge: string;
  /** 「下一次大约 今天 23:09」；"" = 这不是"等一会儿就好"的事。 */
  when: string;
  ready: boolean;
}

export function quietParts(r: QuietStatus, now: Date = new Date()): QuietParts {
  const when = formatNextOk(r.next_ok_at, now);
  if (!r.why) return { head: `${r.role_name} 现在随时能开口`, badge: "", when: "", ready: true };
  return {
    head: `${r.role_name} 静默中 · ${r.why}`,
    badge: r.streak > 0 ? `连着 ${r.streak} 条没被回 · 已退避` : "",
    when,
    ready: false,
  };
}
