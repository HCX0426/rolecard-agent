// 「审计」子页签（P3-1 第二刀：从 pages/SettingsPage.tsx 拆出）。
//
// 2026-10-30 重设计（用户："审计界面也要重新设计下，目前展示得不太好"）。从前的形状：
// 一张 200 行的平铺表 + 一个裸文本筛选 —— 动作多了之后等于让用户在流水里大海捞针，
// "我想看看最近谁动过记忆"这种最自然的问法没有入口。现在：
//
//   1. **动作分组筛选**（按家族分 pill，带条数）：审计动作名是程序写入的受控词表
//      （`base/audit.AUDIT_ACTIONS`），按"这行在说哪类资源"分成七族 —— 记忆 / 角色 /
//      工作区 / 模型与服务 / 会话 / 数据 / 审批，剩下进"其他"。分族是**前端展示层**的
//      事：后端的动作词表自己不分组（它管的是"这些词合法"，不是"这些词怎么给人看"），
//      两边各管各的语义，匹配规则改动不用惊动后端。
//   2. 文本筛选保留（分组盖不住的"我要找 tid=xxx"这一类）。
//   3. 详情展开时**格式化 JSON**：审计的 detail_json 是压缩存储的，直接展示是一坨；
//      展开即 pretty-print（解析失败原样给出 —— 它可能是非 JSON 的旧格式）。
//   4. 刷新按钮：本页只读但数据会增长，挂着一次加载等于让人以为"没有新记录"。
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type AuditRow } from "../api";
import { Button, Card } from "./ui";
import { formatUtcNaive } from "../lib/quiet";

/** 审计动作的展示分组。匹配规则 = 动作名含关键词（受控词表见 `base/audit.py`）。
 *
 *  **顺序有语义**：`set_role_task_dir` 同时含 "role" 与 "task_dir"，它是"给某个角色
 *  配工作区"——按工作区说比按角色说准（角色那族要的是"改了卡的什么"），所以
 *  workspace 排在 roles 前面。改这份清单时从上往下想"这行最像在说哪类资源"。 */
const ACTION_GROUPS: { key: string; label: string; match: (action: string) => boolean }[] = [
  { key: "memory", label: "记忆", match: (a) => a.includes("memory") },
  { key: "workspace", label: "工作区", match: (a) => a.includes("task_dir") },
  { key: "roles", label: "角色", match: (a) => a.includes("role") },
  {
    key: "services",
    label: "模型与服务",
    match: (a) => a.includes("model") || a.includes("service") || a.includes("probe"),
  },
  { key: "sessions", label: "会话", match: (a) => a.includes("session") || a.includes("thread") },
  {
    key: "data",
    label: "数据",
    match: (a) =>
      a.includes("report") || a.includes("domain") || a.includes("index") || a.includes("upload"),
  },
  {
    key: "approvals",
    label: "审批",
    match: (a) => a.includes("command") || a.includes("approval"),
  },
];

/** 落不进任何一组的动作归"其他"——兜底必须是显式的一族，不能让行凭空消失。 */
const OTHER_GROUP = { key: "other", label: "其他" };

function groupOf(action: string): string {
  return ACTION_GROUPS.find((g) => g.match(action))?.key ?? OTHER_GROUP.key;
}

