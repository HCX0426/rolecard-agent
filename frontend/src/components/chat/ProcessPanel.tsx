import type { TurnMessageLike, TurnStep } from "../../lib/turns";
import { Markdown } from "../Markdown";
import ToolStepCard from "./ToolStepCard";

/** 一轮的「过程」折叠面板：思考与工具调用同处一个框，展开后按序可读。
 *
 * 为什么合并：一条带工具的回答在数据里是 [AIMessage(思考+tool_calls) → ToolMessage →
 * AIMessage(最终)]，逐条渲染会散成"思考框 / 工具卡 / 思考框"三个突兀的框（用户反馈）。
 * 每个思考步再各自独立折叠——看完一段可以收起来再看下一段。
 */
export default function ProcessPanel({ steps }: { steps: TurnStep<TurnMessageLike>[] }) {
  const thinks = steps.filter((s) => s.kind === "think").length;
  const tools = steps.filter((s) => s.kind === "tool").length;
  const parts = [thinks ? `思考 ×${thinks}` : "", tools ? `工具 ×${tools}` : ""].filter(Boolean);
  return (
    <details className="group/proc mb-2.5 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50/70 dark:bg-slate-800/50 px-2.5 py-1.5">
      <summary className="cursor-pointer select-none text-xs text-slate-400 dark:text-slate-500">
        过程{parts.length ? ` · ${parts.join(" · ")}` : ""}
      </summary>
      <div className="mt-1.5 space-y-1.5">
        {steps.map((s, i) =>
          s.kind === "think" ? (
            <details
              key={i}
              className="rounded border border-slate-200/70 bg-white dark:border-slate-700 dark:bg-slate-800"
            >
              <summary className="cursor-pointer select-none px-2 py-1 text-[11px] text-slate-400 dark:text-slate-500">
                思考 {steps.slice(0, i + 1).filter((x) => x.kind === "think").length}
                {s.text.length > 120 ? `（${s.text.length} 字）` : ""}
              </summary>
              <pre className="max-h-56 overflow-auto whitespace-pre-wrap px-2 pb-2 text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">
                {s.text}
              </pre>
            </details>
          ) : s.kind === "text" ? (
            <div key={i} className="text-xs text-slate-500 dark:text-slate-400">
              <Markdown text={s.text} />
            </div>
          ) : (
            <ToolStepCard
              key={i}
              step={{
                id: 0,
                name: s.msg.name || "tool",
                status: "ok",
                content: s.msg.content ?? "",
                args: s.msg.args,
              }}
            />
          ),
        )}
      </div>
    </details>
  );
}
