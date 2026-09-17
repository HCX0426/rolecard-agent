import type { ReactNode } from "react";

type Tone = "error" | "warn" | "info" | "ok";

const TONES: Record<Tone, string> = {
  error: "bg-red-50 text-red-600 dark:bg-red-900/30 dark:text-red-400",
  warn: "bg-amber-50 text-amber-700 dark:bg-amber-900/20 dark:text-amber-300",
  info: "bg-slate-50 text-slate-500 dark:bg-slate-800/50 dark:text-slate-400",
  ok: "bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-300",
};

/** 状态/错误/说明提示条 —— 各页此前复制同一串类名（实测 4+ 处）。 */
export default function Notice({
  tone = "error",
  children,
  className = "",
}: {
  tone?: Tone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <p className={`rounded-lg px-3 py-2.5 text-xs leading-relaxed ${TONES[tone]} ${className}`}>
      {children}
    </p>
  );
}
