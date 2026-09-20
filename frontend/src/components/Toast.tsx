import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from "react";

/** 状态提示的语气：成功=绿、告警=琥珀、中性=灰。 */
export type Tone = "info" | "ok" | "warn";

interface Toast {
  id: number;
  text: string;
  tone: Tone;
}

const LIFETIME_MS = 6000;

interface ToastApi {
  /** 当前所有可见 toast（仅供 ToastStack 内部渲染用）。 */
  toasts: Toast[];
  /** 弹一条 toast；空文本被忽略。tone 默认 info。 */
  push: (text: string, tone?: Tone) => void;
  /** 手动关掉某条（点击 toast 即触发）。 */
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

/**
 * 全局 toast 栈：挂在 App 根（见 App.tsx），整个应用（含所有 lazy 页）共享同一个栈，
 * 视口级 `fixed` 定位、不要求调用方容器 `relative`。
 *
 * 用法：任意组件 `const { push } = useToast();` 然后 `push("已保存", "ok")`。
 */
export function ToastProvider({ children }: { children: ReactNode }) {
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

  return (
    <ToastContext.Provider value={{ toasts, push, dismiss }}>
      {children}
      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

// 兜底：未挂载 Provider 时（如单测直接渲染子组件）返回 no-op，避免崩溃；
// 真实应用总在 App 根挂 ToastProvider（见 App.tsx），不会出现无意义静默。
const NOOP_TOAST: ToastApi = { toasts: [], push: () => {}, dismiss: () => {} };

/** 取全局 toast API；Provider 外返回 no-op（见上）。 */
export function useToast(): ToastApi {
  return useContext(ToastContext) ?? NOOP_TOAST;
}

// 修设计稿 §1.2 点名的深色态冲突：原本 `text-slate-600 dark:text-slate-300 dark:text-slate-600`
// 两个 dark 段重复且末尾把字色压成 slate-600（暗底上看不清）。统一成单一 dark 段。
const TONE_CLS: Record<Tone, string> = {
  info: "border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300",
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
  // 视口级 fixed：不依赖调用方容器 relative，整站只在这里渲染一次。
  return (
    <div className="pointer-events-none fixed top-4 right-4 z-[60] flex w-80 flex-col gap-2">
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
