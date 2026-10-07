import { useCallback, useEffect, useState } from "react";
import { ExtensionPanel } from "../components/ExtensionPanel";
import { MemoryPanel } from "../components/MemoryPanel";
import { ModelsPanel } from "../components/ModelsPanel";
import { ServicesPanel } from "../components/ServicesPanel";
import { ShellReleaseCard } from "../components/ShellReleaseCard";
import {
  api,
  type AuditRow,
  type ModelSettings,
  type PluginRow,
  type QuietStatus,
  type RoleCard,
  type RuntimeItem,
  type RuntimePayload,
  type ThreadRow,
} from "../api";
import { formatUtcNaive } from "../lib/quiet";
import QuietLine from "../components/QuietLine";
import { Card } from "../components/ui";
import { describeError } from '../lib/errors';

// 设置页子页签：模型（凭据组 + 模型行，见 components/ModelsPanel）/ 服务（运行时状态与降级
// 策略）/ 记忆与任务目录 / 关于 / 扩展 / 运行环境 / 审计。知识库已升为独立顶层页 ——
// RAG 是内核能力，不该埋在设置里。
const SETTINGS_TABS = [
  { key: "models", label: "模型" },
  { key: "services", label: "服务" },
  { key: "memory", label: "记忆与任务目录" },
  { key: "about", label: "关于与系统状态" },
  { key: "extension", label: "扩展" },
  { key: "runtime", label: "运行环境" },
  { key: "audit", label: "审计" },
] as const;

type SettingsTab = (typeof SETTINGS_TABS)[number]["key"];

