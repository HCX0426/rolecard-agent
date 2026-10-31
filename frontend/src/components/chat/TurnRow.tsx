import { memo } from "react";

import type { BuiltTurn } from "../../lib/turns";

import Button from "../ui/Button";
import { answeredByHint, STOP_HINT } from "../../lib/stream";
import { formatMessageTs, parseMessageTs } from "../../lib/quiet";
import type { MessageRow } from "../../api";
import { MarkdownLazy } from "../MarkdownLazy";
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
 *
 * **为什么是 memo 的（R1031-31）**：流式期间每来一个 token，页面就重渲一次；从前的
 * TurnRow 没有 memo，于是那几百行历史每 token 重渲一遍 —— 实测（`src/bench/
 * streaming.bench.test.tsx`）每提交耗时随历史长度线性涨（10 轮 1.79ms vs 200 轮
 * 11.51ms，比值 6.4×），500 轮（后端硬顶）外推约 26.9ms/提交 ⇒ 一句话的纯渲染
 * 开销上到秒级。
 *
 * memo 只在**props 身份不变**时才拦得住重渲 —— 所以回调一律是页面级的稳定函数，
 * 每行要用的东西（selectId / turn / key）由行内当参数带上，而不是包成闭包塞进来。
 * 改这一层时记得连着改 ChatPage 那一侧，否则 memo 会静默失效（props 每次都变）。
 */
