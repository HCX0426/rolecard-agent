// 「运行环境」子页签（P3-1 第二刀：从 pages/SettingsPage.tsx **逐字搬来**，正文一字未动；
// 等价证明同 MemoryPanel：整页 DOM 驱动，382 支用例数一字不变；tsc 守 import 面）。
import { useCallback, useEffect, useState } from "react";
import {
  api,
  type QuietStatus,
  type RuntimeItem,
  type RuntimePayload,
} from "../api";
import QuietLine from "./QuietLine";
import { Card } from "./ui";
import { describeError } from "../lib/errors";

// ---------------------------------------------------------------- 运行环境（可改 + 只读混合）

export function RuntimePanel() {
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
