// 角色主动开口收件箱（架构计划 B）：铃铛点开后的抽屉。
// 纯静音设计：不弹提示音、不振动；红点与列表只是视觉呈现。
// 点一条 = 打开该角色的「主动会话」（那里能翻历史、能直接回话）+ 顺手把这个角色的未读标完。
// 可按角色筛选（架构总览 §5 按角色卡隔离查看历史）。
//
// 为什么跳转而不是就地回复：主动消息现在同时是"该角色的一条真消息"，落在它的主动会话里。
// 就地再做一个回复框等于把同一次对话做出两个入口、两套状态（历史、流式、审核都在对话页那边）。
//
// 折叠口径（docs/主动消息与记忆设计稿.md §1）：同一角色在 `merge_days` 天窗口里攒下的开口
// **折成一行**，一行 = 角色名 + 时间标签 + 最新一条摘要 + 未读角标。分组只做在这里：
// 后端不算第二份分组逻辑（它只负责把 `merge_days` 随列表带回来），否则接口聚合与界面
// 显示各算一遍，改窗口时只有一边会跟着变。
// 默认展开的规则偏向"别藏东西"：只有一条、或这一摞里有未读 → 直接摊开；
// 只有**整摞都已读**才折起来 —— 折叠要解决的是旧消息糊成流水账，不是藏新消息。

import { useEffect, useMemo, useState } from "react";
import { api, type ReachoutsPage, type ReachoutRow } from "../api";
import { useConfirm } from "../hooks/useConfirm";

const DAY_MS = 86_400_000;

interface Stack {
  key: string;
  role_id: string;
  role_name: string;
  label: string;
  items: ReachoutRow[];
  unread: number;
}

/** 时间戳解析：后端给的是 "YYYY-MM-DD HH:MM:SS"（或 ISO 带 T），按**本地**时间理解。 */
function stamp(row: ReachoutRow): number {
  if (!row.created_at) return Number.NaN;
  const ms = new Date(row.created_at.replace(" ", "T")).getTime();
  return Number.isNaN(ms) ? Number.NaN : ms;
}

/** 桶标签：窗口宽 1 天说"今天/昨天"，宽 3/7 天说"这条摞 newest 落在哪段"。 */
function bucketLabel(newest: number, oldest: number, width: number): string {
  if (Number.isNaN(newest)) return "更早";
  const now = Date.now();
  const days = Math.max(0, Math.floor((now - newest) / DAY_MS));
  const w = Math.max(1, width);
  if (w === 1) {
    if (days === 0) return "今天";
    if (days === 1) return "昨天";
    return formatDate(newest);
  }
  if (days < w) return `最近 ${w} 天`;
  const from = formatDate(oldest);
  const to = formatDate(newest);
  return from === to ? from : `${from}–${to}`;
}

