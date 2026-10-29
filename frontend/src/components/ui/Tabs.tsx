/** 页内区块页签 —— 全站唯一实现。
 *
 *  为什么收编（2026-10-30 交互审计）：此前三套语言并存 —— 设置页下划线式、资料页
 *  强调色胶囊、工具页白底胶囊，注释还互相指错（工具页自称"与设置页同一交互语言"）。
 *  视觉取工具页那版**白底胶囊**：与侧栏导航（强调色药丸）拉开层级 —— 页内区块切换
 *  要"安静"，强调色留给整页级的选中。语义用 `aria-pressed`（开关按钮组，没有
 *  tablist 那套方向键/aria-controls 接线义务，但读屏能如实报"已按下"）。
 */
export default function Tabs<T extends string>({
  items,
  active,
  onChange,
  ariaLabel,
  className = "",
}: {
  items: readonly { key: T; label: string }[];
  active: T;
  onChange: (key: T) => void;
  /** 读屏的名字：这一排页签切的是什么（如"本页区块"）。 */
  ariaLabel: string;
  className?: string;
}) {
  return (
    <nav className={`flex flex-wrap gap-1.5 text-xs ${className}`} aria-label={ariaLabel}>
      {items.map((it) => {
        const on = it.key === active;
        return (
          <button
            key={it.key}
            type="button"
            onClick={() => onChange(it.key)}
            aria-pressed={on}
            className={`rounded-full px-3 py-1 transition-colors ${
              on
                ? "bg-white font-medium text-slate-800 shadow-sm dark:bg-white/10 dark:text-slate-100"
                : "text-slate-500 hover:bg-white/60 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-white/5 dark:hover:text-slate-200"
            }`}
          >
            {it.label}
          </button>
        );
      })}
    </nav>
  );
}
