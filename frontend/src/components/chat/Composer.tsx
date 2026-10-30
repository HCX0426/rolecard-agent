import { Button } from "../ui";
import { useCallback, useEffect, useRef, useState } from "react";

import { fetchLimits } from "../../lib/limits";
import type { Tone } from "../Toast";
import { IconImage, IconSend, IconSparkle, IconStop } from "./icons";

/**
 * 输入框（紧凑 IDE 式）：发送/暂停是嵌在框内的图标按钮；左下「增强提示词」，
 * 右下上下文使用率（悬停看明细）。
 *
 * `inputRef` 由页面传进来：深链跳进一条会话时要把光标直接落在输入框（用户跳进来的目的是回话），
 * 而"落点了没有"是页面的事，不是这一框的事。
 */
export default function Composer({
  input,
  setInput,
  busy,
  enhancing,
  pendingImage,
  setPendingImage,
  ctxUsed,
  ctxBudget,
  ctxPct,
  droppedThisTurn,
  inputRef,
  hostRef,
  onSubmit,
  onStop,
  onEnhance,
  onStatus,
}: {
  input: string;
  setInput: (text: string) => void;
  busy: boolean;
  enhancing: boolean;
  /** 已就绪、待随下一条消息发出的图片（data URL），没有时为 null。 */
  pendingImage: string | null;
  setPendingImage: (dataUrl: string | null) => void;
  ctxUsed: number;
  ctxBudget: number;
  ctxPct: number;
  /** 本轮被折叠掉的历史条数（H3）；没有折叠时为 null。只用于悬停明细。 */
  droppedThisTurn: number | null;
  inputRef: React.RefObject<HTMLTextAreaElement>;
  /** 消息列表那一块（用户 2026-10-29：输入框长高以对话区高度的 40% 为封顶）。
   *  量不到（浏览器调试页没挂、jsdom 里高度为 0）就退回 160px 的旧封顶。 */
  hostRef?: React.RefObject<HTMLElement>;
  onSubmit: () => void;
  onStop: () => void;
  onEnhance: () => void;
  onStatus: (text: string, tone?: Tone) => void;
}) {
  const imageInputRef = useRef<HTMLInputElement>(null);

  /** 用户自拉的输入区上限（px）。localStorage 持久化（坏文件/隐私模式回落 null = 启发式）。 */
  const [userMaxH, setUserMaxH] = useState<number | null>(() => {
    try {
      const raw = localStorage.getItem("chat.inputMaxH");
      const n = raw ? Number(raw) : NaN;
      return Number.isFinite(n) && n >= 72 ? n : null;
    } catch {
      return null;
    }
  });

  /** 顶边把手：上拉 = 输入区上限变高（顶边往上提），下拉收窄。松手才落盘。 */
  const dragStartRef = useRef<{ y: number; cap: number } | null>(null);
  const onHandleDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const host = hostRef?.current;
      const startCap =
        userMaxH ??
        (host && host.clientHeight > 0 ? Math.round(host.clientHeight * 0.4) : 160);
      dragStartRef.current = { y: e.clientY, cap: startCap };
      e.currentTarget.setPointerCapture?.(e.pointerId);
    },
    [hostRef, userMaxH],
  );
  const onHandleMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const start = dragStartRef.current;
      if (!start) return;
      // 上界用**窗口**而不是会话滚动区：把手是全局偏好，空会话态（还没开对话、
      // 宿主未挂载）也得能拉 —— 实测空会话态下挂在宿主上就是"拉了没反应"。
      const max = Math.max(72, Math.round(window.innerHeight * 0.85));
      const next = Math.min(max, Math.max(72, start.cap + (start.y - e.clientY)));
      setUserMaxH(next);
    },
    [],
  );
  const onHandleUp = useCallback(() => {
    dragStartRef.current = null;
  }, []);
  // 持久化走 effect 而不是 setState updater：拖动结束那一下 setState 的值与现状相等时
  // React 会整单跳过，updater 根本不执行 —— 副作用放进 updater 就是"有时不落盘"
  //（实测：拖完 localStorage 还是 null）。高度变化就写，拖动中也写，代价可忽略。
  useEffect(() => {
    try {
      if (userMaxH === null) localStorage.removeItem("chat.inputMaxH");
      else localStorage.setItem("chat.inputMaxH", String(userMaxH));
    } catch {
      /* 隐私模式/坏文件：不落盘，本次会话内仍生效 */
    }
  }, [userMaxH]);

  // 输入框自适应高度：内容多时长高，封顶 = 对话区高度的 40%（量不到的环境用 160px），
  // 超过后框内滚动 —— 滚动条不显示（`no-scrollbar`，用户 2026-10-29：能滚，但别看见那个条）。
  // ResizeObserver 让封顶跟着窗口尺寸走：只按 input 变化重算的话，把窗口拉小之后
  // 输入框还按旧封顶占着，要等下一次打字才缩。
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    const host = hostRef?.current ?? null;
    const resize = () => {
      // 上限：用户拖过把手就用**用户那份**（localStorage 持久化）；没拖过用宿主 40%
      // 的启发式。上限高一些输入区就往上多占一些 —— "对话框往上提"就是这一下。
      const cap =
        userMaxH ??
        (host && host.clientHeight > 0 ? Math.round(host.clientHeight * 0.4) : 160);
      el.style.maxHeight = `${cap}px`;
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, cap)}px`;
    };
    resize();
    if (!host || typeof ResizeObserver === "undefined") return; // jsdom / 无宿主：只有静态那一次
    const ro = new ResizeObserver(resize);
    ro.observe(host);
    return () => ro.disconnect();
  }, [input, inputRef, hostRef, userMaxH]);

  async function pickImage(file: File | undefined) {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      onStatus("只支持图片文件", "warn");
      return;
    }
    // 上限从后端 /api/health 现读（config.max_image_bytes 的镜像），不在前端手抄。
    const limits = await fetchLimits();
    if (file.size > limits.image) {
      const mb = Math.round(limits.image / 1024 / 1024);
      onStatus(`图片超过 ${mb}MB 上限（当前 ${Math.round(file.size / 1024 / 1024)}MB）`, "warn");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => setPendingImage(String(reader.result));
    reader.onerror = () => onStatus("图片读取失败", "warn");
    reader.readAsDataURL(file);
  }

  return (
    <div className="mx-auto max-w-3xl pb-2">
      {/* 输入框 = 浮在暖底上的玻璃卡片（2026-10-25 玻璃重设计）：影子给它"在场"，
          边框/底色交给材质层，聚焦不点亮（用户 10-25 拍板）。 */}
      <div className="glass rounded-2xl shadow-[0_8px_30px_-12px_rgba(60,45,30,0.35)] dark:shadow-[0_8px_30px_-12px_rgba(0,0,0,0.6)]">
        {/* 顶边把手（2026-10-31 用户："宿主 40% 那个我要能自己拉"）：上拉 = 输入区上限
            变高（顶边往上提），下拉收窄；高度记住（localStorage）。平时隐形，悬停显形 ——
            一条常驻的把手会把"输入框"切成"上下两块"的错觉。 */}
        <div
          onPointerDown={onHandleDown}
          onPointerMove={onHandleMove}
          onPointerUp={onHandleUp}
          onPointerCancel={onHandleUp}
          role="separator"
          aria-label="调整输入框高度（拖动）"
          title="上下拖动调整输入框高度"
          className="group/handle flex h-3 cursor-row-resize touch-none items-center justify-center rounded-t-2xl"
        >
          <span className="h-1 w-10 rounded-full bg-slate-300 opacity-0 transition-opacity group-hover/handle:opacity-100 dark:bg-slate-600" />
        </div>
        {pendingImage && (
          <div className="flex items-center gap-2 border-b border-slate-100 px-3 py-2 dark:border-slate-700">
            <img
              src={pendingImage}
              alt="待发送图片"
              className="h-16 w-16 rounded-lg border border-slate-200 object-cover dark:border-slate-600"
            />
            <span className="text-11 text-slate-500 dark:text-slate-400">图片已附加，将随消息一起发送</span>
            <button
              onClick={() => setPendingImage(null)}
              disabled={busy}
              title="移除图片"
              className="ml-auto rounded px-2 py-1 text-11 text-red-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-40 dark:hover:bg-red-900/30"
            >
              移除
            </button>
          </div>
        )}
        <textarea
          ref={inputRef}
          value={input}
          rows={1}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            // Enter 发送、Shift+Enter 换行；输入法组词中（isComposing）不触发。
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              onSubmit();
            }
          }}
          placeholder={
            busy
              ? "正在生成…（可点右下角停止）"
              : "输入消息，Enter 发送 / Shift+Enter 换行（没有对话会自动创建）"
          }
          disabled={busy}
          className="w-full resize-none overflow-y-auto bg-transparent px-4 pt-3 pb-1 leading-relaxed outline-none no-scrollbar disabled:bg-slate-50 dark:disabled:bg-slate-800/50"
        />
        <div className="flex items-center justify-between gap-2 px-2.5 pb-2">
          <div className="flex items-center gap-1">
            <input
              ref={imageInputRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                pickImage(e.target.files?.[0]);
                e.target.value = ""; // 允许连续选同一文件
              }}
            />
            <button
              onClick={() => imageInputRef.current?.click()}
              disabled={busy}
              title="附加图片（发给当前模型识别；需视觉模型支持）"
              className="flex items-center gap-1 rounded-lg px-2 py-1 text-11 text-slate-500 hover:bg-slate-100 disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-700/60"
            >
              <IconImage />
              图片
            </button>
            <button
              onClick={onEnhance}
              disabled={busy || enhancing || !input.trim()}
              title="增强提示词：把草稿改写得更清晰、具体（一次模型调用）"
              className="flex items-center gap-1 rounded-lg px-2 py-1 text-11 text-slate-500 hover:bg-slate-100 disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-700/60"
            >
              <IconSparkle />
              {enhancing ? "增强中…" : "增强提示词"}
            </button>
          </div>
          <div className="flex items-center gap-2">
            {ctxBudget > 0 && (
              <span
                title={`上下文约 ${ctxUsed} 字 / 上限 ${ctxBudget} 字（${ctxPct}%）${
                  droppedThisTurn !== null ? ` · 本轮已裁 ${droppedThisTurn} 条` : ""
                }`}
                className="flex cursor-default items-center gap-1 text-11 text-slate-400 dark:text-slate-500"
              >
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    ctxPct >= 80 ? "bg-amber-400" : "bg-slate-300 dark:bg-slate-600"
                  }`}
                />
                {ctxPct}%
              </span>
            )}
            {busy ? (
              <button
                onClick={onStop}
                title="停止生成"
                aria-label="停止生成"
                className="flex h-8 w-8 items-center justify-center rounded-full bg-slate-700 text-white hover:bg-slate-800 dark:bg-slate-600 dark:hover:bg-slate-500"
              >
                <IconStop />
              </button>
            ) : (
              <Button
                variant="primary"
                size="icon"
                onClick={onSubmit}
                disabled={!input.trim()}
                title="发送（Enter）"
                aria-label="发送"
                className="flex h-8 w-8 items-center justify-center rounded-full"
              >
                <IconSend />
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
