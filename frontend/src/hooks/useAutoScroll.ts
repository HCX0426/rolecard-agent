/** 聊天区的自动滚动（从 ChatPage 抽出）。 */
import { useEffect, useRef } from "react";

/**
 * 只在「用户本来就贴着底部」时跟随新内容。
 *
 * 为什么不能无条件 scrollTo 到底：生成期间用户往上翻历史会被**每个 token** 拽回底部
 * （审查报告 P2）。切会话 / 刚发完消息时强制跟一次（sticky 置位）。
 *
 * 返回值挂在消息区容器上即可；`deps` 由调用方给出（通常是 [messages, live]）。
 */
export function useAutoScroll(sessionId: string | null, deps: readonly unknown[]) {
  const ref = useRef<HTMLDivElement>(null);
  const sticky = useRef(true);

  useEffect(() => {
    sticky.current = true; // 切了上下文 = 用户主动换了对话，强制跟一次
  }, [sessionId]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (!sticky.current && !nearBottom) return;
    el.scrollTo({ top: el.scrollHeight });
    sticky.current = false;
    // deps 由调用方传入（长度恒定），这里不做静态依赖推断。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return ref;
}
