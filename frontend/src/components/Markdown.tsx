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
 *
 * `stripNode`：react-markdown 会把内部的 `node`（AST 对象）混进组件 props，
 * 原样 `{...p}` 铺到真实 DOM 上就是每个元素一个巨型垃圾属性
 * （实测 `<p class="x" node="[object Object]">`）—— 所以先剥掉再展开。
 */
function stripNode<T extends { node?: unknown }>(props: T): Omit<T, "node"> {
  const { node: _node, ...rest } = props;
  return rest;
}

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="text-sm leading-relaxed">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={{
          p: (p) => (
            <p className="my-1.5 first:mt-0 last:mb-0" {...stripNode(p)} />
          ),
          ul: (p) => (
            <ul className="my-1.5 list-disc pl-5 first:mt-0 last:mb-0" {...stripNode(p)} />
          ),
          ol: (p) => (
            <ol className="my-1.5 list-decimal pl-5 first:mt-0 last:mb-0" {...stripNode(p)} />
          ),
          li: (p) => <li className="my-0.5" {...stripNode(p)} />,
          h1: (p) => <h1 className="my-2 text-base font-semibold first:mt-0" {...stripNode(p)} />,
          h2: (p) => <h2 className="my-2 text-base font-semibold first:mt-0" {...stripNode(p)} />,
          h3: (p) => <h3 className="my-1.5 text-sm font-semibold first:mt-0" {...stripNode(p)} />,
          a: (p) => (
            <a
              className="text-blue-600 underline dark:text-blue-400"
              target="_blank"
              rel="noreferrer"
              {...stripNode(p)}
            />
          ),
          blockquote: (p) => (
            <blockquote
              className="my-1.5 border-l-2 border-slate-300 pl-2 text-slate-500 dark:border-slate-600 dark:text-slate-400"
              {...stripNode(p)}
            />
          ),
          pre: (p) => (
            <pre
              className="my-1.5 overflow-x-auto rounded-lg bg-slate-900 p-3 text-[12px] leading-relaxed text-slate-100 dark:bg-slate-900/80"
              {...stripNode(p)}
            />
          ),
          code: (p) => {
            const rest = stripNode(p);
            const inline = !((rest as { className?: string }).className || "").includes("language-");
            if (inline) {
              return (
                <code
                  className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[12px] dark:bg-slate-700/70"
                  {...rest}
                />
              );
            }
            return <code className="font-mono" {...rest} />;
          },
          table: (p) => (
            <div className="my-1.5 overflow-x-auto">
              <table className="w-full border-collapse text-xs" {...stripNode(p)} />
            </div>
          ),
          th: (p) => (
            <th
              className="border border-slate-200 bg-slate-50 px-2 py-1 text-left font-medium dark:border-slate-600 dark:bg-slate-700/50"
              {...stripNode(p)}
            />
          ),
          td: (p) => (
            <td className="border border-slate-200 px-2 py-1 align-top dark:border-slate-600" {...stripNode(p)} />
          ),
          hr: () => <hr className="my-2 border-slate-200 dark:border-slate-600" />,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
