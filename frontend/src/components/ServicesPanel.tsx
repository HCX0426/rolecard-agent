// 「服务」子页签：运行时状态视图 + 各服务引用哪些后端、以什么优先级。
// 架构归一化（引用模型）：模型页是云端配置的唯一事实面 —— 新增 = 从已配置后端中**选择**；
// 移除 = 仅从本服务优先级中摘除引用（不影响模型页配置）；配置的编辑只在模型页。
// 优先级第 1 位即生效；修改立即保存并热生效（嵌入/重排后端热重建）。
import { useCallback, useEffect, useState } from "react";
import { api, USAGE_LABEL, type ModelSettings } from "../api";
import type { ServiceCategoryView, ServiceEndpoint, ServicesView } from "../api";
import { useConfirm } from "../hooks/useConfirm";
import { describeError } from '../lib/errors';

// 载荷契约（ServiceEndpoint/ServiceCategoryView/ServicesView）住在 `api/services.ts`（`R102-22`）：
// 那里自称"与后端契约一一对应"，契约测试也只读它 —— 组件本地声明等于契约的两张脸。

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
  const [providers, setProviders] = useState<ModelSettings["providers"]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState("");
  const [adding, setAdding] = useState<string | null>(null); // 类别 key
  const [picked, setPicked] = useState<string>(""); // 选中的后端名
  const confirm = useConfirm();

  const load = useCallback(async () => {
    const [v, s] = await Promise.all([
      api.get<ServicesView>("/api/services"),
      api.get<ModelSettings>("/api/settings/models"),
    ]);
    setView(v);
    // 候选的宇宙 = 模型页的凭据组（按供应商分组显示）。用途不在这里存第二份：
    // 这一页写的是引用行，模型页读的是派生值。
    setProviders(s.providers ?? []);
  }, []);

  useEffect(() => {
    load().catch((e) => setError(`加载失败：${describeError(e)}`));
  }, [load]);

  const run = async (id: string, fn: () => Promise<void>, okMsg: string) => {
    setBusy(id);
    setError("");
    try {
      await fn();
      await load();
      setFlash(`${okMsg}· ${new Date().toLocaleTimeString()}`);
    } catch (e) {
      setError(`保存失败：${describeError(e)}`);
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

  /** 新增引用：从模型页已配置的后端中选择（不做任何配置复制）。
   *  模型推理那一节没有"引用行"可 POST —— 它的序列本身就是事实面，
   *  所以"加入对话"= 把这个名字追加到全量序里再整体写回。 */
  const addRef = (cat: ServiceCategoryView) => {
    if (!picked) {
      setError("请先选择要引用的模型");
      return;
    }
    if (cat.order_only) {
      const order = [...cat.candidates.map((c) => c.id), picked];
      run(cat.key, () => api.put(`/api/services/${cat.key}`, { order }), "已加入对话序列并热生效");
    } else {
      run(cat.key, () => api.post(`/api/services/${cat.key}/endpoints`, { ref_backend: picked }), "已引用并热生效");
    }
    setAdding(null);
    setPicked("");
  };

  /** 从对话序列里摘掉一行（配置本身留在模型页）。至少要留一行 —— 空序列等于没模型可对话。 */
  const removeFromPool = (cat: ServiceCategoryView, cand: ServiceEndpoint) => {
    const order = cat.candidates.map((c) => c.id).filter((id) => id !== cand.id);
    run(cat.key, () => api.put(`/api/services/${cat.key}`, { order }), "已移出对话序列（模型页配置保留）");
  };

  /** 移除引用：只从本服务优先级中摘除，模型页配置不受影响。 */
  const remove = (cat: ServiceCategoryView, cand: ServiceEndpoint) => {
    run(cat.key, () => api.del(`/api/services/${cat.key}/endpoints/${cand.id}`), "已从本服务移除（模型页配置保留）");
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
          云端条目 = 引用「模型」页配好的模型：在这里排顺序，改配置去模型页，移除只影响这一页的优先级。
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
        // 候选 = 模型页全部模型 − 本服务已引用的；按供应商分组显示（组头就是凭据组名，
        // 与模型页那一屏的分组口径一致，用户在两页看到的是同一套名字）。
        const referenced = new Set(cat.candidates.filter((c) => !c.builtin).map((c) => c.id));
        const selectable = providers
          .flatMap((g) => g.models.map((m) => ({ group: g.label, name: m.name, model: m.model })))
          .filter((m) => !referenced.has(m.name));
        const usedBy = (name: string) =>
          providers.flatMap((g) => g.models).find((m) => m.name === name)?.used_by ?? [];
        // 用途回显给人看的是中文类别名（与模型页同一份译名），不是内部键。
        const usedByLabel = (name: string) =>
          usedBy(name)
            .filter((u) => u !== "chat")
            .map((u) => USAGE_LABEL[u] ?? u)
            .join("、");
        return (
          <section key={cat.key} className="flex flex-col gap-2">
            <div className="flex items-baseline gap-2">
              <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">{cat.title}</h3>
              <span className="text-[11px] text-slate-400 dark:text-slate-500">{cat.hint}</span>
              {!cat.readonly && (
                <button
                  onClick={() => {
                    setAdding(adding === cat.key ? null : cat.key);
                    setPicked("");
                  }}
                  className="ml-auto shrink-0 rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 hover:text-blue-600 dark:hover:text-blue-400"
                >
                  {adding === cat.key ? "取消" : cat.order_only ? "＋ 加入对话" : "＋ 引用模型"}
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
                    <option value="">
                      {cat.order_only ? "选择要用于对话的模型…" : "选择模型页已配置的模型…"}
                    </option>
                    {providers
                      .filter((g) => g.models.some((m) => !referenced.has(m.name)))
                      .map((g) => (
                        <optgroup key={g.id} label={g.label}>
                          {g.models
                            .filter((m) => !referenced.has(m.name))
                            .map((m) => (
                              <option key={m.name} value={m.name}>
                                {m.name} · {m.model}
                              </option>
                            ))}
                        </optgroup>
                      ))}
                  </select>
                  {selectable.length === 0 && (
                    <span className="text-[11px] text-slate-400 dark:text-slate-500">
                      模型页还没有可加入的模型 —— 请先去「模型」页新增
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
                            {/* 禁用必须配一句"怎么恢复"：`title` 在禁用按钮上 Chromium 根本不弹，
                                所以说明要落在 DOM 里，而不是藏在悬浮提示里。 */}
                            {!cand.enabled && (
                              <span className="text-[10px] leading-tight text-slate-400 dark:text-slate-500">
                                该行已停用 —— 先「启用」才能排优先级
                              </span>
                            )}
                            {!cat.order_only ? (
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
                                {!cand.builtin && (
                                  <button
                                    disabled={busy === cat.key}
                                    onClick={async () => {
                                      if (await confirm({ title: "从这个服务顺序里移除这个模型？", body: "仅从本服务优先级中摘除引用，模型页配置不受影响。", confirmText: "确认移除", danger: true })) remove(cat, cand);
                                    }}
                                    title="仅从本服务移除引用，不影响模型页配置"
                                    className="rounded px-2 py-0.5 text-[11px] text-red-400 hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-900/30"
                                  >
                                    移除
                                  </button>
                                )}
                              </>
                            ) : (
                              <>
                                {/* 模型推理这一节：序列本身就是"谁用于对话"的事实面，
                                    所以摘除是一等动作（配置仍留在模型页，不是删配置）。 */}
                                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-500 dark:bg-slate-700 dark:text-slate-300">
                                  {usedByLabel(cand.id)
                                    ? `还用于：${usedByLabel(cand.id)}`
                                    : "仅用于对话"}
                                </span>
                                <button
                                  disabled={busy === cat.key || cat.candidates.length <= 1}
                                  onClick={async () => {
                                    if (await confirm({ title: `把「${cand.label}」移出对话序列？`, body: "只取消「用于对话」这一项，模型页那行配置与它的其他用途都不受影响。至少要保留一个对话模型。", confirmText: "确认移出", danger: true })) removeFromPool(cat, cand);
                                  }}
                                  title={cat.candidates.length <= 1 ? "至少要保留一个对话模型" : "移出对话序列（模型页配置保留）"}
                                  className="rounded px-2 py-0.5 text-[11px] text-red-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-40 dark:hover:bg-red-900/30"
                                >
                                  移出对话
                                </button>
                                {cat.candidates.length <= 1 && (
                                  <span className="text-[10px] leading-tight text-slate-400 dark:text-slate-500">
                                    这是唯一在册的对话模型 —— 想换先「＋ 加入对话」
                                  </span>
                                )}
                              </>
                            )}
                            {cat.order_only && cand.id === (cat.default_backend ?? cat.effective) && (
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
            {/* 没有任何可用后端可生效时，上面那句因 effective=null 不出现 —— 但这一类
                恰恰最需要一句响的（比如 local_only 下本地档全挂：这是设计好的"宁失败不上云"，
                不说出来用户只会看到一串"不可用"徽标，不知道这一轮会直接失败）。 */}
            {!cat.effective && cat.candidates.some((c) => c.enabled) && (
              <p className="rounded-lg border-l-4 border-red-400 bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-900/20 dark:text-red-300">
                当前没有可用的生效后端 —— 这一轮会**直接失败**，不会悄悄改用别的
                （链里第 1 位不可用、且后面没有可用者；见各行的「不可用」原因）。
              </p>
            )}
          </section>
        );
      })}
    </div>
  );
}