/** 详情 JSON 的展示形态：能解析就 pretty-print，不能（旧格式/非 JSON）原样给出。 */
function prettyDetail(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

export function AuditPanel() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [status, setStatus] = useState("");
  const [filter, setFilter] = useState("");
  const [group, setGroup] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setStatus("");
    setLoading(true);
    try {
      setRows(await api.get<AuditRow[]>("/api/audit?limit=200"));
    } catch (e) {
      setStatus(`加载失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 每族条数一次算齐 —— pill 上的数字与筛选结果必须同源，两处各算是第二个事实面。
  const counts = useMemo(() => {
    const map = new Map<string, number>();
    for (const r of rows) {
      const k = groupOf(String(r.action));
      map.set(k, (map.get(k) ?? 0) + 1);
    }
    return map;
  }, [rows]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return rows.filter((r) => {
      if (group && groupOf(String(r.action)) !== group) return false;
      if (!q) return true;
      return [r.action, r.target, r.detail_json].some((v) =>
        (v || "").toLowerCase().includes(q),
      );
    });
  }, [rows, group, filter]);

  return (
    <div className="mt-6">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="筛选：动作名 / 对象 / 详情…"
          className="w-64 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs outline-none focus:border-blue-400 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
        />
        <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
          {loading ? "加载中…" : "刷新"}
        </Button>
        <span className="text-11 text-slate-400">
          {filtered.length} / {rows.length} 条
        </span>
      </div>

      {/* 分组 pill（带条数）：审计的第一问是"最近谁动过 X"，按族点一下就是答案。 */}
      <div className="mb-3 flex flex-wrap items-center gap-1.5">
        <button
          onClick={() => setGroup("")}
          className={`rounded-full px-2.5 py-1 text-xs transition-colors ${
            group === ""
              ? "bg-slate-700 text-white dark:bg-slate-200 dark:text-slate-900"
              : "bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
          }`}
        >
          全部 {rows.length}
        </button>
        {[...ACTION_GROUPS, OTHER_GROUP].map((g) => {
          const n = counts.get(g.key) ?? 0;
          if (n === 0) return null; // 没有记录的族不摆出来 —— 空 pill 是噪音
          const on = group === g.key;
          return (
            <button
              key={g.key}
              onClick={() => setGroup(on ? "" : g.key)}
              className={`rounded-full px-2.5 py-1 text-xs transition-colors ${
                on
                  ? "bg-slate-700 text-white dark:bg-slate-200 dark:text-slate-900"
                  : "bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
              }`}
            >
              {g.label} {n}
            </button>
          );
        })}
      </div>

      {status && (
        <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600 dark:bg-red-900/30 dark:text-red-400">
          {status}
        </p>
      )}
      {/* 横向可滚动：审计列（详情 JSON）天然宽，容器必须给滚动条而不是裁掉。 */}
      <Card className="overflow-x-auto">
        <table className="w-full min-w-[640px] text-left text-xs">
          <thead>
            <tr className="border-b border-slate-100 text-slate-400 dark:border-slate-800 dark:text-slate-500">
              <th className="px-4 py-2 font-medium">时间</th>
              <th className="px-4 py-2 font-medium">操作者</th>
              <th className="px-4 py-2 font-medium">动作</th>
              <th className="px-4 py-2 font-medium">对象</th>
              <th className="px-4 py-2 font-medium">详情</th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-slate-400 dark:text-slate-500">
                  {loading ? "加载中…" : "暂无符合条件的审计记录"}
                </td>
              </tr>
            )}
            {/* 展开键 = 后端下发的唯一 id（`R102-17`）；行 key 仍带序号兜底渲染去歧。 */}
            {filtered.map((a, i) => (
              <tr
                key={`${a.id}|${a.ts}|${a.actor}|${a.action}|${a.target ?? ""}|${i}`}
                className="border-b border-slate-50 transition-colors last:border-0 hover:bg-slate-50/60 dark:border-slate-800/60 dark:hover:bg-slate-800/30"
              >
                <td
                  className="whitespace-nowrap px-4 py-2 font-mono text-slate-500 dark:text-slate-400"
                  title={String(a.ts)}
                >
                  {formatUtcNaive(String(a.ts), true)}
                </td>
                <td className="px-4 py-2">{a.actor}</td>
                <td className="whitespace-nowrap px-4 py-2">
                  <code className="rounded bg-slate-100 px-1.5 py-0.5 dark:bg-slate-700/50">
                    {a.action}
                  </code>
                </td>
                <td className="max-w-40 truncate px-4 py-2 font-mono text-slate-500 dark:text-slate-400" title={a.target ?? ""}>
                  {a.target}
                </td>
                <td className="max-w-56 px-4 py-2 align-top text-slate-400 dark:text-slate-500">
                  {/* 详情可点开：截断摘要 + title 悬浮不够看全 JSON（审计走查反馈）；
                      展开态给 pretty-print（压缩存储的一坨没人读得懂）。 */}
                  {a.detail_json && expanded === String(a.id) ? (
                    <pre className="max-w-96 whitespace-pre-wrap break-all rounded bg-slate-50 p-1.5 font-mono text-11 text-slate-600 dark:bg-slate-700/40 dark:text-slate-300">
                      {prettyDetail(a.detail_json)}
                    </pre>
                  ) : (
                    <button
                      onClick={() => setExpanded(a.detail_json ? String(a.id) : null)}
                      className="max-w-56 cursor-pointer truncate text-left hover:text-slate-600 dark:hover:text-slate-300"
                      title="点击展开完整详情"
                    >
                      {a.detail_json || "—"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <p className="mt-2 text-11 text-slate-400 dark:text-slate-500">
        审计由程序在角色切换、插件启停、对话创建、数据修正/删除时写入；本页只读，最多显示最近 200 条。
      </p>
    </div>
  );
}
