// 命令执行审批面板（架构计划 C·§6.2）：铃铛按钮点开后的右侧抽屉。
// 与 ReachoutPanel 样式同源（审查报告 UI 一致性），内容是待批命令列表：
// 每条：命令 + cwd + 提交角色 + 时间；pending 可 approve / reject，结果（done）可展开看输出。
// 轮询在 App.tsx 侧，Panel 本身只在打开时拉一次最新列表；操作后原地更新。

import { useEffect, useState } from "react";
import {
  api,
  type ApprovalResult,
  type ApprovalRow,
  type ApprovalsPage,
  type ApprovalStatus,
} from "../api";
import { formatBytes } from "../lib/bytes";
import { formatUtcNaive } from "../lib/quiet";

function StatusBadge({ status }: { status: ApprovalStatus }) {
  const style =
    status === "pending"
      ? "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300"
      : status === "approved"
        ? "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300"
        : status === "done"
          ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300"
          : "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300";
  const label =
    status === "pending"
      ? "待批准"
      : status === "approved"
        ? "已批准"
        : status === "done"
          ? "已完成"
          : "已拒绝";
  return (
    <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${style}`}>
      {label}
    </span>
  );
}

function ResultBlock({ result }: { result: ApprovalResult | null }) {
  if (!result) return null;
  return (
    <div className="mt-1.5 rounded-md border border-slate-100 bg-slate-50 p-2 text-[10px] leading-relaxed text-slate-600 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-400">
      <div className="flex gap-3 text-slate-500 dark:text-slate-500">
        <span>退出码 {result.exit_code}</span>
        <span>{result.duration_ms}ms</span>
        <span>{formatBytes(result.output_bytes)}</span>
      </div>
      {result.output && (
        <pre className="mt-1 max-h-32 overflow-x-auto whitespace-pre-wrap break-all font-mono text-[10px] text-slate-700 dark:text-slate-300">
          {result.output}
        </pre>
      )}
    </div>
  );
}

export default function ApprovalPanel({
  open,
  onClose,
  onPendingChange,
}: {
  open: boolean;
  onClose: () => void;
  onPendingChange: (pending: number) => void;
}) {
  const [data, setData] = useState<ApprovalsPage | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!open) return;
    setErr("");
    api
      .getApprovals()
      .then((p) => {
        setData(p);
        onPendingChange(p.pending);
      })
      .catch((e) => setErr(`加载失败：${(e as Error).message}`));
  }, [open, onPendingChange]);

  async function decide(row: ApprovalRow, decision: "approve" | "reject") {
    if (row.status !== "pending") return;
    try {
      // 令牌随行下发、用一次即废：过期或从没拿到时后端回 403，detail 已经是一句人话
      // （"请重新查看待批列表后再批"），这里原样显示，不在前端另造一套判断。
      const updated = await api.decideApproval(row.id, decision, row.decide_token);
      setData((prev) => {
        if (!prev) return prev;
        const items = prev.items.map((r) => (r.id === updated.id ? updated : r));
        const pending = items.filter((r) => r.status === "pending").length;
        onPendingChange(pending);
        return { ...prev, items, pending };
      });
    } catch (e) {
      setErr(`操作失败：${(e as Error).message}`);
    }
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/40" onMouseDown={onClose}>
      <div
        className="absolute right-0 top-0 flex h-full w-80 flex-col border-l border-slate-200 bg-white shadow-xl dark:border-slate-700 dark:bg-slate-800"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2.5 dark:border-slate-700">
          <span className="text-sm font-medium text-slate-900 dark:text-slate-100">
            待审批命令
          </span>
          <button
            onClick={onClose}
            className="text-xs text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
          >
            关闭
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-2">
          {err && <p className="px-2 py-1 text-xs text-red-600 dark:text-red-400">{err}</p>}
          {data && data.items.length === 0 && (
            <p className="px-2 py-6 text-center text-xs text-slate-400 dark:text-slate-500">
              还没有待审批的命令
            </p>
          )}
          {data?.items.map((row) => (
            <div
              key={row.id}
              className={`mb-1.5 rounded-lg border px-3 py-2 ${
                row.status === "pending"
                  ? "border-amber-200 bg-amber-50/60 dark:border-amber-800 dark:bg-amber-900/20"
                  : row.status === "done"
                    ? "border-green-100 bg-green-50/40 dark:border-green-900 dark:bg-green-900/10"
                    : row.status === "approved"
                      ? "border-blue-100 bg-blue-50/40 dark:border-blue-900 dark:bg-blue-900/10"
                      : "border-slate-100 opacity-70 dark:border-slate-700"
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <span className="flex-1 truncate font-mono text-xs text-slate-800 dark:text-slate-100">
                  {row.command}
                </span>
                <StatusBadge status={row.status} />
              </div>
              {row.cwd && (
                <span className="mt-1 block truncate text-[10px] text-slate-500 dark:text-slate-500">
                  {row.cwd}
                  {row.role_name ? ` · ${row.role_name}` : ""}
                </span>
              )}
              <div className="mt-1 flex items-center justify-between">
                <span className="text-[10px] text-slate-400">
                  #{row.id} · {formatUtcNaive(row.created_at)}
                </span>
                {row.status === "pending" && (
                  <div className="flex flex-col items-end gap-0.5">
                    <div className="flex gap-1">
                      <button
                        onClick={() => decide(row, "approve")}
                        disabled={!row.decide_token}
                        className="rounded bg-green-600 px-2 py-0.5 text-[10px] font-medium text-white hover:bg-green-700 disabled:bg-slate-300 disabled:text-slate-500 dark:disabled:bg-slate-700 dark:disabled:text-slate-500"
                      >
                        批准
                      </button>
                      <button
                        onClick={() => decide(row, "reject")}
                        disabled={!row.decide_token}
                        className="rounded bg-red-600 px-2 py-0.5 text-[10px] font-medium text-white hover:bg-red-700 disabled:bg-slate-300 disabled:text-slate-500 dark:disabled:bg-slate-700 dark:disabled:text-slate-500"
                      >
                        拒绝
                      </button>
                    </div>
                    {/* 决定令牌是一次性的：为 null 说明这条已被批过、或列表是旧的。
                        与其让人点一下吃 403，不如当场说清怎么恢复（禁用必须配说明）。 */}
                    {!row.decide_token && (
                      <span className="text-[10px] leading-tight text-slate-400 dark:text-slate-500">
                        这条已经批过了，或者这份列表是旧的 —— 关掉重开待批列表再批
                      </span>
                    )}
                  </div>
                )}
              </div>
              {row.status === "done" && <ResultBlock result={row.result} />}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
