import { useState } from "react";

import type { ToolStep } from "../../lib/stream";

/** 一次工具执行的过程卡片：状态圆点 + 名称 + 入参摘要（可展开看完整结果）。
 *
 * 从 ChatPage 抽出（原为页面内私有组件）：live 气泡与回放的过程面板都要用它。
 */
export default function ToolStepCard({ step }: { step: ToolStep }) {
  const [open, setOpen] = useState(false);
  const dot =
    step.status === "running"
      ? "bg-blue-400 animate-pulse"
      : step.status === "error"
        ? "bg-red-400"
        : "bg-green-500 dark:bg-green-600";
  const body = step.content.trim();
  // 入参摘要：让"过程"可见（搜了什么词 / 抓了哪个地址），截断到一行。
  const argsSummary = Object.entries(step.args ?? {})
    .filter(([, v]) => v !== null && v !== undefined && String(v).trim() !== "")
    .map(([k, v]) => `${k}=${String(v)}`)
    .join("  ")
    .slice(0, 80);
  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50 px-2.5 py-1.5">
      <button
        onClick={() => body && setOpen((o) => !o)}
        className={`flex w-full items-center gap-2 text-left font-mono text-xs text-slate-600 dark:text-slate-300 ${
          body ? "cursor-pointer" : "cursor-default"
        }`}
      >
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
        <span className="shrink-0">{step.name}</span>
        {argsSummary && (
          <span className="min-w-0 flex-1 truncate text-slate-400 dark:text-slate-500">
            {argsSummary}
          </span>
        )}
        {step.status === "running" && <span className="shrink-0 text-slate-400 dark:text-slate-500">执行中…</span>}
        {body && (
          <span className="ml-auto shrink-0 text-slate-300 dark:text-slate-600">
            {open ? "收起 ▴" : `${body.length} 字 ▾`}
          </span>
        )}
      </button>
      {open && body && (
        <pre className="mt-1.5 max-h-56 overflow-auto rounded bg-white dark:bg-slate-800 p-2 text-[11px] whitespace-pre-wrap text-slate-600 dark:text-slate-300">
          {body}
        </pre>
      )}
    </div>
  );
}
