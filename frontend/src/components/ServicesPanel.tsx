// 「服务」子页签：运行时状态视图 + 各服务引用哪些后端、以什么优先级。
// 架构归一化（引用模型）：模型页是云端配置的唯一事实面 —— 新增 = 从已配置后端中**选择**；
// 移除 = 仅从本服务优先级中摘除引用（不影响模型页配置）；配置的编辑只在模型页。
// 优先级第 1 位即生效；修改立即保存并热生效（嵌入/重排后端热重建）。
import { useCallback, useEffect, useState } from "react";
import { api, type ModelSettings } from "../api";

interface ServiceEndpoint {
  id: string;
  label: string;
  kind: "local" | "cloud";
  available: boolean;
  reason: string;
  enabled: boolean;
  builtin: boolean;
  key_masked: string | null;
  base_url: string | null;
  model: string | null;
  ref_backend: string | null;
  stale: boolean;
  order: number | null;
}

interface ServiceCategoryView {
  key: string;
  title: string;
  hint: string;
  effective: string | null;
  effective_kind: string | null;
  degraded_from: string | null;
  readonly: boolean;
  /** order_only = 只能调顺序（第 1 位即默认后端，其后依次回退）；增删与 key 在「模型」页签。 */
  order_only?: boolean;
  candidates: ServiceEndpoint[];
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
  const [backends, setBackends] = useState<ModelSettings["backends"]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState("");
  const [adding, setAdding] = useState<string | null>(null); // 类别 key
  const [picked, setPicked] = useState<string>(""); // 选中的后端名
  const [confirmDel, setConfirmDel] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [v, s] = await Promise.all([
      api.get<ServicesView>("/api/services"),
      api.get<ModelSettings>("/api/settings/models"),
    ]);
    setView(v);
    setBackends(s.backends);
  }, []);

  useEffect(() => {
    load().catch((e) => setError(`加载失败：${(e as Error).message}`));
  }, [load]);

  const run = async (id: string, fn: () => Promise<void>, okMsg: string) => {
    setBusy(id);
    setError("");
    try {
      await fn();
      await load();
      setFlash(`${okMsg}· ${new Date().toLocaleTimeString()}`);
    } catch (e) {
      setError(`保存失败：${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  /** 调优先级：启用行数组内相邻互换，全量提交（第 1 位即生效）。 */
  const reorder = (cat: ServiceCategoryView, cand: ServiceEndpoint, delta: -1 | 1) => {
    const enabled = cat.candidates.filter((c) => c.enabled).map((c) => c.id);
    const idx = enabled.indexOf(cand.id);
    const target = idx + delta;
    if (target < 0 || target >= enabled.length) return;
    [enabled[idx], enabled[target]] = [enabled[target], enabled[idx]];
    run(cat.key, () => api.put(`/api/services/${cat.key}`, { order: enabled }), "优先级已更新并热生效");
  };

  const toggleEnabled = (cat: ServiceCategoryView, cand: ServiceEndpoint) => {
    run(
      cat.key,
      () => api.patch(`/api/services/${cat.key}/endpoints/${cand.id}`, { enabled: !cand.enabled }),
      cand.enabled ? "已停用" : "已启用",
    );
  };

  /** 新增引用：从模型页已配置的后端中选择（不做任何配置复制）。 */
  const addRef = (cat: ServiceCategoryView) => {
    if (!picked) {
      setError("请先选择要引用的后端");
      return;
    }
    run(cat.key, () => api.post(`/api/services/${cat.key}/endpoints`, { ref_backend: picked }), "已引用并热生效");
    setAdding(null);
    setPicked("");
  };

  /** 移除引用：只从本服务优先级中摘除，模型页配置不受影响。 */
  const remove = (cat: ServiceCategoryView, cand: ServiceEndpoint) => {
    run(cat.key, () => api.del(`/api/services/${cat.key}/endpoints/${cand.id}`), "已从本服务移除（模型页配置保留）");
    setConfirmDel(null);
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
          云端条目 = 对「模型」页签已配置后端的**引用**：新增在这里选，改配置去模型页，移除只影响本服务。
          按优先级依次兜底 —— <b>第 1 位即生效</b>，调前几位的顺序即可做多模型比对。
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
        // 可引用的后端 = 模型页全部配置 − 本服务已引用的（含失效引用占位）
        const referenced = new Set(cat.candidates.filter((c) => !c.builtin).map((c) => c.id));
        const selectable = backends.filter((b) => !referenced.has(b.name));
        return (
          <section key={cat.key} className="flex flex-col gap-2">
            <div className="flex items-baseline gap-2">
              <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">{cat.title}</h3>
              <span className="text-[11px] text-slate-400 dark:text-slate-500">{cat.hint}</span>
              {!cat.readonly && !cat.order_only && (
                <button
                  onClick={() => {
                    setAdding(adding === cat.key ? null : cat.key);
                    setPicked("");
                  }}
                  className="ml-auto shrink-0 rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 hover:text-blue-600 dark:hover:text-blue-400"
                >
                  {adding === cat.key ? "取消" : "＋ 引用后端"}
                </button>
              )}
            </div>

            {cat.degraded_from && (
              <p className="rounded-lg border-l-4 border-amber-400 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-900/20 dark:text-amber-200">
                已降级：优先级第 1 位「{cat.degraded_from}」当前不可用，正在使用「
                {cat.effective ?? "无"}」。
                {cat.effective_kind === "cloud" && cat.key === "ocr" && " 注意：图片将发往云端。"}
              </p>
            )}

            {adding === cat.key && (
              <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/20 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    value={picked}
                    onChange={(e) => setPicked(e.target.value)}
                    className="rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 py-1 text-xs"
                  >
                    <option value="">选择模型页已配置的后端…</option>
                    {selectable.map((b) => (
                      <option key={b.name} value={b.name}>
                        {b.name} · {b.model}（{b.usage === "chat" ? "对话" : b.usage === "embedding" ? "嵌入" : b.usage === "rerank" ? "重排" : "OCR"}）
                      </option>
                    ))}
                  </select>
                  {selectable.length === 0 && (
                    <span className="text-[11px] text-slate-400 dark:text-slate-500">
                      模型页还没有可引用的后端 —— 请先去「模型」页签新增
                    </span>
                  )}
                  <button
                    disabled={busy === cat.key || !picked}
                    onClick={() => addRef(cat)}
                    className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
                  >
                    添加
                  </button>
                </div>
              </div>
            )}

            <div className="flex flex-col gap-1.5">
              {cat.candidates.map((cand) => {
                const delKey = `${cat.key}/${cand.id}`;
                return (
                  <div
                    key={cand.id}
                    className={`rounded-lg border px-3 py-2 ${
                      cand.enabled && !cand.stale
                        ? "border-slate-200 dark:border-slate-700"
                        : "border-dashed border-slate-200 opacity-60 dark:border-slate-700"
                    }`}
                  >
                    <div className="flex items-center gap-2.5">
                      <span className="text-xs text-slate-400">
                        {cand.enabled ? (cand.order ?? 0) + 1 : "—"}
                      </span>
                      <KindBadge kind={cand.kind} />
                      <span className="text-sm text-slate-800 dark:text-slate-200">{cand.label}</span>
                      <AvailBadge available={cand.available} reason={cand.reason} />
                      {cand.key_masked && (
                        <span
                          className="font-mono text-[11px] text-slate-400 dark:text-slate-500"
                          title="已保存密钥（不可见明文，编辑在模型页）"
                        >
                          {cand.key_masked}
                        </span>
                      )}
                      {cand.builtin && (
                        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-500 dark:bg-slate-700 dark:text-slate-300">
                          内置
                        </span>
                      )}
                      <span className="ml-auto flex items-center gap-1.5">
                        {(!cat.readonly || cat.order_only) && (
                          <>
                            <button
                              disabled={busy === cat.key || !cand.enabled}
                              onClick={() => reorder(cat, cand, -1)}
                              title="上调优先级"
                              className="rounded border border-slate-200 px-2 py-0.5 text-xs text-slate-500 hover:bg-slate-50 disabled:opacity-40 dark:border-slate-600 dark:hover:bg-slate-800"
                            >
                              ↑
                            </button>
                            <button
                              disabled={busy === cat.key || !cand.enabled}
                              onClick={() => reorder(cat, cand, 1)}
                              title="下调优先级"
                              className="rounded border border-slate-200 px-2 py-0.5 text-xs text-slate-500 hover:bg-slate-50 disabled:opacity-40 dark:border-slate-600 dark:hover:bg-slate-800"
                            >
                              ↓
                            </button>
                            {!cat.order_only && (
                              <>
                                <button
                                  disabled={busy === cat.key}
                                  onClick={() => toggleEnabled(cat, cand)}
                                  className={`rounded-full px-2.5 py-0.5 text-[11px] ${
                                    cand.enabled
                                      ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
                                      : "bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-300"
                                  }`}
                                >
                                  {cand.enabled ? "启用中" : "已停用"}
                                </button>
                                {!cand.builtin &&
                                  (confirmDel === delKey ? (
                                    <button
                                      disabled={busy === cat.key}
                                      onClick={() => remove(cat, cand)}
                                      title="仅从本服务移除引用，不影响模型页配置"
                                      className="rounded bg-red-500 px-2 py-0.5 text-[11px] text-white hover:bg-red-600"
                                    >
                                      确认
                                    </button>
                                  ) : (
                                    <button
                                      disabled={busy === cat.key}
                                      onClick={() => setConfirmDel(delKey)}
                                      title="仅从本服务移除引用，不影响模型页配置"
                                      className="rounded px-2 py-0.5 text-[11px] text-red-400 hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-900/30"
                                    >
                                      移除
                                    </button>
                                  ))}
                              </>
                            )}
                            {cat.order_only && cand.id === cat.effective && (
                              <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[11px] text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
                                默认
                              </span>
                            )}
                          </>
                        )}
                        {cat.readonly && !cat.order_only && (
                          <span className="text-[11px] text-slate-400 dark:text-slate-500">
                            {cand.id === cat.effective ? "默认" : ""}
                          </span>
                        )}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>

            {cat.effective && (
              <p className="text-[11px] text-slate-400 dark:text-slate-500">
                生效：{cat.candidates.find((c) => c.id === cat.effective)?.label ?? cat.effective}
                {cat.degraded_from ? "（优先级第 1 位不可用，已自动顺延）" : ""}
              </p>
            )}
          </section>
        );
      })}
    </div>
  );
}
