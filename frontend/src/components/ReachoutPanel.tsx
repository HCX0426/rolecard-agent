// 角色主动开口收件箱（架构计划 B）：铃铛点开后的抽屉。
// 纯静音设计：不弹提示音、不振动；红点与列表只是视觉呈现。
// 点一条 = 标记已读（后端会返回最新列表，直接吸收）。

import { useEffect, useState } from "react";
import { api, type ReachoutsPage, type ReachoutRow } from "../api";

export default function ReachoutPanel({
  open,
  onClose,
  onUnreadChange,
}: {
  open: boolean;
  onClose: () => void;
  onUnreadChange: (unread: number) => void;
}) {
  const [data, setData] = useState<ReachoutsPage | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!open) return;
    setErr("");
    api
      .getReachouts()
      .then((p) => {
        setData(p);
        onUnreadChange(p.unread);
      })
      .catch((e) => setErr(`加载失败：${(e as Error).message}`));
  }, [open, onUnreadChange]);

  async function markRead(row: ReachoutRow) {
    if (row.state === "read") return;
    try {
      const p = await api.markReachoutRead(row.id);
      setData(p);
      onUnreadChange(p.unread);
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
            角色主动找你
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
              还没有角色主动找过你
            </p>
          )}
          {data?.items.map((row) => (
            <button
              key={row.id}
              onClick={() => markRead(row)}
              className={`mb-1 flex w-full flex-col rounded-lg border px-3 py-2 text-left transition-colors ${
                row.state === "unread"
                  ? "border-blue-200 bg-blue-50/60 dark:border-blue-800 dark:bg-blue-900/20"
                  : "border-slate-100 opacity-70 dark:border-slate-700"
              }`}
            >
              <span className="flex items-center justify-between text-xs">
                <span className="font-medium text-slate-800 dark:text-slate-100">
                  {row.role_name || row.role_id}
                </span>
                {row.state === "unread" && (
                  <span className="rounded-full bg-blue-600 px-1.5 py-0.5 text-[10px] text-white">
                    未读
                  </span>
                )}
              </span>
              <span className="mt-1 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
                {row.text}
              </span>
              <span className="mt-1 text-[10px] text-slate-400">
                {row.created_at?.replace("T", " ").slice(0, 16) || ""}
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}