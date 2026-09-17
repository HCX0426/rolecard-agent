import type { ReactNode } from "react";

/** 页面头统一形状：标题 + 一行副标题 + 右侧动作区。
 *
 * 5 个业务页此前各自手写这段结构（字号/间距/深浅色各抄一遍）——抽出来之后
 * "其他界面对齐对话页的观感"只需改这一处。
 */
export default function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">{title}</h2>
        {subtitle && (
          <p className="mt-0.5 text-xs leading-relaxed text-slate-400 dark:text-slate-500">{subtitle}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}
