import type { ReactNode } from "react";

/** 卡片容器：统一边框/底色/暗色态，替代各页散落的 `rounded-xl border … bg-white dark:bg-slate-800`。
 *  仅做外层容器；padding / 圆角 / 虚线等变体用 className 接管，内部结构保持不变。 */
export default function Card({
  className = "",
  children,
}: {
  className?: string;
  children?: ReactNode;
}) {
  return (
    <section className={`rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 ${className}`}>
      {children}
    </section>
  );
}
