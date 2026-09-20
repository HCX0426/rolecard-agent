import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "outline" | "ghost" | "danger";
type Size = "sm" | "md";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-blue-600 text-white hover:bg-blue-700 disabled:bg-slate-300 dark:bg-blue-600 dark:hover:bg-blue-500 dark:disabled:bg-slate-700",
  outline:
    "border border-slate-200 bg-white text-slate-600 hover:border-blue-300 hover:text-blue-600 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:border-blue-700 dark:hover:text-blue-400",
  // ghost/danger 以前**完全没有禁用态**：灰掉与否看起来一样能点，
  // 于是"禁用 + 说明"这条规则在这些变体上等于没实现。
  ghost:
    "text-slate-500 hover:bg-slate-100 disabled:text-slate-300 disabled:hover:bg-transparent dark:text-slate-400 dark:hover:bg-slate-700 dark:disabled:text-slate-600 dark:disabled:hover:bg-transparent",
  danger:
    "bg-red-500 text-white hover:bg-red-600 disabled:bg-slate-300 disabled:text-slate-500 dark:disabled:bg-slate-700 dark:disabled:text-slate-500",
};

const SIZES: Record<Size, string> = {
  sm: "px-2.5 py-1 text-xs",
  md: "px-4 py-2 text-sm",
};

/**
 * 统一按钮：variant 决定语气，size 决定尺寸；调用方的 className 可以追加覆盖。
 *
 * `disabledHint` 是「暂时不可用」这一档的标准出口（口径见 `docs/模型接入设计稿.md` §4）：
 * 只在 `disabled` 为真时渲染一行说明，告诉用户**怎么让它可用**。
 * 为什么要单独一个属性而不是 `title`：Chromium 在禁用按钮上根本不弹原生 title，
 * 于是"禁用了但不说为什么"就成了既拦不住又查不出的死控件。
 */
export default function Button({
  variant = "primary",
  size = "md",
  className = "",
  children,
  disabledHint,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
  children?: ReactNode;
  disabledHint?: string;
}) {
  const button = (
    <button
      className={`rounded-lg font-medium transition-colors ${VARIANTS[variant]} ${SIZES[size]} ${className}`}
      disabled={disabled}
      {...rest}
    >
      {children}
    </button>
  );
  if (!disabled || !disabledHint) return button;
  return (
    <span className="inline-flex flex-col items-start gap-0.5">
      {button}
      <span className="text-[10px] leading-tight text-slate-400 dark:text-slate-500">
        {disabledHint}
      </span>
    </span>
  );
}
