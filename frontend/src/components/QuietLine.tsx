/**
 * 「她此刻为什么静默」那一格（`S-8`，收件箱抽屉与运行环境页共用）。
 *
 * 分三段渲染的理由见 `lib/quiet.ts`：一条长串在窄抽屉里会从句子中间折行。
 * 文案与时刻都来自后端，这里只负责摆 —— 前端不自己推任何一分钟。
 */
import type { QuietStatus } from "../api";
import { quietParts } from "../lib/quiet";
import Tag from "./ui/Tag";

export default function QuietLine({ q }: { q: QuietStatus }) {
  const { head, badge, when, ready } = quietParts(q);
  return (
    <span className="block text-[11px] leading-4">
      <span className="flex flex-wrap items-center gap-x-1.5">
        <span className={ready ? "text-slate-500 dark:text-slate-400" : ""}>{head}</span>
        {badge && <Tag tone="amber">{badge}</Tag>}
      </span>
      {when && (
        <span className="block text-slate-400 dark:text-slate-500">⇒ 下一次大约 {when}</span>
      )}
    </span>
  );
}
