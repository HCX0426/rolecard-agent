import { useCallback, useEffect, useState } from "react";
import {
  api,
  type AuditRow,
  type PluginRow,
  type ReportRecord,
  type ToolCatalog,
} from "../api";

// 页签式模块容器：新模块（数据管理 / 审计日志）往 MODULES 加一项即可 —— US-9 的可扩展性。
const MODULES = [
  { key: "domains", label: "领域插件" },
  { key: "data", label: "数据管理" },
  { key: "audit", label: "审计日志" },
] as const;

type ModuleKey = (typeof MODULES)[number]["key"];

export default function PluginsPage() {
  const [module, setModule] = useState<ModuleKey>("domains");

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto max-w-4xl">
        <h2 className="text-base font-semibold text-slate-900">插件</h2>
        <p className="mt-0.5 text-xs text-slate-400">
          领域插件启停立即生效（无需重启），操作写入审计并递增全局 tool_epoch
        </p>

        <div className="mt-4 flex gap-1 border-b border-slate-200">
          {MODULES.map((m) => (
            <button
              key={m.key}
              onClick={() => setModule(m.key)}
              className={`rounded-t-lg px-4 py-2 text-sm ${
                module === m.key
                  ? "border-b-2 border-blue-600 font-medium text-blue-700"
                  : "text-slate-500 hover:text-slate-700"
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>

        {module === "domains" && <DomainPlugins />}
        {module === "data" && <DataManagement />}
        {module === "audit" && <AuditLog />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 领域插件

function DomainPlugins() {
  const [plugins, setPlugins] = useState<PluginRow[]>([]);
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [epoch, setEpoch] = useState<number | null>(null);
  const [status, setStatus] = useState("");

  async function load() {
    setPlugins(await api.get<PluginRow[]>("/api/plugins"));
  }
  useEffect(() => {
    load().catch((e) => setStatus(`加载失败：${e.message}`));
    api.get<ToolCatalog>("/api/tools/catalog").then(setCatalog).catch(() => {});
  }, []);

  async function toggle(pluginId: string, enabled: boolean) {
    try {
      const r = await api.post<{ tool_epoch: number }>(
        `/api/plugins/${pluginId}/toggle`,
        { enabled },
      );
      setEpoch(r.tool_epoch);
      setStatus(`${pluginId} → ${enabled ? "已启用" : "已停用"}（tool_epoch=${r.tool_epoch}）`);
      await load();
    } catch (e) {
      setStatus(`切换失败：${(e as Error).message}`);
      await load();
    }
  }

  return (
    <div className="mt-6 grid gap-3">
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
                    {isOpen ? "收起工具 ▴" : "查看工具 ▾"}
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
              <div className="mt-3 space-y-1.5 rounded-lg border border-slate-100 bg-slate-50 p-3">
                <p className="text-xs font-medium text-slate-500">
                  本插件向注册表贡献的工具：
                </p>
                {tools.map((t) => (
                  <div key={t.name} className="text-xs">
                    <code className="text-slate-700">{t.name}</code>
                    <span className="ml-2 text-slate-400">{t.description}</span>
                  </div>
                ))}
                <p className="pt-1 text-[11px] leading-relaxed text-slate-400">
                  说明：知识库检索（RAG）是内核能力、不绑定任何插件（v2.1 接入）；本页的启停
                  只影响本插件自己贡献的工具。
                </p>
              </div>
            )}
          </div>
        );
      })}
      {epoch !== null && (
        <p className="rounded-lg bg-green-50 px-3 py-2 text-xs text-green-700">{status}</p>
      )}
      {status && epoch === null && (
        <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600">{status}</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 数据管理（F2）

function fmtValue(i: { index_value: number | null; value_text: string | null; unit: string | null }): string {
  if (i.index_value !== null) return `${i.index_value}${i.unit ? " " + i.unit : ""}`;
  return i.value_text || "（无）";
}

function DataManagement() {
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
      <div className="mt-6 rounded-xl border border-dashed border-slate-200 bg-white p-8 text-center text-sm text-slate-400">
        档案里没有报告。对话中上传报告（登记）或运行 scripts/seed_demo_data.py 注入演示数据。
      </div>
    );
  }

  return (
    <div className="mt-6 space-y-4">
      {status && (
        <p className={`rounded-lg px-3 py-2 text-xs ${status.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-600"}`}>
          {status.msg}
        </p>
      )}
      {reports.map((r) => (
        <div key={r.report_id} className="rounded-xl border border-slate-200 bg-white">
          <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
            <div>
              <span className="font-medium text-slate-900">
                {String(r.check_time).slice(0, 10)} · {r.report_type}
              </span>
              {r.institution && <span className="ml-2 text-xs text-slate-400">{r.institution}</span>}
              {r.note && <span className="ml-2 text-xs text-slate-400">备注：{r.note}</span>}
            </div>
            {confirmDel === r.report_id ? (
              <button
                onClick={() => remove("report", r.report_id)}
                className="rounded bg-red-500 px-2.5 py-1 text-xs text-white hover:bg-red-600"
              >
                确认删除整份报告
              </button>
            ) : (
              <button
                onClick={() => setConfirmDel(r.report_id)}
                className="rounded px-2 py-1 text-xs text-red-400 hover:bg-red-50 hover:text-red-600"
              >
                删除报告
              </button>
            )}
          </div>
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="text-slate-400">
                <th className="px-4 py-2 font-medium">指标</th>
                <th className="px-4 py-2 font-medium">数值</th>
                <th className="px-4 py-2 font-medium">参考</th>
                <th className="px-4 py-2 font-medium">校验</th>
                <th className="px-4 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {r.indices.map((i) => (
                <tr key={i.index_id} className="border-t border-slate-50">
                  {editId === i.index_id ? (
                    <>
                      <td className="px-4 py-2 font-medium text-slate-700">{i.index_name}</td>
                      <td className="px-4 py-2" colSpan={4}>
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
                      <td className="px-4 py-2 font-medium text-slate-700">{i.index_name}</td>
                      <td className="px-4 py-2">
                        {fmtValue(i)}
                        {i.ref_range ? <span className="ml-1 text-slate-400">（参考 {i.ref_range}）</span> : null}
                        {!i.is_verified && <span className="ml-1 text-amber-600">【未经人工校验】</span>}
                      </td>
                      <td className="px-4 py-2 text-slate-400">{i.source}</td>
                      <td className="px-4 py-2">
                        <span className={`rounded-full px-2 py-0.5 ${i.is_verified ? "bg-green-50 text-green-700" : "bg-amber-50 text-amber-600"}`}>
                          {i.is_verified ? "已校验" : "未校验"}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-right">
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

// ---------------------------------------------------------------- 审计日志（F3）

function AuditLog() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [status, setStatus] = useState("");

  useEffect(() => {
    api.get<AuditRow[]>("/api/audit?limit=200").then(setRows).catch((e) => setStatus(`加载失败：${e.message}`));
  }, []);

  return (
    <div className="mt-6">
      {status && <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600">{status}</p>}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="border-b border-slate-100 text-slate-400">
              <th className="px-4 py-2 font-medium">时间</th>
              <th className="px-4 py-2 font-medium">操作者</th>
              <th className="px-4 py-2 font-medium">动作</th>
              <th className="px-4 py-2 font-medium">对象</th>
              <th className="px-4 py-2 font-medium">详情</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-slate-400">
                  暂无审计记录
                </td>
              </tr>
            )}
            {rows.map((a, i) => (
              <tr key={i} className="border-b border-slate-50 last:border-0">
                <td className="whitespace-nowrap px-4 py-2 font-mono text-slate-500">
                  {String(a.ts).replace("T", " ").slice(0, 19)}
                </td>
                <td className="px-4 py-2">{a.actor}</td>
                <td className="px-4 py-2">
                  <code className="rounded bg-slate-100 px-1.5 py-0.5">{a.action}</code>
                </td>
                <td className="max-w-40 truncate px-4 py-2 font-mono text-slate-500">{a.target}</td>
                <td className="max-w-56 truncate px-4 py-2 text-slate-400" title={a.detail_json || ""}>
                  {a.detail_json}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[11px] text-slate-400">
        审计由后端在角色切换、插件启停、会话创建、数据修正/删除时写入（US-3）；本页只读。
      </p>
    </div>
  );
}
