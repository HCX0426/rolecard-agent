import type { BuiltTurn } from "../../lib/turns";
import { STOP_HINT } from "../../lib/stream";
import { parseMessageTs } from "../../lib/quiet";
import type { MessageRow } from "../../api";
import { Markdown } from "../Markdown";
import { Tag } from "../ui";
import ProcessPanel from "./ProcessPanel";

/** 回答耗时：created_at 配对（用户 → 助手）换算成可读时长；无时间戳的旧消息返回 null。
 *
 * 解析走 `parseMessageTs`（格式即纪元，`lib/quiet.ts`）：升级前后的同一条线程里
 * 两族并存（旧消息本地 naive、新消息 UTC ISO-Z），配对差值跨族也必须正确。
 */
function fmtDuration(from: string, to: string): string | null {
  const a = parseMessageTs(from);
  const b = parseMessageTs(to);
  if (!a || !b) return null;
  const s = Math.max(0, Math.round((b.getTime() - a.getTime()) / 1000));
  if (s < 60) return `${s} 秒`;
  return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}

/**
 * 屏幕上的一轮（紧凑 IDE 式）：用户气泡 → 一个「过程」折叠面板（思考/工具同框）
 * → 最终回答 + 操作行。
 *
 * 为什么合并成一轮而不逐条渲染：一条带工具的回答在数据里是
 * `[用户 → AIMessage(思考+tool_calls) → ToolMessage → AIMessage(最终)]`，逐条渲染会把它
 * 散成三个突兀的框（用户反馈）。
 */
