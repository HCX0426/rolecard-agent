import { useEffect, useRef } from "react";

import type { Tone } from "../Toast";
import { IconImage, IconSend, IconSparkle, IconStop } from "./icons";

/** 待发送图片的大小上限：再大基本是手机原图，模型那边也会被缩，传输与 base64 白付两倍钱。 */
const MAX_IMAGE_BYTES = 15 * 1024 * 1024;

/**
 * 输入框（WorkBuddy 式）：发送/暂停是嵌在框内的图标按钮；左下「增强提示词」，
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
  onSubmit: () => void;
  onStop: () => void;
  onEnhance: () => void;
  onStatus: (text: string, tone?: Tone) => void;
}) {
  const imageInputRef = useRef<HTMLInputElement>(null);

  // 输入框自适应高度：内容多时长高（封顶 160px 后内部滚动），发送/清空后缩回一行。
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [input, inputRef]);

  function pickImage(file: File | undefined) {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      onStatus("只支持图片文件", "warn");
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      onStatus(`图片超过 15MB 上限（当前 ${Math.round(file.size / 1024 / 1024)}MB）`, "warn");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => setPendingImage(String(reader.result));
    reader.onerror = () => onStatus("图片读取失败", "warn");
    reader.readAsDataURL(file);
  }

  return (
    <div className="mx-auto max-w-3xl">
      <div className="rounded-2xl border border-slate-200 bg-white focus-within:border-blue-400 dark:border-slate-700 dark:bg-slate-800">
        {pendingImage && (
          <div className="flex items-center gap-2 border-b border-slate-100 px-3 py-2 dark:border-slate-700">
            <img
              src={pendingImage}
              alt="待发送图片"
              className="h-16 w-16 rounded-lg border border-slate-200 object-cover dark:border-slate-600"
            />
            <span className="text-[11px] text-slate-500 dark:text-slate-400">图片已附加，将随消息一起发送</span>
            <button
              onClick={() => setPendingImage(null)}
              disabled={busy}
              title="移除图片"
              className="ml-auto rounded px-2 py-1 text-[11px] text-red-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-40 dark:hover:bg-red-900/30"
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
          className="max-h-40 w-full resize-none bg-transparent px-4 pt-3 pb-1 leading-relaxed outline-none disabled:bg-slate-50 dark:disabled:bg-slate-800/50"
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
              className="flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-100 disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-700/60"
            >
              <IconImage />
              图片
            </button>
            <button
              onClick={onEnhance}
              disabled={busy || enhancing || !input.trim()}
              title="增强提示词：把草稿改写得更清晰、具体（一次模型调用）"
              className="flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-100 disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-700/60"
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
                className="flex cursor-default items-center gap-1 text-[11px] text-slate-400 dark:text-slate-500"
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
              <button
                onClick={onSubmit}
                disabled={!input.trim()}
                title="发送（Enter）"
                aria-label="发送"
                className="flex h-8 w-8 items-center justify-center rounded-full bg-blue-600 text-white hover:bg-blue-700 disabled:bg-slate-300 dark:disabled:bg-slate-700"
              >
                <IconSend />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
