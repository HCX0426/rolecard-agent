/** 对话页的模型行（供应商分组、上下文窗口、采样惩罚）—— 从 ChatPage 抽出。 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type BackendRow, type ModelSettings } from "../api";
import type { Tone } from "../components/Toast";

/** 模型设置里"这一页要显示的那些行"：只留**参与对话**的模型（`used_by` 含 chat，派生自
 *  服务页的引用行 —— 拆层后没有 usage 列可筛了）。读的是分组视图 `providers`，把凭据组
 *  的 provider/base_url 摊平回行上，菜单那套按 provider 分组的逻辑因此一行不用改。
 *  两条拉取路径（挂载、改窗口后刷新）共用它，state 因此只有一种形状。 */
export function chatRows(settings: ModelSettings): BackendRow[] {
  return (settings.providers ?? []).flatMap((g) =>
    g.models
      .filter((m) => m.used_by.includes("chat"))
      .map((m) => ({
        name: m.name,
        provider: g.provider,
        style: g.style,
        base_url: g.base_url,
        model: m.model,
        sort_order: 0,
        num_ctx: m.num_ctx,
        supports_vision: m.supports_vision ?? false,
        supports_tools: m.supports_tools ?? true,
        // 采样惩罚三栏原样摊平：菜单那一栏要回显"现在是多少 / 根本没设"。
        repeat_penalty: m.repeat_penalty,
        frequency_penalty: m.frequency_penalty,
        presence_penalty: m.presence_penalty,
        has_key: g.has_key,
        key_masked: g.key_masked,
      })),
  );
}

export type SamplingField = "repeat_penalty" | "frequency_penalty" | "presence_penalty";

export function useModelBackends(onStatus: (text: string, tone?: Tone) => void) {
  // 对话页只关心**参与对话**的模型行；类型直接用 `api.ts` 的 `BackendRow`，不再自造窄化
  // 形状（审计 §5）：以前挂载路径手挑 6 个字段、改窗口那条路径塞原始行 —— 同一个 state
  // 两种形状，谁先跑过决定字段在不在，`supports_tools` 这类就这样被页面"看不见"了。
  const [backends, setBackends] = useState<BackendRow[]>([]);
  // 供应商 id → 中文档称（分组标题显示"硅基流动"而非原始 id）
  const [providerLabels, setProviderLabels] = useState<Record<string, string>>({});
  const [defaultBackend, setDefaultBackend] = useState("");

  /** 重读模型设置。失败**抛出**：提示方式由调用方决定（挂载那一次静默，改窗口后要说出来）。 */
  const reload = useCallback(async (): Promise<BackendRow[]> => {
    const s = await api.get<ModelSettings>("/api/settings/models");
    const rows = chatRows(s);
    setBackends(rows);
    setDefaultBackend(s.default || rows[0]?.name || "");
    // 分组标题用供应商的中文 displayName（来自 providers 视图，不再另拉一次目录接口）
    setProviderLabels(Object.fromEntries((s.providers ?? []).map((g) => [g.provider, g.label])));
    return rows;
  }, []);

  useEffect(() => {
    reload().catch(() => {});
  }, [reload]);

  /** 设置某后端的上下文窗口（本地模型 num_ctx），保存后热重建、下一轮生效。 */
  async function setModelCtx(name: string, numCtx: number | null) {
    try {
      await api.setModelContext(name, numCtx);
      await reload();
    } catch (e) {
      onStatus(`设置上下文窗口失败：${(e as Error).message}`, "warn");
    }
  }

  /** 改一栏采样惩罚（一次只改一栏）。后端保存时已经热重建 —— 惩罚项与 temperature 同一条
   *  铁律，只能在构造期传进客户端，所以"下一轮生效"不需要重启，也不需要前端再猜。 */
  async function setModelSampling(name: string, field: SamplingField, value: number | null) {
    try {
      const stored = await api.setModelSampling(name, { [field]: value });
      // 用后端回来的那份现值更新，而不是本地假设：它同时管住了"没提交的那栏保持原样"。
      setBackends((rows) =>
        rows.map((r) =>
          r.name === stored.name
            ? {
                ...r,
                repeat_penalty: stored.repeat_penalty,
                frequency_penalty: stored.frequency_penalty,
                presence_penalty: stored.presence_penalty,
              }
            : r,
        ),
      );
    } catch (e) {
      onStatus(`设置采样惩罚失败：${(e as Error).message}`, "warn");
    }
  }

  const grouped = useMemo(() => {
    const g: Record<string, BackendRow[]> = {};
    // state 里已经只有**对话**后端（`chatRows` 在拉取边界就筛掉了），这里不再重复过滤。
    for (const b of backends) (g[b.provider] ||= []).push(b);
    return Object.entries(g).sort(([a], [z]) => a.localeCompare(z));
  }, [backends]);

  return { backends, providerLabels, defaultBackend, grouped, reload, setModelCtx, setModelSampling };
}
