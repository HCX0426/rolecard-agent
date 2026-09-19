// 流式对话的生命周期（从 ChatPage 抽出）。
// 只管"这一轮流式气泡"的状态机：busy / live 气泡 / 三个 ref / SSE 事件归约 onEvent / 起流 startBubble / 停止 stop。
// errorRef 与 trim 提示留在 ChatPage（applyMeta 里，和上下文裁剪 UI 耦合），避免 hook 反向依赖组件 setter。
// 气泡"当前值"用 liveRef 镜像而非只靠 setState：事件回调需读到最新气泡才能正确归约，
// 而 StrictMode 下 setState 更新函数可能被调用两次（在更新函数里做副作用会重复），故用 ref 明确持有。
import { useCallback, useRef, useState } from "react";

import { type ChatEvent } from "../api";
import { newLiveBubble, reduceChatEvent, type LiveBubble, type StreamMeta } from "../lib/stream";

export function useChatStream(onMeta: (meta: StreamMeta) => void) {
  const [busy, setBusy] = useState(false); // 流式进行中：驱动「停止」按钮与输入禁用
  const [live, setLive] = useState<LiveBubble | null>(null);
  const liveRef = useRef<LiveBubble | null>(null);
  const sendingRef = useRef(false); // 并发发送/切会话的闸门（同步读，避免 setState 异步竞态）
  const abortRef = useRef<AbortController | null>(null);

  /** 事件 → 新气泡的归约（纯逻辑在 lib/stream.reduceChatEvent，已单测）；meta 走旁路回调。 */
  const onEvent = useCallback(
    (ev: ChatEvent) => {
      const prev = liveRef.current ?? newLiveBubble();
      const reduced = reduceChatEvent(prev, ev);
      liveRef.current = reduced.bubble;
      setLive(reduced.bubble);
      onMeta(reduced.meta);
    },
    [onMeta],
  );

  /** 开一轮流：重置气泡 + 建 abort 控制器。返回的 controller 交给调用方传给 streamChat/streamEdit。 */
  function startBubble(): AbortController {
    const bubble = newLiveBubble();
    liveRef.current = bubble;
    setLive(bubble);
    const controller = new AbortController();
    abortRef.current = controller;
    return controller;
  }

  function stop() {
    abortRef.current?.abort();
  }

  return { busy, setBusy, live, setLive, liveRef, sendingRef, abortRef, onEvent, startBubble, stop };
}