function formatDate(ms: number): string {
  const d = new Date(ms);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

function buildStacks(items: ReachoutRow[], width: number): Stack[] {
  const now = Date.now();
  const w = Math.max(1, width);
  const buckets = new Map<string, Stack>();
  for (const row of items) {
    const ms = stamp(row);
    // 没有时间戳的旧行（本功能上线前的记录）全部归进"更早"一摞，不装作它们是今天的。
    const bucket = Number.isNaN(ms) ? -1 : Math.floor((now - ms) / (DAY_MS * w));
    const key = `${row.role_id || "?"}#${bucket}`;
    const stack = buckets.get(key);
    if (stack) {
      stack.items.push(row);
      if (row.state === "unread") stack.unread += 1;
    } else {
      buckets.set(key, {
        key,
        role_id: row.role_id,
        role_name: row.role_name || row.role_id || "未知角色",
        label: "",
        items: [row],
        unread: row.state === "unread" ? 1 : 0,
      });
    }
  }
  const out = [...buckets.values()];
  for (const stack of out) {
    const marks = stack.items.map(stamp).filter((n) => !Number.isNaN(n));
    stack.label = bucketLabel(
      marks.length ? Math.max(...marks) : Number.NaN,
      marks.length ? Math.min(...marks) : Number.NaN,
      w,
    );
  }
  // 组按"组内最新一条"从新到旧排；无时间戳的那摞沉到最后。
  return out.sort((a, b) => {
    const av = Math.max(0, ...a.items.map(stamp).filter((n) => !Number.isNaN(n)));
    const bv = Math.max(0, ...b.items.map(stamp).filter((n) => !Number.isNaN(n)));
    return bv - av;
  });
}

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
  // 手动展开/折起的组（key → 是否展开）。没记的组走默认规则，见 `isOpen`。
  const [toggled, setToggled] = useState<Record<string, boolean>>({});
  const confirm = useConfirm();

  /** 重读列表并把未读报回给铃铛。删完之后必须走它，否则红点与列表会各说一套。 */
  function reload() {
    setErr("");
    return api
      .getReachouts(roleFilter ?? undefined)
      .then((p) => {
        setData(p);
        onUnreadChange(p.unread);
      })
      .catch((e) => setErr(`加载失败：${(e as Error).message}`));
  }

  useEffect(() => {
    if (!open) return;
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, roleFilter]);

  /** 删一行 = 只让这条投递记录从抽屉消失。**她说过的那句仍在对话里**，文案要说清这一点。 */
  async function removeRow(row: ReachoutRow) {
    if (
      !(await confirm({
        title: "从抽屉里删掉这条？",
        body: "只是删掉这条提醒记录。她说出口的那句话仍然留在你们的对话里（那是她下次开口的依据）—— 要连话一起抹掉，去对话里删那条。",
        confirmText: "删除",
        danger: true,
      }))
    )
      return;
    try {
      await api.deleteReachout(row.id);
      await reload();
    } catch (e) {
      setErr(`删除失败：${(e as Error).message}`);
    }
  }

  async function clearAll() {
    const scope = roleFilter ? `「${roles.find((r) => r.id === roleFilter)?.name ?? roleFilter}」` : "所有角色";
    if (
      !(await confirm({
        title: `清空${scope}的主动消息记录？`,
        body: "抽屉会空出来。各条主动会话里的原话不动，她仍然记得自己主动找过你；已提炼进记忆的事实也不跟着走。",
        confirmText: "清空",
        danger: true,
      }))
    )
      return;
    try {
      await api.clearReachouts(roleFilter ?? undefined);
      await reload();
    } catch (e) {
      setErr(`清空失败：${(e as Error).message}`);
    }
  }

  // 从当前列表里聚合出现过的角色，供筛选下拉（按角色卡隔离查看历史）。
  const roles = useMemo(() => {
    const seen = new Map<string, string>();
    for (const r of data?.items ?? []) {
      if (r.role_id) seen.set(r.role_id, r.role_name || r.role_id);
    }
    return [...seen.entries()].map(([id, name]) => ({ id, name }));
  }, [data]);

  const stacks = useMemo(
    () => buildStacks(data?.items ?? [], data?.merge_days ?? 1),
    [data],
  );

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
    // 先进入对话再标已读：跳完这个抽屉就关掉了，回来 setData 没有意义。
    // 标的是**所有**未读（用户 2026-09-23："点进对话界面了就都算看过"），不只是这一个角色。
    // 但**标记失败绝不拦跳转** —— 用户要的是看到那条消息，红点数下一轮轮询自然校正。
    try {
      const p = await api.markAllReachoutsRead();
      setData(p);
      onUnreadChange(p.unread);
    } catch {
      /* 静默：跳转优先 */
    }
    onOpenThread(row.thread_id);
  }

  if (!open) return null;

  const isOpen = (s: Stack) => toggled[s.key] ?? (s.items.length === 1 || s.unread > 0);

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
          <span className="flex items-center gap-2">
            {!!data?.items.length && (
              <button
                onClick={() => void clearAll()}
                title="清空投递记录（对话里她说过的话不动）"
                className="text-xs text-slate-400 hover:text-red-500 dark:text-slate-500 dark:hover:text-red-400"
              >
                清空
              </button>
            )}
            <button
              onClick={onClose}
              className="text-xs text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
            >
              关闭
            </button>
          </span>
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
          {stacks.map((stack) => (
            <div key={stack.key} className="mb-1.5">
              {stack.items.length > 1 && (
                <button
                  onClick={() => setToggled((t) => ({ ...t, [stack.key]: !isOpen(stack) }))}
                  title={isOpen(stack) ? "收起这一摞" : "展开看每一条"}
                  className="flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left text-[11px] text-slate-500 hover:bg-slate-50 dark:text-slate-400 dark:hover:bg-slate-700/40"
                >
                  <span className="w-3 shrink-0 text-slate-400">{isOpen(stack) ? "▾" : "▸"}</span>
                  <span className="font-medium text-slate-700 dark:text-slate-200">
                    {stack.role_name}
                  </span>
                  <span className="text-slate-400 dark:text-slate-500">
                    {stack.label} · {stack.items.length} 条
                  </span>
                  {stack.unread > 0 && (
                    <span className="ml-auto shrink-0 rounded-full bg-blue-600 px-1.5 py-0.5 text-[10px] text-white">
                      {stack.unread} 未读
                    </span>
                  )}
                </button>
              )}
              {isOpen(stack) &&
                stack.items.map((row) => (
                  <MessageRow
                    key={row.id}
                    row={row}
                    showRole={stack.items.length === 1}
                    onOpen={openRow}
                    onDelete={() => void removeRow(row)}
                  />
                ))}
              {!isOpen(stack) && (
                <button
                  onClick={() => void openRow(stack.items[0])}
                  title="打开与该角色的对话（可翻历史、可直接回复）"
                  className="ml-4 flex w-[calc(100%-1rem)] flex-col rounded-lg border border-slate-100 px-3 py-1.5 text-left opacity-70 transition-colors hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-700/40"
                >
                  <span className="truncate text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                    {stack.items[0].text}
                  </span>
                </button>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/** 一条主动消息。组头已经写过角色名与时间标签，所以成组时这里**不重复**（行上自有时间戳）。 */
function MessageRow({
  row,
  showRole,
  onOpen,
  onDelete,
}: {
  row: ReachoutRow;
  showRole: boolean;
  onOpen: (row: ReachoutRow) => void | Promise<void>;
  onDelete: () => void;
}) {
  return (
    // 删除键不能放进那个 `<button>` 里（HTML 不允许按钮套按钮），所以行是 relative 容器、
    // ✕ 绝对定位在右上角，只在悬停这一行时出现 —— 与对话页侧栏那个 ✕ 同一个手势。
    <div className="group relative mb-1">
      <button
        onClick={() => void onOpen(row)}
        title={row.thread_id ? "打开与该角色的对话（可翻历史、可直接回复）" : "标记为已读"}
        className={`flex w-full flex-col rounded-lg border pr-7 py-2 pl-3 text-left transition-colors ${
          row.state === "unread"
            ? "border-blue-200 bg-blue-50/60 dark:border-blue-800 dark:bg-blue-900/20"
            : "border-slate-100 opacity-70 dark:border-slate-700"
        }`}
      >
      {/* 组头已经给了角色与日期区间，行里就不重复；只有"未读"标记值得占一行。 */}
      {(showRole || row.state === "unread") && (
        <span className="flex items-center justify-between text-xs">
          <span className="font-medium text-slate-800 dark:text-slate-100">
            {showRole ? row.role_name || row.role_id || "未知角色" : ""}
          </span>
          {row.state === "unread" && (
            <span className="rounded-full bg-blue-600 px-1.5 py-0.5 text-[10px] text-white">
              未读
            </span>
          )}
        </span>
      )}
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
      <button
        onClick={onDelete}
        title="从抽屉里删掉这条记录（她说过的话仍留在对话里）"
        className="absolute right-1 top-1 hidden h-5 w-5 place-items-center rounded text-xs text-slate-400 hover:bg-red-50 hover:text-red-500 group-hover:grid dark:hover:bg-red-900/30"
      >
        ✕
      </button>
    </div>
  );
}
