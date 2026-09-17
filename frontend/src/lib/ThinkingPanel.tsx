import { useState } from "react";

/**
 * 思考过程的折叠面板 —— AI IDE 式交互。
 *
 * ## 设计决定
 *
 * 1. **流式进行中默认展开**：思考是用户想看的内容，面板本身就是"正在思考"的视觉信号
 *    （不显示"思考中…"省略号——那是冗余噪音）。可手动收起，但不会流结束自动收起。
 * 2. **历史回放默认折叠**（`defaultOpen={false}`）：回放列表里每条思考型回答都全量展开
 *    会喧宾夺主——翻历史时主体是回答，推理过程按需展开（用户 2026-09-17 截图反馈）。
 *
 * 数据来源有两条，最终效果一致（因此组件不需要区分）：
 *   - 流式进行中：`thinking` 事件逐段累加进 live 气泡；
 *   - 回放/历史：思考随消息一起从 checkpoint 回放（`serialize_message` 带 `reasoning`），
 *     所以回答结束后、甚至刷新页面后，思考过程依然在。
 */
export function ThinkingPanel({
  text,
  defaultOpen = true,
}: {
  text: string;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
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