function TurnRow({
  turn,
  roleName,
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
  /** 回答者的角色名（角色签用；2026-10-25 玻璃重设计）。 */
  roleName?: string;
  busy: boolean;
  selectMode: boolean;
  /** 这一轮在多选删除里被勾上了（判据由页面持有：勾选集合是页面的事）。 */
  checked: boolean;
  /** 正在编辑的那一条（不是这一轮时为 null）。 */
  editing: { id: string; text: string; image?: string } | null;
  copied: boolean;
  /** 勾选/取消这一轮 —— **参数带 id**（页面级稳定函数，不包闭包）。 */
  onToggleSelect: (selectId: string) => void;
  /** 开始编辑这一条（参数是消息 id/正文/附图）。 */
  onStartEdit: (id: string, text: string, image?: string) => void;
  onEditChange: (text: string) => void;
  onEditCancel: () => void;
  onSaveEdit: () => void;
  /** 重新生成这一轮 —— **参数带这一轮**（页面级稳定函数）。 */
  onRegenerate: (turn: BuiltTurn<MessageRow>) => void;
  /** 复制这一轮的回答 —— **参数带正文与轮 key**。 */
  onCopy: (text: string, key: string) => void;
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
          onChange={() => onToggleSelect(selectId)}
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
                className="w-full resize-y rounded-lg border border-blue-200 bg-white px-2.5 py-1.5 text-sm outline-none focus-visible:border-2"
              />
              <div className="mt-1.5 flex items-center justify-end gap-2 text-11">
                <span className="text-slate-400 dark:text-slate-500">
                  发送后此条之后的历史将作废并重新生成（Ctrl+Enter 发送）
                </span>
                <Button variant="ghost" size="sm" onClick={onEditCancel}>
                  取消
                </Button>
                <Button variant="primary" size="sm" onClick={onSaveEdit}>
                  保存并重新生成
                </Button>
              </div>
            </div>
          ) : (
            <>
              {/* 铅笔（编辑并重答）挂在**消息下沿的时间行**里（2026-10-31 用户："缩小放在
                  我发出去消息的下边"）：从前 absolute -left-9 悬浮在气泡左侧，位置飘、热区
                  与删除模式复选框同片像素（opacity-0 照样吃点击，真机探针实测勾不上）。
                  现在与时间同排右对齐、尺寸缩一档，仍悬停显现。删除模式下不画它：
                  那时意图是勾选，不是改写。 */}
              <div className="rounded-2xl rounded-br-sm border border-white/50 bg-white/70 px-4 py-2.5 text-slate-900 backdrop-blur-sm dark:border-white/10 dark:bg-slate-800/70 dark:text-slate-100">
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
                <div className="mt-1 flex items-center justify-end gap-1.5">
                  <p className="text-10 text-slate-500 dark:text-slate-400">
                    {formatMessageTs(turn.user.ts)}
                  </p>
                  {!busy && !selectMode && !!userMid && (
                    <Button
                      variant="ghost"
                      onClick={() => onStartEdit(userMid, turn.user!.content, turn.user?.image)}
                      aria-label="编辑并重答"
                      title="编辑这条消息并重新生成（之后的对话会被作废）"
                      className="rounded-full p-0.5 text-slate-400 opacity-0 transition-opacity hover:bg-blue-50 hover:text-blue-600 group-hover:opacity-100 dark:text-slate-500 dark:hover:bg-slate-700/60 dark:hover:text-blue-400"
                    >
                      <svg viewBox="0 0 20 20" fill="currentColor" className="h-3 w-3" aria-hidden="true">
                        <path d="M13.586 3.586a2 2 0 112.828 2.828l-.793.793-2.828-2.828.793-.793zM11.379 5.793L3 14.172V17h2.828l8.38-8.379-2.83-2.828z" />
                      </svg>
                    </Button>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}
      {turn.steps.length > 0 && <ProcessPanel steps={turn.steps} />}
      {turn.answer && (
        <>
          {/* 角色签：强调色圆点 + 名字。回答区不再是无名的一坨文字 ——
              "这句是谁说的"是角色化产品的第一信息（2026-10-25 玻璃重设计）。 */}
          {roleName && (
            <div className="mb-1 flex items-center gap-1.5">
              <span
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ background: "var(--accent)" }}
                aria-hidden="true"
              />
              <span className="text-11 text-slate-500 dark:text-slate-400">{roleName}</span>
            </div>
          )}
          <MarkdownLazy text={turn.answer.content} />
          <div className="mt-1 flex items-center gap-3 text-11 text-slate-400 dark:text-slate-500">
            {/* 这一句是她**主动**说的（R26-40 ①）：主动开口那句与前后的提问之间
                没有"用户消息"作分界，`splitSegments` 已经把它切成独立一段 ——
                判据就是 `turn.user === null`，**不新增后端字段**（那会给同一件事
                造第二个事实面）。它与"这一段不出现耗时"是同一条判据，所以两者
                永远不会互相矛盾。 */}
            {!turn.user && <Tag tone="blue">主动说的</Tag>}
            {dur && <span>耗时 {dur}</span>}
            <Button variant="link" onClick={() => onCopy(turn.answer!.content, turn.key)}>
              {copied ? "已复制" : "复制"}
            </Button>
            {turn.user?.id && !busy && (
              <Button variant="link" onClick={() => onRegenerate(turn)}>
                重新生成
              </Button>
            )}
            {turn.answer.ts && <span>{formatMessageTs(turn.answer.ts)}</span>}
          </div>
          {/* 这一轮是被叫停的半句（R26-13 尾）：标记随 checkpoint 落库，所以刷新后仍在。
              这句提示原先挂在页面 state 上（SSE 的 `End.stopped`），一刷新就没了 —— 挂在
              气泡里也不行，收尾时回放会整体换掉气泡；标记进了历史，提示才能跟着历史走。
              "别的窗口按的停"（桌宠的「停止」）同样被落库的标记覆盖 —— 那扇窗没流过，
              本地 `signal.aborted` 认不出它，判据只能来自后端。 */}
          {turn.answer.stopped && (
            <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">{STOP_HINT}</p>
          )}
          {/* ENGI-36 B：这一轮实际由云端答的话（请求的是本地档）。与 `stopped` 同一条通路 ——
              标记随 checkpoint 落库，所以刷新后仍在；live 那一份走 SSE 的 `answered_by` 事件
              （见 ChatPage 的 live 气泡）。这是隐私事实，不能只靠界面 state（一刷新就没）。 */}
          {turn.answer.answered_by && (
            <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
              {answeredByHint(turn.answer.answered_by)}
            </p>
          )}
        </>
      )}
    </div>
  );
}

/**
 * memo 的比较器用默认的**浅比较**：props 里的 `turn` 是 `useMemo` 出来的稳定引用
 * （`buildTurns(messages)` 只在 messages 变时重建），回调是页面级稳定函数 ——
 * 于是每 token 的那次父级重渲里，这一行的 props 全同 ⇒ React 直接跳过它的渲染。
 * 哪一格变了（勾选状态、复制反馈、编辑态、busy）都会照常重渲 —— 那几处本来就该重渲。
 */
export default memo(TurnRow);
