/** 页面级加载骨架（2026-10-24 UI 打磨，P2 清单"加载态全是裸文字"那格）。
 *
 * 为什么是三根脉冲条而不是转圈：每页的首屏都是"标题 + 表格/表单"的紧凑 IDE 形状，
 * 骨架按同一形状占位，切换页签时布局跳动最小。纯 CSS（animate-pulse），零依赖。
 */
export function PageSkeleton() {
  return (
    <div className="flex h-full items-center justify-center p-8" aria-busy="true" role="status">
      <div className="w-full max-w-3xl space-y-3">
        <div className="h-6 w-1/3 animate-pulse rounded bg-slate-200 dark:bg-slate-700" />
        <div className="h-4 w-2/3 animate-pulse rounded bg-slate-200 dark:bg-slate-700" />
        <div className="h-4 w-1/2 animate-pulse rounded bg-slate-200 dark:bg-slate-700" />
        <span className="sr-only">加载中…</span>
      </div>
    </div>
  );
}
