// 「服务」子页签：运行时状态视图 + OCR/嵌入/重排的端点实例管理。
// 候选实例 = service_endpoint 行：云端行可增删改（各自 base_url/api_key/model，多账号
// 多厂商并存），本地实现行 builtin 不可删但可停用。优先级 = 行序，第 1 位即生效 ——
// 多模型比对就是调这个顺序。修改立即保存并热生效（嵌入/重排后端热重建）。
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

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
  candidates: ServiceEndpoint[];
}

interface ServicesView {
  services: ServiceCategoryView[];
}

interface EndpointDraft {
  label: string;
  base_url: string;
  api_key: string;
  model: string;
  clear_key: boolean;
}

const EMPTY_DRAFT: EndpointDraft = { label: "", base_url: "", api_key: "", model: "", clear_key: false };

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

const inputCls =
  "rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 py-1 text-xs outline-none focus:border-blue-400";

/** 各服务类别的云端新增表单占位文案（引导填什么）。 */
const CLOUD_HINTS: Record<string, { key: string; model?: string }> = {
  ocr: { key: "云端 OCR API Key（如 OCR.space）" },
  embedding: { key: "嵌入服务 API Key", model: "嵌入模型名（默认 BAAI/bge-m3）" },
  rerank: { key: "重排服务 API Key", model: "重排模型名（默认 BAAI/bge-reranker-v2-m3）" },
};

