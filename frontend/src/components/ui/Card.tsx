import type { ReactNode } from "react";

/** 面板：可选标题 + 右侧动作区。深浅色都由这里的类负责。 */
export default function Card({
  title,
  actions,
  children,
  className = "",
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-800 ${className}`}
    >
      {(title || actions) && (
        <div className="flex items-center justify-between gap-3 border-b border-slate-100 px-4 py-2.5 dark:border-slate-700">
          <div className="text-sm font-medium text-slate-700 dark:text-slate-200">{title}</div>
          {actions}
        </div>
      )}
      <div className="p-4">{children}</div>
    </div>
  );
}
