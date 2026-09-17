import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";

/**
 * 助手消息的 Markdown 渲染（用户 2026-09-17："能更好你就改"）。
 *
 * - `remarkGfm`：表格 / 删除线 / 任务列表；
 * - `remarkBreaks`：单个换行也断行（聊天场景预期，模型输出常用单换行分点）；
 * - 样式经 components 映射内联（项目未装 typography 插件），深浅色都适配。
 *
 * 只用于**助手消息**：用户输入是字面文本，保持 whitespace-pre-wrap 原样展示。
 */
export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="text-sm leading-relaxed">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={{
          p: (p) => <p className="my-1.5 first:mt-0 last:mb-0" {...p} />,
          ul: (p) => <ul className="my-1.5 list-disc pl-5 first:mt-0 last:mb-0" {...p} />,
          ol: (p) => <ol className="my-1.5 list-decimal pl-5 first:mt-0 last:mb-0" {...p} />,
          li: (p) => <li className="my-0.5" {...p} />,
          h1: (p) => <h1 className="my-2 text-base font-semibold first:mt-0" {...p} />,
          h2: (p) => <h2 className="my-2 text-base font-semibold first:mt-0" {...p} />,
          h3: (p) => <h3 className="my-1.5 text-sm font-semibold first:mt-0" {...p} />,
          a: (p) => (
            <a className="text-blue-600 underline dark:text-blue-400" target="_blank" rel="noreferrer" {...p} />
          ),
          blockquote: (p) => (
            <blockquote className="my-1.5 border-l-2 border-slate-300 pl-2 text-slate-500 dark:border-slate-600 dark:text-slate-400" {...p} />
          ),
          pre: (p) => (
            <pre
              className="my-1.5 overflow-x-auto rounded-lg bg-slate-900 p-3 text-[12px] leading-relaxed text-slate-100 dark:bg-slate-900/80"
              {...p}
            />
          ),
          code: (p) => {
            const inline = !(p.className || "").includes("language-");
            if (inline) {
              return (
                <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[12px] dark:bg-slate-700/70" {...p} />
              );
            }
            return <code className="font-mono" {...p} />;
          },
          table: (p) => (
            <div className="my-1.5 overflow-x-auto">
              <table className="w-full border-collapse text-xs" {...p} />
            </div>
          ),
          th: (p) => (
            <th className="border border-slate-200 bg-slate-50 px-2 py-1 text-left font-medium dark:border-slate-600 dark:bg-slate-700/50" {...p} />
          ),
          td: (p) => (
            <td className="border border-slate-200 px-2 py-1 align-top dark:border-slate-600" {...p} />
          ),
          hr: () => <hr className="my-2 border-slate-200 dark:border-slate-600" />,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
