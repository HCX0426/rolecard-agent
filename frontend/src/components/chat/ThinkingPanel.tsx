import { useState } from "react";

/**
 * 思考过程的折叠面板 —— AI IDE 式交互。**对话页唯一的思考面板实现**：
 * 流式气泡与历史回放都渲染它，两处观感不会漂移。
 *
 * ## 设计决定
 *
 * 1. **流式进行中默认展开**：思考是用户想看的内容，面板本身就是"正在思考"的视觉信号
 *    （不显示"思考中…"省略号——那是冗余噪音）。
 * 2. **结束之后一律折叠**（回放路径传 `defaultOpen={false}`，含"刚结束的这一轮"）：
 *    展开是用户点击后的动作；每条回答都保持展开会喧宾夺主，主体应该是回答
 *    （用户 2026-09-16 明确："刚结束的一轮保持展开是错的，我点击后才展开"）。
 *
 * 数据来源有两条，最终效果一致（因此组件不需要区分）：
 *   - 流式进行中：`thinking` 事件逐段累加进 live 气泡；
 *   - 回放/历史：思考随消息一起从 checkpoint 回放（`serialize_message` 带 `reasoning`），
 *     所以回答结束后、甚至刷新页面后，思考过程依然在。
 */
export default function ThinkingPanel({
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
      <pre className="mt-1.5 max-h-56 overflow-auto whitespace-pre-wrap text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">
        {text}
      </pre>
    </details>
  );
}
