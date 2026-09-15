import { useCallback, useState } from "react";

/** 状态提示的语气：成功=绿、告警=琥珀、中性=灰。 */
export type Tone = "info" | "ok" | "warn";

interface Toast {
  id: number;
  text: string;
  tone: Tone;
}

const LIFETIME_MS = 6000;

/**
 * 轻量 toast：替代"输入框上方一行 status"。
 *
 * 为什么改：一行 status 会被后来的消息**覆盖**（上一个操作的结果还没看清就没了），
 * 而且不区分语气。toast 可叠加、自动消失、按语气着色。
 *
 * 用法：`const { toasts, push } = useToasts();` + `<ToastStack toasts={toasts} />`。
 * 注意：调用方容器需要 `relative`（ToastStack 用 absolute 定位）。
 */
export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((text: string, tone: Tone = "info") => {
    if (!text) return;
    const id = Date.now() + Math.random();
    setToasts((ts) => [...ts, { id, text, tone }]);
    window.setTimeout(() => {
      setToasts((ts) => ts.filter((t) => t.id !== id));
    }, LIFETIME_MS);
  }, []);

  const dismiss = useCallback((id: number) => {
    setToasts((ts) => ts.filter((t) => t.id !== id));
  }, []);

  return { toasts, push, dismiss };
}

const TONE_CLS: Record<Tone, string> = {
  info: "border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 dark:text-slate-600",
  ok: "border-green-200 bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300",
  warn: "border-amber-200 bg-amber-50 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300",
};

export function ToastStack({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss?: (id: number) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div className="pointer-events-none absolute top-4 right-4 z-50 flex w-80 flex-col gap-2">
      {toasts.map((t) => (
        <button
          key={t.id}
          onClick={() => onDismiss?.(t.id)}
          title="点击关闭"
          className={`pointer-events-auto rounded-xl border px-3 py-2 text-left text-xs leading-relaxed shadow-sm ${TONE_CLS[t.tone]}`}
        >
          {t.text}
        </button>
      ))}
    </div>
  );
}