export default function TurnRow({
  turn,
  busy,
  selectMode,
  checked,
  editing,
  copied,
  onToggleSelect,
  onStartEdit,
  onEditChange,
  onEditCancel,
  onSaveEdit,
  onRegenerate,
  onCopy,
}: {
  turn: BuiltTurn<MessageRow>;
  busy: boolean;
  selectMode: boolean;
  /** 这一轮在多选删除里被勾上了（判据由页面持有：勾选集合是页面的事）。 */
  checked: boolean;
  /** 正在编辑的那一条（不是这一轮时为 null）。 */
  editing: { id: string; text: string; image?: string } | null;
  copied: boolean;
  onToggleSelect: () => void;
  onStartEdit: (id: string, text: string, image?: string) => void;
  onEditChange: (text: string) => void;
  onEditCancel: () => void;
  onSaveEdit: () => void;
  onRegenerate: () => void;
  onCopy: (text: string) => void;
}) {
  const userMid = turn.user?.id ?? "";
  const answerMid = turn.answer?.id ?? "";
  // 勾选自动扩展到整轮：一问一答共用一个勾选位（后端也按整轮删）。
  const selectId = userMid || answerMid;
  const rowTone = checked ? "opacity-60 ring-1 ring-amber-400" : "";
  const isEditingThis = !!userMid && editing?.id === userMid;
  const dur =
    turn.user && turn.answer ? fmtDuration(turn.user.ts ?? "", turn.answer.ts ?? "") : null;

  return (
    <div className={`group relative w-full ${rowTone}`}>
      {selectMode && !!selectId && (
        <input
          type="checkbox"
          aria-label={`选择这一轮：${(turn.user?.content ?? turn.answer?.content ?? "").slice(0, 12)}`}
          checked={checked}
          onChange={onToggleSelect}
          className="absolute -left-7 top-1 h-3.5 w-3.5 accent-amber-500"
        />
      )}
      {turn.user && (
        <div
          className={
            (isEditingThis
              ? "ml-auto w-full max-w-[80%]"
              : "ml-auto w-fit max-w-[80%]") + " mb-3"
          }
        >
          {isEditingThis && editing ? (
            <div className="rounded-2xl rounded-br-sm border border-blue-300 bg-blue-50 dark:bg-slate-800/70 p-2.5">
              <textarea
                autoFocus
                value={editing.text}
                onChange={(e) => onEditChange(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) onSaveEdit();
                  if (e.key === "Escape") onEditCancel();
                }}
                rows={3}
                className="w-full resize-y rounded-lg border border-blue-200 bg-white px-2.5 py-1.5 text-sm outline-none focus:border-blue-400"
              />
              <div className="mt-1.5 flex items-center justify-end gap-2 text-[11px]">
                <span className="text-slate-400 dark:text-slate-500">
                  发送后此条之后的历史将作废并重新生成（Ctrl+Enter 发送）
                </span>
                <button
                  onClick={onEditCancel}
                  className="rounded px-2 py-1 text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700"
                >
                  取消
                </button>
                <button
                  onClick={onSaveEdit}
                  className="rounded bg-blue-600 px-2.5 py-1 text-white hover:bg-blue-700"
                >
                  保存并重新生成
                </button>
              </div>
            </div>
          ) : (
            <>
              {/* 悬浮铅笔：absolute 不占布局（占位会把气泡挤到换行）。
                  删除模式下不画它：铅笔（-left-9）与轮前复选框（-left-7）叠在同一片像素上，
                  而 opacity-0 的元素照样吃点击 —— 真机探针实测勾不上（jsdom 的 fireEvent 不做
                  命中测试，所以这条只能靠浏览器验）。何况删除模式里意图是勾选，不是改写。 */}
              {!busy && !selectMode && !!userMid && (
                <button
                  onClick={() => onStartEdit(userMid, turn.user!.content, turn.user?.image)}
                  aria-label="编辑并重答"
                  title="编辑这条消息并重新生成（之后的对话会被作废）"
                  className="absolute -left-9 top-2 rounded-full p-1.5 text-slate-400 opacity-0 transition-opacity hover:bg-blue-50 hover:text-blue-600 group-hover:opacity-100 dark:text-slate-500 dark:hover:bg-slate-700/60 dark:hover:text-blue-400"
                >
                  <svg viewBox="0 0 20 20" fill="currentColor" className="h-3.5 w-3.5" aria-hidden="true">
                    <path d="M13.586 3.586a2 2 0 112.828 2.828l-.793.793-2.828-2.828.793-.793zM11.379 5.793L3 14.172V17h2.828l8.38-8.379-2.83-2.828z" />
                  </svg>
                </button>
              )}
              <div className="rounded-2xl rounded-br-sm bg-slate-200/90 px-4 py-2.5 text-slate-900 dark:bg-slate-700 dark:text-slate-100">
                {turn.user.image && (
                  <img
                    src={turn.user.image}
                    alt="对话附图"
                    className="mb-2 max-h-48 w-full rounded-lg object-contain"
                  />
                )}
                {turn.user.content && (
                  <p className="whitespace-pre-wrap">{turn.user.content}</p>
                )}
              </div>
              {turn.user.ts && (
                <p className="mt-1 text-right text-[10px] text-slate-500 dark:text-slate-400">
                  {turn.user.ts}
                </p>
              )}
            </>
          )}
        </div>
      )}
      {turn.steps.length > 0 && <ProcessPanel steps={turn.steps} />}
      {turn.answer && (
        <>
          <Markdown text={turn.answer.content} />
          <div className="mt-1 flex items-center gap-3 text-[11px] text-slate-400 dark:text-slate-500">
            {/* 这一句是她**主动**说的（R26-40 ①）：主动开口那句与前后的提问之间
                没有"用户消息"作分界，`splitSegments` 已经把它切成独立一段 ——
                判据就是 `turn.user === null`，**不新增后端字段**（那会给同一件事
                造第二个事实面）。它与"这一段不出现耗时"是同一条判据，所以两者
                永远不会互相矛盾。 */}
            {!turn.user && <Tag tone="blue">主动说的</Tag>}
            {dur && <span>耗时 {dur}</span>}
            <button
              onClick={() => onCopy(turn.answer!.content)}
              className="hover:text-slate-600 dark:hover:text-slate-300"
            >
              {copied ? "已复制" : "复制"}
            </button>
            {turn.user?.id && !busy && (
              <button
                onClick={onRegenerate}
                className="hover:text-slate-600 dark:hover:text-slate-300"
              >
                重新生成
              </button>
            )}
            {turn.answer.ts && <span>{turn.answer.ts}</span>}
          </div>
          {/* 这一轮是被叫停的半句（R26-13 尾）：标记随 checkpoint 落库，所以刷新后仍在。
              这句提示原先挂在页面 state 上（SSE 的 `End.stopped`），一刷新就没了 —— 挂在
              气泡里也不行，收尾时回放会整体换掉气泡；标记进了历史，提示才能跟着历史走。
              "别的窗口按的停"（桌宠的「停止」）同样被落库的标记覆盖 —— 那扇窗没流过，
              本地 `signal.aborted` 认不出它，判据只能来自后端。 */}
          {turn.answer.stopped && (
            <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">{STOP_HINT}</p>
          )}
        </>
      )}
    </div>
  );
}
