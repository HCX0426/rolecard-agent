// 角色主动开口收件箱（架构计划 B）：铃铛点开后的抽屉。
// 纯静音设计：不弹提示音、不振动；红点与列表只是视觉呈现。
// 点一条 = 打开该角色的「主动会话」（那里能翻历史、能直接回话）+ 顺手把这个角色的未读标完。
// 可按角色筛选（架构计划 §5.3 按角色卡隔离查看历史）。
//
// 为什么跳转而不是就地回复：主动消息现在同时是"该角色的一条真消息"，落在它的主动会话里。
// 就地再做一个回复框等于把同一次对话做出两个入口、两套状态（历史、流式、审核都在对话页那边）。

import { useEffect, useMemo, useState } from "react";
import { api, type ReachoutsPage, type ReachoutRow } from "../api";

export default function ReachoutPanel({
  open,
  onClose,
  onUnreadChange,
  onOpenThread,
}: {
  open: boolean;
  onClose: () => void;
  onUnreadChange: (unread: number) => void;
  /** 跳进某个会话（App 负责改 hash 与切页签）。 */
  onOpenThread: (threadId: string) => void;
}) {
  const [data, setData] = useState<ReachoutsPage | null>(null);
  const [err, setErr] = useState("");
  const [roleFilter, setRoleFilter] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setErr("");
    api
      .getReachouts(roleFilter ?? undefined)
      .then((p) => {
        setData(p);
        onUnreadChange(p.unread);
      })
      .catch((e) => setErr(`加载失败：${(e as Error).message}`));
  }, [open, roleFilter, onUnreadChange]);

  // 从当前列表里聚合出现过的角色，供筛选下拉（按角色卡隔离查看历史）。
  const roles = useMemo(() => {
    const seen = new Map<string, string>();
    for (const r of data?.items ?? []) {
      if (r.role_id) seen.set(r.role_id, r.role_name || r.role_id);
    }
    return [...seen.entries()].map(([id, name]) => ({ id, name }));
  }, [data]);

  async function openRow(row: ReachoutRow) {
    if (!row.thread_id) {
      // 没有主动会话可去（本功能上线前的老消息）→ 退回"只标记这一条已读"。
      try {
        const p = await api.markReachoutRead(row.id);
        setData(p);
        onUnreadChange(p.unread);
      } catch (e) {
        setErr(`操作失败：${(e as Error).message}`);
      }
      return;
    }
    // 先标已读再跳转：跳完这个抽屉就关掉了，回来 setData 没有意义。
    // 但**标记失败绝不拦跳转** —— 用户要的是看到那条消息，红点数下一轮轮询自然校正。
    try {
      const p = await api.markRoleReachoutsRead(row.role_id);
      setData(p);
      onUnreadChange(p.unread);
    } catch {
      /* 静默：跳转优先 */
    }
    onOpenThread(row.thread_id);
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
        {roles.length > 1 && (
          <div className="border-b border-slate-100 px-3 py-2 dark:border-slate-700">
            <label className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
              <span>只看</span>
              <select
                value={roleFilter ?? ""}
                onChange={(e) => setRoleFilter(e.target.value || null)}
                className="flex-1 rounded border border-slate-200 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-800"
              >
                <option value="">全部角色</option>
                {roles.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
        {!!data?.file_watch_pending && (
          <div className="border-b border-slate-100 px-4 py-1.5 text-[11px] text-slate-400 dark:border-slate-700 dark:text-slate-500">
            任务目录有 {data.file_watch_pending} 项变化，正等着角色找话题
          </div>
        )}
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
              onClick={() => void openRow(row)}
              title={row.thread_id ? "打开与该角色的对话（可翻历史、可直接回复）" : "标记为已读"}
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
              <span className="mt-1 flex items-center justify-between text-[10px] text-slate-400">
                <span>{row.created_at?.replace("T", " ").slice(0, 16) || ""}</span>
                {row.thread_id ? (
                  <span className="text-blue-600 dark:text-blue-400">打开对话并回复 →</span>
                ) : (
                  <span>标记已读</span>
                )}
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}