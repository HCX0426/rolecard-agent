// 「服务」子页签：运行时状态视图 + OCR/嵌入/重排的优先级与启停。
// 模型推理类只读展示（编辑与增删在「模型」页签 —— 同一份数据不设两个编辑入口）。
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

interface ServiceCandidateView {
  id: string;
  label: string;
  kind: "local" | "cloud";
  needs: string;
  available: boolean;
  reason: string;
  disabled: boolean;
  preferred: boolean;
  order: number | null;
}

interface ServiceCategoryView {
  key: string;
  title: string;
  hint: string;
  preferred: string;
  disabled: string[];
  degraded_from: string | null;
  effective: string | null;
  effective_kind: string | null;
  candidates: ServiceCandidateView[];
}

interface ServicesView {
  services: ServiceCategoryView[];
}

function KindBadge({ kind }: { kind: "local" | "cloud" }) {
  const style =
    kind === "local"
      ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
      : "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300";
  return (
    <span className={`rounded-full px-2 py-0.5 text-[11px] ${style}`}>
      {kind === "local" ? "本地" : "云端"}
    </span>
  );
}

function AvailBadge({ available, reason }: { available: boolean; reason: string }) {
  return (
    <span
      title={reason}
      className={`rounded-full px-2 py-0.5 text-[11px] ${
        available
          ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
          : "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
      }`}
    >
      {available ? "可用" : "不可用"}
    </span>
  );
}

export function ServicesPanel() {
  const [view, setView] = useState<ServicesView | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState("");

  const load = useCallback(async () => {
    setView(await api.get<ServicesView>("/api/services"));
  }, []);

  useEffect(() => {
    load().catch((e) => setError(`加载失败：${(e as Error).message}`));
  }, [load]);

  /** 保存一类服务的策略：本地计算目标顺序（移动 delta 位）后整体提交。 */
  const reorder = async (cat: ServiceCategoryView, cand: ServiceCandidateView, delta: -1 | 1) => {
    const pool = cat.candidates.filter((c) => !c.disabled);
    const idx = pool.findIndex((c) => c.id === cand.id);
    const target = idx + delta;
    if (target < 0 || target >= pool.length) return;
    const next = [...pool];
    [next[idx], next[target]] = [next[target], next[idx]];
    await apply(cat.key, next[0].id, []);
  };

  const toggleDisabled = async (cat: ServiceCategoryView, cand: ServiceCandidateView) => {
    const disabled = cand.disabled
      ? cat.disabled.filter((d) => d !== cand.id)
      : [...cat.disabled, cand.id];
    const remaining = cat.candidates.filter((c) => !disabled.includes(c.id));
    if (remaining.length === 0) return; // 后端也会拒绝；这里提前拦住
    const preferred =
      disabled.includes(cat.preferred) ? remaining[0].id : cat.preferred;
    await apply(cat.key, preferred, disabled);
  };

  const apply = async (key: string, preferred: string, disabled: string[]) => {
    setBusy(key);
    setError("");
    try {
      await api.put(`/api/services/${key}`, { preferred, disabled });
      await load();
      setFlash(`已保存并热生效（无需重启）· ${new Date().toLocaleTimeString()}`);
    } catch (e) {
      setError(`保存失败：${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  if (error && !view) {
    return <p className="text-sm text-red-600 dark:text-red-400">{error}</p>;
  }
  if (!view) {
    return <p className="text-sm text-slate-500 dark:text-slate-400">加载中…</p>;
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-500 dark:text-slate-400">
          本地服务优先使用（数据不出机）；语义嵌入例外 —— 云端质量显著更好，本地仅作兜底。
          优先级与启停立即保存并热生效。
        </p>
        <button
          onClick={() =>
            load().then(() => {
              setFlash("已重新检测");
            })
          }
          className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          重新检测
        </button>
      </div>

      {error && (
        <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-900/30 dark:text-red-300">
          {error}
        </p>
      )}
      {flash && !error && (
        <p className="rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300">
          ✓ {flash}
        </p>
      )}

      {view.services.map((cat) => {
        const effectiveCand = cat.candidates.find((c) => c.id === cat.effective);
        const degraded = cat.degraded_from !== null;
        return (
          <section key={cat.key} className="flex flex-col gap-2">
            <div className="flex items-baseline gap-2">
              <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">{cat.title}</h3>
              <span className="text-[11px] text-slate-400 dark:text-slate-500">{cat.hint}</span>
            </div>

            {degraded && (
              <p className="rounded-lg border-l-4 border-amber-400 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-900/20 dark:text-amber-200">
                已降级：首选「{cat.degraded_from}」当前不可用，正在使用「{cat.effective ?? "无"}」。
                {cat.effective_kind === "cloud" && cat.key === "ocr" && " 注意：图片将发往云端。"}
              </p>
            )}

            <div className="flex flex-col gap-1.5">
              {cat.candidates
                .slice()
                .sort((a, b) => (a.order ?? 99) - (b.order ?? 99))
                .map((cand) => (
                  <div
                    key={cand.id}
                    className={`flex items-center gap-2.5 rounded-lg border px-3 py-2 ${
                      cand.disabled
                        ? "border-dashed border-slate-200 opacity-60 dark:border-slate-700"
                        : "border-slate-200 dark:border-slate-700"
                    }`}
                  >
                    <span className="text-xs text-slate-400">{cand.disabled ? "—" : (cand.order ?? 0) + 1}</span>
                    <KindBadge kind={cand.kind} />
                    <span className="text-sm text-slate-800 dark:text-slate-200">{cand.label}</span>
                    <AvailBadge available={cand.available} reason={cand.reason} />
                    {cand.preferred && (
                      <span className="text-[11px] text-slate-400">首选</span>
                    )}
                    <span className="ml-auto flex items-center gap-1.5">
                      {cat.key !== "models" && (
                        <>
                          <button
                            disabled={busy === cat.key || cand.disabled}
                            onClick={() => reorder(cat, cand, -1)}
                            title="上调优先级"
                            className="rounded border border-slate-200 px-2 py-0.5 text-xs text-slate-500 hover:bg-slate-50 disabled:opacity-40 dark:border-slate-600 dark:hover:bg-slate-800"
                          >
                            ↑
                          </button>
                          <button
                            disabled={busy === cat.key || cand.disabled}
                            onClick={() => reorder(cat, cand, 1)}
                            title="下调优先级"
                            className="rounded border border-slate-200 px-2 py-0.5 text-xs text-slate-500 hover:bg-slate-50 disabled:opacity-40 dark:border-slate-600 dark:hover:bg-slate-800"
                          >
                            ↓
                          </button>
                          <button
                            disabled={busy === cat.key}
                            onClick={() => toggleDisabled(cat, cand)}
                            className={`rounded-full px-2.5 py-0.5 text-[11px] ${
                              cand.disabled
                                ? "bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-300"
                                : "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
                            }`}
                          >
                            {cand.disabled ? "已停用" : "启用中"}
                          </button>
                        </>
                      )}
                      {cat.key === "models" && (
                        <span className="text-[11px] text-slate-400">
                          {cand.id === "local" ? "默认" : ""}· 增删与 key 在「模型」页签
                        </span>
                      )}
                    </span>
                  </div>
                ))}
            </div>

            {cat.key === "models" && effectiveCand && (
              <p className="text-[11px] text-slate-400 dark:text-slate-500">
                生效：{effectiveCand.label}
                {degraded ? "（首选不可用，已自动降级）" : ""}
              </p>
            )}
          </section>
        );
      })}
    </div>
  );
}
