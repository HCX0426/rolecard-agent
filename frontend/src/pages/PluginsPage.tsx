import { useCallback, useEffect, useState } from "react";
import {
  api,
  type PluginRow,
  type ReportRecord,
  type ToolCatalog,
} from "../api";

/**
 * 插件页 = 纯领域插件。
 *
 * 概念边界（读 UI 的人最容易混淆的三件事，这里一次说清）：
 *   1. 插件 = 领域插件（domains/<id>/ 代码包，显式注册进 DOMAINS）——不是 MCP，
 *      本项目设计上不做界面动态加载/市场安装（安全取舍，见 docs/实施计划.md §2.2）。
 *   2. RAG 检索 = 内核能力（search_knowledge），不属于任何插件；角色经
 *      knowledge_scopes 授权使用（设置页 → 知识库 可查看库内容）。
 *   3. 数据随域归属：health 域的档案数据在 health 插件的详情里管理。
 * 新增一个领域插件 = 写一个 domains/<id>/ 包（models/service/tools/schema）并注册
 * 进 DOMAINS —— 代码路径在 README「架构」有说明。
 */

export default function PluginsPage() {
  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-4xl">
        <h2 className="text-base font-semibold text-slate-900">领域插件</h2>
        <p className="mt-0.5 text-xs text-slate-400">
          启停立即生效（无需重启），操作写入审计并递增全局 tool_epoch。
          插件由代码显式注册（DOMAINS）——界面只做启停，不支持动态安装。
        </p>
        <DomainPlugins />

        <div className="mt-6 rounded-xl border border-dashed border-slate-300 bg-white p-5">
          <h3 className="text-sm font-medium text-slate-700">如何新增一个领域插件？</h3>
          <ol className="mt-2 list-decimal space-y-1 pl-5 text-xs leading-relaxed text-slate-500">
            <li>
              新建 <code>src/rolecard_agent/domains/&lt;id&gt;/</code> 包：models.py /
              service.py / tools.py / schema.sql
            </li>
            <li>
              在 <code>domains/registry.py</code> 的 <code>DOMAINS</code> 追加 id，并在
              build_registry 里接线它的工具工厂（漏接线启动即报错）
            </li>
            <li>重启服务：建表、插件行、工具注册自动完成；在角色卡里为角色勾选新工具</li>
          </ol>
          <p className="mt-2 text-[11px] leading-relaxed text-slate-400">
            为什么不做界面动态安装（MCP 市场）：让运行中的 Agent 自己扩权，等于把权限边界
            交给运行时注入——显式注册是本项目权限模型的前提。RAG 检索不受此限：
            它是内核能力，按角色作用域授权（设置页 → 知识库）。
          </p>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 领域插件卡片

function DomainPlugins() {
  const [plugins, setPlugins] = useState<PluginRow[]>([]);
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [status, setStatus] = useState("");

  const load = useCallback(async () => {
    setPlugins(await api.get<PluginRow[]>("/api/plugins"));
  }, []);
  useEffect(() => {
    load().catch((e) => setStatus(`加载失败：${e.message}`));
    api.get<ToolCatalog>("/api/tools/catalog").then(setCatalog).catch(() => {});
  }, [load]);

  async function toggle(pluginId: string, enabled: boolean) {
    try {
      const r = await api.post<{ tool_epoch: number }>(
        `/api/plugins/${pluginId}/toggle`,
        { enabled },
      );
      setStatus(`${pluginId} → ${enabled ? "已启用" : "已停用"}（tool_epoch=${r.tool_epoch}）`);
      await load();
    } catch (e) {
      setStatus(`切换失败：${(e as Error).message}`);
      await load();
    }
  }

  return (
    <div className="mt-6 grid gap-3">
      {status && (
        <p className="rounded-lg bg-green-50 px-3 py-2 text-xs text-green-700">{status}</p>
      )}
      {plugins.map((p) => {
        const tools = catalog?.domains[p.plugin_id] || [];
        const isOpen = expanded === p.plugin_id;
        return (
          <div
            key={p.plugin_id}
            className="rounded-xl border border-slate-200 bg-white px-5 py-4"
          >
            <div className="flex items-center justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-medium text-slate-900">{p.display_name}</span>
                  <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">
                    {p.plugin_id}
                  </code>
                </div>
                <p className="mt-1 text-xs text-slate-400">
                  {p.enabled
                    ? `已启用：${tools.length} 个工具对模型可见`
                    : `已停用：${tools.length} 个工具立即对模型不可见（数据保留）`}
                </p>
              </div>
              <div className="flex items-center gap-3">
                {tools.length > 0 && (
                  <button
                    onClick={() => setExpanded(isOpen ? null : p.plugin_id)}
                    className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-500 hover:border-blue-300 hover:text-blue-600"
                  >
                    {isOpen ? "收起详情 ▴" : "详情（工具与数据）▾"}
                  </button>
                )}
                <button
                  onClick={() => toggle(p.plugin_id, !p.enabled)}
                  className={`relative h-6 w-11 rounded-full transition-colors ${
                    p.enabled ? "bg-green-500" : "bg-slate-300"
                  }`}
                  role="switch"
                  aria-checked={p.enabled}
                >
                  <span
                    className="absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform"
                    style={{ transform: p.enabled ? "translateX(20px)" : "none" }}
                  />
                </button>
              </div>
            </div>
            {isOpen && (
              <div className="mt-3 space-y-4 rounded-lg border border-slate-100 bg-slate-50 p-3">
                <div>
                  <p className="text-xs font-medium text-slate-500">本插件贡献的工具：</p>
                  {tools.map((t) => (
                    <div key={t.name} className="mt-1 text-xs">
                      <code className="text-slate-700">{t.name}</code>
                      <span className="ml-2 text-slate-400">{t.description}</span>
                    </div>
                  ))}
                </div>
                {p.plugin_id === "health" && (
                  <div>
                    <p className="text-xs font-medium text-slate-500">
                      本域数据管理（档案与指标，修正 / 删除写审计）：
                    </p>
                    <div className="mt-2">
                      <DataManagement compact />
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- 数据管理（随域归属）

function fmtValue(i: { index_value: number | null; value_text: string | null; unit: string | null }): string {
  if (i.index_value !== null) return `${i.index_value}${i.unit ? " " + i.unit : ""}`;
  return i.value_text || "（无）";
}

function DataManagement({ compact = false }: { compact?: boolean }) {
  const [reports, setReports] = useState<ReportRecord[]>([]);
  const [editId, setEditId] = useState<string | null>(null);
  const [draft, setDraft] = useState({ index_value: "", value_text: "", unit: "", ref_range: "", verified: false });
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);

  const load = useCallback(async () => {
    setReports(await api.get<ReportRecord[]>("/api/records"));
  }, []);
  useEffect(() => {
    load().catch((e) => setStatus({ ok: false, msg: `加载失败：${e.message}` }));
  }, [load]);

  function startEdit(indexId: string, current: { index_value: number | null; value_text: string | null; unit: string | null; ref_range: string | null; is_verified: number | boolean }) {
    setEditId(indexId);
    setDraft({
      index_value: current.index_value === null ? "" : String(current.index_value),
      value_text: current.value_text || "",
      unit: current.unit || "",
      ref_range: current.ref_range || "",
      verified: !!current.is_verified,
    });
  }

  async function saveEdit(indexId: string) {
    const changes: Record<string, unknown> = { is_verified: draft.verified };
    if (draft.index_value.trim() !== "") changes.index_value = Number(draft.index_value);
    else changes.index_value = null;
    if (draft.value_text.trim() !== "") changes.value_text = draft.value_text.trim();
    else changes.value_text = null;
    changes.unit = draft.unit.trim() || null;
    changes.ref_range = draft.ref_range.trim() || null;
    try {
      await api.patch(`/api/records/index/${indexId}`, changes);
      setEditId(null);
      setStatus({ ok: true, msg: "已保存修正（写入审计）" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `保存失败：${(e as Error).message}` });
    }
  }

  async function remove(kind: "report" | "index", id: string) {
    try {
      await api.del(`/api/records/${kind}/${id}`);
      setConfirmDel(null);
      setStatus({ ok: true, msg: "已删除（写入审计）" });
      await load();
    } catch (e) {
      setStatus({ ok: false, msg: `删除失败：${(e as Error).message}` });
    }
  }

  if (reports.length === 0) {
    return (
      <div className={`rounded-lg border border-dashed border-slate-200 bg-white text-center text-xs text-slate-400 ${compact ? "p-4" : "p-8 text-sm"}`}>
        该域还没有数据。上传 .txt/.md 文档（对话 → 上传）或运行 scripts/seed_demo_data.py 注入演示数据。
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {status && (
        <p className={`rounded-lg px-3 py-2 text-xs ${status.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-600"}`}>
          {status.msg}
        </p>
      )}
      {reports.map((r) => (
        <div key={r.report_id} className="rounded-lg border border-slate-200 bg-white">
          <div className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
            <div className="text-xs">
              <span className="font-medium text-slate-800">
                {String(r.check_time).slice(0, 10)} · {r.report_type}
              </span>
              {r.institution && <span className="ml-2 text-slate-400">{r.institution}</span>}
              {r.note && <span className="ml-2 text-slate-400">备注：{r.note}</span>}
            </div>
            {confirmDel === r.report_id ? (
              <button
                onClick={() => remove("report", r.report_id)}
                className="rounded bg-red-500 px-2 py-1 text-[11px] text-white hover:bg-red-600"
              >
                确认删除整份报告
              </button>
            ) : (
              <button
                onClick={() => setConfirmDel(r.report_id)}
                className="rounded px-2 py-1 text-[11px] text-red-400 hover:bg-red-50 hover:text-red-600"
              >
                删除报告
              </button>
            )}
          </div>
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="text-slate-400">
                <th className="px-3 py-1.5 font-medium">指标</th>
                <th className="px-3 py-1.5 font-medium">数值</th>
                <th className="px-3 py-1.5 font-medium">校验</th>
                <th className="px-3 py-1.5"></th>
              </tr>
            </thead>
            <tbody>
              {r.indices.map((i) => (
                <tr key={i.index_id} className="border-t border-slate-50">
                  {editId === i.index_id ? (
                    <>
                      <td className="px-3 py-2 font-medium text-slate-700">{i.index_name}</td>
                      <td className="px-3 py-2" colSpan={3}>
                        <div className="flex flex-wrap items-center gap-2">
                          <input
                            value={draft.index_value}
                            onChange={(e) => setDraft({ ...draft, index_value: e.target.value })}
                            placeholder="数值（可空）"
                            className="w-24 rounded border border-slate-200 px-2 py-1"
                          />
                          <input
                            value={draft.value_text}
                            onChange={(e) => setDraft({ ...draft, value_text: e.target.value })}
                            placeholder="文本值（可空）"
                            className="w-32 rounded border border-slate-200 px-2 py-1"
                          />
                          <input
                            value={draft.unit}
                            onChange={(e) => setDraft({ ...draft, unit: e.target.value })}
                            placeholder="单位"
                            className="w-20 rounded border border-slate-200 px-2 py-1"
                          />
                          <input
                            value={draft.ref_range}
                            onChange={(e) => setDraft({ ...draft, ref_range: e.target.value })}
                            placeholder="参考区间"
                            className="w-24 rounded border border-slate-200 px-2 py-1"
                          />
                          <label className="flex items-center gap-1">
                            <input
                              type="checkbox"
                              checked={draft.verified}
                              onChange={(e) => setDraft({ ...draft, verified: e.target.checked })}
                            />
                            已人工校验
                          </label>
                          <button
                            onClick={() => saveEdit(i.index_id)}
                            className="rounded bg-blue-600 px-2.5 py-1 text-white hover:bg-blue-700"
                          >
                            保存
                          </button>
                          <button
                            onClick={() => setEditId(null)}
                            className="rounded px-2 py-1 text-slate-500 hover:bg-slate-100"
                          >
                            取消
                          </button>
                        </div>
                      </td>
                    </>
                  ) : (
                    <>
                      <td className="px-3 py-2 font-medium text-slate-700">{i.index_name}</td>
                      <td className="px-3 py-2">
                        {fmtValue(i)}
                        {i.ref_range ? <span className="ml-1 text-slate-400">（参考 {i.ref_range}）</span> : null}
                        {!i.is_verified && <span className="ml-1 text-amber-600">【未经人工校验】</span>}
                      </td>
                      <td className="px-3 py-2">
                        <span className={`rounded-full px-2 py-0.5 ${i.is_verified ? "bg-green-50 text-green-700" : "bg-amber-50 text-amber-600"}`}>
                          {i.is_verified ? "已校验" : "未校验"}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button
                          onClick={() => startEdit(i.index_id, i)}
                          className="rounded px-2 py-1 text-blue-600 hover:bg-blue-50"
                        >
                          修正
                        </button>
                        {confirmDel === i.index_id ? (
                          <button
                            onClick={() => remove("index", i.index_id)}
                            className="rounded bg-red-500 px-2 py-1 text-white hover:bg-red-600"
                          >
                            确认
                          </button>
                        ) : (
                          <button
                            onClick={() => setConfirmDel(i.index_id)}
                            className="rounded px-2 py-1 text-red-400 hover:bg-red-50 hover:text-red-600"
                          >
                            删除
                          </button>
                        )}
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}
