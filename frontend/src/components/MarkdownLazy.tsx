import { lazy, Suspense } from "react";

/** Markdown 渲染栈的**懒加载壳**（2026-10-30 壳与脚本审计 #3）。
 *
 * 渲染栈本体（react-markdown + remark 全家 + highlight.js，约占 ChatPage chunk 的一半）
 * 只在第一条助手消息出现时才被需要 —— 从前经 TurnRow/ProcessPanel/ChatPage 三处静态
 * import 进默认页签的首屏 chunk，空会话冷启动也在解析它。这个壳是唯一允许消费者引用的
 * 入口：谁再**静态** import `../Markdown`，整栈就会被拖回首屏（vite 会对动态+静态混用
 * 发警告，那是红灯不是噪声）。
 */
const Markdown = lazy(() =>
  import("./Markdown").then((m) => ({ default: m.Markdown })),
);

/** 落进回答/工具输出那一拍栈还在拉（本机毫秒级）：兜一个"…"，字到即换。 */
export function MarkdownLazy({ text }: { text: string }) {
  return (
    <Suspense fallback="…">
      <Markdown text={text} />
    </Suspense>
  );
}