export function ServicesPanel() {
  const [view, setView] = useState<ServicesView | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState("");
  const [adding, setAdding] = useState<string | null>(null); // 类别 key
  const [draft, setDraft] = useState<EndpointDraft>(EMPTY_DRAFT);
  const [editing, setEditing] = useState<string | null>(null); // `${key}/${id}`
  const [editDraft, setEditDraft] = useState<EndpointDraft>(EMPTY_DRAFT);
  const [confirmDel, setConfirmDel] = useState<string | null>(null);

  const load = useCallback(async () => {
    setView(await api.get<ServicesView>("/api/services"));
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

  const saveEdit = (cat: ServiceCategoryView, cand: ServiceEndpoint) => {
    const body: Record<string, unknown> = {
      label: editDraft.label.trim(),
      base_url: editDraft.base_url.trim() || null,
      model: editDraft.model.trim() || null,
    };
    if (editDraft.clear_key) body.api_key = ""; // 显式清除
    else if (editDraft.api_key.trim() !== "") body.api_key = editDraft.api_key.trim(); // 不带 = 保留
    run(cat.key, () => api.patch(`/api/services/${cat.key}/endpoints/${cand.id}`, body), "端点已更新");
    setEditing(null);
  };

  const remove = (cat: ServiceCategoryView, cand: ServiceEndpoint) => {
    run(cat.key, () => api.del(`/api/services/${cat.key}/endpoints/${cand.id}`), "端点已删除");
    setConfirmDel(null);
  };

  const submitAdd = (cat: ServiceCategoryView) => {
    if (!draft.label.trim()) {
      setError("端点名称必填");
      return;
    }
    run(
      cat.key,
      () =>
        api.post(`/api/services/${cat.key}/endpoints`, {
          label: draft.label.trim(),
          base_url: draft.base_url.trim() || null,
          api_key: draft.api_key.trim() || null,
          model: draft.model.trim() || null,
        }),
      "端点已新增",
    );
    setAdding(null);
    setDraft(EMPTY_DRAFT);
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
          云端服务可添加多个实例（不同账号 / 厂商），按优先级依次兜底 —— <b>第 1 位即生效</b>，
          调前几位的顺序即可做多模型比对。修改立即保存并热生效。
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
        const hint = CLOUD_HINTS[cat.key];
        return (
          <section key={cat.key} className="flex flex-col gap-2">
            <div className="flex items-baseline gap-2">
              <h3 className="text-sm font-medium text-slate-900 dark:text-slate-100">{cat.title}</h3>
              <span className="text-[11px] text-slate-400 dark:text-slate-500">{cat.hint}</span>
              {!cat.readonly && (
                <button
                  onClick={() => {
                    setAdding(adding === cat.key ? null : cat.key);
                    setDraft(EMPTY_DRAFT);
                  }}
                  className="ml-auto shrink-0 rounded-lg border border-slate-200 dark:border-slate-700 px-2.5 py-1 text-xs text-slate-600 dark:text-slate-300 hover:border-blue-300 hover:text-blue-600 dark:hover:text-blue-400"
                >
                  {adding === cat.key ? "取消" : "＋ 新增云端"}
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
                  <input
                    value={draft.label}
                    onChange={(e) => setDraft({ ...draft, label: e.target.value })}
                    placeholder="名称 *（如 OCR.space 小号）"
                    className={`${inputCls} w-44`}
                  />
                  <input
                    value={draft.base_url}
                    onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
                    placeholder="Base URL（可选，默认官方端点）"
                    className={`${inputCls} w-56`}
                  />
                  <input
                    type="password"
                    value={draft.api_key}
                    onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
                    placeholder={hint?.key ?? "API Key"}
                    className={`${inputCls} w-52`}
                  />
                  {hint?.model && (
                    <input
                      value={draft.model}
                      onChange={(e) => setDraft({ ...draft, model: e.target.value })}
                      placeholder={hint.model}
                      className={`${inputCls} w-56`}
                    />
                  )}
                  <button
                    disabled={busy === cat.key}
                    onClick={() => submitAdd(cat)}
                    className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
                  >
                    保存
                  </button>
                </div>
              </div>
            )}

            <div className="flex flex-col gap-1.5">
              {cat.candidates.map((cand) => {
                const editKey = `${cat.key}/${cand.id}`;
                const editable = !cand.builtin;
                return (
                  <div
                    key={cand.id}
                    className={`rounded-lg border px-3 py-2 ${
                      cand.enabled
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
                          title="已保存密钥（不可见明文）"
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
                        {!cat.readonly && (
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
                            {editable && (
                              <>
                                <button
                                  disabled={busy === cat.key}
                                  onClick={() => {
                                    setEditing(editing === editKey ? null : editKey);
                                    setEditDraft({
                                      label: cand.label,
                                      base_url: cand.base_url || "",
                                      api_key: "",
                                      model: cand.model || "",
                                      clear_key: false,
                                    });
                                  }}
                                  className="rounded px-2 py-0.5 text-[11px] text-blue-600 hover:bg-blue-50 dark:text-blue-400 dark:hover:bg-blue-900/30"
                                >
                                  编辑
                                </button>
                                {confirmDel === editKey ? (
                                  <button
                                    disabled={busy === cat.key}
                                    onClick={() => remove(cat, cand)}
                                    className="rounded bg-red-500 px-2 py-0.5 text-[11px] text-white hover:bg-red-600"
                                  >
                                    确认
                                  </button>
                                ) : (
                                  <button
                                    disabled={busy === cat.key}
                                    onClick={() => setConfirmDel(editKey)}
                                    className="rounded px-2 py-0.5 text-[11px] text-red-400 hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-900/30"
                                  >
                                    删除
                                  </button>
                                )}
                              </>
                            )}
                          </>
                        )}
                        {cat.readonly && (
                          <span className="text-[11px] text-slate-400 dark:text-slate-500">
                            {cand.id === cat.effective ? "默认" : ""}
                          </span>
                        )}
                      </span>
                    </div>
                    {editing === editKey && (
                      <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-2 dark:border-slate-700">
                        <input
                          value={editDraft.label}
                          onChange={(e) => setEditDraft({ ...editDraft, label: e.target.value })}
                          placeholder="名称"
                          className={`${inputCls} w-44`}
                        />
                        <input
                          value={editDraft.base_url}
                          onChange={(e) => setEditDraft({ ...editDraft, base_url: e.target.value })}
                          placeholder="Base URL（可选）"
                          className={`${inputCls} w-56`}
                        />
                        <input
                          type="password"
                          value={editDraft.api_key}
                          onChange={(e) => setEditDraft({ ...editDraft, api_key: e.target.value })}
                          placeholder={cand.key_masked ? `已存 ${cand.key_masked}（留空保留）` : "API Key"}
                          className={`${inputCls} w-52`}
                        />
                        <input
                          value={editDraft.model}
                          onChange={(e) => setEditDraft({ ...editDraft, model: e.target.value })}
                          placeholder="模型名（可选）"
                          className={`${inputCls} w-52`}
                        />
                        {cand.key_masked && (
                          <label className="flex items-center gap-1 text-[11px] text-slate-500 dark:text-slate-400">
                            <input
                              type="checkbox"
                              checked={editDraft.clear_key}
                              onChange={(e) => setEditDraft({ ...editDraft, clear_key: e.target.checked })}
                            />
                            清除已存密钥
                          </label>
                        )}
                        <button
                          disabled={busy === cat.key}
                          onClick={() => saveEdit(cat, cand)}
                          className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
                        >
                          保存
                        </button>
                        <button
                          onClick={() => setEditing(null)}
                          className="rounded px-2 py-1 text-xs text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700/50"
                        >
                          取消
                        </button>
                      </div>
                    )}
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
