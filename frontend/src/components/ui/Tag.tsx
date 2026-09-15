import type { ReactNode } from "react";

export type TagTone = "green" | "amber" | "red" | "blue" | "slate";

const TONES: Record<TagTone, string> = {
  green: "bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-300",
  amber: "bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300",
  red: "bg-red-50 text-red-600 dark:bg-red-900/30 dark:text-red-400",
  blue: "bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300",
  slate: "bg-slate-100 text-slate-500 dark:bg-slate-700/50 dark:text-slate-400",
};

/** 状态标签（已校验 / 未校验 / 插件停用 …）。 */
export default function Tag({
  tone = "slate",
  children,
}: {
  tone?: TagTone;
  children: ReactNode;
}) {
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-[11px] ${TONES[tone]}`}>
      {children}
    </span>
  );
}