export default function SettingsPage({
  onOpenChat,
  theme,
  onToggleTheme,
}: {
  onOpenChat?: () => void;
  theme?: string;
  onToggleTheme?: () => void;
}) {
  const [tab, setTab] = useState<SettingsTab>("models");

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-3xl">
        <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">设置</h2>
        <div className="mt-4 flex gap-1 border-b border-slate-200 dark:border-slate-700">
          {SETTINGS_TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`rounded-t-lg px-4 py-2 text-sm ${
                tab === t.key
                  ? "border-b-2 border-blue-600 font-medium text-blue-700 dark:text-blue-300"
                  : "text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        {/* 子页签常驻挂载 + hidden 隐藏（P1-11 同款）：条件渲染会让每次切页签重新挂载
            面板并重发请求 —— 服务页的探活会因此"每次点开都加载中"（用户 2026-09-18）。 */}
        <div className={tab === "memory" ? "" : "hidden"}>
          <MemoryPanel />
        </div>
        <div className={tab === "about" ? "" : "hidden"}>
          <AboutPanel onOpenChat={onOpenChat} theme={theme} onToggleTheme={onToggleTheme} />
        </div>
        <div className={tab === "models" ? "" : "hidden"}>
          <ModelsPanel onOpenServices={() => setTab("services")} />
        </div>
        <div className={tab === "services" ? "" : "hidden"}>
          <ServicesPanel />
        </div>
        <div className={tab === "extension" ? "" : "hidden"}>
          <ExtensionPanel />
        </div>
        <div className={tab === "runtime" ? "" : "hidden"}>
          <RuntimePanel />
        </div>
        <div className={tab === "audit" ? "" : "hidden"}>
          <AuditPanel />
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------- 关于与系统状态

function AboutPanel({
  onOpenChat,
  theme,
  onToggleTheme,
}: {
  onOpenChat?: () => void;
  theme?: string;
  onToggleTheme?: () => void;
}) {
  const [info, setInfo] = useState<{
    defaultBackend: string;
    plugins: string;
    roles: number;
    sessions: number;
  }>({ defaultBackend: "…", plugins: "…", roles: 0, sessions: 0 });
  const [refreshed, setRefreshed] = useState(false);
  const [loadError, setLoadError] = useState("");

  const loadInfo = useCallback(async () => {
    const [ms, plugins, roles, sessions] = await Promise.all([
      api.get<ModelSettings>("/api/settings/models"),
      api.get<PluginRow[]>("/api/plugins"),
      api.get<RoleCard[]>("/api/roles"),
      api.get<ThreadRow[]>("/api/sessions"),
    ]);
    const enabled = plugins.filter((p) => p.enabled).length;
    setInfo({
      defaultBackend: ms.default || "env 默认（local）",
      plugins: `${enabled} / ${plugins.length} 启用`,
      roles: roles.length,
      sessions: sessions.length,
    });
  }, []);

  useEffect(() => {
    loadInfo().catch((e) => setLoadError(`加载失败：${describeError(e)}`));
  }, [loadInfo]);

  return (
    <div className="mt-6 space-y-4">
      {onToggleTheme && (
        <Card className="p-5">
          <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">外观</h3>
          <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
            界面配色跟随本机偏好时可手动覆盖；切换即时生效、随浏览器记住。
          </p>
          <div className="mt-2.5 flex items-center gap-2">
            <span className="text-xs text-slate-500 dark:text-slate-400">
              当前：{theme === "dark" ? "深色" : "浅色"}
            </span>
            <button
              onClick={onToggleTheme}
              className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:border-blue-300 dark:border-slate-600 dark:text-slate-300 dark:hover:border-blue-700"
            >
              切换为{theme === "dark" ? "浅色" : "深色"}
            </button>
          </div>
        </Card>
      )}

      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">关于</h3>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          rolecard-agent 控制台。多角色对话 Agent：角色卡控制人设与工具权限，
          插件以数据驱动启停。
        </p>
        <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">语言：简体中文（内置）</p>
      </Card>

      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">系统状态</h3>
        {loadError && <p className="mt-1 text-xs text-red-600 dark:text-red-400 dark:text-red-500">{loadError}</p>}
        <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">对话默认（服务页第 1 位）</dt>
            <dd className="font-mono">{info.defaultBackend}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">领域插件（启用/注册）</dt>
            <dd className="font-mono">{info.plugins}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">角色卡数量</dt>
            <dd className="font-mono">{info.roles}</dd>
          </div>
          <div className="flex justify-between border-b border-slate-50 py-1">
            <dt className="text-slate-400 dark:text-slate-500">对话数量</dt>
            <dd className="font-mono">{info.sessions}</dd>
          </div>
        </dl>
        <p className="mt-2 text-[11px] text-slate-400 dark:text-slate-500">
          对话默认谁算、失败时依次回退给谁 → 在「服务」里改；模型的增删与它的密钥 → 在「模型」里改。
          插件的启停在左侧「插件」页（停用立即生效）。
        </p>
      </Card>

      <Card className="p-5">
        <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">演示数据</h3>
        <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">
          评测 / 演示用的虚构档案可由脚本重建；对话与数据的管理操作在对话页与插件页。
        </p>
        <div className="mt-2 flex gap-2">
          <button
            onClick={() => onOpenChat?.()}
            className="rounded-lg border border-slate-200 dark:border-slate-700 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700"
          >
            前往对话
          </button>
          <button
            onClick={async () => {
              await loadInfo();
              setRefreshed(true);
              setTimeout(() => setRefreshed(false), 2000);
            }}
            className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 dark:hover:border-blue-700"
          >
            {refreshed ? "已刷新 ✓" : "刷新状态"}
          </button>
        </div>
      </Card>

      {/* 桌面壳安装包（D②-4）：没有产物时整卡不渲染，所以它放在最后也不会在页面上留空位。 */}
      <ShellReleaseCard />
    </div>
  );
}

// ---------------------------------------------------------------- 运行环境（可改 + 只读混合）

function RuntimePanel() {
  const [data, setData] = useState<RuntimePayload | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [baseline, setBaseline] = useState<Record<string, string>>({});
  const [err, setErr] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);
  // 「她此刻为什么静默」（`S-8`）：读收件箱那份负载里后端算好的 `quiet`，不自己推时间。
  const [quiet, setQuiet] = useState<QuietStatus[]>([]);

  const loadQuiet = useCallback(
    () =>
      api
        .get<{ quiet?: QuietStatus[] }>("/api/reachouts")
        .then((p) => setQuiet(p.quiet ?? []))
        .catch(() => setQuiet([])), // 这一格读不到就不显示，不该把整页报成"加载失败"
    [],
  );

  const absorb = useCallback((p: RuntimePayload) => {
    setData(p);
    const d: Record<string, string> = {};
    const b: Record<string, string> = {};
    for (const g of p.groups) {
      for (const it of g.items) {
        if (it.kind !== "ro") {
          d[it.key] = it.override_value ?? "";
          b[it.key] = it.override_value ?? "";
        }
      }
    }
    setDraft(d);
    setBaseline(b);
  }, []);

  useEffect(() => {
    api
      .get<RuntimePayload>("/api/settings/runtime")
      .then(absorb)
      .catch((e) => setErr(`加载失败：${describeError(e)}`));
    void loadQuiet();
  }, [absorb, loadQuiet]);

  const changedCount = Object.keys(draft).filter((k) => draft[k] !== baseline[k]).length;

  async function save() {
    const values: Record<string, string | null> = {};
    for (const k of Object.keys(draft)) {
      if (draft[k] !== baseline[k]) values[k] = draft[k] === "" ? null : draft[k];
    }
    if (Object.keys(values).length === 0) return;
    setBusy(true);
    setErr("");
    try {
      absorb(await api.put<RuntimePayload>("/api/settings/runtime", { values }));
      // 静默状态要跟着刷一次：改了「开口间隔」，那句"不足 N 分钟"与下一次的时刻都是它算的。
      void loadQuiet();
      setSaved("已保存并生效");
      setTimeout(() => setSaved(""), 3000);
    } catch (e) {
      setErr(`保存失败：${describeError(e)}`);
    } finally {
      setBusy(false);
    }
  }

  if (err && !data) {
    return <p className="mt-6 text-xs text-red-600 dark:text-red-400 dark:text-red-500">{err}</p>;
  }
  if (!data) {
    return <p className="mt-6 text-xs text-slate-400 dark:text-slate-500">加载中…</p>;
  }

  function inputFor(it: RuntimeItem) {
    const set = (v: string) => setDraft((d) => ({ ...d, [it.key]: v }));
    if (it.kind === "bool") {
      return (
        <select
          value={draft[it.key] ?? ""}
          onChange={(e) => set(e.target.value)}
          className="w-28 rounded border border-slate-200 px-1.5 py-1 text-xs outline-none focus:border-blue-400 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200"
        >
          <option value="">跟随默认（{it.default}）</option>
          <option value="1">开</option>
          <option value="0">关</option>
        </select>
      );
    }
    // 模型名单（动态选项来自模型页后端）→ 勾选组：勾一个算一个，存成逗号串。
    if (it.key === "MODEL_THINKING_MODELS" && it.choices && it.choices.length > 0) {
      const selected = (draft[it.key] ?? "").split(",").map((s) => s.trim()).filter(Boolean);
      const toggle = (name: string) => {
        const next = selected.includes(name)
          ? selected.filter((n) => n !== name)
          : [...selected, name];
        set(next.join(","));
      };
      return (
        <div className="flex flex-wrap gap-2">
          {it.choices.map((name) => (
            <label key={name} className="flex cursor-pointer items-center gap-1 font-mono text-xs text-slate-600 dark:text-slate-300">
              <input
                type="checkbox"
                checked={selected.includes(name)}
                onChange={() => toggle(name)}
                className="accent-blue-600"
              />
              {name}
            </label>
          ))}
        </div>
      );
    }
    // 枚举 → 下拉单选（含"跟随 .env"兜底项）
    if (it.choices && it.choices.length > 0) {
      const current = draft[it.key] ?? "";
      const extra = current && !it.choices.includes(current) ? [current] : [];
      return (
        <select
          value={current}
          onChange={(e) => set(e.target.value)}
          className="w-40 rounded border border-slate-200 px-1.5 py-1 text-xs outline-none focus:border-blue-400 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200"
        >
          <option value="">跟随默认（{it.value}）</option>
          {[...it.choices, ...extra].map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      );
    }
    const type = it.kind === "secret" ? "password" : it.kind === "float" || it.kind === "int" ? "number" : "text";
    return (
      <input
        type={type}
        value={draft[it.key] ?? ""}
        onChange={(e) => set(e.target.value)}
        placeholder={it.overridden ? `覆盖中：${it.value}` : `未覆盖（当前 ${it.value}）`}
        className="w-56 rounded border border-slate-200 px-1.5 py-1 font-mono text-xs outline-none focus:border-blue-400 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200"
      />
    );
  }

  return (
    <div className="mt-6 space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-[11px] leading-relaxed text-slate-400 dark:text-slate-500">{data.note}</p>
        <div className="flex shrink-0 items-center gap-2">
          {saved && <span className="text-xs text-green-600 dark:text-green-400">{saved}</span>}
          {err && <span className="text-xs text-red-600 dark:text-red-400">{err}</span>}
          <button
            onClick={save}
            disabled={busy || changedCount === 0}
            className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:bg-slate-300 dark:bg-slate-600 dark:disabled:bg-slate-700"
          >
            {busy ? "保存中…" : `保存${changedCount ? `（${changedCount} 项修改）` : ""}`}
          </button>
        </div>
      </div>
      {data.groups.map((g) => (
        <Card
          key={g.key}
          className="p-5"
        >
          <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">{g.label}</h3>
          <table className="mt-2 w-full text-xs">
            <tbody>
              {g.items.map((it) => (
                <tr key={it.key} className="border-b border-slate-50 last:border-0 dark:border-slate-700/50">
                  <td className="w-40 py-1.5 align-top">
                    <span className="font-medium text-slate-700 dark:text-slate-200">{it.label}</span>
                    <span className="ml-1.5 font-mono text-[10px] text-slate-300 dark:text-slate-600">{it.key}</span>
                  </td>
                  <td className="py-1.5 align-top">
                    {/* 主动开口总闸（reachout_enabled）是「记忆与任务目录」页签的单写点：
                        这里只读展示当前生效值，避免同一开关两处可写（架构审计 §3 的控制开关体系）。 */}
                    {/* 主动开口总闸与收件箱折叠窗口是「记忆与任务目录」页签的单写点：这里
                        只读展示当前生效值，避免同一开关两处可写（架构审计 §3 的控制开关体系）。
                        比对必须用 `field`（保存路径用的稳定身份），**不能用 `key`** —— `key`
                        是 env 名（REACHOUT_ENABLED），以前写成比 env 名的小串，运行环境里
                        其实一直是可编辑的，用例因为 stub 把 key 写成了 field 名而没发现。 */}
                    {it.kind === "ro"
                    || it.field === "reachout_enabled"
                    || it.field === "reachout_merge_days" ? (
                      <span className={`font-mono ${it.changed ? "text-amber-600 dark:text-amber-400" : "text-slate-600 dark:text-slate-300"}`}>
                        {it.value}
                      </span>
                    ) : (
                      inputFor(it)
                    )}
                  </td>
                  <td className="py-1.5 align-top text-slate-400 dark:text-slate-500">
                    {it.field === "reachout_enabled" || it.field === "reachout_merge_days"
                      ? "在「记忆与任务目录」页签修改"
                      : it.note || (it.changed && it.kind === "ro" ? `默认 ${it.default}` : "")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* 「她此刻为什么静默」只挂在主动开口那一组下面 —— 这一格说的就是上面那三个开关
              此刻的效果。文案与时刻都来自后端（`core/reachout.quiet_status`），前端不自己推。 */}
          {g.key === "reachout" && quiet.length > 0 && (
            <ul
              data-testid="quiet-status"
              className="mt-3 space-y-1 border-t border-slate-100 pt-2 dark:border-slate-700"
            >
              {quiet.map((q) => (
                <li key={q.role_id} className="text-slate-500 dark:text-slate-400">
                  <QuietLine q={q} />
                </li>
              ))}
            </ul>
          )}
        </Card>
      ))}
    </div>
  );
}
// ---------------------------------------------------------------- 审计（F3）

function AuditPanel() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [status, setStatus] = useState("");
  const [filter, setFilter] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    api.get<AuditRow[]>("/api/audit?limit=200").then(setRows).catch((e) => setStatus(`加载失败：${e.message}`));
  }, []);

  // 按动作名 / 对象 / 详情模糊过滤
  const filtered = filter.trim()
    ? rows.filter((r) =>
        [r.action, r.target, r.detail_json].some((v) =>
          (v || "").toLowerCase().includes(filter.trim().toLowerCase()),
        ),
      )
    : rows;

  return (
    <div className="mt-6">
      <div className="mb-3 flex items-center gap-2">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="筛选：动作名 / 对象 / 详情…"
          className="w-64 rounded-lg border border-slate-200 px-3 py-1.5 text-xs outline-none focus:border-blue-400 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
        />
        <span className="text-[11px] text-slate-400">{filtered.length} / {rows.length} 条</span>
      </div>
      {status && <p className="rounded-lg bg-red-50 dark:bg-red-900/30 px-3 py-2 text-xs text-red-600 dark:text-red-400 dark:text-red-500">{status}</p>}
      {/* 横向可滚动：审计列（详情 JSON）天然宽，容器必须给滚动条而不是裁掉。 */}
      <Card className="overflow-x-auto">
        <table className="w-full min-w-[640px] text-left text-xs">
          <thead>
            <tr className="border-b border-slate-100 dark:border-slate-800 text-slate-400 dark:text-slate-500">
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
                  暂无审计记录
                </td>
              </tr>
            )}
            {/* 展开键 = 后端下发的唯一 id（`R102-17`）；行 key 仍带序号兜底渲染去歧。 */}
            {filtered.map((a, i) => (
              <tr
                key={`${a.id}|${a.ts}|${a.actor}|${a.action}|${a.target ?? ""}|${i}`}
                className="border-b border-slate-50 last:border-0"
              >
                <td className="whitespace-nowrap px-4 py-2 font-mono text-slate-500 dark:text-slate-400">
                  {formatUtcNaive(String(a.ts))}
                </td>
                <td className="px-4 py-2">{a.actor}</td>
                <td className="px-4 py-2">
                  <code className="rounded bg-slate-100 dark:bg-slate-700/50 px-1.5 py-0.5">{a.action}</code>
                </td>
                <td className="max-w-40 truncate px-4 py-2 font-mono text-slate-500 dark:text-slate-400">{a.target}</td>
                <td className="max-w-56 px-4 py-2 align-top text-slate-400 dark:text-slate-500">
                  {/* 详情可点开：截断摘要 + title 悬浮不够看全 JSON（审计走查反馈） */}
                  {a.detail_json && expanded === String(a.id) ? (
                    <pre className="max-w-72 whitespace-pre-wrap break-all rounded bg-slate-50 p-1.5 font-mono text-[11px] text-slate-600 dark:bg-slate-700/40 dark:text-slate-300">
                      {a.detail_json}
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
      <p className="mt-2 text-[11px] text-slate-400 dark:text-slate-500">
        审计由程序在角色切换、插件启停、对话创建、数据修正/删除时写入（US-3）；本页只读。
      </p>
    </div>
  );
}
