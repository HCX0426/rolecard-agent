import { useState } from "react";

/**
 * 思考过程的折叠面板 —— AI IDE 式交互：默认展开，用户点箭头可收起。
 *
 * ## 两个刻意的设计决定
 *
 * 1. **不做"流结束自动收起"**：思考是用户想回看的内容（要判断模型推理对不对），
 *    回答一出来就把过程藏掉等于白显示一趟。默认展开、可手动收起。
 * 2. **不显示"思考中…"的省略号**：面板本身就是"正在思考"的视觉信号，文字提示是冗余噪音。
 *
 * 数据来源有两条，最终效果一致（因此组件不需要区分）：
 *   - 流式进行中：`thinking` 事件逐段累加进 live 气泡；
 *   - 回放/历史：思考随消息一起从 checkpoint 回放（`serialize_message` 带 `reasoning`），
 *     所以回答结束后、甚至刷新页面后，思考过程依然在。
 */
export function ThinkingPanel({ text }: { text: string }) {
  const [open, setOpen] = useState(true);
  if (!text.trim()) return null;
  return (
    <details
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
      className="mb-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/60 px-2.5 py-1.5"
    >
      <summary className="cursor-pointer select-none text-xs text-slate-400 dark:text-slate-500">
        思考过程
      </summary>
      <pre className="mt-1.5 max-h-56 overflow-auto whitespace-pre-wrap text-[11px] leading-relaxed text-slate-500 dark:text-slate-400 dark:text-slate-500">
        {text}
      </pre>
    </details>
  );
}
