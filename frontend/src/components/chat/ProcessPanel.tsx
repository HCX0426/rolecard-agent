import type { TurnMessageLike, TurnStep } from "../../lib/turns";
import { Markdown } from "../Markdown";
import ThinkingPanel from "./ThinkingPanel";
import ToolStepCard from "./ToolStepCard";

/** 一轮的「过程」折叠面板：思考与工具调用同处一个框，展开后按序可读。
 *
 * 为什么合并：一条带工具的回答在数据里是 [AIMessage(思考+tool_calls) → ToolMessage →
 * AIMessage(最终)]，逐条渲染会散成"思考框 / 工具卡 / 思考框"三个突兀的框（用户反馈）。
 * 每个思考步再各自独立折叠——看完一段可以收起来再看下一段。
 */
export default function ProcessPanel({ steps }: { steps: TurnStep<TurnMessageLike>[] }) {
  // 只有一步：不再套「过程」外层 —— 展开就是那一步本身。
  // （单步时套两层等于"点两下才看到内容"，用户反馈；多步才需要先合并再逐段展开。）
  if (steps.length === 1) {
    const only = steps[0];
    if (only.kind === "think") {
      // 复用流式期间那个面板（标题/样式只有一处定义，也不再带"（N 字）"后缀），
      // 但回放路径恒为**折叠**：流式期间展开是因为"正在思考"本身是过程信号，
      // 结束之后展开就该是用户自己的动作（用户 2026-09-16 明确）。
      return (
        <div className="mb-2.5">
          <ThinkingPanel text={only.text} defaultOpen={false} />
        </div>
      );
    }
    if (only.kind === "text") {
      return (
        <div className="mb-2.5 text-sm text-slate-600 dark:text-slate-300">
          <Markdown text={only.text} />
        </div>
      );
    }
    return (
      <div className="mb-2.5">
        <ToolStepCard
          step={{
            id: 0,
            name: only.msg.name || "tool",
            status: "ok",
            content: only.msg.content ?? "",
            args: only.msg.args,
          }}
        />
      </div>
    );
  }
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
